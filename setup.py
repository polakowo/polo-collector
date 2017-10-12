from setuptools import setup

setup(
    name='plnxgrabber',
    version='1.3', # MAJOR.MINOR
    description='Grabber of trade history from Poloniex exchange',
    url='https://github.com/polakowo/plnx-grabber',
    author='polakowo',
    license='GPL v3',
    packages=['plnxgrabber', 'mongots'],
    install_requires=['arrow', 'pandas', 'poloniex', 'pymongo'],
    python_requires='>=3'
)
