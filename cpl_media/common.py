"""Base classes
=========================

Provides common base classes and tools.

"""

from queue import Queue, Empty
import sys
import traceback
from base_kivy_app.config import Configurable

from kivy.clock import Clock

from cpl_media import error_guard
import cpl_media

__all__ = ('KivyMediaBase', )


class KivyMediaBase(Configurable):
    """A base classes for all the players and recorders.

    It provides methods for the kivy and internal threads to interact safely.
    Specifically, for the internal threads to schedule code to be executed in
    the kivy thread.
    """

    trigger_run_in_kivy = None
    """A Kivy clock trigger that will cause
    :meth:`process_queue_in_kivy_thread` to be called on the next frame in the
    kivy thread.
    """

    kivy_thread_queue = None
    """The queue that the kivy thread will read from and process messages.
    """

    def __init__(self, **kwargs):
        super(KivyMediaBase, self).__init__(**kwargs)
        self.kivy_thread_queue = Queue()
        self.trigger_run_in_kivy = Clock.create_trigger(
            self.process_queue_in_kivy_thread)

    @error_guard
    def process_queue_in_kivy_thread(self, *largs):
        """Method that is called in the kivy thread when
        :attr:`trigger_run_in_kivy` is triggered. It reads messages from the
        thread.
        """
        while self.kivy_thread_queue is not None:
            try:
                msg, value = self.kivy_thread_queue.get(block=False)

                if msg == 'setattr':
                    prop, val = value
                    setattr(self, prop, val)
                elif msg == 'increment':
                    prop, val = value
                    setattr(self, prop, getattr(self, prop) + val)
                else:
                    print('Got unknown KivyMediaBase message', msg, value)
            except Empty:
                break

    def setattr_in_kivy_thread(self, prop, value):
        """Schedules kivy to set the property to the specified value in the
        kivy thread.

        :param prop: The instance property name to set.
        :param value: The value the property will be set to.
        """
        self.kivy_thread_queue.put(('setattr', (prop, value)))
        self.trigger_run_in_kivy()

    def increment_in_kivy_thread(self, prop, value=1):
        """Schedules kivy to increment the property by the specified value in
        the kivy thread.

        :param prop: The instance property name to increment.
        :param value: The value by which it will be incremented.
        """
        self.kivy_thread_queue.put(('increment', (prop, value)))
        self.trigger_run_in_kivy()

    def stop_all(self, join=False):
        """Causes all internal threads to stop and exit.

        :param join: Whether to wait and block the calling thread until the
            internal threads exit.
        """
        pass

    def exception(self, e):
        """Called from under an exception, to report the exception to
        :func:`cpl_media.error_callback`.

        :param e: The exception instance.
        """
        cpl_media.error_callback(
            e, exc_info=''.join(traceback.format_exception(*sys.exc_info())),
            threaded=True)
