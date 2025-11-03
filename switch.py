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
from aiovelib.service import IntegerItem, TextItem, DoubleArrayItem
from aiovelib.localsettings import Setting

from utils import logger, STATUS_OFF, STATUS_ON

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

MODULE_STATE_CONNECTED = 0x100
MODULE_STATE_OVER_TEMPERATURE = 0x101
MODULE_STATE_TEMPERATURE_WARNING = 0x102
MODULE_STATE_CHANNEL_FAULT = 0x103
MODULE_STATE_CHANNEL_TRIPPED = 0x104
MODULE_STATE_UNDER_VOLTAGE = 0x105

background_tasks = set()

# Base class for all switching devices.
class SwitchDevice(object):

	async def add_output(self, output_type, valid_functions=(1 << OutputFunction.MANUAL) | 0, name=""):

		self._type = output_type
		path_base  = '/SwitchableOutput/%s/' % self._channel_id
		self.service.add_item(IntegerItem(path_base + 'State', 0, writeable=True, onchange=self.set_state))
		self.service.add_item(IntegerItem(path_base + 'Status', 0, writeable=False, text=self._status_text_callback))
		self.service.add_item(TextItem(path_base + 'Name', name, writeable=False))
		valid_functions |= (1 << OutputFunction.MANUAL) # Always allow manual function
		if output_type == OutputType.DIMMABLE:
			self.service.add_item(IntegerItem(path_base + 'Dimming', 0, writeable=True, onchange=partial(self.throttled_updater, self._set_dimming_value), text=lambda y: str(y) + '%'))
		elif output_type == OutputType.RGB or output_type == OutputType.RGBW:
			self.service.add_item(DoubleArrayItem(path_base + 'LightControls',value=[0.0, 0.0, 0.0, 0.0, 0.0], writeable=True,
										  onchange=partial(self.throttled_updater, self._set_light_controls), text=self._light_controls_text_callback))

		initial_customname = await self._get_channel_customname()
		self.service.add_item(TextItem(path_base + 'Settings/Group', "", writeable=True, onchange=partial(self._value_changed, path_base + 'Settings/Group')))
		self.service.add_item(TextItem(path_base + 'Settings/CustomName', initial_customname, writeable=True, onchange=self.set_channel_name))
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
			Setting(base + 'ShowUIControl', 1, _min=0, _max=1, alias=f'ShowUIControl_{self._serial}_{self._channel_id}'),
			Setting(base + 'Function', int(OutputFunction.MANUAL), _min=0, _max=6, alias=f'Function_{self._serial}_{self._channel_id}'),
			Setting(base + 'Type', output_type, _min=0, _max=OutputType.TYPE_MAX, alias=f'Type_{self._serial}_{self._channel_id}'),
		)

		self._restore_settings(self._channel_id)

	def _get_valid_types(self):
		ret = 0
		ret |= 1 << OutputType.TOGGLE.value | 1 << OutputType.MOMENTARY.value if self._has_switch else 0
		ret |= 1 << OutputType.DIMMABLE.value if self._has_dimming else 0
		ret |= 1 << OutputType.RGB.value if self._has_rgb else 0
		# Support the RGB color wheel as well for RGBW devices.
		ret |= 1 << OutputType.RGB.value | 1 << OutputType.RGBW.value if self._has_rgbw else 0
		return ret

	def _restore_settings(self, channel):
		try:
			self._type = self.service.get_item("/SwitchableOutput/%s/Settings/Type" % channel).value
			with self.service as s:
				s['/SwitchableOutput/%s/Settings/Group' % channel] = self.settings.get_value(self.settings.alias(f'Group_{self._serial}_{channel}'))
				s['/SwitchableOutput/%s/Settings/ShowUIControl' % channel] = self.settings.get_value(self.settings.alias(f'ShowUIControl_{self._serial}_{channel}'))
				s['/SwitchableOutput/%s/Settings/Function' % channel] = self.settings.get_value(self.settings.alias(f'Function_{self._serial}_{channel}'))
				self._type = s['/SwitchableOutput/%s/Settings/Type' % channel] = self.settings.get_value(self.settings.alias(f'Type_{self._serial}_{channel}'))
		except:
			pass

	async def set_channel_name(self, item, value):
		if value is not None:
			logger.debug("Setting channel name for shelly device %s channel %d to: %s", self._serial, self._channel_id, value)
			await self._rpc_call(f"{self._rpc_device_type}.SetConfig" if self._rpc_device_type is not None else "EM.SetConfig", {"id": self._channel_id, "config": {"name": value}})
			item.set_local_value(value)

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
			self._type = value
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
		if value == OutputType.RGB:
			return "RGB"
		if value == OutputType.RGBW:
			return "RGBW"
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
		if value & (1 << OutputType.RGB):
			if str:
				str += ", "
			str += "RGB"
		if value & (1 << OutputType.RGBW):
			if str:
				str += ", "
			str += "RGBW"
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

	def _light_controls_text_callback(self, v):
		if self._type == OutputType.RGBW:
			return "H: %.1f, S: %.1f, B: %.1f, W: %.1f" % (v[0], v[1], v[2], v[3])
		return "H: %.1f, S: %.1f, B: %.1f" % (v[0], v[1], v[2])

	def on_channel_type_changed(self, channel, value):
		if value == OutputType.RGB:
			# Set white channel to 0 when switching to RGB
			item = self.service.get_item(f'/SwitchableOutput/{self._channel_id}/LightControls')
			v = list(item.value)
			v[3] = 0.0
			self._desired_value = v
			task = asyncio.create_task(self._set_light_controls(item, v, force_white=True))
			background_tasks.add(task)
			task.add_done_callback(background_tasks.discard)

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

	# Throttling mechanism to avoid a queue build-up on the device when the user is dragging a slider in the UI.
	# Used for dimming tasks. The dispatched task should exit quietly
	# if the value it is passed is not the desired value anymore by the time the runner wakes up.
	async def throttled_updater(self, update_task, item, value):
		async with self._throttling_lock:
			self._desired_value = value
			task = asyncio.create_task(self._throttled_updater_runner(update_task, item, value))
			background_tasks.add(task)
			task.add_done_callback(background_tasks.discard)

	async def _throttled_updater_runner(self, update_task, item, value):
		# Only start one updater at a time
		async with self._throttling_runner_lock:
			# If a new dimming setpoint has been set by the time this thread wakes up, then exit this one.
			async with self._throttling_lock:
				if value != self._desired_value:
					return
			await update_task(item, value)

	async def _set_dimming_value(self, item, value):
		if value < 0 or value > 100:
			return

		item.set_local_value(value) # Set the value here already to make the UI more responsive

		await self._rpc_call(
			"Light.Set",
			{
				"id": self._channel_id,
				"brightness": value,
			}
		)

	async def _set_light_controls(self, item, value, force_white=False):
		if not (self._has_rgb or self._has_rgbw) or not isinstance(value, list) or len(value) != 5:
			return

		item.set_local_value(value) # Set the value here already to make the UI more responsive
		rgb = self._hsv2rgb(value, normalise=True)

		params = {
					"id": self._channel_id,
					"rgb": rgb,
					"brightness": int(value[2])
				}
		if force_white or (self._has_rgbw and self._type == OutputType.RGBW):
			params["white"] = int(value[3] * 2.55)

		await self._rpc_call(
			f"{self._rpc_device_type}.Set",
			params
		)

	def _rgb2hsv(self, rgb):
		h, s, v = colorsys.rgb_to_hsv(rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0)
		h = h * 360.0
		s = s * 100.0
		v = v * 100.0
		# If value or saturation is zero, restore previous hue/saturation
		if v == 0.0:
			h = self._desired_value[0]
			s = self._desired_value[1]
		elif s == 0.0:
			h = self._desired_value[0]
		return h, s, v

	def _hsv2rgb(self, hsv, normalise=False):
		h = hsv[0]
		s = hsv[1]
		v = hsv[2]
		if v == 0.0:
			return [0, 0, 0]
		brightness = 1.0 if normalise else v / 100.0
		if s == 0.0:
			# Achromatic (grey)
			r = g = b = int(brightness * 255)
		else:
			rf, gf, bf = colorsys.hsv_to_rgb(h / 360.0, s / 100.0, brightness)
			r = int(rf * 255)
			g = int(gf * 255)
			b = int(bf * 255)
		return [r, g, b]

	def update(self, status_json):
		if not (self._has_switch or self._has_dimming or self._has_rgb or self._has_rgbw) or status_json is None:
			return
		try:
			switch_prefix = f'/SwitchableOutput/{self._channel_id}/'
			status = STATUS_ON if status_json["output"] else STATUS_OFF
			with self.service as s:
				s[switch_prefix + 'State'] = 1 if status == STATUS_ON else 0
				s[switch_prefix + 'Status'] = status
				if self._has_dimming:
					s[switch_prefix + 'Dimming'] = status_json.get("brightness", 0)
				if self._has_rgb or self._has_rgbw:
					brightness = status_json.get("brightness", 0)
					white = status_json.get("white", 0) / 2.55 if self._has_rgbw and self._type == OutputType.RGBW else 0.0
					hue, sat, val = self._rgb2hsv(status_json.get("rgb", [0, 0, 0]))
					s[switch_prefix + 'LightControls'] = [hue, sat, brightness, white, 0.0]
		except:
			pass
