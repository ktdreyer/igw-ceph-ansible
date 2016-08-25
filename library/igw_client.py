#!/usr/bin/env python

__author__ = 'pcuzner@redhat.com'


import logging
from logging.handlers import RotatingFileHandler
from ansible.module_utils.basic import *

import rtslib_fb.root as lio_root
from rtslib_fb.target import NodeACL, TPG
from rtslib_fb.utils import RTSLibError


class Client(object):
    """
    This class holds a representation of a client connecting to LIO
    """

    supported_access_types = ['chap']

    def __init__(self, client_iqn, image_list, auth_type, credentials):
        """
        Instantiate an instance of an LIO client
        :param client_iqn: iscsi iqn string
        :param image_list: list of rbd images to attach to this client
        :param auth_type: authentication type - null or chap
        :param credentials: chap credentials in the format 'user/password'
        :return:
        """

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

    def setup_luns(self):
        """
        Add the requested LUNs to the node ACL definition. The image list defined for the
        client is compared to the current runtime settings, resulting in new images being
        added, or images removed.
        """

        self.client_luns = get_images(self.acl)
        for image_name in self.client_luns:
            lun_id = self.client_luns[image_name]['lun_id']
            self.lun_id_list.remove(lun_id)
            logger.debug("(Client.setup_luns) {} has id of {}".format(image_name, lun_id))

        self.tpg_luns = get_images(self.tpg)
        current_map = dict(self.client_luns)

        for image in self.requested_images:
            if image in self.client_luns:
                del current_map[image]
                continue
            else:
                rc = self._add_lun(image, self.tpg_luns[image])
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
                self._del_lun_map(image)
                if self.error:
                    logger.error("(Client.setup) unable to delete {} from {}".format(self.iqn,
                                                                                     image))
                    return

    def define_client(self):
        """
        Establish the links for this object to the corresponding ACL and TPG objects from LIO
        :return:
        """

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
        """
        Attempt to configure authentication for the client, given the credentials provided
        :return:
        """

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

    def _add_lun(self, image, lun):
        """
        Add a given image to the client ACL
        :param image: rbd image name (str)
        :param lun: rtslib lun object
        :return:
        """

        rc = 0
        # get the tpg lun to map this client to
        tpg_lun = lun['tpg_lun']
        lun_id = self.lun_id_list[0]        # pick the lowest available lun ID
        logger.debug("(Client._add_lun) Adding {} to {} at id {}".format(image, self.iqn, lun_id))
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

    def _del_lun_map(self, image):
        """
        Delete a lun from the client's ACL
        :param image: rbd image name to remove
        :return:
        """

        lun = self.client_luns[image]['mapped_lun']
        try:
            lun.delete()
            self.change_count += 1
        except RTSLibError as err:
            self.error = True
            self.error_msg = err

    def delete(self):
        """
        Delete the client definition from LIO
        :return:
        """

        try:
            self.acl.delete()
            self.change_count += 1
            logger.info("(Client.delete) deleted NodeACL for {}".format(self.iqn))
        except RTSLibError as err:
            self.error = True
            self.error_msg = "RTS NodeACL delete failure"
            logger.error("(Client.delete) failed to delete client {} - error: {}".format(self.iqn,
                                                                                         err))

    def exists(self):
        """
        This function determines whether this instances iqn is already defined to LIO
        :return: Boolean
        """

        r = lio_root.RTSRoot()
        client_list = [client.node_wwn for client in r.node_acls]
        return self.iqn in client_list


def get_images(rts_object):
    """
    Funtion to return a dict of luns mapped to either a node ACL or the TPG, based on the passed
    object type
    :param rts_object: rtslib object - either NodeACL or TPG
    :return:
    """

    luns_mapped = {}

    if isinstance(rts_object, NodeACL):
        # return a dict of images assigned to this client
        for m_lun in rts_object.mapped_luns:
            image_name = m_lun.tpg_lun.storage_object.name
            luns_mapped[image_name] = {"lun_id": m_lun.mapped_lun,
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


def validate_images(image_list, tpg):
    """
    Confirm that the images listed are actually allocated to the tpg and can
    therefore be used by a client
    :param image_list: list of rbd image names
    :param tpg: TPG object
    :return: a list of images that are NOT in the tpg ... should be empty!
    """
    bad_images = []
    tpg_lun_list = get_images(tpg).keys()
    for image in image_list:
        if image not in tpg_lun_list:
            bad_images.append(image)

    return bad_images


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
            "required": True,
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
        module.fail_json(msg="Unable to configure - auth method of '{}' requested, without"
                             " credentials for {}".format(auth_type, client_iqn))

    logger.info("START - Client configuration started : {}".format(client_iqn))

    client = Client(client_iqn, image_list, auth_type, credentials)

    if desired_state == 'present':                          # NB. This is the default

        client.define_client()
        if client.error:
            module.fail_json(msg="Unable to define the client ({}) - {}".format(client_iqn,
                                                                                client.error_msg))

        bad_images = validate_images(image_list, client.tpg)
        if not bad_images:

            client.setup_luns()
            if client.error:
                module.fail_json(msg="Unable to setup the client lun maps ({}) - {}".format(client_iqn,
                                                                                            client.error_msg))

            if client.auth_type in Client.supported_access_types:
                client.configure_auth()
                if client.error:
                    module.fail_json(msg="Unable to configure authentication for {} - {}".format(client_iqn,
                                                                                                 client.error_msg))
            else:
                logger.warning("(main) client '{}' configured without security".format(client_iqn))
        else:
            module.fail_json(msg="(main) non-existent images {} requested for {}".format(bad_images, client_iqn))

    else:
        # the desired state for this client is absent, so remove it if necessary
        if client.exists():
            client.define_client()          # grab the client and parent tpg objects
            client.delete()
            if client.error:
                module.fail_json(msg="Unable to delete the client ({}) - {}".format(client_iqn,
                                                                                    client.error_msg))
        else:
            # desired state is absent, but the client does not exist in LIO - Nothing to do!
            logger.info("(main) client {} removal request, but the client is not "
                        "defined...skipping".format(client_iqn))

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
