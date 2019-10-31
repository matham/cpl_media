"""Commonly used classes
=========================

Contains common base classes and tools.

"""

from queue import Queue, Empty

from kivy.clock import Clock

from cpl_media import error_guard

__all__ = ('KivyMediaBase', )


class KivyMediaBase(object):
    """A common base classes for all the players and recorders.

    It provides interaction with the kivy thread.
    """

    trigger_run_in_kivy = None

    kivy_thread_queue = None

    def __init__(self, **kwargs):
        super(KivyMediaBase, self).__init__(**kwargs)
        self.kivy_thread_queue = Queue()
        self.trigger_run_in_kivy = Clock.create_trigger(
            self.process_queue_in_kivy_thread)

    @error_guard
    def process_queue_in_kivy_thread(self, *largs):
        while self.kivy_thread_queue is not None:
            try:
                msg, value = self.kivy_thread_queue.get(block=False)

                if msg == 'setattr':
                    prop, val = value
                    setattr(self, prop, value)
                elif msg == 'increment':
                    prop, val = value
                    setattr(self, prop, getattr(self, prop) + value)
                else:
                    print('Got unknown KivyMediaBase message', msg, value)
            except Empty:
                break

    def setattr_in_kivy_thread(self, prop, value):
        self.kivy_thread_queue.put(('setattr', (prop, value)))
        self.trigger_run_in_kivy()

    def increment_in_kivy_thread(self, prop, value=1):
        self.kivy_thread_queue.put(('increment', (prop, value)))
        self.trigger_run_in_kivy()

    def stop_all(self, join=False):
        pass
