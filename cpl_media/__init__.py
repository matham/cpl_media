"""cpl_media
=================

A library providing kivy support for playing and recording from
various cameras.
"""
from functools import wraps
import sys

__all__ = ('error_guard', 'error_callback')

__version__ = '0.1.1.dev0'


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
            error_callback(e, exc_info=sys.exc_info(), threaded=True)

    return safe_func
