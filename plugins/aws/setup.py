from setuptools import setup, find_packages

setup(
    name='qodalis-cli-server-plugin-aws',
    version='1.0.0b1',
    packages=find_packages(),
    install_requires=['boto3>=1.34.0', 'qodalis-cli-server-abstractions'],
    extras_require={'test': ['pytest', 'pytest-asyncio', 'moto[all]>=5.0.0']},
)
