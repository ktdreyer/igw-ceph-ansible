# igw-ceph-ansible
This project provides a mechanism to deploy iSCSI gateways in front of a ceph cluster. It consists of two discrete components - one installed  
on the intended ceph nodes that will act as gateways - and the other supplements the ceph-ansible solution by providing playbooks/tasks and some  
custom python modules for defining the iSCSI gateway configuration.  

##Introduction
At a high level, the project delivers custom code into the ansible controller, and the intended gateway nodes. The code deployed to the gateway  
nodes provides the decision making logic needed to manage rbd and LIO. The custom modules installed on the ansible controller serve as a interface  
layer to the decision logic. This "separation" provides the potential to exploit the custom logic independently of Ansible.

The custom modules handle the following tasks;  

* definition of rbd images (including resize support and preferred path setting across gateway nodes)  
* iSCSI gateway creation (single target, multiple tpg, single portal per tpg, initial lun maps)  
* Client assignment (registering clients to LIO, chap authentication, and associating the client to specific rbd images)  
  
In addition to building the required configuration, the project also provide a playbook for purging the gateway configuration which includes the optional removal of rbd's.

##Features    
  
- confirms RHEL7.3** - and aborts if necessary
- ensures targetcli/device-mapper-multipath is installed (for rtslib support)
- configures multipath.conf
- creates rbd's if needed - at allocation time, each rbd is assigned an owner, which will become the preferred path  
- checks the size of the rbds at run time and expands if necessary
- maps the rbd's to the host (gateway)
- enables the rbdmap service to start on boot, and reconfigures the target service to be dependent on rbdmap
- adds the rbd's to the /etc/ceph/rbdmap file ensuring the devices are automatically mapped following a gateway reboot
- maps these rbds to LIO
- once mapped, the alua preferred path state is set or cleared (supporting an active/passive topology)  
- creates an iscsi target - common iqn, and multiple tpg's  
- adds a portal ip based on a the provided IP addresses defined in the group vars to each tpg  
- enables the local tpg, other gateways are defined as disabled  
- adds all the mapped luns to ALL tpg's (ready for client assignment)  
- add clients to the active/enabled tpg, with/without CHAP  
- images mapped to clients can be added/removed by changing image_list and rerunning the playbook
- clients can be removed using the state=absent variable and rerunning the playbook. At this point the entry can be 
  removed from the group variables file
- configuration can be wiped with the purge_gateway playbook
- current state can be seen by looking at the configuration object (stored in the rbd pool)

###Why RHEL 7.3?
There are several system dependencies that are required to ensure the correct (i.e. don't eat my data!) behaviors when OSD connectivity or gateway nodes    
fail. RHEL 7.3 delivers the necessary kernel changes, and also provides an updated multipathd, enabling rbd images to be managed by multipathd.

##Prerequisites  
* a working ceph cluster ( *rbd pool defined* )  
* nodes intended to be gateways should be at least ceph clients, with the ability to create and map rbd images  
* ansible installed on a *controller*, with passwordless ssh set up between the controller and the gateway nodes  

##Testing So far
The solution has been tested on a collocated cluster where the osd/mons and gateways all reside on the same node.  

##Quick Start
###Prepare the iSCSI Gateway Nodes  
The packages directory provides an rpm for the ceph-iscsi-config package. This package provides the LIO control logic that 
is used/called by the custom ansible modules. Simply install the latest ceph-iscsi-config rpm on each of the intended gateway  
nodes.

Alternatively, you may also use the provided tar files

  1. Unzip the project archive on your ansible controller host  
  2. Install the ceph_iscsi_gw package on each of the nodes.  
  2.a In the *root* of the project's archive on your ansible controller host  
        ```tar cvzf ceph_iscsi_config.tar.gz ceph-iscsi-config/```  
        ```scp ceph_iscsi_config.tar.gz <NODE>:~/.```  
  2.b On each node, install the package  
        ```tar xvzf ceph_iscsi_config.tar.gz```  
        ```cd ceph-isci-config```  
        ```python setup.py install```  
  This package provides;  
  - the common python class used by the ansible modules when interacting with the rados configuration object.  
  - the core logic when defining a LUN, iscsi gateway and client

###Configure the Playbook 
The package directory also provides an rpm for the ansible playbooks and custom modules. The pre-requisite rpm is ceph-ansible, so 
assuming it is installed, you may simply install the latest ceph-iscsi-ansible rpm on the ansible controller from the packages directory.  

Alternatively, you can use the files within the ceph-iscsi-ansible directory, directly.  

Once the playbook is installed, follow these steps to configure  
1. Ensure that /etc/ansible/hosts has an entry for ceph-iscsi-gw, listing the hosts you want to deploy the gateway configuration to.    
2. The playbook used to create a gateway environment is called ceph-iscsi-gw.yml in /usr/share/ceph-ansible.  
3. Parameters that govern the configuration are defined in group_vars/ceph-iscs-gw.yml  
4. run the playbook  
  ```> ansible-playbook ceph-iscsi-gw.yml```  
 
###Purging the configuration
You'll also find a purge task under roles/ceph-iscsi-gw/tasks that can be used to wipe the gateway configuration    
  ```> ansible-playbook -i hosts purge_gateways.yml```  
  *NB. If you just hit enter, the purge process will simply abort.* There is also a check to ensure that there are no active iSCSI sessions*   
  
You have to specify either;  
  - *lio* ... to remove the targetcli (LIO) config across each gateway  
  - *all* ... remove the LIO configuration AND delete all rbd devices that were mapped by LIO    
  


##Known Issues  
1. Preferred path state on a gateway can be lost following either a gateway reboot, or a restart of the target service  
  **Workaround**: Rerun the playbook to correct preferred paths following gateway or target service restart    
  **Issue**: The *rtslib* 'save_to_file' call does **not** persist alua state information in the saveconfig.json file, so when the service restarts the alua preferred setting is lost    
    
2. preferred paths are defined by using the name of the gateway, which assumes the gateway names resolves to the interface used for the iscsi service.    
3. the ceph cluster name is the default 'ceph', so the corresponding configuration file /etc/ceph/ceph.conf is valid