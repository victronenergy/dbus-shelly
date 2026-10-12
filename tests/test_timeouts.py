"""
Devices that stop answering instead of closing their connection, as a Shelly
does when it loses WiFi or power.

The driver runs with all timeouts and retry intervals scaled by TIME_SCALE (see
tests/run_driver.py). Real values, and what they become here:

  RPC timeout (shelly_device._rpc_call)          4s  -> 0.4s
  ping timeout (shelly_device.ping_shelly)       2s  -> 0.2s
  connect timeout (aioshelly DEVICE_INIT_TIMEOUT) 30s -> 3s
  websocket heartbeat (aioshelly WS_HEARTBEAT)   55s -> 5.5s; a silent device is noticed after 1.5x that
  reconnect backoff (discovery)                  10s -> 1s initially
  PING_RETRIES (3, 1s apart) and CONNECTION_RETRIES (10) are not scaled.

So against a device that doesn't answer, the basic reconnect loop takes about
3 pings + 10 connect timeouts: ~36s here, ~5 minutes on real hardware, during
which the service stays registered with its last values.

Mock test hooks used: Mock.SetFaults with mode "timeout" (a method never
replies) or "delay" (replies late), and Mock.Freeze (open websockets go silent,
pings are not answered and new connections hang in the handshake).
"""
import time

import pytest

from shelly_stack import make_stack, service_name

TIME_SCALE = 0.1
RPC_TIMEOUT = 4 * TIME_SCALE
CONNECT_TIMEOUT = 30 * TIME_SCALE
HEARTBEAT = 55 * TIME_SCALE

SLOW = "aabbccdd4001"
RPC_TIMEOUT_DEV = "aabbccdd4002"
SHORT_FREEZE = "aabbccdd4003"
LONG_FREEZE = "aabbccdd4004"


def setup(s):
	for name, sn in (("slow", SLOW), ("rpc-timeout", RPC_TIMEOUT_DEV), ("short-freeze", SHORT_FREEZE), ("long-freeze", LONG_FREEZE)):
		s.add_mock(name, sn, "--switch-channels", "1")


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
	yield from make_stack(tmp_path_factory, "timeouts", setup, preseed_ips=True, time_scale=TIME_SCALE)


def enable(stack, sn):
	stack.wait_reachable(sn)
	stack.set_enabled(sn, 1, 1)
	name = service_name("acload", sn)
	stack.wait_service(name)
	return name


def pings_since(mock, t0):
	""" The driver's reconnect logic starts by pinging with Shelly.GetDeviceInfo. """
	return [t for t, m in mock.rpc("Mock.GetCallLog") if m == "Shelly.GetDeviceInfo" and t >= t0]


def output(mock):
	return mock.rpc("Switch.GetStatus", id=0)["output"]


def test_slow_responses_do_not_reconnect(stack):
	name = enable(stack, SLOW)
	mock = stack.mocks["slow"]
	owner_changes = len(stack.client.owner_changes(name))
	mock.rpc("Mock.SetFaults", methods=["Switch.Set", "Switch.GetStatus"], mode="delay", delay=RPC_TIMEOUT / 2)
	try:
		t0 = time.time()
		assert stack.client.set_value(name, "/SwitchableOutput/0/State", "i", 1) == 0
		stack.wait_for(lambda: output(mock) is True, "mock on")
		stack.wait_value(name, "/SwitchableOutput/0/State", 1)
		time.sleep(1)
		assert pings_since(mock, t0) == [], "a slow but timely reply must not start a reconnect"
		assert stack.client.owner_changes(name)[owner_changes:] == []
	finally:
		mock.rpc("Mock.SetFaults", methods=[])


def test_rpc_timeout_reconnects_and_retries(stack):
	# The device stays connected and answers pings, but one call never gets a reply.
	name = enable(stack, RPC_TIMEOUT_DEV)
	mock = stack.mocks["rpc-timeout"]
	owner_changes = len(stack.client.owner_changes(name))
	mock.rpc("Mock.SetFaults", methods=["Switch.Set"], mode="timeout")
	try:
		t0 = time.time()
		assert stack.client.set_value(name, "/SwitchableOutput/0/State", "i", 1) == 0
		# The timeout starts the reconnect logic, which pings the device
		stack.wait_for(lambda: pings_since(mock, t0), "reconnect ping after the RPC timeout", timeout=5)
		assert output(mock) is False
		assert stack.client.get_value(name, "/Connected") == 1
	finally:
		mock.rpc("Mock.SetFaults", methods=[])

	# Once the device answers again, the call is retried and goes through
	stack.wait_for(lambda: output(mock) is True, "retried Switch.Set to reach the device", timeout=5)
	stack.wait_value(name, "/SwitchableOutput/0/State", 1)
	assert stack.client.owner_changes(name)[owner_changes:] == [], "service must stay registered"
	assert stack.device(RPC_TIMEOUT_DEV, "Reachable") == 1


def test_freeze_within_reconnect_loop(stack):
	# The device drops off without closing its connection and comes back while the driver is still
	# in its basic reconnect loop. An RPC from D-Bus is what makes the driver notice.
	name = enable(stack, SHORT_FREEZE)
	mock = stack.mocks["short-freeze"]
	owner_changes = len(stack.client.owner_changes(name))
	mock.rpc("Mock.Freeze", frozen=True)
	try:
		assert stack.client.set_value(name, "/SwitchableOutput/0/State", "i", 1) == 0
		# Long enough for the RPC timeout, the pings and at least one hanging connect attempt
		time.sleep(RPC_TIMEOUT + 3 * 1.2 + CONNECT_TIMEOUT)
		assert stack.client.has_name(name)
	finally:
		mock.rpc("Mock.Freeze", frozen=False)

	stack.wait_for(lambda: output(mock) is True, "retried Switch.Set to reach the device", timeout=2 * CONNECT_TIMEOUT + 5)
	stack.wait_value(name, "/SwitchableOutput/0/State", 1)
	assert stack.client.get_value(name, "/Connected") == 1
	assert stack.client.owner_changes(name)[owner_changes:] == [], "service must stay registered"
	assert stack.device(SHORT_FREEZE, "Reachable") == 1


def test_freeze_longer_than_reconnect_loop(stack):
	# The device drops off without closing its connection and nothing is sent to it, so only the
	# websocket heartbeat can notice. It comes back after the driver gave up.
	name = enable(stack, LONG_FREEZE)
	mock = stack.mocks["long-freeze"]
	mock.rpc("Mock.Freeze", frozen=True)
	try:
		t0 = time.time()
		# Not noticed right away: the service keeps its last values for now
		stack.hold(lambda: stack.client.get_value(name, "/Connected") == 1, "service to stay up at first", HEARTBEAT / 2)
		# Heartbeat (~1.5x) + pings + CONNECTION_RETRIES connect timeouts
		stack.wait_service_gone(name, timeout=1.5 * HEARTBEAT + 5 + 10 * CONNECT_TIMEOUT + 15)
		print(f"service removed {time.time() - t0:.1f}s after the device froze")
		stack.wait_for(lambda: stack.device(LONG_FREEZE, "Reachable") == 0, "device unreachable")
		assert stack.enabled_setting(LONG_FREEZE, 1) == 1
	finally:
		mock.rpc("Mock.Freeze", frozen=False)

	# The connection manager's backoff brings it back
	stack.wait_service(name, connected=True)
	stack.wait_for(lambda: stack.device(LONG_FREEZE, "Reachable") == 1, "device reachable again")
	assert stack.client.set_value(name, "/SwitchableOutput/0/State", "i", 1) == 0
	stack.wait_for(lambda: output(mock) is True, "mock on")
