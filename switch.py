#!/usr/bin/python3

from __future__ import annotations
import sys
import os
from functools import partial
from enum import IntEnum
import asyncio
import colorsys
# aiovelib
sys.path.insert(1, os.path.join(os.path.dirname(__file__), 'ext', 'aiovelib'))
from aiovelib.service import IntegerItem, TextItem
from aiovelib.localsettings import Setting

from utils import logger, STATUS_OFF, STATUS_ON

class OutputType(IntEnum):
	MOMENTARY = 0
	TOGGLE = 1
	DIMMABLE = 2,
	TYPE_MAX = RGB_DIMMABLE = 11

class OutputFunction(IntEnum):
	ALARM = 0
	GENSET_START_STOP = 1
	MANUAL = 2
	TANK_PUMP = 3
	TEMPERATURE = 4
	CONNECTED_GENSET_HELPER_RELAY = 5
	S2_RM = 6

MODULE_STATE_CONNECTED = 0x100
MODULE_STATE_OVER_TEMPERATURE = 0x101
MODULE_STATE_TEMPERATURE_WARNING = 0x102
MODULE_STATE_CHANNEL_FAULT = 0x103
MODULE_STATE_CHANNEL_TRIPPED = 0x104
MODULE_STATE_UNDER_VOLTAGE = 0x105

# Base class for all switching devices.
class SwitchDevice(object):

	def __init__(self, service, settings, serial, channel_id, capabilities, server, restart, rpc_callback, productid, productName):
		self._dimming_lock = asyncio.Lock()
		self._desired_dimming_value = None

	async def add_output(self, channel, output_type, valid_functions=(1 << OutputFunction.MANUAL) | 0, name=""):
		self._channel_id = channel
		path_base  = '/SwitchableOutput/%s/' % self._channel_id
		self.service.add_item(IntegerItem(path_base + 'State', 0, writeable=True, onchange=self.set_state))
		self.service.add_item(IntegerItem(path_base + 'Status', 0, writeable=False, text=self._status_text_callback))
		self.service.add_item(TextItem(path_base + 'Name', name, writeable=False))
		valid_functions |= (1 << OutputFunction.MANUAL) # Always allow manual function
		if output_type == OutputType.DIMMABLE:
			self.service.add_item(IntegerItem(path_base + 'Dimming', 0, writeable=True, onchange=self.set_dimming_value, text=lambda y: str(y) + '%'))
		elif output_type == OutputType.RGB_DIMMABLE:
				if not (self._has_rgb_dimming or self._has_rgbw_dimming):
					logger.warning("Output type RGB selected, but device does not support RGB or RGBW dimming.")
					return

				# Shelly RGB(W) dimmers must be controlled with RGB values, an optional brightness and a white component (RGBW only)
				self.service.add_item(IntegerItem(path_base + 'Dimming', 0, writeable=True, onchange=self.set_dimming_brightness, text=lambda y: str(y) + '%'))
				self.service.add_item(IntegerItem(path_base + 'Hue', 0, writeable=True, onchange=partial(self.set_dimming_rgb, 'h'), text=lambda y: str(y)))
				self.service.add_item(IntegerItem(path_base + 'Saturation', 0, writeable=True, onchange=partial(self.set_dimming_rgb, 's'), text=lambda y: str(y) + '%'))
				self.service.add_item(IntegerItem(path_base + 'Value', 0, writeable=True, onchange=partial(self.set_dimming_rgb, 'v'), text=lambda y: str(y) + '%'))
				if self._has_rgbw_dimming:
					self.service.add_item(IntegerItem(path_base + 'White', 0, writeable=True, onchange=self.set_dimming_white, text=lambda y: str(y) + '%'))

		self.service.add_item(TextItem(path_base + 'Settings/Group', "", writeable=True, onchange=partial(self._value_changed, path_base + 'Settings/Group')))
		self.service.add_item(TextItem(path_base + 'Settings/CustomName', "", writeable=True, onchange=partial(self._value_changed, path_base + 'Settings/CustomName')))
		self.service.add_item(IntegerItem(path_base + 'Settings/ShowUIControl', 1, writeable=True, onchange=partial(self._value_changed, path_base + 'Settings/ShowUIControl')))
		self.service.add_item(IntegerItem(path_base + 'Settings/Type', output_type, writeable=True, onchange=partial(self._value_changed, path_base + 'Settings/Type'),
							text=self._type_text_callback))
		self.service.add_item(IntegerItem(path_base + 'Settings/Function', int(OutputFunction.MANUAL), writeable=True, onchange=partial(self._value_changed, path_base + 'Settings/Function'),
							text=self._function_text_callback))

		self.service.add_item(IntegerItem(path_base + 'Settings/ValidTypes', int(self._get_valid_types()),
							writeable=False, text=self._valid_types_text_callback))
		self.service.add_item(IntegerItem(path_base + 'Settings/ValidFunctions', int(valid_functions), writeable=False,
							text=self._valid_functions_text_callback))

		base = self._settings_base + '%s/' % self._channel_id
		await self.settings.add_settings(
			Setting(base + 'Group', "", alias=f'Group_{self._serial}_{self._channel_id}'),
			Setting(base + 'CustomName', "", alias=f'CustomName_{self._serial}_{self._channel_id}'),
			Setting(base + 'ShowUIControl', 1, _min=0, _max=1, alias=f'ShowUIControl_{self._serial}_{self._channel_id}'),
			Setting(base + 'Function', int(OutputFunction.MANUAL), _min=0, _max=6, alias=f'Function_{self._serial}_{self._channel_id}'),
			Setting(base + 'Type', output_type, _min=0, _max=OutputType.TYPE_MAX, alias=f'Type_{self._serial}_{self._channel_id}'),
		)

		self._restore_settings(self._channel_id)

	def _get_valid_types(self):
		if self._has_dimming:
			return 1 << OutputType.DIMMABLE.value
		elif self._has_rgb_dimming or self._has_rgbw_dimming:
			return 1 << OutputType.RGB_DIMMABLE.value
		else:
			return 1 << OutputType.TOGGLE.value | 1 << OutputType.MOMENTARY.value

	def _restore_settings(self, channel):
		try:
			with self.service as s:
				s['/SwitchableOutput/%s/Settings/Group' % channel] = self.settings.get_value(self.settings.alias(f'Group_{self._serial}_{channel}'))
				s['/SwitchableOutput/%s/Settings/CustomName' % channel] = self.settings.get_value(self.settings.alias(f'CustomName_{self._serial}_{channel}'))
				s['/SwitchableOutput/%s/Settings/ShowUIControl' % channel] = self.settings.get_value(self.settings.alias(f'ShowUIControl_{self._serial}_{channel}'))
				s['/SwitchableOutput/%s/Settings/Function' % channel] = self.settings.get_value(self.settings.alias(f'Function_{self._serial}_{channel}'))
				s['/SwitchableOutput/%s/Settings/Type' % channel] = self.settings.get_value(self.settings.alias(f'Type_{self._serial}_{channel}'))
		except :
			pass

	async def _value_changed(self, path, item, value):
		split = path.split('/')
		if len(split) > 3 and split[3] == 'Settings':
			if split[-1] == 'Type':
				if not self._set_channel_type(split[-3], value):
					return
			elif split[-1] == 'Function':
				if not self._set_channel_function(split[-3], value):
					return
			setting = split[-1] + '_' + self._serial + '_' + split[-3]
			try:
				await self.settings.set_value(self.settings.alias(setting), value)
			except :
				return
			item.set_local_value(value)

	def _set_channel_type(self, channel, value):
		ret = (1 << value) & self.service.get_item("/SwitchableOutput/%s/Settings/ValidTypes" % channel).value
		if ret:
			self.on_channel_type_changed(channel, value)
		return ret

	def _set_channel_function(self, channel, value):
		ret = (1 << value) & self.service.get_item("/SwitchableOutput/%s/Settings/ValidFunctions" % channel).value
		if ret:
			self.on_channel_function_changed(channel, value)
		return ret

	def _module_state_text_callback(self, value):
		if value == MODULE_STATE_CONNECTED:
			return "Connected"
		if value == MODULE_STATE_OVER_TEMPERATURE:
			return "Over temperature"
		if value == MODULE_STATE_TEMPERATURE_WARNING:
			return "Temperature warning"
		if value == MODULE_STATE_CHANNEL_FAULT:
			return "Channel fault"
		if value == MODULE_STATE_CHANNEL_TRIPPED:
			return "Channel tripped"
		if value == MODULE_STATE_UNDER_VOLTAGE:
			return "Under voltage"
		return "Unknown"

	def _status_text_callback(self, value):
		if value == 0x00:
			return "Off"
		if value == 0x09:
			return "On"
		if value == 0x02:
			return "Tripped"
		if value == 0x04:
			return "Over temperature"
		if value == 0x01:
			return "Powered"
		if value == 0x08:
			return "Output fault"
		if value == 0x10:
			return "Short fault"
		if value == 0x20:
			return "Disabled"
		return "Unknown"

	def _type_text_callback(self, value):
		if value == OutputType.MOMENTARY:
			return "Momentary"
		if value == OutputType.TOGGLE:
			return "Toggle"
		if value == OutputType.DIMMABLE:
			return "Dimmable"
		if value == OutputType.RGB_DIMMABLE:
			return "RGB Dimmable"
		return "Unknown"

	def _function_text_callback(self, value):
		if value == OutputFunction.ALARM:
			return "Alarm"
		if value == OutputFunction.GENSET_START_STOP:
			return "Genset start stop"
		if value == OutputFunction.MANUAL:
			return "Manual"
		if value == OutputFunction.TANK_PUMP:
			return "Tank pump"
		if value == OutputFunction.TEMPERATURE:
			return "Temperature"
		if value == OutputFunction.CONNECTED_GENSET_HELPER_RELAY:
			return "Connected genset helper relay"
		if value == OutputFunction.S2_RM:
			return "S2 resource manager"
		return "Unknown"

	def _valid_types_text_callback(self, value):
		str = ""
		if value & (1 << OutputType.DIMMABLE):
			str += "Dimmable"
		if value & (1 << OutputType.TOGGLE):
			if str:
				str += ", "
			str += "Toggle"
		if value & (1 << OutputType.MOMENTARY):
			if str:
				str += ", "
			str += "Momentary"
		if value & (1 << OutputType.RGB_DIMMABLE):
			if str:
				str += ", "
			str += "RGB Dimmable"
		return str

	def _valid_functions_text_callback(self, value):
		str = ""
		if value & (1 << OutputFunction.ALARM):
			str += "Alarm"
		if value & (1 << OutputFunction.GENSET_START_STOP):
			if str:
				str += ", "
			str += "Genset start stop"
		if value & (1 << OutputFunction.MANUAL):
			if str:
				str += ", "
			str += "Manual"
		if value & (1 << OutputFunction.TANK_PUMP):
			if str:
				str += ", "
			str += "Tank pump"
		if value & (1 << OutputFunction.TEMPERATURE):
			if str:
				str += ", "
			str += "Temperature"
		if value & (1 << OutputFunction.CONNECTED_GENSET_HELPER_RELAY):
			if str:
				str += ", "
			str += "Connected genset helper relay"
		if value & (1 << OutputFunction.S2_RM):
			if str:
				str += ", "
			str += "S2 resource manager"
		return str

	def on_channel_type_changed(self, channel, value):
		pass

	def on_channel_function_changed(self, channel, value):
		pass

	async def set_state(self, item, value):
		if value not in (0, 1):
			return

		await self._rpc_call(
			f'{self._rpc_device_type}.Set',
			{
				# id is the switch channel, starting from 0
				"id":self._channel_id,
				"on":True if value == 1 else False,
			}
		)

		item.set_local_value(value)

	async def set_dimming_value(self, item, value):
		self._desired_dimming_value = value
		asyncio.create_task(self._set_dimming_value(item, value))

	async def _set_dimming_value(self, item, value):
		if value < 0 or value > 100:
			return

		async with self._dimming_lock:
			# If a new dimming setpoint has been set by the time this thread wakes up, then exit.
			if value != self._desired_dimming_value:
				return

			await self._rpc_call(
				"Light.Set",
				{
					"id": self._channel_id,
					"brightness": value,
				}
			)

			item.set_local_value(value)

	async def set_dimming_brightness(self, item, value):
		if value < 0 or value > 100:
			return
		await self._rpc_call(
					f'{self._rpc_device_type}.Set',
					{
						"id": self._channel_id,
						"brightness": value,
					}
				)
		item.set_local_value(value)

	async def set_dimming_white(self, item, value):
		if value < 0 or value > 100:
			return
		await self._rpc_call(
					f'{self._rpc_device_type}.Set',
					{
						"id": self._channel_id,
						"white": value * 255 // 100,
					}
				)
		item.set_local_value(value)

	async def set_dimming_rgb(self, param, item, value):
		hsv = {'h': None, 's': None, 'v': None}
		hsv[param] = value

		try:
			if not hsv['h']:
				hsv['h'] = int(self.service.get_item(f'/SwitchableOutput/{self._channel_id}/Hue').value)
			if not hsv['s']:
				hsv['s'] = int(self.service.get_item(f'/SwitchableOutput/{self._channel_id}/Saturation').value)
			if not hsv['v']:
				hsv['v'] = int(self.service.get_item(f'/SwitchableOutput/{self._channel_id}/Dimming').value)
		except:
			return
		if hsv['h'] < 0 or hsv['h'] > 360 or hsv['s'] < 0 or hsv['s'] > 100 or hsv['v'] < 0 or hsv['v'] > 100:
			return

		item.set_local_value(value)

		# Convert HSV to RGB
		rgb = colorsys.hsv_to_rgb(hsv['h'] / 360, hsv['s'] / 100, hsv['v'] / 100)
		rgb = [int(c * 255) for c in rgb]

		await self._set_dimming_rgb(rgb)

	async def _set_dimming_rgb(self, rgb):
		async with self._dimming_lock:
			logger.debug("Setting RGB dimming for channel %d to %s", self._channel_id, rgb)
			if any(c < 0 or c > 255 for c in rgb):
				return

			await self._rpc_call(
				f'{self._rpc_device_type}.Set',
				{"id": self._channel_id, "rgb": rgb[:3]}
			)

	def update(self, status_json):
		if not (self._has_switch or self._has_dimming or self._has_rgb_dimming or self._has_rgbw_dimming):
			return
		try:
			switch_prefix = f'/SwitchableOutput/{self._channel_id}/'
			status = STATUS_ON if status_json["output"] else STATUS_OFF
			with self.service as s:
				s[switch_prefix + 'State'] = 1 if status == STATUS_ON else 0
				s[switch_prefix + 'Status'] = status
				if self._has_dimming:
					s[switch_prefix + 'Dimming'] = status_json.get("brightness", 0)
				elif self._has_rgb_dimming or self._has_rgbw_dimming:
					#TODO if the conversion from RGB to HSV and vice versa is a bit off (due to rounding errors), it may cause an unstable loop of the set_dimming_rgb callback being called repeatedly.
					# Check if this is the case. Otherwise, consider storing the last set RGB value and use that to update the HSV values instead of converting back.
					r, g, b = status_json.get("rgb", [0, 0, 0])
					hue, saturation, value = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
					s[switch_prefix + 'Hue'] = round(hue * 360)
					s[switch_prefix + 'Saturation'] = round(saturation * 100)
					s[switch_prefix + 'Value'] = round(value * 100)
					s[switch_prefix + 'Dimming'] = status_json.get("brightness", 0)
					if self._has_rgbw_dimming:
						s[switch_prefix + 'White'] = round(status_json.get("white", 0) * 100 / 255)
		except:
			pass
