#!/usr/bin/python3

from __future__ import annotations
import sys
import os
import asyncio
import re
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

from shelly_device import ShellyDevice
from utils import logger, wait_for_settings

background_tasks = set()
ADD_BY_IP_RECHECK_SECONDS = 15 * 60

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
		self._add_by_ip_task = None
		self._add_by_ip_wakeup = asyncio.Event()
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

		if ip_addresses != "":
			self._start_add_by_ip_address_task()
			self._add_by_ip_wakeup.set()

		await self.bus.wait_for_disconnect()

	def _start_add_by_ip_address_task(self):
		if self._add_by_ip_task is None or self._add_by_ip_task.done():
			self._add_by_ip_task = asyncio.create_task(self._run_add_by_ip_periodic())

	async def _run_add_by_ip_periodic(self):
		try:
			while True:
				# Run immediately if already woken; otherwise wait for wakeup or periodic timeout.
				if self._add_by_ip_wakeup.is_set():
					self._add_by_ip_wakeup.clear()
				else:
					try:
						await asyncio.wait_for(self._add_by_ip_wakeup.wait(), timeout=ADD_BY_IP_RECHECK_SECONDS)
						self._add_by_ip_wakeup.clear()
					except asyncio.TimeoutError:
						# Don't clear on timeout; a wakeup may have arrived just after timeout.
						pass

				ip_addresses = self.settings.get_value(self.settings.alias('ipaddresses'))
				if ip_addresses == "":
					return

				await self._add_devices_by_ip(ip_addresses.split(','))
		except asyncio.CancelledError:
			raise
		finally:
			self._add_by_ip_task = None
			self._add_by_ip_wakeup.clear()

	def _cancel_add_by_ip_address_task(self):
		if self._add_by_ip_task is not None and not self._add_by_ip_task.done():
			self._add_by_ip_task.cancel()
		self._add_by_ip_task = None
		self._add_by_ip_wakeup.clear()

	async def _on_ip_addresses_changed(self, item, value):
		if value == "":
			if self.settings.get_value(self.settings.alias('ipaddresses')) != "":
				await self.settings.set_value(self.settings.alias('ipaddresses'), value)
			item.set_local_value(value)
			self._cancel_add_by_ip_address_task()
			return

		for ip in value.split(','):
			# Validate IP address format
			rgx = re.compile(
				r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?::[0-9]{1,5})?$"
			)
			if not rgx.match(ip):
				logger.error("Invalid IP address format: %s", ip)
				return
			if ":" in ip:
				port_str = ip.rsplit(":", 1)[1]
				if not port_str.isdigit() or not 1 <= int(port_str) <= 65535:
					logger.error("Invalid port in IP address: %s", ip)
					return
		if value != self.settings.get_value(self.settings.alias('ipaddresses')):
			await self.settings.set_value(self.settings.alias('ipaddresses'), value)
		item.set_local_value(value)
		self._start_add_by_ip_address_task()
		self._add_by_ip_wakeup.set()

	async def _add_devices_by_ip(self, ipaddresses):
		# Track which serials are in saved_devices for safe iteration
		saved_serials = list(self.saved_devices)

		for serial in saved_serials:
			device_ip = self.service.get_item('/Devices/{}/Ip'.format(serial))

			# Check if a manually added device's IP is no longer in the list
			if device_ip is not None and device_ip.value not in ipaddresses:
				# Remove from saved_devices since the manual IP is gone
				if serial in self.saved_devices:
					self.saved_devices.remove(serial)

				# If device is still discoverable via mDNS, update DiscoveryType to reflect that
				if serial in self.discovered_devices:
					with self.service as s:
						s['/Devices/{}/DiscoveryType'.format(serial)] = 'mDNS'
				# Otherwise, if not enabled and not discoverable via mDNS, fully remove the device
				elif serial not in self.shellies:
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

		# Clear refresh path in case it was triggered.
		if self.service['/Refresh'] != 0:
			with self.service as s:
				s['/Refresh'] = 0

	async def refresh(self, item, value):
		if value == 1:
			# Instead of setting the value back to 0 immediately, set it to 1 here and reset it later.
			# This is needed to make sure refresh can be triggered again later.
			item.set_local_value(value)
			# Delete discovered devices that are currently disabled.
			for serial in self.discovered_devices + self.saved_devices:
				delete = True
				i = 1
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
			ip_addresses = self.settings.get_value(self.settings.alias('ipaddresses'))
			if ip_addresses != "":
				self._start_add_by_ip_address_task()
				self._add_by_ip_wakeup.set()
			else:
				self._cancel_add_by_ip_address_task()

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
			for path in ['Enabled', 'Type']:
				i = 1
				while self.service.get_item(key := f'/Devices/{serial}/{i}/{path}') is not None:
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
		self._cancel_add_by_ip_address_task()
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

				# Usually happens when the device profile has changed. E.g. from triphase measuring to monophase measuring.
				# The number of channels and their capabilities have changed, so stop all channels and refresh the device info.
				elif e == "capabilities_changed":
					logger.info("The capabilities of shelly device %s have changed, disabling all channels", serial)
					# Disable all channels and reset the Enabled setting.
					await self.stop_and_disable_all_channels(serial)
					# Refresh device info
					await self.refresh_device(serial)

				event.clear()
		except asyncio.CancelledError:
			logger.info("Shelly event monitor for %s cancelled", serial)
		return

	async def stop_and_disable_all_channels(self, serial):
		# Not only disables all channels, but also clears the Enabled setting.
		try:
			i = 1
			while self.service.get_item(key := f'/Devices/{serial}/{i}/Enabled') is not None:
				enabled_item = self.service.get_item(key)
				if enabled_item is not None and enabled_item.value == 1:
					ch_type = self.service.get_item(f'/Devices/{serial}/{i}/Type')
					# Call callback to disable channel and update the setting.
					await self._on_enabled_changed(serial, f"{ch_type}_{i-1}", enabled_item, 0)
				i += 1
		except Exception as e:
			logger.error("Error while stopping channels of shelly device %s: %s", serial, e)

	async def refresh_device(self, serial):
		if serial not in self.shellies:
			logger.error("Device not found for refresh: %s", serial)
			return

		shelly = self.shellies[serial]['device']
		host = shelly.server

		self.remove_discovered_device(serial)
		self.delete_shelly_device(serial)
		await self._add_device(host, serial)

	async def _get_device_info(self, server, serial=None):
		ip = None
		info = None
		channel_info = []
		# Only server info is needed for obtaining device info
		shelly = ShellyDevice(
			server=server,
			serial=serial
		)

		try:
			if not await shelly.connect():
				raise Exception()

			if not shelly.is_supported():
				logger.warning("Unsupported shelly device: %s", server)
				raise Exception()

			if not shelly._shelly_device or not shelly._shelly_device.connected:
				logger.error("Failed to connect to shelly device %s", server)
				raise Exception()

			info = shelly.shelly_info
			ip = shelly.server

			channel_info = shelly.channel_info

		except:
			pass
		finally:
			await shelly.stop()
			del shelly
		return ip, info, channel_info

	def on_service_state_change(self, zeroconf, service_type, name, state_change):
		rgx = re.compile(
			r"^shelly[\d\w\-]+-[0-9a-f]{12}\._shelly\._tcp\.local\.$"
		)

		if rgx.match(name):
			task = asyncio.get_event_loop().create_task(self._on_service_state_change_async(zeroconf, service_type, name, state_change))
			task.add_done_callback(background_tasks.discard)
			background_tasks.add(task)

	async def _add_device(self, server, serial=None, manual=False):
		ip, device_info, channel_info = await self._get_device_info(server, serial)
		if device_info is None:
			logger.error("Failed to get device info for %s", server)
			return

		if serial is None:
			serial = device_info.get('mac', 'unknown').replace(":", "")

		# Track if this is a known (previously discovered) device
		known = serial in self.discovered_devices or serial in self.saved_devices

		# Handle device inventory: manual devices take priority over mDNS-discovered devices.
		# If a device is found by both methods, it should remain in both lists to preserve origin info.
		if manual:
			# Add to saved devices (or ensure it's there if not already)
			if serial not in self.saved_devices:
				self.saved_devices.append(serial)
		else:
			# If device hasn't been discovered yet (either path), add to discovered
			if serial not in self.discovered_devices and serial not in self.saved_devices:
				self.discovered_devices.append(serial)

		# 'app' is a more user-friendly name for the model. Use that if available.
		# Shelly plus plug S example: 'app': 'PlusPlugS', 'model': 'SNPL-00112EU'
		model_name = device_info.get('app', device_info.get('model', 'unknown'))
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
			# DiscoveryType reflects current state: 'Manual' if in saved_devices, 'mDNS' if only in discovered_devices
			s['/Devices/{}/DiscoveryType'.format(serial)] = 'Manual' if serial in self.saved_devices else 'mDNS'

		# Skip channel setup for already-known devices; their dbus items and settings are already configured
		if known:
			return

		for i, ch in enumerate(channel_info):
			ch_type = ch.split('_')[0] # 'switch', 'em'

			# Don't encode the channel type in the setting path to remain compatible with older versions.
			# There are two types of channels: 'switch' and 'em'. Switch channels are enumerated first, then em channels.
			# Note, settings are stored 0-indexed, while the channels on dbus are 1 indexed.
			await self.settings.add_settings(Setting(f'/Settings/Devices/shelly_{serial}/{i}/Enabled', 0, alias=f"enabled_{serial}_{ch}"))
			enabled = self.settings.get_value(self.settings.alias(f"enabled_{serial}_{ch}"))

			if self.service.get_item(f'/Devices/{serial}/{i + 1}/Enabled') is None:
				self.service.add_item(IntegerItem(f'/Devices/{serial}/{i + 1}/Enabled',
									  writeable=True, onchange=partial(self._on_enabled_changed, serial, ch)))
			if self.service.get_item(f'/Devices/{serial}/{i + 1}/Type') is None:
				self.service.add_item(TextItem(f'/Devices/{serial}/{i + 1}/Type', writeable=False))

			with self.service as s:
				s[f'/Devices/{serial}/{i + 1}/Type'] = ch_type
				s[f'/Devices/{serial}/{i + 1}/Enabled'] = enabled

			if enabled:
				enabled_item = self.service.get_item(f'/Devices/{serial}/{i + 1}/Enabled')
				await self._on_enabled_changed(serial, ch, enabled_item, enabled)

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
			await self.settings.set_value(self.settings.alias(f'enabled_{serial}_{channel}'), value)