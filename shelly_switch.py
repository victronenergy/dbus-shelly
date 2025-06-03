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

from zeroconf import ServiceStateChange, Zeroconf
from zeroconf.asyncio import (
	AsyncServiceBrowser,
	AsyncServiceInfo,
	AsyncZeroconf,
	AsyncZeroconfServiceTypes,
)
from aioshelly.common import ConnectionOptions
from aioshelly.rpc_device import RpcDevice, WsServer
from aioshelly.rpc_device import RpcDevice, WsServer

from switch_device import SwitchDevice, STATUS_ON, STATUS_OFF
from switch_device_rm import SwitchDeviceWithRm

VERSION = "0.1"
logger = logging.getLogger('dbus-shelly')
logger.setLevel(logging.DEBUG)
background_tasks = set()

class ShellySwitch(SwitchDeviceWithRm):
	shelly_device = None
	ws_context = None
	aiohttp_session = None
	_server = None
	_mac = None
	_num_channels = 0
	_state_change_pending = False

	@property
	def num_channels(self):
		return self._num_channels

	def get_power(self, channel):
		return self.service.get_item("/SwitchableOutput/{}/P".format(channel)).value

	@classmethod
	async def create(cls, bus_type, mac, server, version=VERSION, productName="Shelly switch", processName=__file__):
		shelly = await super().create(
			bus_type=bus_type,
			product_id=0,
			tty="shelly",
			serial=mac,
			version=version,
			connection=server,
			productName=productName,
			processName=processName
		)
		shelly._server = server
		shelly._mac = mac
		return shelly

	async def start(self):
		logger.info("Starting shelly device %s", self._mac)
		options = ConnectionOptions(self._server, "", "")
		self.aiohttp_session = aiohttp.ClientSession()
		self.ws_context = WsServer()

		self.shelly_device = await RpcDevice.create(self.aiohttp_session, self.ws_context, options)
		await self.shelly_device.initialize()

		if not self.shelly_device.connected:
			logger.warning("Failed to connect to shelly device")
			return

		await self.init()

		self._num_channels = await self.get_number_of_channels()
		for channel in range(self._num_channels):
			await self.add_output(
				channel=channel,
				output_type=1,
				set_state_cb=partial(self.set_state_cb, channel),
				valid_functions=self.CHANNEL_VALID_FUNCTIONS,
				name="Switch",
				customName="Shelly Switch",
			)

			self.shelly_device.subscribe_updates(partial(self.device_updated, channel))
			status = await self.request_channel_status(channel)
			if status is not None:
				self.parse_status(channel, status)

		await self.service.register()

	async def get_number_of_channels(self):
		channel = 0
		while True:
			try:
				await self.shelly_device.call_rpc(
				"Switch.GetStatus",
				{
					# id is the switch channel, starting from 0
					"id":channel
				}
			)
			except:
				break
			channel += 1
		return channel

	async def request_channel_status(self, channel):
		resp = None
		try:
			resp = await self.shelly_device.call_rpc(
				"Switch.GetStatus",
				{
					# id is the switch channel, starting from 0
					"id":channel
				}
			)
		except:
			logger.warning("Failed to get status for channel %d", channel)
		return resp

	async def close(self):
		await self.shelly_device.shutdown()
		await self.aiohttp_session.close()
		if self.service is not None:
			self.service.__del__()
		self.service = None
		self.settings = None
		self.ws_context = None
		self.shelly_device = None
		self.aiohttp_session = None

	def device_updated(self, channel, cb_device, update_type):
		switch="switch:{}".format(channel)

		# Update status
		if switch in cb_device.status:
			self.parse_status(channel, cb_device.status[switch])

	def parse_status(self, channel, status_json):
		status = STATUS_ON if status_json["output"] else STATUS_OFF

		values = {}
		try:
			values['State'] = 1 if status == STATUS_ON else 0
			values['Status'] = status
			values['V'] = status_json["voltage"]
			values['I'] = status_json["current"]
			values['P'] = status_json["apower"]
			values['T'] = status_json["temperature"]["tC"]
		except:
			pass

		self.set_channel_values(channel, values)

	def set_state_cb(self, channel, value):
		if self._state_change_pending:
			return False

		if self.service.get_item("/SwitchableOutput/{}/State".format(channel)) != value:
			with self.service as s:
				s["/SwitchableOutput/{}/State".format(channel)] = value

		self._state_change_pending = True
		self.state = value

		task = self._runningloop.create_task(self.shelly_device.call_rpc(
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

class SettingsMonitor(Monitor):
	def __init__(self, bus, **kwargs):
		super().__init__(bus, handlers = {
			'com.victronenergy.settings': SettingsClient
		}, **kwargs)

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
		self.monitor = await Monitor.create(self.bus, self.settings_changed)

		await self.wait_for_settings()

		# Set up the service
		self.service = Service(self.bus, "com.victronenergy.shelly")
		await self.service.register()

		self.aiozc = AsyncZeroconf()
		services = list(
			await AsyncZeroconfServiceTypes.async_find(aiozc=self.aiozc)
		)

		self.aiobrowser = AsyncServiceBrowser(
			self.aiozc.zeroconf, services, handlers=[self.on_service_state_change]
		)
		await self.bus.wait_for_disconnect()

	async def wait_for_settings(self):
		""" Attempt a connection to localsettings. """
		settingsmonitor = await SettingsMonitor.create(self.bus)
		self.settings = await asyncio.wait_for(
			settingsmonitor.wait_for_service(SETTINGS_SERVICE), 5)

	async def add_shelly(self, mac, server):
		s = await ShellySwitch.create(
			self.bus_type,
			mac,
			server,
		)
		await s.start()
		self.shelly_switches[mac] = s

	async def remove_shelly(self, mac):
		if mac in self.shelly_switches:
			await self.shelly_switches[mac].close()
			logger.info("Deleting device %s ", mac)
			self.shelly_switches[mac] = None
		else:
			logger.warning("Device not found: ", mac)

	def settings_changed(self, service, values):
		pass

	async def async_close(self):
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
		mac = info.server.split(".")[0].split("-")[-1]

		if state_change == ServiceStateChange.Added:
			if mac not in self.shellies:
				logger.info("Found shelly device: %s", mac)
				self.shellies.append(mac)
				await self.settings.add_settings(Setting('/Settings/Devices/shelly_%s/Enabled' % mac, 0, alias="enabled_%s" % mac))
				enabled = self.settings.get_value(self.settings.alias('enabled_%s' % mac))
				self.service.add_item(TextItem('/Devices/{}/Server'.format(mac), info.server[:-1]))
				self.service.add_item(TextItem('/Devices/{}/Mac'.format(mac), mac))
				self.service.add_item(IntegerItem('/Devices/{}/Enabled'.format(mac), value=enabled, writeable=True, onchange=partial(self.on_enabled_changed, mac)))
				if enabled:
					self.on_enabled_changed(mac, enabled)
		elif state_change == ServiceStateChange.Removed:
			if mac in self.shellies:
				logger.info("Shelly device: %s disappeared", mac)
				self.shellies.remove(mac)
				self.service.remove_item('/Devices/{}/Server'.format(mac))
				self.service.remove_item('/Devices/{}/Mac'.format(mac))
				self.service.remove_item('/Devices/{}/Enabled'.format(mac))

	def on_enabled_changed(self, mac, value):
		if value not in (0, 1):
			return False
		server = self.service['/Devices/{}/Server'.format(mac)]
		loop = asyncio.get_event_loop()
		task = None
		if value == 1:
			task = loop.create_task(self.add_shelly(mac, server))
		elif value == 0:
			task = loop.create_task(self.remove_shelly(mac))

		if task:
			background_tasks.add(task)
			task.add_done_callback(background_tasks.discard)

		self.settings.set_value_async(self.settings.alias('enabled_%s' % mac), value)
		return True