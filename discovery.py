#!/usr/bin/python3

from __future__ import annotations
import sys
import os
import asyncio
from functools import partial

# aiovelib
sys.path.insert(1, os.path.join(os.path.dirname(__file__), 'ext', 'aiovelib'))
from aiovelib.service import Service, IntegerItem, TextItem
from aiovelib.localsettings import Setting
from aiovelib.client import Monitor

try:
	from dbus_fast.aio import MessageBus
except ImportError:
	from dbus_next.aio import MessageBus

from zeroconf import ServiceStateChange
from zeroconf.asyncio import (
	AsyncServiceBrowser,
	AsyncServiceInfo,
	AsyncZeroconf
)

from base import ShellyDevice
from utils import logger, wait_for_settings

background_tasks = set()

class ShellyDiscovery:
	shellies = []
	shelly_switches = {}
	service = None

	def __init__(self, bus_type):
		self.bus_type = bus_type
		self.aiobrowser = None
		self.aiozc = None

	async def start(self):
		# Connect to dbus, localsettings
		self.bus = await MessageBus(bus_type=self.bus_type).connect()
		self.monitor = await Monitor.create(self.bus, itemsChanged=self.items_changed)

		self.settings = await wait_for_settings(self.bus)

		# Set up the service
		self.service = Service(self.bus, "com.victronenergy.shelly")
		self.service.add_item(IntegerItem('/Scan', 0, writeable=True,
			onchange=self.start_scan))
		await self.service.register()

		self.aiozc = AsyncZeroconf()
		self.aiobrowser = AsyncServiceBrowser(
			self.aiozc.zeroconf, ["_shelly._tcp.local."], handlers=[self.on_service_state_change]
		)

		await self.bus.wait_for_disconnect()

	def start_scan(self, value):
		if value == 1:
			task = asyncio.create_task(self.scan())
			background_tasks.add(task)
			task.add_done_callback(background_tasks.discard)
			return True

	async def scan(self):
		""" Start a scan for shelly devices. """
		if self.aiobrowser is not None:
			await self.aiobrowser.async_cancel()
			self.aiobrowser = AsyncServiceBrowser(
				self.aiozc.zeroconf, ["_shelly._tcp.local."], handlers=[self.on_service_state_change]
			)
		else:
			logger.warning("Shelly discovery not started, cannot scan for devices")

		with self.service as s:
			s['/Scan'] = 0

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

	def on_service_state_change(self, zeroconf, service_type, name, state_change):
		task = asyncio.get_event_loop().create_task(self.on_service_state_change_async(zeroconf, service_type, name, state_change))
		background_tasks.add(task)
		task.add_done_callback(background_tasks.discard)

	async def on_service_state_change_async(self, zeroconf, service_type, name, state_change):
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