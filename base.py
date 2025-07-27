try:
	from dbus_fast.aio import MessageBus
except ImportError:
	from dbus_next.aio import MessageBus

from aiovelib.service import Service, IntegerItem, TextItem
from aiovelib.localsettings import Setting

from switch import SwitchDevice, MODULE_STATE_CONNECTED
from energymeter import EnergyMeter

from __main__ import VERSION
from utils import logger, wait_for_settings, formatters as fmt

class ShellyChannel(SwitchDevice, EnergyMeter, object):

	@classmethod
	async def create(cls, bus_type=None, serial=None, channel=0, has_em=False, has_switch=False, server=None, restart=None, version=VERSION, productid=0x0000, productName="Shelly switch", processName=__file__):
		bus = await MessageBus(bus_type=bus_type).connect()
		c = cls(bus, productid, serial, channel, version, server, restart, has_em, has_switch, productName, processName)
		c.settings = await wait_for_settings(bus)

		role = 'acload' if c._has_em else 'switch'
		await c.settings.add_settings(
			Setting(c._settings_base + 'ClassAndVrmInstance'.format(c._serial, c._channel), '{}:50'.format(role), alias='instance_{}_{}'.format(c._serial, c._channel)),
			Setting(c._settings_base + 'CustomName'.format(c._serial, c._channel), "", alias="customname_{}_{}".format(c._serial, c._channel)),
		)

		return c

	@property
	def status(self):
		return self.service.get_item(f'/SwitchableOutput/{self._channel}/Status').value

	@property
	def state(self):
		return self.service.get_item(f'/SwitchableOutput/{self._channel}/State').value

	@state.setter
	def state(self, value):
		""" Set the state of the switch. """
		self.service.get_item(f'/SwitchableOutput/{self._channel}/State')._set_value(value)

	@property
	def serial(self):
		return self._serial

	def __init__(self, bus, productid, serial, channel, version, connection, restart, has_em, has_switch, productName, processName):
		self.service = None
		self.settings = None
		self._productId = productid
		self._serial = serial
		self._channel = channel
		self.bus = bus
		self.version = version
		self.connection = connection
		self._restart = restart
		self._em_role = None
		self._has_em = has_em
		self._has_switch = has_switch
		self.productName = productName
		self.processName = processName

		# We don't know the service type yet. Will be .acload if shelly supports energy metering, otherwise .switch.
		# If the shelly does not support switching, it may be acload, pvinverter or genset.
		self.serviceName = ''
		self._settings_base = '/Settings/Devices/shelly_{}_{}/'.format(self._serial, self._channel)

	async def init(self):
		# Set up the service name
		stype = self._em_role if self._has_em else 'switch'
		self.set_service_type(stype)
		self.serviceName = "com.victronenergy.{}.shelly_{}_{}".format(stype, self._serial, self._channel)

		self.service = Service(self.bus, self.serviceName)

		self.service.add_item(TextItem('/Mgmt/ProcessName', self.processName))
		self.service.add_item(TextItem('/Mgmt/ProcessVersion', self.version))
		self.service.add_item(TextItem('/Mgmt/Connection', self.connection))
		self.service.add_item(IntegerItem('/ProductId', self._productId, text=fmt['productid']))
		self.service.add_item(TextItem('/ProductName', self.productName))
		self.service.add_item(IntegerItem('/Connected', 1))
		self.service.add_item(TextItem('/Serial', self._serial))
		self.service.add_item(TextItem('/CustomName', self.settings.get_value(self.settings.alias("customname_{}_{}".format(self._serial, self._channel))), writeable=True, onchange=self._set_customname))
		self.service.add_item(IntegerItem('/State', MODULE_STATE_CONNECTED))
		self.service.add_item(IntegerItem('/DeviceInstance', int(self.settings.get_value(self.settings.alias('instance_{}_{}'.format(self._serial, self._channel))).split(':')[-1])))

	def stop(self):
		if self.service is not None:
			self.service.__del__()
		else:
			logger.warning("ShellyChannel service is None, cannot stop")

		self.service = None
		self.settings = None

	async def start(self):
		await self.service.register()

	async def restart(self):
		await self._restart()

	@property
	def customname(self):
		return self.service.get_item("/CustomName").value

	@customname.setter
	def customname(self, v):
		with self.service as s:
			s["/CustomName"] = v or "Switching device"

	def set_service_type(self, _stype):
		setting = self.settings.get_value(self.settings.alias('instance_{}_{}'.format(self._serial, self._channel)))
		if setting is None:
			logger.warning("No instance setting found for {}, setting default to switch:50".format(self._serial))
			return
		stype, instance = self.role_instance(setting)

		if stype != _stype:
			p = self.settings.alias('instance_{}_{}'.format(self._serial, self._channel))
			role, instance = self.role_instance(self.settings.get_value(p))
			self.settings.set_value_async(p, "{}:{}".format(_stype, instance))

	def role_instance(self, value):
		val = value.split(':')
		return val[0], int(val[1])

	def items_changed(self, service, values):
		try:
			self.customname = values[self.settings.alias("customname_{}_{}".format(self._serial, self._channel))]
		except:
			pass # Not a customname change

	def _set_customname(self, value):
		try:
			cn = self.settings.get_value(self.settings.alias("customname_{}_{}".format(self._serial, self._channel)))
			if cn != value:
				self.settings.set_value_async(self.settings.alias("customname_{}_{}".format(self._serial, self._channel)), value)
			return True
		except:
			return False

	def value_changed(self, path, value):
		""" Handle a value change from the settings service. """
		super().value_changed(path, value)

	def update(self, status_json, phase):
		""" Update the service with new values. """
		if not self.service:
			return

		SwitchDevice.update(self, status_json)
		EnergyMeter.update(self, status_json, phase)