from setuptools import setup, find_packages
from io import open
from os import path

from cpl_media import __version__

here = path.abspath(path.dirname(__file__))

with open(path.join(here, 'README.md'), encoding='utf-8') as f:
    long_description = f.read()

URL = 'https://github.com/matham/cpl_media'

setup(
    name='cpl_media',
    version=__version__,
    author='Matthew Einhorn',
    author_email='moiein2000@gmail.com',
    license='MIT',
    description=(
        'Kivy support for playing and recording various cameras.'),
    long_description=long_description,
    long_description_content_type='text/markdown',
    url=URL,
    classifiers=[
        'Development Status :: 4 - Beta',
        'Intended Audience :: Developers',
        'Topic :: Software Development :: Libraries',
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: 3.7',
        'Programming Language :: Python :: 3.8',
    ],
    packages=find_packages(),
    install_requires=['base_kivy_app', 'ffpyplayer', 'kivy', 'tree-config'],
    extras_require={
        'dev': ['pytest>=3.6', 'pytest-cov', 'flake8', 'sphinx-rtd-theme',
                'coveralls', 'trio', 'pytest-trio'],
    },
    package_data={'cpl_media': ['*.kv', '**/*.kv']},
    project_urls={
        'Bug Reports': URL + '/issues',
        'Source': URL,
    },
    entry_points={
        'console_scripts': ['cpl_media=cpl_media.tests.app.demo_app:run_app']},
)
