import asyncio

try:
	from dbus_fast.aio import MessageBus
except ImportError:
	from dbus_next.aio import MessageBus

from aiovelib.service import Service, IntegerItem, TextItem
from aiovelib.localsettings import Setting

from switch import SwitchDevice, MODULE_STATE_CONNECTED
from energymeter import EnergyMeter

from __main__ import VERSION, __file__ as processName
from utils import logger, wait_for_settings, formatters as fmt

background_tasks = set()

class ShellyChannel(SwitchDevice, EnergyMeter, object):

	@classmethod
	async def create(cls, bus_type=None, serial=None, channel_id=0, rpc_device_type=None, has_em=False,
				server=None, restart=None, rpc_callback=None, productid=0x0000, productName=None):
		bus = await MessageBus(bus_type=bus_type).connect()
		c = cls(bus, productid, serial, channel_id, server, restart, rpc_callback, rpc_device_type, has_em, productName)
		c.settings = await wait_for_settings(bus)

		role = 'acload' if c._has_em else 'switch'
		await c.settings.add_settings(
			Setting(c._settings_base + 'ClassAndVrmInstance', f'{role}:50', alias=f'instance_{c._serial}_{c._channel_id}'),
			Setting(c._settings_base + 'CustomName', "", alias=f'customname_{c._serial}_{c._channel_id}'),
		)

		return c

	def __init__(self, bus, productid, serial, channel_id, connection, restart, rpc_callback, rpc_device_type, has_em, productName):
		self.service = None
		self.settings = None
		self._productId = productid
		self._serial = serial
		self._channel_id = channel_id
		self.bus = bus
		self.connection = connection
		self._restart = restart
		self._em_role = None
		self._rpc_device_type = rpc_device_type
		self._has_dimming = rpc_device_type == 'Light'
		self._has_switch = rpc_device_type == 'Switch'
		self._has_rgb = rpc_device_type == 'RGB'
		self._has_rgbw = rpc_device_type == 'RGBW'
		self._has_em = has_em
		self._rpc_call = rpc_callback
		self.productName = productName
		self._throttling_lock = asyncio.Lock()
		self._throttling_runner_lock = asyncio.Lock()
		self._desired_value = None

		# We don't know the service type yet. Will be .acload if shelly supports energy metering, otherwise .switch.
		# If the shelly does not support switching, it may be acload, pvinverter or genset.
		self.serviceName = ''
		self._settings_base = f'/Settings/Devices/shelly_{self._serial}_{self._channel_id}/'

	async def init(self):
		# Set up the service name
		stype = self._em_role if self._has_em else 'switch'
		self.set_service_type(stype)
		self.serviceName = f'com.victronenergy.{stype}.shelly_{self._serial}_{self._channel_id}'

		self.service = Service(self.bus, self.serviceName)

		self.service.add_item(TextItem('/Mgmt/ProcessName', processName))
		self.service.add_item(TextItem('/Mgmt/ProcessVersion', VERSION))
		self.service.add_item(TextItem('/Mgmt/Connection', self.connection))
		self.service.add_item(IntegerItem('/ProductId', self._productId, text=fmt['productid']))
		self.service.add_item(TextItem('/ProductName', self.productName))
		self.service.add_item(IntegerItem('/Connected', 1))
		self.service.add_item(TextItem('/Serial', self._serial))
		initial_custom_name = await self._get_device_customname()
		self.service.add_item(TextItem('/CustomName', initial_custom_name, writeable=True, onchange=self.set_customname))
		self.service.add_item(IntegerItem('/State', MODULE_STATE_CONNECTED))
		self.service.add_item(IntegerItem('/DeviceInstance', int(self.settings.get_value(self.settings.alias(f'instance_{self._serial}_{self._channel_id}')).split(':')[-1])))

	# Reinitialize the callbacks without restarting the service.
	async def reinit(self, rpc_callback, restart):
		self._rpc_call = rpc_callback
		self._restart = restart

		# Update the service with the latest status
		await self.force_update()
		await self._set_device_customname()

	async def force_update(self):
		status = await self._rpc_call(f'{self._rpc_device_type if self._rpc_device_type else "EM"}.GetStatus', {"id": self._channel_id})
		if status is not None:
			self.update(status)

	def disable(self):
		if self.service.get_item(f'/SwitchableOutput/{self._channel_id}/Status') is not None:
			with self.service as s:
				s['/SwitchableOutput/%s/Status' % self._channel_id] = 0x20 # Disabled

	async def stop(self):
		if self.service is not None:
			await self.service.close()
		else:
			logger.warning("ShellyChannel service is None, cannot stop")

		self.service = None
		self.settings = None

	async def start(self):
		await self.service.register()

	async def restart(self):
		await self._restart()

	def set_service_type(self, _stype):
		setting = self.settings.get_value(self.settings.alias(f'instance_{self._serial}_{self._channel_id}'))
		if setting is None:
			logger.warning(f'No instance setting found for {self._serial}, setting default to switch:50')
			return
		stype, instance = self.role_instance(setting)

		if stype != _stype:
			p = self.settings.alias(f'instance_{self._serial}_{self._channel_id}')
			role, instance = self.role_instance(self.settings.get_value(p))
			self.settings.set_value_async(p, f'{_stype}:{instance}')

	def role_instance(self, value):
		val = value.split(':')
		return val[0], int(val[1])

	async def set_customname(self, item, value):
		if value is not None:
			logger.debug("Setting device name for shelly device %s to: %s", self._serial, value)
			item.set_local_value(value)
			await self._rpc_call("Sys.SetConfig", {"config": {"device": {"name": value}}})

	async def value_changed(self, item, value):
		""" Handle a value change from the settings service. """
		return await super().value_changed(item, value)

	def channel_config_changed(self):
		t1 = asyncio.create_task(self._set_channel_customname())
		t2 = asyncio.create_task(self._set_device_customname())
		background_tasks.add(t1)
		background_tasks.add(t2)
		t1.add_done_callback(background_tasks.discard)
		t2.add_done_callback(background_tasks.discard)

		# TODO: other things to handle?

	async def _set_device_customname(self):
		name = await self._get_device_customname()
		with self.service as s:
			s['/CustomName'] = name

	async def _get_device_customname(self):
		config = await self.request_device_config()
		if config is not None and 'device' in config and 'name' in config['device'] and config['device']['name']:
			return config['device']['name']
		return f'Shelly {self._serial}'

	async def _set_channel_customname(self):
		if self.service.get_item(f'/SwitchableOutput/{self._channel_id}/Settings/CustomName') is None:
			return
		name = await self._get_channel_customname()
		with self.service as s:
			s[f'/SwitchableOutput/{self._channel_id}/Settings/CustomName'] = name

	async def _get_channel_customname(self):
		config = await self.request_channel_config(self._channel_id)
		if config is not None and 'name' in config and config['name']:
			return config['name']
		return f'Channel {self._channel_id + 1}'

	async def request_device_config(self):
		return await self._rpc_call("Sys.GetConfig", {})

	async def request_channel_config(self, channel):
		return await self._rpc_call(f"{self._rpc_device_type}.GetConfig" if self._rpc_device_type is not None else "EM.GetConfig", {"id": channel})

	def update(self, status_json):
		""" Update the service with new values. """
		if not self.service:
			return

		SwitchDevice.update(self, status_json)
		EnergyMeter.update(self, status_json)
