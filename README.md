# igw-ceph-ansible
Ansible modules and playbook for setting up iscsi gateway for a ceph cluster

##Introduction
The goal of this project is to provide a simple means of maintaining configuration state across a number of iscsi (LIO) gateways that front a ceph cluster. The code uses a number of custom modules to handle the following 
functional tasks

* definition of rbd images (including resize support)  
* iSCSI gateway creation (single tpg, single portal, initial lun maps)  
* Client assignment (registering clients to LIO, chap authentication, and associating the client to specific rbd images)  
* lun/gateway balancing (not implemented yet)

##Quick Start
