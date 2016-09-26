# igw-ceph-ansible
Ansible modules and playbook for setting up iscsi gateway(s) for a ceph cluster

##Introduction
The goal of this project is to provide a simple means of maintaining configuration state across a number of iscsi (LIO) gateways that front a ceph cluster. The code uses a number of custom modules to handle the following 
functional tasks

* definition of rbd images (including resize support)  
* iSCSI gateway creation (single target, multiple tpg, single portal, initial lun maps)  
* Client assignment (registering clients to LIO, chap authentication, and associating the client to specific rbd images)  
* balance alua preferred path state across gateway nodes (performed during addition of new rbd image to the configuration)    
  
In addition to building the required configuration, the project also provide a playbook for purging the gateway configuration which includes the optional removal of rbd's.

##Features    
  
- confirms RHEL7.3 - and aborts if necessary
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
- configuration can be wiped with the purge_cluster playbook
- current state can be seen by looking at the configuration object (stored in the rbd pool)

##Prerequisites  
* a working ceph cluster ( *rbd pool defined* )  
* nodes intended to be gateways should be at least ceph clients, with the ability to create and map rbd images  
* ansible installed on a *controller*, with passwordless ssh set up between the controller and the gateway nodes  

##Testing So far
The solution has been tested on a collocated cluster where the osd/mons and gateways all reside on the same node.  

##Quick Start
###Prepare the iSCSI Gateway Nodes  
  1. Unzip the project archive on your ansible controller host  
  2. Install the ceph_iscsi_gw package on each of the nodes.  
  2.a In the *root* of the project's archive on your ansible controller host  
        ```tar cvzf ceph_iscsi_gw.tar.gz common/```  
        ```scp ceph_iscsi_gw.tar.gz <NODE>:~/.```  
  2.b On each node, install the package  
        ```tar xvzf ceph_iscsi_gw.tar.gz```  
        ```cd common```  
        ```python setup.py install```  
  This package provides;  
  - the common python class used by the ansible modules when interacting with the rados configuration object.  
  - the core logic when defining a LUN, iscsi gateway and client

###Configure the Playbook    
  1. Configure the playbook on the controller  
  1.a Update the **'hosts'** file to match the node names/ip's for the gateways you want  
  1.b update **group_vars/ceph_iscsi_gw.yml** file to define the gateway name and IP, rbd images and client connections.    
  2. run the playbook    
  ```> ansible-playbook -i hosts easy-gw.yml```  
  
  To purge the configuration  
  ```> ansible-playbook -i hosts purge_gateways.yml```  
  *NB. If you just hit enter, the purge will abort.* There is also a check to ensure that there are no active iSCSI sessions*   
  
  You have to specify either;  
  - *lio* ... to remove the targetcli (LIO) config across each gateway  
  - *all* ... remove the LIO configuration AND delete all rbd devices that were mapped by LIO    
  


##Known Issues  
1. Preferred path state on a gateway can be lost following either a gateway reboot, or a restart of the target service  
  **Workaround**: Rerun the playbook to correct preferred paths following gateway or target service restart    
  **Issue**: The *rtslib* 'save_to_file' call does **not** persist alua state information in the saveconfig.json file, so when the service restarts the alua preferred setting is lost    
    
2. preferred paths are defined by using the name of the gateway, which assumes the gateway names resolves to the interface used for the iscsi service. This is a big assumption and needs to be addressed   
3. the ceph cluster name is the default 'ceph', so the corresponding configuration file /etc/ceph/ceph.conf is valid