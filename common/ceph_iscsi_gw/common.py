#!/usr/bin/env python

import rados
import time
import json
import os
import traceback

class ConfigTransaction(object):
    def __init__(self, cfg_type, element_name):
        self.type = cfg_type
        self.item_name = element_name
        self.item_content = {}


class CephCluster(object):

    def __init__(self,
                 conf_file='/etc/ceph/ceph.conf',
                 conf_keyring='/etc/ceph/ceph.client.admin.keyring'):

        self.cluster = rados.Rados(conffile=conf_file,
                                   conf=dict(keyring=conf_keyring))
        self.cluster.connect()

    def shutdown(self):
        self.cluster.shutdown()


class Config(object):

    seed_config = {"disks": {},
                   "gateways": {},
                   "clients": {}}
    lock_time_limit = 30

    def __init__(self, logger, cfg_name='gateway.conf', pool='rbd'):
        self.logger = logger
        self.config_name = cfg_name
        self.pool = pool
        self.ceph = None
        self.platform = Config.get_platform()
        self.error = False
        self.error_msg = ""
        self.txn_list = []
        self.txn_ptr = 0

        if self.platform == 'rbd':
            self.ceph = CephCluster()
            self.get_config = self._get_rbd_config
            self.commit_config = self._commit_rbd
        else:
            self.error = True
            self.error_msg = "Unsupported platform - rbd only (for now!)"

        self.config = self.get_config()
        self.changed = False

    def _get_rbd_config(self):

        cfg_dict = {}

        try:
            self.logger.debug("(_get_rbd_config) Opening connection to {} pool".format(self.pool))
            ioctx = self.ceph.cluster.open_ioctx(self.pool)       # open connection to pool
        except rados.ObjectNotFound:
            self.error = True
            self.error_msg = "'{}' pool does not exist!".format(self.pool)
            self.logger.error("(Config._get_rbd_config) {}".format(self.error_msg))
            return {}

        try:
            cfg_data = ioctx.read(self.config_name)
            ioctx.close()
        except rados.ObjectNotFound:
            # config object is not there, create a seed config
            self.logger.debug("(_get_rbd_config) config object doesn't exist..seeding it")
            self._seed_rbd_config()
            if self.error:
                self.logger.error("(Config._get_rbd_config) Unable to seed the config object")
                return {}
            else:
                cfg_data = json.dumps(Config.seed_config)

        if cfg_data:
            self.logger.debug("(_get_rbd_config) config object contains '{}'".format(cfg_data))
            cfg_dict = json.loads(cfg_data)
        else:
            self.logger.debug("(_get_rbd_config) config object exists, but is empty '{}'".format(cfg_data))
            self._seed_rbd_config()
            if self.error:
                self.logger.error("(Config._get_rbd_config) Unable to seed the config object")
                return {}
            else:
                cfg_dict = Config.seed_config

        return cfg_dict

    def lock(self):

        ioctx = self.ceph.cluster.open_ioctx(self.pool)

        secs = 0

        while secs < Config.lock_time_limit:
            try:
                ioctx.lock_exclusive(self.config_name, 'lock', 'config')
                break
            except rados.ObjectBusy:
                self.logger.debug("(Config.lock) waiting for excl lock on {} object".format(self.config_name))
                time.sleep(1)
                secs += 1

        if secs >= Config.lock_time_limit:
            self.error = True
            self.error_msg = ("Timed out ({}) waiting for excl "
                              "lock on {} object".format(Config.lock_time_limit, self.config_name))
            self.logger.error("(Config.lock) {}".format(self.error_msg))

        ioctx.close()

    def unlock(self):
        ioctx = self.ceph.cluster.open_ioctx(self.pool)

        try:
            ioctx.unlock(self.config_name, 'lock', 'config')
        except Exception as e:
            self.error = True
            self.error_msg = ("Unable to unlock {} - {}".format(self.config_name,
                                                                traceback.format_exc()))
            self.logger.error("(Config.unlock) {}".format(self.error_msg))

        ioctx.close()

    def _seed_rbd_config(self):

        ioctx = self.ceph.cluster.open_ioctx(self.pool)

        self.lock()
        if self.error:
            return

        # if the config object is empty, seed it - if not just leave as is
        cfg_data = ioctx.read(self.config_name)
        if not cfg_data:
            self.logger.debug("_seed_rbd_config found empty config object")
            seed = json.dumps(Config.seed_config)
            ioctx.write_full(self.config_name, seed)
            self.changed = True

        self.unlock()

        ioctx.close()

    def _get_glfs_config(self):
        pass

    def refresh(self):
        self.logger.debug("config refresh - current config is {}".format(self.config))
        self.config = self.get_config()


    def add_item(self, cfg_type, element_name):
        self.config[cfg_type][element_name] = {}
        self.logger.debug("(Config.add_item) config updated to {}".format(self.config))
        self.changed = True

        txn = ConfigTransaction(cfg_type, element_name)
        self.txn_list.append(txn)
        self.txn_ptr = len(self.txn_list) - 1


    def update_item(self, cfg_type, element_name, attr_dict):
        self.config[cfg_type][element_name] = attr_dict
        self.logger.debug("(Config.update_item) config is {}".format(self.config))
        self.changed = True
        self.logger.debug("update_item: type={}, item={}, update={}".format(cfg_type,element_name,attr_dict))
        self.logger.debug("update_item point ; txn list length is {}, ptr is set to {}".format(len(self.txn_list),
                                                                                                   self.txn_ptr))
        self.txn_list[self.txn_ptr].item_content = attr_dict

    def _commit_rbd(self, config_str):

        self.logger.debug("_commit_rbd updating config with {}".format(config_str))

        ioctx = self.ceph.cluster.open_ioctx(self.pool)

        self.lock()
        if self.error:
            return

        # reread the config to account for updates made by other systems
        # then apply this hosts update(s)
        current_config = json.loads(ioctx.read(self.config_name))
        for txn in self.txn_list:
            current_config[txn.type][txn.item_name] = txn.item_content

        config_str = json.dumps(current_config)
        ioctx.write_full(self.config_name, config_str)

        self.unlock()
        ioctx.close()

        self.ceph.shutdown()

    def _commit_glfs(self, config_str):
        pass

    def commit(self):
        config_str = json.dumps(self.config)
        self.logger.debug("(Config.commit) Config being updated to {}".format(config_str))
        self.commit_config(config_str)

    @classmethod
    def get_platform(cls):

        """
        :return: rbd or gluster
        """
        if (any(os.access(os.path.join(path, 'rbd'), os.X_OK)
                for path in os.environ["PATH"].split(os.pathsep))):
            return 'rbd'

        return ''


def main():
    pass

if __name__ == '__main__':

    main()
