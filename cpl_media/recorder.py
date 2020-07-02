"""Recorder
===========

Provides the base class for video recorders.
"""
from threading import Thread
from fractions import Fraction
from time import perf_counter as clock
from queue import Queue
from os.path import expanduser, join, exists, isdir, abspath, dirname

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

__all__ = ('BaseRecorder', 'ImageFileRecorder', 'VideoRecorder',
           'ImageFileRecordSettingsWidget', 'VideoRecordSettingsWidget')


class BaseRecorder(EventDispatcher, KivyMediaBase):
    """Records images from :class:cpl_media.player.BasePlayer` to a recorder.
    """

    _config_props_ = ('metadata_record', )

    player: BasePlayer = None
    """The :class:cpl_media.player.BasePlayer` this is being recorded from.
    """

    record_thread = None
    """The internal thread that records the frames from the player.
    """

    record_state = StringProperty('none')
    '''The current state of the state machine of the recorder.

    Can be one of none, starting, recording, stopping.

    State management:

    All state changes happen in the kivy main thread. It starts in none state.
    Requesting to record takes us out of none - we cannot get back to none
    until the internal thread requests it and we then get back to none.

    I.e. once out of none, the only way we could be back to none state is if
    all the internal thread requests has been processed. And only the internal
    thread is allowed to request we be back to none.
    '''

    image_queue = None
    """The queue used to communicate with the internal recording thread.
    """

    can_record = BooleanProperty(True)
    """Whether the recorder source can record now.
    """

    metadata_player = ObjectProperty(None)
    '''Describes the video metadata of the video player when we started
    recording.
    '''

    metadata_record = ObjectProperty(None)
    '''(internal) Describes the video metadata of the recorder. This is
    the requested format, or best guess of the metadata.

    Read only.
    '''

    metadata_record_used = ObjectProperty(None)
    '''(internal) Describes the video metadata of the recorder that is
    actually used by the recorder.

    Read only.
    '''

    frames_recorded = NumericProperty(0)
    """The number of frames recorded so far since :meth:`record`.
    """

    frames_skipped = NumericProperty(0)
    """The number of frames skipped and not recorded so far since
    :meth:`record`.
    """

    size_recorded = NumericProperty(0)
    """The estimated size of the data recorded so far since :meth:`record`.
    """

    ts_record = NumericProperty(0)
    """The time when the camera started recording.
    """

    data_rate = NumericProperty(0)
    """The estimated rate in B/s at which we're recording.
    """

    recorder_summery = StringProperty('')
    """Textual summary of the recorder type and config options.
    """

    elapsed_record_time = NumericProperty(0)
    """Number of seconds we have been recording since :meth:`record`.

    Automatically computed and updated a few times a second.
    """

    _elapsed_record_trigger = None

    def __init__(self, **kwargs):
        self.metadata_record_used = VideoMetadata('', 0, 0, 0)
        self.metadata_player = VideoMetadata(
            *kwargs.pop('metadata_player', ('', 0, 0, 0)))
        self.metadata_record = VideoMetadata(
            *kwargs.pop('metadata_record', ('', 0, 0, 0)))
        super(BaseRecorder, self).__init__(**kwargs)

        self._elapsed_record_trigger = Clock.create_trigger(
            self._update_elapsed_record, .2, True)

        self.fbind('metadata_player', self._update_data_rate)
        self.fbind('metadata_record', self._update_data_rate)
        self.fbind('metadata_record_used', self._update_data_rate)
        self._update_data_rate()

    def _update_elapsed_record(self, *largs):
        if self.ts_record:
            self.elapsed_record_time = clock() - self.ts_record

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

    def get_config_property(self, name):
        """(internal) used by the config system to get the special config data
        of the recorder.
        """
        if name == 'metadata_record':
            return tuple(self.metadata_record)
        return getattr(self, name)

    def apply_config_property(self, name, value):
        """(internal) used by the config system to set the special config data
        of the recorder.
        """
        if name == 'metadata_record':
            self.metadata_record = VideoMetadata(*value)
        else:
            setattr(self, name, value)

    @staticmethod
    def save_image(fname, img, codec='bmp', pix_fmt='', lib_opts={}):
        """Saves the given image to disk in the format requested.

        :param fname: The filename where to save the image.
        :param img: The :class:`ffpyplayer.pic.Image` to save.
        :param codec: The codec to pass to
            :class:`ffpyplayer.writer.MediaWriter` that determines the image
            type. Defaults to 'bmp'.
        :param pix_fmt: The pixel format into which to convert the image before
            saving. If empty, the original pixel format is used. If the
            codec doesn't support the image format, we first convert it to the
            closest supported format.
        :param lib_opts: Any additional `lib_opts` options to pass to
            :class:`ffpyplayer.writer.MediaWriter`.
        :return: The estimated size of the image on disk.
        """
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
        size = writer.write_frame(img=img, pts=0, stream=0)
        writer.close()
        return size

    @error_guard
    def record(self, player: BasePlayer):
        """Starts recording from the provided player and sets the
        :attr:`record_state` to `starting`.

        May be called from main kivy thread only.

        May only be called when :attr:`record_state` is `none`, otherwise an
        exception is raised. Similarly, only when the player's play state is
        `playing`. The players `metadata_play_used`, must also have been set to
        the value it's using.

        Recorders need to eventually call :meth:`complete_start` to finish
        starting recording.
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
        """Stops recording from the player, if it is recording and sets the
        :attr:`record_state` to `stopping`.

        Recorders need to eventually call :meth:`complete_stop` to finish
        stopping recording.

        :param join: whether to block the thread until the internal record
            thread has exited.
        :return: Whether we stopped recording (True) or were already
            stop(ping/ed) recording.
        """
        if self.record_state == 'none':
            assert self.record_thread is None
            return False

        assert self.image_queue is not None
        assert self.record_thread is not None

        if self.record_state == 'stopping':
            if join:
                self.record_thread.join()
            return False

        self.image_queue.put('eof')
        self.record_state = 'stopping'
        self._elapsed_record_trigger.cancel()
        if join:
            self.record_thread.join()
        return True

    def complete_start(self, *largs):
        """After :meth:`record`, this is called to set the recorder into
        `recording` :attr:`record_state`.
        """
        # when this is called, there may be a subsequent call scheduled
        # to stop, from the internal thread, but the internal thread never
        # requests first to stop and then to be in recording state
        assert self.record_state != 'none'
        # only internal thread sets to recording, and only once
        assert self.record_state != 'recording'
        if self.record_state == 'starting':  # not stopping
            self.record_state = 'recording'
        self._elapsed_record_trigger()

    def complete_stop(self, *largs):
        """After :meth:`stop`, this is called to set the recorder into `none`
        :attr:`record_state`.
        """
        assert self.record_state != 'none'

        self.record_thread = None
        self.image_queue = None
        self.record_state = 'none'

    def record_thread_run(self, *largs):
        """The method that runs in the internal record thread.
        """
        raise NotImplementedError

    def stop_all(self, join=False):
        super(BaseRecorder, self).stop_all(join=join)
        self.stop(join=join)


class ImageFileRecorder(BaseRecorder):
    """Records images as files to disk.
    """

    _config_props_ = (
        'record_directory', 'record_prefix', 'compression', 'extension')

    record_directory = StringProperty(expanduser('~'))
    '''The directory into which videos should be saved.
    '''

    record_prefix = StringProperty('image_')
    '''The prefix to the filename of the images being saved.
    '''

    extension = StringProperty('tiff')
    """The extension of the images being saved.
    """

    compression = StringProperty('raw')
    """Whether to compress when :attr:`extension` is `tiff`. Can be one of
    ``'raw', 'lzw', 'zip'``.
    """

    def __init__(self, **kwargs):
        super(ImageFileRecorder, self).__init__(**kwargs)

        self.fbind('record_directory', self._update_summary)
        self.fbind('record_prefix', self._update_summary)
        self.fbind('extension', self._update_summary)
        self._update_summary()

    def _update_summary(self, *largs):
        self.recorder_summery = 'FFmpeg "{}*.{}"'.format(
            join(self.record_directory, self.record_prefix), self.extension)

    def send_image_to_recorder(self, image):
        """Sends the image to the recorder queue to save the image.

        :param image: A tuple of the image and metadata as provided to
            :attr:`cpl_media.player.BasePlayer.frame_callbacks`.
        """
        if self.image_queue is None:
            return

        self.image_queue.put(image)

    @error_guard
    def record(self, player: BasePlayer):
        if not player.metadata_play_used or not player.metadata_play_used.rate:
            raise TypeError(
                'Can only record from player once the fps is known')

        super(ImageFileRecorder, self).record(player=player)
        self.player.frame_callbacks.append(self.send_image_to_recorder)

    @error_guard
    def stop(self, *largs, join=False):
        if super(ImageFileRecorder, self).stop(join=join):
            self.player.frame_callbacks.remove(self.send_image_to_recorder)

    def _start_recording(self):
        self.record_directory = expanduser(self.record_directory)
        thread = self.record_thread = Thread(
            target=self.record_thread_run, name='Record image thread',
            args=(self.record_directory, self.record_prefix, self.compression,
                  self.extension))
        thread.start()

    def complete_stop(self, *largs):
        super(ImageFileRecorder, self).complete_stop()
        if self.send_image_to_recorder in self.player.frame_callbacks:
            self.player.frame_callbacks.remove(self.send_image_to_recorder)

    def record_thread_run(
            self, record_directory, record_prefix, compression, extension):
        queue = self.image_queue
        last_img = None

        while self.record_state != 'stopping':
            item = queue.get()
            if item == 'eof':
                break
            image, metadata = item

            try:
                if last_img is None:
                    self.setattr_in_kivy_thread('ts_record', clock())
                    self.setattr_in_kivy_thread(
                        'metadata_record_used', self.player.metadata_play_used)
                    Clock.schedule_once(self.complete_start)
                    last_img = image

                suffix = 't={}'.format(metadata['t'])
                if 'count' in metadata:
                    suffix += '_count={}'.format(metadata['count'])
                ext = '.' + extension
                filename = join(record_directory, record_prefix + suffix + ext)
                counter = 0
                while exists(filename):
                    counter += 1
                    filename = join(
                        record_directory,
                        record_prefix + suffix + '-{}'.format(counter) + ext)

                if extension == 'tiff':
                    lib_opts = {
                        'compression_algo': 'deflate' if compression == 'zip'
                        else compression}
                else:
                    lib_opts = {}

                size = self.save_image(
                    filename, image, codec=extension,
                    pix_fmt=image.get_pixel_format(), lib_opts=lib_opts)
                self.increment_in_kivy_thread('size_recorded', size)
                self.increment_in_kivy_thread('frames_recorded')
            except Exception as e:
                self.exception(e)
                self.increment_in_kivy_thread('frames_skipped')

        Clock.schedule_once(self.complete_stop)


class VideoRecorder(BaseRecorder):
    """Records images to a video file on disk.

    Cannot start recording until the player fps is known. Otherwise, an error
    is raised.
    """

    _config_props_ = (
        'record_directory', 'record_fname', 'record_fname_count',
        'estimate_record_rate')

    record_directory = StringProperty(expanduser('~'))
    '''The directory into which videos should be saved.
    '''

    record_fname = StringProperty('video{}.mkv')
    '''The filename to be used to record the next video.

    If ``{}`` is present in the filename, it'll be replaced with the value of
    :attr:`record_fname_count` which auto increments after every video, when
    used. We use Python's ``format`` machinery for this.
    '''

    record_fname_count = NumericProperty(0)
    '''A counter that auto increments by one after every recorded video.

    Used to give unique filenames for each video file. See
    :attr:`record_fname`.
    '''

    estimate_record_rate = BooleanProperty(False)
    """Whether to use :attr:`cpl_media.player.BasePlayer.real_rate` for the
    recorder frame rate, as opposed to the one initially provided by the
    player.
    """

    record_filename = StringProperty('')
    """The full filename of the video that will be saved as computed by the
    configuration options of the instance.
    """

    def __init__(self, **kwargs):
        super(VideoRecorder, self).__init__(**kwargs)

        self.fbind('record_directory', self._update_record_fname)
        self.fbind('record_fname', self._update_record_fname)
        self.fbind('record_fname_count', self._update_record_fname)
        self._update_record_fname()

        self.fbind('record_filename', self._update_summary)
        self._update_summary()

    def _update_summary(self, *largs):
        self.recorder_summery = 'FFmpeg "{}"'.format(self.record_filename)

    def compute_recording_opts(self, ifmt=None, iw=None, ih=None):
        """Computes the recording metadata to use, from the provided options
        and from the player's metadata.
        """
        play_used = self.metadata_player
        ifmt = ifmt or play_used.fmt
        iw = iw or play_used.w
        ih = ih or play_used.h
        irate = play_used.rate

        ifmt = ifmt or 'yuv420p'
        iw = iw or 640
        ih = ih or 480
        assert irate

        if self.record_fname.endswith('mkv') and (
                ifmt.startswith('rgb') or ifmt.startswith('bgr')):
            ifmt = get_supported_pixfmts('libx264', ifmt)

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
            self.record_fname.format(self.record_fname_count))

    def send_image_to_recorder(self, image):
        """Sends the image to the recorder queue to save the image in the
        video.

        :param image: A tuple of the image and metadata as provided to
            :attr:`cpl_media.player.BasePlayer.frame_callbacks`.
        """
        if self.image_queue is None:
            return

        self.image_queue.put(image)

    @error_guard
    def record(self, player: BasePlayer):
        if not player.metadata_play_used or not player.metadata_play_used.rate:
            raise TypeError(
                'Can only record from player once the fps is known')

        super(VideoRecorder, self).record(player=player)
        self.player.frame_callbacks.append(self.send_image_to_recorder)

    @error_guard
    def stop(self, *largs, join=False):
        if super(VideoRecorder, self).stop(join=join):
            self.player.frame_callbacks.remove(self.send_image_to_recorder)

    def _start_recording(self):
        self.record_directory = expanduser(self.record_directory)
        thread = self.record_thread = Thread(
            target=self.record_thread_run, name='Record thread',
            args=(self.record_filename, ))
        thread.start()

    def complete_stop(self, *largs):
        super(VideoRecorder, self).complete_stop()
        self.record_fname_count += 1
        if self.send_image_to_recorder in self.player.frame_callbacks:
            self.player.frame_callbacks.remove(self.send_image_to_recorder)

    def record_thread_run(self, filename):
        queue = self.image_queue
        recorder = None
        t0 = None
        last_t = None

        while self.record_state != 'stopping':
            item = queue.get()
            if item == 'eof':
                break
            img, metadata = item

            if recorder is None:
                try:
                    self.setattr_in_kivy_thread('ts_record', clock())
                    t0 = metadata['t']
                    iw, ih = img.get_size()
                    ipix_fmt = img.get_pixel_format()

                    (ifmt, iw, ih, irate), (opix_fmt, ow, oh, orate) = \
                        self.compute_recording_opts(ipix_fmt, iw, ih)

                    self.setattr_in_kivy_thread(
                        'metadata_record_used',
                        VideoMetadata(opix_fmt, ow, oh, orate))

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
                except Exception as e:
                    self.exception(e)
                    Clock.schedule_once(self.complete_stop)
                    return

                Clock.schedule_once(self.complete_start)

            try:
                self.setattr_in_kivy_thread(
                    'size_recorded',
                    recorder.write_frame(img, metadata['t'] - t0))
                self.increment_in_kivy_thread('frames_recorded')
            except Exception as e:
                self.exception(e)
                self.increment_in_kivy_thread('frames_skipped')

        if recorder is not None:
            try:
                recorder.close()
            except Exception as e:
                self.exception(e)

        Clock.schedule_once(self.complete_stop)


class ImageFileRecordSettingsWidget(BoxLayout):
    """Settings widget for :class:`ImageFileRecorder`.
    """

    recorder: ImageFileRecorder = None
    """The recorder.
    """

    def __init__(self, recorder=None, **kwargs):
        if recorder is None:
            recorder = ImageFileRecorder()
        self.recorder = recorder
        super(ImageFileRecordSettingsWidget, self).__init__(**kwargs)

    def set_filename(self, text_wid, paths):
        """Called by the GUI to set the directory.
        """
        if not paths:
            return

        self.recorder.record_directory = paths[0]
        text_wid.text = paths[0]


class VideoRecordSettingsWidget(BoxLayout):
    """Settings widget for :class:`VideoRecorder`.
    """

    recorder: VideoRecorder = None
    """The recorder.
    """

    def __init__(self, recorder=None, **kwargs):
        if recorder is None:
            recorder = VideoRecorder()
        self.recorder = recorder
        super(VideoRecordSettingsWidget, self).__init__(**kwargs)

    def set_filename(self, text_wid, paths):
        """Called by the GUI to set the directory.
        """
        if not paths:
            return

        self.recorder.record_directory = paths[0]
        text_wid.text = paths[0]


Builder.load_file(join(dirname(__file__), 'recorder.kv'))
