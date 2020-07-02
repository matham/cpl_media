"""Thor based player
======================

This player can play Thor cameras using :mod:`thorcam`.
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
from kivy.logger import Logger
from kivy.lang import Builder

from cpl_media.player import BasePlayer, VideoMetadata
from cpl_media import error_guard
import cpl_media

try:
    from thorcam.camera import ThorCamClient
except ImportError as err:
    ThorCamClient = object
    Logger.debug('cpl_media: Could not import thorcam: {}'.format(err))


__all__ = ('ThorCamPlayer', 'ThorCamSettingsWidget')


class ThorCamPlayer(BasePlayer, ThorCamClient):
    """Wrapper for Thor .Net camera based player.
    """

    _config_props_ = (
        'supported_freqs', 'freq', 'supported_taps', 'taps', 'supports_color',
        'exposure_range', 'exposure_ms', 'binning_x_range', 'binning_x',
        'binning_y_range', 'binning_y', 'sensor_size', 'roi_x', 'roi_y',
        'roi_width', 'roi_height', 'gain_range', 'gain', 'black_level_range',
        'black_level', 'frame_queue_size', 'supported_triggers',
        'trigger_type', 'trigger_count', 'num_queued_frames', 'color_gain',
        'serial', 'serials')

    supported_freqs = ListProperty(['20 MHz', ])
    """The supported frequencies."""

    freq = StringProperty('20 MHz')
    """The frequency to use."""

    supported_taps = ListProperty(['1', ])
    """The supported taps."""

    taps = StringProperty('1')
    """The tap to use."""

    supports_color = BooleanProperty(False)
    """Whether the camera supports color."""

    exposure_range = ListProperty([0, 100])
    """The supported exposure range in ms."""

    exposure_ms = NumericProperty(0)
    """The exposure value in ms to use."""

    binning_x_range = ListProperty([0, 0])
    """The supported exposure range."""

    binning_x = NumericProperty(0)
    """The x binning value to use."""

    binning_y_range = ListProperty([0, 0])
    """The supported exposure range."""

    binning_y = NumericProperty(0)
    """The y binning value to use."""

    sensor_size = ListProperty([0, 0])
    """The size of the sensor in pixels."""

    roi_x = NumericProperty(0)
    """The x start position of the ROI in pixels."""

    roi_y = NumericProperty(0)
    """The y start position of the ROI in pixels."""

    roi_width = NumericProperty(0)
    """The width after the x start position of the ROI in pixels, to use."""

    roi_height = NumericProperty(0)
    """The height after the y start position of the ROI in pixels, to use."""

    gain_range = ListProperty([0, 100])
    """The supported exposure range."""

    gain = NumericProperty(0)
    """The gain value to use."""

    black_level_range = ListProperty([0, 100])
    """The supported exposure range."""

    black_level = NumericProperty(0)
    """The black level value to use."""

    frame_queue_size = NumericProperty(1)
    """The max number of image frames to be allowed on the camera's hardware
    queue. Once exceeded, the frames are dropped."""

    supported_triggers = ListProperty(['SW Trigger', 'HW Trigger'])
    """The trigger types supported by the camera."""

    trigger_type = StringProperty('SW Trigger')
    """The trigger type of the camera to use."""

    trigger_count = NumericProperty(0)
    """The number of frames to capture in response to the trigger."""

    num_queued_frames = NumericProperty(0)
    """The number of image frames currently on the camera's hardware queue."""

    color_gain = ListProperty([1, 1, 1])
    """The color gain for each red, green, and blue channel."""

    serials = ListProperty([])
    """The list of serial numbers representing the cameras available.
    """

    serial = StringProperty('')
    """The serial number of the camera that will be opened.
    """

    to_kivy_queue = None
    """The queue that sends messages to be executed in the Kivy thread.
    """

    is_available = BooleanProperty(ThorCamClient is not object)
    """Whether :mod:`thorcam` is available to play."""

    cam_state = StringProperty('none')
    """The current state of the state machine of the camera.

    Can be one of none, opening, open, closing.
    """

    _frame_count = 0

    _ivl_start = 0

    def __init__(self, open_thread=True, **kwargs):
        super(ThorCamPlayer, self).__init__(**kwargs)
        self.can_play = False
        self._kivy_trigger = Clock.create_trigger(self.process_in_kivy_thread)
        self.to_kivy_queue = Queue()
        if ThorCamClient is not object and open_thread:
            self.start_cam_process()

        self.fbind('serial', self._update_summary)
        self._update_summary()

    def _update_summary(self, *largs):
        self.player_summery = 'Thor "{}"'.format(self.serial)

    @error_guard
    def received_camera_response(self, msg, value):
        if msg == 'image':
            value = self.create_image_from_msg(value)
        self.to_kivy_queue.put((msg, value))
        self._kivy_trigger()

    def handle_exception(self, e, exc_info):
        self.to_kivy_queue.put(('exception', (e, exc_info)))
        self._kivy_trigger()

    @error_guard
    def process_in_kivy_thread(self, *largs):
        """Processes messages from the camera in the kivy thread.
        """
        while self.to_kivy_queue is not None:
            try:
                msg, value = self.to_kivy_queue.get(block=False)

                if msg == 'exception':
                    e, exec_info = value
                    cpl_media.error_callback(e, exc_info=exec_info)
                    if self.cam_state == 'open':
                        self.close_camera()
                elif msg == 'cam_open':
                    assert self.cam_state == 'opening'
                    self.cam_state = 'open'
                    self.can_play = True
                elif msg == 'cam_closed':
                    # either remote sent exception, or we asked to stop
                    assert self.cam_state == 'closing'
                    self.cam_state = 'none'
                    self.can_play = False
                    if self.play_state != 'none':
                        self.complete_stop()
                elif msg == 'image':
                    self._handle_image_received(value)
                elif msg == 'playing':
                    if value:
                        assert self.play_state == 'starting'
                    else:
                        assert self.play_state == 'stopping'
                        self.complete_stop()
                elif msg == 'settings':
                    # maintain the last settings
                    old_vals = {
                        key: getattr(self, key) for key in self.settings}
                    for key, val in value.items():
                        setattr(self, key, val)
                        if key in old_vals and old_vals[key] != val and \
                                old_vals[key]:
                            self.set_setting(key, old_vals[key])
                elif msg == 'setting':
                    for key, val in value.items():
                        setattr(self, key, val)
                elif msg == 'serials':
                    self.serials = value
                else:
                    print('Got unknown ThorCamPlayer message', msg, value)
            except Empty:
                break

    def _handle_image_received(self, value):
        """Runs in the kivy thread when we get an image
        """
        t = clock()
        if self.play_state == 'starting':
            self._frame_count = 0
            self.ts_play = self._ivl_start = t

            img = value[0]
            self.metadata_play_used = VideoMetadata(
                img.get_pixel_format(), *img.get_size(), 0)
            self.complete_start()

        if self.play_state != 'playing':
            return

        if t - self._ivl_start >= 1.:
            r = self.real_rate = self._frame_count / (t - self._ivl_start)
            if not self.metadata_play_used.rate:
                self.metadata_play_used = VideoMetadata(
                    *self.metadata_play_used[:3], int(2 * r))
            self._frame_count = 0
            self._ivl_start = t

        self._frame_count += 1
        self.frames_played += 1

        img, count, queued_count, t_img = value
        self.num_queued_frames = queued_count
        self.process_frame(img, {'t': t_img, 'count': count})

    @error_guard
    def open_camera(self, serial):
        """Opens the camera so that we can :meth:`play`.
        """
        if self.cam_state != 'none':
            raise TypeError('Can only open camera if it has not been opened')
        self.cam_state = 'opening'
        self.send_camera_request('open_cam', serial)

    @error_guard
    def close_camera(self):
        """closes the camera and also stops playing if we were playing.
        """
        if self.cam_state != 'open':
            raise TypeError('Can only close camera if it has been opened')
        self.stop()
        self.cam_state = 'closing'
        self.can_play = False
        self.send_camera_request('close_cam', None)

    def refresh_cameras(self):
        """Refreshes the list of Thor cameras plugged in.

        It updates :attr:`serials` when we have the new list.
        """
        self.send_camera_request('serials', None)

    @error_guard
    def play(self):
        if self.cam_state != 'open':
            raise TypeError("Cannot play camera that isn't open")

        self.start_cam_process()
        super(ThorCamPlayer, self).play()
        self.metadata_play_used = VideoMetadata('', 0, 0, 0)
        self.send_camera_request('play')

    def stop(self, *largs, join=False):
        if self.cam_state != 'open':
            return

        if super(ThorCamPlayer, self).stop(join=join):
            self.send_camera_request('stop')

    def set_setting(self, name, value):
        """Requests that the setting should be changed for the camera.

        :param name: The setting name to be changed. E.g. ``"exposure_ms"``.
        :param value: The new setting value.
        """
        self.send_camera_request('setting', (name, value))

    def play_thread_run(self):
        pass

    def stop_all(self, join=False):
        super(ThorCamPlayer, self).stop_all(join=join)
        if ThorCamClient is not object and self._server_thread is not None:
            self.stop_cam_process(join=join)


class ThorCamSettingsWidget(BoxLayout):
    """Settings widget for :class:`ThorCamPlayer`.
    """

    player: ThorCamPlayer = None
    """The player.
    """

    def __init__(self, player=None, **kwargs):
        if player is None:
            player = ThorCamPlayer()
        self.player = player
        super(ThorCamSettingsWidget, self).__init__(**kwargs)


Builder.load_file(join(dirname(__file__), 'thorcam_player.kv'))
