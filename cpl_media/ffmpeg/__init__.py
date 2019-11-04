"""FFmpeg based player
======================

This can play USB cameras, video files, etc.

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
    '''Wrapper for ffmapeg based player.
    '''

    __settings_attrs__ = (
        'play_filename', 'file_fmt', 'icodec',
        'dshow_true_filename', 'dshow_opt', 'use_dshow', 'dshow_rate')

    play_filename = StringProperty('')
    '''The filename of the media being played. Can be e.g. a url etc.
    '''

    file_fmt = StringProperty('')
    '''The format used to play the video. Can be empty or a format e.g.
    ``dshow`` for webcams.
    '''

    icodec = StringProperty('')
    '''The codec used to open the video stream with.
    '''

    use_dshow = BooleanProperty(True)

    dshow_rate = NumericProperty(0)

    dshow_true_filename = StringProperty('')
    '''The real and complete filename of the direct show (webcam) device.
    '''

    dshow_opt = StringProperty('')
    '''The camera options associated with :attr:`dshow_true_filename` when
    dshow is used.
    '''

    dshow_names = DictProperty({})

    dshow_opts = DictProperty({})

    dshow_opt_pat = re.compile(
        '([0-9]+)X([0-9]+) (.+), ([0-9\\.]+)(?: - ([0-9\\.]+))? fps')

    def __init__(self, **kw):
        play_filename = kw.get('play_filename')
        use_dshow = kw.get('use_dshow')
        dshow_true_filename = kw.get('dshow_true_filename')
        dshow_opt = kw.get('dshow_opt')

        if (use_dshow and play_filename and dshow_true_filename and
                dshow_opt):
            self.dshow_names = {play_filename: dshow_true_filename}
            self.dshow_opts = {play_filename:
                               {dshow_opt: self.parse_dshow_opt(dshow_opt)}}
        super(FFmpegPlayer, self).__init__(**kw)

        self.fbind('play_filename', self._update_summary)
        self.fbind('file_fmt', self._update_summary)
        self.fbind('use_dshow', self._update_summary)
        self._update_summary()

    def _set_metadata_play(self, fmt, w, h):
        self.metadata_play = VideoMetadata(fmt, w, h, 0)

    def _update_summary(self, *largs):
        fname = self.play_filename
        if not self.file_fmt and not self.use_dshow:
            fname = splitext(fname)[0]

        if len(fname) > 8:
            name = fname[:4] + '...' + fname[-4:]
        else:
            name = fname
        self.player_summery = 'FFMpeg-{}'.format(name)

    @error_guard
    def refresh_dshow(self):
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
        if self.play_filename not in names2:
            if not names2:
                self.play_filename = ''
                self.dshow_true_filename = ''
            else:
                self.play_filename = list(names2.keys())[0]
                self.dshow_true_filename = names2[self.play_filename]
        self.update_dshow_file()

    @error_guard
    def update_dshow_file(self):
        if not self.use_dshow or not self.play_filename:
            self.dshow_opt = ''
            self.dshow_true_filename = ''
            return

        assert self.play_filename in self.dshow_opts
        self.dshow_true_filename = self.dshow_names[self.play_filename]
        if self.dshow_opt in self.dshow_opts[self.play_filename]:
            return
        opts = list(self.dshow_opts[self.play_filename].keys())
        if not opts:
            self.dshow_opt = ''
        else:
            self.dshow_opt = opts[0]

    @error_guard
    def parse_dshow_opt(self, opt):
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
        if mode.endswith('error'):
            raise Exception(
                'FFmpeg Player: internal error "{}", "{}"'.format(mode, value))

    def play_thread_run(self):
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

        try:
            ffplayer = MediaPlayer(
                fname, callback=self.player_callback, ff_opts=ff_opts,
                lib_opts=lib_opts)
        except Exception as e:
            self.exception(e)
            Clock.schedule_once(self._complete_stop)
            return

        # wait for media to init pixel fmt
        src_fmt = ''
        s = clock()
        while self.play_state == 'starting' and clock() - s < 5.:
            src_fmt = ffplayer.get_metadata().get('src_pix_fmt')
            if src_fmt:
                break
            time.sleep(0.01)

        if not src_fmt:
            try:
                raise ValueError("Player failed, couldn't get pixel type")
            except Exception as e:
                self.exception(e)
                Clock.schedule_once(self._complete_stop)
                return

        # if ipix_fmt:
        #     src_fmt = ipix_fmt
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
                try:
                    raise ValueError("Player failed, reached eof")
                except Exception as e:
                    self.exception(e)
                    Clock.schedule_once(self._complete_stop)
                    return

            if img:
                ivl_start = clock()
                break
            time.sleep(0.01)

        rate = ffplayer.get_metadata().get('frame_rate')
        if rate == (0, 0) or not rate or not rate[1]:
            try:
                raise ValueError("Player failed, couldn't read frame rate")
            except Exception as e:
                self.exception(e)
                Clock.schedule_once(self._complete_stop)
                return

        if not img:
            try:
                raise ValueError("Player failed, couldn't read frame")
            except Exception as e:
                self.exception(e)
                Clock.schedule_once(self._complete_stop)
                return

        # ready to start
        rate = rate[0] / float(rate[1])
        w, h = img[0].get_size()
        fmt = img[0].get_pixel_format()
        use_rt = self.use_real_time

        Clock.schedule_once(
            partial(eat_first, self.update_metadata, rate=rate, w=w, h=h,
                    fmt=fmt), 0)
        Clock.schedule_once(self._complete_start)

        # started
        process_frame(img[0], ivl_start if use_rt else img[1])

        min_sleep = 1 / (rate * 8.)
        self.setattr_in_kivy_thread('ts_play', ivl_start)
        self.setattr_in_kivy_thread('frames_played', 1)
        count = 1

        try:
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
                    time.sleep(min(val, min_sleep))

                count += 1
                self.increment_in_kivy_thread('frames_played')
                process_frame(img[0], ivl_end if use_rt else img[1])

        except Exception as e:
            self.exception(e)

        Clock.schedule_once(self._complete_stop)


class FFmpegSettingsWidget(BoxLayout):

    player: FFmpegPlayer = None

    def __init__(self, player=None, **kwargs):
        if player is None:
            player = FFmpegPlayer()
        self.player = player
        super(FFmpegSettingsWidget, self).__init__(**kwargs)

    def set_filename(self, text_wid, path, selection, filename, is_dir=True):
        '''Called by the GUI to set the filename.
        '''
        if not selection:
            if exists(join(path, filename)):
                selection = [filename]
            else:
                text_wid.text = ''
                return

        f = abspath(join(path, selection[0]))
        if is_dir and not isdir(f):
            f = dirname(f)
        self.player.play_filename = f
        text_wid.text = f


Builder.load_file(join(dirname(__file__), 'ffmpeg_player.kv'))
