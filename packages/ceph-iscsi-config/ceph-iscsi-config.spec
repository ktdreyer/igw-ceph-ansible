Name:           ceph-iscsi-ansible
Version:        0.5
Release:        1%{?dist}
Summary:        Ansible playbooks for deploying LIO iscsi gateways in front of Ceph
License:        ASL 2.0
URL:            https://github.com/pcuzner/igw-ceph-ansible
Source0:        https://github.com/pcuzner/igw-ceph-ansible/packages/%{name}/%{name}-%{version}.tar.gz
BuildRoot:      %(mktemp -ud %{_tmppath}/%{name}-%{version}-%{release}-XXXXXX)
BuildArch:      noarch

Requires: ansible
Requires: ceph-ansible

%description
Ansible playbooks that define nodes as iSCSI gateways (LIO). Once complete, the LIO instance on
each node provides an ISCSI endpoint for clients to connect to. The playbook defines the front-end
iSCSI environment (target -> tpgN -> NodeACLS/client), as well as the underlying rbd definition for
the rbd images exported over LIO

%prep
%setup -q -n %{name}

%install
mkdir -p %{buildroot}%{_datarootdir}/ceph-ansible

for f in group_vars library roles ceph-iscsi-gw.yml; do
  cp -a $f %{buildroot}%{_datarootdir}/ceph-ansible
done

%files
%{_datarootdir}/ceph-ansible/group_vars/ceph-iscsi-gw.yml
%{_datarootdir}/ceph-ansible/roles/ceph-iscsi-gw
%{_datarootdir}/ceph-ansible/library/igw*
%{_datarootdir}/ceph-ansible/ceph-iscsi-gw.yml
%exclude %{_datarootdir}/ceph-ansible/library/igw*.pyo
%exclude %{_datarootdir}/ceph-ansible/library/igw*.pyc

%changelog
* Tue Sep 27 2016 Paul Cuzner <pcuzner@redhat.com> - 0.5-1
- initial rpm package


