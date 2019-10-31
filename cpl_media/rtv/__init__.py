"""RTV24 based player
======================

This player can play Point Gray ethernet cameras.
"""
from kivy.properties import (
    NumericProperty, ReferenceListProperty,
    ObjectProperty, ListProperty, StringProperty, BooleanProperty,
    DictProperty, AliasProperty, OptionProperty, ConfigParserProperty)

from cpl_media.player import BasePlayer

try:
    from pybarst.core.server import BarstServer
    from pybarst.rtv import RTVChannel
except ImportError:
    RTVChannel = BarstServer = None
    Logger.debug('cpl_media: Could not import pybarst: '.format(e))


class RTVPlayer(BasePlayer):
    '''Wrapper for RTV based player.
    '''

    __settings_attrs__ = ('remote_computer_name', 'pipe_name', 'port',
                          'video_fmt')

    video_fmts = {
        'full_NTSC': (640, 480), 'full_PAL': (768, 576),
        'CIF_NTSC': (320, 240), 'CIF_PAL': (384, 288),
        'QCIF_NTSC': (160, 120), 'QCIF_PAL': (192, 144)
    }

    remote_computer_name = StringProperty('')
    '''The name of the computer running Barst, if it's a remote computer.
    Otherwise it's the empty string.
    '''

    pipe_name = StringProperty('filers_rtv')
    '''The internal name used to communicate with Barst. When running remotely,
    the name is used to discover Barst.
    '''

    port = NumericProperty(0)
    '''The RTV port on the card to use.
    '''

    video_fmt = StringProperty('full_NTSC')
    '''The video format of the video being played.

    It can be one of the keys in::

        {'full_NTSC': (640, 480), 'full_PAL': (768, 576),
        'CIF_NTSC': (320, 240), 'CIF_PAL': (384, 288),
        'QCIF_NTSC': (160, 120), 'QCIF_PAL': (192, 144)}
    '''

    channel = None

    def __init__(self, **kwargs):
        super(RTVPlayer, self).__init__(**kwargs)
        if BarstServer is None:
            raise ImportError('Could not import pybasrt.')

        self.metadata_play = self.metadata_play_used = \
            VideoMetadata('gray', 0, 0, 0)
        self.on_port()

    def on_port(self, *largs):
        self.player_summery = 'RTV-Port{}'.format(self.port)

    def play_thread_run(self):
        files = (
            r'C:\Program Files\Barst\Barst.exe',
            r'C:\Program Files\Barst\Barst64.exe',
            r'C:\Program Files (x86)\Barst\Barst.exe')
        if hasattr(sys, '_MEIPASS'):
            files = files + (join(sys._MEIPASS, 'Barst.exe'),
                             join(sys._MEIPASS, 'Barst64.exe'))
        barst_bin = None
        for f in files:
            f = abspath(f)
            if isfile(f):
                barst_bin = f
                break

        local = not self.remote_computer_name
        name = self.remote_computer_name if not local else '.'
        pipe_name = self.pipe_name
        full_name = r'\\{}\pipe\{}'.format(name, pipe_name)

        try:
            server = BarstServer(barst_path=barst_bin, pipe_name=full_name)
            server.open_server()
            img_fmt = self.metadata_play.fmt
            w, h = self.video_fmts[self.video_fmt]
            chan = RTVChannel(
                chan=self.port, server=server, video_fmt=self.video_fmt,
                frame_fmt=img_fmt, luma_filt=img_fmt == 'gray', lossless=True)
            chan.open_channel()
            try:
                chan.close_channel_server()
            except:
                pass
            chan.open_channel()
            chan.set_state(True)

            last_queue = None
            put = None
            started = False
            trigger = self.display_trigger
            use_rt = self.use_real_time
            count = 0

            while self.play_state != 'stopping':
                ts, buf = chan.read()
                if not started:
                    ivl_start = clock()
                    self.setattr_in_kivy_thread('ts_play', ivl_start)
                    self.change_status('play', True)
                    started = True

                ivl_end = clock()
                if ivl_end - ivl_start >= 1.:
                    real_rate = count / (ivl_end - ivl_start)
                    self.setattr_in_kivy_thread('real_rate', real_rate)
                    count = 0
                    ivl_start = ivl_end

                count += 1
                self.increment_in_kivy_thread('frames_played')

                if last_queue is not self.image_queue:
                    last_queue = self.image_queue
                    if last_queue is not None:
                        put = last_queue.put
                        put(('rate', 29.97))
                    else:
                        put = None

                img = Image(plane_buffers=[buf], pix_fmt=img_fmt, size=(w, h))
                if put is not None:
                    put((img, ivl_end if use_rt else ts))

                self.last_image = img, ivl_end if use_rt else ts
                trigger()
        except Exception as e:
            self.change_status('play', False, e)
            try:
                chan.close_channel_server()
            except:
                pass
            return

        try:
            chan.close_channel_server()
        except:
            pass
        self.change_status('play', False)

