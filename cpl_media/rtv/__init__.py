"""RTV24 based player
======================

This player can play RTV24 camera feeds using :mod:`pybarst`.
"""
from time import perf_counter as clock
import sys
import itertools
from os.path import splitext, join, exists, isdir, abspath, dirname, isfile

from ffpyplayer.pic import Image

from kivy.clock import Clock
from kivy.properties import (
    NumericProperty, StringProperty, BooleanProperty)
from kivy.uix.boxlayout import BoxLayout
from kivy.logger import Logger
from kivy.lang import Builder

from cpl_media.player import BasePlayer, VideoMetadata
from cpl_media import error_guard

try:
    import pybarst
    from pybarst.core.server import BarstServer
    from pybarst.rtv import RTVChannel
except ImportError as err:
    RTVChannel = BarstServer = None
    Logger.debug('cpl_media: Could not import pybarst: {}'.format(err))

__all__ = ('RTVPlayer', 'RTVSettingsWidget')


class RTVPlayer(BasePlayer):
    """Wrapper for RTV based player.
    """

    _config_props_ = ('remote_computer_name', 'pipe_name', 'port',
                          'video_fmt', 'pixel_fmt')

    video_fmts = {
        'full_NTSC': (640, 480), 'full_PAL': (768, 576),
        'CIF_NTSC': (320, 240), 'CIF_PAL': (384, 288),
        'QCIF_NTSC': (160, 120), 'QCIF_PAL': (192, 144)
    }
    """The video size formats supported by the player.
    """

    video_fmts_inverse = {v: k for k, v in video_fmts.items()}
    """Inverse of :attr:`video_fmts`.
    """

    image_fmts = {
        'rgb16': 'rgb565le', 'gray': 'gray', 'rgb15': 'rgb555le',
        'rgb24': 'rgb24', 'rgb32': 'rgba'}
    """The pixel formats from RTV -> ffmpeg, supported by the cameras.
    """

    remote_computer_name = StringProperty('')
    '''The name of the computer running Barst, if it's a remote computer.
    Otherwise it's the empty string.
    '''

    pipe_name = StringProperty('RTVPlayer')
    '''The internal name used to communicate with Barst. When running remotely,
    or if the server already is open, the name is used to discover Barst.
    '''

    port = NumericProperty(0)
    '''The RTV port (camera number) on the card to use.
    '''

    pixel_fmt = StringProperty('gray')
    '''The pixel format of the images being played.

    It can be one of the keys in :attr:`image_fmts`.
    '''

    video_fmt = StringProperty('full_NTSC')
    '''The video format of the video being played.

    It can be one of the keys in :attr:`video_fmts`.
    '''

    is_available = BooleanProperty(BarstServer is not None)
    """Whether pybarst is available to play."""

    barst_server = None
    """The :class:`pybarst.core.server.BarstServer` instance.
    """

    def __init__(self, **kwargs):
        super(RTVPlayer, self).__init__(**kwargs)

        self.fbind('remote_computer_name', self._update_summary)
        self.fbind('pipe_name', self._update_summary)
        self.fbind('port', self._update_summary)
        self._update_summary()

        self.fbind('video_fmt', self._update_metadata)
        self.fbind('pixel_fmt', self._update_metadata)
        self._update_metadata()

    def _update_summary(self, *largs):
        local = not self.remote_computer_name
        name = self.remote_computer_name if not local else '.'
        pipe_name = self.pipe_name

        self.player_summery = r'RTV "\\{}\pipe\{}:{}"'.format(
            name, pipe_name, self.port)

    def _update_metadata(self, *largs):
        w, h = self.video_fmts[self.video_fmt]
        pix_fmt = self.image_fmts[self.pixel_fmt]
        self.metadata_play = self.metadata_play_used = VideoMetadata(
            pix_fmt, w, h, 29.97)

    def play_thread_run(self):
        chan = None
        try:
            process_frame = self.process_frame
            paths = list(pybarst.dep_bins)
            if hasattr(sys, '_MEIPASS'):
                paths.append(sys._MEIPASS)

            barst_bin = None
            for p, f in itertools.product(paths, ('Barst64.exe', 'Barst.exe')):
                fname = join(abspath(p), f)
                if isfile(fname):
                    barst_bin = fname
                    break

            local = not self.remote_computer_name
            name = self.remote_computer_name if not local else '.'
            pipe_name = self.pipe_name
            full_name = r'\\{}\pipe\{}'.format(name, pipe_name)

            img_fmt = self.pixel_fmt
            ffmpeg_pix_fmt = self.image_fmts[img_fmt]
            w, h = self.video_fmts[self.video_fmt]
            video_fmt = self.video_fmt
            port = self.port

            if self.barst_server is None:
                self.barst_server = BarstServer(
                    barst_path=barst_bin, pipe_name=full_name)

            server = self.barst_server
            server.open_server()

            chan = RTVChannel(
                chan=port, server=server, video_fmt=video_fmt,
                frame_fmt=img_fmt, luma_filt=img_fmt == 'gray', lossless=True)

            chan.open_channel()
            try:
                chan.close_channel_server()
            except Exception:
                pass
            chan.open_channel()
            chan.set_state(True)

            started = False
            # use_rt = self.use_real_time
            count = 0
            ivl_start = 0

            while self.play_state != 'stopping':
                ts, buf = chan.read()
                if not started:
                    ivl_start = clock()
                    self.setattr_in_kivy_thread('ts_play', ivl_start)
                    Clock.schedule_once(self.complete_start)
                    started = True

                ivl_end = clock()
                if ivl_end - ivl_start >= 1.:
                    real_rate = count / (ivl_end - ivl_start)
                    self.setattr_in_kivy_thread('real_rate', real_rate)
                    count = 0
                    ivl_start = ivl_end

                count += 1
                self.increment_in_kivy_thread('frames_played')

                img = Image(
                    plane_buffers=[buf], pix_fmt=ffmpeg_pix_fmt, size=(w, h))
                process_frame(img, {'t': ts})
        except Exception as e:
            self.exception(e)
        finally:
            try:
                if chan is not None:
                    chan.close_channel_server()
            finally:
                Clock.schedule_once(self.complete_stop)

    @error_guard
    def stop_all(self, join=False):
        super(RTVPlayer, self).stop_all(join=join)

        barst_server = self.barst_server
        if barst_server is not None:
            barst_server.close_server()
            self.barst_server = None


class RTVSettingsWidget(BoxLayout):
    """Settings widget for :class:`RTVPlayer`.
    """

    player: RTVPlayer = None
    """The player.
    """

    def __init__(self, player=None, **kwargs):
        if player is None:
            player = RTVPlayer()
        self.player = player
        super(RTVSettingsWidget, self).__init__(**kwargs)


Builder.load_file(join(dirname(__file__), 'rtv_player.kv'))
