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
from time import perf_counter as clock
from queue import Queue, Empty
import traceback
from os.path import splitext, join, exists, isdir, abspath, dirname
import select

from kivy.properties import ObjectProperty, NumericProperty, StringProperty, \
    BooleanProperty, ListProperty
from kivy.logger import Logger
from kivy.clock import Clock
from kivy.uix.boxlayout import BoxLayout
from kivy.lang import Builder

from base_kivy_app.utils import yaml_dumps, yaml_loads

from cpl_media import error_guard
from cpl_media.recorder import BaseRecorder
import cpl_media

__all__ = ('RemoteVideoRecorder', 'RemoteRecordSettingsWidget', 'RemoteData',
           'EndConnection')


class EndConnection(Exception):
    """Raised when the connection is closed.
    """
    pass


connection_errors = (
    EndConnection, ConnectionAbortedError, ConnectionResetError)


class RemoteData(object):
    """Provides methods to send and receive messages from a socket.

    """

    def send_msg(self, sock, msg, value):
        """Sends message to the server.

        :param sock: The socket
        :param msg: The message name string (e.g. image).
        :param value: The message value.
        :return:
        """
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
        """Decodes buffer data received from the network.

        :param msg_buff: The bytes data received so far.
        :param msg_len: The expected size of the message as tuple -
            The size of the message and any associated binary data.
        :return: A tuple of the message name and value, or (None, None) if
            we haven't read the full message.
        """
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

    def read_msg(self, sock, msg_len, msg_buff):
        """Reads the message and decodes it once we read the full message.

        :param sock: The socket.
        :param msg_len: The tuple of the message length and associated data.
            If empty, the start of the next message will provide this
            information.
        :param msg_buff: The message buffer.
        :return: A 4-tuple of ``(msg_len, msg_buff, msg, value)``. Where
            ``msg_len, msg_buff`` are similar to the input, and
            ``(msg, value)`` is the message and its value if we read a full
            message, otherwise they are None.
        """
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

                msg_len = ()
                msg_buff = b''
        return msg_len, msg_buff, msg, value


class RemoteVideoRecorder(BaseRecorder, RemoteData):
    """A server recorder that takes images from a player and sends it
    to a client player over a socket.

    The server is intended to be started before anything else can be
    processed. Once the server is run, we can start recording
    (whether a client is connected or not). If the client is not playing,
    we don't send frames and just skip them.

    A client should accept these messages: exception, started_recording,
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
    message or after a stopped_playing message (i.e. no duplicates).
    """

    _config_props_ = ('server', 'port', 'timeout', 'max_images_buffered')

    server = StringProperty('localhost')
    """The server address on which to broadcast the data.
    """

    port = NumericProperty(10000)
    """The server port on which to broadcast the data.
    """

    timeout = NumericProperty(.01)
    """How long to wait before timing out when reading data before checking the
    queue for other requests.
    """

    server_active = BooleanProperty(False)
    """Whether the server is currently running.
    """

    max_images_buffered = NumericProperty(5)
    """How many images the server should buffer before it starts dropping
    images, rather than queuing them to be sent to the client.
    """

    from_kivy_queue = None
    """The queue that receives messages from Kivy.

    This queue can receive these messages: eof, image, started_recording,
    or stopped_recording.
    """

    to_kivy_queue = None
    """The queue that sends messages to Kivy.
    """

    _kivy_trigger = None
    """Trigger for kivy thread to read the queue - to be called after adding
    something to the queue.
    """

    server_thread = None
    """The server thread instance.
    """

    _server_client_playing = False
    """Whether the client is playing

    May only be set from the server thread.
    """

    _server_client_requested_playing = False
    """Whether the client requested playing

    May only be set from the server thread.
    """

    _server_recording = None
    """Keeps track of whether we are recording rn

    May only be set from the server thread.
    """

    _first_image_while_playing = False
    """If this is the first image right after we started recording.

    May only be set from the server thread.
    """

    def __init__(self, **kwargs):
        super(RemoteVideoRecorder, self).__init__(**kwargs)
        self._kivy_trigger = Clock.create_trigger(self.process_in_kivy_thread)

        self.fbind('server', self._update_summary)
        self.fbind('port', self._update_summary)
        self._update_summary()

    def _update_summary(self, *largs):
        self.recorder_summery = 'Network "{}:{}"'.format(
            self.server, self.port)

    def server_run(self, from_kivy_queue, to_kivy_queue):
        """Server method, that is executed in the internal server thread.
        """
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
                            if 'eof' == self._server_message_from_queue(
                                    None, from_kivy_queue):
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
                                connection, msg_len, msg_buff)
                            if msg is not None:
                                if not self._server_message_from_client(
                                        connection, msg, value):
                                    to_kivy_queue.put((msg, value))
                                    trigger()

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
                    self._server_client_playing = False
                    self._server_client_requested_playing = False
                    try:
                        connection.close()
                    except Exception:
                        pass
        except Exception as e:
            exc_info = ''.join(traceback.format_exception(*sys.exc_info()))
            to_kivy_queue.put(
                ('exception', (str(e), exc_info)))
            trigger()
        finally:
            Logger.info('closing socket')
            sock.close()

    def _server_message_from_client(self, connection, msg, value):
        """Processes message from the client.

        Executed from the internal thread.
        """
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
                return True
            elif msg == 'stopped_playing':
                self._server_client_requested_playing = False
                self._server_client_playing = False
                self.send_msg(connection, 'stopped_playing', None)
                return True
        except Exception as e:
            exc_info = ''.join(traceback.format_exception(*sys.exc_info()))
            self.send_msg(connection, 'exception', (str(e), exc_info))
            return True
        return False

    def _server_message_from_queue(self, connection, from_kivy_queue):
        """Processes message from the kivy queue.

        Executed from the internal thread.
        """
        msg, value = from_kivy_queue.get_nowait()
        if msg == 'eof':
            return 'eof'

        if msg == 'image':
            if self._first_image_while_playing:
                self.setattr_in_kivy_thread('ts_record', clock())
                self._first_image_while_playing = False

            if self._server_client_playing:
                assert connection is not None
                self.increment_in_kivy_thread(
                    'size_recorded', sum(value[0].get_buffer_size()))
                self.increment_in_kivy_thread('frames_recorded')

                self.send_msg(connection, msg, value)
            else:
                self.increment_in_kivy_thread('frames_skipped')
        elif msg == 'started_recording':
            self._server_recording = recording = tuple(value)
            # cannot be playing as it should at most be in requested_playing
            assert not self._server_client_playing
            self._first_image_while_playing = True

            if self._server_client_requested_playing:
                assert connection is not None
                self._server_client_requested_playing = False
                self._server_client_playing = True
                self.send_msg(connection, 'started_recording', recording)
        elif msg == 'stopped_recording':
            assert self._server_recording is not None
            self._server_recording = None
            if self._server_client_requested_playing or \
                    self._server_client_playing:
                assert connection is not None
                self.send_msg(connection, 'stopped_recording', None)
                self._server_client_requested_playing = False
                self._server_client_playing = False
        elif connection is not None:
            self.send_msg(connection, msg, value)

    @error_guard
    def send_message_to_client(self, msg, value):
        """Sends the message to the client through the server.

        :param msg: The message name string.
        :param value: The message value.
        """
        if self.from_kivy_queue is None:
            return

        self.from_kivy_queue.put((msg, value))

    @error_guard
    def send_image_to_client(self, image):
        """Sends a image (tuple of image, metadata) to the client through the
        server.
        """
        if self.from_kivy_queue is None:
            return
        image, metadata = image

        if not self.max_images_buffered or \
                self.from_kivy_queue.qsize() < self.max_images_buffered:
            self.from_kivy_queue.put(('image', (image, metadata)))
        else:
            self.increment_in_kivy_thread('frames_skipped')

    @error_guard
    def process_in_kivy_thread(self, *largs):
        """Processes messages from the server in the kivy thread.
        """
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
        """Starts the server, so that we can :meth:`record`.
        """
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
        """Stops the server and also stops recording if we were recording.
        """
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

        self.metadata_record_used = self.metadata_player
        self.player.frame_callbacks.append(self.send_image_to_client)
        self.from_kivy_queue.put(
            ('started_recording', self.metadata_record_used))

        self.complete_start()

    @error_guard
    def stop(self, *largs, join=False):
        if super(RemoteVideoRecorder, self).stop(join=join):
            self.player.frame_callbacks.remove(self.send_image_to_client)
            self.from_kivy_queue.put(('stopped_recording', None))
            self.complete_stop()

    def stop_all(self, join=False):
        super(RemoteVideoRecorder, self).stop_all(join=join)
        self.stop_server(join=join)

    def record_thread_run(self, *largs):
        pass


class RemoteRecordSettingsWidget(BoxLayout):
    """Settings widget for :class:`RemoteVideoRecorder`.
    """

    recorder: RemoteVideoRecorder = None
    """The recorder.
    """

    def __init__(self, recorder=None, **kwargs):
        if recorder is None:
            recorder = RemoteVideoRecorder()
        self.recorder = recorder
        super(RemoteRecordSettingsWidget, self).__init__(**kwargs)


Builder.load_file(join(dirname(__file__), 'server_recorder.kv'))
