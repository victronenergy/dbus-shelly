# dbus_shelly/utils.py
import asyncio
import json
import logging
from itertools import cycle
from typing import Any, Dict
from enum import IntEnum
from aiovelib.client import Monitor
from aiovelib.localsettings import SettingsService as SettingsClient, SETTINGS_SERVICE

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

STATUS_OFF = 0x00
STATUS_ON = 0x09

class OutputType(IntEnum):
	MOMENTARY = 0
	TOGGLE = 1
	DIMMABLE = 2
	RGB = 11
	TYPE_MAX = RGBW = 13

class OutputFunction(IntEnum):
	ALARM = 0
	GENSET_START_STOP = 1
	MANUAL = 2
	TANK_PUMP = 3
	TEMPERATURE = 4
	CONNECTED_GENSET_HELPER_RELAY = 5
	S2_RM = 6

# Text formatters
formatters = {
	'watt': lambda v: "{:.0f}W".format(v),
	'volt': lambda v: "{:.1f}V".format(v),
	'amp': lambda v: "{:.1f}A".format(v),
	'celsius': lambda v: "{:.1f}°C".format(v),
	'kwh': lambda v: "{:.2f}kWh".format(v),
	'productid': lambda v: "0x{:X}".format(v)
}

class SettingsMonitor(Monitor):
	def __init__(self, bus, **kwargs):
		super().__init__(bus, handlers = {
			'com.victronenergy.settings': SettingsClient
		}, **kwargs)
		

async def wait_for_settings(bus):
	""" Attempt a connection to localsettings. """
	settingsmonitor = await SettingsMonitor.create(bus)
	""" Attempt a connection to localsettings. If it does not show
		    up within 5 seconds, return None. """
	try:
		return await asyncio.wait_for(
			settingsmonitor.wait_for_service(SETTINGS_SERVICE), 5)
	except TimeoutError:
		pass

	return None