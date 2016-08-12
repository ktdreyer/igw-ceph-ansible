#!/usr/bin/env python
__author__ = 'pcuzner@redhat.com'


import os
import netifaces
import logging
import socket
from logging.handlers import RotatingFileHandler
from ansible.module_utils.basic import *

from rtslib_fb.target import Target, TPG, NetworkPortal, LUN
from rtslib_fb.fabric import ISCSIFabricModule
from rtslib_fb import root
from rtslib_fb.utils import RTSLibError

from ceph_iscsi_gw.common import Config

class Gateway(object):

    def __init__(self, name, portal_interface):
        self.iqn = "iqn.2003-01.com.redhat.iscsi-gw:{}".format(name)
        self.portal_interface = portal_interface
        self.ip_address = get_ip(portal_interface)
        self.error = False
        self.error_msg = ''
        self.type = Config.get_platform()
        self.changes_made = False
        self.portal = None
        self.target = None
        self.tpg = None

    def exists(self):
        return os.path.exists('/sys/kernel/config/target/iscsi/{}'.format(self.iqn))

    def create_target(self):

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
        mapped_state = False
        for l in self.tpg.luns:
            if l.storage_object.name == storage_object.name:
                mapped_state = True
                break

        return mapped_state

    def delete(self):
        self.target.delete()


def get_ip(interface):
    try:
        ip = netifaces.ifaddresses(interface)[2][0]['addr']
    except ValueError:
        ip = '0.0.0.0'
    return ip


def main():
    # Configures the gateway on the host. All images defined are added to
    # the default tpg for later allocation to clients
    fields = {"gateway_name": {"required": False, "type": "str", "default": "ceph-igw"},
              "portal_interface": {"required": True, "type": "str"}
              }

    module = AnsibleModule(argument_spec=fields,
                           supports_check_mode=False)

    gateway_name = module.params['gateway_name']
    portal_interface = module.params['portal_interface']

    logger.info("START - GATEWAY configuration started")

    gateway = Gateway(gateway_name, portal_interface)
    if gateway.exists():
        gateway.load_config()
    else:
        gateway.create_target()

    if gateway.error:
        logger.critical("(main) Gateway creation or load failed, unable to continue")
        module.fail_json(msg="iSCSI gateway creation/load failure ({})".format(gateway.error_msg))
    else:
        # ensure that the config has an entry for this gateway
        this_host = socket.gethostname().split('.')[0]
        config = Config(logger)
        if config.error:
            module.fail_json(msg=config.error_msg)
        else:
            gateway_metadata = {"ip_address": gateway.ip_address}
            config.add_item("gateways", this_host)
            config.update_item("gateways", this_host, gateway_metadata)
            config.commit()

    gateway.map_luns()
    if gateway.error:
        logger.critical("(main) LUN mapping to the tpg failed, unable to continue")
        module.fail_json(msg="iSCSI LUN mapping to tpg1 failed ({})".format(gateway.error_msg))

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
