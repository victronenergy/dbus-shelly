#!/usr/bin/python3

from __future__ import annotations
import sys
import os
from functools import partial
from enum import IntEnum
# aiovelib
sys.path.insert(1, os.path.join(os.path.dirname(__file__), 'ext', 'aiovelib'))
from aiovelib.service import IntegerItem, TextItem
from aiovelib.localsettings import Setting

from utils import STATUS_OFF, STATUS_ON

class OutputType(IntEnum):
	MOMENTARY = 0
	TOGGLE = 1
	DIMMABLE = 2

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

	async def add_output(self, channel, output_type, set_state_cb, valid_functions=(1 << OutputFunction.MANUAL) | 0, name="", customName="", set_dimming_cb=None):
		path_base  = '/SwitchableOutput/%s/' % channel
		self._set_state_cb = set_state_cb
		self.service.add_item(IntegerItem(path_base + 'State', 0, writeable=True, onchange=set_state_cb))
		self.service.add_item(IntegerItem(path_base + 'Status', 0, writeable=False, text=self._status_text_callback))
		self.service.add_item(TextItem(path_base + 'Name', name, writeable=False))
		valid_functions |= (1 << OutputFunction.MANUAL) # Always allow manual function
		if output_type == OutputType.DIMMABLE:
			self.service.add_item(IntegerItem(path_base + 'Dimming', 0, writeable=True, onchange=set_dimming_cb, text=lambda y: str(y) + '%'))

		# Settings
		validTypesDimmable = 1 << OutputType.DIMMABLE.value
		validTypesToggle = 1 << OutputType.TOGGLE.value
		validTypesMomentary = 1 << OutputType.MOMENTARY.value

		self.service.add_item(TextItem(path_base + 'Settings/Group', "", writeable=True, onchange=partial(self._value_changed, path_base + 'Settings/Group')))
		self.service.add_item(TextItem(path_base + 'Settings/CustomName', customName, writeable=True, onchange=partial(self._value_changed, path_base + 'Settings/CustomName')))
		self.service.add_item(IntegerItem(path_base + 'Settings/ShowUIControl', 1, writeable=True, onchange=partial(self._value_changed, path_base + 'Settings/ShowUIControl')))
		self.service.add_item(IntegerItem(path_base + 'Settings/Type', output_type, writeable=True, onchange=partial(self._value_changed, path_base + 'Settings/Type'),
							text=self._type_text_callback))
		self.service.add_item(IntegerItem(path_base + 'Settings/Function', int(OutputFunction.MANUAL), writeable=True, onchange=partial(self._value_changed, path_base + 'Settings/Function'),
							text=self._function_text_callback))

		self.service.add_item(IntegerItem(path_base + 'Settings/ValidTypes', validTypesDimmable if
							output_type == OutputType.DIMMABLE else validTypesToggle | validTypesMomentary,
							writeable=False, text=self._valid_types_text_callback))
		self.service.add_item(IntegerItem(path_base + 'Settings/ValidFunctions', int(valid_functions), writeable=False,
							text=self._valid_functions_text_callback))

		base = self._settings_base + '%s/' % channel
		await self.settings.add_settings(
			Setting(base + 'Group', "", alias=f'Group_{self._serial}_{channel}'),
			Setting(base + 'CustomName', "", alias=f'CustomName_{self._serial}_{channel}'),
			Setting(base + 'ShowUIControl', 1, _min=0, _max=1, alias=f'ShowUIControl_{self._serial}_{channel}'),
			Setting(base + 'Function', int(OutputFunction.MANUAL), _min=0, _max=6, alias=f'Function_{self._serial}_{channel}'),
			Setting(base + 'Type', output_type, _min=0, _max=2, alias=f'Type_{self._serial}_{channel}'),
		)

		if output_type == OutputType.DIMMABLE:
			await self.settings.add_settings(
				Setting(base + 'Dimming', 0, _min=0, _max=100, alias=f'Dimming_{self._serial}_{channel}'),
			)

		self._restore_settings(channel)

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
				if not await self._set_channel_function(split[-3], value):
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

	async def _set_channel_function(self, channel, value):
		ret = (1 << value) & self.service.get_item("/SwitchableOutput/%s/Settings/ValidFunctions" % channel).value
		if ret:
			await self.on_channel_function_changed(channel, value)
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

	async def on_channel_function_changed(self, channel, value):
		pass

	def update(self, status_json):
		if self._has_switch:
			try:
				switch_prefix = f"/SwitchableOutput/{self._channel}/"
				status = STATUS_ON if status_json["output"] else STATUS_OFF
				with self.service as s:
					s[switch_prefix + 'State'] = 1 if status == STATUS_ON else 0
					s[switch_prefix + 'Status'] = status
			except:
				pass
