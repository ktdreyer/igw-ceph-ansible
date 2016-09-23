#!/usr/bin/python

from setuptools import setup
import distutils.command.install_scripts
import shutil

f = open('README')
long_description = f.read().strip()
f.close()

# idea from http://stackoverflow.com/a/11400431/2139420
class strip_py_ext(distutils.command.install_scripts.install_scripts):
    def run(self):
        distutils.command.install_scripts.install_scripts.run(self)
        for script in self.get_outputs():
            if script.endswith(".py"):
                shutil.move(script, script[:-3])


setup(
    name = "ceph_iscsi_gw",
    version= "0.4o",
    description= "Common classes/functions across the ceph-ansible-iscsi modules",
    long_description = long_description,
    author = "Paul Cuzner",
    author_email = "pcuzner@redhat.com",
    url = "some URL",
    license = "GPLv3",
    packages = [
        "ceph_iscsi_gw"
        ]
    #scripts = [
    #    'igw_config.py'
    #]
)
