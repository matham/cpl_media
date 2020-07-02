"""FFmpeg based player
======================

:class:`FFmpegPlayer` can play USB cameras, video files, etc using
:mod:`ffpyplayer`.

"""
import re
import time
from collections import defaultdict
from functools import partial
from time import perf_counter as clock
from os.path import splitext, join, exists, isdir, abspath, dirname

from ffpyplayer.player import MediaPlayer
from ffpyplayer.pic import get_image_size
from ffpyplayer.tools import list_dshow_devices

from kivy.clock import Clock
from kivy.logger import Logger
from kivy.properties import StringProperty, DictProperty, BooleanProperty, \
    NumericProperty
from kivy.uix.boxlayout import BoxLayout
from kivy.lang import Builder

from cpl_media.player import BasePlayer, VideoMetadata
from cpl_media import error_guard

__all__ = ('FFmpegPlayer', 'FFmpegSettingsWidget')


def eat_first(f, val, *largs, **kwargs):
    f(*largs, **kwargs)


class FFmpegPlayer(BasePlayer):
    """Wrapper for :mod:`ffpyplayer` ffmpeg based player.
    """

    _config_props_ = (
        'play_filename', 'file_fmt', 'icodec',
        'dshow_true_filename', 'dshow_opt', 'use_dshow', 'dshow_rate',
        'dshow_filename')

    play_filename = StringProperty('')
    '''The filename of the media being played. Can be e.g. a filename etc.
    '''

    file_fmt = StringProperty('')
    '''The format used to play the video. Can be empty or a format e.g.
    ``mjpeg`` for webcams.
    '''

    icodec = StringProperty('')
    '''The codec used to open the video stream with if it needs to be
    specified for the camera.
    '''

    use_dshow = BooleanProperty(True)
    """Whether we use dshow - i.e. USB webcams, or normal media sources.
    """

    dshow_rate = NumericProperty(0)
    """The frame rate to request from the dshow camera.
    """

    dshow_filename = StringProperty('')
    """The name of the dshow camera to open.
    """

    dshow_true_filename = StringProperty('')
    '''The real and complete filename of the direct show (webcam) device.
    '''

    dshow_opt = StringProperty('')
    '''The camera options associated with :attr:`dshow_true_filename` when
    dshow is used.
    '''

    dshow_names = DictProperty({})
    """All the cameras that can be opened.
    """

    dshow_opts = DictProperty({})
    """The options supported by the cameras that can be opened.
    """

    dshow_opt_pat = re.compile(
        '([0-9]+)X([0-9]+) (.+), ([0-9.]+)(?: - ([0-9.]+))? fps')

    _config_dshow_filename = ''

    def __init__(self, **kw):
        super(FFmpegPlayer, self).__init__(**kw)

        self.fbind('play_filename', self._update_summary)
        self.fbind('dshow_filename', self._update_summary)
        self.fbind('file_fmt', self._update_summary)
        self.fbind('use_dshow', self._update_summary)
        self._update_summary()

    def _set_metadata_play(self, fmt, w, h):
        self.metadata_play = VideoMetadata(fmt, w, h, 0)

    def _update_summary(self, *largs):
        name = self.dshow_filename if self.use_dshow else self.play_filename
        self.player_summery = 'FFmpeg "{}"'.format(name)

    def apply_config_property(self, name, value):
        if name == 'dshow_filename':
            self._config_dshow_filename = value
        else:
            super(FFmpegPlayer, self).apply_config_property(name, value)

    def post_config_applied(self):
        """Handles settings as applied by the app config system so the
        properties are set to correct values.
        """
        # this must be set after everything so we can loop up its opts in dict
        dshow_filename = self._config_dshow_filename

        try:
            if self.dshow_opt:
                self._parse_dshow_opt(self.dshow_opt)
        except Exception:
            self.dshow_opt = ''

        if not self.dshow_true_filename or not dshow_filename or not \
                self.dshow_opt:
            dshow_filename = self.dshow_true_filename = ''
            self.dshow_opt = ''

        if dshow_filename:
            if dshow_filename not in self.dshow_names:
                self.dshow_names[dshow_filename] = \
                    self.dshow_true_filename

            if dshow_filename not in self.dshow_opts:
                self.dshow_opts[dshow_filename] = {}

            self.dshow_opts[dshow_filename][self.dshow_opt] = \
                self._parse_dshow_opt(self.dshow_opt)

        self.dshow_filename = dshow_filename
        super().post_config_applied()

    @error_guard
    def refresh_dshow(self):
        """Refreshes list of direct show cameras available.
        """
        counts = defaultdict(int)
        video, _, names = list_dshow_devices()
        video2 = {}
        names2 = {}

        # rename to have pretty unique names
        for true_name, name in names.items():
            if true_name not in video:
                continue

            count = counts[name]
            name2 = '{}-{}'.format(name, count) if count else name
            counts[name] = count + 1

            # filter and clean cam opts
            names2[name2] = true_name
            opts = video2[name2] = {}

            for fmt, _, (w, h), (rmin, rmax) in video[true_name]:
                if not fmt:
                    continue
                if rmin != rmax:
                    key = '{}X{} {}, {} - {} fps'.format(w, h, fmt, rmin, rmax)
                else:
                    key = '{}X{} {}, {} fps'.format(w, h, fmt, rmin)
                if key not in opts:
                    opts[key] = (fmt, (w, h), (rmin, rmax))

        self.dshow_opts = video2
        self.dshow_names = names2
        if self.dshow_filename not in names2:
            if not names2:
                self.dshow_filename = ''
                self.dshow_true_filename = ''
            else:
                self.dshow_filename = list(names2.keys())[0]
                self.dshow_true_filename = names2[self.dshow_filename]
        self.update_dshow_file()

    @error_guard
    def update_dshow_file(self):
        """Updates the dshow camera name and options in response to a
        re-configuration.
        """
        if not self.use_dshow or not self.dshow_filename:
            self.dshow_opt = ''
            self.dshow_true_filename = ''
            return

        assert self.dshow_filename in self.dshow_opts
        self.dshow_true_filename = self.dshow_names[self.dshow_filename]
        if self.dshow_opt in self.dshow_opts[self.dshow_filename]:
            return
        opts = list(self.dshow_opts[self.dshow_filename].keys())
        if not opts:
            self.dshow_opt = ''
        else:
            self.dshow_opt = opts[0]

    @error_guard
    def parse_dshow_opt(self, opt):
        """Parses the :attr:`dshow_opts` type string option into
        `(fmt, (w, h), (rmin, rmax))`
        """
        return self._parse_dshow_opt(opt)

    def _parse_dshow_opt(self, opt):
        m = re.match(self.dshow_opt_pat, opt)
        if m is None:
            raise ValueError('{} not a valid option'.format(opt))

        w, h, fmt, rmin, rmax = m.groups()
        if rmax is None:
            rmax = rmin

        w, h, rmin, rmax = int(w), int(h), float(rmin), float(rmax)
        return fmt, (w, h), (rmin, rmax)

    def get_opt_image_size(self, opt):
        fmt, (w, h), _ = self.parse_dshow_opt(opt)
        return w * h, sum(get_image_size(fmt, w, h))

    @error_guard
    def player_callback(self, mode, value):
        """Called internally by ffpyplayer when an error occurs internally.
        """
        if mode.endswith('error'):
            raise Exception(
                'FFmpeg Player: internal error "{}", "{}"'.format(mode, value))

    def play_thread_run(self):
        try:
            self._play_thread_run()
        except Exception as e:
            self.exception(e)
        finally:
            Clock.schedule_once(self.complete_stop)

    def _play_thread_run(self):
        process_frame = self.process_frame
        ff_opts = {'sync': 'video', 'an': True, 'sn': True, 'paused': True}

        ifmt, icodec = self.file_fmt, self.icodec
        use_dshow = self.use_dshow
        if ifmt:
            ff_opts['f'] = ifmt
        if use_dshow:
            ff_opts['f'] = 'dshow'
        if icodec:
            ff_opts['vcodec'] = icodec

        ipix_fmt, iw, ih, _ = self.metadata_play
        ff_opts['x'] = iw
        ff_opts['y'] = ih

        lib_opts = {}
        if use_dshow:
            rate = self.dshow_rate
            if self.dshow_opt:
                fmt, size, (rmin, rmax) = self.parse_dshow_opt(self.dshow_opt)
                lib_opts['pixel_format'] = fmt
                lib_opts['video_size'] = '{}x{}'.format(*size)
                if rate:
                    rate = min(max(rate, rmin), rmax)
                    lib_opts['framerate'] = '{}'.format(rate)
            elif rate:
                lib_opts['framerate'] = '{}'.format(rate)

        fname = self.play_filename
        if use_dshow:
            fname = 'video={}'.format(self.dshow_true_filename)

        ffplayer = MediaPlayer(
            fname, callback=self.player_callback, ff_opts=ff_opts,
            lib_opts=lib_opts)

        # wait for media to init pixel fmt
        src_fmt = ''
        s = clock()
        while self.play_state == 'starting' and clock() - s < 5.:
            src_fmt = ffplayer.get_metadata().get('src_pix_fmt')
            if src_fmt:
                break
            time.sleep(0.01)

        if not src_fmt:
            raise ValueError("Player failed, couldn't get pixel type")

        if ipix_fmt:
            src_fmt = ipix_fmt
        fmt = {'gray': 'gray', 'rgb24': 'rgb24', 'bgr24': 'rgb24',
               'rgba': 'rgba', 'bgra': 'rgba'}.get(src_fmt, 'yuv420p')
        ffplayer.set_output_pix_fmt(fmt)

        ffplayer.toggle_pause()
        Logger.info('FFmpeg Player: input, output formats are: {}, {}'.
                    format(src_fmt, fmt))

        # wait for first frame
        img = None
        s = clock()
        ivl_start = None
        while self.play_state == 'starting' and clock() - s < 5.:
            img, val = ffplayer.get_frame()
            if val == 'eof':
                raise ValueError("Player failed, reached eof")

            if img:
                ivl_start = clock()
                break
            time.sleep(0.01)

        rate = ffplayer.get_metadata().get('frame_rate')
        if rate == (0, 0) or not rate or not rate[1]:
            raise ValueError("Player failed, couldn't read frame rate")

        if not img:
            raise ValueError("Player failed, couldn't read frame")

        # ready to start
        rate = rate[0] / float(rate[1])
        w, h = img[0].get_size()
        fmt = img[0].get_pixel_format()
        use_rt = self.use_real_time

        Clock.schedule_once(
            partial(eat_first, self.update_metadata, rate=rate, w=w, h=h,
                    fmt=fmt), 0)
        Clock.schedule_once(self.complete_start)

        # started
        process_frame(img[0], {'t': ivl_start if use_rt else img[1]})

        min_sleep = 1 / (rate * 8.)
        self.setattr_in_kivy_thread('ts_play', ivl_start)
        self.setattr_in_kivy_thread('frames_played', 1)
        count = 1

        while self.play_state != 'stopping':
            img, val = ffplayer.get_frame()
            ivl_end = clock()

            if ivl_end - ivl_start >= 1.:
                real_rate = count / (ivl_end - ivl_start)
                self.setattr_in_kivy_thread('real_rate', real_rate)
                count = 0
                ivl_start = ivl_end

            if val == 'paused':
                raise ValueError("Player {} got {}".format(self, val))
            if val == 'eof':
                break

            if not img:
                time.sleep(min(val, min_sleep) if val else min_sleep)
                continue
            elif val:
                ts = clock()
                leftover = val
                while leftover > min_sleep and \
                        self.play_state != 'stopping':
                    time.sleep(min_sleep)
                    leftover = max(val - (clock() - ts), 0)

            count += 1
            self.increment_in_kivy_thread('frames_played')
            process_frame(img[0], {'t': ivl_end if use_rt else img[1]})


class FFmpegSettingsWidget(BoxLayout):
    """Settings widget for :class:`FFmpegPlayer`.
    """

    player: FFmpegPlayer = None
    """The player.
    """

    def __init__(self, player=None, **kwargs):
        if player is None:
            player = FFmpegPlayer()
        self.player = player
        super(FFmpegSettingsWidget, self).__init__(**kwargs)

    def set_filename(self, text_wid, paths):
        """Called by the GUI to set the filename.
        """
        if not paths:
            return

        self.player.play_filename = paths[0]
        text_wid.text = paths[0]


Builder.load_file(join(dirname(__file__), 'ffmpeg_player.kv'))
