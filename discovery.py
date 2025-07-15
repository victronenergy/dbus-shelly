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
lock = asyncio.Lock()
background_tasks = set()

class ShellyDiscovery(object):
	def __init__(self, bus_type):
		self.discovered_devices = []
		self.shellies = {}
		self.service = None
		self.settings = None
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
		self.service.add_item(IntegerItem('/Refresh', 0, writeable=True,
			onchange=self.refresh))
		await self.service.register()

		self.aiozc = AsyncZeroconf()
		self.aiobrowser = AsyncServiceBrowser(
			self.aiozc.zeroconf, ["_shelly._tcp.local."], handlers=[self.on_service_state_change]
		)

		await self.bus.wait_for_disconnect()

	async def refresh(self, item, value):
		if value == 1:
			async with lock:
				if self.aiobrowser is not None:
					await self.aiobrowser.async_cancel()
				self.aiobrowser = AsyncServiceBrowser(
					self.aiozc.zeroconf, ["_shelly._tcp.local."], handlers=[self.on_service_state_change]
				)
		item.set_local_value(0)

	def remove_discovered_device(self, serial):
		if serial in self.discovered_devices:
			self.discovered_devices.remove(serial)
			with self.service as s:
				s['/Devices/{}/Server'.format(serial)] = None
				s['/Devices/{}/Mac'.format(serial)] = None
				s['/Devices/{}/Model'.format(serial)] = None
				s['/Devices/{}/Name'.format(serial)] = None
				i = 0
				while self.service.get_item(key := f'/Devices/{serial}/{i}/Enabled') is not None:
					s[key] = None
					i += 1

	async def add_shelly_device(self, serial, server):
		event = asyncio.Event()
		s = ShellyDevice(
			bus_type=self.bus_type,
			serial=serial,
			server=server,
			event=event
		)

		e = asyncio.create_task(
			self._shelly_event_monitor(event, s)
		)
		try:
			await s.start()
		except Exception as e:
			logger.error("Failed to start shelly device %s: %s", serial, e)
			await s.stop()
			return
		e.add_done_callback(partial(self.delete_shelly_device, serial))
		self.shellies[serial] = {'device': s, 'event_mon': e}

	async def enable_shelly_channel(self, serial, channel, server):
		""" Enable a shelly channel. """
		if serial not in self.shellies:
			await self.add_shelly_device(serial, server)

		await self.shellies[serial]['device'].start_channel(channel)

	def delete_shelly_device(self, serial, fut=None):
		if serial in self.shellies:
			del self.shellies[serial]

	async def disable_shelly_channel(self, serial, channel):
		""" Disable a shelly channel. """
		if serial not in self.shellies:
			logger.error("Shelly device %s not found", serial)
			return
		self.shellies[serial]['device'].stop_channel(channel)

		if len(self.shellies[serial]['device'].active_channels) == 0:
			logger.info("No active channels left for device %s, stopping device", serial)
			await self.shellies[serial]['device'].stop()

	async def stop_shelly_device(self, serial):
		if serial in self.shellies:
			await self.shellies[serial]['device'].stop()
		else:
			logger.warning("Device not found: %s", serial)

	def items_changed(self, service, values):
		pass

	async def stop(self):
		assert self.aiozc is not None
		assert self.aiobrowser is not None
		await self.aiobrowser.async_cancel()
		await self.aiozc.async_close()

	async def _shelly_event_monitor(self, event, shelly):
		serial = shelly.serial
		try:
			while True:
				await event.wait()
				event.clear()
				e = shelly.event

				if e == "disconnected":
					logger.warning("Shelly device %s disconnected", serial)
					await self.stop_shelly_device(serial)
					self.remove_discovered_device(serial)
					return

				elif e == "stopped":
					return

				event.clear()
		except asyncio.CancelledError:
			logger.info("Shelly event monitor for %s cancelled", serial)
		return

	async def _get_device_info(self, server):
		# Only server info is needed for obtaining device info
		shelly = ShellyDevice(
			server=server
		)

		if not await shelly.connect():
			return None, 0

		if not (shelly.has_em or shelly.has_switch or shelly.has_dimming):
			logger.warning("Shelly device %s does not have an energy meter, switch or dimmer.", server)
			await shelly.stop()
			return None, 0

		if not shelly._shelly_device or not shelly._shelly_device.connected:
			logger.error("Failed to connect to shelly device %s", server)
			return None, 0

		info = await shelly.get_device_info()

		# Report shelly energy meter as device with one channel, so it shows up once in the UI
		num_channels = len(await shelly.get_channels()) if shelly.has_switch else 1

		await shelly.stop()
		del shelly
		return info, num_channels

	def on_service_state_change(self, zeroconf, service_type, name, state_change):
		task = asyncio.get_event_loop().create_task(self._on_service_state_change_async(zeroconf, service_type, name, state_change))
		background_tasks.add(task)
		task.add_done_callback(background_tasks.discard)

	async def _on_service_state_change_async(self, zeroconf, service_type, name, state_change):
		async with lock:
			info = AsyncServiceInfo(service_type, name)
			await info.async_request(zeroconf, 3000)
			if not info or not info.server:
				return
			serial = info.server.split(".")[0].split("-")[-1]

			if state_change == ServiceStateChange.Added or state_change == ServiceStateChange.Updated and serial not in self.discovered_devices:
				logger.info("Found shelly device: %s", serial)
				device_info, num_channels = await self._get_device_info(info.server)
				if device_info is None:
					logger.error("Failed to get device info for %s", serial)
					return

				# Shelly plus plug S example: 'app': 'PlusPlugS', 'model': 'SNPL-00112EU'
				model_name = device_info.get('app', device_info.get('model', 'Unknown'))
				# Custom name of the shelly device, if available
				name = device_info.get('name', None)

				for p in ['Server', 'Mac', 'Model', 'Name']:
					if self.service.get_item('/Devices/{}/{}'.format(serial, p)) is None:
						self.service.add_item(TextItem('/Devices/{}/{}'.format(serial, p), writeable=False))

				with self.service as s:
					s['/Devices/{}/Server'.format(serial)] = info.server[:-1]
					s['/Devices/{}/Mac'.format(serial)] = serial
					s['/Devices/{}/Model'.format(serial)] = model_name
					s['/Devices/{}/Name'.format(serial)] = name

				for i in range(num_channels):
					await self.settings.add_settings(Setting('/Settings/Devices/shelly_{}/{}/Enabled'.format(serial, i), 0, alias="enabled_{}_{}".format(serial, i)))
					enabled = self.settings.get_value(self.settings.alias('enabled_{}_{}'.format(serial, i)))

					if self.service.get_item('/Devices/{}/{}/Enabled'.format(serial, i)) is None:
						enabled_item = IntegerItem('/Devices/{}/{}/Enabled'.format(serial, i), writeable=True, onchange=partial(self._on_enabled_changed, serial, i))
						self.service.add_item(enabled_item)
					else:
						enabled_item = self.service.get_item('/Devices/{}/{}/Enabled'.format(serial, i))

					with self.service as s:
						s['/Devices/{}/{}/Enabled'.format(serial, i)] = enabled

					if enabled:
						await self._on_enabled_changed(serial, i, enabled_item, enabled)

					self.discovered_devices.append(serial)

			elif state_change == ServiceStateChange.Removed:
				logger.warning("Shelly device: %s disappeared", serial)
				self.remove_discovered_device(serial)

	async def _on_enabled_changed(self, serial, channel, item, value):
		if value not in (0, 1) or item.service is None:
			return

		# Set the value before first so it feels responsive to the user
		item.set_local_value(value)
		server = self.service['/Devices/{}/Server'.format(serial)]
		if value == 1:
			await self.enable_shelly_channel(serial, channel, server)
		else:
			await self.disable_shelly_channel(serial, channel)

		await self.settings.set_value(self.settings.alias('enabled_{}_{}'.format(serial, channel)), value)
