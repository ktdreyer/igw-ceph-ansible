#!/usr/bin/env python

__author__ = 'pcuzner@redhat.com'

import logging

from logging.handlers import RotatingFileHandler
from ansible.module_utils.basic import *
from rtslib_fb.root import root
from ceph_iscsi_gw import Config


def main():

    fields = {
        "mode": {
            "required": True,
            "default": "prepare",
            "choices": ["prepare", "commit"],
            "type": "str"
            },
        "host": {"required": False, "type": "str"}
        }

    config = Config()
    if config.error:
        pass


    # change count = 0
    # if mode is prepare and host is null
    #   error - can't run a prepare without a host parameter

    # if mode is prepare
    #   if host is this host
    #       lock the config .... NEED A LOCK / UNLOCK METHOD
    #       rebalance the disks .. FUNCTION
    #       writefull from the new config dict
    #       unlock the config
    #   else
    #       set msg = nothing to do, skipping

    # else if mode is commit
    #   process the disks
    #       if the disk has an owner = this host
    #           set preferred on this LUN
    #           increment change count
    #       else skip this disk
    #   msg = X configuration changes made

    # closing message

    pass


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
