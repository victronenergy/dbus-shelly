import aiohttp
import asyncio
import async_timeout
from functools import partial

try:
	from dbus_fast.aio import MessageBus
except ImportError:
	from dbus_next.aio import MessageBus

from aiovelib.service import Service, IntegerItem, TextItem
from aiovelib.localsettings import Setting

from aioshelly.common import ConnectionOptions
from aioshelly.rpc_device import RpcDevice, RpcUpdateType, WsServer
from aioshelly.exceptions import DeviceConnectionError

from switch import SwitchDevice, OutputFunction, OutputType, MODULE_STATE_CONNECTED
from energymeter import EnergyMeter

from __main__ import VERSION, __file__ as processName
from utils import logger, wait_for_settings, formatters as fmt

PRODUCT_ID_SHELLY_EM = 0xB034
PRODUCT_ID_SHELLY_SWITCH = 0xB074
CONNECTION_RETRIES = 10
background_tasks = set()

class ShellyConnectionError(Exception):
	pass

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
		self._has_em = has_em
		self._rpc_call = rpc_callback
		self.productName = productName
		self._dimming_lock = asyncio.Lock()
		self._dimming_task = None
		self._desired_dimming_value = None

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


# Represents a Shelly device, which can be a switch or an energy meter.
# Handles the websocket connection to the Shelly device and provides methods to control it.
# Creates an instance of ShellyChannel for each enabled channel.
class ShellyDevice(object):

	def __init__(self, bus_type=None, serial=None, server=None, event=None):
		self._bus_type= bus_type
		self._serial = serial
		self._event_obj = event
		self._shelly_device = None
		self._ws_context = None
		self._aiohttp_session = None
		self._server = server
		self._rpc_lock = asyncio.Lock()
		self._reconnect_lock = asyncio.Lock()
		self._device_lock = asyncio.Lock() # Protects device operations, enabling/disabling channels etc.
		self._rpc_device_type = None
		self._has_em = False
		self._has_dimming = False
		self._reconnecting = False
		self._channels = {}
		self._num_channels = 0

	@property
	def event(self):
		return self._event

	def set_event(self, event_str):
		if self._event_obj:
			self._event = event_str
			self._event_obj.set()

	@property
	def is_connected(self):
		return self._shelly_device and self._shelly_device.connected

	@property
	def serial(self):
		return self._serial

	@property
	def has_switch(self):
		return self._rpc_device_type == 'Switch'

	@property
	def has_dimming(self):
		return self._rpc_device_type == 'Light'

	@property
	def has_em(self):
		return self._has_em

	@property
	def active_channels(self):
		return list(self._channels.keys())

	async def stop_channel(self, ch):
		async with self._device_lock:
			logger.warning("Stopping channel %d for shelly device %s", ch, self._serial)
			if ch in self._channels.keys():
				self._channels[ch].stop()
				del self._channels[ch]

	async def connect(self):
		options = ConnectionOptions(self._server, "", "")
		self._aiohttp_session = aiohttp.ClientSession()
		self._ws_context = WsServer()

		try:
			self._shelly_device = await RpcDevice.create(self._aiohttp_session, self._ws_context, options)

			if not self._shelly_device:
				logger.warning("Failed to create shelly device")
				raise ShellyConnectionError()

			try:
				async with async_timeout.timeout(2):
					await self._shelly_device.initialize()
			except Exception:
				logger.warning("Failed to initialize shelly device %s", self._serial)
				raise ShellyConnectionError()

			if not (self._shelly_device.connected and self._shelly_device.initialized):
				logger.warning("Failed to initialize shelly device %s", self._serial)
				raise ShellyConnectionError()

			# List shelly methods
			methods = await self.list_methods()
			if len(methods) == 0:
				logger.warning("Failed to list shelly methods")
				raise ShellyConnectionError()

			if 'Switch.GetStatus' in methods:
				self._rpc_device_type = 'Switch'
			elif 'Light.GetStatus' in methods:
				self._rpc_device_type = 'Light'

			if self.has_switch or self.has_dimming:
				channels = await self.get_channels()

				if 'aenergy' in channels[0]:
					# Energy metering capabilities -> acload service
					self._has_em = True
					# Switchable AC load with EM capability can only have the acload role.
					self.allowed_em_roles = ['acload']

			# No switching capabilities, check for energy metering capabilities.
			elif 'EM.GetStatus' in methods:
				# Energy metering capabilities -> acload service
				self._has_em = True
				self._rpc_device_type = 'EM'
				# Using a shelly as grid meter is not supported because the update frequency is too low.
				self.allowed_em_roles = ['acload', 'pvinverter', 'genset']

		# No switching capabilities, check for energy metering capabilities.
			elif 'EM.GetStatus' in methods:
				# Energy metering capabilities -> acload service
				self._has_em = True
				self._rpc_device_type = 'EM'
				# Using a shelly as grid meter is not supported because the update frequency is too low.
				self.allowed_em_roles = ['acload', 'pvinverter', 'genset']

			self._num_channels = len(channels) if self.has_switch or self.has_dimming else 1

			logger.info("Shelly device %s has %d channels, supports switching: %s, energy metering: %s, dimming: %s",
				self._serial, self._num_channels, self.has_switch, self.has_em, self.has_dimming)

			return True
		except Exception:
			if self._shelly_device:
				await self._shelly_device.shutdown()
			if self._aiohttp_session:
				await self._aiohttp_session.close()
			self._shelly_device = None
			return False

	def do_reconnect(self):
		if self._reconnecting:
			return False
		self._reconnecting = True
		task = asyncio.create_task(self._reconnect())
		background_tasks.add(task)
		task.add_done_callback(background_tasks.discard)
		def clear_reconnecting(fut):
			self._reconnecting = False
		task.add_done_callback(clear_reconnecting)

	async def _reconnect(self):
		logger.info("Reconnecting to shelly device %s", self._serial)
		if self._shelly_device:
			try:
				for ch in self._channels.keys():
					self._channels[ch].disable()
				async with async_timeout.timeout(2):
					await self._shelly_device.shutdown()
					await self._aiohttp_session.close()
			except Exception:
				pass
		self._shelly_device = None
		# Try reconnecting a few times
		for i in range(CONNECTION_RETRIES):
			if await self.ping_shelly() and self._shelly_device.initialized:
				break
			logger.info("Attempting to reconnect to shelly device %s (%d/%d)", self._serial, i + 1, CONNECTION_RETRIES)

			if await self.start():
				break

		logger.info("Reconnected to shelly device %s", self._serial)

		if not (await self.ping_shelly() and self._shelly_device.initialized):
			logger.error("Failed to reconnect to shelly device %s", self._serial)
			self.set_event("disconnected")
			return False
		# Reinit all channels
		for ch in self._channels.keys():
			await self._channels[ch].reinit(self.rpc_call, partial(self.restart_channel, ch))
		return True

	async def start(self):
		async with self._device_lock:
			if not (await self.ping_shelly()):
				if not await self.connect():
					return False

			logger.info(f"Starting shelly device {self._serial}")

			if not (self.has_em or self.has_switch or self.has_dimming):
				logger.error("Shelly device %s does not support switching or energy metering", self._serial)
				return False

			self._shelly_device.subscribe_updates(self.device_updated)
			return True

	async def start_channel(self, channel):
		async with self._device_lock:
			logger.warning("Starting channel %d for shelly device %s", channel, self._serial)
			if channel < 0 or channel >= self._num_channels:
				logger.error("Invalid channel number %d for shelly device %s", channel, self._serial)
				return False

			ch = await ShellyChannel.create(
				bus_type=self._bus_type,
				serial=self._serial,
				channel_id=channel,
				rpc_device_type=self._rpc_device_type,
				has_em=self._has_em,
				server=self._shelly_device.ip_address or self._server,
				restart=partial(self.restart_channel, channel),
				rpc_callback=self.rpc_call,
				productid=PRODUCT_ID_SHELLY_SWITCH if self.has_switch or self.has_dimming else PRODUCT_ID_SHELLY_EM,
				productName="Shelly switch" if self.has_switch else "Shelly dimmer" if self.has_dimming else "Shelly EM",
			)
			# Determine service name.
			if self.has_em:
				phases = await self.get_num_phases()
				await ch.init_em(phases, self.allowed_em_roles)
			await ch.init()
			if self.has_em:
				await ch.setup_em()
			if self.has_switch or self.has_dimming:
				type = OutputType.DIMMABLE if self.has_dimming \
					else OutputType.TOGGLE
				await ch.add_output(
					output_type=type,
					valid_functions=(1 << OutputFunction.MANUAL),
					name=f'Channel {channel + 1}'
				)

			self._channels[channel] = ch
			status = await self.request_channel_status(channel)
			if status is not None:
				self.parse_status(channel, status)
				await self._channels[channel].start()

			return True

	async def restart_channel(self, channel):
		await self.stop_channel(channel)
		await self.start_channel(channel)

	async def stop(self):
		async with self._device_lock:
			for ch in self._channels.keys():
				self._channels[ch].stop()
			self._channels.clear()

			if self._shelly_device:
				await self._shelly_device.shutdown()
			if self._aiohttp_session:
				await self._aiohttp_session.close()

			self._ws_context = None
			self._shelly_device = None
			self._aiohttp_session = None
			self.set_event("stopped")

	async def ping_shelly(self):
		if not self.is_connected:
			return False
		try:
			async with self._rpc_lock:
				async with async_timeout.timeout(2):
					resp = await self._shelly_device.call_rpc("Shelly.GetDeviceInfo")
					return resp is not None
		except Exception as e:
			logger.error("Ping to shelly device %s failed: %s", self._serial, e)
			return False
		return False

	async def rpc_call(self, method, params=None):
		resp = None
		if not self.is_connected:
			return None
		try:
			async with self._rpc_lock:
				# Aioshelly uses a timeout of 10 seconds.
				# We use a shorter timeout to detect connection issues faster.
				async with async_timeout.timeout(4):
					resp = await self._shelly_device.call_rpc(method, params)
		except (DeviceConnectionError, TimeoutError):
			logger.error("Failed to call RPC method on shelly device %s", self._serial)
			self.do_reconnect()
			return None
		except:
			return None
		return resp

	async def get_device_info(self):
		return await self.rpc_call("Shelly.GetDeviceInfo")

	async def get_num_phases(self):
		status = await self.request_channel_status(0)
		if status is not None:
			if (self.has_dimming or self.has_switch) and self.has_em:
				return 1
			elif self.has_em:
				return sum([f'{i}_voltage' in status for i in ['a','b','c']])
		return 0

	async def get_channels(self):
		channels = []
		ch = 0
		while True:
			resp = await self.request_channel_status(ch)
			if resp is not None:
				channels.append(resp)
				ch += 1
			else:
				return channels

	async def request_channel_status(self, channel):
		return await self.rpc_call(f'{self._rpc_device_type if self._rpc_device_type else "EM"}.GetStatus' , {"id": channel})

	async def list_methods(self):
		resp = await self.rpc_call("Shelly.ListMethods")
		return resp['methods'] if resp and 'methods' in resp else []

	def device_updated(self, cb_device, update_type):
		if update_type == RpcUpdateType.STATUS:
			for channel in self._channels.keys():
				if f'emdata:{channel}' in cb_device.status and self.has_em:
					self._channels[channel].update_energies(cb_device.status[f'emdata:{channel}'])
				# Get the switch status for this channel
				id=f'{self._rpc_device_type.lower()}:{channel}'
				# Check if the channel is present in the status
				if id in cb_device.status:
					self.parse_status(channel, cb_device.status[id])
		elif update_type == RpcUpdateType.DISCONNECTED:
			if self._shelly_device:
				logger.warning("Shelly device %s disconnected", self._serial)
				self.do_reconnect()

		elif update_type == RpcUpdateType.EVENT:
			for event in cb_device.event['events']:
				if event['event'] == "config_changed":
					for channel in self._channels.keys():
						self._channels[channel].channel_config_changed()
					return

	def parse_status(self, channel, status_json):
		self._channels[channel].update(status_json)