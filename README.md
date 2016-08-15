# igw-ceph-ansible
Ansible modules and playbook for setting up iscsi gateway(s) for a ceph cluster

##Introduction
The goal of this project is to provide a simple means of maintaining configuration state across a number of iscsi (LIO) gateways that front a ceph cluster. The code uses a number of custom modules to handle the following 
functional tasks

* definition of rbd images (including resize support)  
* iSCSI gateway creation (single tpg, single portal, initial lun maps)  
* Client assignment (registering clients to LIO, chap authentication, and associating the client to specific rbd images)  
* lun/gateway balancing (backlog item!)  
  
In addition to building the required configuration, the project also provide a playbook for purging the gateway configuration including the removal of rbd's.

##Prerequisites  
* a working ceph cluster ( *rbd pool defined* )  
* nodes intended to be gateways should be at least ceph clients  
* ansible installed on a *controller*, with passwordless ssh set up between the controller and the gateway nodes  

##Quick Start
  
  1. Unzip the project archive on your ansible controller host  
  2. Install the ceph_iscsi_gw package on each of the nodes.  
  2.a In the *root* of the project's archive on your ansible controller host  
        ```tar cvzf ceph_iscsi_gw.tar.gz common/```  
        ```scp ceph_iscsi_gw.tar.gz <NODE>:~/.```  
  2.b On each node, install the package  
        ```tar xvzf ceph_iscsi_gw.tar.gz```  
        ```cd common```  
        ```python setup.py install```  
  *This provides the common python class used by the custom ansible modules when interacting with the rados configuration object.*  
    
  3. Configure the playbook on the controller  
  3.a Update the **'hosts'** file to match the node names/ip's for the gateways you want  
  3.b update **group_vars/ceph_iscsi_gw.yml** file to define the gateway name and IP, rbd images and client connections.    
  4. run the playbook    
  ```> ansible-playbook -i hosts easy-gw.yml```  
  
  To purge the configuration  
  ```> ansible-playbook -i hosts purge_gateways.yml```  
  *NB. By default this will delete the gateway LIO configuration **and** any rbd's declared within the original configuration*  
  
##Features    
  
- confirms RHEL7.3 - and aborts if necessary
- ensures targetcli is installed (for rtslib support)
- creates rbd's if needed
- checks the size of the rbds at run time and expands if necessary
- maps the rbds to the host (gateway)
- maps these rbds to LIO
- creates an iscsi target - common iqn, and tpg
- adds a portal ip based on a given network CIDR
- adds all the mapped luns to the tpg (ready for client assignment)
- add clients to the gateways, with/without CHAP
- images mapped to clients can be added/removed by changing image_list and rerunning the playbook
- clients can be removed using the state=absent variable and rerunning the playbook. At this point the entry can be 
  removed from the variables file
- configuration can be wiped with the purge_cluster playbook
- current state can be seen by looking at the configuration object (stored in the rbd pool)