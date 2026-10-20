"""
Test harness: runs dbus-shelly against mock Shelly devices on a private D-Bus.

A Stack owns:
  - a private dbus-daemon (the desktop session bus is never touched)
  - tests/fake_localsettings.py as com.victronenergy.settings
  - mock Shelly devices (mock/mock_shelly_server.py) on free localhost ports
  - dbus_shelly.py --dbus session

mDNS is disabled through the AutoScan setting, so devices on the local network
are never picked up. With fake_mdns, AutoScan is on but the driver runs with a
fake mDNS browser (see tests/run_driver.py), and announce() simulates a device
announcing itself. The legacy websocket server in dbus_shelly.py always binds
port 8000, so that port must be free.
"""
import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request

import pytest
from dbus_fast import Message, MessageType, Variant
from dbus_fast.aio import MessageBus

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHELLY_SERVICE = "com.victronenergy.shelly"
SETTINGS_SERVICE = "com.victronenergy.settings"

TIMEOUT = 30

PRODUCT_ID_EM = 0xB034
PRODUCT_ID_SWITCH = 0xB075
PRODUCT_ID_SMOKE_FLOOD = 0xB0A0


_used_ports = set()

def free_port():
	# The kernel can hand out the same ephemeral port again once the socket is
	# closed, so remember which ones were given out already.
	while True:
		with socket.socket() as s:
			s.bind(("127.0.0.1", 0))
			port = s.getsockname()[1]
		if port not in _used_ports:
			_used_ports.add(port)
			return port


def wait_for_port(port, timeout=10):
	deadline = time.time() + timeout
	while time.time() < deadline:
		try:
			with socket.create_connection(("127.0.0.1", port), timeout=0.5):
				return
		except OSError:
			time.sleep(0.1)
	raise RuntimeError(f"Mock on port {port} did not start")


def service_name(kind, serial, channel_id=0):
	return f"com.victronenergy.{kind}.shelly_{serial}_{channel_id}"


class DbusClient:
	""" Small synchronous wrapper around dbus_fast for use in tests. Also
	    records every NameOwnerChanged signal, so tests can check that a
	    service was never dropped. """
	def __init__(self, address):
		self.name_events = []
		self.loop = asyncio.new_event_loop()
		self.bus = self.loop.run_until_complete(self._connect(address))

	async def _connect(self, address):
		bus = await MessageBus(bus_address=address).connect()
		bus.add_message_handler(self._on_message)
		await bus.call(Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
			interface="org.freedesktop.DBus", member="AddMatch", signature="s",
			body=["type='signal',sender='org.freedesktop.DBus',member='NameOwnerChanged'"]))
		return bus

	def _on_message(self, msg):
		if msg.message_type == MessageType.SIGNAL and msg.member == "NameOwnerChanged":
			self.name_events.append(tuple(msg.body))

	def close(self):
		self.bus.disconnect()
		self.loop.close()

	def pump(self):
		""" Process pending incoming messages (signals). """
		self.loop.run_until_complete(asyncio.sleep(0.01))

	def _call(self, **kwargs):
		reply = self.loop.run_until_complete(self.bus.call(Message(**kwargs)))
		if reply.message_type == MessageType.ERROR:
			raise RuntimeError(f"{kwargs.get('destination')}{kwargs.get('path')} {kwargs.get('member')}: {reply.body}")
		return reply.body

	def _busitem(self, service, path, member, signature="", body=()):
		return self._call(destination=service, path=path, interface="com.victronenergy.BusItem",
			member=member, signature=signature, body=list(body))

	def get_value(self, service, path):
		v = self._busitem(service, path, "GetValue")[0].value
		return None if v == [] else v

	def set_value(self, service, path, signature, value):
		return self._busitem(service, path, "SetValue", "v", [Variant(signature, value)])[0]

	def get_items(self, service):
		items = self._busitem(service, "/", "GetItems")[0]
		return {p: (None if v["Value"].value == [] else v["Value"].value) for p, v in items.items()}

	def has_name(self, name):
		return self._call(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
			interface="org.freedesktop.DBus", member="NameHasOwner", signature="s", body=[name])[0]

	def owner_changes(self, name):
		self.pump()
		return [e for e in self.name_events if e[0] == name]


class MockDevice:
	""" One mock_shelly_server.py process. Can be stopped and restarted on the
	    same port, optionally with different arguments (capabilities). """
	def __init__(self, stack, name, serial, args):
		self.stack = stack
		self.name = name
		self.serial = serial
		self.args = list(args)
		self.port = free_port()
		self.proc = None
		self.log = None

	@property
	def endpoint(self):
		return f"127.0.0.1:{self.port}"

	def start(self, args=None):
		if args is not None:
			self.args = list(args)
		self.log = open(os.path.join(self.stack.logdir, f"mock-{self.name}.log"), "a")
		self.proc = subprocess.Popen([sys.executable, "mock/mock_shelly_server.py", "--host", "127.0.0.1",
			"--port", str(self.port), "--mac", self.serial, "--device-name", f"Mock {self.name}"] + self.args,
			cwd=REPO, env=self.stack.env, stdout=self.log, stderr=subprocess.STDOUT)
		wait_for_port(self.port)

	def stop(self):
		""" Kill the device: its websocket connection drops and the port is closed. """
		if self.proc is not None and self.proc.poll() is None:
			self.proc.kill()
			self.proc.wait()
		self.proc = None
		if self.log is not None:
			self.log.close()
			self.log = None

	def rpc(self, method, **params):
		req = urllib.request.Request(f"http://{self.endpoint}/rpc",
			data=json.dumps({"id": 1, "method": method, "params": params}).encode(),
			headers={"Content-Type": "application/json"})
		with urllib.request.urlopen(req, timeout=5) as resp:
			return json.loads(resp.read())["result"]


class Stack:
	def __init__(self, logdir, time_scale=None, fake_mdns=False):
		""" With time_scale, the driver runs through tests/run_driver.py with all
		    its timeouts and retry intervals scaled by that factor. """
		self.logdir = logdir
		self.time_scale = time_scale
		self.fake_mdns = fake_mdns
		self.mdns_file = os.path.join(logdir, "mdns.json")
		self.procs = {}
		self.mocks = {}
		self.client = None
		self.env = None

	def add_mock(self, name, serial, *args):
		self.mocks[name] = MockDevice(self, name, serial, args)
		return self.mocks[name]

	def _start(self, name, cmd):
		log = open(os.path.join(self.logdir, f"{name}.log"), "w")
		self.procs[name] = (subprocess.Popen(cmd, cwd=REPO, env=self.env,
			stdout=log, stderr=subprocess.STDOUT), log)

	def start(self, preseed_ips=False):
		""" Start everything. With preseed_ips, /Settings/Shelly/IpAddresses
		    already lists all mocks when the driver starts. """
		daemon = subprocess.Popen(["dbus-daemon", "--session", "--nofork", "--print-address=1"],
			stdout=subprocess.PIPE, text=True)
		self.procs["dbus-daemon"] = (daemon, None)
		address = daemon.stdout.readline().strip()
		self.env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=address, PYTHONUNBUFFERED="1")
		self.client = DbusClient(address)

		for mock in self.mocks.values():
			mock.start()

		initial = {"/Settings/Shelly/AutoScan": 1 if self.fake_mdns else 0}
		if preseed_ips:
			initial["/Settings/Shelly/IpAddresses"] = ",".join(m.endpoint for m in self.mocks.values())
		self._start("localsettings", [sys.executable, "tests/fake_localsettings.py",
			"--initial", json.dumps(initial)])
		self.wait_for(lambda: self.client.has_name(SETTINGS_SERVICE), "fake localsettings on D-Bus", timeout=10)

		if self.time_scale is None and not self.fake_mdns:
			driver = ["dbus_shelly.py"]
		else:
			driver = ["tests/run_driver.py"]
			if self.time_scale is not None:
				self.env["SHELLY_TEST_TIME_SCALE"] = str(self.time_scale)
			if self.fake_mdns:
				self.env["SHELLY_TEST_FAKE_MDNS"] = self.mdns_file
		self._start("driver", [sys.executable, *driver, "--dbus", "session", "--debug"])
		self.wait_for(lambda: self.client.has_name(SHELLY_SERVICE), f"{SHELLY_SERVICE} on D-Bus")

	def stop(self):
		if self.client is not None:
			self.client.close()
		# Driver first, so it can still talk to settings and the bus while shutting down.
		for name in ["driver", "localsettings"]:
			self._stop_proc(name)
		for mock in self.mocks.values():
			mock.stop()
		self._stop_proc("dbus-daemon")

	def _stop_proc(self, name):
		if name not in self.procs:
			return
		proc, log = self.procs[name]
		if proc.poll() is None:
			proc.send_signal(signal.SIGTERM)
			try:
				proc.wait(timeout=5)
			except subprocess.TimeoutExpired:
				proc.kill()
				proc.wait()
		if log is not None:
			log.close()

	def announce(self, *mocks):
		""" Simulate mDNS announcements of mocks (needs fake_mdns). """
		assert self.fake_mdns
		with open(self.mdns_file, "w") as f:
			json.dump([{"serial": m.serial, "host": m.endpoint} for m in mocks], f)
		self.procs["driver"][0].send_signal(signal.SIGUSR1)

	def driver_log_tail(self, lines=40):
		try:
			with open(os.path.join(self.logdir, "driver.log")) as f:
				return "".join(f.readlines()[-lines:])
		except OSError:
			return "<no driver log>"

	def fail(self, message):
		pytest.fail(f"{message}\n--- driver log ({self.logdir}) ---\n{self.driver_log_tail()}")

	def check_driver_alive(self, what):
		driver = self.procs.get("driver")
		if driver is not None and driver[0].poll() is not None:
			self.fail(f"Driver exited with code {driver[0].returncode} while waiting for {what}")

	def wait_for(self, predicate, what, timeout=TIMEOUT):
		""" Poll predicate until it returns something truthy. Exceptions
		    (e.g. a service or path that does not exist yet) count as not yet. """
		deadline = time.time() + timeout
		last = None
		while time.time() < deadline:
			self.check_driver_alive(what)
			try:
				result = predicate()
				if result:
					return result
				last = result
			except Exception as e:
				last = e
			time.sleep(0.25)
		self.fail(f"Timed out after {timeout}s waiting for {what} (last: {last!r})")

	def hold(self, predicate, what, duration):
		""" Assert predicate stays truthy for duration seconds. """
		deadline = time.time() + duration
		while time.time() < deadline:
			self.check_driver_alive(what)
			try:
				ok = predicate()
			except Exception as e:
				ok = False
				last = e
			else:
				last = ok
			if not ok:
				self.fail(f"Expected {what} to hold for {duration}s, but it did not (last: {last!r})")
			time.sleep(0.25)

	# Convenience accessors

	def device(self, serial, path):
		return self.client.get_value(SHELLY_SERVICE, f"/Devices/{serial}/{path}")

	def setting(self, path):
		return self.client.get_value(SETTINGS_SERVICE, path)

	def enabled_setting(self, serial, index):
		""" index is 1-based, as in /Devices/<serial>/<index>/Enabled. """
		return self.setting(f"/Settings/Devices/shelly_{serial}/{index}/Enabled")

	def wait_reachable(self, serial):
		self.wait_for(lambda: self.device(serial, "Reachable") == 1, f"{serial} reachable")

	def set_enabled(self, serial, index, value):
		assert self.client.set_value(SHELLY_SERVICE, f"/Devices/{serial}/{index}/Enabled", "i", value) == 0

	def wait_service(self, name, connected=True):
		self.wait_for(lambda: self.client.has_name(name), name)
		if connected:
			self.wait_for(lambda: self.client.get_value(name, "/Connected") == 1, f"{name} connected")
		return self.client.get_items(name)

	def wait_service_gone(self, name, timeout=TIMEOUT):
		self.wait_for(lambda: not self.client.has_name(name), f"{name} to disappear", timeout=timeout)

	def wait_value(self, service, path, expected, timeout=TIMEOUT):
		self.wait_for(lambda: self.client.get_value(service, path) == expected,
			f"{service}{path} == {expected!r}", timeout=timeout)


def make_stack(tmp_path_factory, name, setup, preseed_ips=False, time_scale=None, fake_mdns=False):
	""" Generator for module-scoped fixtures: setup(stack) adds mocks. """
	if subprocess.run(["which", "dbus-daemon"], capture_output=True).returncode != 0:
		pytest.skip("dbus-daemon not installed")
	s = Stack(str(tmp_path_factory.mktemp(name)), time_scale=time_scale, fake_mdns=fake_mdns)
	setup(s)
	try:
		s.start(preseed_ips=preseed_ips)
		yield s
	finally:
		s.stop()
