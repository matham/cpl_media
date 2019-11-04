"""Recorder
===========

Provides the base class for video recorders.
"""
from os.path import isfile, join, abspath, expanduser
import traceback
import sys
from threading import Thread
from fractions import Fraction
from time import perf_counter as clock
from queue import Queue
from os.path import splitext, join, exists, isdir, abspath, dirname

from ffpyplayer.pic import get_image_size, Image, SWScale
from ffpyplayer.tools import get_supported_pixfmts, get_format_codec
from ffpyplayer.writer import MediaWriter

from kivy.clock import Clock
from kivy.properties import (
    NumericProperty, ObjectProperty, StringProperty, BooleanProperty)
from kivy.event import EventDispatcher
from kivy.uix.boxlayout import BoxLayout
from kivy.lang import Builder

from .player import VideoMetadata, BasePlayer
from cpl_media import error_guard
from .common import KivyMediaBase
import cpl_media

__all__ = ('BaseRecorder', 'VideoRecorder', 'VideoRecordSettingsWidget')


class BaseRecorder(EventDispatcher, KivyMediaBase):
    """Records images to media.
    """

    __settings_attrs__ = ('metadata_record', 'estimate_record_rate')

    player: BasePlayer = None

    record_thread = None

    record_state = StringProperty('none')
    '''Can be one of none, starting, recording, stopping.
    
    State management:
    
    All state changes happen in the kivy main thread. It starts in none state.
    Requesting to record takes us out of none - we cannot get back to none
    until the internal thread requests it and we then get back to none.

    I.e. once out of none, the only way we could be back to none state is if
    all the internal thread requests has been processed. And only the internal
    thread is allowed to request we be back to none.
    '''

    image_queue = None

    metadata_player = ObjectProperty(None)
    '''Describes the video metadata of the video recorder.
    '''

    metadata_record = ObjectProperty(None)
    '''Describes the video metadata of the video recorder.
    '''

    metadata_record_used = ObjectProperty(None)
    '''Describes the video metadata of the video recorder.
    '''

    estimate_record_rate = BooleanProperty(False)

    frames_recorded = NumericProperty(0)
    """Only set from second thread.
    """

    frames_skipped = NumericProperty(0)
    """Only set from second thread.
    """

    size_recorded = NumericProperty(0)
    """Only set from second thread.
    """

    ts_record = NumericProperty(0)

    data_rate = NumericProperty(0)
    """MB/s.
    """

    def __init__(self, **kwargs):
        self.metadata_record_used = VideoMetadata('', 0, 0, 0)
        self.metadata_player = VideoMetadata(
            *kwargs.pop('metadata_player', ('', 0, 0, 0)))
        self.metadata_record = VideoMetadata(
            *kwargs.pop('metadata_record', ('', 0, 0, 0)))
        super(BaseRecorder, self).__init__(**kwargs)

        self.fbind('metadata_player', self._update_data_rate)
        self.fbind('metadata_record', self._update_data_rate)
        self.fbind('metadata_record_used', self._update_data_rate)
        self._update_data_rate()

    def _update_data_rate(self, *largs):
        fmt = self.metadata_record_used.fmt
        w = self.metadata_record_used.w
        h = self.metadata_record_used.h
        rate = self.metadata_record_used.rate

        fmt = fmt or self.metadata_record.fmt or self.metadata_player.fmt
        w = w or self.metadata_record.w or self.metadata_player.w
        h = h or self.metadata_record.h or self.metadata_player.h
        rate = rate or self.metadata_record.rate or self.metadata_player.rate

        if not fmt or not w or not h:
            self.data_rate = 0
        else:
            rate = rate or 30
            self.data_rate = sum(get_image_size(fmt, w, h)) * rate

    def get_settings_attrs(self, attrs):
        d = {}
        for key in attrs:
            if key == 'metadata_record':
                d[key] = tuple(getattr(self, key))
            else:
                d[key] = getattr(self, key)
        return d

    def apply_settings(self, settings):
        for k, v in settings.items():
            if k == 'metadata_record':
                v = VideoMetadata(*v)
            setattr(self, k, v)

    @staticmethod
    def save_image(fname, img, codec='bmp', pix_fmt='bgr24', lib_opts={}):
        fmt = img.get_pixel_format()
        w, h = img.get_size()

        if not codec:
            codec = get_format_codec(fname)
            ofmt = get_supported_pixfmts(codec, fmt)[0]
        else:
            ofmt = get_supported_pixfmts(codec, pix_fmt or fmt)[0]
        if ofmt != fmt:
            sws = SWScale(w, h, fmt, ofmt=ofmt)
            img = sws.scale(img)
            fmt = ofmt

        out_opts = {'pix_fmt_in': fmt, 'width_in': w, 'height_in': h,
                    'frame_rate': (30, 1), 'codec': codec}
        writer = MediaWriter(fname, [out_opts], lib_opts=lib_opts)
        writer.write_frame(img=img, pts=0, stream=0)
        writer.close()

    @error_guard
    def record(self, player: BasePlayer):
        """Called from main thread only, starts recording and sets record state
        to `starting`. Only called when :attr:`record_state` is `none`.
        """
        if self.record_state != 'none':
            raise TypeError(
                'Asked to record while {}'.format(self.record_state))

        if player.play_state != 'playing':
            raise TypeError(
                'Can only record from player once the player is playing')

        self.record_state = 'starting'
        self.player = player
        self.size_recorded = self.ts_record = 0
        self.frames_recorded = self.frames_skipped = 0
        self.metadata_player = player.metadata_play_used
        self.image_queue = Queue()
        self._start_recording()

    def _start_recording(self):
        thread = self.record_thread = Thread(
            target=self.record_thread_run, name='Record thread')
        thread.start()

    @error_guard
    def stop(self, *largs, join=False):
        if self.record_state == 'none':
            assert self.record_thread is None
            return False

        assert self.image_queue is not None
        assert self.record_thread is not None

        if self.record_state == 'stopping':
            if join:
                self.record_thread.join()
            return False

        self.image_queue.put_nowait('eof')
        self.record_state = 'stopping'
        if join:
            self.record_thread.join()
        return True

    def _complete_start(self, *largs):
        # when this is called, there may be a subsequent call scheduled
        # to stop, from the internal thread, but the internal thread never
        # requests first to stop and then to be in recording state
        assert self.record_state != 'none'
        # only internal thread sets to recording, and only once
        assert self.record_state != 'recording'
        if self.record_state == 'starting':  # not stopping
            self.record_state = 'recording'

    def _complete_stop(self, *largs):
        assert self.record_state != 'none'

        self.record_thread = None
        self.image_queue = None
        self.record_state = 'none'

    def record_thread_run(self, *largs):
        raise NotImplementedError

    def stop_all(self, join=False):
        super(BaseRecorder, self).stop_all(join=join)
        self.stop(join=join)


class VideoRecorder(BaseRecorder):
    """Records videos to disk.

    Cannot start recording until the player fps is known. Otherwise, an error
    is raised.
    """

    __settings_attrs__ = (
        'record_directory', 'record_fname', 'record_fname_count')

    record_directory = StringProperty(expanduser('~'))
    '''The directory into which videos should be saved.
    '''

    record_fname = StringProperty('video{}.avi')
    '''The filename to be used to record the next video.

    If ``{}`` is present in the filename, it'll be replaced with the value of
    :attr:`record_fname_count` which auto increments after every video, when
    used.
    '''

    record_fname_count = NumericProperty(0)
    '''A counter that auto increments by one after every recorded video.

    Used to give unique filenames for each video file.
    '''

    record_filename = StringProperty('')

    def __init__(self, **kwargs):
        super(VideoRecorder, self).__init__(**kwargs)

        self.fbind('record_directory', self._update_record_fname)
        self.fbind('record_fname', self._update_record_fname)
        self.fbind('record_fname_count', self._update_record_fname)
        self._update_record_fname()

    def compute_recording_opts(self, ifmt=None, iw=None, ih=None):
        play_used = self.metadata_player
        ifmt = ifmt or play_used.fmt
        iw = iw or play_used.w
        ih = ih or play_used.h
        irate = play_used.rate

        ifmt = ifmt or 'yuv420p'
        iw = iw or 640
        ih = ih or 480
        assert irate

        ofmt, ow, oh, orate = self.metadata_record
        ofmt = ofmt or ifmt
        ow = ow or iw
        oh = oh or ih
        if self.estimate_record_rate:
            orate = orate or self.player.real_rate
        orate = orate or irate

        return (ifmt, iw, ih, irate), (ofmt, ow, oh, orate)

    def _set_metadata_record(self, fmt, w, h, rate):
        self.metadata_record = VideoMetadata(fmt, w, h, rate)

    def _update_record_fname(self, *largs):
        self.record_filename = join(
            self.record_directory,
            self.record_fname.replace('{}', str(self.record_fname_count)))

    @error_guard
    def record(self, player: BasePlayer):
        if not player.metadata_play_used or not player.metadata_play_used.rate:
            raise TypeError(
                'Can only record from player once the fps is known')

        super(VideoRecorder, self).record(player=player)

    def _start_recording(self):
        thread = self.record_thread = Thread(
            target=self.record_thread_run, name='Record thread',
            args=(self.record_filename, ))
        thread.start()

    def _complete_stop(self, *largs):
        super(VideoRecorder, self)._complete_stop()
        self.record_fname_count += 1

    def record_thread_run(self, filename):
        queue = self.image_queue
        recorder = None
        t0 = None
        put = queue.put_nowait

        while self.record_state != 'stopping':
            item = queue.get()
            if item == 'eof':
                break
            img, t = item

            if recorder is None:
                try:
                    self.setattr_in_kivy_thread('ts_record', clock())
                    t0 = t
                    iw, ih = img.get_size()
                    ipix_fmt = img.get_pixel_format()

                    (ifmt, iw, ih, irate), (opix_fmt, ow, oh, orate) = \
                        self.compute_recording_opts(ipix_fmt, iw, ih)

                    Clock.schedule_once(
                        lambda *largs: setattr(
                            self, 'metadata_record_used',
                            VideoMetadata(opix_fmt, ow, oh, orate)), 0)

                    orate = Fraction(orate)
                    if orate >= 1.:
                        orate = Fraction(orate.denominator, orate.numerator)
                        orate = orate.limit_denominator(2 ** 30 - 1)
                        orate = (orate.denominator, orate.numerator)
                    else:
                        orate = orate.limit_denominator(2 ** 30 - 1)
                        orate = (orate.numerator, orate.denominator)

                    stream = {
                        'pix_fmt_in': ipix_fmt, 'pix_fmt_out': opix_fmt,
                        'width_in': iw, 'height_in': ih, 'width_out': ow,
                        'height_out': oh, 'codec': 'rawvideo',
                        'frame_rate': orate}

                    recorder = MediaWriter(filename, [stream])
                    self.player.frame_callbacks.append(put)
                except Exception as e:
                    self.exception(e)
                    if put in self.player.frame_callbacks:
                        self.player.frame_callbacks.remove(put)
                    Clock.schedule_once(self._complete_stop)
                    return

                Clock.schedule_once(self._complete_start)

            try:
                self.setattr_in_kivy_thread(
                    'size_recorded', recorder.write_frame(img, t - t0))
                self.increment_in_kivy_thread('frames_recorded')
            except Exception as e:
                self.exception(e)
                self.increment_in_kivy_thread('frames_skipped')

        if recorder is not None:
            try:
                recorder.close()
            except Exception as e:
                self.exception(e)

        if put in self.player.frame_callbacks:
            self.player.frame_callbacks.remove(put)
        Clock.schedule_once(self._complete_stop)


class VideoRecordSettingsWidget(BoxLayout):

    recorder: VideoRecorder = None

    def __init__(self, recorder=None, **kwargs):
        if recorder is None:
            recorder = VideoRecorder()
        self.recorder = recorder
        super(VideoRecordSettingsWidget, self).__init__(**kwargs)

    def set_filename(self, text_wid, path, selection, filename, is_dir=True):
        '''Called by the GUI to set the filename.
        '''
        if not selection:
            if exists(join(path, filename)):
                f = abspath(join(path, filename))
                if is_dir and not isdir(f):
                    f = dirname(f)
            elif is_dir and exists(path):
                f = abspath(path)
            else:
                text_wid.text = ''
                return
        else:
            f = abspath(join(path, selection[0]))
            if is_dir and not isdir(f):
                f = dirname(f)

        self.recorder.record_directory = f
        text_wid.text = f


Builder.load_file(join(dirname(__file__), 'recorder.kv'))
