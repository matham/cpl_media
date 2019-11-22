import os
import pytest
import trio
import time
import warnings
from .media_test_app import DemoTestApp

warnings.filterwarnings(
    "ignore",
    message="numpy.ufunc size changed, may indicate binary incompatibility. "
            "Expected 192 from C header, got 216 from PyObject"
)

os.environ['KIVY_USE_DEFAULTCONFIG'] = '1'


@pytest.fixture()
async def media_app(request, nursery, tmp_path):
    ts0 = time.perf_counter()
    from kivy.core.window import Window
    from kivy.context import Context
    from kivy.clock import ClockBase
    from kivy.base import stopTouchApp
    from kivy.logger import LoggerHistory

    context = Context(init=False)
    context['Clock'] = ClockBase(async_lib='trio')
    context.push()

    Window.create_window()
    Window.register()
    Window.initialized = True
    Window.canvas.clear()

    from base_kivy_app.app import report_exception_in_app
    import cpl_media
    cpl_media.error_callback = report_exception_in_app

    app = DemoTestApp(
        json_config_path=str(tmp_path / 'config.yaml'),
        ini_file=str(tmp_path / 'config.ini'))

    try:
        app.set_async_lib('trio')
        nursery.start_soon(app.async_run)

        ts = time.perf_counter()
        while not app.app_has_started:
            await trio.sleep(.1)
            if time.perf_counter() - ts >= 10:
                raise TimeoutError()

        await app.wait_clock_frames(5)

        ts1 = time.perf_counter()
        yield app
        ts2 = time.perf_counter()

        stopTouchApp()

        ts = time.perf_counter()
        while not app.app_has_stopped:
            await trio.sleep(.1)
            if time.perf_counter() - ts >= 10:
                raise TimeoutError()
    finally:
        stopTouchApp()
        app.clean_up()
        for child in Window.children[:]:
            Window.remove_widget(child)

        context.pop()
        del context
        LoggerHistory.clear_history()

    ts3 = time.perf_counter()
    print(ts1 - ts0, ts2 - ts1, ts3 - ts2)


@pytest.fixture(scope='session')
def video_file(tmp_path_factory):
    from ffpyplayer.writer import MediaWriter
    from ffpyplayer.pic import Image
    fname = str(tmp_path_factory.mktemp('data') / 'test_video.avi')

    w, h = 64, 64
    size = w * h
    out_opts = {
        'pix_fmt_in': 'gray', 'width_in': w, 'height_in': h,
        'codec': 'rawvideo', 'frame_rate': (2997, 100)}

    buf = bytearray([int(x * 255 / size) for x in range(size)])
    buf2 = bytearray([0] * size)
    img = Image(plane_buffers=[buf, buf2], pix_fmt='gray', size=(w, h))

    writer = MediaWriter(fname, [out_opts])
    for i in range(20):
        writer.write_frame(img=img, pts=i / 29.97, stream=0)
    writer.close()

    return fname
