#!/usr/bin/env python

__author__ = 'pcuzner@redhat.com'

import json
import logging
from logging.handlers import RotatingFileHandler

from socket import gethostname
from time import sleep
import os
import rados
import rbd

from ansible.module_utils.basic import *
from rtslib_fb import BlockStorageObject, root
from rtslib_fb.utils import RTSLibError, fwrite, fread

from ceph_iscsi_gw.common import Config

SIZE_SUFFIXES = ['M', 'G', 'T']
CEPH_CONF = '/etc/ceph/ceph.conf'
KEYRING = '/etc/ceph/ceph.client.admin.keyring'

# remove this list, once the rbd handling works through the rbd module
# RBD_FEATURES = ['--image-format 2',
#                 '--image-shared',
#                 '--image-feature layering']

# RBD_FEATURE_LIST lists the features needs for an rbd image to be exported correctly via
# LIO to iSCSI clients
RBD_FEATURE_LIST = ['RBD_FEATURE_LAYERING']

TIME_OUT_SECS = 30
LOOP_DELAY = 2


def convert_2_bytes(disk_size):
    power = [2, 3, 4]
    unit = disk_size[-1]
    offset = SIZE_SUFFIXES.index(unit)
    value = int(disk_size[:-1])     # already validated, so no need for try/except clause

    _bytes = value*(1024**power[offset])

    return _bytes


def valid_size(size):
    valid = True
    unit = size[-1]

    if unit.upper() not in SIZE_SUFFIXES:
        valid = False
    else:
        try:
            value = int(size[:-1])
        except ValueError:
            valid = False

    return valid


def get_rbd_map(module, image, pool):
    changed = False
    # Now look at mapping of the device - which would execute on all target hosts
    map_cmd = 'rbd showmapped --format=json'
    rc, map_out, err = module.run_command(map_cmd)
    if rc != 0:
        module.fail_json(msg="failed to execute {}".format(map_cmd))

    map_device = rbd_mapped(map_out, image, pool)

    if not map_device:
        # not mapped, so map it
        map_cmd = 'rbd map {}/{}'.format(pool, image)
        rc, map_device, err = module.run_command(map_cmd)
        if rc != 0:
            module.fail_json(msg="map of {}/{} failed".format(pool, image))
        map_device = map_device.rstrip()
        changed = True

    return changed, map_device


def rbd_mapped(rbd_map_output, image, pool='rbd'):
    device = ''
    mapped_rbds = json.loads(rbd_map_output)
    for rbd_id in mapped_rbds:
        if (mapped_rbds[rbd_id]['name'] == image and
                mapped_rbds[rbd_id]['pool'] == pool):
            device = mapped_rbds[rbd_id]['device']
            break
    return device.rstrip()


def lun_in_lio(image):
    found_it = False
    rtsroot = root.RTSRoot()
    for stg_object in rtsroot.storage_objects:
        if stg_object.name == image:
            found_it = True
            break

    return stg_object if found_it else None


def rbd_create(image, size, pool):
    """
    Create an rbd image compatible with exporting through LIO to multiple clients
    :param image: image name (str)
    :param size: size of the image to create nX - where n is an int, and X is the unit M,G,T
    :param pool: pool (str) to use for the allocation
    :return: status code and msg
    """
    rc = 0
    msg = ''
    size_bytes = convert_2_bytes(size)

    # build the required feature settings into an int
    feature_int = 0
    for feature in RBD_FEATURE_LIST:
        feature_int += getattr(rbd, feature)

    with rados.Rados(conffile=CEPH_CONF) as cluster:
        with cluster.open_ioctx(pool) as ioctx:
            rbd_inst = rbd.RBD()
            try:
                rbd_inst.create(ioctx, image, size_bytes, features=feature_int, old_format=False)
            except (rbd.ImageExists, rbd.InvalidArgument) as err:
                rc = 12
                msg = "Failed to create rbd image {} in pool {} : {}".format(image,
                                                                             pool,
                                                                             err)
    return rc, msg


def rbd_add_device(module, image, device_path, in_wwn=None):
    """
    Add an rbd device to the LIO configuration
    :param module: ansible module used to run CLI commands
    :param image: rbd image name (str)
    :param device_path: path for the device e.g. /dev/rbdX
    :param in_wwn: optional wwn identifying the rbd image to clients - must match across gateways
    :return: LIO LUN object
    """

    logger.info("(add_device) Adding image '{}' with path {} to LIO".format(image, device_path))
    new_lun = None
    try:
        new_lun = BlockStorageObject(name=image, dev=device_path, wwn=in_wwn)
        set_alua(new_lun, "standby")
    except RTSLibError as err:
        module.fail_json(msg="failed to add {} to LIO - error({})".format(image, str(err)))

    return new_lun


def rbd_size(image, reqd_size, pool):
    """
    Confirm that the existing rbd image size, matches the requirement passed in the ansible
    config file - if the required size is > than current, resize the rbd image to match
    :param image: rbd image name (str)
    :param reqd_size: size (str) of the form nX - where n is integer, and X the unit M, G, T
    :param pool: pool (str) where the rbd images exists
    :return: boolean value reflecting whether the rbd image was resized
    """

    changes_made = False

    with rados.Rados(conffile=CEPH_CONF) as cluster:
        with cluster.open_ioctx(pool) as ioctx:
            with rbd.Image(ioctx, image) as rbd_image:

                logger.debug('rbd image {} opened OK'.format(image))

                # get the current size in bytes
                current_bytes = rbd_image.size()     # bytes
                target_bytes = convert_2_bytes(reqd_size)

                if target_bytes > current_bytes:
                    logger.debug("rbd image {} size needs to be changed".format(image))

                    # resize method, doesn't document potential exceptions
                    rbd_image.resize(target_bytes)
                    logger.info("(rbd_size) resized {}/{} to {}".format(pool, image, reqd_size))
                    changes_made = True

    return changes_made


def rbd_list(pool):
    """
    return a list of rbd images in a given pool
    :param pool: pool name to look at to return a list of rbd image names for (str)
    :return: list of rbd image names (list)
    """

    with rados.Rados(conffile=CEPH_CONF) as cluster:
        with cluster.open_ioctx(pool) as ioctx:
            rbd_inst = rbd.RBD()
            rbd_names = rbd_inst.list(ioctx)
    return rbd_names


def rados_pool(pool):
    """
    determine if a given pool name is defined within the ceph cluster
    :param pool: pool name to check for (str)
    :return: Boolean representing the pool's existence
    """

    with rados.Rados(conffile=CEPH_CONF) as cluster:
        pool_list = cluster.list_pools()

    return pool in pool_list


def rbdmap_entry(pool, image):
    """
    check the given image has an entry in /etc/ceph/rbdmap - if not add it!
    :param pool: pool name (str)
    :param image: rbd image name (str)
    :return: boolean indicating whether the rbdmap file was updated
    """

    # Assume it's not there, so if we find it flip this to False
    entry_needed = True

    srch_str = pool + '/' + image
    with open('/etc/ceph/rbdmap', 'a+') as rbdmap:

        for entry in rbdmap:
            if entry.startswith(srch_str):
                # found it - get out,
                entry_needed = False
                break

        if entry_needed:
            # need to add an entry to the rbdmap file
            rbdmap.write("{}\t\tid=admin,keyring={},options=noshare\n".format(srch_str,
                                                                             KEYRING))

    return entry_needed


def set_alua(lun, desired_state='standby'):
    """
    Sets the ALUA state of a LUN (active/standby)
    :param lun: LIO LUN object
    :param desired_state: active or standby state
    :return: None
    """

    alua_state_options = {"active": '0',
                          "active/unoptimized": '1',
                          "standby": '2'}
    configfs_path = lun.path
    lun_name = lun.name
    alua_access_state = 'alua/default_tg_pt_gp/alua_access_state'
    alua_access_type = 'alua/default_tg_pt_gp/alua_access_type'
    type_fullpath = os.path.join(configfs_path, alua_access_type)

    if fread(type_fullpath) != 'Implicit':
        logger.info("(set_alua) Switching device alua access type to Implicit - i.e. active path set by gateways")
        fwrite(type_fullpath, '1')
    else:
        logger.debug("(set_alua) lun alua_access_type already set to Implicit - no change needed")

    state_fullpath = os.path.join(configfs_path, alua_access_state)
    if fread(state_fullpath) != alua_state_options[desired_state]:
        logger.debug("(set_alua) Updating alua_access_state for {} to {}".format(lun_name,
                                                                                 desired_state))
        fwrite(state_fullpath, alua_state_options[desired_state])
    else:
        logger.debug("(set_alua) Skipping alua update - already set to desired state '{}'".format(desired_state))


def set_owner(gateways):
    """
    Determine the gateway in the configuration with the lowest number of active LUNs. This
    gateway is then selected as the owner for the primary path of the current LUN being
    processed
    :param gateways: gateway dict returned from the RADOS configuration object
    :return: specific gateway hostname (str) that should provide the active path for the next LUN
    """

    # turn the dict into a list of tuples
    gw_items = gateways.items()

    # first entry is the lowest number of active_luns
    gw_items.sort(key=lambda x: (x[1]['active_luns']))

    # 1st tuple is gw with lowest active_luns, so return the 1st
    # element which is the hostname
    return gw_items[0][0]


def main():

    num_changes = 0

    # Define the fields needs to create/map rbd's the the host(s)
    # NB. features and state are reserved/unused
    fields = {
        "pool": {"required": False, "default": "rbd", "type": "str"},
        "image": {"required": True, "type": "str"},
        "size": {"required": True, "type": "str"},
        "host": {"required": True, "type": "str"},
        "features": {"required": False, "type": "str"},
        "state": {
            "default": "present",
            "choices": ['present', 'absent'],
            "type": "str"
        },
    }

    updates_made = False

    # not supporting check mode currently
    module = AnsibleModule(argument_spec=fields,
                           supports_check_mode=False)

    pool = module.params["pool"]
    image = module.params['image']
    size = module.params['size']
    target_host = module.params['host']

    if not valid_size(size):
        logger.critical("image '{}' has an invalid size specification '{}' in the ansible configuration".format(image,
                                                                                                         size))
        module.fail_json(msg="(main) Unable to use the size parameter '{}' for image '{}' from the playbook - "
                             "must be a number suffixed by M, G or T".format(size, image))

    config = Config(logger)
    if config.error:
        module.fail_json(msg=config.error_msg)

    # Before we start make sure that the target host is actually defined to the config
    if target_host not in config.config['gateways'].keys():
        logger.critical("target host is not valid, please check the config entry for this rbd image")
        module.fail_json(msg="(main) host name given for {} is not a valid gateway name".format(image))

    if config.platform != 'rbd':
        module.fail_json(msg="Storage platform not supported. Only Ceph is currently supported.")

    logger.info("START - LUN configuration started for {} {}/{}".format(config.platform, pool, image))

    # ensure the rbd pool is valid
    if not rados_pool(pool):
        # Could create the pool, but a fat finger moment in the config file would mean rbd images
        # get created and mapped, and then need correcting. Better to exit if the pool doesn't exist
        module.fail_json(msg="Pool '{}' does not exist. Unable to continue")

    # first look at disks in the specified pool
    disk_list = rbd_list(pool)
    logger.debug("rbd pool contains the following - {}".format(disk_list))
    this_host = gethostname().split('.')[0]
    logger.debug("Hostname Check - this host is {}, target host for allocations is {}".format(this_host,
                                                                                              target_host))

    # if the image required isn't defined, create it!
    if image not in disk_list:
        # create the requested disk if this is the 'owning' host
        if this_host == target_host:            # is_this_host(target_host):

            rc, msg = rbd_create(image, size, pool)
            if rc == 0:
                config.add_item('disks', image)
                updates_made = True
                logger.info("(main) created {}/{} successfully".format(image, pool))
                num_changes += 1
            else:
                module.fail_json("(main) problem creating rbd image {} : {}".format(image, msg))

        else:
            # the image isn't there, and this isn't the 'owning' host
            # so wait until the disk arrives
            waiting = 0
            while image not in disk_list:
                sleep(LOOP_DELAY)
                disk_list = rbd_list(pool)
                waiting += LOOP_DELAY
                if waiting >= TIME_OUT_SECS:
                    module.fail_json(msg="(main) timed out waiting for rbd to show up")
    else:
        # requested image is defined to ceph, so ensure it's in the config
        if image not in config.config['disks']:
            config.add_item('disks', image)

    logger.debug("Check the rbd image size matches the ansible request")

    # if updates_made is not set, the disk pre-exists so on the owning host see if it needs to be resized
    if not updates_made and this_host == target_host:       # is_this_host(target_host):

        # check the size, and update if needed
        changed = rbd_size(image, size, pool)
        if changed:
            logger.debug("rbd image {} resized to {}".format(image, size))
            updates_made = True
            num_changes += 1
        else:
            logger.debug("rbd image {} size matches the configuration file request".format(image))

    logger.debug("Begin processing LIO mapping requirement")

    changed, map_device = get_rbd_map(module, image, pool)
    if changed:
        updates_made = True
        num_changes += 1

    # the rbd image exists, and it's the required size, so time to check that it's
    # listed in rbdmap file (so it gets remapped automagically at boot time)
    if rbdmap_entry(pool, image):
        logger.debug('Entry added to /etc/ceph/rbdmap for {}/{}'.format(pool, image))
        updates_made = True
        num_changes += 1

    # now see if we need to add this rbd image to LIO
    lun = lun_in_lio(image)
    if not lun:
        # this image has not been defined to LIO, so check the config for the details and
        # if it's  missing define the wwn/alua_state and update the config

        if this_host == target_host:
            # first check to see if the device needs adding
            try:
                wwn = config.config['disks'][image]['wwn']
            except KeyError:
                wwn = ''

            if wwn == '':
                # disk hasn't been defined before
                lun = rbd_add_device(module, image, map_device)
                wwn = lun._get_wwn()
                owner = set_owner(config.config['gateways'])

                disk_attr = {"wwn": wwn, "owner": owner}
                config.update_item('disks', image, disk_attr)

                gateway_dict = config.config['gateways'][owner]
                gateway_dict['active_luns'] += 1

                config.update_item('gateways', owner, gateway_dict)

                logger.debug("(main) registered '{}' with wwn '{}' with the config object".format(image, wwn))
                logger.info("(main) added '{}/{}' to LIO".format(pool, image))

            else:
                # config already has wwn and owner information
                lun = rbd_add_device(module, image, map_device, wwn)
                logger.debug("(main) registered '{}' with wwn '{}' from the config object".format(image, wwn))

            updates_made = True
            num_changes += 1

        else:
            # lun is not already in LIO, but this is not the owning node that defines the wwn
            # we need the wwn from the config (placed by the owning node), so we wait!
            waiting = 0
            while waiting < TIME_OUT_SECS:
                config.refresh()
                if image in config.config['disks']:
                    if 'wwn' in config.config['disks'][image]:
                        if config.config['disks'][image]['wwn']:
                            wwn = config.config['disks'][image]['wwn']
                            break
                sleep(LOOP_DELAY)
                waiting += LOOP_DELAY
                logger.debug("waiting for config object to show {} with it's wwn".format(image))

            if waiting >= TIME_OUT_SECS:
                module.fail_json(msg="(main) waited too long for the wwn information on image {}".format(image))

            # At this point we have a usable config, so we just need to add the wwn
            lun = rbd_add_device(module, image, map_device, wwn)

            logger.debug("(main) added {} to LIO using wwn '{}' defined by {}".format(image,
                                                                                      wwn,
                                                                                      target_host))
            logger.info("(main) added {} to LIO for this gateway".format(image))
            updates_made = True
            num_changes += 1

    logger.debug("Checking ALUA state for this rbd image")

    # lun/image is defined to LIO, so just check the preferred alua state is OK
    if config.config['disks'][image]["owner"] == this_host:
        # get LUN object for this image
        logger.info("Setting alua state to active for image {}".format(image))
        set_alua(lun, 'active')
    else:
        logger.info("Setting alua state to standby for image {}".format(image))
        set_alua(lun, 'standby')

    # the owning host for an image is the only host that commits to the config
    if this_host == target_host and config.changed:

        logger.debug("(main) Committing change(s) to the config object in pool {}".format(pool))
        config.commit()
        if config.error:
            module.fail_json(msg="Unable to commit changes to config object '{}' in pool '{}'".format(config.config_name,
                                                                                                  config.pool))

    if not updates_made:
        logger.info("END   - No changes needed")
    else:
        logger.info("END   - {} configuration changes made".format(num_changes))

    module.exit_json(changed=updates_made, meta={"msg": "Configuration updated"})


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
