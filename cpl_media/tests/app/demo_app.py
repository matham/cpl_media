"""Demo App
===========

A demo app showing the configuration and usage of the players and recorders.

"""
from os.path import join, dirname
import math

from kivy.uix.boxlayout import BoxLayout
from kivy.properties import ObjectProperty, StringProperty, NumericProperty
from kivy.lang import Builder

from base_kivy_app.app import BaseKivyApp, run_app as run_base_app,\
    report_exception_in_app
from base_kivy_app.graphics import BufferImage
from cpl_media.ptgray import PTGrayPlayer, PTGraySettingsWidget
from cpl_media.ffmpeg import FFmpegPlayer, FFmpegSettingsWidget
from cpl_media.thorcam import ThorCamPlayer, ThorCamSettingsWidget
from cpl_media.remote.client import RemoteVideoPlayer, \
    ClientPlayerSettingsWidget
from cpl_media.rtv import RTVPlayer, RTVSettingsWidget
from cpl_media.player import BasePlayer

from cpl_media.recorder import ImageFileRecorder, VideoRecorder, \
    ImageFileRecordSettingsWidget, VideoRecordSettingsWidget
from cpl_media.remote.server import RemoteVideoRecorder, \
    RemoteRecordSettingsWidget
from cpl_media.recorder import BaseRecorder
import cpl_media


__all__ = ('DemoApp', 'run_app')

Builder.load_file(join(dirname(__file__), 'demo_app.kv'))


class RootAppWidget(BoxLayout):
    pass


class RecorderMetadata(BoxLayout):

    recorder = ObjectProperty(None, rebind=True)


class DemoApp(BaseKivyApp):

    _config_props_ = ('player_name', 'display_rotation')

    _config_children_ = {
        'ffmpeg': 'ffmpeg_player', 'ptgray': 'ptgray_player',
        'thor': 'thor_player', 'network_client': 'client_player',
        'rtv': 'rtv_player', 'image_file_recorder': 'image_file_recorder',
        'video_recorder': 'video_recorder',
        'network_server': 'server_recorder',
    }

    ffmpeg_player: FFmpegPlayer = None

    ffmpeg_settings = None

    ptgray_player: PTGrayPlayer = None

    ptgray_settings = None

    thor_player: ThorCamPlayer = None

    thor_settings = None

    client_player: RemoteVideoPlayer = None

    client_player_settings = None

    rtv_player: RTVPlayer = None

    rtv_settings = None

    player: BasePlayer = ObjectProperty(None, rebind=True)

    player_name = StringProperty('ffmpeg')

    image_display: BufferImage = None

    image_file_recorder: ImageFileRecorder = None

    image_file_recorder_settings = None

    video_recorder: VideoRecorder = None

    video_recorder_settings = None

    server_recorder: RemoteVideoRecorder = None

    server_recorder_settings = None

    display_rotation = NumericProperty(0)

    def build(self):
        self.ffmpeg_settings = FFmpegSettingsWidget()
        self.ffmpeg_player = self.ffmpeg_settings.player
        self.ptgray_settings = PTGraySettingsWidget()
        self.ptgray_player = self.ptgray_settings.player
        self.thor_settings = ThorCamSettingsWidget()
        self.thor_player = self.thor_settings.player
        self.client_player_settings = ClientPlayerSettingsWidget()
        self.client_player = self.client_player_settings.player
        self.rtv_settings = RTVSettingsWidget()
        self.rtv_player = self.rtv_settings.player

        self.image_file_recorder_settings = ImageFileRecordSettingsWidget()
        self.image_file_recorder = self.image_file_recorder_settings.recorder
        self.video_recorder_settings = VideoRecordSettingsWidget()
        self.video_recorder = self.video_recorder_settings.recorder
        self.server_recorder_settings = RemoteRecordSettingsWidget()
        self.server_recorder = self.server_recorder_settings.recorder

        self.load_app_settings_from_file()
        self.apply_app_settings()

        self.player = getattr(self, '{}_player'.format(self.player_name))

        self.ffmpeg_player.display_frame = self._display_frame
        self.ptgray_player.display_frame = self._display_frame
        self.thor_player.display_frame = self._display_frame
        self.client_player.display_frame = self._display_frame

        return super(DemoApp, self).build(root=RootAppWidget())

    def _display_frame(self, image, metadata):
        if self.image_display is not None:
            self.image_display.update_img(image)

    def stop_recording(self):
        self.server_recorder.stop()
        self.video_recorder.stop()
        self.image_file_recorder.stop()

    def clean_up(self):
        super(DemoApp, self).clean_up()
        for player in (
                self.ffmpeg_player, self.thor_player, self.client_player,
                self.image_file_recorder, self.video_recorder,
                self.server_recorder, self.rtv_player, self.ptgray_player):
            if player is not None:
                player.stop_all(join=True)
        self.dump_app_settings_to_file()


def run_app():
    """The function that starts the GUI and the entry point for
    the main script.
    """
    cpl_media.error_callback = report_exception_in_app
    return run_base_app(DemoApp)
