import asyncio
from aiovelib.service import Service, IntegerItem, DoubleItem, TextItem
from aiovelib.service import TextArrayItem

from utils import logger, formatters as fmt
background_tasks = set()

class EnergyMeter(object):

	async def init_em(self, allowed_roles):
		self.allowed_em_roles = allowed_roles
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

		# Meter paths
		self.service.add_item(DoubleItem('/Ac/Energy/Forward', None, text=fmt['kwh']))
		self.service.add_item(DoubleItem('/Ac/Energy/Reverse', None, text=fmt['kwh']))
		self.service.add_item(DoubleItem('/Ac/Power', None, text= fmt['watt']))

	def add_em_channel(self, channel):
		prefix = '/Ac/L{}/'.format(channel + 1)
		self.service.add_item(DoubleItem(prefix + 'Voltage', None, text=fmt['volt']))
		self.service.add_item(DoubleItem(prefix + 'Current', None, text=fmt['amp']))
		self.service.add_item(DoubleItem(prefix + 'Power', None, text=fmt['watt']))
		self.service.add_item(DoubleItem(prefix + 'Energy/Forward', None, text=fmt['kwh']))
		self.service.add_item(DoubleItem(prefix + 'Energy/Reverse', None, text=fmt['kwh']))
		self.service.add_item(DoubleItem(prefix + 'PowerFactor', None))

	def update(self, values):
		if not self._has_em:
			return
		eforward = 0
		ereverse = 0
		power = 0

		def get_value(path):
			i = self.service.get_item(path)
			return i.value or 0 if i is not None else 0

		for l in range(1,3):
			eforward += get_value('/Ac/L{}/Energy/Forward'.format(l))
			ereverse += get_value('/Ac/L{}/Energy/Reverse'.format(l))
			power += get_value('/Ac/L{}/Power'.format(l))

		with self.service as s:
			s['/Ac/Energy/Forward'] = eforward
			s['/Ac/Energy/Reverse'] = ereverse
			s['/Ac/Power'] = power

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

	async def restart(self):
		raise NotImplementedError("Restart method not implemented for EnergyMeter")