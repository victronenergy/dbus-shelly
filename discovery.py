#!/usr/bin/python3

from __future__ import annotations
import sys
import os
import asyncio
import re
from functools import partial

# aiovelib
sys.path.insert(1, os.path.join(os.path.dirname(__file__), 'ext', 'aiovelib'))
from aiovelib.service import Service, IntegerItem, TextItem, TextArrayItem
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

class ShellyDiscovery(object):
	def __init__(self, bus_type):
		self.discovered_devices = []
		self.saved_devices = []
		self.shellies = {}
		self.service = None
		self.settings = None
		self.bus_type = bus_type
		self.aiobrowser = None
		self.aiozc = None
		self._mdns_lock = asyncio.Lock()
		self._shelly_lock = asyncio.Lock()
		self._enable_tasks = {}

	async def start(self):
		# Connect to dbus, localsettings
		self.bus = await MessageBus(bus_type=self.bus_type).connect()
		self.monitor = await Monitor.create(self.bus, itemsChanged=self.items_changed)

		self.settings = await wait_for_settings(self.bus)

		# Set up the service
		self.service = Service(self.bus, "com.victronenergy.shelly")
		await self.settings.add_settings(Setting('/Settings/Shelly/IpAddresses', "", alias="ipaddresses"))

		ip_addresses = self.settings.get_value(self.settings.alias('ipaddresses'))

		self.service.add_item(IntegerItem('/Refresh', 0, writeable=True,
			onchange=self.refresh))
		self.service.add_item(TextItem('/IpAddresses', ip_addresses, writeable=True, onchange=self._on_ip_addresses_changed))
		await self.service.register()

		self.aiozc = AsyncZeroconf()
		self.aiobrowser = AsyncServiceBrowser(
			self.aiozc.zeroconf, ["_shelly._tcp.local."], handlers=[self.on_service_state_change]
		)

		self._start_add_by_ip_address_task(ip_addresses)

		await self.bus.wait_for_disconnect()

	def _start_add_by_ip_address_task(self, ip_addresses):
		task = asyncio.create_task(self._add_devices_by_ip(ip_addresses.split(',')))
		background_tasks.add(task)
		task.add_done_callback(background_tasks.discard)

	async def _on_ip_addresses_changed(self, item, value):
		if value == "":
			if self.settings.get_value(self.settings.alias('ipaddresses')) != "":
				await self.settings.set_value(self.settings.alias('ipaddresses'), value)
			item.set_local_value(value)
			return

		for ip in value.split(','):
			# Validate IP address format
			rgx = re.compile(
				r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$"
			)
			if not rgx.match(ip):
				logger.error("Invalid IP address format: %s", ip)
				return
		if value != self.settings.get_value(self.settings.alias('ipaddresses')):
			await self.settings.set_value(self.settings.alias('ipaddresses'), value)
		item.set_local_value(value)

		self._start_add_by_ip_address_task(value)

	async def _add_devices_by_ip(self, ipaddresses):
		for serial in self.saved_devices:
			device_ip = self.service.get_item('/Devices/{}/Ip'.format(serial))

			# Remove manually added devices that are no longer in the list, but only if they are not currently enabled.
			if device_ip is not None and device_ip.value not in ipaddresses and serial not in self.shellies:
				await self.stop_shelly_device(serial)
				self.remove_discovered_device(serial)

		for ip in set(ipaddresses): # Use set to avoid duplicates
			ip_found = False
			# Check if we already have this device
			for serial in self.discovered_devices + self.saved_devices:
				device_ip = self.service.get_item('/Devices/{}/Ip'.format(serial))
				if device_ip is not None and device_ip.value == ip:
					logger.info("Device with IP %s already found, SN: %s", ip, serial)
					ip_found = True
					break

			if not ip_found:
				await self._add_device(ip, serial=None, manual=True)

	async def refresh(self, item, value):
		if value == 1:
			# Delete discovered devices that are currently disabled.
			for serial in self.discovered_devices + self.saved_devices:
				delete = True
				i = 0
				while (True):
					enabled_item = self.service.get_item(f'/Devices/{serial}/{i}/Enabled')
					if enabled_item is None:
						break
					# Only delete if all channels are disabled
					if enabled_item.value == 1:
						delete = False
						break
					i += 1
				if delete:
					# Remove from the list if not enabled
					self.remove_discovered_device(serial)
				elif serial in self.shellies:
					# Try reconnecting if enabled.
					self.shellies[serial]['device'].do_reconnect()
			async with self._mdns_lock:
				if self.aiobrowser is not None:
					await self.aiobrowser.async_cancel()
				self.aiobrowser = AsyncServiceBrowser(
					self.aiozc.zeroconf, ["_shelly._tcp.local."], handlers=[self.on_service_state_change]
				)

			# Retry adding the manually added IP addresses
			self._start_add_by_ip_address_task(self.settings.get_value(self.settings.alias('ipaddresses')))
		item.set_local_value(0)

	def remove_discovered_device(self, serial):
		if serial in self.discovered_devices:
			self.discovered_devices.remove(serial)
		elif serial in self.saved_devices:
			self.saved_devices.remove(serial)
		else:
			return
		with self.service as s:
			s['/Devices/{}/Ip'.format(serial)] = None
			s['/Devices/{}/Mac'.format(serial)] = None
			s['/Devices/{}/Model'.format(serial)] = None
			s['/Devices/{}/Name'.format(serial)] = None
			s['/Devices/{}/DiscoveryType'.format(serial)] = None
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

		# Sync device creation to prevent creating a device multiple times
		# when enabling multiple channels at once.
		async with self._shelly_lock:
			if serial not in self.shellies:
				await self.add_shelly_device(serial, server)

		return await self.shellies[serial]['device'].start_channel(channel)

	def delete_shelly_device(self, serial, fut=None):
		if serial in self.shellies:
			del self.shellies[serial]

	async def disable_shelly_channel(self, serial, channel):
		""" Disable a shelly channel. """
		if serial not in self.shellies:
			return False
		await self.shellies[serial]['device'].stop_channel(channel)

		if len(self.shellies[serial]['device'].active_channels) == 0:
			logger.info("No active channels left for device %s, stopping device", serial)
			await self.shellies[serial]['device'].stop()
		return True

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
		ip = None
		info = None
		num_channels = 0
		# Only server info is needed for obtaining device info
		shelly = ShellyDevice(
			server=server
		)

		try:
			if not await shelly.connect():
				raise Exception()

			if not (shelly.has_em or shelly.has_switch or shelly.has_dimming):
				logger.warning("Shelly device %s does not have an energy meter, switch or dimmer.", server)
				await shelly.stop()
				raise Exception()

			if not shelly._shelly_device or not shelly._shelly_device.connected:
				logger.error("Failed to connect to shelly device %s", server)
				raise Exception()

			info = await shelly.get_device_info()
			ip = shelly.server

			# Report shelly energy meter as device with one channel, so it shows up once in the UI
			num_channels = len(await shelly.get_channels()) if shelly.has_switch else 1

		except:
			pass
		finally:
			await shelly.stop()
			del shelly
		return ip, info, num_channels

	def on_service_state_change(self, zeroconf, service_type, name, state_change):
		rgx = re.compile(
			r"^shelly[\d\w\-]+-[0-9a-f]{12}\._shelly\._tcp\.local\.$"
		)

		if rgx.match(name):
			task = asyncio.get_event_loop().create_task(self._on_service_state_change_async(zeroconf, service_type, name, state_change))
			task.add_done_callback(background_tasks.discard)
			background_tasks.add(task)

	async def _add_device(self, server, serial=None, manual=False):
		ip, device_info, num_channels = await self._get_device_info(server)
		if device_info is None:
			logger.error("Failed to get device info for %s", server)
			return

		if serial is None:
			serial = device_info.get('mac', 'unknown').replace(":", "")

		if manual:
			self.saved_devices.append(serial)
		else:
			self.discovered_devices.append(serial)

		# Shelly plus plug S example: 'app': 'PlusPlugS', 'model': 'SNPL-00112EU'
		model_name = device_info.get('app', device_info.get('model', 'Unknown'))
		# Custom name of the shelly device, if available
		name = device_info.get('name', None)

		for p in ['Ip', 'Mac', 'Model', 'Name', 'DiscoveryType']:
			if self.service.get_item('/Devices/{}/{}'.format(serial, p)) is None:
				self.service.add_item(TextItem('/Devices/{}/{}'.format(serial, p), writeable=False))

		with self.service as s:
			s['/Devices/{}/Ip'.format(serial)] = ip
			s['/Devices/{}/Mac'.format(serial)] = serial
			s['/Devices/{}/Model'.format(serial)] = model_name
			s['/Devices/{}/Name'.format(serial)] = name
			s['/Devices/{}/DiscoveryType'.format(serial)] = 'Manual' if manual else 'mDNS'

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

	async def _on_service_state_change_async(self, zeroconf, service_type, name, state_change):
		async with self._mdns_lock:
			info = AsyncServiceInfo(service_type, name)
			await info.async_request(zeroconf, 3000)
			if not info or not info.server:
				return
			serial = info.server.split(".")[0].split("-")[-1]

			if (state_change == ServiceStateChange.Added or state_change == ServiceStateChange.Updated) and serial not in self.discovered_devices + self.saved_devices:
				logger.info("Found shelly device: %s", serial)
				await self._add_device(info.server[:-1], serial)

			elif state_change == ServiceStateChange.Removed and serial in self.discovered_devices:
				logger.warning("Shelly device: %s disappeared", serial)
				if serial in self.shellies:
					self.shellies[serial]['device'].do_reconnect()

	async def _on_enabled_changed(self, serial, channel, item, value):
		if value not in (0, 1) or item.service is None:
			return

		if value == 1:
			server = self.service['/Devices/{}/Ip'.format(serial)]
			# Start enabling a channel as a task, so multiple channels can be enabled simultaneously.
			task = asyncio.create_task(self.enable_shelly_channel(serial, channel, server))
			# Keep track of enabling tasks per device, so we can wait for them to finish when disabling channels.
			if serial not in self._enable_tasks:
				self._enable_tasks[serial] = set()
			self._enable_tasks[serial].add(task)
			task.add_done_callback(self._enable_tasks[serial].discard)
			ret = True
		else:
			if serial in self._enable_tasks:
				# Wait for any ongoing enable task on this device to finish before disabling
				await asyncio.gather(*self._enable_tasks[serial])
				self._enable_tasks[serial].clear()
			ret = await self.disable_shelly_channel(serial, channel)

		if ret:
			item.set_local_value(value)
			await self.settings.set_value(self.settings.alias('enabled_{}_{}'.format(serial, channel)), value)
