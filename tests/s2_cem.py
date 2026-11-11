"""
Test CEM: drives an S2 resource manager on D-Bus with S2Session from
s2-cem-cli.py (venus-s2-tools, the tests/ext/venus-s2-tools submodule), the
same CEM used for manual testing.

S2Session is asyncio-based and needs its loop running all the time (incoming
signals, ReceptionStatus acks and KeepAlives), while the tests are
synchronous. So the CEM gets its own bus connection and event loop in a
background thread, and the methods below wait for the result.
"""
import asyncio
import importlib.util
import os
import sys
import threading

import pytest
from dbus_fast import Message, MessageType
from dbus_fast.aio import MessageBus

CLI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ext", "venus-s2-tools", "s2-cem-cli.py")


def load_cli():
	""" Import s2-cem-cli.py (not importable by name, it has dashes). """
	if not os.path.exists(CLI):
		pytest.skip("venus-s2-tools submodule not checked out (git submodule update --init)")
	if "s2_cem_cli" not in sys.modules:
		spec = importlib.util.spec_from_file_location("s2_cem_cli", CLI)
		module = importlib.util.module_from_spec(spec)
		# dataclasses need the module in sys.modules while it is executed
		sys.modules["s2_cem_cli"] = module
		# No __pycache__ inside the submodule, it would show up as untracked content
		dont_write_bytecode = sys.dont_write_bytecode
		sys.dont_write_bytecode = True
		try:
			spec.loader.exec_module(module)
		finally:
			sys.dont_write_bytecode = dont_write_bytecode
	return sys.modules["s2_cem_cli"]


class Cem:
	def __init__(self, bus_address, service_name, client_id="test_cem", keepalive=15):
		self.cli = load_cli()
		self.loop = asyncio.new_event_loop()
		self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
		self.thread.start()
		self.bus = None
		self.session = None
		try:
			self._run(self._open(bus_address, service_name, client_id, keepalive))
		except BaseException:
			self.close()
			raise

	def _run(self, coro, timeout=30):
		return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

	async def _open(self, bus_address, service_name, client_id, keepalive):
		self.bus = await MessageBus(bus_address=bus_address).connect()
		owner = await self.cli.get_name_owner(self.bus, Message, MessageType, service_name)
		candidate = self.cli.ServiceCandidate(service_name=service_name, owner=owner, label=service_name)
		self.session = self.cli.S2Session(self.bus, Message, MessageType, candidate, client_id, keepalive)
		await self.session.__aenter__()
		await self.session.connect()
		await self.session.bootstrap()

	def close(self):
		if self.session is not None:
			try:
				# Also sends Disconnect when still connected
				self._run(self.session.close(), timeout=10)
			except Exception:
				pass
			self.session = None
		if self.bus is not None:
			self.loop.call_soon_threadsafe(self.bus.disconnect)
			self.bus = None
		self.loop.call_soon_threadsafe(self.loop.stop)
		self.thread.join(timeout=5)

	# State received from the RM (updated by the receiver task)

	@property
	def connected(self):
		return self.session.connected

	@property
	def rm_details(self):
		return self.session.rm_details

	@property
	def ombc_status(self):
		return self.session.ombc_status

	@property
	def ombc_system_description(self):
		return self.session.ombc_system_description

	@property
	def power(self):
		""" Value of the latest PowerMeasurement, None if none was received. """
		pm = self.session.power_measurement
		return None if pm is None else pm.values[0].value

	@property
	def power_commodity(self):
		pm = self.session.power_measurement
		return None if pm is None else pm.values[0].commodity_quantity

	# Actions

	def disconnect(self):
		self._run(self.session.disconnect())

	def select_control_type(self, control_type):
		return self._run(self.session.select_control_type(control_type))

	def select_ombc(self):
		""" Select OMBC and wait for its SystemDescription and Status. """
		self.select_control_type(self.cli.ControlType.OPERATION_MODE_BASED_CONTROL)
		self._run(self.session.wait_for_state())

	def ombc_mode(self, label):
		""" The OMBC operation mode with this diagnostic label ("On" or "Off"). """
		modes = [m for m in self.ombc_system_description.operation_modes if m.diagnostic_label == label]
		assert len(modes) == 1, f"expected one {label!r} operation mode, got {self.ombc_system_description.operation_modes}"
		return modes[0]

	def active_mode_label(self):
		status = self.ombc_status
		if status is None or self.ombc_system_description is None:
			return None
		active = self.cli.normalize_id(status.active_operation_mode_id)
		for mode in self.ombc_system_description.operation_modes:
			if self.cli.normalize_id(mode.id) == active:
				return mode.diagnostic_label
		return None

	def send_instruction(self, label):
		""" Send an OMBC.Instruction for the mode with this label, return the ReceptionStatus. """
		return self._run(self.session.send_ombc_instruction(self.ombc_mode(label), abnormal_condition=False))
