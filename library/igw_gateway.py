#!/usr/bin/env python
__author__ = 'pcuzner@redhat.com'


import os
import logging
import socket
import netaddr
import netifaces
import struct

from logging.handlers import RotatingFileHandler
from ansible.module_utils.basic import *

from rtslib_fb.target import Target, TPG, NetworkPortal, LUN
from rtslib_fb.fabric import ISCSIFabricModule
from rtslib_fb import root
from rtslib_fb.utils import RTSLibError

from ceph_iscsi_gw.common import Config


def valid_cidr(subnet):
    """
    Confirm whether a given cidr is valid
    :param subnet: string of the form ip_address/netmask
    :return: Boolean representing when the CIDR passed is valid
    """

    try:
        ip, s_mask = subnet.split('/')
        netmask = int(s_mask)
        if not 1 <= netmask <= 32:
            raise ValueError
        ip_as_long = struct.unpack('!L', socket.inet_aton(ip))[0]
    except ValueError:
        # netmask is invalid
        return False
    except socket.error:
        # illegal ip address component
        return False

    # at this point the ip and netmask are ok to use, so return True
    return True


class Gateway(object):
    """
    Class representing the state of the local LIO environment
    """

    def __init__(self, iqn, iscsi_network):
        """
        Instantiate the class
        :param iqn: iscsi iqn name for the gateway
        :param iscsi_network: network subnet to bind to (i.e. use for the portal IP)
        :return: gateway object
        """

        self.error = False
        self.error_msg = ''

        self.iqn = iqn

        self.ip_address = get_ip_address(iscsi_network)
        if not self.ip_address:
            self.error = True
            self.error_msg = ("Unable to find an IP on this host, that matches"
                              " the iscsi_network setting {}".format(iscsi_network))

        self.type = Config.get_platform()
        self.changes_made = False
        self.portal = None
        self.target = None
        self.tpg = None

    def exists(self):
        """
        Basic check to see whether this iqn already exists in kernel's configFS directory
        :return: boolean
        """

        return os.path.exists('/sys/kernel/config/target/iscsi/{}'.format(self.iqn))

    def create_target(self):
        """
        Add an iSCSI target to LIO with this objects iqn name, and bind to the IP that
        aligns with the given iscsi_network
        """

        try:
            iscsi_fabric = ISCSIFabricModule()
            self.target = Target(iscsi_fabric, wwn=self.iqn)
            logger.debug("(Gateway.create_target) Added iscsi target - {}".format(self.iqn))
            self.tpg = TPG(self.target)
            logger.debug("(Gateway.create_target) Added tpg")
            self.tpg.enable = True
            self.portal = NetworkPortal(self.tpg, self.ip_address)
            logger.debug("(Gateway.create_target) Added portal IP '{}' to tpg".format(self.ip_address))
        except RTSLibError as err:
            self.error_msg = err
            self.error = True
            self.delete()

        self.changes_made = True
        logger.info("(Gateway.create_target) created an iscsi target with iqn of '{}'".format(self.iqn))

    def load_config(self):
        """
        Grab the target, tpg and portal objects from LIO and store in this Gateway object
        """

        try:
            # since we only support one target/TPG, we just grab the first iterable
            lio_root = root.RTSRoot()
            self.target = lio_root.targets.next()
            self.tpg = self.target.tpgs.next()
            self.portal = self.tpg.network_portals.next()

        except RTSLibError as err:
            self.error_msg = err
            self.error = True

        logger.info("(Gateway.load_config) successfully loaded existing target definition")

    def map_luns(self):
        """
        LIO will have blockstorage objects already defined by the igw_lun module, so this
        method, brings those objects into the gateways TPG
        """

        lio_root = root.RTSRoot()
        # process each storage object added to the gateway, and map to the tpg
        for stg_object in lio_root.storage_objects:
            if not self.lun_mapped(stg_object):

                # use the iblock number for the lun id - /sys/kernel/config/target/core/iblock_1/ansible4
                #                                                                              ^
                lun_id = int(stg_object._path.split('/')[-2].split('_')[1])

                try:
                    mapped_lun = LUN(self.tpg, lun=lun_id, storage_object=stg_object)
                    self.changes_made = True
                except RTSLibError as err:
                    self.error = True
                    self.error_msg = err
                    break

    def lun_mapped(self, storage_object):
        """
        Check to see if a given storage object (i.e. block device) is already mapped to the gateway's TPG
        :param storage_object: storage object to look for
        :return: boolean - is the storage object mapped or not
        """

        mapped_state = False
        for l in self.tpg.luns:
            if l.storage_object.name == storage_object.name:
                mapped_state = True
                break

        return mapped_state

    def delete(self):
        self.target.delete()


def ipv4_addresses():
    """
    Generator function providing ipv4 network addresses on this host
    :return: IP address - dotted quad format
    """

    for iface in netifaces.interfaces():
        for link in netifaces.ifaddresses(iface)[netifaces.AF_INET]:
            yield link['addr']


def get_ip_address(iscsi_network):
    """
    Return an IP address assigned to the running host that matches the given
    subnet address. This IP becomes the portal IP for the target portal group
    :param iscsi_network: cidr network address
    :return: IP address, or '' if the host does not have an interface on the required subnet
    """

    ip = ''
    subnet = netaddr.IPSet([iscsi_network])
    target_ip_range = [str(ip) for ip in subnet]   # list where each element is an ip address

    for local_ip in ipv4_addresses():
        if local_ip in target_ip_range:
            ip = local_ip
            break

    return ip


def main():
    # Configures the gateway on the host. All images defined are added to
    # the default tpg for later allocation to clients
    fields = {"gateway_iqn": {"required": True, "type": "str"},
              "iscsi_network": {"required": True, "type": "str"},
              "mode": {
                  "required": True,
                  "choices": ['target', 'map']
                  }
              }

    module = AnsibleModule(argument_spec=fields,
                           supports_check_mode=False)

    gateway_iqn = module.params['gateway_iqn']
    iscsi_network = module.params['iscsi_network']
    mode = module.params['mode']

    if not valid_cidr(iscsi_network):
        module.fail_json(msg="Invalid 'iscsi_network' provided - must use CIDR notation of a.b.c.d/nn")

    logger.info("START - GATEWAY configuration started in mode {}".format(mode))

    gateway = Gateway(gateway_iqn, iscsi_network)

    if mode == 'target':

        if gateway.exists():
            gateway.load_config()
        else:
            gateway.create_target()

        if gateway.error:
            logger.critical("(main) Gateway creation or load failed, unable to continue")
            module.fail_json(msg="iSCSI gateway creation/load failure ({})".format(gateway.error_msg))
        else:
            # ensure that the config object has an entry for this gateway
            this_host = socket.gethostname().split('.')[0]
            config = Config(logger)
            if config.error:
                module.fail_json(msg=config.error_msg)
            else:
                gateway_group = config.config["gateways"].keys()

                # this action could be carried out by multiple nodes concurrently, but since the value
                # is the same it's not worthwhile looking into methods for serialising
                if "iqn" not in gateway_group:
                    config.add_item("gateways", "iqn", initial_value=gateway.iqn)

                if this_host not in gateway_group:
                    gateway_metadata = {"portal_ip_address": gateway.ip_address,
                                        "iqn": gateway.iqn,
                                        "active_luns": 0}

                    config.add_item("gateways", this_host)
                    config.update_item("gateways", this_host, gateway_metadata)
                    config.commit()

    elif mode == 'map':

        # assume that if the iqn exists, we put it there, so the config object is OK
        if gateway.exists():

            gateway.load_config()

            gateway.map_luns()

            if gateway.error:
                logger.critical("(main) LUN mapping to the tpg failed, unable to continue")
                module.fail_json(msg="iSCSI LUN mapping to tpg1 failed ({})".format(gateway.error_msg))
        else:
            module.fail_json(msg="Attempted to map to a gateway '{}' that hasn't been defined yet..."
                                 "out of order steps?".format(gateway_iqn))

    logger.info("END - GATEWAY configuration complete")
    module.exit_json(changed=gateway.changes_made, meta={"msg": "Gateway setup complete"})


if __name__ == '__main__':

    module_name = os.path.basename(__file__).replace('ansible_module_', '')
    logger = logging.getLogger(os.path.basename(module_name))
    logger.setLevel(logging.DEBUG)
    handler = RotatingFileHandler('/var/log/ansible-module-igw_config.log',
                                  maxBytes=5242880,
                                  backupCount=7)
    log_fmt = logging.Formatter('%(asctime)s %(name)s %(levelname)-8s : %(message)s')
    handler.setFormatter(log_fmt)
    logger.addHandler(handler)

    main()
