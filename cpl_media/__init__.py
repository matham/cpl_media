"""cpl_media
=================

A library providing kivy support for playing and recording from
various cameras.
"""
from functools import wraps
import traceback
import sys
import pathlib
import os

__all__ = ('error_guard', 'error_callback', 'get_pyinstaller_datas')

__version__ = '0.1.3'


def _error_callback(e, exc_info=None, threaded=False):
    pass


error_callback = _error_callback
"""When set, care must be taken to handle errors from secondary threads.

It's signature is ``error_callback(e, exc_info=None, threaded=False)``.
"""


def error_guard(error_func):
    """A decorator which wraps the function in `try...except` and calls
    :func:`error_callback` if a exception is raised.

    E.g.::

        @error_guard
        def do_something():
            do_something_interesting
    """
    @wraps(error_func)
    def safe_func(*largs, **kwargs):
        try:
            return error_func(*largs, **kwargs)
        except Exception as e:
            exc_info = sys.exc_info()
            stack = traceback.extract_stack()
            tb = traceback.extract_tb(exc_info[2])
            full_tb = stack[:-1] + tb
            exc_line = traceback.format_exception_only(*exc_info[:2])

            err = 'Traceback (most recent call last):'
            err += "".join(traceback.format_list(full_tb))
            err += "".join(exc_line)
            error_callback(e, exc_info=err, threaded=True)

    return safe_func


def get_pyinstaller_datas():
    """Returns the ``datas`` list required by PyInstaller to be able to package
    :mod:`cpl_media` in a application.

    """
    root = pathlib.Path(os.path.dirname(sys.modules[__name__].__file__))
    datas = []
    for pat in ('**/*.kv', '*.kv'):
        for f in root.glob(pat):
            datas.append((str(f), str(f.relative_to(root.parent).parent)))

    return datas
