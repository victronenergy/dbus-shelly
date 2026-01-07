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
	async def create(cls, bus_type, serial, channel_type_id, server, productid=0x0000, productName=None):
		bus = await MessageBus(bus_type=bus_type).connect()
		c = cls(bus, productid, serial, channel_type_id, server, productName)
		c.service = Service(bus, None)
		c.settings = await wait_for_settings(bus)

		settings_base = f'/Settings/Devices/shelly_{serial}_{c._channel_id}/'
		await c.settings.add_settings(
			Setting(settings_base + 'ClassAndVrmInstance', 'switch:50', alias=f'instance_{c._serial}_{c._channel_id}')
		)

		await c.ainit()
		return c

	def __init__(self, bus, productid, serial, channel_type_id, connection, productName):
		self.service = None
		self.settings = None
		self.channel_custom_name = ""
		self._productId = productid
		self._serial = serial
		self._channel_id = int(channel_type_id.split('_')[1])
		self.bus = bus
		self.connection = connection
		self.productName = productName

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

	async def start_service(self):
		if self.service.name is None:
			logger.error("No service name set for shelly device %s channel %d", self._serial, self._channel_id)
			return
		await self.service.register()

	async def stop_service(self):
		await self.service.close()


# Represents a Shelly device, which can be a switch or an energy meter.
# Handles the websocket connection to the Shelly device and provides methods to control it.
# Creates an instance of ShellyChannel for each enabled channel.
class ShellyDevice(object):
	SWITCHING_CAPABILITIES = ['Switch', 'Light', 'RGB', 'RGBW']
	EM_CAPABILITIES = ['EM', 'EM1']

	def __init__(self, bus_type=None, serial=None, server=None, event=None):
		self._bus_type= bus_type
		self._serial = serial
		self._event_obj = event
		self._shelly_device = None
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
		if self._shelly_device:
			return self._shelly_device.ip_address or self._server
		return None

	@property
	def is_connected(self):
		return self._shelly_device and self._shelly_device.connected

	@property
	def serial(self):
		return self._serial
	
	@property
	def channel_info(self):
		return self._channel_info

	@property
	def active_channels(self):
		return list(self._channels.keys())
	
	def is_supported(self):
		return shelly_handlers.has_functional_handler(self._capabilities)

	async def stop_channel(self, ch):
		async with self._device_lock:
			logger.warning(f"Stopping channel {ch} for shelly device {self._serial}")
			if ch in self._channels:
				entry = self._channels[ch]
				channel_obj = entry.get("channel")
				if channel_obj is not None:
					if hasattr(channel_obj, "stop"):
						await channel_obj.stop()
					elif hasattr(channel_obj, "service") and channel_obj.service is not None:
						await channel_obj.service.close()
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
				async with async_timeout.timeout(5):
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
		logger.info("Reconnecting to shelly device %s", self._serial)
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
			logger.info("Attempting to reconnect to shelly device %s (%d/%d)", self._serial, i + 1, CONNECTION_RETRIES)

			if await self.start():
				break

		logger.info("Reconnected to shelly device %s", self._serial)

		if not (await self.ping_shelly() and self._shelly_device.initialized):
			logger.error("Failed to reconnect to shelly device %s", self._serial)
			self.set_event("disconnected")
			return False
		# Reinit all channels
		#FIXME: Add reinit method, this should also check for changed capabilities (happens when switching profiles on the shelly)
		for ch in self._channels.keys():
			channel_obj = self._channels[ch].get("channel")
			if channel_obj is not None and hasattr(channel_obj, "reinit"):
				await channel_obj.reinit(self.rpc_call, partial(self.restart_channel, ch))
		return True

	# Get the number of switching and/or metering channels.
	async def _get_channels_info(self):
		channels = []

		async def add_channels(capabilities, ch_type):
			for cap in (c for c in capabilities if c in self._capabilities):
				ch = 0
				while True:
					resp = await self.rpc_call(f"{cap}.GetStatus", {"id": ch})
					if resp is None:
						break
					channels.append(f"{ch_type}_{ch}")
					ch += 1

		await add_channels(self.SWITCHING_CAPABILITIES, "switch")
		await add_channels(self.EM_CAPABILITIES, "em")

		return channels

	async def start(self):
		async with self._device_lock:
			if not (await self.ping_shelly()):
				if not await self.connect():
					return False

			logger.info(f"Starting shelly device {self._serial}")

			if not shelly_handlers.has_functional_handler(self._capabilities):
				logger.error("Unsupported Shelly device %s", self._serial)
				return False

			self._shelly_device.subscribe_updates(self.device_updated)
			return True

	async def start_channel(self, channel):
		async with self._device_lock:
			logger.info(f"Starting channel {channel} for shelly device {self._serial}")
			if channel not in self._channel_info:
				logger.error(f"Invalid channel {channel} for shelly device {self._serial}, which has channels: {self._channel_info}")
				return False

			is_switch = 'switch' in channel
			name = f"Shelly {"Switch" if is_switch else "EM"}"
			id = PRODUCT_ID_SHELLY_SWITCH if is_switch else PRODUCT_ID_SHELLY_EM

			# Create channel object. The dbus service lives here.
			ch = await ShellyChannel.create(
				bus_type=self._bus_type,
				serial=self._serial,
				channel_type_id=channel,
				productid=id,
				productName=name,
				server=self._shelly_device.ip_address or self._server
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
		async with self._device_lock:
			for ch in self._channels.keys():
				channel_obj = self._channels[ch].get("channel")
				if channel_obj is not None:
					if hasattr(channel_obj, "stop"):
						await channel_obj.stop()
					elif hasattr(channel_obj, "service") and channel_obj.service is not None:
						await channel_obj.service.close()
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
				logger.warning("Shelly device %s disconnected", self._serial)
				self.do_reconnect()

		elif update_type == RpcUpdateType.EVENT:
			for event in cb_device.event['events']:
				for channel in self._channels.keys():
					entry = self._channels[channel]
					handlers = entry.get("handlers", {})
					for handler in handlers.values():
						if hasattr(handler, "on_event"):
							handler.on_event(event)
