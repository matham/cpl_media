from kivy.uix.boxlayout import BoxLayout

from base_kivy_app.app import BaseKivyApp, run_app, report_exception_in_app
from cpl_media.ptgray import PTGrayPlayer, PTGraySettingsWidget
from cpl_media.ffmpeg import FFmpegPlayer, FFmpegSettingsWidget
from cpl_media.recorder import VideoRecorder, VideoRecordSettingsWidget
import cpl_media


class RootAppWidget(BoxLayout):
    pass


class DemoApp(BaseKivyApp):

    ffmpeg_player: FFmpegPlayer = None

    ptgray_player: PTGrayPlayer = None

    ptgray_settings = None

    ffmpeg_settings = None

    def build(self):
        self.ptgray_settings = PTGraySettingsWidget()
        self.ptgray_player = self.ptgray_settings.player
        self.ffmpeg_settings = FFmpegSettingsWidget()
        self.ffmpeg_player = self.ffmpeg_settings.player
        return RootAppWidget()


if __name__ == '__main__':
    cpl_media.error_callback = report_exception_in_app
    app: DemoApp = run_app(DemoApp)
    if app is not None:
        if app.ffmpeg_player is not None:
            app.ffmpeg_player.stop(join=True)
        if app.ptgray_player is not None:
            app.ptgray_player.stop(join=True)
            app.ptgray_player.stop_config(join=True)


