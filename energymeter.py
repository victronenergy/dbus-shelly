import asyncio
from aiovelib.service import Service, IntegerItem, DoubleItem, TextItem
from aiovelib.service import TextArrayItem
from aiovelib.localsettings import Setting

from utils import logger, formatters as fmt
background_tasks = set()

class EnergyMeter(object):

	async def init_em(self, num_phases, allowed_roles):
		self._num_phases = num_phases
		self.allowed_em_roles = allowed_roles
		self._phase = None
		# Determine role and instance
		self._em_role, instance = self.role_instance(
			self.settings.get_value(self.settings.alias('instance_{}_{}'.format(self._serial, self._channel_id))))

		if self._em_role not in self.allowed_em_roles:
			logger.warning("Role {} not allowed for shelly energy meter, resetting to {}".format(self._em_role, self.allowed_em_roles[0]))
			self._em_role = self.allowed_em_roles[0]
			await self.settings.set_value(self.settings.alias('instance_{}_{}'.format(self._serial, self._channel_id)), "{}:{}".format(self._em_role, instance))

	async def setup_em(self):
		self.service.add_item(TextItem('/Role', self._em_role, writeable=True,
			onchange=self.role_changed))
		self.service.add_item(TextArrayItem('/AllowedRoles', self.allowed_em_roles, writeable=False))

		await self.settings.add_settings(
			Setting(self._settings_base + '%s/' % self._channel_id + 'Position', 0, 0, 2, alias="position_{}_{}".format(self._serial, self._channel_id))
		)

		self.service.add_item(IntegerItem('/Position', self.settings.get_value(self.settings.alias("position_{}_{}".format(self._serial, self._channel_id))),
				writeable=True, onchange=self.position_changed))

		if self._num_phases == 1:
			await self.settings.add_settings(
				Setting(self._settings_base + '%s/' % self._channel_id + 'PhaseSetting', 1, 1, 3, alias="phasesetting_{}_{}".format(self._serial, self._channel_id))
			)

			self._phase = self.settings.get_value(self.settings.alias("phasesetting_{}_{}".format(self._serial, self._channel_id)))
			self.service.add_item(IntegerItem('/PhaseSetting', self._phase, writeable=True, onchange=self.value_changed))

		# Indicate when we're masquerading for another device
		if self._em_role != "grid":
			self.service.add_item(IntegerItem('/IsGenericEnergyMeter', 1))

		# Meter paths
		self.service.add_item(DoubleItem('/Ac/Energy/Forward', None, text=fmt['kwh']))
		self.service.add_item(DoubleItem('/Ac/Energy/Reverse', None, text=fmt['kwh']))
		self.service.add_item(DoubleItem('/Ac/Power', None, text= fmt['watt']))

		for channel in range(1, 4):
			prefix = '/Ac/L{}/'.format(channel)
			self.service.add_item(DoubleItem(prefix + 'Voltage', None, text=fmt['volt']))
			self.service.add_item(DoubleItem(prefix + 'Current', None, text=fmt['amp']))
			self.service.add_item(DoubleItem(prefix + 'Power', None, text=fmt['watt']))
			self.service.add_item(DoubleItem(prefix + 'Energy/Forward', None, text=fmt['kwh']))
			self.service.add_item(DoubleItem(prefix + 'Energy/Reverse', None, text=fmt['kwh']))
			self.service.add_item(DoubleItem(prefix + 'PowerFactor', None))

	def update(self, status_json):
		if self._has_em:
			eforward = 0
			ereverse = 0
			power = 0

			try:
				with self.service as s:
					if self._has_switch or self._has_dimming:
						em_prefix = f"/Ac/L{self._phase}/"
						s[em_prefix + 'Voltage'] = status_json["voltage"]
						s[em_prefix + 'Current'] = status_json["current"]
						s[em_prefix + 'Power'] = status_json["apower"]
						s[em_prefix + 'PowerFactor'] = status_json["pf"] if 'pf' in status_json else None
						# Shelly reports energy in Wh, so convert to kWh
						s[em_prefix + 'Energy/Forward'] = status_json["aenergy"]["total"] / 1000 if 'aenergy' in status_json else None
						s[em_prefix + 'Energy/Reverse'] = status_json["ret_aenergy"]["total"] / 1000 if 'ret_aenergy' in status_json else None
					else:
						for l in range(1, self._num_phases + 1):
							em_prefix = f"/Ac/L{l}/"
							p = {1:'a', 2:'b', 3:'c'}.get(l)
							s[em_prefix + 'Voltage'] = status_json[f"{p}_voltage"]
							s[em_prefix + 'Current'] = status_json[f"{p}_current"]
							s[em_prefix + 'Power'] = status_json[f"{p}_act_power"]
							s[em_prefix + 'PowerFactor'] = status_json[f"{p}_pf"]
			except KeyError as e:
				logger.error("KeyError in update: %s", e)
				pass

			def get_value(path):
				i = self.service.get_item(path)
				return i.value or 0 if i is not None else 0

			for l in range(1, 4):
				eforward += get_value(f'/Ac/L{l}/Energy/Forward')
				ereverse += get_value(f'/Ac/L{l}/Energy/Reverse')
				power += get_value(f'/Ac/L{l}/Power')

			with self.service as s:
				s['/Ac/Energy/Forward'] = eforward
				s['/Ac/Energy/Reverse'] = ereverse
				s['/Ac/Power'] = power

	def update_energies(self, emdata):
		try:
			with self.service as s:
				if self._phase is None:
					for l in range(1, self._num_phases + 1):
						em_prefix = f'/Ac/L{l}/'
						p = {1:'a', 2:'b', 3:'c'}.get(l)
						s[em_prefix + 'Energy/Forward'] = emdata[f'{p}_total_act_energy'] / 1000
						s[em_prefix + 'Energy/Reverse'] = emdata[f'{p}_total_act_ret_energy'] / 1000
				else:
					em_prefix = f'/Ac/L{self._phase}/'
					p = {1:'a', 2:'b', 3:'c'}.get(self._phase)
					s[em_prefix + 'Energy/Forward'] = emdata[f'{p}_total_act_energy'] / 1000
					s[em_prefix + 'Energy/Reverse'] = emdata[f'{p}_total_act_ret_energy'] / 1000
		except:
			pass

	def role_changed(self, val):
		if val not in self.allowed_em_roles:
			return False

		p = self.settings.alias('instance_{}_{}'.format(self._serial, self._channel_id))
		role, instance = self.role_instance(self.settings.get_value(p))
		self.settings.set_value_async(p, "{}:{}".format(val, instance))
		self._em_role = val

		task = asyncio.get_event_loop().create_task(self._restart())
		background_tasks.add(task)
		task.add_done_callback(background_tasks.discard)
		return True

	async def value_changed(self, item, value):
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

	async def position_changed(self, item, value):
		if not 0 <= value <= 2:
			return False

		await self.settings.set_value(self.settings.alias("position_{}_{}".format(self._serial, self._channel_id)), value)
		item.set_local_value(value)
		return True

	async def restart(self):
		raise NotImplementedError("Restart method not implemented for EnergyMeter")