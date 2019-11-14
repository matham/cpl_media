import time
import trio
from ..media_test_app import DemoTestApp


async def test_open_app(media_app: DemoTestApp, video_file):
    await media_app.wait_clock_frames(2)
    media_app.ffmpeg_player.play_filename = video_file
    media_app.ffmpeg_player.use_dshow = False
    media_app.player_name = 'ffmpeg'

    assert media_app.player == media_app.ffmpeg_player

    media_app._images_showed = 0
    ts = time.perf_counter()
    media_app.ffmpeg_player.play()
    while not media_app._images_showed:
        await trio.sleep(.1)
        if time.perf_counter() - ts >= 10:
            raise TimeoutError()

    media_app.ffmpeg_player.stop()
