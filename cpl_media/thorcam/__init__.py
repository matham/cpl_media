"""Thor based player
======================

This player can play Thor cameras using ``thorcam``.
"""

from time import perf_counter as clock
from queue import Queue, Empty
from os.path import splitext, join, exists, isdir, abspath, dirname

from kivy.clock import Clock
from kivy.properties import (
    NumericProperty, ReferenceListProperty,
    ObjectProperty, ListProperty, StringProperty, BooleanProperty,
    DictProperty, AliasProperty, OptionProperty, ConfigParserProperty)
from kivy.uix.boxlayout import BoxLayout
from kivy.lang import Builder

from cpl_media.player import BasePlayer, VideoMetadata
from cpl_media import error_guard
import cpl_media

from thorcam.camera import ThorCamClient


__all__ = ('ThorCamPlayer', 'ThorCamSettingsWidget')


class ThorCamPlayer(BasePlayer, ThorCamClient):
    """Wrapper for Thor .Net camera based player.
    """

    __settings_attrs__ = (
        'supported_freqs', 'freq', 'supported_taps', 'taps', 'supports_color',
        'exposure_range', 'exposure_ms', 'binning_x_range', 'binning_x',
        'binning_y_range', 'binning_y', 'sensor_size', 'roi_x', 'roi_y',
        'roi_width', 'roi_height', 'gain_range', 'gain', 'black_level_range',
        'black_level', 'frame_queue_size', 'supported_triggers',
        'trigger_type', 'trigger_count', 'num_queued_frames', 'color_gain')

    supported_freqs = ListProperty(['20 MHz', ])

    freq = StringProperty('20 MHz')

    supported_taps = ListProperty(['1', ])

    taps = StringProperty('1')

    supports_color = BooleanProperty(False)

    exposure_range = ListProperty([0, 100])

    exposure_ms = NumericProperty(5)

    binning_x_range = ListProperty([0, 0])

    binning_x = NumericProperty(0)

    binning_y_range = ListProperty([0, 0])

    binning_y = NumericProperty(0)

    sensor_size = ListProperty([0, 0])

    roi_x = NumericProperty(0)

    roi_y = NumericProperty(0)

    roi_width = NumericProperty(0)

    roi_height = NumericProperty(0)

    gain_range = ListProperty([0, 100])

    gain = NumericProperty(0)

    black_level_range = ListProperty([0, 100])

    black_level = NumericProperty(0)

    frame_queue_size = NumericProperty(1)

    supported_triggers = ListProperty(['SW Trigger', 'HW Trigger'])

    trigger_type = StringProperty('SW Trigger')

    trigger_count = NumericProperty(1)

    num_queued_frames = NumericProperty(0)

    color_gain = ListProperty([1, 1, 1])

    serials = ListProperty([])
    """The list of cameras serial number available.
    """

    to_kivy_queue = None

    cam_state = StringProperty('none')
    """Can be one of none, opening, open, closing.
    """

    _frame_count = 0

    _ivl_start = None

    def __init__(self, **kwargs):
        super(ThorCamPlayer, self).__init__(**kwargs)
        self._kivy_trigger = Clock.create_trigger(self.process_in_kivy_thread)
        self.to_kivy_queue = Queue()
        self.start_cam_process()

    @error_guard
    def received_camera_response(self, msg, value):
        if msg == 'image':
            value = self.create_image_from_msg(*value)
        self.to_kivy_queue.put((msg, value))
        self._kivy_trigger()

    def handle_exception(self, e, exc_info):
        self.to_kivy_queue.put(('exception', (e, exc_info)))
        self._kivy_trigger()

    @error_guard
    def process_in_kivy_thread(self, *largs):
        while self.to_kivy_queue is not None:
            try:
                msg, value = self.to_kivy_queue.get(block=False)

                if msg == 'exception':
                    e, exec_info = value
                    cpl_media.error_callback(e, exc_info=exec_info)
                elif msg == 'cam_open':
                    assert self.cam_state == 'opening'
                    self.cam_state = 'open'
                elif msg == 'cam_closed':
                    assert self.cam_state == 'closing'
                    self.cam_state = 'none'
                elif msg == 'image':
                    self._handle_image_received(value)
                elif msg == 'playing':
                    if value:
                        assert self.play_state == 'starting'

                        self._ivl_start = None
                    else:
                        assert self.play_state == 'stopping'
                        self._complete_stop()
                elif msg == 'settings':
                    for key, val in value.items():
                        setattr(self, key, val)
                elif msg == 'serials':
                    self.serials = value
                else:
                    print('Got unknown ThorCamPlayer message', msg, value)
            except Empty:
                break

    def _handle_image_received(self, value):
        t = clock()
        if self.play_state == 'starting':
            # first time getting image
            if self._ivl_start is None:
                self._ivl_start = clock()

            # wait for 2 secs to estimate fps of cam
            if t - self._ivl_start >= 2.:
                self.real_rate = self._frame_count / (t - self._ivl_start)
                self._frame_count = 0

                self.ts_play = self._ivl_start = t
                img = value[0]
                self.metadata_play_used = VideoMetadata(
                    img.get_pixel_format(), *img.get_size(),
                    2 * int(self.real_rate))
                self._complete_start()

            self._frame_count += 1

        if self.play_state != 'playing':
            return

        if t - self._ivl_start >= 1.:
            self.real_rate = self._frame_count / (t - self._ivl_start)
            self._frame_count = 0
            self._ivl_start = t

        self._frame_count += 1
        self.frames_played += 1

        img, count, queued_count, t_img = value
        self.num_queued_frames = queued_count
        self.process_frame(img, t_img)

    @error_guard
    def open_camera(self, serial):
        if self.cam_state != 'none':
            raise TypeError('Can only open camera if it has not been opened')
        self.cam_state = 'opening'
        self.send_camera_request('open_cam', serial)

    @error_guard
    def close_camera(self):
        if self.cam_state != 'open':
            raise TypeError('Can only close camera if it has been opened')
        self.stop()
        self.cam_state = 'closing'
        self.send_camera_request('close_cam', None)

    def refresh_cameras(self):
        self.send_camera_request('serials', None)

    @error_guard
    def play(self):
        if self.cam_state != 'open':
            raise TypeError("Cannot play camera that isn't open")

        self.start_cam_process()
        super(ThorCamPlayer, self).play()
        self.send_camera_request('play')

    def stop(self, *largs, join=False):
        if self.cam_state != 'open':
            return

        if super(ThorCamPlayer, self).stop(join=join):
            self.send_camera_request('stop')

    def set_setting(self, name, value):
        self.send_camera_request('setting', (name, value))

    def play_thread_run(self):
        pass

    def stop_all(self, join=False):
        super(ThorCamPlayer, self).stop_all(join=join)
        self.stop_cam_process(join=join)


class ThorCamSettingsWidget(BoxLayout):

    player: ThorCamPlayer = None

    def __init__(self, player=None, **kwargs):
        if player is None:
            player = ThorCamPlayer()
        self.player = player
        super(ThorCamSettingsWidget, self).__init__(**kwargs)

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


Builder.load_file(join(dirname(__file__), 'thormcam_player.kv'))
