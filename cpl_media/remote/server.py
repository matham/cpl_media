"""Remote server recorder
=========================

This class acts as a recorder that receives media from a camera and records it
to the network. E.g. the server can be configured
to receive video from a FFmpeg player and send it to the network where a client
player plays the video.
"""
from itertools import accumulate
from threading import Thread
import socket
import sys
import struct
from time import clock
from queue import Queue, Empty
import traceback
import select

from kivy.properties import ObjectProperty, NumericProperty, StringProperty, \
    BooleanProperty, ListProperty
from kivy.lang import Builder
from kivy.logger import Logger
from kivy.clock import Clock

from base_kivy_app.utils import yaml_dumps, yaml_loads

from cpl_media import error_guard
from cpl_media.recorder import BaseRecorder
import cpl_media

__all__ = ('RemoteVideoRecorder', 'RemoteData')


class EndConnection(Exception):
    pass


connection_errors = (
    EndConnection, ConnectionAbortedError, ConnectionResetError)


class RemoteData(object):

    def send_msg(self, sock, msg, value):
        # ts = time.monotonic()
        if msg == 'image':
            image, metadata = value
            bin_data = image.to_bytearray()
            data = yaml_dumps((
                'image', (list(map(len, bin_data)), image.get_pixel_format(),
                          image.get_size(), image.get_linesizes(), metadata)))
            data = data.encode('utf8')
        else:
            data = yaml_dumps((msg, value))
            data = data.encode('utf8')
            bin_data = []
        # ts2 = time.monotonic()

        sock.sendall(struct.pack('>II', len(data), sum(map(len, bin_data))))
        sock.sendall(data)
        for item in bin_data:
            sock.sendall(item)
        # print('part1: {}, part2: {}'.format(ts2 - ts, time.monotonic() - ts2))

    def decode_data(self, msg_buff, msg_len):
        n, bin_n = msg_len
        assert n + bin_n == len(msg_buff)
        data = msg_buff[:n].decode('utf8')
        msg, value = yaml_loads(data)

        if msg == 'image':
            bin_data = msg_buff[n:]
            planes_sizes, pix_fmt, size, linesize, metadata = value
            starts = list(accumulate([0] + list(planes_sizes[:-1])))
            ends = accumulate(planes_sizes)
            planes = [bin_data[s:e] for s, e in zip(starts, ends)]

            value = planes, pix_fmt, size, linesize, metadata
        else:
            assert not bin_n
        return msg, value

    def read_msg(self, sock, to_kivy_queue, trigger, msg_len, msg_buff):
        # still reading msg size
        msg = value = None
        if not msg_len:
            assert 8 - len(msg_buff)
            data = sock.recv(8 - len(msg_buff))
            if not data:
                raise EndConnection('Remote client was closed')

            msg_buff += data
            if len(msg_buff) == 8:
                msg_len = struct.unpack('>II', msg_buff)
                msg_buff = b''
        else:
            total = sum(msg_len)
            assert total - len(msg_buff)
            data = sock.recv(total - len(msg_buff))
            if not data:
                raise EndConnection('Remote client was closed')

            msg_buff += data
            if len(msg_buff) == total:
                msg, value = self.decode_data(msg_buff, msg_len)
                to_kivy_queue.put((msg, value))
                trigger()

                msg_len = ()
                msg_buff = b''
        return msg_len, msg_buff, msg, value


class RemoteVideoRecorder(BaseRecorder, RemoteData):
    """The server is intended to be started before anything else can be
    processed. Once the server is run, we can start recording
    (whether a client is connected or not). If the client is not playing,
    we don't send frames and just skip them.

    Client should accept these messages: exception, started_recording,
    stopped_recording, or image.
    Server accepts messages from client: started_playing, stopped_playing.

    We send started_recording in response to started_playing request.
    Either immediately if we were already recording, or later when we start.
    We send stopped_recording in response to stopping recording, if the client
    was between started_playing and stopped_playing (i.e. it expected to get
    images).
    We send images if between started_playing and stopped_playing requests
    and if we are recording.

    We only accept started_playing either before any started_playing
    message or after right a stopped_playing message (i.e. no duplicates).
    """

    __settings_attrs__ = ('server', 'port', 'timeout', 'max_images_buffered')

    server = StringProperty('')

    port = NumericProperty(10000)

    timeout = NumericProperty(.01)

    server_active = BooleanProperty(False)

    max_images_buffered = NumericProperty(5)

    from_kivy_queue = None
    """This queue can receive these messages: eof, image, started_recording,
    or stopped_recording.
    """

    to_kivy_queue = None

    _kivy_trigger = None

    server_thread = None

    _server_client_playing = False  # if client is playing

    _server_client_requested_playing = False  # if client requested playing

    _server_recording = None  # keeps track of whether we are recording rn

    def __init__(self, **kwargs):
        super(RemoteVideoRecorder, self).__init__(**kwargs)
        self._kivy_trigger = Clock.create_trigger(self.process_in_kivy_thread)

    def server_run(self, from_kivy_queue, to_kivy_queue):
        trigger = self._kivy_trigger
        timeout = self.timeout

        # Create a TCP/IP socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        # Connect the socket to the port where the server is listening
        server_address = (self.server, self.port)
        Logger.info('RemoteVideoRecorder: starting up on {} port {}'
                    .format(*server_address))

        try:
            sock.bind(server_address)
            sock.listen(1)

            while True:
                r, _, _ = select.select([sock], [], [], timeout)
                if not r:
                    try:
                        while True:
                            msg, value = from_kivy_queue.get_nowait()
                            if msg == 'eof':
                                return
                    except Empty:
                        pass

                    continue

                connection, client_address = sock.accept()
                msg_len, msg_buff = (), b''

                try:
                    while True:
                        r, _, _ = select.select([connection], [], [], timeout)
                        if r:
                            msg_len, msg_buff, msg, value = self.read_msg(
                                connection, to_kivy_queue,
                                trigger, msg_len, msg_buff)
                            self._server_message_from_client(connection, msg)

                        try:
                            while True:
                                if 'eof' == self._server_message_from_queue(
                                        connection, from_kivy_queue):
                                    return
                        except Empty:
                            pass
                except connection_errors:
                    pass
                finally:
                    Logger.info(
                        'RemoteVideoRecorder: closing client connection')
                    connection.close()
                    self._server_client_playing = False
                    self._server_client_requested_playing = False
        except Exception as e:
            exc_info = ''.join(traceback.format_exception(*sys.exc_info()))
            to_kivy_queue.put(
                ('exception', (str(e), exc_info)))
            trigger()
        finally:
            Logger.info('closing socket')
            sock.close()

    def _server_message_from_client(self, connection, msg):
        try:
            if msg == 'started_playing':
                if self._server_client_playing or \
                        self._server_client_requested_playing:
                    raise TypeError(
                        'Client already notified that it is playing')

                if self._server_recording:
                    self._server_client_playing = True
                    self.send_msg(connection, 'started_recording',
                                  self._server_recording)
                else:
                    self._server_client_requested_playing = True
            elif msg == 'stopped_playing':
                self._server_client_requested_playing = False
                self._server_client_playing = False
        except Exception as e:
            exc_info = ''.join(traceback.format_exception(*sys.exc_info()))
            self.send_msg(connection, 'exception', (str(e), exc_info))

    def _server_message_from_queue(self, connection, from_kivy_queue):
        msg, value = from_kivy_queue.get_nowait()
        if msg == 'eof':
            return 'eof'

        if msg == 'image':
            if self._server_client_playing:
                self.send_msg(connection, msg, value)
        elif msg == 'started_recording':
            self._server_recording = recording = tuple(value)
            # cannot be playing as it should at most be in requested_playing
            assert not self._server_client_playing

            if self._server_client_requested_playing:
                self._server_client_requested_playing = False
                self._server_client_playing = True
                self.send_msg(connection, 'started_recording', recording)
        elif msg == 'stopped_recording':
            assert self._server_recording is not None
            self._server_recording = None
            if self._server_client_requested_playing or \
                    self._server_client_playing:
                self.send_msg(connection, 'stopped_recording', None)
                self._server_client_requested_playing = False
                self._server_client_playing = False
        else:
            self.send_msg(connection, msg, value)

    @error_guard
    def send_message_to_client(self, msg, value):
        if self.from_kivy_queue is None:
            return

        self.from_kivy_queue.put((msg, value))

    @error_guard
    def send_image_to_client(self, image):
        if self.from_kivy_queue is None:
            return
        image, t = image

        if not self.max_images_buffered or \
                self.from_kivy_queue.qsize() < self.max_images_buffered:
            self.from_kivy_queue.put(('image', (image, {'t': t})))
            self.increment_in_kivy_thread(
                'size_recorded', sum(image.get_buffer_size()))
            self.increment_in_kivy_thread('frames_recorded')
        else:
            self.increment_in_kivy_thread('frames_skipped')

    @error_guard
    def process_in_kivy_thread(self, *largs):
        while self.to_kivy_queue is not None:
            try:
                msg, value = self.to_kivy_queue.get(block=False)

                if msg == 'exception':
                    e, exec_info = value
                    cpl_media.error_callback(e, exc_info=exec_info)
                    self.stop_server()
                else:
                    print('Got unknown RemoteVideoRecorder message',
                          msg, value)
            except Empty:
                break

    def start_server(self):
        if self.server_thread is not None:
            return

        self.server_active = True
        self._server_client_playing = False
        self._server_client_requested_playing = False
        self._server_recording = None
        from_kivy_queue = self.from_kivy_queue = Queue()
        to_kivy_queue = self.to_kivy_queue = Queue()

        server_thread = self.server_thread = Thread(
            target=self.server_run, args=(from_kivy_queue, to_kivy_queue))
        server_thread.start()

    @error_guard
    def stop_server(self, join=False):
        self.stop(join=join)
        if self.server_thread is None:
            return

        self.from_kivy_queue.put(('eof', None))
        if join:
            self.server_thread.join()

        self.server_thread = self.to_kivy_queue = self.from_kivy_queue = None
        self.server_active = False

    @error_guard
    def record(self, *largs, **kwargs):
        self.start_server()
        super(RemoteVideoRecorder, self).record(*largs, **kwargs)

        self.metadata_record_used = self.player.metadata_play_used
        self.ts_record = clock()
        self.player.frame_callbacks.append(self.send_image_to_client)
        self.from_kivy_queue.put(
            ('started_recording', self.metadata_record_used))

        self._complete_start()

    def stop(self, *largs, join=False):
        if super(RemoteVideoRecorder, self).stop(join=join):
            self.player.frame_callbacks.remove(self.send_image_to_client)
            self.from_kivy_queue.put(('stopped_recording', None))
            self._complete_stop()

    def stop_all(self, join=False):
        super(RemoteVideoRecorder, self).stop_all(join=join)
        self.stop_server(join=join)

    def record_thread_run(self, *largs):
        pass
