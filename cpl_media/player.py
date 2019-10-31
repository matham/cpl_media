"""Player
===========

Provides the base class for video players.
"""

import logging
import traceback
import sys
from threading import Thread
from collections import namedtuple

import ffpyplayer
from ffpyplayer.pic import get_image_size
from ffpyplayer.tools import set_log_callback

from kivy.clock import Clock
from kivy.properties import (
    NumericProperty, ReferenceListProperty,
    ObjectProperty, ListProperty, StringProperty, BooleanProperty,
    DictProperty, AliasProperty, OptionProperty, ConfigParserProperty)
from kivy.event import EventDispatcher
from kivy.logger import Logger

from cpl_media import error_guard
from .common import KivyMediaBase
import cpl_media

__all__ = ('BasePlayer', 'VideoMetadata')

set_log_callback(logger=Logger, default_only=True)
logging.info('cpl_media: Using ffpyplayer {}'.format(ffpyplayer.__version__))

VideoMetadata = namedtuple('VideoMetadata', ['fmt', 'w', 'h', 'rate'])
"""namedtuple describing a video stream.
"""


class BasePlayer(EventDispatcher, KivyMediaBase):
    """Base class for every player.
    """

    __settings_attrs__ = ('metadata_play', 'metadata_play_used')

    display_frame = None
    """Called from second thread.
    """

    display_trigger = None

    frame_callbacks = []
    """Called from second thread.
    """

    error_callback = None

    play_thread = None

    play_state = StringProperty('none')
    '''Can be one of none, starting, playing, stopping.
    '''

    config_active = BooleanProperty(False)

    last_image = None

    last_image_t = 0

    use_real_time = False

    metadata_play = ObjectProperty(None)
    '''(internal) Describes the video metadata of the video player.
    '''

    metadata_play_used = ObjectProperty(None)
    '''(internal) Describes the video metadata of the video player that is
    actually used by the player.
    '''

    real_rate = NumericProperty(0)

    frames_played = NumericProperty(0)

    ts_play = NumericProperty(0)

    player_summery = StringProperty('')

    data_rate = NumericProperty(0)
    """MB/s.
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
            self.display_frame(self.last_image)

    def process_frame(self, frame, t):
        """Called from second thread

        :param frame:
        :param t:
        :return:
        """
        self.last_image = frame
        self.last_image_t = t
        for callback in self.frame_callbacks:
            callback((frame, t))
        self.display_trigger()

    def get_settings_attrs(self, attrs):
        d = {}
        for key in attrs:
            if key in ('metadata_play', 'metadata_play_used'):
                d[key] = tuple(getattr(self, key))
            else:
                d[key] = getattr(self, key)
        return d

    def apply_settings(self, settings):
        for k, v in settings.items():
            if k in ('metadata_play', 'metadata_play_used'):
                v = VideoMetadata(*v)
            setattr(self, k, v)

    @error_guard
    def play(self):
        """Called from main thread only, starts playing and sets play state to
        `starting`. Only called when :attr:`play_state` is `none`.
        """
        if self.play_state != 'none' or self.config_active:
            raise TypeError(
                'Asked to play while {} or configuring'.format(
                    self.play_state))

        self.play_state = 'starting'
        self.ts_play = self.real_rate = 0.
        self.frames_played = 0
        thread = self.play_thread = Thread(
            target=self.play_thread_run, name='Play thread')
        thread.start()

    @error_guard
    def stop(self, *largs, join=False):
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

    def _complete_start(self, *largs):
        # when this is called, there may be a subsequent call scheduled
        # to stop, from the internal thread, but the internal thread never
        # requests first to stop and then to be in playing state
        assert self.play_state != 'none'
        # only internal thread sets to playing, and only once
        assert self.play_state != 'playing'
        if self.play_state == 'starting':  # not stopping
            self.play_state = 'playing'

    def _complete_stop(self, *largs):
        assert self.play_state != 'none'

        self.play_thread = None
        self.image_queue = None
        self.play_state = 'none'

    def exception(self, e):
        (self.error_callback or cpl_media.error_callback)(
            e, exc_info=
            ''.join(traceback.format_exception(*sys.exc_info())))

    def play_thread_run(self):
        raise NotImplementedError

    def stop_all(self, join=False):
        super(BasePlayer, self).stop_all(join=join)
        self.stop(join=join)
