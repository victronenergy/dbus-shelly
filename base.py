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

from switch import SwitchDevice, OutputFunction, MODULE_STATE_CONNECTED
from energymeter import EnergyMeter

from __main__ import VERSION
from utils import logger, wait_for_settings, formatters as fmt

PRODUCT_ID_SHELLY_EM = 0xB034
PRODUCT_ID_SHELLY_SWITCH = 0xB074

STATUS_OFF = 0x00
STATUS_ON = 0x09

class ShellyChannel(SwitchDevice, EnergyMeter, object):

	@classmethod
	async def create(cls, bus_type=None, serial=None, channel_id=0, has_em=False, server=None, restart=None, version=VERSION, productid=0x0000, productName="Shelly switch", processName=__file__):
		bus = await MessageBus(bus_type=bus_type).connect()
		c = cls(bus, productid, serial, channel_id, version, server, restart, has_em, productName, processName)
		c.settings = await wait_for_settings(bus)

		role = 'acload' if c._has_em else 'switch'
		await c.settings.add_settings(
			Setting(c._settings_base + 'ClassAndVrmInstance'.format(c._serial, c._channel_id), '{}:50'.format(role), alias='instance_{}_{}'.format(c._serial, c._channel_id)),
			Setting(c._settings_base + 'CustomName'.format(c._serial, c._channel_id), "", alias="customname_{}_{}".format(c._serial, c._channel_id)),
		)

		return c

	def __init__(self, bus, productid, serial, channel_id, version, connection, restart, has_em, productName, processName):
		self.service = None
		self.settings = None
		self._productId = productid
		self._serial = serial
		self._channel_id = channel_id
		self.bus = bus
		self.version = version
		self.connection = connection
		self._restart = restart
		self._em_role = None
		self._has_em = has_em
		self.productName = productName
		self.processName = processName

		# We don't know the service type yet. Will be .acload if shelly supports energy metering, otherwise .switch.
		# If the shelly does not support switching, it may be acload, pvinverter or genset.
		self.serviceName = ''
		self._settings_base = '/Settings/Devices/shelly_{}_{}/'.format(self._serial, self._channel_id)

	async def init(self):
		# Set up the service name
		stype = self._em_role if self._has_em else 'switch'
		self.set_service_type(stype)
		self.serviceName = "com.victronenergy.{}.shelly_{}_{}".format(stype, self._serial, self._channel_id)

		self.service = Service(self.bus, self.serviceName)

		self.service.add_item(TextItem('/Mgmt/ProcessName', self.processName))
		self.service.add_item(TextItem('/Mgmt/ProcessVersion', self.version))
		self.service.add_item(TextItem('/Mgmt/Connection', self.connection))
		self.service.add_item(IntegerItem('/ProductId', self._productId, text=fmt['productid']))
		self.service.add_item(TextItem('/ProductName', self.productName))
		self.service.add_item(IntegerItem('/Connected', 1))
		self.service.add_item(TextItem('/Serial', self._serial))
		self.service.add_item(TextItem('/CustomName', self.settings.get_value(self.settings.alias("customname_{}_{}".format(self._serial, self._channel_id))), writeable=True, onchange=self._set_customname))
		self.service.add_item(IntegerItem('/State', MODULE_STATE_CONNECTED))
		self.service.add_item(IntegerItem('/DeviceInstance', int(self.settings.get_value(self.settings.alias('instance_{}_{}'.format(self._serial, self._channel_id))).split(':')[-1])))

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
		setting = self.settings.get_value(self.settings.alias('instance_{}_{}'.format(self._serial, self._channel_id)))
		if setting is None:
			logger.warning("No instance setting found for {}, setting default to switch:50".format(self._serial))
			return
		stype, instance = self.role_instance(setting)

		if stype != _stype:
			p = self.settings.alias('instance_{}_{}'.format(self._serial, self._channel_id))
			role, instance = self.role_instance(self.settings.get_value(p))
			self.settings.set_value_async(p, "{}:{}".format(_stype, instance))

	def role_instance(self, value):
		val = value.split(':')
		return val[0], int(val[1])

	def items_changed(self, service, values):
		try:
			self.customname = values[self.settings.alias("customname_{}_{}".format(self._serial, self._channel_id))]
		except :
			pass # Not a customname change

	def _set_customname(self, value):
		try:
			cn = self.settings.get_value(self.settings.alias("customname_{}_{}".format(self._serial, self._channel_id)))
			if cn != value:
				self.settings.set_value_async(self.settings.alias("customname_{}_{}".format(self._serial, self._channel_id)), value)
			return True
		except:
			return False

	def value_changed(self, path, value):
		""" Handle a value change from the settings service. """
		super().value_changed(path, value)

	def update(self, values):
		""" Update the service with new values. """
		if not self.service:
			return

		with self.service as s:
			for path, value in values.items():
				if self.service.get_item(path) is not None:
					s[path] = value
				else:
					logger.warning("Item %s not found in service %s", path, self.service.name)

		super().update(values)


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
		self._state_change_pending = False
		self._has_switch = False
		self._has_em = False
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
			return False
		return True

	async def start(self):
		if not self._shelly_device or not self._shelly_device.connected:
			if not await self.connect():
				logger.error("Failed to connect to shelly device {}".format(self._serial))
				return

		logger.info("Starting shelly device {}".format(self._serial))

		# List shelly methods
		methods = await self.list_methods()
		if len(methods) == 0:
			logger.error("Failed to list shelly methods")
			return

		if 'Switch.GetStatus' in methods:
			channels = await self.get_channels()
			self._num_channels = len(channels)
			self._has_switch = True

			if 'aenergy' in channels[0]:
				# Energy metering capabilities -> acload service
				self._has_em = True
				# Switchable AC load with EM capability can only have the acload role.
				self.allowed_em_roles = ['acload']

		# No switching capabilities, check for energy metering capabilities.
		elif 'EM.GetStatus' in methods:
			# Energy metering capabilities -> acload service
			self._has_em = True
			# Using a shelly as grid meter is not supported because the update frequency is too low.
			self.allowed_em_roles = ['acload', 'pvinverter', 'genset']

		logger.info("Shelly device %s has %d channels, support switching: %s, energy metering: %s",
			self._serial, self._num_channels, self._has_switch, self._has_em)
		if not self._has_em and not self._has_switch:
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
			has_em=self._has_em,
			server=self._server,
			restart=partial(self.restart_channel, channel),
			productid=PRODUCT_ID_SHELLY_SWITCH if self._has_switch else PRODUCT_ID_SHELLY_EM,
			productName="Shelly switch" if self._has_switch else "Shelly energy meter",
			processName="dbus-shelly"
		)
		# Determine service name.
		if self._has_em:
			await ch.init_em(self.allowed_em_roles)
		await ch.init()
		if self._has_switch:
			await ch.add_output(
				channel=0,
				output_type=0,
				set_state_cb=partial(self.set_state_cb, channel),
				valid_functions=(1 << OutputFunction.MANUAL),
				name="Channel {}".format(channel + 1),
				customName="Shelly Switch",
			)
		if self._has_em:
			await ch.setup_em()
			ch.add_em_channel(0) # L1

		self._channels[channel] = ch
		status = await self.request_channel_status(channel)
		if status is not None:
			self.parse_status(channel, status)
			await self._channels[channel].start()

	async def restart_channel(self, channel):
		self.stop_channel(channel)
		await self.start_channel(channel)

	async def stop(self):
		if self._shelly_device:
			await self._shelly_device.shutdown()
		if self._aiohttp_session:
			await self._aiohttp_session.close()

		for ch in self._channels.keys():
			self._channels[ch].stop()
		self._channels.clear()
		self._ws_context = None
		self._shelly_device = None
		self._aiohttp_session = None
		self.set_event("stopped")

	async def _rpc_call(self, method, params=None):
		resp = None
		try:
			resp = await self._shelly_device.call_rpc(method, params)
		except DeviceConnectionError:
			logger.error("Failed to call RPC method on shelly device %s", self._serial)
			self.set_event("disconnected")
		except:
			pass
		return resp

	async def get_device_info(self):
		return await self._rpc_call("Shelly.GetDeviceInfo")

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
		return await self._rpc_call("Switch.GetStatus", {"id": channel})

	async def list_methods(self):
		resp = await self._rpc_call("Shelly.ListMethods")
		return resp['methods'] if resp and 'methods' in resp else []

	def device_updated(self, cb_device, update_type):
		if update_type == RpcUpdateType.STATUS:
			for channel in self._channels.keys():
				# Get the switch status for this channel
				switch="switch:{}".format(channel)
				# Check if the channel is present in the status
				if switch in cb_device.status:
					self.parse_status(channel, cb_device.status[switch])
		elif update_type == RpcUpdateType.DISCONNECTED:
			logger.warning("Shelly device %s disconnected, closing service", self._serial)
			self.shelly_device = None
			self.set_event("disconnected")

		elif update_type == RpcUpdateType.EVENT:
			# TODO: Anything that needs to be handled?
			pass

	def parse_status(self, channel, status_json):
		values = {}
		try:
			if self._has_switch:
				switch_prefix = "/SwitchableOutput/0/"
				status = STATUS_ON if status_json["output"] else STATUS_OFF
				values[switch_prefix + 'State'] = 1 if status == STATUS_ON else 0
				values[switch_prefix + 'Status'] = status

			if self._has_em:
				em_prefix = "/Ac/L1/"
				values[em_prefix + 'Voltage'] = status_json["voltage"]
				values[em_prefix + 'Current'] = status_json["current"]
				values[em_prefix + 'Power'] = status_json["apower"]
				values[em_prefix + 'PowerFactor'] = status_json["pf"] if 'pf' in status_json else None
				# Shelly reports energy in Wh, so convert to kWh
				values[em_prefix + 'Energy/Forward'] = status_json["aenergy"]["total"] / 1000 if 'aenergy' in status_json else None
				values[em_prefix + 'Energy/Reverse'] = status_json["ret_aenergy"]["total"] / 1000 if 'ret_aenergy' in status_json else None
		except:
			pass

		self._channels[channel].update(values)

	async def set_state_cb(self, channel, item, value):
		await self._rpc_call(
			"Switch.Set",
			{
				# id is the switch channel, starting from 0
				"id":channel,
				"on":True if value == 1 else False,
			}
		)

		item.set_local_value(value)