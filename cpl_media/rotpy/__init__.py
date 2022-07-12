"""Flir based player
======================

This player can play Flir cameras using :mod:`rotpy`.
"""

from typing import List, Union, Dict, Optional, Tuple
import ipaddress
from threading import Thread
from collections import defaultdict
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
from kivy.event import EventDispatcher
from kivy.lang import Builder

from cpl_media.player import BasePlayer, VideoMetadata
from cpl_media import error_guard

try:
    from rotpy.camera import Camera, CameraList
    from rotpy.system import SpinSystem, SpinnakerAPIException
    from rotpy.system_nodes import SystemNodes
    from rotpy.camera_nodes import CameraNodes
    from rotpy.image import Image as RotPyImage
    from rotpy.node import SpinIntNode, SpinStrNode, SpinFloatNode, \
        SpinCommandNode, SpinBoolNode, SpinEnumDefNode, SpinValueNode
except ImportError as e:
    Logger.debug('cpl_media: Could not import RotPy: {}'.format(e))
    Camera = CameraList = SpinSystem = SystemNodes = CameraNodes = \
        RotPyImage = SpinIntNode = SpinStrNode = SpinFloatNode = \
        SpinCommandNode = SpinBoolNode = SpinEnumDefNode = SpinValueNode = None

__all__ = ('FlirPlayer', 'FlirSettingsWidget')


class CameraSetting(EventDispatcher):

    __events__ = ('on_setting_update', )

    player: 'FlirPlayer' = None

    node: SpinValueNode = None

    available: bool = BooleanProperty(False)

    readable: bool = BooleanProperty(False)

    writable: bool = BooleanProperty(False)

    description: str = StringProperty('')

    display_name: str = StringProperty('')

    name: str = ''

    node_id = ()

    def __init__(self, node=None, node_id=(), player=None, **kwargs):
        self.node = node
        self.node_id = node_id
        self.player = player
        super().__init__(**kwargs)

    def on_setting_update(self):
        pass

    def _set_initial_setup_data(self, data):
        self.available = data['metadata']['available']
        self.readable = data['metadata']['readable']
        self.writable = data['metadata']['writable']
        self.description = data['metadata']['description']
        self.display_name = data['metadata']['display_name']
        self.name = data['metadata']['name']

    def _post_initial_setup(self, data=None, failed=False):
        if not failed:
            self._set_initial_setup_data(data)

            self.dispatch('on_setting_update')
        self.player.end_config_item(self)

    def _get_initial_setup_data(self):
        node = self.node
        available = node.is_available()
        metadata = {
            'available': available,
            'readable': node.is_readable(),
            'writable': node.is_writable(),
            'description': node.get_short_description() if available else '',
            'display_name': node.get_display_name() if available else '',
            'name': node.get_name() if available else '',
        }
        return {'metadata': metadata}

    def _initial_setup(self):
        try:
            values = self._get_initial_setup_data()
        except BaseException:
            self.player.call_in_kivy_thread(
                self._post_initial_setup, failed=True)
            raise

        self.player.call_in_kivy_thread(
            self._post_initial_setup, data=values)

    def initial_setup(self):
        self.player.ask_setting_config(self, self._initial_setup)

    def refresh_value(self):
        pass


class SimpleValueSetting(CameraSetting):

    value = None

    def _get_node_value(self):
        return self.node.get_node_value()

    def _set_node_value(self, value):
        self.node.set_node_value(value, verify=True)

    def _post_refresh_value(
            self, data=None, readable=None, writable=None, failed=False):
        if not failed:
            self.readable = readable
            self.writable = writable
            if readable:
                self.value = data
        # always make sure data presented is upto date in case user broke it
        self.dispatch('on_setting_update')
        self.player.end_config_item(self)

    def _refresh_value(self):
        try:
            readable = self.node.is_readable()
            writable = self.node.is_writable()
            value = None
            if readable:
                value = self._get_node_value()
        except BaseException:
            self.player.call_in_kivy_thread(
                self._post_refresh_value, failed=True)
            raise
        self.player.call_in_kivy_thread(
            self._post_refresh_value, data=value, readable=readable,
            writable=writable
        )

    def refresh_value(self):
        self.player.ask_setting_config(self, self._refresh_value)

    def _post_set_value(self, data=None, failed=False):
        if not failed:
            self.value = data
            self.dispatch('on_setting_update')
        self.player.end_config_item(self)

    def _set_value(self, value):
        try:
            self._set_node_value(value)
        finally:
            try:
                value = self._get_node_value()
            except BaseException:
                self.player.call_in_kivy_thread(
                    self._post_set_value, failed=True)
                raise
            self.player.call_in_kivy_thread(self._post_set_value, data=value)

    def set_value(self, value):
        if self.writable:
            self.player.ask_setting_config(self, self._set_value, value)
        else:
            self.dispatch('on_setting_update')

    def on_setting_update(self):
        self.property('value').dispatch(self)


class NumericSetting(SimpleValueSetting):

    node: Union[SpinIntNode, SpinFloatNode] = None

    value = NumericProperty(0)

    min_val = NumericProperty(0)

    max_val = NumericProperty(0)

    representation: str = StringProperty('')

    unit: str = StringProperty('')

    increment: Union[float, int] = NumericProperty(1)

    has_increment: bool = BooleanProperty(False)

    def _set_initial_setup_data(self, data):
        super()._set_initial_setup_data(data)
        self.min_val = data['min']
        self.max_val = data['max']
        self.has_increment = data['has_inc']
        self.increment = data['inc']
        self.representation = data['rep']
        self.unit = data['unit']
        self.value = data['value']

    def _get_initial_setup_data(self):
        data = super()._get_initial_setup_data()
        node = self.node
        readable = data['metadata']['readable']
        has_inc = readable and 'noIncrement' != node.get_increment_mode()

        values = {
            'min': node.get_min_value() if readable else 0,
            'max': node.get_max_value() if readable else 0,
            'has_inc': has_inc,
            'inc': node.get_increment() if has_inc else 0,
            'rep': node.get_representation() if readable else '',
            'unit': node.get_unit() if readable else '',
            'value': self._get_node_value() if readable else 0,
            **data,
        }
        return values


class IntSetting(NumericSetting):

    node: SpinIntNode = None


class FloatSetting(NumericSetting):

    node: SpinFloatNode = None


class BoolSetting(SimpleValueSetting):

    node: SpinBoolNode = None

    value = BooleanProperty(False)

    def _set_initial_setup_data(self, data):
        super()._set_initial_setup_data(data)
        self.value = data['value']

    def _get_initial_setup_data(self):
        data = super()._get_initial_setup_data()
        readable = data['metadata']['readable']

        values = {
            'value': self._get_node_value() if readable else False,
            **data,
        }
        return values


class StrSetting(SimpleValueSetting):

    node: SpinStrNode = None

    value = StringProperty('')

    max_len: int = 0

    def _set_initial_setup_data(self, data):
        super()._set_initial_setup_data(data)
        self.value = data['value']
        self.max_len = data['max_len']

    def _get_initial_setup_data(self):
        data = super()._get_initial_setup_data()
        readable = data['metadata']['readable']
        node = self.node

        values = {
            'value': self._get_node_value() if readable else '',
            'max_len': node.get_max_len() if readable else 0,
            **data,
        }
        return values


class EnumSetting(SimpleValueSetting):

    node: SpinEnumDefNode = None

    value = StringProperty('')

    values: List[str] = ListProperty([])

    descriptions = {}

    display_names = {}

    def _get_node_value(self):
        return self.node.get_node_value().get_enum_name()

    def _set_node_value(self, value):
        self.node.set_node_value_from_str(value, verify=True)

    def _set_initial_setup_data(self, data):
        super()._set_initial_setup_data(data)
        self.value = data['value']
        self.values = data['values']
        self.descriptions = data['descriptions']
        self.display_names = data['display_names']

    def _get_initial_setup_data(self):
        data = super()._get_initial_setup_data()
        readable = data['metadata']['readable']
        node = self.node
        opts = []
        descriptions = {}
        display_names = {}
        if readable:
            opts = [entry.get_enum_name() for entry in node.get_entries()]
            descriptions = {
                entry.get_enum_name(): entry.get_short_description()
                for entry in node.get_entries()
            }
            display_names = {
                entry.get_enum_name(): entry.get_display_name()
                for entry in node.get_entries()
            }

        values = {
            'value': self._get_node_value() if readable else '',
            'values': opts,
            'descriptions': descriptions,
            'display_names': display_names,
            **data,
        }
        return values


class CommandSetting(CameraSetting):

    node: SpinCommandNode = None

    timeout: float = 30

    executing: bool = BooleanProperty(False)

    def _post_execute(self, failed=False):
        self.executing = False
        if not failed:
            self.dispatch('on_setting_update')
        self.player.end_config_item(self)

    def _execute(self):
        node = self.node
        try:
            node.execute_node(verify=True)
            ts = time.monotonic()
            timeout = self.timeout
            while time.monotonic() - ts < timeout and \
                    not node.is_done():
                time.sleep(.2)

            if not node.is_done():
                raise TimeoutError('Timed out waiting for command to finish')
        except BaseException:
            self.player.call_in_kivy_thread(self._post_execute, failed=True)
            raise
        self.player.call_in_kivy_thread(self._post_execute)

    def execute(self):
        if self.available:
            self.executing = True
            self.player.ask_setting_config(self, self._execute)
        else:
            self.dispatch('on_setting_update')

    def on_setting_update(self):
        self.property('executing').dispatch(self)


class FlirPlayer(BasePlayer):

    _config_props_ = ('serial', 'saved_nodes')

    is_available = BooleanProperty(Camera is not None)
    """Whether RotPy is available to play."""

    serial = StringProperty('')
    '''The serial number of the camera to open.
    '''

    serials = ListProperty([])
    """The serials of all the cams available.
    """

    saved_nodes = {}

    _saved_nodes = [
        'PixelFormat', 'Height', 'Width',
        'OffsetX', 'OffsetY', 'ReverseX', 'ReverseY',
        'AcquisitionFrameRateEnable', 'AcquisitionFrameRate',
        'BinningSelector',
        'BinningHorizontalMode', 'BinningHorizontal', 'BinningVerticalMode',
        'BinningVertical', 'DecimationSelector', 'DecimationHorizontalMode',
        'DecimationHorizontal', 'DecimationVerticalMode',
        'DecimationVertical', 'ExposureActiveMode', 'ExposureAuto',
        'ExposureMode', 'ExposureTimeMode', 'ExposureTimeSelector',
        'ExposureTime', 'Gain', 'GainAuto', 'GainAutoBalance',
        'GainSelector', 'RgbTransformLightSource', 'Saturation',
        'SaturationEnable', 'Gamma', 'GammaEnable', 'Sharpening',
        'SharpeningAuto', 'SharpeningEnable', 'SharpeningThreshold',
        'WhiteClip', 'WhiteClipSelector', 'AutoExposureLightingMode',
        'AutoExposureMeteringMode', 'AutoExposureTargetGreyValue',
        'AutoExposureTargetGreyValueAuto', 'BalanceRatioSelector',
        'BalanceRatio', 'BalanceWhiteAuto', 'BalanceWhiteAutoDamping',
        'BalanceWhiteAutoLowerLimit', 'BalanceWhiteAutoProfile',
        'BlackLevel', 'BlackLevelAuto', 'BlackLevelAutoBalance',
        'ImageCompressionBitrate', 'ImageCompressionJPEGFormatOption',
        'ImageCompressionMode', 'ImageCompressionQuality',
        'ImageCompressionRateOption', 'PacketResendRequestCount',
        'PixelColorFilter', 'PixelDynamicRangeMax', 'PixelDynamicRangeMin',
        'AdcBitDepth', 'AutoExposureControlPriority',
        'AutoExposureExposureTimeLowerLimit',
        'AutoExposureExposureTimeUpperLimit', 'AutoExposureGainLowerLimit',
        'AutoExposureGainUpperLimit', 'CompressionRatio',
        'CompressionSaturationPriority',
    ]

    _saved_nodes_set = set(_saved_nodes)

    config_active: bool = BooleanProperty(False)

    active_settings_count: Dict[CameraSetting, int] = {}

    total_active_count: int = 0

    camera_settings_names: Dict[str, List[str]] = {
        'Camera': [
            'PixelFormat', 'Height', 'HeightMax', 'Width', 'WidthMax',
            'OffsetX', 'OffsetY', 'ReverseX', 'ReverseY',
            'AcquisitionFrameRateEnable', 'AcquisitionFrameRate',
            'AcquisitionResultingFrameRate', 'BinningSelector',
            'BinningHorizontalMode', 'BinningHorizontal', 'BinningVerticalMode',
            'BinningVertical', 'DecimationSelector', 'DecimationHorizontalMode',
            'DecimationHorizontal', 'DecimationVerticalMode',
            'DecimationVertical', 'ExposureActiveMode', 'ExposureAuto',
            'ExposureMode', 'ExposureTimeMode', 'ExposureTimeSelector',
            'ExposureTime', 'Gain', 'GainAuto', 'GainAutoBalance',
            'GainSelector', 'RgbTransformLightSource', 'Saturation',
            'SaturationEnable', 'Gamma', 'GammaEnable', 'Sharpening',
            'SharpeningAuto', 'SharpeningEnable', 'SharpeningThreshold',
            'WhiteClip', 'WhiteClipSelector', 'AutoExposureLightingMode',
            'AutoExposureMeteringMode', 'AutoExposureTargetGreyValue',
            'AutoExposureTargetGreyValueAuto', 'BalanceRatioSelector',
            'BalanceRatio', 'BalanceWhiteAuto', 'BalanceWhiteAutoDamping',
            'BalanceWhiteAutoLowerLimit', 'BalanceWhiteAutoProfile',
            'BlackLevel', 'BlackLevelAuto', 'BlackLevelAutoBalance',
            'DeviceFeaturePersistenceStart', 'DeviceFeaturePersistenceEnd',
            'DeviceIndicatorMode', 'DeviceSerialNumber', 'DeviceTemperature',
            'DeviceTemperatureSelector', 'DeviceReset', 'FactoryReset',
            'ImageCompressionBitrate', 'ImageCompressionJPEGFormatOption',
            'ImageCompressionMode', 'ImageCompressionQuality',
            'ImageCompressionRateOption', 'PacketResendRequestCount',
            'PixelColorFilter', 'PixelDynamicRangeMax', 'PixelDynamicRangeMin',
            'AcquisitionMode', 'AdcBitDepth', 'AutoExposureControlPriority',
            'AutoExposureExposureTimeLowerLimit',
            'AutoExposureExposureTimeUpperLimit', 'AutoExposureGainLowerLimit',
            'AutoExposureGainUpperLimit', 'CompressionRatio',
            'CompressionSaturationPriority', 'DeviceFamilyName',
            'DeviceFirmwareVersion', 'DeviceGenCPVersionMajor',
            'DeviceGenCPVersionMinor', 'DeviceID',
            'DeviceLinkCurrentThroughput', 'DeviceLinkHeartbeatMode',
            'DeviceLinkHeartbeatTimeout', 'DeviceManufacturerInfo',
            'DeviceMaxThroughput', 'DeviceModelName', 'DeviceType',
            'DeviceUptime', 'DeviceUserID', 'DeviceVendorName', 'DeviceVersion',
            'GevCurrentDefaultGateway', 'GevCurrentIPAddress',
            'GevCurrentIPConfigurationDHCP', 'GevCurrentIPConfigurationLLA',
            'GevCurrentIPConfigurationPersistentIP',
            'GevCurrentPhysicalLinkConfiguration', 'GevCurrentSubnetMask',
            'GevDiscoveryAckDelay', 'GevGVCPHeartbeatDisable',
            'GevGVCPPendingTimeout', 'GevHeartbeatTimeout',
            'GevIPConfigurationStatus', 'GevMACAddress',
            'GevPersistentIPAddress', 'GevPersistentSubnetMask',
            'GevSCPSPacketSize', 'PixelSize', 'SensorDescription',
            'SensorHeight', 'SensorShutterMode', 'SensorWidth'
        ],
        'DeviceTL': [
            'GevDeviceIsWrongSubnet', 'GevDeviceDiscoverMaximumPacketSize',
            'GevDeviceMaximumPacketSize', 'GevDeviceAutoForceIP',
            'GevDeviceForceGateway', 'GevDeviceForceIP',
            'GevDeviceForceIPAddress', 'GevDeviceForceSubnetMask',
            'GevDeviceGateway', 'GevDeviceIPAddress', 'GevDeviceMACAddress',
            'GevDeviceSubnetMask', 'DeviceDisplayName', 'DeviceID',
            'DeviceModelName', 'DeviceSerialNumber', 'DeviceInstanceId',
            'DeviceLinkSpeed', 'DeviceLocation', 'DevicePortId', 'DeviceType',
            'DeviceVendorName', 'DeviceVersion', 'GevVersionMajor',
            'GevVersionMinor', 'GevDeviceMaximumRetryCount'
        ],
        'StreamTL': [
            'StreamPacketResendEnable', 'StreamPacketResendMaxRequests',
            'StreamPacketResendReceivedPacketCount',
            'StreamPacketResendRequestCount',
            'StreamPacketResendRequestSuccessCount',
            'StreamPacketResendRequestedPacketCount',
            'StreamDeliveredFrameCount', 'StreamDroppedFrameCount',
            'StreamIncompleteFrameCount', 'StreamInputBufferCount',
            'StreamLostFrameCount', 'StreamMissedPacketCount',
            'StreamOutputBufferCount', 'StreamReceivedPacketCount',
            'StreamStartedFrameCount', 'StreamType'
        ],
        'System': [
            'EnumerateGEVInterfaces', 'EnumerateGen2Cameras',
            'EnumerateUSBInterfaces', 'InterfaceUpdateList',
            'InterfaceSelector', 'InterfaceDisplayName', 'InterfaceID',
            'GevInterfaceDefaultGateway', 'GevInterfaceDefaultIPAddress',
            'GevInterfaceDefaultSubnetMask', 'GevInterfaceMACAddress'
        ],
    }

    camera_settings: Dict[str, CameraSetting] = {}

    available_camera_settings: List[str] = ListProperty([])

    supports_ip: bool = BooleanProperty(False)

    bad_subnet: bool = BooleanProperty(False)

    camera_inited: bool = BooleanProperty(False)

    config_thread = None
    """The configuration thread.
    """

    config_queue: Queue = None
    """The configuration queue that the threads uses to get messages.
    """

    initial_play_queue: Queue = None

    _camera: Optional[Camera] = None

    ffmpeg_pix_map = {
        'Mono8': 'gray8', 'Mono16': 'gray16le',
        'YUV422Packed': 'uyvy422',
        'YUV444Packed': 'yuv444p', 'YCbCr8_CbYCr': 'yuv444p', 'RGB8': 'rgb24',
        'RGB565p': 'rgb565le', 'RGBa8': 'rgba', 'BGR8': 'bgr24',
        'RGB8Packed': 'bgr24', 'BGR8Packed': 'bgr24',
        'BGR565p': 'bgr565le', 'BGRa8': 'bgra',
        # not supported by ffmpeg swscale
        # 'YCbCr411_8_CbYYCrYY': 'uyyvyy411', 'YUV411_8_UYYVYY': 'uyyvyy411',
        # 'YUV411Packed': 'uyyvyy411',
        'YCbCr422_8_CbYCrY': 'uyvy422', 'YUV422_8_UYVY': 'uyvy422'
    }
    """Pixel formats supported by the camera and their :mod:`ffpyplayer`
    equivalent.

    https://www.emva.org/wp-content/uploads/GenICamPixelFormatValues.pdf
    http://softwareservices.flir.com/BFS-U3-89S6/latest/Model/public/\
ImageFormatControl.html
    """

    def __init__(self, open_thread=True, **kwargs):
        super().__init__(**kwargs)
        self.active_settings_count = defaultdict(int)

        if Camera is not None and open_thread:
            self.start_config()

        self.fbind('serial', self.update_serial)
        self.fbind('serial', self._update_summary)
        self._update_summary()
        self.can_play = False
        self.saved_nodes = {}

    def _update_summary(self, *largs):
        name = str(self.serial)
        self.player_summery = 'FLIR "{}"'.format(name)

    def _update_setting_callback(
            self, nodes_name, name, setting: SimpleValueSetting, *args):
        self.saved_nodes[(nodes_name, name)] = setting.value

    @error_guard
    def start_config_item(self, item):
        self.active_settings_count[item] += 1
        self.total_active_count += 1
        self.config_active = True

    @error_guard
    def end_config_item(self, item):
        count = self.active_settings_count
        count[item] -= 1
        self.total_active_count -= 1

        assert count[item] >= 0
        assert self.total_active_count >= 0
        if not count[item]:
            del count[item]

        if not self.total_active_count:
            self.config_active = False

    @error_guard
    def ask_setting_config(self, setting, f, *args, **kwargs):
        self.start_config_item(setting)
        self.config_queue.put(('setting', (f, args, kwargs)))

    @error_guard
    def update_serial(self, *args):
        self.can_play = False
        self.ask_config('serial')
        self.camera_settings = {}
        self.available_camera_settings = []
        self.camera_inited = False

    def _do_update_serial(self, system: SpinSystem, serial):
        settings = {}
        available_settings = []
        success = False
        supports_ip = False
        bad_subnet = False
        camera: Camera = self._camera
        saved_nodes = self._saved_nodes_set

        nodes_cls_map = {
            SpinIntNode: IntSetting,
            SpinFloatNode: FloatSetting,
            SpinBoolNode: BoolSetting,
            SpinStrNode: StrSetting,
            SpinCommandNode: CommandSetting,
            SpinEnumDefNode: EnumSetting,
        }

        try:
            if camera is not None:
                self._camera = None
                camera.deinit_cam()
                camera.release()
                camera = None

            if serial:
                cameras: CameraList = CameraList.create_from_system(
                    system, True, True)
                try:
                    camera = cameras.create_camera_by_serial(serial)
                except ValueError:
                    return None

                nodes_map = {
                    'Camera': camera.camera_nodes,
                    'DeviceTL': camera.tl_dev_nodes,
                    'StreamTL': camera.tl_stream_nodes,
                    'System': system.system_nodes,
                }

                self._camera = camera
                supports_ip = \
                    camera.tl_dev_nodes.GevDeviceIsWrongSubnet.is_readable()
                if supports_ip:
                    bad_subnet = camera.tl_dev_nodes.GevDeviceIsWrongSubnet.\
                        get_node_value()
            else:
                nodes_map = {'System': system.system_nodes}

            for nodes_name, names in self.camera_settings_names.items():
                if nodes_name not in nodes_map:
                    continue

                for name in names:
                    node = getattr(nodes_map[nodes_name], name)
                    setting = nodes_cls_map[node.__class__](
                        node=node, node_id=(nodes_name, name), player=self)
                    display_name = f'{nodes_name}.{name}'
                    settings[display_name] = setting

                    if node.is_available():
                        available_settings.append(display_name)

                    if nodes_name == 'Camera' and name in saved_nodes:
                        setting.fbind(
                            'on_setting_update',
                            self._update_setting_callback, nodes_name, name,
                            setting
                        )

            success = True
        finally:
            self.call_in_kivy_thread(
                self._post_update_serial, settings, available_settings, success,
                supports_ip, bad_subnet, serial)
        return camera

    @error_guard
    def _post_update_serial(
            self, settings, available_settings, success, supports_ip,
            bad_subnet, serial):
        self.camera_settings = settings
        self.available_camera_settings = available_settings
        self.can_play = success and (not supports_ip or not bad_subnet) \
            and serial
        self.supports_ip = supports_ip
        self.bad_subnet = bad_subnet

        try:
            for name in available_settings:
                settings[name].initial_setup()
        finally:
            self.end_config_item('serial')

    @error_guard
    def init_camera(self):
        if self.camera_inited:
            raise TypeError('Camera already init')
        self.can_play = False
        self.ask_config('init_cam')

    def _create_available_settings(self):
        available_settings = []
        saved_nodes = {}
        saved_values = self.saved_nodes
        saved_keys = {('Camera', v) for v in self._saved_nodes_set}

        for name, setting in self.camera_settings.items():
            node = setting.node
            if node.is_available():
                available_settings.append(name)

            key = (*name.split('.'), )
            if key not in saved_keys or not node.is_readable():
                continue

            try:
                if node.is_writable() and key in saved_values:
                    setting._set_node_value(saved_values[key])
            except SpinnakerAPIException:
                continue
            saved_nodes[key] = setting._get_node_value()

        self.saved_nodes = saved_nodes
        return available_settings

    def _do_init_camera(self, camera: Camera):
        available_settings = []
        inited = False
        try:
            camera.init_cam()
            inited = True
            available_settings = self._create_available_settings()
        finally:
            self.call_in_kivy_thread(
                self._post_init_camera, self.camera_settings,
                available_settings, inited)

    @error_guard
    def _post_init_camera(self, settings, available_settings, inited):
        self.available_camera_settings = available_settings
        self.camera_inited = inited
        self.can_play = inited
        self.property('camera_inited').dispatch(self)

        try:
            for name in available_settings:
                settings[name].initial_setup()
        finally:
            self.end_config_item('init_cam')

    def _do_init_play(self, camera: Camera):
        available_settings = []
        inited = False
        failed = True
        metadata = None
        try:
            camera.init_cam()
            inited = True
            available_settings = self._create_available_settings()

            values = []
            for name in (
                    'Height', 'Width', 'AcquisitionResultingFrameRate',
                    'AcquisitionFrameRate'):
                node = getattr(camera.camera_nodes, name)
                if node.is_readable():
                    values.append(node.get_node_value())
                else:
                    values.append(None)

            h, w, rate, rate2 = values
            rate = rate or rate2 or 30.
            fmt = None
            node = camera.camera_nodes.PixelFormat
            if node.is_readable():
                fmt = node.get_node_value().get_enum_name()
            if not h or not w or not fmt or not rate:
                raise ValueError(
                    f'Unable to set play metadata. Got Rate: {rate}, '
                    f'Width: {w}, Height: {h}, Pixel format: {fmt}')
            ff_fmt = self.ffmpeg_pix_map.get(fmt)
            if not ff_fmt:
                raise ValueError(f'Pixel format {fmt} is not supported')

            metadata = VideoMetadata(ff_fmt, w, h, rate)
            failed = False
        finally:
            self.call_in_kivy_thread(
                self._post_init_play, self.camera_settings,
                available_settings, inited, camera, failed, metadata)

    @error_guard
    def _post_init_play(
            self, settings, available_settings, inited, camera, failed,
            metadata):
        self.available_camera_settings = available_settings
        self.camera_inited = inited

        # make sure to stop if init failed
        if failed:
            self.stop()
        elif self.initial_play_queue is not None:
            self.metadata_play = metadata
            self.metadata_play_used = metadata
            self.initial_play_queue.put(('camera', camera))

        try:
            for name in available_settings:
                settings[name].initial_setup()
        finally:
            self.end_config_item('init_play_cam')

    @error_guard
    def update_serials(self, *args):
        self.ask_config('serials')

    def _do_update_serials(self, system: SpinSystem):
        serials = []

        try:
            cameras: CameraList = CameraList.create_from_system(system)
            for i in range(cameras.get_size()):
                camera: Camera = cameras.create_camera_by_index(i)
                if camera.tl_dev_nodes.DeviceSerialNumber.is_readable():
                    serial = \
                        camera.tl_dev_nodes.DeviceSerialNumber.get_node_value()
                    serials.append(serial)
        finally:
            self.call_in_kivy_thread(self._post_update_serials, serials)

    @error_guard
    def _post_update_serials(self, serials):
        if self.serial not in serials:
            if not self.serial:
                # update things
                self.property('serial').dispatch(self)
            else:
                self.serial = ''
        self.serials = serials
        self.end_config_item('serials')

    @error_guard
    def force_ip(self, *args):
        if not self.supports_ip or self._camera is None:
            raise TypeError('IP not supported for camera')
        self.ask_config('force_ip')

    def _do_force_ip(self, camera: Camera):
        success = False
        bad_subnet = True
        try:
            node = camera.tl_dev_nodes.GevDeviceAutoForceIP
            node.execute_node(verify=True)
            ts = time.monotonic()
            while time.monotonic() - ts < 30 and not node.is_done():
                time.sleep(.2)
            if not node.is_done():
                raise TimeoutError('Timed out waiting for command to finish')

            if camera.tl_dev_nodes.GevDeviceIsWrongSubnet.is_readable():
                bad_subnet = \
                    camera.tl_dev_nodes.GevDeviceIsWrongSubnet.get_node_value()
            success = True
        finally:
            self.call_in_kivy_thread(self._post_force_ip, success, bad_subnet)

    @error_guard
    def _post_force_ip(self, success, bad_subnet):
        self.can_play = success and not bad_subnet
        self.bad_subnet = bad_subnet
        self.end_config_item('force_ip')

    @error_guard
    def start_config(self, *largs):
        """Called by ``__init__`` to start the configuration thread.
        """
        self.config_queue = Queue()
        thread = self.config_thread = Thread(
            target=self.config_thread_run, name='Config thread')
        thread.start()

    @error_guard
    def stop_config(self, *largs, join=False):
        """Stops the configuration thread.
        """
        self.ask_config('eof')
        if join and self.config_thread is not None:
            self.config_thread.join()
            self.config_thread = None

    @error_guard
    def ask_config(self, item, *args, **kwargs):
        """Asks to add something for the config thread to do.

        :param item: The request to send.
        """
        queue = self.config_queue
        if queue is not None:
            if item != 'eof':
                self.start_config_item(item)
            queue.put((item, (args, kwargs)))

    def config_thread_run(self):
        """The function run by the configuration thread.
        """
        cmd_queue = self.config_queue

        system = SpinSystem()
        camera: Optional[Camera] = None

        while True:
            item, value = cmd_queue.get()
            try:
                if item == 'eof':
                    if camera is not None:
                        if self.camera_inited:
                            try:
                                camera.deinit_cam()
                            except SpinnakerAPIException:
                                pass
                        camera.release()
                    camera = self._camera = system = None
                    return

                if item == 'serial':
                    if camera is not None and self.camera_inited:
                        try:
                            camera.deinit_cam()
                        except SpinnakerAPIException:
                            pass
                    camera = None

                    serial = self.serial
                    camera = self._do_update_serial(system, serial)
                    if camera is None and serial:
                        raise TypeError(f'Failed to create camera for {serial}')
                elif item == 'serials':
                    self._do_update_serials(system)
                elif item == 'setting':
                    f, args, kwargs = value
                    f(*args, **kwargs)
                elif item == 'init_cam':
                    if camera is None:
                        self.call_in_kivy_thread(
                            self.end_config_item, 'init_cam')
                        raise TypeError('No camera')
                    self._do_init_camera(camera)
                elif item == 'init_play_cam':
                    if camera is None:
                        self.call_in_kivy_thread(
                            self.end_config_item, 'init_play_cam')
                        raise TypeError('No camera')
                    self._do_init_play(camera)
                elif item == 'force_ip':
                    if camera is None:
                        self.call_in_kivy_thread(
                            self.end_config_item, 'force_ip')
                        raise TypeError('No camera')
                    self._do_force_ip(camera)
            except Exception as err:
                self.exception(err)

    @error_guard
    def play(self):
        if not super().play():
            return

        # if we're here, we are not currently initing and we have a serial
        self.ask_config('init_play_cam')

    @error_guard
    def stop(self, *args, **kwargs):
        if self.initial_play_queue is not None:
            self.initial_play_queue.put(('eof', None))
        return super().stop(*args, **kwargs)

    def _start_play_thread(self):
        self.initial_play_queue = Queue()
        super()._start_play_thread()

    def play_thread_run(self):
        queue = self.initial_play_queue
        process_frame = self.process_frame
        ffmpeg_fmts = self.ffmpeg_pix_map
        camera: Optional[Camera] = None
        acquiring = False
        image = None

        try:
            while True:
                item, value = queue.get()
                if item == 'eof':
                    return
                if item == 'camera':
                    camera = value
                    break

            camera.begin_acquisition()
            acquiring = True
            while self.play_state != 'stopping':
                try:
                    if camera.get_next_image(.2) is not None:
                        break
                except Exception as err:
                    self.exception(err)
                    continue

            count = 0
            ivl_start = clock()
            self.setattr_in_kivy_thread('ts_play', ivl_start)
            Clock.schedule_once(self.complete_start)

            while self.play_state != 'stopping':
                ivl_end = clock()
                if ivl_end - ivl_start >= 1.:
                    real_rate = count / (ivl_end - ivl_start)
                    self.setattr_in_kivy_thread('real_rate', real_rate)
                    count = 0
                    ivl_start = ivl_end

                try:
                    image: Optional[RotPyImage] = camera.get_next_image(.2)
                    if image is None:
                        continue
                except Exception as err:
                    self.exception(err)
                    if image is not None:
                        image.release()
                    continue

                count += 1
                self.increment_in_kivy_thread('frames_played')

                pix_fmt = image.get_pix_fmt()
                w = image.get_width()
                h = image.get_height()
                t = image.get_frame_timestamp() / 1e9
                if pix_fmt not in ffmpeg_fmts:
                    image.release()
                    raise Exception(f'Pixel format {pix_fmt} cannot be used')

                ff_fmt = ffmpeg_fmts[pix_fmt]
                buff = image.get_image_data()
                if ff_fmt == 'yuv444p':
                    img = Image(
                        plane_buffers=[buff[1::3], buff[0::3], buff[2::3]],
                        pix_fmt=ff_fmt, size=(w, h))
                else:
                    img = Image(
                        plane_buffers=[buff], pix_fmt=ff_fmt, size=(w, h))

                image.release()
                process_frame(img, {'t': t})
        except Exception as err:
            self.exception(err)
        finally:
            try:
                if camera is not None and acquiring:
                    camera.end_acquisition()
            finally:
                Clock.schedule_once(self.complete_stop)

    def stop_all(self, join=False):
        super().stop_all(join=join)
        self.stop_config(join=join)

    def get_valid_enum_values(self, setting: CameraSetting, values):
        if setting.name == 'PixelFormat':
            fmts = self.ffmpeg_pix_map
            return [v for v in values if v in fmts]
        return values


class FlirSettingsWidget(BoxLayout):
    """Settings widget for :class:`FlirPlayer`.
    """

    player: FlirPlayer = ObjectProperty(None)
    """The player.
    """

    setting: Optional[CameraSetting] = ObjectProperty(None, allownone=True)

    selected_name = StringProperty('')

    current_widget = None

    widget_cls_cache = {}

    setting_parent: ObjectProperty(None)

    def __init__(self, player=None, **kwargs):
        if player is None:
            player = FlirPlayer()
        self.player = player
        super().__init__(**kwargs)
        self.widget_cls_cache = {}

        def setting_callback(*args):
            self.update_setting('')
        player.fbind('available_camera_settings', setting_callback)
        player.fbind('serial', setting_callback)

    def update_setting(self, name):
        if self.current_widget is not None:
            self.current_widget.setting = None
            self.current_widget.parent.remove_widget(self.current_widget)
            self.current_widget = None
        self.setting = None

        self.selected_name = name
        if not name:
            return

        setting = self.player.camera_settings[name]
        widget = self.get_setting_widget(setting)
        self.setting_parent.add_widget(widget)
        self.current_widget = widget
        self.setting = setting
        setting.refresh_value()

    def get_setting_widget(self, setting):
        representation = None
        if isinstance(setting, (IntSetting, FloatSetting)):
            if setting.representation in (
                    'MACAddress', 'IPV4Address', 'HexNumber'):
                representation = setting.representation
                cls = FlirTextSettingWidget
            else:
                cls = FlirNumericSettingWidget
        elif isinstance(setting, StrSetting):
            cls = FlirTextSettingWidget
            representation = ''
        elif isinstance(setting, BoolSetting):
            cls = FlirBoolSettingWidget
        elif isinstance(setting, EnumSetting):
            cls = FlirEnumSettingWidget
        elif isinstance(setting, CommandSetting):
            cls = FlirCommandSettingWidget
        else:
            assert False

        cache = self.widget_cls_cache
        if cls in cache:
            widget = cache[cls]
            assert widget.parent is None
        else:
            widget = cache[cls] = cls()
        if representation is not None:
            widget.representation = representation
        widget.setting = setting
        widget.player = self.player

        return widget


class FlirSettingWidget(BoxLayout):

    setting: Optional[CameraSetting] = ObjectProperty(
        None, allownone=True, rebind=True)

    player: FlirPlayer = ObjectProperty(None, allownone=True, rebind=True)

    populated = BooleanProperty(False)


class FlirTextSettingWidget(FlirSettingWidget):

    representation = StringProperty('')

    @error_guard
    def set_value(self, text):
        rep = self.representation
        try:
            if rep == 'MACAddress':
                value = int(text, 0)
            elif rep == 'HexNumber':
                value = int(text, 0)
            elif rep == 'IPV4Address':
                value = int(ipaddress.ip_address(text))
            elif not rep:
                value = text
            else:
                assert False

            self.setting.set_value(value)
        except BaseException:
            self.setting.property('value').dispatch(self.setting)
            raise

    @error_guard
    def get_value(self, value):
        rep = self.representation
        try:
            if not rep:
                return value
            if rep == 'MACAddress':
                return hex(value)
            elif rep == 'HexNumber':
                return hex(value)
            elif rep == 'IPV4Address':
                return str(ipaddress.ip_address(value))
            else:
                assert False
        except BaseException:
            self.setting.property('value').dispatch(self.setting)
            raise


class FlirNumericSettingWidget(FlirSettingWidget):

    _filter_cls = {IntSetting: 'int', FloatSetting: 'float'}

    def get_input_filter(self, setting):
        return self._filter_cls.get(setting.__class__)

    @error_guard
    def set_value(self, text):
        cls_name = self._filter_cls[self.setting.__class__]
        try:
            if cls_name == 'int':
                value = int(text or 0)
            elif cls_name == 'float':
                value = float(text or 0)
            else:
                assert False
            self.setting.set_value(value)
        except BaseException:
            self.setting.property('value').dispatch(self.setting)
            raise


class FlirBoolSettingWidget(FlirSettingWidget):
    pass


class FlirEnumSettingWidget(FlirSettingWidget):
    pass


class FlirCommandSettingWidget(FlirSettingWidget):
    pass


Builder.load_file(join(dirname(__file__), 'rotpy_player.kv'))
