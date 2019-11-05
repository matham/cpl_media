from kivy.uix.boxlayout import BoxLayout
from kivy.properties import ObjectProperty

from base_kivy_app.app import BaseKivyApp, run_app, report_exception_in_app
from cpl_media.ptgray import PTGrayPlayer, PTGraySettingsWidget
from cpl_media.ffmpeg import FFmpegPlayer, FFmpegSettingsWidget
from cpl_media.thorcam import ThorCamPlayer, ThorCamSettingsWidget
from cpl_media.player import BasePlayer
from cpl_media.recorder import VideoRecorder, VideoRecordSettingsWidget
import cpl_media


class RootAppWidget(BoxLayout):
    pass


class DemoApp(BaseKivyApp):

    ffmpeg_player: FFmpegPlayer = None

    ffmpeg_settings = None

    ptgray_player: PTGrayPlayer = None

    ptgray_settings = None

    thor_player: ThorCamPlayer = None

    thor_settings = None

    player: BasePlayer = ObjectProperty(None, rebind=True)

    image_display = None

    @classmethod
    def get_config_classes(cls):
        d = super(DemoApp, cls).get_config_classes()
        d['ffmpeg'] = FFmpegPlayer
        d['ptgray'] = PTGrayPlayer
        d['thor'] = ThorCamPlayer
        return d

    def get_app_config_classes(self):
        d = super(DemoApp, self).get_app_config_classes()
        d['ffmpeg'] = self.ffmpeg_player
        d['ptgray'] = self.ptgray_player
        d['thor'] = self.thor_player
        return d

    def build(self):
        self.ffmpeg_settings = FFmpegSettingsWidget()
        self.player = self.ffmpeg_player = self.ffmpeg_settings.player
        self.ptgray_settings = PTGraySettingsWidget()
        self.ptgray_player = self.ptgray_settings.player
        self.thor_settings = ThorCamSettingsWidget()
        self.thor_player = self.thor_settings.player

        self.load_app_settings_from_file()
        self.apply_app_settings()

        self.ffmpeg_player.display_frame = self._display_frame
        self.ptgray_player.display_frame = self._display_frame
        self.thor_player.display_frame = self._display_frame
        return RootAppWidget()

    def _display_frame(self, img):
        if self.image_display is not None:
            self.image_display.update_img(img)

    def clean_up(self):
        super(DemoApp, self).clean_up()
        for player in (
                self.ffmpeg_player, self.thor_player, self.ptgray_player):
            if player is not None:
                player.stop_all(join=True)
        self.dump_app_settings_to_file()


if __name__ == '__main__':
    cpl_media.error_callback = report_exception_in_app
    run_app(DemoApp)
