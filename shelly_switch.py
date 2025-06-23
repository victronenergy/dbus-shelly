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

	async def setup_em(self):
		# Determine role and instance
		self.em_role, instance = self.role_instance(
			self.settings.get_value(self.settings.alias('instance_{}'.format(self._serial))))

		if self.em_role not in self.allowed_em_roles:
			logger.warning("Role {} not allowed for shelly energy meter, resetting to {}".format(self.em_role, self.allowed_em_roles[0]))
			self.em_role = self.allowed_em_roles[0]
			self.settings.set_value_async(self.settings.alias('instance_{}'.format(self._serial)), "{}:{}".format(self.em_role, instance))

		self.service.add_item(TextItem('/Role', self.em_role, writeable=True,
			onchange=self.role_changed))
		self.service.add_item(TextArrayItem('/AllowedRoles', self.allowed_em_roles, writeable=False))

		# Meter paths
		self.service.add_item(DoubleItem('/Ac/Energy/Forward', None, text=unit_kwh))
		self.service.add_item(DoubleItem('/Ac/Energy/Reverse', None, text=unit_kwh))
		self.service.add_item(DoubleItem('/Ac/Power', None, text=unit_watt))

	def add_em_channel(self, channel):
		logger.info("Adding energy meter channel {} for shelly device {}".format(channel, self._serial))
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

		p = self.settings.alias('instance_{}'.format(self._serial))
		role, instance = self.role_instance(val)
		self.settings.set_value(p, "{}:{}".format(val, instance))

		self.set_event("role_changed")
		return True


# Basic shelly service.
class ShellyService(object):
	service = None
	settings = None
	_event_obj = None
	_event = ""
	_serial = None
	_num_channels = 0

	@classmethod
	async def create(cls, bus_type, product_id, tty="", serial="", version="", connection="", productName="Switching device", processName=""):
		""" Create a new instance of the SwitchDevice class. """
		bus = await MessageBus(bus_type=bus_type).connect()
		self = cls(bus, product_id, tty, serial, version, connection, productName, processName)
		return self

	@property
	def event(self):
		return self._event

	def set_event(self, event_str):
		if self._event_obj:
			self._event = event_str
			self._event_obj.set()

	def __init__(self, bus, product_id, tty, serial, version, connection, productName, processName):
		self._runningloop = asyncio.get_event_loop()
		self._productId = product_id
		self._serial = serial
		self._tty = tty
		self.bus = bus
		self.version = version
		self.connection = connection
		self.productName = productName
		self.processName = processName
		self.serviceName = '' # We don't know the service type yet. Will be .acload if shelly supports energy metering, otherwise .switch.

		self.service = Service(bus, self.serviceName)

	async def init(self):
		await self.wait_for_settings()

		val = self.settings.get_value(self.settings.alias('instance_{}'.format(self._serial)))

		self.service.add_item(TextItem('/Mgmt/ProcessName', self.processName))
		self.service.add_item(TextItem('/Mgmt/ProcessVersion', self.version))
		self.service.add_item(TextItem('/Mgmt/Connection', self.connection))
		self.service.add_item(IntegerItem('/ProductId', self._productId))
		self.service.add_item(TextItem('/ProductName', self.productName))
		self.service.add_item(IntegerItem('/Connected', 1))
		self.service.add_item(TextItem('/Serial', self._serial))
		self.service.add_item(TextItem('/CustomName', "", writeable=True, onchange=self._set_customname))
		self.service.add_item(IntegerItem('/State', MODULE_STATE_CONNECTED))
		self.service.add_item(IntegerItem('/DeviceInstance', int(self.settings.get_value(self.settings.alias('instance_{}'.format(self._serial))).split(':')[-1])))

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
			Setting('/Settings/Devices/shelly_%s/ClassAndVrmInstance' % self._serial, 'switch:50', alias='instance_{}'.format(self._serial)),
			Setting('/Settings/Devices/shelly_%s/CustomName' % self._serial, "", alias="customname"),
		)

	def set_service_type(self, _stype):
		setting = self.settings.get_value(self.settings.alias('instance_{}'.format(self._serial)))
		if setting is None:
			logger.warning("No instance setting found for {}, setting default to switch:50".format(self._serial))
			return
		stype, instance = self.role_instance(setting)

		if stype != _stype:
			p = self.settings.alias('instance_{}'.format(self._serial))
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


class ShellyDevice(ShellyService, SwitchDevice, EnergyMeter):
	_shelly_device = None
	_ws_context = None
	_aiohttp_session = None
	_server = None
	_state_change_pending = False
	_has_switch = False
	_has_em = False

	@classmethod
	async def create(cls, bus_type, event, serial, server, version=VERSION, productName="Shelly switch", processName=__file__):
		shelly = await super().create(
			bus_type=bus_type,
			product_id=0,
			tty="shelly",
			serial=serial,
			version=version,
			connection=server,
			productName=productName,
			processName=processName
		)
		shelly._server = server
		shelly._event_obj = event
		return shelly

	@property
	def serial(self):
		return self._serial

	async def start(self):
		logger.info("Starting shelly device %s", self._serial)
		options = ConnectionOptions(self._server, "", "")
		self._aiohttp_session = aiohttp.ClientSession()
		self._ws_context = WsServer()

		self._shelly_device = await RpcDevice.create(self._aiohttp_session, self._ws_context, options)
		await self._shelly_device.initialize()

		if not self._shelly_device.connected:
			logger.warning("Failed to connect to shelly device")
			return

		await self.init()

		# List shelly methods
		methods = await self.list_methods()
		if len(methods) == 0:
			logger.error("Failed to list shelly methods")
			return

		if 'Switch.GetStatus' in methods:
			channels = await self.get_channels()
			self._num_channels = len(channels)
			self._has_switch = True

			# If switch channels are present, energy metering capabilities are reported in the switch status, if present.
			# There are 3 channel shelly devices, but they cannot be used as a 3 phase switch, only as 3 separate single phase switches. 
			# Reason is that it cannot be guaranteed that all relays switch at the same time, which possibly overloads a single relay.
			# And since the API currently does not support multiple AC load devices in a single service, energy metering capabilities are disabled for multi-channel switches for now.
			if self._num_channels <= 1 and all(x in channels[0] for x in ['apower', 'voltage', 'current', 'aenergy']):
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

		if self._has_em:
			await self.setup_em()

		for channel in range(self._num_channels):
			if self._has_switch:
				await self.add_output(
					channel=channel,
					output_type=1,
					set_state_cb=partial(self.set_state_cb, channel),
					valid_functions=(1 << OutputFunction.MANUAL),
					name="Channel {}".format(channel + 1),
					customName="Shelly Switch",
				)

			if self._has_em:
				self.add_em_channel(channel)

			status = await self.request_channel_status(channel)
			if status is not None:
				self.parse_status(channel, status)

		self._shelly_device.subscribe_updates(self.device_updated)

		# Set up the service name
		stype = self.em_role if self._has_em else 'switch'
		self.set_service_type(stype)
		self.serviceName = "com.victronenergy.{}.shelly_{}".format(stype, self._serial)
		self.service.name = self.serviceName

		await self.service.register()

	async def stop(self):
		if self._shelly_device is None and self._aiohttp_session is None:
			return

		if self._shelly_device:
			await self._shelly_device.shutdown()
		await self._aiohttp_session.close()
		if self.service is not None:
			self.service.__del__()
		self.service = None
		self.settings = None
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
			for channel in range(self._num_channels):
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
				switch_prefix = "/SwitchableOutput/{}/".format(channel)
				status = STATUS_ON if status_json["output"] else STATUS_OFF
				values[switch_prefix + 'State'] = 1 if status == STATUS_ON else 0
				values[switch_prefix + 'Status'] = status

			if self._has_em:
				em_prefix = "/Ac/L{}/".format(channel + 1)
				values[em_prefix + 'Voltage'] = status_json["voltage"]
				values[em_prefix + 'Current'] = status_json["current"]
				values[em_prefix + 'Power'] = status_json["apower"]
				values[em_prefix + 'PowerFactor'] = status_json["pf"] if 'pf' in status_json else None
				# Shelly reports energy in Wh, so convert to kWh
				values[em_prefix + 'Energy/Forward'] = status_json["aenergy"]["total"] / 1000 if 'aenergy' in status_json else None
				values[em_prefix + 'Energy/Reverse'] = status_json["ret_aenergy"]["total"] / 1000 if 'ret_aenergy' in status_json else None
		except:
			pass

		with self.service as s:
			for key, value in values.items():
				if value is not None and self.service.get_item(key) is not None:
					s[key] = value

			self.update(values)

	def set_state_cb(self, channel, value):
		if self._state_change_pending:
			return False

		if self.service.get_item("/SwitchableOutput/{}/State".format(channel)) != value:
			with self.service as s:
				s["/SwitchableOutput/{}/State".format(channel)] = value

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
				logger.info("Stopping shelly device scan")
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

				# Handle event
				if e == "role_changed":
					# Role changed, restart service
					if serial in self.shellies:
						logger.info("Role changed for device %s, restarting service", serial)
						await self.restart_shelly_device(serial)
					else:
						logger.warning("Device not found: %s", serial)

				elif e == "disconnected":
					logger.warning("Shelly device %s disconnected, removing from service", serial)
					await self.stop_shelly_device(serial)
					self.remove_shelly(serial)
					return

				elif e == "stopped":
					logger.info("Shelly device %s stopped, removing from service", serial)
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
				s['/Devices/{}/Enabled'.format(serial)] = None

	async def add_shelly_device(self, serial, server):
		event = asyncio.Event()
		s = await ShellyDevice.create(
			self.bus_type,
			event,
			serial,
			server
		)

		e = asyncio.create_task(
			self.shelly_event_monitor(event, s)
		)
		await s.start()
		background_tasks.add(e)
		e.add_done_callback(partial(self.delete_shelly_device, serial))
		self.shelly_switches[serial] = {'device': s, 'event_mon': e}

	def delete_shelly_device(self, serial, fut):
		if serial in self.shelly_switches:
			del self.shelly_switches[serial]

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

		if state_change == ServiceStateChange.Added or state_change == ServiceStateChange.Updated:
			if serial not in self.shellies:
				logger.info("Found shelly device: %s", serial)
				self.shellies.append(serial)
				await self.settings.add_settings(Setting('/Settings/Devices/shelly_%s/Enabled' % serial, 0, alias="enabled_%s" % serial))
				enabled = self.settings.get_value(self.settings.alias('enabled_%s' % serial))
				try:
					self.service.add_item(TextItem('/Devices/{}/Server'.format(serial)))
					self.service.add_item(TextItem('/Devices/{}/Mac'.format(serial)))
					self.service.add_item(IntegerItem('/Devices/{}/Enabled'.format(serial), writeable=True, onchange=partial(self.on_enabled_changed, serial)))
				except:
					pass

				with self.service as s:
					s['/Devices/{}/Server'.format(serial)] = info.server[:-1]
					s['/Devices/{}/Mac'.format(serial)] = serial
					s['/Devices/{}/Enabled'.format(serial)] = enabled

				if enabled:
					self.on_enabled_changed(serial, enabled)
		elif state_change == ServiceStateChange.Removed:
			logger.info("Shelly device: %s disappeared", serial)
			self.remove_shelly(serial)

	def on_enabled_changed(self, serial, value):
		if value not in (0, 1):
			return False
		server = self.service['/Devices/{}/Server'.format(serial)]
		loop = asyncio.get_event_loop()
		task = loop.create_task(self.add_shelly_device(serial, server) if value == 1 else self.stop_shelly_device(serial))
		background_tasks.add(task)
		task.add_done_callback(background_tasks.discard)

		self.settings.set_value_async(self.settings.alias('enabled_%s' % serial), value)
		return True