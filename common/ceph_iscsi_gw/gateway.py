#!/usr/bin/env python

__author__ = 'paul'
import os
import socket

from rtslib_fb.target import Target, TPG, NetworkPortal, LUN
from rtslib_fb.fabric import ISCSIFabricModule
from rtslib_fb import root
from rtslib_fb.utils import RTSLibError

from ceph_iscsi_gw.utils import ipv4_addresses
from ceph_iscsi_gw.common import Config


class GWTarget(object):
    """
    Class representing the state of the local LIO environment
    """

    def __init__(self, logger, iqn, gateway_ip_list):
        """
        Instantiate the class
        :param iqn: iscsi iqn name for the gateway
        :param gateway_ip_list: list of IP addresses to be defined as portals to LIO
        :return: gateway object
        """

        self.error = False
        self.error_msg = ''

        self.logger = logger                # logger object

        self.iqn = iqn

        # if the ip list provided doesn't match any ip of this host, abort
        # the assumption here is that we'll only have one matching ip in the list!
        matching_ip = set(gateway_ip_list).intersection(ipv4_addresses())
        if not matching_ip:
            self.error = True
            self.error_msg = "gateway IP addresses provided do not match any ip on this host"

        self.active_portal_ip = list(matching_ip)[0]
        self.logger.debug("active portal will use {}".format(self.active_portal_ip))

        self.gateway_ip_list = gateway_ip_list
        self.logger.debug("tpg's will be defined in this order - {}".format(self.gateway_ip_list))

        self.changes_made = False
        # self.portal = None
        self.target = None
        self.tpg = None
        self.tpg_list = []

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
            self.logger.debug("(Gateway.create_target) Added iscsi target - {}".format(self.iqn))

            # tpg's are defined in the sequence provide by the gateway_ip_list, so across multiple gateways the
            # same tpg number will be associated with the same IP - however, only the tpg with an IP on the host
            # will be in an enabled state. The other tpgs are necessary for systems like ESX who issue a rtpg scsi
            # inquiry only to one of the gateways - so that gateway must provide details for the whole configuration
            self.logger.debug("Creating tpgs")
            for ip in self.gateway_ip_list:

                tpg = TPG(self.target)
                NetworkPortal(tpg, ip)
                self.logger.debug("(Gateway.create_target) Added tpg for portal ip {}".format(ip))
                if ip == self.active_portal_ip:
                    tpg.enable = True
                    self.logger.debug("(Gateway.create_target) Added tpg for portal ip {} is enabled".format(ip))
                else:
                    tpg.enable = False
                    self.logger.debug("(Gateway.create_target) Added tpg for portal ip {} is disabled".format(ip))

                self.tpg_list.append(tpg)

        except RTSLibError as err:
            self.error_msg = err
            self.error = True
            self.delete()

        self.changes_made = True
        self.logger.info("(Gateway.create_target) created an iscsi target with iqn of '{}'".format(self.iqn))

    def load_config(self):
        """
        Grab the target, tpg and portal objects from LIO and store in this Gateway object
        """

        try:

            lio_root = root.RTSRoot()
            # since we only support one target, we just grab the first iterable
            self.target = lio_root.targets.next()
            # but there could/should be multiple tpg's for the target
            for tpg in self.target.tpgs:
                self.tpg_list.append(tpg)

            # self.portal = self.tpg.network_portals.next()

        except RTSLibError as err:
            self.error_msg = err
            self.error = True

        self.logger.info("(Gateway.load_config) successfully loaded existing target definition")

    def map_luns(self):
        """
        LIO will have blockstorage objects already defined by the igw_lun module, so this
        method, brings those objects into the gateways TPG
        """

        lio_root = root.RTSRoot()
        # process each storage object added to the gateway, and map to the tpg
        for stg_object in lio_root.storage_objects:

            for tpg in self.tpg_list:

                if not self.lun_mapped(tpg, stg_object):

                    # use the iblock number for the lun id - /sys/kernel/config/target/core/iblock_1/ansible4
                    #                                                                              ^
                    lun_id = int(stg_object._path.split('/')[-2].split('_')[1])

                    try:
                        mapped_lun = LUN(tpg, lun=lun_id, storage_object=stg_object)
                        self.changes_made = True
                    except RTSLibError as err:
                        self.error = True
                        self.error_msg = err
                        break

    def lun_mapped(self, tpg, storage_object):
        """
        Check to see if a given storage object (i.e. block device) is already mapped to the gateway's TPG
        :param storage_object: storage object to look for
        :return: boolean - is the storage object mapped or not
        """

        mapped_state = False
        for l in tpg.luns:
            if l.storage_object.name == storage_object.name:
                mapped_state = True
                break

        return mapped_state

    def delete(self):
        self.target.delete()

    def manage(self, mode):
        """
        Manage the definition of the gateway, given a mode of 'target' or 'map'. In 'target' mode the
        LIO TPG is defined, whereas in map mode, the required LUNs are added to the existing TPG
        :param mode: run mode - target or map (str)
        :return: None - but sets the objects error flags to be checked by the caller
        """
        config = Config(self.logger)
        if config.error:
            self.error = True
            self.error_msg = config.error_msg
            return

        if mode == 'target':

            if self.exists():
                self.load_config()
            else:
                self.create_target()

            # ensure that the config object has an entry for this gateway
            this_host = socket.gethostname().split('.')[0]

            gateway_group = config.config["gateways"].keys()

            # this action could be carried out by multiple nodes concurrently, but since the value
            # is the same (i.e all gateway nodes use the same iqn) it's not worth worrying about!
            if "iqn" not in gateway_group:
                config.add_item("gateways", "iqn", initial_value=self.iqn)

            if this_host not in gateway_group:
                inactive_portal_ip = self.gateway_ip_list
                inactive_portal_ip.remove(self.active_portal_ip)
                gateway_metadata = {"portal_ip_address": self.active_portal_ip,
                                    "iqn": self.iqn,
                                    "active_luns": 0,
                                    "tpgs:": len(self.tpg_list),
                                    "inactive_portal_ips": inactive_portal_ip}

                config.add_item("gateways", this_host)
                config.update_item("gateways", this_host, gateway_metadata)
                config.commit()

        elif mode == 'map':

            if self.exists():

                self.load_config()

                self.map_luns()

            else:
                self.error = True
                self.error_msg = ("Attempted to map to a gateway '{}' that hasn't been defined yet..."
                                  "out of order steps?".format(self.iqn))



