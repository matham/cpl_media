"""Remote player
================

This class plays video from the network. E.g. a server can be configured
to receive video from a FFmpeg camera. This client plays the video from
the server.

"""

from threading import Thread
from time import perf_counter as clock
import socket
import sys
from queue import Queue, Empty
from os.path import splitext, join, exists, isdir, abspath, dirname
import traceback
import select

from ffpyplayer.pic import Image, SWScale

from kivy.logger import Logger
from kivy.properties import StringProperty, NumericProperty, BooleanProperty
from kivy.clock import Clock
from kivy.uix.boxlayout import BoxLayout
from kivy.lang import Builder

from cpl_media import error_guard
from cpl_media.player import BasePlayer, VideoMetadata
import cpl_media
from .server import RemoteData

__all__ = ('RemoteVideoPlayer', 'ClientPlayerSettingsWidget')


class RemoteVideoPlayer(BasePlayer, RemoteData):

    __settings_attrs__ = ('server', 'port', 'timeout')

    server = StringProperty('')

    port = NumericProperty(0)

    timeout = NumericProperty(.01)

    client_active = BooleanProperty(False)

    from_kivy_queue = None

    to_kivy_queue = None

    _kivy_trigger = None

    listener_thread = None

    _frame_count = 0

    _ivl_start = 0

    def __init__(self, **kwargs):
        super(RemoteVideoPlayer, self).__init__(**kwargs)
        self._kivy_trigger = Clock.create_trigger(self.process_in_kivy_thread)

        self.fbind('server', self._update_summary)
        self.fbind('port', self._update_summary)
        self._update_summary()

    def _update_summary(self, *largs):
        self.player_summery = 'Network "{}:{}"'.format(self.server, self.port)

    def listener_run(self, from_kivy_queue, to_kivy_queue):
        trigger = self._kivy_trigger
        timeout = self.timeout

        # Create a TCP/IP socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        # Connect the socket to the port where the server is listening
        server_address = (self.server, self.port)
        Logger.info('RemoteVideoPlayer: connecting to {} port {}'
                    .format(*server_address))

        msg_len, msg_buff = (), b''

        try:
            sock.connect(server_address)
            done = False

            while not done:
                r, _, _ = select.select([sock], [], [], timeout)
                if r:
                    msg_len, msg_buff, msg, value = self.read_msg(
                        sock, msg_len, msg_buff)
                    if msg is not None:
                        to_kivy_queue.put((msg, value))
                        trigger()

                try:
                    while True:
                        msg, value = from_kivy_queue.get_nowait()
                        if msg == 'eof':
                            done = True
                            break
                        else:
                            self.send_msg(sock, msg, value)
                except Empty:
                    pass
        except Exception as e:
            exc_info = ''.join(traceback.format_exception(*sys.exc_info()))
            to_kivy_queue.put(
                ('exception_exit', (str(e), exc_info)))
            trigger()
        finally:
            Logger.info('RemoteVideoPlayer: closing socket')
            sock.close()

    def send_message_to_server(self, key, value):
        if self.from_kivy_queue is None:
            return
        self.from_kivy_queue.put((key, value))

    def start_listener(self):
        if self.listener_thread is not None:
            return

        self.client_active = True
        from_kivy_queue = self.from_kivy_queue = Queue()
        to_kivy_queue = self.to_kivy_queue = Queue()
        thread = self.listener_thread = Thread(
            target=self.listener_run, args=(from_kivy_queue, to_kivy_queue))
        thread.start()

    @error_guard
    def process_in_kivy_thread(self, *largs):
        while self.to_kivy_queue is not None:
            try:
                msg, value = self.to_kivy_queue.get(block=False)

                if msg == 'exception':
                    e, exec_info = value
                    cpl_media.error_callback(e, exc_info=exec_info)
                elif msg == 'exception_exit':
                    e, exec_info = value
                    cpl_media.error_callback(e, exc_info=exec_info)
                    self.stop_all()
                    if self.play_state != 'none':
                        self._complete_stop()
                elif msg == 'started_recording':
                    if self.play_state == 'starting':
                        self.ts_play = self._ivl_start = clock()
                        self._frame_count = 0

                        self.metadata_play_used = VideoMetadata(*value)
                        self._complete_start()
                elif msg == 'stopped_recording':
                    self.stop()
                elif msg == 'stopped_playing':
                    self._complete_stop()
                elif msg == 'image':
                    if self.play_state != 'playing':
                        continue

                    t = clock()
                    if t - self._ivl_start >= 1.:
                        self.real_rate = self._frame_count / (
                            t - self._ivl_start)
                        self._frame_count = 0
                        self._ivl_start = t

                    self._frame_count += 1
                    self.frames_played += 1

                    plane_buffers, pix_fmt, size, linesize, metadata = value
                    sws = SWScale(*size, pix_fmt, ofmt=pix_fmt)
                    img = Image(
                        plane_buffers=plane_buffers, pix_fmt=pix_fmt,
                        size=size, linesize=linesize)
                    self.process_frame(sws.scale(img), metadata)
                else:
                    print('Got unknown RemoteVideoPlayer message', msg, value)
            except Empty:
                break

    @error_guard
    def stop_listener(self, join=False):
        self.stop(join=join)
        if self.listener_thread is None:
            return

        self.from_kivy_queue.put(('eof', None))
        if join:
            self.listener_thread.join()

        self.listener_thread = self.to_kivy_queue = self.from_kivy_queue = None
        self.client_active = False

    @error_guard
    def play(self):
        # we make sure client is connected and request metadata. If the player
        # is not playing on the server, we cannot ask it to play so we wait
        # until it sends us the metadata. If it was already playing, it sends
        # it again, otherwise, it'll send it to us when it starts playing
        self.start_listener()
        super(RemoteVideoPlayer, self).play()
        self.send_message_to_server('started_playing', None)

    def stop(self, *largs, join=False):
        if super(RemoteVideoPlayer, self).stop(join=join):
            self.send_message_to_server('stopped_playing', None)

    def play_thread_run(self):
        pass

    def stop_all(self, join=False):
        super(RemoteVideoPlayer, self).stop_all(join=join)
        self.stop_listener(join=join)


class ClientPlayerSettingsWidget(BoxLayout):

    player: RemoteVideoPlayer = None

    def __init__(self, player=None, **kwargs):
        if player is None:
            player = RemoteVideoPlayer()
        self.player = player
        super(ClientPlayerSettingsWidget, self).__init__(**kwargs)


Builder.load_file(join(dirname(__file__), 'client_player.kv'))
