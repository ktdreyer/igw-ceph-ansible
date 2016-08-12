#!/usr/bin/env python

__author__ = 'pcuzner@redhat.com'


import logging
from logging.handlers import RotatingFileHandler
from ansible.module_utils.basic import *

import rtslib_fb.root as lio_root
from rtslib_fb.target import NodeACL, TPG
from rtslib_fb.utils import RTSLibError


class Client(object):
    supported_access_types = ['chap']

    def __init__(self, client_iqn, image_list, auth_type, credentials):
        self.iqn = client_iqn
        self.requested_images = image_list
        self.auth_type = auth_type              # auth ... '' or chap
        self.credentials = credentials          # parameters for auth
        self.acl = None
        self.error = False
        self.error_msg = ''
        self.client_luns = {}
        self.tpg = None
        self.tpg_luns = {}
        self.lun_id_list = range(256)           # available LUN ids 0..255
        self.change_count = 0

    def setup(self):

        if self.auth_type in Client.supported_access_types:
            self.configure_auth()
            if self.error:
                return
        else:
            logger.warning("(Client.setup) client '{}' configured without security".format(self.iqn))

        self.client_luns = get_images(self.acl)
        for image_name in self.client_luns:
            lun_id = self.client_luns[image_name]['lun_id']
            self.lun_id_list.remove(lun_id)

        self.tpg_luns = get_images(self.tpg)
        current_map = dict(self.client_luns)

        for image in self.requested_images:
            if image in self.client_luns:
                del current_map[image]
                continue
            else:
                rc = self.add_lun(image, self.tpg_luns[image])
                if rc != 0:
                    self.error = True
                    self.error_msg = "{} is missing from the tpg - unable to map".format(image)
                    logger.debug("(Client.setup) tpg luns {}".format(self.tpg_luns))
                    logger.error("(Client.setup) missing image '{}' from the tpg".format(image))
                    return

        # 'current_map' should be empty, if not the remaining images need to be removed
        # from the client
        if current_map:
            for image in current_map:
                self.del_lun_map(image)
                if self.error:
                    logger.error("(Client.setup) unable to delete {} from {}".format(self.iqn,
                                                                                     image))
                    return


    def define_client(self):
        r = lio_root.RTSRoot()

        # NB. this will check all tpg's for a matching iqn
        for client in r.node_acls:
            if client.node_wwn == self.iqn:
                self.acl = client
                self.tpg = client.parent_tpg
                logger.debug("(Client.define_client) - {} already defined".format(self.iqn))
                return

        # at this point the client does not exist, so create it
        # NB. The solution supports only a single tpg definition, so simply grabbing the
        # first tpg is fine. If multiple tpgs are required this will need more work
        self.tpg = r.tpgs.next()

        try:
            self.acl = NodeACL(self.tpg, self.iqn)
            self.change_count += 1
        except RTSLibError as err:
            logger.error("(Client.define_client) FAILED to define {}".format(self.iqn))
            logger.debug("(Client.define_client) failure msg {}".format(err))
            self.error = True
            self.error_msg = err

        logger.info("(Client.define_client) {} added successfully".format(self.iqn))

    def configure_auth(self):

        try:
            client_username, client_password = self.credentials.split('/')

            if self.acl.chap_userid == '' or self.acl.chap_userid != client_username:
                self.acl.chap_userid = client_username
                logger.info("(Client.configure_auth) chap user name changed for {}".format(self.iqn))
                self.change_count += 1
            if self.acl.chap_password == '' or self.acl.chap_password != client_password:
                self.acl.chap_password = client_password
                logger.info("(Client.configure_auth) chap password changed for {}".format(self.iqn))

        except RTSLibError as err:
            self.error = True
            self.error_msg = "Unable to (re)configure chap - ".format(err)
            logger.error("Client.configure_auth) failed to set credentials on node")

    def add_lun(self, image, lun):

        rc = 0
        # get the tpg lun to map this client to
        tpg_lun = lun['tpg_lun']
        lun_id = self.lun_id_list[0]        # pick the lowest available lun ID
        try:
            m_lun = self.acl.mapped_lun(lun_id, tpg_lun=tpg_lun)
            self.client_luns[image] = {"lun_id": lun_id,
                                       "mapped_lun": m_lun,
                                       "tpg_lun": tpg_lun}
            self.lun_id_list.remove(lun_id)
            logger.info("(Client.add_lun) added image '{}' to {}".format(image, self.iqn))
            self.change_count += 1

        except RTSLibError as err:
            logger.error("Client.add_lun RTSLibError for lun id {} - {}".format(lun_id, err))
            rc = 12

        return rc

    def del_lun_map(self, image):

        lun = self.client_luns[image]['mapped_lun']
        try:
            lun.delete()
            self.change_count += 1
        except RTSLibError as err:
            self.error = True
            self.error_msg = err

    def delete(self):
        try:
            self.acl.delete()
            self.change_count += 1
            logger.info("(Client.delete) deleted NodeACL for {}".format(self.iqn))
        except RTSLibError as err:
            self.error = True
            self.error_msg = "RTS NodeACL delete failure"
            logger.error("(Client.delete) failed to delete client {} - error: {}".format(self.iqn,
                                                                                         err))



def get_images(rts_object):

    luns_mapped = {}

    if isinstance(rts_object, NodeACL):
        # return a dict of images assigned to this client
        for m_lun in rts_object.mapped_luns:
            image_name = m_lun.tpg_lun.storage_object.name
            luns_mapped[image_name] = {"lun_id": m_lun.tpg_lun.lun,
                                       "mapped_lun": m_lun,
                                       "tpg_lun": m_lun.tpg_lun}

    elif isinstance(rts_object, TPG):
        # return a dict of *all* images available to this tpg
        for m_lun in rts_object.luns:
            image_name = m_lun.storage_object.name
            luns_mapped[image_name] = {"lun_id": m_lun.lun,
                                       "mapped_lun": None,
                                       "tpg_lun": m_lun}

    return luns_mapped


def main():

    fields = {
        "client_iqn": {"required": True, "type": "str"},
        "image_list": {"required": True, "type": "list"},
        "credentials": {"required": False, "type": "str", "default": ''},
        "auth": {
            "required": False,
            "default": '',
            "choices": ['', 'chap'],
            "type": "str"
        },
        "state": {
            "required": False,
            "default": "present",
            "choices": ['present', 'absent'],
            "type": "str"
            },
        }

    module = AnsibleModule(argument_spec=fields,
                           supports_check_mode=False)

    client_iqn = module.params['client_iqn']
    image_list = module.params['image_list']
    credentials = module.params['credentials']
    auth_type = module.params['auth']
    desired_state = module.params['state']

    auth_methods = ['chap']

    if auth_type in auth_methods and not credentials:
        module.fail_json(msg="Unable to configure - auth method of '{}' defined, without"
                             " credentials for {}".format(auth_type, client_iqn))

    logger.info("START - Client configuration started : {}".format(client_iqn))

    client = Client(client_iqn, image_list, auth_type, credentials)

    client.define_client()
    if client.error:
        module.fail_json(msg="Unable to define the client ({}) - {}".format(client_iqn,
                                                                            client.error_msg))

    if desired_state == 'present':
        action = 'define'
        client.setup()
    else:
        action = 'remove'
        client.delete()

    if client.error:
        module.fail_json(msg="Unable to {} client ({}) - {}".format(action,
                                                                    client_iqn,
                                                                    client.error_msg))

    logger.info("END   - Client configuration complete - {} changes made".format(client.change_count))

    changes_made = True if client.change_count > 0 else False

    module.exit_json(changed=changes_made, meta={"msg": "Client definition completed {} "
                                                 "changes made".format(client.change_count)})

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
