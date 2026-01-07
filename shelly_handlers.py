from aiovelib.localsettings import Setting
from aiovelib.service import IntegerItem, TextItem, DoubleItem, IntegerArrayItem, TextArrayItem
from utils import logger, formatters as fmt, STATUS_OFF, STATUS_ON

from functools import partial
from enum import IntEnum
import asyncio
import colorsys

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
HANDLER_KIND_SWITCH = "switch"
HANDLER_KIND_EM = "em"
HANDLER_KIND_GENERIC = "generic"

_HANDLER_REGISTRY = {}

def register_handler(*capabilities, kind):
	def _decorator(handler_cls):
		for cap in capabilities:
			_HANDLER_REGISTRY[cap] = {"cls": handler_cls, "kind": kind}
		handler_cls._rpc_device_type = list(capabilities) if len(capabilities) > 1 else capabilities[0]
		return handler_cls
	return _decorator

def get_handler_class(capability, kind=None):
	info = _HANDLER_REGISTRY.get(capability)
	return info["cls"] if info and (kind is None or info["kind"] == kind or info["kind"] == HANDLER_KIND_GENERIC) else None

def get_handler_kind(capability):
	info = _HANDLER_REGISTRY.get(capability)
	return info["kind"] if info else None

def has_functional_handler(capabilities):
	for cap in capabilities:
		if get_handler_kind(cap) == HANDLER_KIND_SWITCH or get_handler_kind(cap) == HANDLER_KIND_EM:
			return True
	return False

# The shelly handler base class implements basic functionality common to all shelly capability handlers.
# These methods apply to channels of all devices
class ShellyHandler(object):
	_rpc_device_type = ""

	@classmethod
	async def create(cls, cap, rpc_callback=None, restart_callback=None, shelly_channel=None):
		handler_cls = get_handler_class(cap)
		if handler_cls is None:
			return None

		c = handler_cls()
		c._channel_id = getattr(shelly_channel, "_channel_id", 0)
		c._rpc_call = rpc_callback
		c.restart = restart_callback
		c.sc = shelly_channel
		c.service = getattr(shelly_channel, "service", None)
		c.settings = getattr(shelly_channel, "settings", None)
		c._serial = getattr(shelly_channel, "_serial", None)
		c._settings_base = f'/Settings/Devices/shelly_{c._serial}_{c._channel_id}/'
		await c.ainit()

		return c

	def __init__(self):
		self._channel_id = 0
		self._rpc_call = None
		self.restart = None
		self.sc = None
		self.service = None
		self.settings = None
		self._serial = None
		self._settings_base = None

	async def ainit(self):
		pass

	async def restart(self):
		pass

	def set_service_name(self, type):
		self.service.name = f"com.victronenergy.{type}.shelly_{self._serial}_{self._channel_id}"

	async def rpc_call(self, method, params, fun=None):
		is_single = isinstance(self._rpc_device_type, str)
		rpc_types = [self._rpc_device_type] if is_single else self._rpc_device_type
		results = {}
		for rpc in rpc_types:
			resp = await self._rpc_call(f"{rpc}.{method}", params)
			if fun:
				fun(resp, cap=rpc.lower())
			results[rpc] = resp
		return results[rpc_types[0]] if is_single else results
	
	# Override this in the handlers
	def update(self, status_json, cap=None):
		pass

# Temperature handler, puts temperature readings on dbus.
@register_handler('Temperature', kind=HANDLER_KIND_GENERIC)
class ShellyHandler_temperature(ShellyHandler):
	async def ainit(self):
		# Temperature path must be updated.
		self.service.add_item(DoubleItem(f'/Temperature', None, text=fmt['celsius']))

	def update(self, status_json, cap=None):
		try:
			with self.service as s:
				s[f'/Temperature'] = status_json["tC"]
		except:
			pass

# System handler, handles device custom name setting and getting.
@register_handler('Sys', kind=HANDLER_KIND_GENERIC)
class ShellyHandler_sys(ShellyHandler):
	async def ainit(self):
		self.service.add_item(TextItem('/CustomName', "", writeable=True, onchange=self.set_custom_name))
		await self._set_device_customname()

	async def set_custom_name(self, item, value):
		if value is not None:
			logger.debug("Setting device name for shelly device %s to: %s", self._serial, value)
			item.set_local_value(value)
			await self.rpc_call("SetConfig", {"config": {"device": {"name": value}}})

	async def _set_device_customname(self):
		name = await self._get_device_customname()
		with self.service as s:
			s['/CustomName'] = name

	async def _get_device_customname(self):
		config = await self.rpc_call("GetConfig", {})
		if config is not None and 'device' in config and 'name' in config['device'] and config['device']['name']:
			return config['device']['name']
		return f'Shelly {self._serial}'

	def on_event(self, event):
		if event['event'] == "config_changed":
			task = asyncio.get_event_loop().create_task(self._set_device_customname())
			background_tasks.add(task)
			task.add_done_callback(background_tasks.discard)

	async def restart(self):
		await self._set_device_customname()


class ShellyHandler_EM_paths_mixin():
	# Adds common energy metering paths to the service
	async def add_em_paths(self, num_phases):
		for channel in range(1, num_phases + 1):
			prefix = '/Ac/L{}/'.format(channel)
			self.service.add_item(DoubleItem(prefix + 'Voltage', None, text=fmt['volt']))
			self.service.add_item(DoubleItem(prefix + 'Current', None, text=fmt['amp']))
			self.service.add_item(DoubleItem(prefix + 'Power', None, text=fmt['watt']))
			self.service.add_item(DoubleItem(prefix + 'PowerFactor', None))
			self.service.add_item(DoubleItem(prefix + 'Energy/Forward', None, text=fmt['kwh']))
			self.service.add_item(DoubleItem(prefix + 'Energy/Reverse', None, text=fmt['kwh']))

		self.service.add_item(DoubleItem('/Ac/Power', None, text= fmt['watt']))
		self.service.add_item(DoubleItem('/Ac/Energy/Forward', None, text=fmt['kwh']))
		self.service.add_item(DoubleItem('/Ac/Energy/Reverse', None, text=fmt['kwh']))


# Contains common code for shelly handlers that have energy metering capabilities (single or multi-phase)
class Shelly_EM_base(ShellyHandler_EM_paths_mixin):
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

		# a shelly with a switch may only be single-phased, but it could be mapped to any of these. 
		# thus, we need to create all 3 phase-paths, but only make use of the values (in update) the shelly
		# is actually mapped to.
		await self.add_em_paths(3)

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
		await self.rpc_call('GetStatus', {"id": self._channel_id}, fun=self.update)

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
@register_handler('EM', 'EMData', kind=HANDLER_KIND_EM)
class ShellyHandler_em(Shelly_EM_base, ShellyHandler):
	async def ainit(self):
		self._num_phases = await self.get_num_phases()
		role = await self.init_em(self._num_phases, ['acload', 'pvinverter', 'genset'])
		self.set_service_name(role)
		await self.force_update()

	async def get_num_phases(self):
		status = await self.rpc_call('GetStatus', {"id": self._channel_id})
		if status is not None and 'EM' in status:
			return sum([f'{i}_voltage' in status['EM'] for i in ['a','b','c']])
		return 0

	def update(self, status_json, cap=None):
		power = 0
		try:
			with self.service as s:
				if cap == 'em':
					for l in range(1, self._num_phases + 1):
						em_prefix = f"/Ac/L{l}/"
						p = {1:'a', 2:'b', 3:'c'}.get(l)
						s[em_prefix + 'Voltage'] = status_json[f"{p}_voltage"]
						s[em_prefix + 'Current'] = status_json[f"{p}_current"]
						power += status_json[f"{p}_act_power"]
						s[em_prefix + 'Power'] = status_json[f"{p}_act_power"]
						s[em_prefix + 'PowerFactor'] = status_json[f"{p}_pf"]
					s['/Ac/Power'] = power

				if cap == 'emdata':
					for l in range(1, self._num_phases + 1):
						with self.service as s:
							em_prefix = f'/Ac/L{l}/'
							p = {1:'a', 2:'b', 3:'c'}.get(l)
							s[em_prefix + 'Energy/Forward'] = status_json[f'{p}_total_act_energy'] / 1000
							s[em_prefix + 'Energy/Reverse'] = status_json[f'{p}_total_act_ret_energy'] / 1000
					with self.service as s:
						s['/Ac/Energy/Forward'] = status_json['total_act'] / 1000
						s['/Ac/Energy/Reverse'] = status_json['total_act_ret'] / 1000
		except KeyError as e:
			logger.error("KeyError in update: %s", e)
			pass

# EM1Data handler, puts energy metering data on dbus.
# The EM1Data component is available on single-phase shelly energy meters.
@register_handler('EM1', 'EM1Data', kind=HANDLER_KIND_EM)
class ShellyHandler_em1(Shelly_EM_base, ShellyHandler):
	async def ainit(self):
		self._num_phases = 1
		role = await self.init_em(self._num_phases, ['acload', 'pvinverter', 'genset'])
		self.set_service_name(role)
		await self.force_update()

	def update(self, status_json, cap=None):
		try:
			with self.service as s:
				if cap == 'em1':
					em_prefix = "/Ac/L{}/".format(self._phase or 1)
					s[em_prefix + 'Voltage'] = status_json["voltage"]
					s[em_prefix + 'Current'] = status_json["current"]
					s[em_prefix + 'Power'] = status_json["act_power"]
					s[em_prefix + 'PowerFactor'] = status_json["pf"] if 'pf' in status_json else None
					s['/Ac/Power'] = status_json["act_power"]
				elif cap == 'em1data':
					em_prefix = "/Ac/L{}/".format(self._phase or 1)
					s['/Ac/Energy/Forward'] = s[em_prefix + 'Energy/Forward'] = status_json['total_act_energy'] / 1000
					s['/Ac/Energy/Reverse'] = s[em_prefix + 'Energy/Reverse'] = status_json['total_act_ret_energy'] / 1000

		except KeyError as e:
			logger.error("KeyError in update: %s", e)
			pass


class ShellyHandler_switch_base(ShellyHandler, Shelly_EM_base):
	_default_output_type = OutputType.TOGGLE
	_valid_types_mask = int((1 << OutputType.TOGGLE.value) | (1 << OutputType.MOMENTARY.value))
	_valid_functions_mask = int(1 << OutputFunction.MANUAL)

	_service_type_no_em = "switch"
	_service_type_with_em = "acload"

	async def ainit(self, allow_em=True):
		base = self._settings_base + '%s/' % self._channel_id
		self._type = self._default_output_type
		self._has_em = False
		await self.settings.add_settings(
			Setting(base + 'Group', "", alias=f'Group_{self._serial}_{self._channel_id}'),
			Setting(base + 'ShowUIControl', 1, _min=0, _max=6, alias=f'ShowUIControl_{self._serial}_{self._channel_id}'),
			Setting(base + 'Function', int(OutputFunction.MANUAL), _min=0, _max=6, alias=f'Function_{self._serial}_{self._channel_id}'),
			Setting(base + 'Type', self._type, _min=0, _max=OutputType.TYPE_MAX, alias=f'Type_{self._serial}_{self._channel_id}'),
		)

		initial_custom_name = await self._get_channel_customname()

		path_base  = '/SwitchableOutput/%s/' % self._channel_id
		self.service.add_item(IntegerItem(path_base + 'State', 0, writeable=True, onchange=self.set_state))
		self.service.add_item(IntegerItem(path_base + 'Status', 0, writeable=False, text=self._status_text_callback))
		self.service.add_item(TextItem(path_base + 'Name', f'Channel {self._channel_id}', writeable=False))

		self.service.add_item(TextItem(path_base + 'Settings/Group', "", writeable=True, onchange=partial(self._value_changed, path_base + 'Settings/Group')))
		self.service.add_item(TextItem(path_base + 'Settings/CustomName', initial_custom_name, writeable=True, onchange=self.set_channel_name))
		self.service.add_item(IntegerItem(path_base + 'Settings/ShowUIControl', 1, writeable=True, onchange=partial(self._value_changed, path_base + 'Settings/ShowUIControl')))
		self.service.add_item(IntegerItem(path_base + 'Settings/Type', self._type, writeable=True, onchange=partial(self._value_changed, path_base + 'Settings/Type'),
							text=self._type_text_callback))
		self.service.add_item(IntegerItem(path_base + 'Settings/Function', int(OutputFunction.MANUAL), writeable=True, onchange=partial(self._value_changed, path_base + 'Settings/Function'),
							text=self._function_text_callback))

		self.service.add_item(IntegerItem(path_base + 'Settings/ValidTypes', self._valid_types_mask,
							writeable=False, text=self._valid_types_text_callback))
		self.service.add_item(IntegerItem(path_base + 'Settings/ValidFunctions', int(self._valid_functions_mask), writeable=False,
							text=self._valid_functions_text_callback))

		self._restore_settings(self._channel_id)

		if allow_em:
			status = await self.request_channel_status()
			if status is not None and 'aenergy' in status:
				# Add energy metering paths
				await self.init_em(1, ['acload'])
				self._has_em = True
		self.set_service_name(self._service_type_with_em if self._has_em else self._service_type_no_em)

	def update(self, status_json, cap=None):
		try:
			switch_prefix = f'/SwitchableOutput/{self._channel_id}/'
			status = STATUS_ON if status_json["output"] else STATUS_OFF
			with self.service as s:
				s[switch_prefix + 'State'] = 1 if status == STATUS_ON else 0
				s[switch_prefix + 'Status'] = status
		except:
			pass

		if self._has_em:
			try:
				with self.service as s:
					#a shelly with a switch is single phased. But it may be connected to either phase. 
					#so, report values on the proper phase.
					em_prefix = "/Ac/L{}/".format(self._phase)
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

	async def _get_channel_customname(self):
		config = await self.request_channel_config()
		if config is not None and 'name' in config and config['name']:
			return config['name']
		return f'Channel {self._channel_id + 1}'

	async def set_channel_name(self, item, value):
		if value is not None:
			logger.debug("Setting channel name for shelly device %s channel %d to: %s", self._serial, self._channel_id, value)
			await self.rpc_call("SetConfig", {"id": self._channel_id, "config": {"name": value}})
			item.set_local_value(value)

	async def request_channel_status(self):
		return await self.rpc_call('GetStatus' , {"id": self._channel_id})
	async def request_channel_config(self):
		return await self.rpc_call('GetConfig', {"id": self._channel_id})

	def _restore_settings(self, channel):
		try:
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
		status_map = {
			0x00: "Off",
			0x09: "On",
			0x02: "Tripped",
			0x04: "Over temperature",
			0x01: "Powered",
			0x08: "Output fault",
			0x10: "Short fault",
			0x20: "Disabled",
		}
		return status_map.get(value, "Unknown")

	def _type_text_callback(self, value):
		type_map = {
			OutputType.MOMENTARY: "Momentary",
			OutputType.TOGGLE: "Toggle",
			OutputType.DIMMABLE: "Dimmable",
			OutputType.RGB: "RGB",
			OutputType.RGBW: "RGBW",
		}
		return type_map.get(value, "Unknown")

	def _function_text_callback(self, value):
		function_map = {
			OutputFunction.ALARM: "Alarm",
			OutputFunction.GENSET_START_STOP: "Genset start stop",
			OutputFunction.MANUAL: "Manual",
			OutputFunction.TANK_PUMP: "Tank pump",
			OutputFunction.TEMPERATURE: "Temperature",
			OutputFunction.CONNECTED_GENSET_HELPER_RELAY: "Connected genset helper relay",
			OutputFunction.S2_RM: "S2 resource manager",
		}
		return function_map.get(value, "Unknown")

	def _bitmask_text(self, value, entries):
		parts = []
		for flag, label in entries:
			if value & (1 << flag):
				parts.append(label)
		return ", ".join(parts)

	def _valid_functions_text_callback(self, value):
		entries = [
			(OutputFunction.ALARM, "Alarm"),
			(OutputFunction.GENSET_START_STOP, "Genset start stop"),
			(OutputFunction.MANUAL, "Manual"),
			(OutputFunction.TANK_PUMP, "Tank pump"),
			(OutputFunction.TEMPERATURE, "Temperature"),
			(OutputFunction.CONNECTED_GENSET_HELPER_RELAY, "Connected genset helper relay"),
			(OutputFunction.S2_RM, "S2 resource manager"),
		]
		return self._bitmask_text(value, entries)

	def _valid_types_text_callback(self, value):
		entries = [
			(OutputType.DIMMABLE, "Dimmable"),
			(OutputType.TOGGLE, "Toggle"),
			(OutputType.MOMENTARY, "Momentary"),
			(OutputType.RGB, "RGB"),
			(OutputType.RGBW, "RGBW"),
		]
		return self._bitmask_text(value, entries)

	def on_channel_type_changed(self, channel, value):
		pass

	def on_channel_function_changed(self, channel, value):
		pass

	async def set_state(self, item, value):
		if value not in (0, 1):
			return

		await self.rpc_call(
			'Set',
			{
				# id is the switch channel, starting from 0
				"id":self._channel_id,
				"on":True if value == 1 else False,
			}
		)

		item.set_local_value(value)

@register_handler('Switch', kind=HANDLER_KIND_SWITCH)
class ShellyHandler_switch(ShellyHandler_switch_base):
	pass


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

@register_handler('Light', kind=HANDLER_KIND_SWITCH)
class ShellyHandler_light(ShellyHandler_switch_base, ThrottledUpdaterMixin):
	_default_output_type = OutputType.DIMMABLE
	_valid_types_mask = int(1 << OutputType.DIMMABLE.value)

	async def ainit(self):
		await super().ainit()
		self._desired_value = 0
		self._throttling_lock = asyncio.Lock()
		self._throttling_runner_lock = asyncio.Lock()

		path_base  = '/SwitchableOutput/%s/' % self._channel_id
		self.service.add_item(IntegerItem(path_base + 'Dimming', 0, writeable=True,
			onchange=partial(self.throttled_updater, self._set_dimming_value),
			text=lambda y: str(y) + '%'))

	def update(self, status_json, cap=None):
		super().update(status_json, cap)
		if self._throttling_runner_lock.locked():
			# A throttled update is in progress, don't override Dimming path
			return
		try:
			with self.service as s:
				s[f'/SwitchableOutput/{self._channel_id}/Dimming'] = status_json.get("brightness", 0)
		except:
			pass

	async def _set_dimming_value(self, item, value):
		if value is None or value < 0 or value > 100:
			return

		value = int(value)
		await self.rpc_call(
			'Set',
			{
				"id": self._channel_id,
				"on": value > 0,
				"brightness": value,
			}
		)
		item.set_local_value(value)

# We support both the RGB and RGBW type on RGBW devices.
@register_handler('RGBW', kind=HANDLER_KIND_SWITCH)
class ShellyHandler_RGBW(ShellyHandler_switch_base, ThrottledUpdaterMixin):
	_default_output_type = OutputType.RGBW
	_valid_types_mask = int(1 << OutputType.RGB.value) | int(1 << OutputType.RGBW.value)

	async def ainit(self):
		await super().ainit(allow_em=False)
		self._desired_value = 0
		self._throttling_lock = asyncio.Lock()
		self._throttling_runner_lock = asyncio.Lock()

		path_base  = '/SwitchableOutput/%s/' % self._channel_id
		self.service.add_item(IntegerArrayItem(path_base + 'LightControls',value=[0, 0, 0, 0, 0], writeable=True,
				onchange=partial(self.throttled_updater, self._set_light_controls), text=self._light_controls_text_callback))

	def _light_controls_text_callback(self, v):
		if self._type == OutputType.RGBW:
			return "H: %.1f, S: %.1f, B: %.1f, W: %.1f" % (v[0], v[1], v[2], v[3])
		return "H: %.1f, S: %.1f, B: %.1f" % (v[0], v[1], v[2])

	def update(self, status_json, cap=None):
		super().update(status_json, cap)
		if self._throttling_runner_lock.locked():
			# A throttled update is in progress, don't override LightControls path
			return
		try:
			switch_prefix = f'/SwitchableOutput/{self._channel_id}/'
			with self.service as s:
				brightness = status_json.get("brightness", 0)
				white = status_json.get("white", 0) / 2.55 if self._type == OutputType.RGBW else 0.0
				hue, sat, val = self._rgb2hsv(status_json.get("rgb", [0, 0, 0]))
				s[switch_prefix + 'LightControls'] = [round(hue), round(sat), round(brightness), round(white), 0]
		except:
			pass

	def on_channel_type_changed(self, channel, value):
		if value == OutputType.RGB:
			# Set white channel to 0 when switching to RGB
			item = self.service.get_item(f'/SwitchableOutput/{self._channel_id}/LightControls')
			v = list(item.value)
			v[3] = 0
			self._desired_value = v
			task = asyncio.create_task(self._set_light_controls(item, v, force_white=True))
			background_tasks.add(task)
			task.add_done_callback(background_tasks.discard)

	async def _set_light_controls(self, item, value, force_white=False):
		if not self._sanity_check_values(value):
			return

		item.set_local_value(value) # Set the value here already to make the UI more responsive
		rgb = self._hsv2rgb(value, normalise=True)

		params = {
					"id": self._channel_id,
					"rgb": rgb,
					"brightness": value[2]
				}
		if force_white or self._type == OutputType.RGBW:
			params["white"] = round(value[3] * 2.55)

		await self.rpc_call(
			'Set',
			params
		)

	def _sanity_check_values(self, l):
		if not isinstance(l, list) or len(l) != 5:
			return False
		if l[0] < 0.0 or l[0] >= 360.0:		# Hue
			return False
		if l[1] < 0.0 or l[1] > 100.0:		# Saturation
			return False
		if l[2] < 0 or l[2] > 100:			# Brightness
			return False
		if l[3] < 0 or l[3] > 100:			# White brightness
			return False
		if l[4] < 0 or l[4] > 6500:			# Color temperature
			return False
		return True

	def _rgb2hsv(self, rgb):
		h, s, v = colorsys.rgb_to_hsv(rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0)
		h = h * 360.0
		s = s * 100.0
		v = v * 100.0
		# If value or saturation is zero, restore previous hue/saturation
		if v == 0.0:
			h = self._desired_value[0]
			s = self._desired_value[1]
		# Ignore the calculated hue if the saturation is very low because the calculation will
		# be very inaccurate, which will cause small jumps in hue when changing the saturation.
		elif s <= 5.0:
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
			r = round(rf * 255)
			g = round(gf * 255)
			b = round(bf * 255)
		return [r, g, b]


# RGB handler is the same as RGBW but without the white channel
@register_handler('RGB', kind=HANDLER_KIND_SWITCH)
class ShellyHandler_RGB(ShellyHandler_RGBW):
	_default_output_type = OutputType.RGB
	_valid_types_mask = int(1 << OutputType.RGB.value)
