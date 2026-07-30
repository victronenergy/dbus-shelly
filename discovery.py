#!/usr/bin/python3

from __future__ import annotations
import sys
import os
import asyncio
import re
from functools import partial
from enum import Enum
from typing import Any
from dataclasses import dataclass, field
import time

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
DEVICE_RECHECK_SECONDS = 15 * 60 # Retry interval for devices that have been successfully connected to, to check if they are still reachable.

ENDPOINT_SOURCE_MANUAL = "Manual"
ENDPOINT_SOURCE_MDNS = "mDNS"

MDNS_RETRY_WINDOW_SECONDS = 24 * 60 * 60
BACKOFF_INITIAL_SECONDS = 10 # Initial backoff for failed connection attempts, doubles with each failure up to BACKOFF_MAX_SECONDS.
BACKOFF_MAX_SECONDS = 10 * 60
RECONNECT_LOOP_HEARTBEAT_SECONDS = BACKOFF_INITIAL_SECONDS # Interval for the reconnect loop to check device connectivity. Should be equal to or less than BACKOFF_INITIAL_SECONDS to ensure timely retries.

cache_lock = asyncio.Lock()

# ProbeStatus is used to indicate the result of a connection attempt to a Shelly device.
class ProbeStatus(str, Enum):
	SUPPORTED = "supported"
	UNSUPPORTED = "unsupported"
	UNREACHABLE = "unreachable"
	ERROR = "error"

	def __str__(self) -> str:
		return self.value

@dataclass(slots=True)
class DeviceProbeResult:
	status: ProbeStatus = ProbeStatus.ERROR
	ip: str | None = None
	info: dict[str, Any] | None = None
	channel_info: dict[int, dict[str, Any]] = field(default_factory=dict)

# ShellyEndpoint represents a discovered Shelly device endpoint, including its serial number,
# endpoint address, source of discovery (manual or mDNS), and connection status.
@dataclass
class ShellyEndpoint:
	serial: str | None
	host: str	# The hostname or IP address of the Shelly device.
	source: str  # "Manual" or "mDNS"
	supported: bool | None = None  # None => unknown, True => supported, False => unsupported
	model: str | None = None # Only used for printing

# ConnectMeta tracks the connection retry state for a Shelly device, 
# including the number of attempts, next retry time, and whether the connection has expired.
@dataclass
class ConnectMeta:
	created_at: float = field(default_factory=time.time)
	next_retry_at: float = field(default_factory=time.time)
	attempts: int = 0
	success: bool = False
	deadline_at: float | None = None  # None => retry forever
	retry_window_s: int = None
	last_seen: float | None = None

	@property
	def last_seen_str(self) -> str | None:
		return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.last_seen)) if self.last_seen is not None else None

	@classmethod
	def manual(cls) -> "ConnectMeta":
		now = time.time()
		return cls(
			created_at=now,
			next_retry_at=now,
			attempts=0,
			deadline_at=None,
			retry_window_s=None,
			last_seen=None,
		)

	@classmethod
	def mdns(cls, retry_window_s: int = MDNS_RETRY_WINDOW_SECONDS) -> "ConnectMeta":
		now = time.time()
		return cls(
			created_at=now,
			next_retry_at=now,
			attempts=0,
			deadline_at=now + retry_window_s,	# mDNS connections expire after the retry window.
			retry_window_s=retry_window_s,
			last_seen=None
		)

	# Immediately retry connecting, set the deadline to the retry window from now and clear the attempts1.
	# Does not change the connected state.
	def refresh(self) -> None:
		now = time.time()
		self.next_retry_at = now
		if self.retry_window_s is not None:
			self.deadline_at = now + self.retry_window_s
		self.attempts = 0

	def due(self, now: float | None = None) -> bool:
		now = time.time() if now is None else now
		return now >= self.next_retry_at

	def expired(self, now: float | None = None) -> bool:
		if self.deadline_at is None:
			return False
		now = time.time() if now is None else now
		return now >= self.deadline_at

	def mark_failure(self, now: float | None = None) -> None:
		now = time.time() if now is None else now
		self.attempts += 1
		delay = min(BACKOFF_INITIAL_SECONDS * (2 ** (self.attempts - 1)), BACKOFF_MAX_SECONDS)
		self.next_retry_at = now + delay
		self.success = False

	def mark_success(self, now: float | None = None) -> None:
		now = time.time() if now is None else now
		self.attempts = 0
		self.next_retry_at = now + DEVICE_RECHECK_SECONDS	# Check again after a fixed interval to see if the device is still reachable.
		self.success = True
		self.last_seen = now

# ShellyEndpointState represents the state of a Shelly device endpoint, including its connection status and retry metadata.
@dataclass
class ShellyEndpointState:
	endpoint: ShellyEndpoint
	connect: ConnectMeta | None = None
	deviceInfo: dict[str, Any] | None = None
	channel_info: dict[int, dict[str, Any]] = field(default_factory=dict)

	@property
	def is_connecting(self) -> bool:
		return self.connect is not None and self.connect.success is False

	@property
	def is_connected(self) -> bool:
		return self.connect is not None and self.connect.success is True

	# Make a ConnectMeta for this endpoint, based on its source.
	def start_connect(self) -> None:
		if self.endpoint.source == ENDPOINT_SOURCE_MANUAL:
			self.connect = ConnectMeta.manual()
		else:
			self.connect = ConnectMeta.mdns()

	def stop_connect(self) -> None:
		self.connect = None

	def update_endpoint(self, host: str, source: str | None = None) -> None:
		self.endpoint.host = host
		if source is not None:
			self.endpoint.source = source

class ShellyDeviceCache:
	"""Caches Shelly devices that have been discovered or manually added.

	Tracks the last known endpoint for each device, and manages retry policies
	for reconnecting to devices that have gone offline.
	"""

	def __init__(self, cache_file: str | None = None):
		self._cache: dict[str, ShellyEndpointState] = {}
		self._cache_changes: asyncio.Queue[None] = asyncio.Queue()
		self._device_changes: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

		# Start task to print cache state every 30 seconds for debugging purposes
		if cache_file is not None:
			self._cache_file = cache_file
			asyncio.create_task(self._periodic_cache_print())

	async def _periodic_cache_print(self):
		retries = 0
		while True:
			try:
				await self.print_cache_to_file()
			except Exception as e:
				if retries > 3:
					logger.error("Failed (%d times) to print cache to file, not retrying: %s", retries, e)
					return
				retries += 1
			else:
				retries = 0
			await asyncio.sleep(30)

	async def print_cache_to_file(self):
		# Pretty print to file in table form
		with open(self._cache_file, "w") as f:
			f.write("Updated: {}\n".format(time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())))
			f.write(f"{'Serial':<15}{'Endpoint':<40}{'Model':<15}{'Source':<10}{'Supported':<10}{'Last Seen':<20}{'Connecting':<12}{'Next Retry At':<20}\n")
			f.write("=" * 142 + "\n")
			async with cache_lock:
				for key, state in self._cache.items():
					serial = state.endpoint.serial or "N/A"
					host = state.endpoint.host
					source = state.endpoint.source
					last_seen = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(state.connect.last_seen)) if state.connect and state.connect.last_seen else "N/A"
					connecting = "Yes" if state.is_connecting else "No"
					next_retry_at = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(state.connect.next_retry_at)) if state.connect else "N/A"
					supported = "Unknown" if state.endpoint.supported is None else "Yes" if state.endpoint.supported else "No"
					model = state.endpoint.model or "N/A"
					f.write(f"{serial:<15}{host:<40}{model:<15}{source:<10}{supported:<10}{last_seen:<20}{connecting:<12}{next_retry_at:<20}\n")

	@staticmethod
	def _host_key(host: str) -> str:
		return f"host:{host.strip().lower()}"

	@staticmethod
	def _serial_key(serial: str) -> str:
		return f"serial:{serial.strip().lower()}"

	@staticmethod
	def _normalize_endpoint(endpoint: str) -> str:
		return endpoint.strip().lower()

	def _queue_device_change(self, action: str, state: ShellyEndpointState | None = None, identifier: str | None = None) -> None:
		change: dict[str, Any] = {
			"action": action,
			"identifier": identifier,
			"state": state,
		}
		self._device_changes.put_nowait(change)

	def _queue_cache_change(self) -> None:
		self._cache_changes.put_nowait(None)

	def _resolve_key(self, serial: str | None = None, host: str | None = None) -> str | None:
		if serial is not None:
			serial_key = self._serial_key(serial)
			if serial_key in self._cache:
				return serial_key
		if host is not None:
			host_key = self._host_key(host)
			if host_key in self._cache:
				return host_key

			# Endpoint entries can be promoted to serial-keyed entries once SN is known.
			# Fall back to searching current states by endpoint so remove-by-IP still works.
			normalized_host = self._normalize_endpoint(host)
			for key, state in self._cache.items():
				if self._normalize_endpoint(state.endpoint.host) == normalized_host:
					return key
		return None

	def get(self, serial: str | None = None, host: str | None = None) -> ShellyEndpointState | None:
		key = self._resolve_key(serial=serial, host=host)
		if key is None:
			return None
		return self._cache.get(key)

	def contains(self, serial: str) -> bool:
		return self._resolve_key(serial=serial) is not None

	def refresh(self) -> None:
		# Refresh all connect states in the cache, resetting their retry timers and clearing attempts.
		# Also retry devices that were marked as unsupported, in case they are supported now (can happen when the profile is changed)
		for target in self.all_targets():
			if target.endpoint.supported is False:
				target.endpoint.supported = None
				target.start_connect()
			if target.connect is not None:
				target.connect.refresh()

		self._queue_cache_change()

	async def add(self, endpoint: ShellyEndpoint, start_connect = True, force_reconnect = False) -> None:
		async with cache_lock:
			current_key = self._resolve_key(serial=endpoint.serial, host=endpoint.host)
			if current_key is not None and not force_reconnect:
				return

			if current_key is not None and force_reconnect:
				# If the device is already in the cache, but we want to force a reconnect, reset its connect state.
				self._cache[current_key].start_connect()
				self._queue_cache_change()
				return

			# Key by serial if known, otherwise fallback to host.
			# When keyed by host, it will be promoted to serial once the serial is known.
			if endpoint.serial is not None:
				key = self._serial_key(endpoint.serial)
			else:
				key = self._host_key(endpoint.host)
			self._cache[key] = ShellyEndpointState(endpoint=endpoint)
			current_key = key

			if start_connect:
				self._cache[current_key].start_connect()
			self._queue_device_change("upsert", self._cache[current_key])	# ShellyManager won't listen to this event.
			self._queue_cache_change()

	async def start_connect(self, serial: str) -> None:
		async with cache_lock:
			key = self._resolve_key(serial=serial)
			if key is not None:
				self._cache[key].start_connect()
				self._queue_cache_change()

	async def remove(self, identifier: str) -> None:
		async with cache_lock:
			key = self._resolve_key(serial=identifier, host=identifier)
			if key is not None:
				removed_state = self._cache[key]
				del self._cache[key]
				self._queue_device_change("remove", removed_state, identifier=identifier)
				self._queue_cache_change()

	async def promote_endpoint_to_serial(self, host: str, serial: str) -> None:
		if host is None or serial is None or host.strip() == "" or serial.strip() == "":
			return

		async with cache_lock:
			# Find state in cache by host key
			host_key = self._host_key(host)
			state = self._cache.get(host_key)
			if state is None:
				return

			serial_key = self._serial_key(serial) # Make the serial key
			state.endpoint.serial = serial # Make sure the state has the correct serial number
			self._cache.pop(host_key) # Remove the host-keyed entry from the cache

			if serial_key in self._cache: # Device already present in cache, update the serial-keyed entry
				existing = self._cache[serial_key]
				existing.update_endpoint(state.endpoint.host, state.endpoint.source)
				if state.is_connecting:
					existing.start_connect()
			else:
				self._cache[serial_key] = state # Device not present in cache keyed by serial, add it.

	def all_targets(self) -> list[ShellyEndpointState]:
		return list(self._cache.values())

	def notify_state_changed(self, action, state: ShellyEndpointState) -> None:
		self._queue_device_change(action, state)

	# Wait on changes to the connected state of the devices in the cache
	# Used by the ShellyManager to update the DBus device inventory when a device is added, removed, or updated.
	async def wait_for_device_change(self, timeout: float | None = None) -> dict[str, Any] | None:
		if timeout is None:
			return await self._device_changes.get()
		try:
			return await asyncio.wait_for(self._device_changes.get(), timeout=timeout)
		except asyncio.TimeoutError:
			return None

	# Wait on cache entry updates, which can be triggered by device discovery or connection attempts.
	# Used by the ShellyConnectionManager to wait for the next device to attempt to connect to.
	async def wait_for_cache_change(self, timeout: float | None = None) -> None:
		if timeout is None:
			await self._cache_changes.get()
		else:
			try:
				await asyncio.wait_for(self._cache_changes.get(), timeout=timeout)
			except asyncio.TimeoutError:
				return

		# Coalesce burst updates into a single wakeup so the worker doesn't spin.
		while True:
			try:
				self._cache_changes.get_nowait()
			except asyncio.QueueEmpty:
				break


class ShellyConnectionManager:
	"""Manages a background loop that attempts to connect to Shelly devices.

	Devices can be added to the loop with a retry policy (manual or mDNS).
	The loop will attempt to connect to devices that are due for retry,
	and will remove devices that have expired (for mDNS) or succeeded.
	"""

	def __init__(self, shelly_device_cache, probe_device=None):
		self._probe_device = probe_device
		self.shelly_device_cache = shelly_device_cache
		self._loop_task: asyncio.Task | None = None

	async def start(self):
		if self._loop_task is None or self._loop_task.done():
			self._loop_task = asyncio.create_task(self._worker())

	async def stop(self):
		if self._loop_task is not None:
			self._loop_task.cancel()
			try:
				await self._loop_task
			except asyncio.CancelledError:
				pass
			self._loop_task = None

	async def _worker(self):
		try:
			while True:
				# Fetch targets and process connection attempts.
				targets = self.shelly_device_cache.all_targets()
				await asyncio.gather(*(self._attempt_connect(ep_state) for ep_state in targets if ep_state.connect is not None))

				# Wait for the next update signal or a heartbeat timeout.
				await self.shelly_device_cache.wait_for_cache_change(timeout=RECONNECT_LOOP_HEARTBEAT_SECONDS)
		except asyncio.CancelledError:
			pass

	async def _attempt_connect(self, ep_state: ShellyEndpointState) -> bool:
		try:
			now = time.time()
			async with cache_lock:
				if ep_state.connect is None:
					return False
				connect_ref = ep_state.connect
			if ep_state.connect.due(now):
				remove_entry = False
				promote_serial = False
				result = await self._probe_device(ep_state.endpoint.host, ep_state.endpoint.serial)
				action = str(result.status)
				async with cache_lock:
					if ep_state.connect is not connect_ref:
						# The connect state has changed since we started the attempt, ignore this result.
						return False

					# Supported device -> mark as supported and reset retry state
					if result.status == ProbeStatus.SUPPORTED:
						if ep_state.is_connected:
							action = "update" # If the device was already connected, this is just an update to the state.
						# First succesfull connection to this device, update info.
						else:
							ep_state.deviceInfo = result.info
							ep_state.channel_info = result.channel_info
							# Make sure the host is set to the device IP address, in case it was discovered by mDNS with a hostname.
							ep_state.endpoint.host = result.ip or ep_state.endpoint.host
							if ep_state.endpoint.serial is None and result.info is not None:
								promote_serial = True
							if ep_state.endpoint.model is None and result.info is not None:
								ep_state.endpoint.model = result.info.get('app', result.info.get('model', 'unknown'))
							ep_state.endpoint.supported = True
						ep_state.connect.mark_success(now)

					# Unsupported device -> stop retrying and mark as unsupported
					elif result.status == ProbeStatus.UNSUPPORTED:
						ep_state.stop_connect()
						ep_state.endpoint.supported = False
						logger.info("Shelly device %s at %s is unsupported, will not retry", ep_state.endpoint.serial, ep_state.endpoint.host)
					else:
						ep_state.connect.mark_failure(now)
						logger.info("Failed to connect to shelly device %s at %s, retrying in %d seconds", ep_state.endpoint.serial, ep_state.endpoint.host, ep_state.connect.next_retry_at - now)
						if ep_state.connect.expired(now):
							logger.warning("Shelly device %s has not been seen for %d hours, removing from cache", ep_state.endpoint.serial, ep_state.connect.retry_window_s // 3600)
							remove_entry = True

				if promote_serial and result.info is not None:
					serial = result.info.get('mac', 'unknown').replace(":", "")
					await self.shelly_device_cache.promote_endpoint_to_serial(ep_state.endpoint.host, serial)
				if remove_entry:
					await self.shelly_device_cache.remove(ep_state.endpoint.serial or ep_state.endpoint.host)

				self.shelly_device_cache.notify_state_changed(action, ep_state)

		except Exception as e:
			logger.error("Error while attempting to connect to shelly device %s at %s: %s", ep_state.endpoint.serial, ep_state.endpoint.host, e)

class ManualIpDiscovery(object):
	def __init__(self, add_endpoint, remove_endpoint):
		self._add_endpoint = add_endpoint
		self._remove_endpoint = remove_endpoint
		self._previous_ip_addresses = ""
		self._ip_rgx = re.compile(r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?::[0-9]{1,5})?$")

	async def start(self, ip_addresses: str):
		await self.on_ip_addresses_changed(ip_addresses)

	async def on_ip_addresses_changed(self, ip_addresses):
		try:
			ips = ip_addresses.split(',')
			ips = [ip.strip() for ip in ips if ip.strip() != ""]  # Remove empty strings and whitespace
			for ip in ips:
				if ip != "" and not self._validate_ip(ip):
					return False
		except Exception as e:
			logger.error("Error processing IP addresses: %s", e)
			return False

		# Register new IPs
		if len(ips) > 0:
			await asyncio.gather(*(self._register_endpoint(ip) for ip in ips))

		# Remove old IPs that are no longer in the list
		ips_to_remove = set(self._previous_ip_addresses.split(',')) - set(ips)
		await asyncio.gather(*(self._remove_endpoint(ip) for ip in ips_to_remove if ip))

		self._previous_ip_addresses = ip_addresses
		return True

	def _validate_ip(self, ip: str) -> bool:
		if not self._ip_rgx.match(ip):
			logger.error("Invalid IP address format: %s", ip)
			return False

		if ":" in ip:
			port_str = ip.rsplit(":", 1)[1]
			if not port_str.isdigit() or not 1 <= int(port_str) <= 65535:
				logger.error("Invalid port in IP address: %s", ip)
				return False

		return True

	async def _register_endpoint(self, ip: str):
		if not self._validate_ip(ip):
			return False

		await self._add_endpoint(serial=None, host=ip, source=ENDPOINT_SOURCE_MANUAL)
		return True


class MdnsDiscovery(object):
	"""Handles Zeroconf mDNS browsing for Shelly devices.

	Translates mDNS add/update/remove events into callback-based
	candidate and removal notifications for the manager.
	"""

	def __init__(self, add_endpoint, remove_endpoint):
		self._add_endpoint = add_endpoint
		self._remove_endpoint = remove_endpoint
		self.aiozc = None
		self.aiobrowser = None
		self._mdns_lock = asyncio.Lock()
		self._name_rgx = re.compile(r"^shelly[\d\w\-]+-[0-9a-f]{12}\._shelly\._tcp\.local\.$")

	async def start(self):
		self.aiozc = AsyncZeroconf()
		self.aiobrowser = AsyncServiceBrowser(
			self.aiozc.zeroconf,
			["_shelly._tcp.local."],
			handlers=[self.on_service_state_change],
		)

	async def restart(self):
		async with self._mdns_lock:
			if self.aiobrowser is not None:
				await self.aiobrowser.async_cancel()
			if self.aiozc is not None:
				self.aiobrowser = AsyncServiceBrowser(
					self.aiozc.zeroconf,
					["_shelly._tcp.local."],
					handlers=[self.on_service_state_change],
				)

	async def stop(self):
		if self.aiobrowser is not None:
			await self.aiobrowser.async_cancel()
			self.aiobrowser = None
		if self.aiozc is not None:
			await self.aiozc.async_close()
			self.aiozc = None

	def on_service_state_change(self, zeroconf, service_type, name, state_change):
		if not self._name_rgx.match(name):
			return
		task = asyncio.get_event_loop().create_task(
			self._on_service_state_change_async(zeroconf, service_type, name, state_change)
		)
		task.add_done_callback(background_tasks.discard)
		background_tasks.add(task)

	async def _on_service_state_change_async(self, zeroconf, service_type, name, state_change):
		async with self._mdns_lock:
			info = AsyncServiceInfo(service_type, name)
			await info.async_request(zeroconf, 3000)
			if not info or not info.server:
				return
			serial = info.server.split(".")[0].split("-")[-1]

			if state_change in (ServiceStateChange.Added, ServiceStateChange.Updated):
				# Force start connect of devices added by mDNS that are already in the cache but have not been reachable yet.
				# This makes sure that devices discovered by mDNS are retried immediately, even if they were previously unreachable and not retried yet.
				# There are (unsupported) shelly devices that show up every x minutes and then go offline again. e.g. Shelly H&T Gen3.
				# When they show up, we need to try to connect to them immediately, otherwise they will be ignored until the next retry window, and we'll likely miss them again.
				# If the connection succeeds, we'll probably find out that it is unsupported and the connection manager will stop retrying it.
				await self._add_endpoint(serial=serial, host=info.server[:-1], source=ENDPOINT_SOURCE_MDNS, force_reconnect=True)
			elif state_change == ServiceStateChange.Removed:
				await self._remove_endpoint(serial)


class ShellyManager(object):
	"""Owns Shelly device state and runtime behavior.

	Merges discovery sources, updates DBus device inventory,
	and controls device/channel lifecycle operations.
	"""
	def __init__(self, bus_type, service, settings, shelly_device_cache):
		self.bus_type = bus_type
		self.service = service
		self.settings = settings
		self.shelly_device_cache = shelly_device_cache
		self.shellies = {}
		self._shelly_lock = asyncio.Lock()
		self._enable_tasks = {}
		self._cache_sync_task: asyncio.Task | None = None

	async def _apply_cache_change(self, change: dict[str, Any]) -> None:
		state = change.get("state")
		if state is None:
			return

		serial = state.endpoint.serial
		action = change.get("action")
		last_seen = state.connect.last_seen_str if state.connect is not None else None
		if not serial:	# The serial should be known at this point. The ShellyConnectionManager will fetch and update the serial if it was previously unknown.
			return

		if action == ProbeStatus.SUPPORTED.value:
			await self._add_device(state)
		elif action == "update":
			# For now only update lastseen
			with self.service as s:
				s['/Devices/{}/LastSeen'.format(serial)] = last_seen
		# Removed from cache.
		elif action == "remove":
			await self.disable_shelly_channel(serial, channel=None)
			self.clear_discovered_device_paths(serial)

	async def start_cache_sync(self) -> None:
		if self._cache_sync_task is None or self._cache_sync_task.done():
			self._cache_sync_task = asyncio.create_task(self._cache_change_worker())

	async def stop_cache_sync(self) -> None:
		if self._cache_sync_task is not None:
			self._cache_sync_task.cancel()
			try:
				await self._cache_sync_task
			except asyncio.CancelledError:
				pass
			self._cache_sync_task = None

	async def _cache_change_worker(self):
		try:
			while True:
				change = await self.shelly_device_cache.wait_for_device_change(timeout=RECONNECT_LOOP_HEARTBEAT_SECONDS)
				if change is None:
					continue
				await self._apply_cache_change(change)
		except asyncio.CancelledError:
			pass

	def clear_discovered_device_paths(self, serial):
		with self.service as s:
			s['/Devices/{}/Ip'.format(serial)] = None
			s['/Devices/{}/Mac'.format(serial)] = None
			s['/Devices/{}/Model'.format(serial)] = None
			s['/Devices/{}/Name'.format(serial)] = None
			s['/Devices/{}/DiscoveryType'.format(serial)] = None
			for path in ['Enabled', 'Type', 'Name']:
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
			# Cancel the event monitor task if it exists and hasn't finished
			event_mon = self.shellies[serial].get('event_mon')
			if event_mon and not event_mon.done():
				event_mon.cancel()
			del self.shellies[serial]

	async def disable_shelly_channel(self, serial, channel):
		async with self._shelly_lock:
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
					self.clear_discovered_device_paths(serial)
					await self.shelly_device_cache.start_connect(serial)
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

		self.clear_discovered_device_paths(serial)	# Clear device info paths from the discovery service
		self.delete_shelly_device(serial)			# Delete the shelly device instance and stop its event monitor

		# Trigger a new connection attempt to refresh the device info and channels.
		await self.shelly_device_cache.start_connect(serial)

	async def _get_device_info(self, server, serial=None):
		result = DeviceProbeResult()
		# Only server info is needed for obtaining device info
		shelly = ShellyDevice(
			server=server,
			serial=serial
		)

		try:
			if not await shelly.connect():
				result.status = ProbeStatus.UNREACHABLE
				raise Exception()

			if not shelly.is_supported():
				result.status = ProbeStatus.UNSUPPORTED
				raise Exception()

			if not shelly._shelly_device or not shelly._shelly_device.connected:
				result.status = ProbeStatus.ERROR
				raise Exception()

			result.status = ProbeStatus.SUPPORTED
			result.info = shelly.shelly_info
			result.ip = shelly.server
			result.channel_info = shelly.channel_info

		except:
			pass
		finally:
			await shelly.stop()
			del shelly
		return result

	async def _probe_device(self, server, serial=None):
		result = await self._get_device_info(server, serial)
		return result

	async def _add_device(self, state):
		serial = state.endpoint.serial
		source = state.endpoint.source
		last_seen = state.connect.last_seen_str if state.connect is not None else None
		ip = state.endpoint.host
		device_info = state.deviceInfo or {}
		channel_info = state.channel_info or {}

		# A known device can have updated info, so we always update the device info in the service, but skip channel setup for known devices.
		# If the number of channels or its capabilities have changed, the device will emit a "capabilities_changed" event, which will trigger a refresh of the device info and channel setup.
		known = self.service.get_item('/Devices/{}/Name'.format(serial)) is not None

		# 'app' is a more user-friendly name for the model. Use that if available.
		# Shelly plus plug S example: 'app': 'PlusPlugS', 'model': 'SNPL-00112EU'
		model_name = device_info.get('app', device_info.get('model', 'unknown'))
		# Custom name of the shelly device, if available
		name = device_info.get('name', None)

		for p in ['Ip', 'Mac', 'Model', 'Name', 'DiscoveryType', 'LastSeen']:
			if self.service.get_item('/Devices/{}/{}'.format(serial, p)) is None:
				self.service.add_item(TextItem('/Devices/{}/{}'.format(serial, p), writeable=False))

		with self.service as s:
			s['/Devices/{}/Ip'.format(serial)] = ip
			s['/Devices/{}/Mac'.format(serial)] = serial
			s['/Devices/{}/Model'.format(serial)] = model_name
			s['/Devices/{}/Name'.format(serial)] = name
			s['/Devices/{}/DiscoveryType'.format(serial)] = source
			s['/Devices/{}/LastSeen'.format(serial)] = last_seen

		# Skip channel setup for already-known devices; their dbus items and settings are already configured
		# TODO: Check this
		#if known:
		#	return result.status

		for i, ch_prop in channel_info.items():
			ch_type = ch_prop['type']
			ch_name = ch_prop['name']

			# Don't encode the channel type in the setting path to remain compatible with older versions.
			# There are two types of channels: 'switch' and 'em'. Switch channels are enumerated first, then em channels.
			# Note: the channels and settings are 1-indexed.
			await self.settings.add_settings(Setting(f'/Settings/Devices/shelly_{serial}/{i + 1}/Enabled', 0, alias=f"enabled_{serial}_{i}"))
			enabled = self.settings.get_value(self.settings.alias(f"enabled_{serial}_{i}"))

			if self.service.get_item(f'/Devices/{serial}/{i + 1}/Enabled') is None:
				self.service.add_item(IntegerItem(f'/Devices/{serial}/{i + 1}/Enabled',
									  writeable=True, onchange=partial(self._on_enabled_changed, serial, i)))
			if self.service.get_item(f'/Devices/{serial}/{i + 1}/Type') is None:
				self.service.add_item(TextItem(f'/Devices/{serial}/{i + 1}/Type', writeable=False))
			if self.service.get_item(f'/Devices/{serial}/{i + 1}/Name') is None:
				self.service.add_item(TextItem(f'/Devices/{serial}/{i + 1}/Name', value='', writeable=False))

			with self.service as s:
				s[f'/Devices/{serial}/{i + 1}/Type'] = ch_type
				s[f'/Devices/{serial}/{i + 1}/Enabled'] = enabled
				s[f'/Devices/{serial}/{i + 1}/Name'] = ch_name

			if enabled:
				enabled_item = self.service.get_item(f'/Devices/{serial}/{i + 1}/Enabled')
				await self._on_enabled_changed(serial, i, enabled_item, enabled)

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
			try:
				ret = await task
			except Exception as e:
				logger.error("Failed to enable channel %s for shelly device %s: %s", channel, serial, e)
				ret = False
		else:
			if serial in self._enable_tasks:
				# Wait for any ongoing enable task on this device to finish before disabling
				await asyncio.gather(*self._enable_tasks[serial])
				self._enable_tasks[serial].clear()
			ret = await self.disable_shelly_channel(serial, channel)

		if ret:
			item.set_local_value(value)
			await self.settings.set_value(self.settings.alias(f'enabled_{serial}_{channel}'), value)

class ShellyDiscovery(object):
	"""Thin composition root for discovery subsystem wiring.

	Initializes bus/settings/service, creates discovery sources,
	and coordinates lifecycle and refresh orchestration.
	"""

	def __init__(self, bus_type, print_cache_file=None):
		self.service = None
		self.settings = None
		self.bus_type = bus_type
		self._shelly_device_cache = None
		self._manager = None
		self._manual_ip_discovery = None
		self._mdns_discovery = None
		self._connection_manager = None
		self.bus = None
		self.monitor = None
		self._refresh_task = None
		self._cache_file = print_cache_file

	async def start(self):
		# Connect to dbus, localsettings
		self.bus = await MessageBus(bus_type=self.bus_type).connect()
		self.monitor = await Monitor.create(self.bus, itemsChanged=self.items_changed)

		self.settings = await wait_for_settings(self.bus)

		# Set up the service
		self.service = Service(self.bus, "com.victronenergy.shelly")

		# Add DeviceInstance path set to 0 to avoid systemcalc legacy scanning
		self.service.add_item(IntegerItem('/DeviceInstance', 0, writeable=False))

		await self.settings.add_settings(Setting('/Settings/Shelly/IpAddresses', "", alias="ipaddresses"))

		ip_addresses = self.settings.get_value(self.settings.alias('ipaddresses'))

		self.service.add_item(IntegerItem('/Refresh', 0, writeable=True,
			onchange=self.start_refresh_task))
		self._shelly_device_cache = ShellyDeviceCache(cache_file=self._cache_file)

		self._manager = ShellyManager(
			bus_type=self.bus_type,
			service=self.service,
			settings=self.settings,
			shelly_device_cache=self._shelly_device_cache
		)
		await self._manager.start_cache_sync()

		self.service.add_item(TextItem('/IpAddresses', ip_addresses, writeable=True, onchange=self._on_ip_addresses_changed))

		self._connection_manager = ShellyConnectionManager(self._shelly_device_cache,probe_device=self._manager._probe_device)
		await self._connection_manager.start()

		self._manual_ip_discovery = ManualIpDiscovery(
			add_endpoint=self.add_endpoint,
			remove_endpoint=self.remove_endpoint
		)

		self._mdns_discovery = MdnsDiscovery(
			add_endpoint=self.add_endpoint,
			remove_endpoint=self.remove_endpoint,
		)

		await self._mdns_discovery.start()
		await self._manual_ip_discovery.start(ip_addresses)

		await self.service.register()
		await self.bus.wait_for_disconnect()

	async def add_endpoint(self, host, serial, source, force_reconnect=False):
		await self._shelly_device_cache.add(ShellyEndpoint(serial=serial, host=host, source=source), force_reconnect=force_reconnect)

	async def remove_endpoint(self, serial):
		await self._shelly_device_cache.remove(serial)

	async def _on_ip_addresses_changed(self, item, value):
		if self._manual_ip_discovery is not None:
			if await self._manual_ip_discovery.on_ip_addresses_changed(value):
				item.set_local_value(value)
				if value != self.settings.get_value(self.settings.alias('ipaddresses')):
					await self.settings.set_value(self.settings.alias('ipaddresses'), value)

	async def start_refresh_task(self, item, value):
		if value != 1 or self._manager is None:
			return False

		# Instead of setting the value back to 0 immediately, set it to 1 here and reset it later.
		# This is needed to make sure refresh can be triggered again later.
		item.set_local_value(value)

		if self._refresh_task is None or self._refresh_task.done():
			self._refresh_task = asyncio.create_task(self._refresh())

		# Do this outside of the event loop
		task = asyncio.get_event_loop().create_task(self._clear_refresh())
		task.add_done_callback(background_tasks.discard)
		background_tasks.add(task)

	async def _refresh(self):
			# Actions on refresh:
			# 1. Refresh cached devices. This will reset their retry timers and clear attempts, so they will be retried immediately.
			if self._shelly_device_cache is not None:
				self._shelly_device_cache.refresh()

			# 2. Restart mDNS discovery.
			if self._mdns_discovery is not None:
				await self._mdns_discovery.restart()

	async def _clear_refresh(self):
		# Wait for the refresh task to complete before clearing the refresh flag
		if self._refresh_task is not None:
			await self._refresh_task
			self._refresh_task = None
		with self.service as s:
			s['/Refresh'] = 0

	def items_changed(self, service, values):
		pass

	async def stop(self):
		if self._manager is not None:
			await self._manager.stop_cache_sync()
		if self._mdns_discovery is not None:
			await self._mdns_discovery.stop()
		if self._connection_manager is not None:
			await self._connection_manager.stop()