import asyncio
from functools import partial

from aiovelib.service import Service, IntegerItem, DoubleItem, TextItem
from aiovelib.localsettings import Setting
from enum import IntEnum

class OutputType(IntEnum):
	MOMENTARY = 0
	LATCHING = 1
	DIMMABLE = 2

class OutputFunction(IntEnum):
	ALARM = 0
	GENSET_START_STOP = 1
	MANUAL = 2
	TANK_PUMP = 3
	TEMPERATURE = 4
	CONNECTED_GENSET_HELPER_RELAY = 5
	S2_RM = 6

STATUS_OFF = 0x00
STATUS_ON = 0x09
STATUS_OUTPUT_FAULT = 0x08
STATUS_DISABLED = 0x20

MODULE_STATE_CONNECTED = 0x100
MODULE_STATE_OVER_TEMPERATURE = 0x101
MODULE_STATE_TEMPERATURE_WARNING = 0x102
MODULE_STATE_CHANNEL_FAULT = 0x103
MODULE_STATE_CHANNEL_TRIPPED = 0x104
MODULE_STATE_UNDER_VOLTAGE = 0x105

# Base class for all switching devices.
class SwitchDevice(object):
	_runningloop = None

	def _value_changed(self, path, value):
		split = path.split('/')
		if len(split) > 3 and split[3] == 'Settings':
			if split[-1] == 'Type':
				if not self._set_channel_type(split[-3], value):
					return False
			elif split[-1] == 'Function':
				if not self._set_channel_function(split[-3], value):
					return False
			setting = split[-1] + '_' + split[-3]
			try:
				self.settings.set_value_async(self.settings.alias(setting), value)
			except :
				return False
			return True

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

	async def add_output(self, channel, output_type, set_state_cb, valid_functions=(1 << OutputFunction.MANUAL) | 0, name="", customName="", set_dimming_cb=None):
		path_base  = '/SwitchableOutput/%s/' % channel
		self.service.add_item(IntegerItem(path_base + 'State', 0, writeable=True, onchange=set_state_cb))
		self.service.add_item(IntegerItem(path_base + 'Status', 0, writeable=False, text=self._status_text_callback))
		self.service.add_item(TextItem(path_base + 'Name', name, writeable=False))
		valid_functions |= (1 << OutputFunction.MANUAL) # Always allow manual function
		if output_type == OutputType.DIMMABLE:
			self.service.add_item(IntegerItem(path_base + 'Dimming', 0, writeable=True, onchange=set_dimming_cb, text=lambda y: str(y) + '%'))

		# Settings
		validTypesDimmable = 1 << OutputType.DIMMABLE.value
		validTypesLatching = 1 << OutputType.LATCHING.value
		validTypesMomentary = 1 << OutputType.MOMENTARY.value

		self.service.add_item(TextItem(path_base + 'Settings/Group', "", writeable=True, onchange=partial(self._value_changed, path_base + 'Settings/Group')))
		self.service.add_item(TextItem(path_base + 'Settings/CustomName', customName, writeable=True, onchange=partial(self._value_changed, path_base + 'Settings/CustomName')))
		self.service.add_item(IntegerItem(path_base + 'Settings/ShowUIControl', 1, writeable=True, onchange=partial(self._value_changed, path_base + 'Settings/ShowUIControl')))
		self.service.add_item(IntegerItem(path_base + 'Settings/Type', output_type, writeable=True, onchange=partial(self._value_changed, path_base + 'Settings/Type'),
							text=self._type_text_callback))
		self.service.add_item(IntegerItem(path_base + 'Settings/Function', int(OutputFunction.MANUAL), writeable=True, onchange=partial(self._value_changed, path_base + 'Settings/Function'),
							text=self._function_text_callback))

		self.service.add_item(IntegerItem(path_base + 'Settings/ValidTypes', validTypesDimmable if
							output_type == OutputType.DIMMABLE else validTypesLatching | validTypesMomentary, 
							writeable=False, text=self._valid_types_text_callback))
		self.service.add_item(IntegerItem(path_base + 'Settings/ValidFunctions', int(valid_functions), writeable=False,
							text=self._valid_functions_text_callback))

		await self.settings.add_settings(
			Setting('/Settings/Devices/shelly_%s/%s/Group' % (self._serial, channel), "", alias='Group_%s' % channel),
			Setting('/Settings/Devices/shelly_%s/%s/CustomName' % (self._serial, channel), "", alias='CustomName_%s' % channel),
			Setting('/Settings/Devices/shelly_%s/%s/ShowUIControl' % (self._serial, channel), 1, _min=0, _max=1, alias='ShowUIControl_%s' % channel),
			Setting('/Settings/Devices/shelly_%s/%s/Function' % (self._serial, channel), int(OutputFunction.MANUAL), _min=0, _max=6, alias='Function_%s' % channel),
			Setting('/Settings/Devices/shelly_%s/%s/Type' % (self._serial, channel), output_type, _min=0, _max=2, alias='Type_%s' % channel)
		)

		if output_type == OutputType.DIMMABLE:
			await self.settings.add_settings(
				Setting('/Settings/%s/%s/Dimming' % (self._serial, channel), 0, _min=0, _max=100, alias='dimming_%s' % channel)
			)

		self.restore_settings(channel)
		self.initialize_channel(channel)

	def restore_settings(self, channel):
		try:
			with self.service as s:
				s['/SwitchableOutput/%s/Settings/Group' % channel] = self.settings.get_value(self.settings.alias('Group_%s' % channel))
				s['/SwitchableOutput/%s/Settings/CustomName' % channel] = self.settings.get_value(self.settings.alias('CustomName_%s' % channel))
				s['/SwitchableOutput/%s/Settings/ShowUIControl' % channel] = self.settings.get_value(self.settings.alias('ShowUIControl_%s' % channel))
				s['/SwitchableOutput/%s/Settings/Function' % channel] = self.settings.get_value(self.settings.alias('Function_%s' % channel))
				s['/SwitchableOutput/%s/Settings/Type' % channel] = self.settings.get_value(self.settings.alias('Type_%s' % channel))
		except :
			pass

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
		if value == OutputType.LATCHING:
			return "Latching"
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
		if value & (1 << OutputType.LATCHING):
			if str:
				str += ", "
			str += "Latching"
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

	def on_channel_values_changed(self, channel, values):
		pass

	def initialize_channel(self, channel):
		pass

	def on_channel_type_changed(self, channel, value):
		pass

	def on_channel_function_changed(self, channel, value):
		pass