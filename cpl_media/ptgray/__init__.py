"""PTGray based player
======================

This player can play Point Gray ethernet cameras.
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
    Logger.debug('cpl_media: Could not import pyflycap2: '.format(err))

__all__ = ('PTGrayPlayer', 'PTGraySettingsWidget')


class PTGrayPlayer(BasePlayer):
    """Wrapper for Point Gray based player.
    """

    __settings_attrs__ = (
        'serial', 'ip', 'cam_config_opts', 'brightness', 'exposure',
        'sharpness', 'hue', 'saturation', 'gamma', 'shutter', 'gain',
        'iris', 'frame_rate', 'pan', 'tilt', 'mirror')

    is_available = BooleanProperty(CameraContext is not None)

    serial = NumericProperty(0)
    '''The serial number of the camera to open. Either :attr:`ip` or
    :attr:`serial` must be provided.
    '''

    serials = ListProperty([])

    ip = StringProperty('')
    '''The ip address of the camera to open. Either :attr:`ip` or
    :attr:`serial` must be provided.
    '''

    ips = ListProperty([])

    cam_config_opts = DictProperty({})
    '''The configuration options used to configure the camera after opening.
    '''

    active_settings = ListProperty([])

    brightness = DictProperty({})
    exposure = DictProperty({})
    sharpness = DictProperty({})
    hue = DictProperty({})
    saturation = DictProperty({})
    gamma = DictProperty({})
    shutter = DictProperty({})
    gain = DictProperty({})
    iris = DictProperty({})
    frame_rate = DictProperty({})
    pan = DictProperty({})
    tilt = DictProperty({})

    mirror = BooleanProperty(False)

    config_thread = None

    config_queue = None

    config_active_queue = ListProperty([])

    _camera = None

    ffmpeg_pix_map = {
        'mono8': 'gray', 'yuv411': 'uyyvyy411', 'yuv422': 'uyvy422',
        'yuv444': 'yuv444p', 'rgb8': 'rgb8', 'mono16': 'gray16le',
        'rgb16': 'rgb565le', 's_mono16': 'gray16le', 's_rgb16': 'rgb565le',
        'bgr': 'bgr24', 'bgru': 'bgra', 'rgb': 'rgb24', 'rgbu': 'rgba',
        'bgr16': 'bgr565le', 'yuv422_jpeg': 'yuvj422p'}

    cam_registers = {}

    def __init__(self, **kwargs):
        self.active_settings = self.get_setting_names()
        super(PTGrayPlayer, self).__init__(**kwargs)
        if CameraContext is not None:
            self.start_config()

        def do_serial(*largs):
            self.ask_config('serial')

        self.fbind('serial', do_serial)

        def do_ip(*largs):
            self.player_summery = 'PT-{}'.format(self.ip)
            self.ask_config('serial')

        self.fbind('ip', do_ip)
        do_ip()

    def start_config(self, *largs):
        self.config_queue = Queue()
        self.config_active_queue = []
        thread = self.config_thread = Thread(
            target=self.config_thread_run, name='Config thread')
        thread.start()
        self.ask_config('serials')

    @error_guard
    def stop_config(self, *largs, join=False):
        self.ask_config('eof')
        if join and self.config_thread:
            self.config_thread.join()
            self.config_thread = None

    @error_guard
    def ask_config(self, item):
        if self.play_state != 'none':
            raise TypeError('Cannot configure while playing')

        queue = self.config_queue
        if queue is not None:
            self.config_active = True
            self.config_active_queue.append(item)
            queue.put_nowait(item)

    def ask_cam_option_config(self, setting, name, value):
        if not name or getattr(self, setting)[name] != value:
            self.ask_config(('option', (setting, name, value)))

    def finish_ask_config(self, item, *largs, **kwargs):
        if isinstance(item, tuple) and item[0] == 'option':
            setting, _, _ = item[1]
            getattr(self, setting).update(kwargs['values'])
        else:
            for k, v in kwargs.items():
                setattr(self, k, v)

        self.active_settings = self.get_active_settings()

    @error_guard
    def _remove_config_item(self, item, *largs):
        self.config_active_queue.remove(item)
        if not self.config_active_queue:
            self.config_active = False

    def get_active_settings(self):
        settings = []
        for setting in self.get_setting_names():
            if getattr(self, setting).get('present', False):
                settings.append(setting)
        return list(sorted(settings))

    def get_setting_names(self):
        return list(sorted((
            'brightness', 'exposure', 'sharpness', 'hue', 'saturation',
            'gamma', 'shutter', 'gain', 'iris', 'frame_rate', 'pan', 'tilt')))

    def read_cam_option_config(self, setting, cam):
        options = {}
        mn, mx = cam.get_cam_abs_setting_range(setting)
        options['min'], options['max'] = mn, mx
        options['value'] = cam.get_cam_abs_setting_value(setting)
        options.update(cam.get_cam_setting_option_values(setting))
        return options

    def write_cam_option_config(self, setting, cam, name, value):
        if name == 'value':
            cam.set_cam_abs_setting_value(setting, value)
        else:
            cam.set_cam_setting_option_values(setting, **{name: value})
            if name == 'one_push' and value:
                while cam.get_cam_setting_option_values(setting)['one_push']:
                    time.sleep(.2)

    def write_cam_options_config(self, cam):
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
        for setting in self.get_setting_names():
            Clock.schedule_once(partial(
                self.finish_ask_config, None,
                **{setting: self.read_cam_option_config(setting, cam)}))

        if cam.get_horizontal_mirror()[0]:
            Clock.schedule_once(partial(
                self.finish_ask_config, None,
                mirror=cam.get_horizontal_mirror()[1]))

    def write_gige_opts(self, c, opts):
        c.set_gige_mode(opts['mode'])
        c.set_drop_mode(opts['drop'])
        c.set_gige_config(opts['offset_x'], opts['offset_y'], opts['width'],
                          opts['height'], opts['fmt'])
        c.set_gige_packet_config(opts['resend'], opts['timeout'],
                                 opts['timeout_retries'])
        c.set_gige_binning(opts['horizontal'], opts['vertical'])

    def read_gige_opts(self, c):
        opts = self.cam_config_opts
        opts['drop'] = c.get_drop_mode()
        opts.update(c.get_gige_config())
        opts['mode'] = c.get_gige_mode()
        opts.update(c.get_gige_packet_config())
        opts['horizontal'], opts['vertical'] = c.get_gige_binning()

    def config_thread_run(self):
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
                            serial = cams[0]
                            ip = ips[0]
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
                            self.write_cam_option_config(setting, cam, name, value)
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
            rate = self.metadata_play_used.rate

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
                    Clock.schedule_once(self._complete_start)
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

                process_frame(img, ivl_end)
        except Exception as e:
            self.exception(e)
        finally:
            self._camera = None

        try:
            c.disconnect()
        except:
            pass
        Clock.schedule_once(self._complete_stop)

    def stop_all(self, join=False):
        super(PTGrayPlayer, self).stop_all(join=join)
        self.stop_config(join=join)


class PTGraySettingsWidget(BoxLayout):

    settings_last = ''

    opt_settings = DictProperty({})

    player: PTGrayPlayer = None

    def __init__(self, player=None, **kwargs):
        if player is None:
            player = PTGrayPlayer()
        self.player = player
        super(PTGraySettingsWidget, self).__init__(**kwargs)

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

    def _track_setting(self, *largs):
        self.opt_settings = getattr(self.player, self.settings_last)

    def bind_pt_setting(self, setting):
        if self.settings_last:
            self.player.funbind(self.settings_last, self._track_setting)
        self.settings_last = ''
        self.opt_settings = {}

        if setting:
            self.settings_last = setting
            self.player.fbind(setting, self._track_setting)
            self._track_setting()


Builder.load_file(join(dirname(__file__), 'ptgray_player.kv'))
