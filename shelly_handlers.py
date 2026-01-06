from aiovelib.localsettings import Setting
from aiovelib.service import IntegerItem, TextItem, DoubleItem, TextArrayItem
from utils import logger, formatters as fmt, STATUS_OFF, STATUS_ON

from functools import partial
from enum import IntEnum
import asyncio

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

background_tasks = set()

# Capability -> handler class registry
HANDLER_KIND_FUNCTIONAL = "functional"
HANDLER_KIND_GENERIC = "generic"

_HANDLER_REGISTRY = {}

def register_handler(*capabilities, kind=HANDLER_KIND_FUNCTIONAL):
	def _decorator(handler_cls):
		for cap in capabilities:
			_HANDLER_REGISTRY[cap] = {"cls": handler_cls, "kind": kind}
		return handler_cls
	return _decorator

def get_handler_class(capability):
	info = _HANDLER_REGISTRY.get(capability)
	return info["cls"] if info else None

def get_handler_kind(capability):
	info = _HANDLER_REGISTRY.get(capability)
	return info["kind"] if info else None

def has_functional_handler(capabilities):
	for cap in capabilities:
		if get_handler_kind(cap) == HANDLER_KIND_FUNCTIONAL:
			return True
	return False

# The shelly handler base class implements basic functionality common to all shelly capability handlers.
# These methods apply to channels of all devices
class ShellyHandler(object):
	@classmethod
	async def create(cls, cap, channel_id=0, rpc_callback=None, restart_callback=None, shelly_channel=None):
		handler_cls = get_handler_class(cap)
		if handler_cls is None:
			return None

		c = handler_cls()
		c._channel_id = channel_id
		c.rpc_call = rpc_callback
		c.restart = restart_callback
		c.sc = shelly_channel
		c.service = getattr(shelly_channel, "service", None)
		c.settings = getattr(shelly_channel, "settings", None)
		c._serial = getattr(shelly_channel, "_serial", None)
		c._settings_base = f'/Settings/Devices/shelly_{c._serial}_{c._channel_id}/'
		await c.ainit()

		return c

	@property
	def capability(self):
		return self._rpc_device_type.lower()

	def __init__(self):
		self._channel_id = 0
		self.rpc_call = None
		self.restart = None
		self.sc = None
		self.service = None
		self.settings = None
		self._serial = None
		self._settings_base = None

	async def ainit(self):
		pass

	def set_service_name(self, type):
		self.service.name = f"com.victronenergy.{type}.shelly_{self._serial}_{self._channel_id}"
	
	# Override this in the handlers
	def update(self, status_json):
		pass

# Temperature handler, puts temperature readings on dbus.
@register_handler('Temperature', kind=HANDLER_KIND_GENERIC)
class ShellyHandler_temperature(ShellyHandler):
	_rpc_device_type = "Temperature"

	async def ainit(self):
		# Temperature path must be updated.
		self.service.add_item(DoubleItem(f'/Temperature', None, text=fmt['celsius']))

	def update(self, status_json):
		try:
			with self.service as s:
				s[f'/Temperature'] = status_json["tC"]
		except:
			pass

# System handler, handles device custom name setting and getting.
@register_handler('Sys', kind=HANDLER_KIND_GENERIC)
class ShellyHandler_sys(ShellyHandler):
	_rpc_device_type = "Sys"

	async def ainit(self):
		self.service.add_item(TextItem('/CustomName', "", writeable=True, onchange=self.set_custom_name))
		await self._set_device_customname()

	async def set_custom_name(self, item, value):
		if value is not None:
			logger.debug("Setting device name for shelly device %s to: %s", self._serial, value)
			item.set_local_value(value)
			await self.rpc_call(f"{self._rpc_device_type}.SetConfig", {"config": {"device": {"name": value}}})

	async def _set_device_customname(self):
		name = await self._get_device_customname()
		with self.service as s:
			s['/CustomName'] = name

	async def _get_device_customname(self):
		config = await self.rpc_call(f"{self._rpc_device_type}.GetConfig", {})
		if config is not None and 'device' in config and 'name' in config['device'] and config['device']['name']:
			return config['device']['name']
		return f'Shelly {self._serial}'

	def on_event(self, event):
		if event['event'] == "config_changed":
			task = asyncio.get_event_loop().create_task(self._set_device_customname())
			background_tasks.add(task)
			task.add_done_callback(background_tasks.discard)

# EMData handler, puts energy metering data on dbus.
# The EMData component is available on three-phase shelly energy meters.
@register_handler('EMData', kind=HANDLER_KIND_GENERIC)
class ShellyHandler_emdata(ShellyHandler):
	_rpc_device_type = "EMData"

	async def ainit(self):
		self._num_phases = 3

		self.service.add_item(DoubleItem('/Ac/Energy/Forward', None, text=fmt['kwh']))
		self.service.add_item(DoubleItem('/Ac/Energy/Reverse', None, text=fmt['kwh']))

		for channel in range(1, self._num_phases + 1):
			prefix = '/Ac/L{}/'.format(channel)
			self.service.add_item(DoubleItem(prefix + 'Energy/Forward', None, text=fmt['kwh']))
			self.service.add_item(DoubleItem(prefix + 'Energy/Reverse', None, text=fmt['kwh']))

	def update(self, emdata):
		try:
			for l in range(1, self._num_phases + 1):
				with self.service as s:
					em_prefix = f'/Ac/L{l}/'
					p = {1:'a', 2:'b', 3:'c'}.get(l)
					s[em_prefix + 'Energy/Forward'] = emdata[f'{p}_total_act_energy'] / 1000
					s[em_prefix + 'Energy/Reverse'] = emdata[f'{p}_total_act_ret_energy'] / 1000
			with self.service as s:
				s['/Ac/Energy/Forward'] = emdata['total_act_energy'] / 1000
				s['/Ac/Energy/Reverse'] = emdata['total_act_ret'] / 1000
		except:
			pass

# Contains common code for shelly handlers that have energy metering capabilities (single or multi-phase)
class Shelly_EM_base(object):
	async def init_em(self, num_phases, allowed_roles):
		# Determine role and instance
		_em_role, instance = self.role_instance(
			self.settings.get_value(self.settings.alias('instance_{}_{}'.format(self._serial, self._channel_id))))

		self.allowed_em_roles = allowed_roles
		if _em_role not in allowed_roles:
			_em_role = allowed_roles[0]
			await self.settings.set_value(self.settings.alias('instance_{}_{}'.format(self._serial, self._channel_id)), "{}:{}".format(_em_role, instance))

		self.service.add_item(TextItem('/Role', _em_role, writeable=True,
			onchange=self.role_changed))
		self.service.add_item(TextArrayItem('/AllowedRoles', allowed_roles, writeable=False))

		await self.settings.add_settings(
			Setting(self._settings_base + '%s/' % self._channel_id + 'Position', 0, 0, 2, alias="position_{}_{}".format(self._serial, self._channel_id))
		)

		self.service.add_item(IntegerItem('/Position', self.settings.get_value(self.settings.alias("position_{}_{}".format(self._serial, self._channel_id))),
				writeable=True, onchange=self.position_changed))

		if num_phases == 1:
			await self.settings.add_settings(
				Setting(self._settings_base + '%s/' % self._channel_id + 'PhaseSetting', 1, 1, 3, alias="phasesetting_{}_{}".format(self._serial, self._channel_id))
			)

			self._phase = self.settings.get_value(self.settings.alias("phasesetting_{}_{}".format(self._serial, self._channel_id)))
			self.service.add_item(IntegerItem('/PhaseSetting', self._phase, writeable=True, onchange=self.phase_changed))

		# Indicate when we're masquerading for another device
		self.service.add_item(IntegerItem('/IsGenericEnergyMeter', 1))
		# Meter paths
		self.service.add_item(DoubleItem('/Ac/Power', None, text= fmt['watt']))

		# a shelly with a switch may only be single-phased, but it could be mapped to any of these. 
		# thus, we need to create all 3 phase-paths, but only make use of the values (in update) the shelly
		# is actually mapped to.
		for channel in range(1, 4):
			prefix = '/Ac/L{}/'.format(channel)
			self.service.add_item(DoubleItem(prefix + 'Voltage', None, text=fmt['volt']))
			self.service.add_item(DoubleItem(prefix + 'Current', None, text=fmt['amp']))
			self.service.add_item(DoubleItem(prefix + 'Power', None, text=fmt['watt']))
			self.service.add_item(DoubleItem(prefix + 'PowerFactor', None))

		return _em_role

	async def phase_changed(self, item, value):
		if not 1 <= value <= 3:
			return False

		self._phase = value
		await self.settings.set_value(self.settings.alias("phasesetting_{}_{}".format(self._serial, self._channel_id)), value)

		# Clear values of other phases
		for i in range (1, 4):
			if i == value:
				continue
			prefix = '/Ac/L{}/'.format(i)
			with self.service as s:
				s[prefix + 'Voltage'] = None
				s[prefix + 'Current'] = None
				s[prefix + 'Power'] = None
				s[prefix + 'Energy/Forward'] = None
				s[prefix + 'Energy/Reverse'] = None
				s[prefix + 'PowerFactor'] = None

		await self.force_update()
		item.set_local_value(value)
		return True

	async def force_update(self):
		status = await self.rpc_call(f'{self._rpc_device_type}.GetStatus', {"id": self._channel_id})
		if status is not None:
			self.update(status)

	def role_instance(self, value):
		val = value.split(':')
		return val[0], int(val[1])

	def role_changed(self, val):
		if val not in self.allowed_em_roles:
			return False

		p = self.settings.alias('instance_{}_{}'.format(self._serial, self._channel_id))
		role, instance = self.role_instance(self.settings.get_value(p))
		self.settings.set_value_async(p, "{}:{}".format(val, instance))

		task = asyncio.get_event_loop().create_task(self.restart())
		background_tasks.add(task)
		task.add_done_callback(background_tasks.discard)
		return True

	async def position_changed(self, item, value):
		if not 0 <= value <= 2:
			return False

		await self.settings.set_value(self.settings.alias("position_{}_{}".format(self._serial, self._channel_id)), value)
		item.set_local_value(value)
		return True

# EM handler, puts voltage, current, power measurements on dbus.
@register_handler('EM', kind=HANDLER_KIND_FUNCTIONAL)
class ShellyHandler_em(ShellyHandler, Shelly_EM_base):
	_rpc_device_type = "EM"

	async def ainit(self):
		self._num_phases = await self.get_num_phases()
		role = await self.init_em(self._num_phases, ['acload', 'pvinverter', 'genset'])
		self.set_service_name(role)
		await self.force_update()

	async def get_num_phases(self):
		status = await self.rpc_call(f"{self._rpc_device_type}.GetStatus" , {"id": self._channel_id})
		if status is not None:
			return sum([f'{i}_voltage' in status for i in ['a','b','c']])
		return 0

	def update(self, status_json):
		power = 0
		try:
			with self.service as s:
				for l in range(1, self._num_phases + 1):
					em_prefix = f"/Ac/L{l}/"
					p = {1:'a', 2:'b', 3:'c'}.get(l)
					s[em_prefix + 'Voltage'] = status_json[f"{p}_voltage"]
					s[em_prefix + 'Current'] = status_json[f"{p}_current"]
					power += status_json[f"{p}_act_power"]
					s[em_prefix + 'Power'] = status_json[f"{p}_act_power"]
					s[em_prefix + 'PowerFactor'] = status_json[f"{p}_pf"]
				s['/Ac/Power'] = power
		except KeyError as e:
			logger.error("KeyError in update: %s", e)
			pass

# Mixin class for single-phase shelly devices with energy metering capabilities
# In these devices, the energy metering data is reported in the same RPC method as the switch status, which can be 'Switch, 'Light', etc.
class ShellySinglePhaseEmMixin:
	def update_em(self, status_json, phase):
		try:
			with self.service as s:
				#a shelly with a switch is single phased. But it may be connected to either phase. 
				#so, report values on the proper phase.
				em_prefix = "/Ac/L{}/".format(phase)
				s[em_prefix + 'Voltage'] = status_json["voltage"]
				s[em_prefix + 'Current'] = status_json["current"]
				s[em_prefix + 'Power'] = status_json["apower"]
				s[em_prefix + 'PowerFactor'] = status_json["pf"] if 'pf' in status_json else None
				# Shelly reports energy in Wh, so convert to kWh
				eforward = status_json["aenergy"]["total"] / 1000 if 'aenergy' in status_json else None
				ereverse = status_json["ret_aenergy"]["total"] / 1000 if 'ret_aenergy' in status_json else None
				s[em_prefix + 'Energy/Forward'] = eforward
				s[em_prefix + 'Energy/Reverse'] = ereverse

				s['/Ac/Energy/Forward'] = eforward
				s['/Ac/Energy/Reverse'] = ereverse
				s['/Ac/Power'] = status_json["apower"]

		except KeyError as e:
			logger.error("KeyError in update: %s", e)
			pass

class ShellyHandler_switch_base(ShellyHandler, Shelly_EM_base, ShellySinglePhaseEmMixin):
	_default_output_type = OutputType.TOGGLE
	_valid_types_mask = int((1 << OutputType.TOGGLE.value) | (1 << OutputType.MOMENTARY.value))
	_valid_functions_mask = int(1 << OutputFunction.MANUAL)

	_service_type_no_em = "switch"
	_service_type_with_em = "acload"

	_rpc_device_type = "Switch"

	async def ainit(self):
		base = self._settings_base + '%s/' % self._channel_id
		self.output_type = self._default_output_type
		self._has_em = False
		await self.settings.add_settings(
			Setting(base + 'Group', "", alias=f'Group_{self._serial}_{self._channel_id}'),
			Setting(base + 'ShowUIControl', 1, _min=0, _max=6, alias=f'ShowUIControl_{self._serial}_{self._channel_id}'),
			Setting(base + 'Function', int(OutputFunction.MANUAL), _min=0, _max=6, alias=f'Function_{self._serial}_{self._channel_id}'),
			Setting(base + 'Type', self.output_type, _min=0, _max=OutputType.TYPE_MAX, alias=f'Type_{self._serial}_{self._channel_id}'),
		)

		initial_custom_name = await self._get_channel_customname()

		path_base  = '/SwitchableOutput/%s/' % self._channel_id
		self.service.add_item(IntegerItem(path_base + 'State', 0, writeable=True, onchange=self.set_state))
		self.service.add_item(IntegerItem(path_base + 'Status', 0, writeable=False, text=self._status_text_callback))
		self.service.add_item(TextItem(path_base + 'Name', f'Channel {self._channel_id}', writeable=False))

		self.service.add_item(TextItem(path_base + 'Settings/Group', "", writeable=True, onchange=partial(self._value_changed, path_base + 'Settings/Group')))
		self.service.add_item(TextItem(path_base + 'Settings/CustomName', initial_custom_name, writeable=True, onchange=self.set_channel_name))
		self.service.add_item(IntegerItem(path_base + 'Settings/ShowUIControl', 1, writeable=True, onchange=partial(self._value_changed, path_base + 'Settings/ShowUIControl')))
		self.service.add_item(IntegerItem(path_base + 'Settings/Type', self.output_type, writeable=True, onchange=partial(self._value_changed, path_base + 'Settings/Type'),
							text=self._type_text_callback))
		self.service.add_item(IntegerItem(path_base + 'Settings/Function', int(OutputFunction.MANUAL), writeable=True, onchange=partial(self._value_changed, path_base + 'Settings/Function'),
							text=self._function_text_callback))

		self.service.add_item(IntegerItem(path_base + 'Settings/ValidTypes', self._valid_types_mask,
							writeable=False, text=self._valid_types_text_callback))
		self.service.add_item(IntegerItem(path_base + 'Settings/ValidFunctions', int(self._valid_functions_mask), writeable=False,
							text=self._valid_functions_text_callback))

		self._restore_settings(self._channel_id)

		status = await self.request_channel_status(self._channel_id)
		if status is not None and 'aenergy' in status:
			# Add energy metering paths
			await self.init_em(1, ['acload'])
			self._has_em = True

		self.set_service_name(self._service_type_with_em if self._has_em else self._service_type_no_em)

	def update(self, status_json):
		try:
			switch_prefix = f'/SwitchableOutput/{self._channel_id}/'
			status = STATUS_ON if status_json["output"] else STATUS_OFF
			with self.service as s:
				s[switch_prefix + 'State'] = 1 if status == STATUS_ON else 0
				s[switch_prefix + 'Status'] = status
		except:
			pass

		if self._has_em:
			self.update_em(status_json, self._phase or 1)

	async def _get_channel_customname(self):
		config = await self.request_channel_config(self._channel_id)
		if config is not None and 'name' in config and config['name']:
			return config['name']
		return f'Channel {self._channel_id + 1}'

	async def set_channel_name(self, item, value):
		if value is not None:
			logger.debug("Setting channel name for shelly device %s channel %d to: %s", self._serial, self._channel_id, value)
			await self.rpc_call(f"{self._rpc_device_type}.SetConfig", {"id": self._channel_id, "config": {"name": value}})
			item.set_local_value(value)

	async def request_channel_status(self, channel):
		return await self.rpc_call(f'{self._rpc_device_type}.GetStatus' , {"id": channel})

	async def request_channel_config(self, channel):
		return await self.rpc_call(f"{self._rpc_device_type}.GetConfig", {"id": channel})

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

	async def _value_changed(self, path, item, value):
		split = path.split('/')
		if len(split) > 3 and split[3] == 'Settings':
			if split[-1] == 'Type':
				if not self._set_channel_type(split[-3], value):
					return
			elif split[-1] == 'Function':
				if not self._set_channel_function(split[-3], value):
					return
			elif split[-1] == 'ShowUIControl':
				if value > 6 or value < 0:
					return
			setting = split[-1] + '_' + self._serial + '_' + split[-3]
			try:
				await self.settings.set_value(self.settings.alias(setting), value)
			except :
				return
			item.set_local_value(value)

	def _set_channel_type(self, channel, value):
		if value < 0 or value > OutputType.TYPE_MAX:
			return False
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

	def on_channel_type_changed(self, channel, value):
		pass

	def on_channel_function_changed(self, channel, value):
		pass

	async def set_state(self, item, value):
		if value not in (0, 1):
			return

		await self.rpc_call(
			f'{self._rpc_device_type}.Set',
			{
				# id is the switch channel, starting from 0
				"id":self._channel_id,
				"on":True if value == 1 else False,
			}
		)

		item.set_local_value(value)

@register_handler('Switch')
class ShellyHandler_switch(ShellyHandler_switch_base):
	_rpc_device_type = "Switch"


class ThrottledUpdaterMixin:
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

@register_handler('Light')
class ShellyHandler_light(ShellyHandler_switch_base, ThrottledUpdaterMixin):
	_rpc_device_type = "Light"
	_default_output_type = OutputType.DIMMABLE
	_valid_types_mask = int(1 << OutputType.DIMMABLE.value)

	async def ainit(self):
		self._desired_value = 0
		self._throttling_lock = asyncio.Lock()
		self._throttling_runner_lock = asyncio.Lock()

	async def _ainit_extras(self, path_base):
		self.service.add_item(IntegerItem(path_base + 'Dimming', 0, writeable=True,
			onchange=partial(self.throttled_updater, self._set_dimming_value),
			text=lambda y: str(y) + '%'))

	def update(self, status_json):
		super().update(status_json)
		if self._throttling_runner_lock.locked():
			# A throttled update is in progress, don't override Dimming path
			return
		try:
			switch_prefix = f'/SwitchableOutput/{self._channel_id}/'
			with self.service as s:
				s[switch_prefix + 'Dimming'] = status_json.get("brightness", 0)
		except:
			pass

	async def _set_dimming_value(self, item, value):
		if value is None or value < 0 or value > 100:
			return

		value = int(value)
		await self.rpc_call(
			f'{self._rpc_device_type}.Set',
			{
				"id": self._channel_id,
				"on": value > 0,
				"brightness": value,
			}
		)
		item.set_local_value(value)
