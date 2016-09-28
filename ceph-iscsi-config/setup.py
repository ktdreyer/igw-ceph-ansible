#!/usr/bin/python

from setuptools import setup
import distutils.command.install_scripts
import shutil

f = open('README')
long_description = f.read().strip()
f.close()

setup(
    name="ceph_iscsi_config",
    version="0.5",
    description="Common classes/functions used to configure iscsi gateways backed by ceph/kRBD",
    long_description=long_description,
    author="Paul Cuzner",
    author_email="pcuzner@redhat.com",
    url="http://github.com/pcuzner/igw-ceph-ansible",
    license="GPLv3",
    packages=[
        "ceph_iscsi_config"
        ]
)
