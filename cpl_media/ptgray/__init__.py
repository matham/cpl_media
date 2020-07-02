"""PTGray based player
======================

This player can play Point Gray ethernet cameras using :mod:`pyflycap2`.
"""

from threading import Thread
from time import perf_counter as clock
from functools import partial
import time
from queue import Queue
from os.path import splitext, join, exists, isdir, abspath, dirname

from ffpyplayer.pic import Image

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

try:
    from pyflycap2.interface import GUI, Camera, CameraContext
except ImportError as err:
    GUI = Camera = CameraContext = None
    Logger.debug('cpl_media: Could not import pyflycap2: {}'.format(err))

__all__ = ('PTGrayPlayer', 'PTGraySettingsWidget')


class PTGrayPlayer(BasePlayer):
    """Wrapper for Point Gray based player.
    """

    _config_props_ = (
        'serial', 'ip', 'cam_config_opts', 'brightness', 'exposure',
        'sharpness', 'hue', 'saturation', 'gamma', 'shutter', 'gain',
        'iris', 'frame_rate', 'pan', 'tilt', 'mirror')

    is_available = BooleanProperty(CameraContext is not None)
    """Whether ptgray is available to play."""

    serial = NumericProperty(0)
    '''The serial number of the camera to open. Either :attr:`ip` or
    :attr:`serial` must be provided.
    '''

    serials = ListProperty([])
    """The serials of all the cams available.

    This may only be set by calling :meth:`ask_config`, not set directly.
    """

    ip = StringProperty('')
    '''The IP address of the camera to open. Either :attr:`ip` or
    :attr:`serial` must be provided.
    '''

    ips = ListProperty([])
    """The IPs of all the cams available.

    This may only be set by calling :meth:`ask_config`, not set directly.
    """

    cam_config_opts = DictProperty({'fmt': 'yuv422'})
    '''The configuration options used to configure the camera after opening.
    This are internal and can only be set by the internal thread once
    initially set by config.
    '''

    active_settings = ListProperty([])
    """The list of settings that the camera can control.

    This may only be set by calling :meth:`ask_config`, not set directly.
    """

    brightness = DictProperty({})
    """The camera options for the brightness setting.

    This may only be set by calling :meth:`ask_cam_option_config`, not
    set directly.
    """

    exposure = DictProperty({})
    """The camera options for the exposure setting.

    This may only be set by calling :meth:`ask_cam_option_config`, not
    set directly.
    """

    sharpness = DictProperty({})
    """The camera options for the sharpness setting.

    This may only be set by calling :meth:`ask_cam_option_config`, not
    set directly.
    """

    hue = DictProperty({})
    """The camera options for the hue setting.

    This may only be set by calling :meth:`ask_cam_option_config`, not
    set directly.
    """

    saturation = DictProperty({})
    """The camera options for the saturation setting.

    This may only be set by calling :meth:`ask_cam_option_config`, not
    set directly.
    """

    gamma = DictProperty({})
    """The camera options for the gamma setting.

    This may only be set by calling :meth:`ask_cam_option_config`, not
    set directly.
    """

    shutter = DictProperty({})
    """The camera options for the shutter setting.

    This may only be set by calling :meth:`ask_cam_option_config`, not
    set directly.
    """

    gain = DictProperty({})
    """The camera options for the gain setting.

    This may only be set by calling :meth:`ask_cam_option_config`, not
    set directly.
    """

    iris = DictProperty({})
    """The camera options for the iris setting.

    This may only be set by calling :meth:`ask_cam_option_config`, not
    set directly.
    """

    frame_rate = DictProperty({})
    """The camera options for the frame_rate setting.

    This may only be set by calling :meth:`ask_cam_option_config`, not
    set directly.
    """

    pan = DictProperty({})
    """The camera options for the pan setting.

    This may only be set by calling :meth:`ask_cam_option_config`, not
    set directly.
    """

    tilt = DictProperty({})
    """The camera options for the tilt setting.

    This may only be set by calling :meth:`ask_cam_option_config`, not
    set directly.
    """

    mirror = BooleanProperty(False)
    """Whether the camera is mirrored. Read only.
    """

    config_thread = None
    """The configuration thread.
    """

    config_queue = None
    """The configuration queue.
    """

    config_active_queue = ListProperty([])
    """The items currently being configured in the configuration thread.
    """

    config_active = BooleanProperty(False)
    """Whether the configuration is currently active.
    """

    _camera = None

    ffmpeg_pix_map = {
        'mono8': 'gray', 'yuv411': 'uyyvyy411', 'yuv422': 'uyvy422',
        'yuv444': 'yuv444p', 'rgb8': 'rgb8', 'mono16': 'gray16le',
        'rgb16': 'rgb565le', 's_mono16': 'gray16le', 's_rgb16': 'rgb565le',
        'bgr': 'bgr24', 'bgru': 'bgra', 'rgb': 'rgb24', 'rgbu': 'rgba',
        'bgr16': 'bgr565le', 'yuv422_jpeg': 'yuvj422p'}
    """Pixel formats supported by the camera and their :mod:`ffpyplayer`
    equivalent.
    """

    def __init__(self, open_thread=True, **kwargs):
        self.active_settings = self.get_setting_names()
        super(PTGrayPlayer, self).__init__(**kwargs)
        if CameraContext is not None and open_thread:
            self.start_config()

        def do_serial(*largs):
            self.ask_config('serial')

        self.fbind('serial', do_serial)

        def do_ip(*largs):
            self.ask_config('serial')

        self.fbind('ip', do_ip)
        do_ip()

        self.fbind('ip', self._update_summary)
        self.fbind('serial', self._update_summary)
        self._update_summary()

    def _update_summary(self, *largs):
        name = str(self.serial or self.ip)
        self.player_summery = 'PTGray "{}"'.format(name)

    def start_config(self, *largs):
        """Called by `__init__` to start the configuration thread.
        """
        self.config_queue = Queue()
        self.config_active_queue = []
        thread = self.config_thread = Thread(
            target=self.config_thread_run, name='Config thread')
        thread.start()
        self.ask_config('serials')

    @error_guard
    def stop_config(self, *largs, join=False):
        """Stops the configuration thread.
        """
        self.ask_config('eof', ignore_play=True)
        if join and self.config_thread:
            self.config_thread.join()
            self.config_thread = None

    @error_guard
    def ask_config(self, item, ignore_play=False):
        """Asks to read the config values of the item. E.g. ``'serials'``
        for the list of serials or ``'gui'`` to show the PTGray GUI to the
        user.

        :param item: The request to send.
        :param ignore_play: Whether to send it event if the camera is playing.
        """
        if not ignore_play and self.play_state != 'none':
            raise TypeError('Cannot configure while playing')

        queue = self.config_queue
        if queue is not None:
            self.config_active = True
            if item != 'eof':
                # XXX: really strange bug, but somehow if this is set here when
                # we call stop_all and we join, it blocks forever on setting
                # can_play. Blocking only happens when kv binds to can_play.
                # Makes no sense as it's all from the same thread???
                self.can_play = False
            self.config_active_queue.append(item)
            queue.put(item)

    def ask_cam_option_config(self, setting, name, value):
        """Asks to set the setting of the camera to a specific value.

        :param setting: The setting, e.g. ``"brightness"``.
        :param name: How to set it, e.g. ``"value"`` to set the value or
            ``'one push'`` to auto configure it.
        :param value: The value to use to set it to.
        """
        if not name or getattr(self, setting)[name] != value:
            self.ask_config(
                ('option', (setting, name, value)), ignore_play=True)

    def finish_ask_config(self, item, *largs, **kwargs):
        """Called in the kivy thread automatically after the camera
        has been re-configured.
        """
        if isinstance(item, tuple) and item[0] == 'option':
            setting, _, _ = item[1]
            getattr(self, setting).update(kwargs['values'])
            set_rate = setting in ('frame_rate', 'metadata_play_used')
        else:
            for k, v in kwargs.items():
                setattr(self, k, v)
            set_rate = 'frame_rate' in kwargs or 'metadata_play_used' in kwargs

        self.active_settings = self.get_active_settings()

        if set_rate:
            fmt, w, h, r = self.metadata_play_used
            if 'max' in self.frame_rate:
                r = max(r, self.frame_rate['max'])
                self.metadata_play_used = VideoMetadata(fmt, w, h, r)

    @error_guard
    def _remove_config_item(self, item, *largs):
        self.config_active_queue.remove(item)
        if not self.config_active_queue:
            self.config_active = False
            self.can_play = True

    def get_active_settings(self):
        """List of settings supported by the camera.
        """
        settings = []
        for setting in self.get_setting_names():
            if getattr(self, setting).get('present', False):
                settings.append(setting)
        return list(sorted(settings))

    def get_setting_names(self):
        """List of all settings potentially supported by a camera.
        """
        return list(sorted((
            'brightness', 'exposure', 'sharpness', 'hue', 'saturation',
            'gamma', 'shutter', 'gain', 'iris', 'frame_rate', 'pan', 'tilt')))

    def read_cam_option_config(self, setting, cam):
        """Reads the setting from the camera.

        Called from the internal configuration thread.
        """
        options = {}
        mn, mx = cam.get_cam_abs_setting_range(setting)
        options['min'], options['max'] = mn, mx
        options['value'] = cam.get_cam_abs_setting_value(setting)
        options.update(cam.get_cam_setting_option_values(setting))
        return options

    def write_cam_option_config(self, setting, cam, name, value):
        """Writes the setting to the camera.

        Called from the internal configuration thread.
        """
        if name == 'value':
            cam.set_cam_abs_setting_value(setting, value)
        else:
            cam.set_cam_setting_option_values(setting, **{name: value})
            if name == 'one_push' and value:
                while cam.get_cam_setting_option_values(setting)['one_push']:
                    time.sleep(.2)

    def write_cam_options_config(self, cam):
        """Writes all the settings as provided as properties of this instance
        to the camera.

        Called from the internal configuration thread.
        """
        for setting in self.get_setting_names():
            settings = getattr(self, setting)
            cam.set_cam_setting_option_values(
                setting, abs=settings.get('abs', None),
                controllable=settings.get('controllable', None),
                auto=settings.get('auto', None)
            )
            settings_read = cam.get_cam_setting_option_values(setting)
            if settings_read['controllable'] and not settings_read['auto']:
                if settings_read['abs'] and 'value' in settings:
                    cam.set_cam_abs_setting_value(setting, settings['value'])
                elif not settings_read['abs'] and 'relative_value' in settings:
                    cam.set_cam_setting_option_values(
                        setting, relative_value=settings['relative_value'])

        if cam.get_horizontal_mirror()[0]:
            cam.set_horizontal_mirror(self.mirror)

    def read_cam_options_config(self, cam):
        """Reads all the settings of this instance
        to the camera.

        Called from the internal configuration thread.
        """
        for setting in self.get_setting_names():
            Clock.schedule_once(partial(
                self.finish_ask_config, None,
                **{setting: self.read_cam_option_config(setting, cam)}))

        if cam.get_horizontal_mirror()[0]:
            Clock.schedule_once(partial(
                self.finish_ask_config, None,
                mirror=cam.get_horizontal_mirror()[1]))

    def write_gige_opts(self, c, opts):
        """Writes the GIGE setting to the camera.

        Called from the internal configuration thread.
        """
        c.set_gige_mode(opts['mode'])
        c.set_drop_mode(opts['drop'])
        c.set_gige_config(opts['offset_x'], opts['offset_y'], opts['width'],
                          opts['height'], opts['fmt'])
        c.set_gige_packet_config(opts['resend'], opts['timeout'],
                                 opts['timeout_retries'])
        c.set_gige_binning(opts['horizontal'], opts['vertical'])

    def read_gige_opts(self, c):
        """Reads the GIGE setting from the camera.

        Called from the internal configuration thread.
        """
        opts = self.cam_config_opts
        opts['drop'] = c.get_drop_mode()
        opts.update(c.get_gige_config())
        opts['mode'] = c.get_gige_mode()
        opts.update(c.get_gige_packet_config())
        opts['horizontal'], opts['vertical'] = c.get_gige_binning()

    def config_thread_run(self):
        """The function run by the configuration thread.
        """
        queue = self.config_queue
        cc = CameraContext()

        while True:
            item = queue.get()
            try:
                if item == 'eof':
                    return

                ip = ''
                serial = 0
                do_serial = False
                if item == 'serials':
                    cc.rescan_bus()
                    cams = cc.get_gige_cams()
                    old_serial = serial = self.serial
                    old_ip = ip = self.ip

                    ips = ['.'.join(map(str, Camera(serial=s).ip))
                           for s in cams]
                    if cams:
                        if serial not in cams and ip not in ips:
                            serial = 0
                            ip = ''
                        elif serial in cams:
                            ip = ips[cams.index(serial)]
                        else:
                            serial = cams[ips.index(ip)]

                    Clock.schedule_once(partial(
                        self.finish_ask_config, item, serials=cams,
                        serial=serial, ips=ips, ip=ip))

                    if serial:
                        c = Camera(serial=serial)
                        c.connect()
                        if old_serial == serial or old_ip == ip:
                            self.write_gige_opts(c, self.cam_config_opts)
                            self.write_cam_options_config(c)
                        self.read_gige_opts(c)
                        self.read_cam_options_config(c)

                        if self.cam_config_opts['fmt'] not in \
                            self.ffmpeg_pix_map or \
                                self.cam_config_opts['fmt'] == 'yuv411':
                            self.cam_config_opts['fmt'] = 'rgb'
                            self.write_gige_opts(c, self.cam_config_opts)
                        c.disconnect()
                        c = None
                elif item == 'serial':
                    do_serial = True
                elif item == 'gui':
                    gui = GUI()
                    gui.show_selection()
                    do_serial = True  # read possibly updated config
                elif c or self._camera:
                    cam = c or self._camera
                    if isinstance(item, tuple) and item[0] == 'mirror':
                        if cam.get_horizontal_mirror()[0]:
                            cam.set_horizontal_mirror(item[1])
                        Clock.schedule_once(partial(
                            self.finish_ask_config, item,
                            mirror=cam.get_horizontal_mirror()[1]))
                    elif isinstance(item, tuple) and item[0] == 'option':
                        _, (setting, name, value) = item
                        if name:
                            self.write_cam_option_config(
                                setting, cam, name, value)
                        Clock.schedule_once(partial(
                            self.finish_ask_config, item,
                            values=self.read_cam_option_config(setting, cam)))

                if do_serial:
                    _ip = ip = self.ip
                    serial = self.serial
                    if serial or ip:
                        if _ip:
                            _ip = list(map(int, _ip.split('.')))
                        c = Camera(serial=serial or None, ip=_ip or None)
                        serial = c.serial
                        ip = '.'.join(map(str, c.ip))
                        c.connect()
                        self.read_gige_opts(c)
                        self.read_cam_options_config(c)

                        if self.cam_config_opts['fmt'] not in \
                            self.ffmpeg_pix_map or \
                                self.cam_config_opts['fmt'] == 'yuv411':
                            self.cam_config_opts['fmt'] = 'rgb'
                            self.write_gige_opts(c, self.cam_config_opts)
                        c.disconnect()
                        c = None

                if serial or ip:
                    opts = self.cam_config_opts
                    if opts['fmt'] not in self.ffmpeg_pix_map:
                        raise Exception('Pixel format {} cannot be converted'.
                                        format(opts['fmt']))
                    if opts['fmt'] == 'yuv411':
                        raise ValueError('yuv411 is not currently supported')
                    metadata = VideoMetadata(
                        self.ffmpeg_pix_map[opts['fmt']], opts['width'],
                        opts['height'], 30.0)
                    Clock.schedule_once(partial(
                        self.finish_ask_config, item, metadata_play=metadata,
                        metadata_play_used=metadata, serial=serial, ip=ip))
            except Exception as e:
                self.exception(e)
            finally:
                Clock.schedule_once(partial(self._remove_config_item, item))

    def play_thread_run(self):
        process_frame = self.process_frame
        c = None
        ffmpeg_fmts = self.ffmpeg_pix_map

        try:
            ip = list(map(int, self.ip.split('.'))) if self.ip else None
            c = Camera(serial=self.serial or None, ip=ip)
            c.connect()

            started = False
            # use_rt = self.use_real_time
            count = 0
            ivl_start = 0

            c.start_capture()
            while self.play_state != 'stopping':
                try:
                    c.read_next_image()
                except Exception as e:
                    self.exception(e)
                    continue
                if not started:
                    ivl_start = clock()
                    self.setattr_in_kivy_thread('ts_play', ivl_start)
                    Clock.schedule_once(self.complete_start)
                    started = True
                    self._camera = c

                ivl_end = clock()
                if ivl_end - ivl_start >= 1.:
                    real_rate = count / (ivl_end - ivl_start)
                    self.setattr_in_kivy_thread('real_rate', real_rate)
                    count = 0
                    ivl_start = ivl_end

                count += 1
                self.increment_in_kivy_thread('frames_played')

                image = c.get_current_image()
                pix_fmt = image['pix_fmt']
                if pix_fmt not in ffmpeg_fmts:
                    raise Exception('Pixel format {} cannot be converted'.
                                    format(pix_fmt))
                ff_fmt = ffmpeg_fmts[pix_fmt]
                if ff_fmt == 'yuv444p':
                    buff = image['buffer']
                    img = Image(
                        plane_buffers=[buff[1::3], buff[0::3], buff[2::3]],
                        pix_fmt=ff_fmt, size=(image['cols'], image['rows']))
                elif pix_fmt == 'yuv411':
                    raise ValueError('yuv411 is not currently supported')
                else:
                    img = Image(
                        plane_buffers=[image['buffer']], pix_fmt=ff_fmt,
                        size=(image['cols'], image['rows']))

                process_frame(img, {'t': ivl_end})
        except Exception as e:
            self.exception(e)
        finally:
            self._camera = None

            try:
                c.disconnect()
            finally:
                Clock.schedule_once(self.complete_stop)

    def stop_all(self, join=False):
        self.stop_config(join=join)
        super(PTGrayPlayer, self).stop_all(join=join)


class PTGraySettingsWidget(BoxLayout):
    """Settings widget for :class:`PTGrayPlayer`.
    """

    settings_last = ''
    """The last name of the setting the GUI currently controls (i.e. one
    of :meth:`PTGrayPlayer.get_active_settings`).
    """

    opt_settings = DictProperty({})
    """The values for the settings currently being controlled in the GUI.
    """

    player: PTGrayPlayer = None
    """The player.
    """

    def __init__(self, player=None, **kwargs):
        if player is None:
            player = PTGrayPlayer()
        self.player = player
        super(PTGraySettingsWidget, self).__init__(**kwargs)

    def _track_setting(self, *largs):
        self.opt_settings = getattr(self.player, self.settings_last)

    def bind_pt_setting(self, setting):
        """Tracks the setting currently selected in the GUI and auto-updates
        when it changes.
        """
        if self.settings_last:
            self.player.funbind(self.settings_last, self._track_setting)
        self.settings_last = ''
        self.opt_settings = {}

        if setting:
            self.settings_last = setting
            self.player.fbind(setting, self._track_setting)
            self._track_setting()


Builder.load_file(join(dirname(__file__), 'ptgray_player.kv'))
