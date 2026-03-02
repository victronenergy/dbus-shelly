import aiohttp
from functools import partial

from aioshelly.common import ConnectionOptions
from aioshelly.rpc_device import RpcDevice, RpcUpdateType, WsServer
from aioshelly.exceptions import DeviceConnectionError
import asyncio
import async_timeout
try:
	from dbus_fast.aio import MessageBus
except ImportError:
	from dbus_next.aio import MessageBus

from aiovelib.localsettings import Setting
from aiovelib.service import Service, IntegerItem, TextItem
from utils import logger, wait_for_settings, formatters as fmt

import shelly_handlers

from utils import logger
from __main__ import VERSION, __file__ as processName

PRODUCT_ID_SHELLY_EM = 0xB034
PRODUCT_ID_SHELLY_SWITCH = 0xB075
CONNECTION_RETRIES = 10
background_tasks = set()

class ShellyConnectionError(Exception):
	pass

class ShellyChannel(object):
	@classmethod
	async def create(cls, bus_type, serial, channel_type_id, server, productid=0x0000, productName=None, shellyModel=None):
		bus = await MessageBus(bus_type=bus_type).connect()
		c = cls(bus_type, bus, productid, serial, channel_type_id, server, productName, shellyModel)
		c.service = Service(bus, None)
		c.settings = await wait_for_settings(bus, itemsChanged=c.itemsChanged)

		settings_base = f'/Settings/Devices/shelly_{serial}_{c._channel_id}/'
		await c.settings.add_settings(
			Setting(settings_base + 'ClassAndVrmInstance', 'switch:50', alias=f'instance_{c._serial}_{c._channel_id}')
		)

		await c.ainit()
		return c

	def __init__(self, bus_type, bus, productid, serial, channel_type_id, connection, productName, shellyModel=None):
		self.service = None
		self.settings = None
		self.channel_custom_name = ""
		self._productId = productid
		self._serial = serial
		self._channel_id = int(channel_type_id.split('_')[1])
		self.bus_type = bus_type
		self.bus = bus
		self.connection = connection
		self.productName = productName
		self.shellyModel = shellyModel

	def itemsChanged(self, service, values):
		# Check if the device instance is changed
		if not f'/Settings/Devices/shelly_{self._serial}_{self._channel_id}/ClassAndVrmInstance' in values:
			return
		if f'/Settings/Devices/shelly_{self._serial}_{self._channel_id}/ClassAndVrmInstance' in values:
			try:
				instance = int(values[f'/Settings/Devices/shelly_{self._serial}_{self._channel_id}/ClassAndVrmInstance'].split(':')[-1])
				with self.service as s:
					s['/DeviceInstance'] = instance
			except:
				pass

	async def ainit(self):
		instance = int(self.settings.get_value(self.settings.alias('instance_{}_{}'.format(self._serial, self._channel_id))).split(':')[-1])

		self.service.add_item(TextItem('/Mgmt/ProcessName', processName))
		self.service.add_item(TextItem('/Mgmt/ProcessVersion', VERSION))
		self.service.add_item(TextItem('/Mgmt/Connection', self.connection))
		self.service.add_item(IntegerItem('/DeviceInstance', instance))
		self.service.add_item(IntegerItem('/ProductId', self._productId, text=fmt['productid']))
		self.service.add_item(TextItem('/ProductName', self.productName))
		self.service.add_item(IntegerItem('/Connected', 1))
		self.service.add_item(TextItem('/Serial', self._serial))
		self.service.add_item(IntegerItem('/State', 0x100)) # Connected
		self.service.add_item(TextItem('/ShellyModel', self.shellyModel))

	async def start_service(self):
		if self.service.name is None:
			logger.error("No service name set for shelly device %s channel %d", self._serial, self._channel_id)
			return
		await self.service.register()

	async def stop(self):
		await self.service.close()

	# Reinitialize the callbacks without restarting the service.
	async def reinit(self, rpc_callback, restart):
		self._rpc_call = rpc_callback
		self._restart = restart

# Represents a Shelly device, which can be a switch or an energy meter.
# Handles the websocket connection to the Shelly device and provides methods to control it.
# Creates an instance of ShellyChannel for each enabled channel.
class ShellyDevice(object):

	def __init__(self, bus_type=None, serial=None, server=None, event=None):
		self._bus_type= bus_type
		self._serial = serial
		self._event_obj = event
		self._shelly_device = None
		self._shelly_info = None
		self._ws_context = None
		self._aiohttp_session = None
		self._server = server
		self._rpc_lock = asyncio.Lock()
		self._device_lock = asyncio.Lock() # Protects device operations, enabling/disabling channels etc.
		self._reconnecting = False
		self._channels = {}
		self._channel_info = []
		self._capabilities = []
		self._event = None

	@property
	def event(self):
		return self._event

	def set_event(self, event_str):
		if self._event_obj:
			self._event = event_str
			self._event_obj.set()

	@property
	def server(self):
		if self._server:
			return self._server
		if self._shelly_device:
			return self._shelly_device.ip_address
		return None

	@property
	def is_connected(self):
		return self._shelly_device and self._shelly_device.connected

	@property
	def serial(self):
		return self._serial

	@property
	def shelly_info(self):
		return self._shelly_info

	@property
	def channel_info(self):
		return self._channel_info

	@property
	def active_channels(self):
		return list(self._channels.keys())
	
	def is_supported(self):
		return shelly_handlers.has_functional_handler(self._capabilities)

	def get_product_name_and_id(self):
		# Get product ID from the capabilities
		if any(cap in self._capabilities for cap in self.SWITCHING_CAPABILITIES):
			return "Shelly switch", PRODUCT_ID_SHELLY_SWITCH
		elif 'EM' in self._capabilities:
			return "Shelly EM", PRODUCT_ID_SHELLY_EM
		return "Unknown", 0x0000

	@property
	def serial_or_server(self):
		return self._serial if self._serial else self.server

	async def stop_channel(self, ch):
		async with self._device_lock:
			logger.warning(f"Stopping channel {ch} for shelly device {self._serial}")
			if ch in self._channels:
				entry = self._channels[ch]
				handlers = entry.get("handlers", {})
				for handler in handlers.values():
					await handler.stop()
				channel_obj = entry.get("channel")
				if channel_obj is not None:
					await channel_obj.stop()
				del self._channels[ch]

	def _parse_server(self):
		if not self._server:
			return None, None
		if self._server.count(":") == 1:
			host, port_str = self._server.rsplit(":", 1)
			if host and port_str.isdigit():
				port = int(port_str)
				if 1 <= port <= 65535:
					return host, port
		return self._server, None

	async def connect(self):
		host, port = self._parse_server()
		if host is None:
			return False
		if port is None:
			options = ConnectionOptions(host, "", "")
		else:
			options = ConnectionOptions(host, "", "", port=port)
		self._aiohttp_session = aiohttp.ClientSession()
		self._ws_context = WsServer()

		try:
			self._shelly_device = await RpcDevice.create(self._aiohttp_session, self._ws_context, options)

			if not self._shelly_device:
				logger.warning("Failed to create shelly device")
				raise ShellyConnectionError()

			try:
				async with async_timeout.timeout(5):
					await self._shelly_device.initialize()
			except Exception:
				logger.warning("Failed to initialize shelly device %s", self.serial_or_server)
				raise ShellyConnectionError()

			if not (self._shelly_device.connected and self._shelly_device.initialized):
				logger.warning("Failed to initialize shelly device %s", self.serial_or_server)
				raise ShellyConnectionError()

			# Get device info
			self._shelly_info = await self._get_device_info()
			logger.info("Connected to shelly device %s model %s", self._serial, self._shelly_info.get('model', 'Unknown'))

			# List shelly methods
			methods = await self.list_methods()
			if len(methods) == 0:
				logger.warning("Failed to list shelly methods")
				raise ShellyConnectionError()

			# Will be a list of capabilities, e.g. ['Switch', 'EM', 'Sys']
			reported_capabilities = list(set([m.split('.')[0] for m in methods]))
			if not shelly_handlers.has_functional_handler(reported_capabilities):
				logger.warning(
					"Shelly device %s with capabilities: %s is not supported",
					self._serial,
					reported_capabilities,
				)
				raise ShellyConnectionError()
			self._capabilities = reported_capabilities

			# Fetch device's serial if not known yet
			if not self._serial:
				info = await self.get_device_info()
				if info and 'mac' in info:
					self._serial = info['mac']
					logger.info("Shelly host %s has serial number %s", self.server, self._serial)
				else:
					logger.warning("Failed to get serial number for shelly device at %s", self.server)
					raise ShellyConnectionError()

			logger.info("Shelly device %s has %d channels, capabilities: %s", self.serial_or_server, self._num_channels, self._capabilities)
			self._num_channels = await self._get_num_channels()
			self._channel_info = await self._get_channels_info()

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
		logger.info("Reconnecting to shelly device %s", self.serial_or_server)
		if self._shelly_device:
			try:
				for ch in self._channels.keys():
					channel_obj = self._channels[ch].get("channel")
					if channel_obj is not None and hasattr(channel_obj, "disable"):
						channel_obj.disable()
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
			logger.info("Attempting to reconnect to shelly device %s (%d/%d)", self.serial_or_server, i + 1, CONNECTION_RETRIES)

			if await self.start():
				break

		logger.info("Reconnected to shelly device %s", self.serial_or_server)

		if not (await self.ping_shelly() and self._shelly_device.initialized):
			logger.error("Failed to reconnect to shelly device %s", self.serial_or_server)
			self.set_event("disconnected")
			return False
		# Reinit all channels
		await asyncio.gather(*(self._reinit_channel_and_handlers(ch) for ch in self._channels))
		return True

	async def _reinit_channel_and_handlers(self, ch):
		# Check if capabilities changed
		cap_changed = False
		handlers = self._channels[ch].get("handlers", {})
		# Capability removed
		for cap in list(handlers.keys()):
			if cap not in self._capabilities:
				cap_changed = True
				break
		# Capability added for which we have a handler
		for cap in self._capabilities:
			if cap not in handlers and shelly_handlers.get_handler_class(cap, ch.split('_')[0]) is not None:
				cap_changed = True
				break

		# Capabilities changed, Stop all channels and let the discovery service refresh the device.
		if cap_changed:
			self.set_event("capabilities_changed")
		else:
			# No capability change, just reinitialize the existing handlers with the new RPC connection.
			channel_obj = self._channels[ch].get("channel")
			await channel_obj.reinit(self.rpc_call, partial(self.restart_channel, ch))
			# Use set here to avoid refreshing the same handler multiple times if it handles multiple capabilities.
			await asyncio.gather(*(handler.refresh() for handler in set(handlers.values())))

	# Get the number of switching and/or metering channels.
	async def _get_channels_info(self):
		channels = []

		async def add_channels(ch_type, capabilities):
			ch = 0
			while True:
				found = False
				for cap in (c for c in capabilities if c in self._capabilities):
					resp = await self.rpc_call(f"{cap}.GetStatus", {"id": ch})
					if resp is None:
						continue
					found = True
					channels.append(f"{ch_type}_{ch}")
					ch += 1
				if not found:
					# No capabilities on this channel number, or the channel doesn't exist at all. Done.
					return

		for kind, caps in shelly_handlers.get_capabilities_by_kind().items():
			await add_channels(kind, caps)

		return channels

	async def start(self):
		async with self._device_lock:
			if not (await self.ping_shelly()):
				if not await self.connect():
					return False

			logger.info(f"Starting shelly device {self.serial_or_server}")

			if not shelly_handlers.has_functional_handler(self._capabilities):
				logger.error("Unsupported Shelly device %s", self._serial)
				return False

			self._shelly_device.subscribe_updates(self.device_updated)
			return True

	async def start_channel(self, channel):
		async with self._device_lock:
			logger.info(f"Starting channel {channel} for shelly device {self.serial_or_server}")
			if channel not in self._channel_info:
				logger.error(f"Invalid channel {channel} for shelly device {self.serial_or_server}, which has channels: {self._channel_info}")
				return False

			is_switch = 'switch' in channel
			name = f"Shelly {'Switch' if is_switch else 'EM'}"
			id = PRODUCT_ID_SHELLY_SWITCH if is_switch else PRODUCT_ID_SHELLY_EM

			# Create channel object. The dbus service lives here.
			ch = await ShellyChannel.create(
				bus_type=self._bus_type,
				serial=self._serial,
				channel_type_id=channel,
				productid=id,
				productName=name,
				server=self._shelly_device.ip_address or self._server,
				shellyModel=self._shelly_info.get('model', 'Unknown')
			)

			# Create handlers for the generic + switching OR EM capabilities
			handlers = {}
			for cap in self._capabilities:
				cls = shelly_handlers.get_handler_class(cap, channel.split('_')[0])
				if cls is None:
					continue
				create_new = True

				# Reuse existing handler if the same handler class is used for more capabilities (e.g. EM1 and EM1Data)
				for c in handlers.values():
					if cls == type(c):
						handlers[cap] = c
						create_new = False
						break
				if create_new:
					handlers[cap] = await shelly_handlers.ShellyHandler.create(
						cap,
						rpc_callback=self.rpc_call,
						restart_callback=partial(self.restart_channel, channel),
					shelly_channel=ch
					)
				
			await ch.start_service()

			self._channels[channel] = {"channel": ch, "handlers": handlers}
			return True

	async def restart_channel(self, channel):
		await self.stop_channel(channel)
		await self.start_channel(channel)

	async def stop(self):
		for ch in self._channels.keys():
			await self.stop_channel(ch)
		self._channels.clear()

		async with self._device_lock:
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
			logger.error("Ping to shelly device %s failed: %s", self.serial_or_server, e)
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
			logger.error("Failed to call RPC method on shelly device %s", self.serial_or_server)
			self.do_reconnect()
			return None
		except:
			return None
		return resp

	async def _get_device_info(self):
		return await self.rpc_call("Shelly.GetDeviceInfo")

	async def list_methods(self):
		resp = await self.rpc_call("Shelly.ListMethods")
		return resp['methods'] if resp and 'methods' in resp else []

	def device_updated(self, cb_device, update_type):
		if update_type == RpcUpdateType.STATUS:
			for ch in self._channels.keys():
				channel = ch.split('_')[1]
				entry = self._channels[ch]
				handlers = entry.get("handlers", {})
				for cap, handler in handlers.items():
					cap = cap.lower()
					key = f'{cap}:{channel}'
					if key in cb_device.status:
						handler.update(cb_device.status[key], cap=cap)

		elif update_type == RpcUpdateType.DISCONNECTED:
			if self._shelly_device:
				logger.warning("Shelly device %s disconnected", self.serial_or_server)
				self.do_reconnect()

		elif update_type == RpcUpdateType.EVENT:
			for event in cb_device.event['events']:
				for channel in self._channels.keys():
					entry = self._channels[channel]
					handlers = entry.get("handlers", {})
					for handler in handlers.values():
						if hasattr(handler, "on_event"):
							handler.on_event(event)
