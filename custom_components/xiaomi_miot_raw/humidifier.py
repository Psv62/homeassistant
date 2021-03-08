import asyncio
import json
import logging
from datetime import timedelta
from functools import partial
from typing import Optional

from collections import OrderedDict
import async_timeout
import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from aiohttp import ClientSession
from homeassistant.components.humidifier import (
    HumidifierEntity, PLATFORM_SCHEMA)
from homeassistant.const import *
from homeassistant.components.humidifier.const import *
from homeassistant.exceptions import PlatformNotReady
from homeassistant.helpers import aiohttp_client
from homeassistant.util import Throttle
from miio.device import Device
from miio.exceptions import DeviceException
from miio.miot_device import MiotDevice

from . import GenericMiotDevice, ToggleableMiotDevice, dev_info
from .deps.const import (
    DOMAIN,
    CONF_UPDATE_INSTANT,
    CONF_MAPPING,
    CONF_CONTROL_PARAMS,
    CONF_CLOUD,
    CONF_MODEL,
    ATTR_STATE_VALUE,
    ATTR_MODEL,
    ATTR_FIRMWARE_VERSION,
    ATTR_HARDWARE_VERSION,
    SCHEMA,
    MAP,
    DUMMY_IP,
    DUMMY_TOKEN,
)
import copy
TYPE = 'humidifier'

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = "Generic MIoT " + TYPE
DATA_KEY = TYPE + '.' + DOMAIN

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    SCHEMA
)

SCAN_INTERVAL = timedelta(seconds=10)
# pylint: disable=unused-argument

@asyncio.coroutine
async def async_setup_platform(hass, config, async_add_devices, discovery_info=None):
    """Set up the sensor from config."""

    if DATA_KEY not in hass.data:
        hass.data[DATA_KEY] = {}

    host = config.get(CONF_HOST)
    token = config.get(CONF_TOKEN)
    mapping = config.get(CONF_MAPPING)
    params = config.get(CONF_CONTROL_PARAMS)

    mappingnew = {}

    main_mi_type = None
    this_mi_type = []

    for t in MAP[TYPE]:
        if mapping.get(t):
            this_mi_type.append(t)
        if 'main' in (params.get(t) or ""):
            main_mi_type = t

    if main_mi_type or type(params) == OrderedDict:
        for k,v in mapping.items():
            for kk,vv in v.items():
                mappingnew[f"{k[:10]}_{kk}"] = vv

        _LOGGER.info("Initializing %s with host %s (token %s...)", config.get(CONF_NAME), host, token[:5])
        if type(params) == OrderedDict:
            miio_device = MiotDevice(ip=host, token=token, mapping=mapping)
        else:
            miio_device = MiotDevice(ip=host, token=token, mapping=mappingnew)
        try:
            if host == DUMMY_IP and token == DUMMY_TOKEN:
                raise DeviceException
            device_info = miio_device.info()
            model = device_info.model
            _LOGGER.info(
                "%s %s %s detected",
                model,
                device_info.firmware_version,
                device_info.hardware_version,
            )

        except DeviceException as de:
            if not config.get(CONF_CLOUD):
                _LOGGER.warn(de)
                raise PlatformNotReady
            else:
                if not (di := config.get('cloud_device_info')):
                    _LOGGER.error(f"未能获取到设备信息，请删除 {config.get(CONF_NAME)} 重新配置。")
                    raise PlatformNotReady
                else:
                    device_info = dev_info(
                        di['model'],
                        di['mac'],
                        di['fw_version'],
                        ""
                    )
        device = MiotHumidifier(miio_device, config, device_info, hass, main_mi_type)

        _LOGGER.info(f"{main_mi_type} is the main device of {host}.")
        hass.data[DOMAIN]['miot_main_entity'][host] = device
        hass.data[DOMAIN]['entities'][device.unique_id] = device
        async_add_devices([device], update_before_add=True)
    else:
        _LOGGER.error(f"加湿器只能作为主设备！请检查{config.get(CONF_NAME)}配置")

async def async_setup_entry(hass, config_entry, async_add_entities):
    config = copy.copy(hass.data[DOMAIN]['configs'].get(config_entry.entry_id, dict(config_entry.data)))
    # config[CONF_MAPPING] = config[CONF_MAPPING][TYPE]
    # config[CONF_CONTROL_PARAMS] = config[CONF_CONTROL_PARAMS][TYPE]
    await async_setup_platform(hass, config, async_add_entities)

class MiotHumidifier(ToggleableMiotDevice, HumidifierEntity):
    """Representation of a humidifier device."""
    def __init__(self, device, config, device_info, hass, main_mi_type):
        ToggleableMiotDevice.__init__(self, device, config, device_info, hass, main_mi_type)
        self._target_humidity = None
        self._mode = None
        self._available_modes = None
        self._device_class = DEVICE_CLASS_HUMIDIFIER

    @property
    def supported_features(self):
        """Return the list of supported features."""
        s = 0
        if self._did_prefix + 'mode' in self._mapping:
            s |= SUPPORT_MODES
        return s

    @property
    def min_humidity(self):
        try:
            return (self._ctrl_params['target_humidity']['value_range'][0])
        except KeyError:
            return None

    @property
    def max_humidity(self):
        try:
            return (self._ctrl_params['target_humidity']['value_range'][1])
        except KeyError:
            return None

    @property
    def target_humidity(self):
        """Return the humidity we try to reach."""
        return self._target_humidity

    @property
    def mode(self):
        """Return current mode."""
        return self._mode

    @property
    def available_modes(self):
        """Return available modes."""
        return list(self._ctrl_params['mode'].keys())

    @property
    def device_class(self):
        """Return the device class of the humidifier."""
        return self._device_class

    async def async_set_humidity(self, humidity):
        """Set new humidity level."""
        hum = self.convert_value(humidity, "target_humidity")
        result = await self.set_property_new(self._did_prefix + "target_humidity", hum)

        if result:
            self._target_humidity = hum

    async def async_set_mode(self, mode):
        """Update mode."""
        result = await self.set_property_new(self._did_prefix + "mode", self._ctrl_params['mode'].get(mode))

        if result:
            self._mode = mode

    def _handle_platform_specific_attrs(self):
        super()._handle_platform_specific_attrs()
        self._target_humidity = self._state_attrs.get(self._did_prefix + 'target_humidity')
        self._mode = self.get_key_by_value(self._ctrl_params['mode'], self._state_attrs.get(self._did_prefix + 'mode'))
