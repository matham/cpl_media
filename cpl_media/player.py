"""Player
===========

Provides the base class for video players.
"""

import logging
from threading import Thread
from collections import namedtuple

from ffpyplayer.pic import get_image_size

from kivy.clock import Clock
from kivy.properties import (
    NumericProperty, ObjectProperty, StringProperty, BooleanProperty)
from kivy.event import EventDispatcher

from cpl_media import error_guard
from .common import KivyMediaBase

__all__ = ('BasePlayer', 'VideoMetadata')

VideoMetadata = namedtuple('VideoMetadata', ['fmt', 'w', 'h', 'rate'])
"""Namedtuple type describing a video stream.
"""


class BasePlayer(EventDispatcher, KivyMediaBase):
    """Base class for every player.
    """

    _config_props_ = ('metadata_play', 'metadata_play_used')

    display_frame = None
    """Called from kivy thread to display the frame whenever a new image
    arrives. This is called once per kivy frame, so if multiple images arrive
    during a kivy frame, it is called only for the last one.

    This callback takes two arguments: ``(image, metadata)``, where ``image``
    is the :class:`ffpyplayer.pic.Image`, and ``metadata`` is a dict with
    metadata.
    """

    display_trigger = None
    """Clock trigger to call :meth:`display_frame` in the kivy thread.
    """

    frame_callbacks = []
    """A list of callbacks that are called from the internal thread whenever
    a new image is available.

    All the callbacks are called with a single tuple argument
    ``(image, metadata)``, where ``image`` is the
    :class:`ffpyplayer.pic.Image`, and ``metadata`` is a dict with metadata.

    It always contains at least the key ``'t'`` indicating the timestamp of the
    image, but it may also contains other metadata keys specific to the player
    such as ``'count'`` for the frame number, when sent by the camera.
    """

    play_thread = None
    """The thread that plays the camera.
    """

    can_play = BooleanProperty(True)
    """Whether the video source can play now.
    """

    play_state = StringProperty('none')
    '''The current state of the state machine of the player.

    Can be one of none, starting, playing, stopping.
    '''

    last_image = None
    """The last :class:`ffpyplayer.pic.Image` received by the camera.
    """

    last_image_metadata = {'t': 0}
    """The metadata of the last image received by the camera.
    """

    use_real_time = False
    """Whether the video should use the current real time when we got the
    image, e.g. when the camera provided timestamp is not reliable.
    """

    metadata_play = ObjectProperty(None)
    '''(internal) Describes the video metadata of the video player. This is
    the requested format, or best guess of the metadata.

    Read only.
    '''

    metadata_play_used = ObjectProperty(None)
    '''(internal) Describes the video metadata of the video player that is
    actually used by the player. This must be set before recorders may allow
    recording the player.

    Depending on the metadata needed by the recorder, it may refuse to
    record until the needed metadata is given.

    Read only.
    '''

    real_rate = NumericProperty(0)
    """The estimated real fps of the video source being played.
    """

    frames_played = NumericProperty(0)
    """The number of frames that have been played since :meth:`play`
    was called.
    """

    ts_play = NumericProperty(0)
    """The time when the camera started playing.
    """

    player_summery = StringProperty('')
    """Textual summary of the camera type and config options.
    """

    data_rate = NumericProperty(0)
    """The estimated rate in B/s at which the camera is playing.
    """

    def __init__(self, **kwargs):
        self.frame_callbacks = []
        self.metadata_play = VideoMetadata(
            *kwargs.pop('metadata_play', ('', 0, 0, 0)))
        self.metadata_play_used = VideoMetadata(
            *kwargs.pop('metadata_play_used', ('', 0, 0, 0)))

        super(BasePlayer, self).__init__(**kwargs)
        self.display_trigger = Clock.create_trigger(self._display_frame, 0)

        self.fbind('metadata_play', self._update_data_rate)
        self.fbind('metadata_play_used', self._update_data_rate)
        self._update_data_rate()

    def _update_data_rate(self, *largs):
        fmt = self.metadata_play_used.fmt or self.metadata_play.fmt
        w = self.metadata_play_used.w or self.metadata_play.w
        h = self.metadata_play_used.h or self.metadata_play.h
        rate = self.metadata_play_used.rate or self.metadata_play.rate or 30
        if not fmt or not w or not h:
            self.data_rate = 0
        else:
            self.data_rate = sum(get_image_size(fmt, w, h)) * rate

    def _display_frame(self, *largs):
        if self.display_frame is not None:
            self.display_frame(self.last_image, self.last_image_metadata)

    def process_frame(self, frame, metadata):
        """Called from internal thread to process a new image frame received.

        :param frame: The :class:`ffpyplayer.pic.Image`.
        :param metadata: The metadata of the image. See
            :attr:`frame_callbacks`.
        """
        self.last_image = frame
        self.last_image_metadata = metadata
        for callback in self.frame_callbacks:
            callback((frame, metadata))
        self.display_trigger()

    def get_config_property(self, name):
        """(internal) used by the config system to get the special config data
        of the player.
        """
        if name == 'metadata_play':
            return tuple(self.metadata_play)
        if name == 'metadata_play_used':
            return tuple(self.metadata_play_used)
        return getattr(self, name)

    def apply_config_property(self, name, value):
        """(internal) used by the config system to set the special config data
        of the player.
        """
        if name == 'metadata_play':
            self.metadata_play = VideoMetadata(*value)
        elif name == 'metadata_play_used':
            self.metadata_play_used = VideoMetadata(*value)
        else:
            setattr(self, name, value)

    @error_guard
    def play(self):
        """Starts playing the video source and sets the :attr:`play_state` to
        `starting`.

        May be called from main kivy thread only.

        May only be called when :attr:`play_state` is `none`, otherwise an
        exception is raised.

        Players need to eventually call :meth:`complete_start` to finish
        starting playing.
        """
        if self.play_state != 'none' or not self.can_play:
            raise TypeError(
                'Asked to play while {} or disabled'.format(self.play_state))

        self.play_state = 'starting'
        self.ts_play = self.real_rate = 0.
        self.frames_played = 0
        thread = self.play_thread = Thread(
            target=self.play_thread_run, name='Play thread')
        thread.start()

    @error_guard
    def stop(self, *largs, join=False):
        """Stops playing the video source, if it is playing and sets the
        :attr:`play_state` to `stopping`.

        Players need to eventually call :meth:`complete_stop` to finish
        stopping playing.

        :param join: whether to block the thread until the internal play thread
            has exited.
        :return: Whether we stopped playing (True) or were already
            stop(ping/ed) playing.
        """
        if self.play_state == 'none':
            assert self.play_thread is None
            return False

        assert self.play_thread is not None

        if self.play_state == 'stopping':
            if join:
                self.play_thread.join()
            return False

        self.play_state = 'stopping'
        if join:
            self.play_thread.join()
        return True

    def update_metadata(self, fmt=None, w=None, h=None, rate=None):
        """Sets :attr:`metadata_play_used` based on :attr:`metadata_play` and
        any values provided to the function, if not None.
        """
        ifmt, iw, ih, irate = self.metadata_play
        if fmt is not None:
            ifmt = fmt
        if w is not None:
            iw = w
        if h is not None:
            ih = h
        if rate is not None:
            irate = rate

        self.metadata_play_used = VideoMetadata(ifmt, iw, ih, irate)

    def complete_start(self, *largs):
        """After :meth:`play`, this is called to set the player into `playing`
        :attr:`play_state`.
        """
        # when this is called, there may be a subsequent call scheduled
        # to stop, from the internal thread, but the internal thread never
        # requests first to stop and then to be in playing state
        assert self.play_state != 'none'
        # only internal thread sets to playing, and only once
        assert self.play_state != 'playing'
        if self.play_state == 'starting':  # not stopping
            self.play_state = 'playing'

    def complete_stop(self, *largs):
        """After :meth:`stop`, this is called to set the player into `none`
        :attr:`play_state`.
        """
        assert self.play_state != 'none'

        self.play_thread = None
        self.play_state = 'none'

    def play_thread_run(self):
        """The method that runs in the internal play thread.
        """
        raise NotImplementedError

    def stop_all(self, join=False):
        logging.info('cpl_media: Stopping all "{}"'.format(self))
        super(BasePlayer, self).stop_all(join=join)
        self.stop(join=join)
