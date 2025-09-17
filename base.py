import aiohttp
import asyncio
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
		self.service.add_item(TextItem('/CustomName', self.settings.get_value(self.settings.alias(f'customname_{self._serial}_{self._channel_id}')), writeable=True, onchange=self._set_customname))
		self.service.add_item(IntegerItem('/State', MODULE_STATE_CONNECTED))
		self.service.add_item(IntegerItem('/DeviceInstance', int(self.settings.get_value(self.settings.alias(f'instance_{self._serial}_{self._channel_id}')).split(':')[-1])))

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

	def items_changed(self, service, values):
		try:
			self.customname = values[self.settings.alias(f'customname_{self._serial}_{self._channel_id}')]
		except:
			pass # Not a customname change

	def _set_customname(self, value):
		try:
			cn = self.settings.get_value(self.settings.alias(f'customname_{self._serial}_{self._channel_id}'))
			if cn != value:
				self.settings.set_value_async(self.settings.alias(f'customname_{self._serial}_{self._channel_id}'), value)
			return True
		except:
			return False

	def value_changed(self, path, value):
		""" Handle a value change from the settings service. """
		super().value_changed(path, value)

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
		self._rpc_device_type = None
		self._has_em = False
		self._has_dimming = False
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

	def stop_channel(self, ch):
		if ch in self._channels.keys():
			self._channels[ch].stop()
			del self._channels[ch]

	async def connect(self):
		options = ConnectionOptions(self._server, "", "")
		self._aiohttp_session = aiohttp.ClientSession()
		self._ws_context = WsServer()

		self._shelly_device = await RpcDevice.create(self._aiohttp_session, self._ws_context, options)
		await self._shelly_device.initialize()

		if not self._shelly_device.connected:
			logger.warning("Failed to connect to shelly device")
			return

		# List shelly methods
		methods = await self.list_methods()
		if len(methods) == 0:
			logger.error("Failed to list shelly methods")
			return False

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

		self._num_channels = len(channels) if self.has_switch or self.has_dimming else 1

		logger.info("Shelly device %s has %d channels, supports switching: %s, energy metering: %s, dimming: %s",
			self._serial, self._num_channels, self.has_switch, self.has_em, self.has_dimming)

		return True

	async def start(self):
		if not self._shelly_device or not self._shelly_device.connected:
			if not await self.connect():
				logger.error(f"Failed to connect to shelly device {self._serial}")
				return

		logger.info(f"Starting shelly device {self._serial}")

		if not (self.has_em or self.has_switch or self.has_dimming):
			logger.error("Shelly device %s does not support switching or energy metering", self._serial)
			return

		self._shelly_device.subscribe_updates(self.device_updated)

	async def start_channel(self, channel):
		if channel < 0 or channel >= self._num_channels:
			logger.error("Invalid channel number %d for shelly device %s", channel, self._serial)
			return

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
		if self.has_switch or self.has_dimming:
			type = OutputType.DIMMABLE if self.has_dimming \
				else OutputType.TOGGLE
			await ch.add_output(
				channel=0,
				output_type=type,
				valid_functions=(1 << OutputFunction.MANUAL),
				name=f'Channel {channel + 1}'
			)
		if self.has_em:
			await ch.setup_em()

		self._channels[channel] = ch
		status = await self.request_channel_status(channel)
		if status is not None:
			self.parse_status(channel, status)
			await self._channels[channel].start()

	async def restart_channel(self, channel):
		self.stop_channel(channel)
		await self.start_channel(channel)

	async def stop(self):
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

	async def rpc_call(self, method, params=None):
		resp = None
		try:
			async with self._rpc_lock:
				resp = await self._shelly_device.call_rpc(method, params)
		except DeviceConnectionError:
			logger.error("Failed to call RPC method on shelly device %s", self._serial)
			self.set_event("disconnected")
		except:
			pass
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
		return await self.rpc_call(f'{self._rpc_device_type}.GetStatus', {"id": channel})

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
			logger.warning("Shelly device %s disconnected, closing service", self._serial)
			self.shelly_device = None
			self.set_event("disconnected")

		elif update_type == RpcUpdateType.EVENT:
			# TODO: Anything that needs to be handled?
			pass

	def parse_status(self, channel, status_json):
		self._channels[channel].update(status_json)