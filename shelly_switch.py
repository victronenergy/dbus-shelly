#!/usr/bin/python3
from __future__ import annotations

import sys
import os
import argparse
import asyncio
from functools import partial
import logging
import uuid
import aiohttp
# aiovelib
sys.path.insert(1, os.path.join(os.path.dirname(__file__), 'ext', 'aiovelib'))
from aiovelib.service import Service, IntegerItem, DoubleItem, TextItem
from aiovelib.service import TextArrayItem
from aiovelib.client import Service as Client
from aiovelib.client import Monitor, ServiceHandler
from aiovelib.localsettings import SettingsService as SettingsClient, Setting, SETTINGS_SERVICE

try:
	from dbus_fast.aio import MessageBus
except ImportError:
	from dbus_next.aio import MessageBus

try:
	from dbus_fast.constants import BusType
except ImportError:
	from dbus_next.constants import BusType

from zeroconf import ServiceStateChange, Zeroconf, ServiceListener
from zeroconf.asyncio import (
	AsyncServiceBrowser,
	AsyncServiceInfo,
	AsyncZeroconf,
	AsyncZeroconfServiceTypes,
)
from aioshelly.common import ConnectionOptions
from aioshelly.rpc_device import RpcDevice, RpcUpdateType, WsServer
from aioshelly.exceptions import DeviceConnectionError

from switch_device import SwitchDevice, OutputFunction, STATUS_ON, STATUS_OFF, MODULE_STATE_CONNECTED

VERSION = "0.1"
PRODUCT_ID_SHELLY_EM = 0xB034
PRODUCT_ID_SHELLY_SWITCH = 0xB074
logger = logging.getLogger('dbus-shelly')
logger.setLevel(logging.DEBUG)
background_tasks = set()

# Text formatters
unit_watt = lambda v: "{:.0f}W".format(v)
unit_volt = lambda v: "{:.1f}V".format(v)
unit_amp = lambda v: "{:.1f}A".format(v)
unit_kwh = lambda v: "{:.2f}kWh".format(v)
unit_productid = lambda v: "0x{:X}".format(v)

class SettingsMonitor(Monitor):
	def __init__(self, bus, **kwargs):
		super().__init__(bus, handlers = {
			'com.victronenergy.settings': SettingsClient
		}, **kwargs)


class EnergyMeter:
	allowed_em_roles = None
	_em_role = None

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
		self.service.add_item(DoubleItem('/Ac/Energy/Forward', None, text=unit_kwh))
		self.service.add_item(DoubleItem('/Ac/Energy/Reverse', None, text=unit_kwh))
		self.service.add_item(DoubleItem('/Ac/Power', None, text=unit_watt))

	def add_em_channel(self, channel):
		logger.info("Adding energy meter for shelly device {}".format(channel, self._serial))
		prefix = '/Ac/L{}/'.format(channel + 1)
		self.service.add_item(DoubleItem(prefix + 'Voltage', None, text=unit_volt))
		self.service.add_item(DoubleItem(prefix + 'Current', None, text=unit_amp))
		self.service.add_item(DoubleItem(prefix + 'Power', None, text=unit_watt))
		self.service.add_item(DoubleItem(prefix + 'Energy/Forward', None, text=unit_kwh))
		self.service.add_item(DoubleItem(prefix + 'Energy/Reverse', None, text=unit_kwh))
		self.service.add_item(DoubleItem(prefix + 'PowerFactor', None))

	def update(self, values):
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


class ShellyChannel(SwitchDevice, EnergyMeter):
	service = None
	settings = None
	_restart = None
	_channel_id = 0
	_serial = None
	_has_em = False

	@classmethod
	async def create(cls, bus_type=None, serial=None, channel_nr=0, has_em=False, server=None, restart=None, version=VERSION, productid=0x0000, productName="Shelly switch", processName=__file__):
		bus = await MessageBus(bus_type=bus_type).connect()
		self = cls(bus, productid, "shelly", serial, version, server, productName, processName)
		self._channel_id = channel_nr
		self._restart = restart
		self._has_em = has_em
		await self.wait_for_settings()
		return self

	def __init__(self, bus, productid, tty, serial, version, connection, productName, processName):
		self._productId = productid
		self._serial = serial
		self._tty = tty
		self.bus = bus
		self.version = version
		self.connection = connection
		self.productName = productName
		self.processName = processName
		self.serviceName = '' # We don't know the service type yet. Will be .acload if shelly supports energy metering, otherwise .switch.

	async def init(self):
		# Set up the service name
		stype = self._em_role if self._has_em else 'switch'
		self.set_service_type(stype)
		self.serviceName = "com.victronenergy.{}.shelly_{}_{}".format(stype, self._serial, self._channel_id)

		self.service = Service(self.bus, self.serviceName)

		self.service.add_item(TextItem('/Mgmt/ProcessName', self.processName))
		self.service.add_item(TextItem('/Mgmt/ProcessVersion', self.version))
		self.service.add_item(TextItem('/Mgmt/Connection', self.connection))
		self.service.add_item(IntegerItem('/ProductId', self._productId))
		self.service.add_item(TextItem('/ProductName', self.productName))
		self.service.add_item(IntegerItem('/Connected', 1))
		self.service.add_item(TextItem('/Serial', self._serial))
		self.service.add_item(TextItem('/CustomName', "", writeable=True, onchange=self._set_customname))
		self.service.add_item(IntegerItem('/State', MODULE_STATE_CONNECTED))
		self.service.add_item(IntegerItem('/DeviceInstance', int(self.settings.get_value(self.settings.alias('instance_{}_{}'.format(self._serial, self._channel_id))).split(':')[-1])))

	def stop(self):
		if self.service is not None:
			self.service.__del__()
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

	async def wait_for_settings(self):
		""" Attempt a connection to localsettings. """
		settingsmonitor = await SettingsMonitor.create(self.bus,
			itemsChanged=self.items_changed)
		self.settings = await asyncio.wait_for(
			settingsmonitor.wait_for_service(SETTINGS_SERVICE), 5)

		await self.settings.add_settings(
			Setting('/Settings/Devices/shelly_{}/{}/ClassAndVrmInstance'.format(self._serial, self._channel_id), 'switch:50', alias='instance_{}_{}'.format(self._serial, self._channel_id)),
			Setting('/Settings/Devices/shelly_{}/{}/CustomName'.format(self._serial, self._channel_id), "", alias="customname"),
		)

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
			self.customname = values[self.settings.alias('customname')]
		except :
			pass # Not a customname change

	def _set_customname(self, value):
		try:
			cn = self.settings.get_value(self.settings.alias("customname"))
			if cn != value:
				self.settings.set_value_async(self.settings.alias("customname"), value)
			return True
		except:
			return False

	def value_changed(self, path, value):
		""" Handle a value change from the settings service. """
		super().value_changed(path, value)

	def __del__(self):
		self.stop()

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
class ShellyDevice():
	_shelly_device = None
	_ws_context = None
	_aiohttp_session = None
	_server = None
	_state_change_pending = False
	_has_switch = False
	_has_em = False
	_channels = {}

	def __init__(self, bus_type=None, productid=None, tty=None, serial=None, server=None, event=None, productName=None, processName=None):
		self._runningloop = asyncio.get_event_loop()
		self._productId = productid
		self._bus_type= bus_type
		self._tty = tty
		self._serial = serial
		self._server = server
		self._event_obj = event
		self._num_channels = 0
		self._shelly_device = None
		self._ws_context = None
		self._aiohttp_session = None

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

	def channel_enabled(self, ch):
		return ch in self._channels.keys()

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
				logger.error("Failed to connect to shelly device %s", self._serial)
				return

		logger.info("Starting shelly device %s", self._serial)

		# List shelly methods
		methods = await self.list_methods()
		if len(methods) == 0:
			logger.error("Failed to list shelly methods")
			return

		if 'Switch.GetStatus' in methods:
			channels = await self.get_channels()
			self._num_channels = len(channels)
			self._has_switch = True

			if all(x in channels[0] for x in ['apower', 'voltage', 'current', 'aenergy']):
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
			channel_nr=channel,
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
			await ch.start()

	async def restart_channel(self, channel):
		self.stop_channel(channel)
		await self.start_channel(channel)

	async def stop(self):
		if self._shelly_device is None and self._aiohttp_session is None:
			return

		if self._shelly_device:
			await self._shelly_device.shutdown()
		await self._aiohttp_session.close()

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

	def set_state_cb(self, channel, value):
		if self._state_change_pending:
			return False

		self._state_change_pending = True
		self.state = value

		task = self._runningloop.create_task(self._rpc_call(
			"Switch.Set",
			{
				# id is the switch channel, starting from 0
				"id":channel,
				"on":True if value == 1 else False,
			}
		))
		background_tasks.add(task)

		def on_switch_set_task_done(arg):
			self._state_change_pending = False

		task.add_done_callback(on_switch_set_task_done)
		task.add_done_callback(background_tasks.discard)
		return True


class ShellyDiscovery:
	shellies = []
	shelly_switches = {}
	service = None
	listener = None
	def __init__(self, bus_type):
		self.bus_type = bus_type
		self.aiobrowser = None
		self.aiozc = None

	async def start(self):
		# Connect to dbus, localsettings
		self.bus = await MessageBus(bus_type=self.bus_type).connect()
		self.monitor = await Monitor.create(self.bus, itemsChanged=self.items_changed)

		await self.wait_for_settings()

		# Set up the service
		self.service = Service(self.bus, "com.victronenergy.shelly")
		self.service.add_item(IntegerItem('/Scan', 0, writeable=True,
			onchange=self.scan))
		await self.service.register()

		self.aiozc = AsyncZeroconf()
		self.aiobrowser = AsyncServiceBrowser(
			self.aiozc.zeroconf, ["_shelly._tcp.local."], handlers=[self.on_service_state_change]
		)

		await self.bus.wait_for_disconnect()

	async def scan(self, old_value, new_value):
		if new_value == 1:
			""" Start a scan for shelly devices. """
			if self.aiobrowser is not None:
				await self.aiobrowser.async_cancel()
				logger.info("Starting shelly device scan")
				self.aiobrowser = AsyncServiceBrowser(
					self.aiozc.zeroconf, ["_shelly._tcp.local."], handlers=[self.on_service_state_change]
				)
			else:
				logger.warning("Shelly discovery not started, cannot scan for devices")
		return False # Reset the scan value to 0 after scanning

	async def wait_for_settings(self):
		""" Attempt a connection to localsettings. """
		settingsmonitor = await SettingsMonitor.create(self.bus)
		self.settings = await asyncio.wait_for(
			settingsmonitor.wait_for_service(SETTINGS_SERVICE), 5)

	async def shelly_event_monitor(self, event, shelly):
		serial = shelly.serial
		try:
			while True:
				await event.wait()
				event.clear()
				e = shelly.event

				if e == "disconnected":
					logger.warning("Shelly device %s disconnected, removing from service", serial)
					await self.stop_shelly_device(serial)
					self.remove_shelly(serial)
					return

				elif e == "stopped":
					return

				event.clear()
		except asyncio.CancelledError:
			logger.info("Shelly event monitor for %s cancelled", serial)
		return

	def remove_shelly(self, serial):
		if serial in self.shellies:
			self.shellies.remove(serial)
			with self.service as s:
				s['/Devices/{}/Server'.format(serial)] = None
				s['/Devices/{}/Mac'.format(serial)] = None
				s['/Devices/{}/Model'.format(serial)] = None
				s['/Devices/{}/Name'.format(serial)] = None
				try:
					i = 0
					while True:
						s.remove_item('/Devices/{}/{}/Enabled'.format(serial, i))
						i += 1
				except KeyError:
					pass

	async def add_shelly_device(self, serial, server):
		event = asyncio.Event()
		s = ShellyDevice(
			bus_type=self.bus_type,
			serial=serial,
			server=server,
			event=event
		)

		e = asyncio.create_task(
			self.shelly_event_monitor(event, s)
		)
		await s.start()
		e.add_done_callback(partial(self.delete_shelly_device, serial))
		self.shelly_switches[serial] = {'device': s, 'event_mon': e}

	async def enable_shelly_channel(self, serial, channel, server):
		""" Enable a shelly channel. """
		if serial not in self.shelly_switches:
			await self.add_shelly_device(serial, server)

		await self.shelly_switches[serial]['device'].start_channel(channel)

		return True

	def delete_shelly_device(self, serial, fut=None):
		if serial in self.shelly_switches:
			del self.shelly_switches[serial]

	async def disable_shelly_channel(self, serial, channel):
		""" Disable a shelly channel. """
		if serial not in self.shelly_switches:
			logger.error("Shelly device %s not found", serial)
			return False

		self.shelly_switches[serial]['device'].stop_channel(channel)

		if len(self.shelly_switches[serial]['device'].active_channels) == 0:
			logger.info("No active channels left for device %s, stopping device", serial)
			await self.shelly_switches[serial]['device'].stop()
		return True

	async def stop_shelly_device(self, serial):
		if serial in self.shelly_switches:
			await self.shelly_switches[serial]['device'].stop()
		else:
			logger.warning("Device not found: %s", serial)

	async def restart_shelly_device(self, serial):
		if serial in self.shelly_switches:
			logger.info("Restarting shelly device %s", serial)
			await self.shelly_switches[serial]['device'].stop()
			await self.shelly_switches[serial]['device'].start()
		else:
			logger.warning("Device not found: %s", serial)

	def items_changed(self, service, values):
		pass

	async def stop(self):
		assert self.aiozc is not None
		assert self.aiobrowser is not None
		await self.aiobrowser.async_cancel()
		await self.aiozc.async_close()

	async def get_device_info(self, server):
		# Only server info is needed for obtaining device info
		shelly = ShellyDevice(
			server=server
		)
		await shelly.connect()
		if not shelly._shelly_device or not shelly._shelly_device.connected:
			logger.error("Failed to connect to shelly device %s", server)
			return None, 0

		info = await shelly.get_device_info()
		num_channels = len(await shelly.get_channels())

		await shelly.stop()
		del shelly
		return info, num_channels

	def on_service_state_change(self, zeroconf: Zeroconf, 
		service_type: str, name: str, state_change: ServiceStateChange):

		if not name.startswith("shelly"):
			return

		task = asyncio.get_event_loop().create_task(self.update_devices(zeroconf, service_type, name, state_change))
		background_tasks.add(task)
		task.add_done_callback(background_tasks.discard)

	async def update_devices(self, zeroconf: Zeroconf, service_type: str, name: str, state_change: ServiceStateChange):
		info = AsyncServiceInfo(service_type, name)
		await info.async_request(zeroconf, 3000)
		serial = info.server.split(".")[0].split("-")[-1]

		if state_change == ServiceStateChange.Added or state_change == ServiceStateChange.Updated and serial not in self.shellies:
			logger.info("Found shelly device: %s", serial)
			device_info, num_channels = await self.get_device_info(info.server)
			if device_info is None:
				logger.error("Failed to get device info for %s", serial)
				return
			# Shelly plus plug S example: 'app': 'PlusPlugS', 'model': 'SNPL-00112EU'
			model_name = device_info.get('app', device_info.get('model', 'Unknown'))
			# Custom name of the shelly device, if available
			name = device_info.get('name', None)

			try:
				self.service.add_item(TextItem('/Devices/{}/Server'.format(serial)))
				self.service.add_item(TextItem('/Devices/{}/Mac'.format(serial)))
				self.service.add_item(TextItem('/Devices/{}/Model'.format(serial), model_name))
				self.service.add_item(TextItem('/Devices/{}/Name'.format(serial), name))
			except:
				pass

			with self.service as s:
				s['/Devices/{}/Server'.format(serial)] = info.server[:-1]
				s['/Devices/{}/Mac'.format(serial)] = serial
				s['/Devices/{}/Model'.format(serial)] = model_name
				s['/Devices/{}/Name'.format(serial)] = name

			for i in range(num_channels):
				await self.settings.add_settings(Setting('/Settings/Devices/shelly_{}/{}/Enabled'.format(serial, i), 0, alias="enabled_{}_{}".format(serial, i)))
				enabled = self.settings.get_value(self.settings.alias('enabled_{}_{}'.format(serial, i)))

				try:
					self.service.add_item(IntegerItem('/Devices/{}/{}/Enabled'.format(serial, i), writeable=True, onchange=partial(self.on_enabled_changed, serial, i)))
				except:
					pass

				with self.service as s:
					s['/Devices/{}/{}/Enabled'.format(serial, i)] = enabled

				if enabled:
					self.on_enabled_changed(serial, i, enabled)

				self.shellies.append(serial)

		elif state_change == ServiceStateChange.Removed:
			logger.info("Shelly device: %s disappeared", serial)
			self.remove_shelly(serial)

	def on_enabled_changed(self, serial, channel, value):
		if value not in (0, 1):
			return False

		server = self.service['/Devices/{}/Server'.format(serial)]
		task = asyncio.get_event_loop().create_task(self.enable_shelly_channel(serial, channel, server) if value == 1 else
			self.disable_shelly_channel(serial, channel))
		background_tasks.add(task)
		task.add_done_callback(background_tasks.discard)

		self.settings.set_value_async(self.settings.alias('enabled_{}_{}'.format(serial, channel)), value)
		return True