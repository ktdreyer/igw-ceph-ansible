#!/usr/bin/env python

__author__ = 'pcuzner@redhat.com'

import json
import logging
from logging.handlers import RotatingFileHandler

from socket import gethostname
from time import sleep
import tempfile
import os

from ansible.module_utils.basic import *
from rtslib_fb import BlockStorageObject, root
from rtslib_fb.utils import RTSLibError

SIZE_SUFFIXES = ['M', 'G', 'T']

RBD_FEATURES = ['--image-format 2',
                '--image-shared',
                '--image-feature layering']
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
    lun_state = False
    rtsroot = root.RTSRoot()
    for stg_object in rtsroot.storage_objects:
        if stg_object.name == image:
            lun_state = True
            break

    return lun_state


def rbd_create(module, image, size, pool):
    create_rbd = 'rbd create {} --size {} --pool {} {}'.format(image,
                                                               size,
                                                               pool,
                                                               ' '.join(RBD_FEATURES))
    rc, rbd_out, err = module.run_command(create_rbd)
    if rc != 0:
        module.fail_json(msg="failed to create rbd with '{}'".format(create_rbd))


def rbd_add_device(module, image, device_path, in_wwn=None):
    logger.info("(add_device) Adding image '{}' with path {} to LIO".format(image, device_path))
    new_lun = None
    try:
        new_lun = BlockStorageObject(name=image, dev=device_path, wwn=in_wwn)
    except RTSLibError as err:
        module.fail_json(msg="failed to add {} to LIO - error({})".format(image, str(err)))

    return new_lun

def rbd_size(module, image, reqd_size, pool):
    changes_made = False
    # get the current device size
    rc, rbd_info, err = module.run_command("rbd info {}/{} --format=json".format(pool,
                                                                                 image))
    if rc != 0:
        module.fail_json(msg="(rbd_size) unable to get rbd information")
    rbd_json = json.loads(rbd_info)
    image_size = int(rbd_json['size'])
    tgt_bytes = convert_2_bytes(reqd_size)

    if tgt_bytes > image_size:
        rc, rsize_out, error = module.run_command("rbd resize -s {} {}/{}".format(reqd_size, pool, image))
        if rc != 0:
            module.fail_json(msg="(rbd_size) failed to resize {}/{} to {}".format(pool, image, reqd_size))
        logger.info("(rbd_size) resized {}/{} to {}".format(pool, image, reqd_size))
        changes_made = True

    return changes_made


def is_this_host(tgt_hostname):
    this_host = gethostname()
    if '.' in tgt_hostname:
        tgt_hostname = tgt_hostname.split('.')[0]
    if '.' in this_host:
        this_host = this_host.split('.')[0]

    return this_host == tgt_hostname


def get_rbds(module, pool):
    list_rbds = 'rbd -p {} ls --format=json'.format(pool)
    rc, rbd_str, err = module.run_command(list_rbds)
    if rc != 0:
        module.fail_json(msg="failed to execute {}".format(list_rbds))

    return json.loads(rbd_str)


class Config(object):

    seed_config = {"disks": {},
                   "gateways": {},
                   "clients": {}}

    def __init__(self, module, igw_platform, pool):
        self.module = module
        self.pool = pool
        self.platform = igw_platform
        if igw_platform == 'rbd':
            self.get_config = self._get_rbd_config
            self.commit_config = self._commit_rbd
        else:
            module.fail_json(msg="Unsupported platform - rbd only currently.")

        self.config = self.get_config()
        self.changed = False

    def _get_rbd_config(self):
        tmp = tempfile.NamedTemporaryFile()
        rc, config, err = self.module.run_command('rados -p {} get igw.conf {}'.format(self.pool, tmp.name))
        if rc != 0:
            # config object in this pool doesn't exist yet, so create it
            init_str = json.dumps(Config.seed_config)
            tmp.write(init_str)
            tmp.seek(0)
            rc, create_out, err = self.module.run_command('rados -p {} put igw.conf {}'.format(self.pool, tmp.name))
            tmp.close()
            cfg_dict = json.loads(init_str)
        else:
            tmp.seek(0)
            cfg_dict = json.loads(tmp.read())
            tmp.close()

        return cfg_dict

    def _get_glfs_config(self):
        pass

    def refresh(self):
        self.config = self.get_config()
        logger.debug("config object holds {}".format(self.config))

    def add_disk(self, image):
        self.config['disks'][image] = {}
        logger.debug("(Config.add_disk) config updated to {}".format(self.config))
        self.changed = True

    def update_disk(self, image, attr_dict):
        self.config['disks'][image] = attr_dict
        logger.debug("(Config.update_disk) config is {}".format(self.config))
        self.changed = True

    def _commit_rbd(self, config_str):
        with tempfile.NamedTemporaryFile() as tmp:
            tmp.write(config_str)
            tmp.flush()
            rc, rados_out, err = self.module.run_command('rados -p {} put igw.conf {}'.format(self.pool,
                                                                                              tmp.name))
            if rc != 0:
                self.module.fail_json(
                    "msg='unable to commit change to the config file stored in pool {}".format(self.pool))

    def _commit_glfs(self, config_str):
        pass

    def commit(self):
        config_str = json.dumps(self.config)
        logger.debug("(Config.commit) Config being updated to {}".format(config_str))
        self.commit_config(config_str)


def platform_type():
    """
    :return: rbd or gluster
    """
    if (any(os.access(os.path.join(path, 'rbd'), os.X_OK)
            for path in os.environ["PATH"].split(os.pathsep))):
        return 'rbd'

    return ''


def main():

    num_changes = 0

    # Define the fields needs to create/map rbd's the the host(s)
    # NB. features and state are reserved/unused
    fields = {
        "pool": {"required": True, "type": "str"},
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

    igw_platform = platform_type()
    if igw_platform == 'rbd':
        add_device = rbd_add_device
        get_disks = get_rbds
        create_disk = rbd_create
    else:
        module.fail_json(msg="Storage platform not supported. Only Ceph is currently supported.")

    pool = module.params["pool"]
    image = module.params['image']
    size = module.params['size']
    target_host = module.params['host']

    logger.info("START - LUN configuration started for {} {}/{}".format(igw_platform, pool, image))

    if not valid_size(size):
        logger.critical("image '{}' has an invalid size specification '{}' in the ansible configuration".format(image,
                                                                                                         size))
        module.fail_json(msg="(main) Unable to use the size parameter '{}' for image '{}' from the playbook - "
                             "must be a number suffixed by M, G or T".format(size, image))

    # first look at disks in the specified pool
    disk_list = get_disks(module, pool)
    config = Config(module, igw_platform, pool)

    # if the image required isn't defined, create it!
    if image not in disk_list:
        # create the requested disk if this is the 'owning' host
        if is_this_host(target_host):

            create_disk(module, image, size, pool)

            config.add_disk(image)
            updates_made = True
            logger.debug("(main) added {} to the config".format(image))
            logger.info("(main) created {}/{} successfully".format(image, pool))
            num_changes += 1
        else:
            # the image isn't there, and this isn't the 'owning' host
            # so wait until the disk arrives
            waiting = 0
            while image not in disk_list:
                sleep(LOOP_DELAY)
                disk_list = get_disks(module, pool)
                waiting += LOOP_DELAY
                if waiting >= TIME_OUT_SECS:
                    module.fail_json(msg="(main) timed out waiting for rbd to show up")

    if igw_platform == 'rbd':
        # if updates_made is not set, the disk pre-exists so on the owning host see if it needs to be resized
        if not updates_made and is_this_host(target_host):

            # check the size, and update if needed
            changed = rbd_size(module, image, size, pool)
            if changed:
                updates_made = True
                num_changes += 1

        changed, map_device = get_rbd_map(module, image, pool)
        if changed:
            updates_made = True
            num_changes += 1

    # now see if we need to add this rbd image to LIO
    if not lun_in_lio(image):
        # this image has not been added to LIO, so add it, get the wwn and update the
        # config if this is the owning host
        if is_this_host(target_host):
            lun = add_device(module, image, map_device)
            wwn = lun._get_wwn()
            disk_attr = {"wwn": wwn}
            config.update_disk(image, disk_attr)
            updates_made = True
            logger.debug("(main) registered '{}' with wwn '{}' with the config file".format(image, wwn))
            logger.info("(main) added '{}/{}' to LIO".format(pool, image))
            num_changes += 1
        else:
            # lun is not already in LIO, but this is not the owning node
            # we need the wwn from the config (placed by the owning node)
            waiting = 0
            while waiting < TIME_OUT_SECS:
                config.refresh()
                if image in config.config['disks']:
                    if 'wwn' in config.config['disks'][image]:
                        wwn = config.config['disks'][image]['wwn']
                        break
                sleep(LOOP_DELAY)
                waiting += LOOP_DELAY
                logger.debug("waiting for {} with it's wwn to show up".format(image))

            if waiting >= TIME_OUT_SECS:
                module.fail_json(msg="(main) waited too long for the wwn information on image {}".format(image))

            # At this point we have a usable config, so we just need to add the wwn
            lun = add_device(module, image, map_device, wwn)
            logger.debug("(main) added {} to LIO using wwn '{}' defined by {}".format(image,
                                                                                      wwn,
                                                                                      target_host))
            logger.info("(main) added {} to LIO for this gateway".format(image))
            updates_made = True
            num_changes += 1

    if is_this_host(target_host) and config.changed:
        # config is only written by the owning host of the image
        logger.debug("(main) Committing change(s) to the config object in pool {}".format(pool))
        config.commit()

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
