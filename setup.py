# -*- coding: utf-8 -*-
from setuptools import setup, find_packages

from woocommerceconnector import __version__ as version

with open('requirements.txt') as f:
	install_requires = f.read().strip().split('\n')

setup(
	name='woocommerceconnector',
	version=version,
	description='WooCommerce Connector for ERPNext',
	author='libracore',
	author_email='info@libracore.com',
	packages=find_packages(),
	zip_safe=False,
	include_package_data=True,
	install_requires=install_requires
)