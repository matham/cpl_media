.. _install-cpl_media:

*************
Installation
*************

Dependencies
-------------

* Python 3.6+
* Kivy Master `E.g. Windows nightly wheels <https://kivy.org/docs/installation/installation-windows.html#nightly-wheel-installation>`_
* Dependencies automatically installed with pip and listed in ``setup.py``.
* Optional dependencies that should be installed depending on the cameras to be used:
  * Thor camera: :mod:`thorcam`.
  * RTV analog camera board: :mod:`pybarst`.
  * Flir camera: :mod:`rotpy`.

Installing CPL_Media
---------------------
After installing the dependencies cpl_media can be installed using::

    pip install cpl_media

to get the last release from pypi, or to get cpl_media master for the most current cpl_media version do::

    pip install https://github.com/matham/cpl_media/archive/master.zip
