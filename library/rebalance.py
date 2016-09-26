#!/usr/bin/env python

__author__ = 'pcuzner@redhat.com'

# may need to separate this into a different playbook due to the path rebalance
# affect on client connections...needs testing - Windows/RHEL/Ubuntu clients

import logging

from logging.handlers import RotatingFileHandler
from ansible.module_utils.basic import *

from rtslib_fb import root
from rtslib_fb.utils import fwrite

from ceph_iscsi_config.common import Config

# function set_alua_state <- a .. active, s .. standby
# input image name, required state
# build path to alua_access_state
# update state


# rebalance function logic - prepare the plan
# find the number of gateways in the configuration
# sort the list of gateways by name
# get the list of disks
# num_gateways is len keys in gateways element of config (-1)
# gateway ptr = 0
# for each disk
#   get current config for this disk, update the metadata
#   set disk owner to gateway [ ptr ]
#   ptr ++
#   if ptr > num gateways
#       reset the ptr = 0

# Set disk path function
# for each disk in the config
#   if the disk has an owner for this host
#        set_preferred for this disk
#   else
#       set_standby state for this disk


def main():

    fields = {
        "mode": {
            "required": True,
            "default": "prepare",
            "choices": ["prepare", "commit"],
            "type": "str"
            },
        "host": {"required": False, "type": "str"},
        "gateway_iqn": {"required": True, "type": "str"}
        }

    module = AnsibleModule(argument_spec=fields,
                           supports_check_mode=False)

    mode = module.params['mode']
    tgt_host = module.params['host']
    gateway_iqn = module.params['gateway_iqn']

    changes_made = False

    config = Config()
    logger.info("START - REBALANCE process started in {} mode".format(mode))
    if config.error:
        # get out
        pass

    # change count = 0
    # if mode is prepare and host is null
    #   error - can't run a prepare without a host parameter

    # if mode is prepare and there are disks in the config to balance
    #   if host is this host
    #       lock the config
    #       rebalance the disks .. FUNCTION
    #       commit the new config
    #       unlock the config
    #   else
    #       set msg = nothing to do, skipping

    # elif mode is commit and there are disks in the config to balance
    #   process the disks
    #       if the disk has an owner = this host
    #           set preferred on this LUN
    #           increment change count
    #       else skip this disk
    #   msg = X configuration changes made

    logger.info("END  - REBALANCE configuration complete")
    module.exit_json(changed=changes_made, meta={"msg": "LUN rebalance mode '{}' complete".format(mode)})

if __name__ == "__main__":

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
