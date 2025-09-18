import aiohttp
from functools import partial

from aiovelib.service import Service, IntegerItem, TextItem
from aiovelib.localsettings import Setting

from aioshelly.common import ConnectionOptions
from aioshelly.rpc_device import RpcDevice, RpcUpdateType, WsServer
from aioshelly.exceptions import DeviceConnectionError
import asyncio
from switch import OutputFunction
from shelly_s2 import ShellyChannelWithRm as ShellyChannel

from utils import logger, STATUS_OFF, STATUS_ON

PRODUCT_ID_SHELLY_EM = 0xB034
PRODUCT_ID_SHELLY_SWITCH = 0xB074

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
			for event in cb_device.event['events']:
				if event['event'] == "config_changed":
					for channel in self._channels.keys():
						self._channels[channel].channel_config_changed(self._shelly_device.name)
					return

	def parse_status(self, channel, status_json):
		self._channels[channel].update(status_json)
