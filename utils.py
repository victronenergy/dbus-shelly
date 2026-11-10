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

STATUS_OFF = 0x00
STATUS_ON = 0x09

class OutputType(IntEnum):
	MOMENTARY = 0
	TOGGLE = 1
	DIMMABLE = 2
	THREE_STATE_SWITCH = 9
	RGB = 11
	CCT = 12
	TYPE_MAX = RGBW = 13

# Venus generic input API, see https://github.com/victronenergy/venus/wiki/dbus#generic-input-api
class InputType(IntEnum):
	DISCRETE = 0		# discrete value indicator (0/1, but also more integers)
	VALUE_NO_RANGE = 1	# value indicator without range
	VALUE_WITH_RANGE = 2	# value indicator with range
	TEMPERATURE = 3		# temperature indicator (with range)

class InputStatus(IntEnum):
	OK = 0x00
	FAULT = 0x01
	BATTERY_LOW = 0x02

# /GenericInput/x/Event: bit 0 toggles with every new event, the other bits say what happened.
# Only single tap is defined so far; double tap and swipe are reserved for future expansion.
EVENT_TOGGLE_BIT = 0x01
EVENT_SINGLE_TAP = 0x02

class OutputFunction(IntEnum):
	ALARM = 0
	GENSET_START_STOP = 1
	MANUAL = 2
	TANK_PUMP = 3
	TEMPERATURE = 4
	CONNECTED_GENSET_HELPER_RELAY = 5
	OPPORTUNITY_LOAD = 6 # Also known as S2 Resource manager

# Text formatters
formatters = {
	'watt': lambda v: "{:.0f}W".format(v),
	'volt': lambda v: "{:.1f}V".format(v),
	'amp': lambda v: "{:.1f}A".format(v),
	'celsius': lambda v: "{:.1f}°C".format(v),
	'kwh': lambda v: "{:.2f}kWh".format(v),
	'percent': lambda v: "{:.0f}%".format(v),
	'productid': lambda v: "0x{:X}".format(v)
}

class SettingsMonitor(Monitor):
	def __init__(self, bus, itemsChanged=None, **kwargs):
		super().__init__(bus, itemsChanged=itemsChanged, handlers = {
			'com.victronenergy.settings': SettingsClient
		}, **kwargs)
		

async def wait_for_settings(bus, itemsChanged=None):
	""" Attempt a connection to localsettings. """
	settingsmonitor = await SettingsMonitor.create(bus, itemsChanged=itemsChanged)
	""" Attempt a connection to localsettings. If it does not show
		    up within 5 seconds, return None. """
	try:
		return await asyncio.wait_for(
			settingsmonitor.wait_for_service(SETTINGS_SERVICE), 5)
	except TimeoutError:
		pass

	return None