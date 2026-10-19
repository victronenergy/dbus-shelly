"""
Lifecycle scenarios: role changes, devices dropping off and coming back,
capability changes and removing a device from /IpAddresses.

Every scenario uses its own mock device, so they don't influence each other.
All mocks are listed in /Settings/Shelly/IpAddresses before the driver starts.

Timing context (shelly_device.py / discovery.py): when a device drops, the
driver first pings it 3 times (1s apart), then tries to reconnect
CONNECTION_RETRIES times. Against a closed port those attempts fail at once,
so this "basic reconnect loop" lasts a few seconds. After that the channels
are stopped and the connection manager retries with a backoff starting at 10s.
"""
import time

import pytest

from shelly_stack import make_stack, service_name, SHELLY_SERVICE

ROLE_SWITCH = "aabbccdd2001"
ROLE_EM1 = "aabbccdd2002"
RECONNECT = "aabbccdd2003"
OFFLINE = "aabbccdd2004"
CAP_CHANGE = "aabbccdd2005"
CH_COUNT = "aabbccdd2006"
CAP_OFFLINE = "aabbccdd2007"
REMOVE = "aabbccdd2008"
OFFLINE_DISABLE = "aabbccdd2009"
OFFLINE_ENABLE = "aabbccdd200a"

TWO_SWITCHES = ["--switch-channels", "2"]
ONE_SWITCH = ["--switch-channels", "1"]
EM1_ONLY = ["--switch-channels", "0", "--em1-channels", "1"]

# Time a mock is down for a reconnect that should happen within the basic reconnect loop.
SHORT_OUTAGE = 1


def setup(s):
	s.add_mock("role-switch", ROLE_SWITCH, *ONE_SWITCH)
	s.add_mock("role-em1", ROLE_EM1, *EM1_ONLY)
	s.add_mock("reconnect", RECONNECT, *ONE_SWITCH)
	s.add_mock("offline", OFFLINE, *ONE_SWITCH)
	s.add_mock("cap-change", CAP_CHANGE, *TWO_SWITCHES)
	s.add_mock("ch-count", CH_COUNT, *TWO_SWITCHES)
	s.add_mock("cap-offline", CAP_OFFLINE, *TWO_SWITCHES)
	s.add_mock("remove", REMOVE, *ONE_SWITCH)
	s.add_mock("offline-disable", OFFLINE_DISABLE, *ONE_SWITCH)
	s.add_mock("offline-enable", OFFLINE_ENABLE, *TWO_SWITCHES)


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
	yield from make_stack(tmp_path_factory, "lifecycle", setup, preseed_ips=True)


def enable_all(stack, serial, count):
	stack.wait_reachable(serial)
	for i in range(1, count + 1):
		stack.set_enabled(serial, i, 1)
	for ch in range(count):
		stack.wait_service(service_name("acload", serial, ch))


def instance_setting(stack, serial, ch=0):
	return stack.setting(f"/Settings/Devices/shelly_{serial}_{ch}/ClassAndVrmInstance")


def wait_reconnect_loop_done(stack, serial, services):
	""" Wait until the driver gave up on the basic reconnect loop for a device that is down. """
	for name in services:
		stack.wait_service_gone(name)
	stack.wait_for(lambda: stack.device(serial, "Reachable") == 0, f"{serial} unreachable")


# Role changes

def test_role_change_switch_to_pvinverter(stack):
	enable_all(stack, ROLE_SWITCH, 1)
	acload = service_name("acload", ROLE_SWITCH)
	pvinverter = service_name("pvinverter", ROLE_SWITCH)
	instance = stack.client.get_value(acload, "/DeviceInstance")

	assert stack.client.set_value(acload, "/Role", "s", "pvinverter") == 0
	stack.wait_service_gone(acload)
	items = stack.wait_service(pvinverter)
	assert items["/Role"] == "pvinverter"
	assert items["/StatusCode"] == 99
	assert items["/DeviceInstance"] == instance
	assert instance_setting(stack, ROLE_SWITCH) == f"pvinverter:{instance}"


def test_role_not_allowed_is_rejected(stack):
	# genset is not an allowed role for a switch
	pvinverter = service_name("pvinverter", ROLE_SWITCH)
	assert stack.client.set_value(pvinverter, "/Role", "s", "genset") != 0
	stack.hold(lambda: stack.client.get_value(pvinverter, "/Role") == "pvinverter", "role to stay pvinverter", 1)
	assert not stack.client.has_name(service_name("genset", ROLE_SWITCH))
	assert instance_setting(stack, ROLE_SWITCH).startswith("pvinverter:")


def test_role_change_back_to_acload(stack):
	pvinverter = service_name("pvinverter", ROLE_SWITCH)
	acload = service_name("acload", ROLE_SWITCH)
	assert stack.client.set_value(pvinverter, "/Role", "s", "acload") == 0
	stack.wait_service_gone(pvinverter)
	items = stack.wait_service(acload)
	assert items["/Role"] == "acload"
	assert "/StatusCode" not in items
	# Switching still works after the restart
	assert stack.client.set_value(acload, "/SwitchableOutput/0/State", "i", 1) == 0
	stack.wait_for(lambda: stack.mocks["role-switch"].rpc("Switch.GetStatus", id=0)["output"] is True, "mock on")


def test_role_change_em1_to_genset(stack):
	enable_all(stack, ROLE_EM1, 1)
	acload = service_name("acload", ROLE_EM1)
	genset = service_name("genset", ROLE_EM1)
	assert stack.client.set_value(acload, "/Role", "s", "genset") == 0
	stack.wait_service_gone(acload)
	items = stack.wait_service(genset)
	assert items["/Role"] == "genset"
	assert items["/Ac/Power"] == 1000.0
	assert instance_setting(stack, ROLE_EM1).startswith("genset:")


# Device disappearing and reappearing

def test_reconnect_within_reconnect_loop(stack):
	enable_all(stack, RECONNECT, 1)
	name = service_name("acload", RECONNECT)
	mock = stack.mocks["reconnect"]
	assert stack.client.set_value(name, "/SwitchableOutput/0/State", "i", 1) == 0
	stack.wait_value(name, "/SwitchableOutput/0/State", 1)
	owner_changes = len(stack.client.owner_changes(name))

	mock.stop()
	time.sleep(SHORT_OUTAGE)
	mock.start()	# Restarted mock has its output off

	# The driver refreshes the handlers after the reconnect and picks up the new state.
	stack.wait_value(name, "/SwitchableOutput/0/State", 0)
	assert stack.client.owner_changes(name)[owner_changes:] == [], "service must stay registered during the reconnect"
	assert stack.client.get_value(name, "/Connected") == 1
	assert stack.device(RECONNECT, "Reachable") == 1

	# Control works on the new connection
	assert stack.client.set_value(name, "/SwitchableOutput/0/State", "i", 1) == 0
	stack.wait_for(lambda: mock.rpc("Switch.GetStatus", id=0)["output"] is True, "mock on")


def test_reconnect_after_reconnect_loop(stack):
	enable_all(stack, OFFLINE, 1)
	name = service_name("acload", OFFLINE)
	mock = stack.mocks["offline"]

	mock.stop()
	wait_reconnect_loop_done(stack, OFFLINE, [name])
	# The channel stays enabled while the device is away
	assert stack.enabled_setting(OFFLINE, 1) == 1
	assert stack.device(OFFLINE, "1/Enabled") == 1

	mock.start()
	# The connection manager retries with a backoff, starting at 10s.
	stack.wait_service(name, connected=True)
	stack.wait_for(lambda: stack.device(OFFLINE, "Reachable") == 1, f"{OFFLINE} reachable")
	assert stack.client.set_value(name, "/SwitchableOutput/0/State", "i", 1) == 0
	stack.wait_for(lambda: mock.rpc("Switch.GetStatus", id=0)["output"] is True, "mock on")


# Enabling and disabling channels while a device is offline

def test_disable_channel_while_offline(stack):
	enable_all(stack, OFFLINE_DISABLE, 1)
	name = service_name("acload", OFFLINE_DISABLE)
	mock = stack.mocks["offline-disable"]

	mock.stop()
	wait_reconnect_loop_done(stack, OFFLINE_DISABLE, [name])

	# Disabling is accepted and stored while the device is away
	stack.set_enabled(OFFLINE_DISABLE, 1, 0)
	stack.wait_for(lambda: stack.enabled_setting(OFFLINE_DISABLE, 1) == 0, "Enabled setting reset to 0")
	stack.hold(lambda: stack.device(OFFLINE_DISABLE, "1/Enabled") == 0, "/Enabled to stay 0", 2)

	# The device comes back, but the channel stays disabled
	mock.start()
	stack.wait_reachable(OFFLINE_DISABLE)
	assert stack.device(OFFLINE_DISABLE, "1/Enabled") == 0
	stack.hold(lambda: not stack.client.has_name(name), "channel to stay disabled", 2)


def test_enable_channel_while_offline_is_rejected(stack):
	enable_all(stack, OFFLINE_ENABLE, 1)
	first = service_name("acload", OFFLINE_ENABLE, 0)
	second = service_name("acload", OFFLINE_ENABLE, 1)
	mock = stack.mocks["offline-enable"]

	mock.stop()
	wait_reconnect_loop_done(stack, OFFLINE_ENABLE, [first])

	stack.set_enabled(OFFLINE_ENABLE, 2, 1)
	stack.hold(lambda: stack.device(OFFLINE_ENABLE, "2/Enabled") == 0, "/Enabled to stay 0", 2)
	assert stack.enabled_setting(OFFLINE_ENABLE, 2) == 0

	# Only the channel that was enabled before comes back
	mock.start()
	stack.wait_service(first, connected=True)
	assert stack.device(OFFLINE_ENABLE, "2/Enabled") == 0
	stack.hold(lambda: not stack.client.has_name(second), "second channel to stay disabled", 2)


# Capability changes

def assert_channels_disabled(stack, serial, count, timeout=10):
	for i in range(1, count + 1):
		stack.wait_for(lambda: stack.enabled_setting(serial, i) == 0, f"Enabled setting {i} of {serial} reset to 0",
			timeout=timeout)


def test_capability_change_within_reconnect_loop(stack):
	# Two switches become a single EM1 meter while the driver is reconnecting.
	enable_all(stack, CAP_CHANGE, 2)
	services = [service_name("acload", CAP_CHANGE, ch) for ch in (0, 1)]
	mock = stack.mocks["cap-change"]

	mock.stop()
	time.sleep(SHORT_OUTAGE)
	mock.start(EM1_ONLY)

	for name in services:
		stack.wait_service_gone(name)
	assert_channels_disabled(stack, CAP_CHANGE, 2)
	stack.wait_for(lambda: stack.device(CAP_CHANGE, "1/Type") == "em1", "channel 1 to become em1")
	assert stack.device(CAP_CHANGE, "1/Enabled") == 0
	assert stack.device(CAP_CHANGE, "2/Type") is None
	# The new channel must not be started on its own
	stack.hold(lambda: not stack.client.has_name(services[0]), "new em1 channel to stay disabled", 2)


@pytest.mark.xfail(strict=True, reason="shelly_device.py _reinit_channel_and_handlers only compares capability "
	"types, so a lost switch:1 keeps its service; its refresh then raises TypeError on a None status")
def test_channel_count_change_within_reconnect_loop(stack):
	# Same capabilities (Switch), but one channel less.
	enable_all(stack, CH_COUNT, 2)
	second = service_name("acload", CH_COUNT, 1)
	mock = stack.mocks["ch-count"]

	mock.stop()
	time.sleep(SHORT_OUTAGE)
	mock.start(ONE_SWITCH)

	# The reconnect itself takes a few seconds
	stack.wait_service_gone(second, timeout=10)
	stack.wait_for(lambda: stack.enabled_setting(CH_COUNT, 2) == 0, "Enabled setting of the removed channel reset to 0")
	stack.wait_for(lambda: stack.device(CH_COUNT, "2/Type") is None, "removed channel gone from /Devices")


@pytest.mark.xfail(strict=True, reason="discovery.py _add_device reuses the old Enabled=1 setting of channel 1 "
	"for the new em1 channel and starts it, instead of disabling the channels of the changed device")
def test_capability_change_while_offline(stack):
	# Two switches become a single EM1 meter after the driver gave up reconnecting.
	enable_all(stack, CAP_OFFLINE, 2)
	services = [service_name("acload", CAP_OFFLINE, ch) for ch in (0, 1)]
	mock = stack.mocks["cap-offline"]

	mock.stop()
	wait_reconnect_loop_done(stack, CAP_OFFLINE, services)
	mock.start(EM1_ONLY)

	stack.wait_for(lambda: stack.device(CAP_OFFLINE, "1/Type") == "em1", "channel 1 to become em1", timeout=60)
	assert_channels_disabled(stack, CAP_OFFLINE, 2)
	assert stack.device(CAP_OFFLINE, "1/Enabled") == 0
	stack.hold(lambda: not stack.client.has_name(services[0]), "new em1 channel to stay disabled", 2)


# Removing a device from /IpAddresses

def test_remove_from_ip_addresses(stack):
	enable_all(stack, REMOVE, 1)
	name = service_name("acload", REMOVE)
	others = [m.endpoint for m in stack.mocks.values() if m.serial != REMOVE]

	assert stack.client.set_value(SHELLY_SERVICE, "/IpAddresses", "s", ",".join(others)) == 0
	stack.wait_service_gone(name)
	stack.wait_for(lambda: stack.device(REMOVE, "Reachable") is None, "/Devices paths cleared")
	assert stack.device(REMOVE, "Ip") is None
	assert stack.device(REMOVE, "1/Enabled") is None
	assert stack.setting("/Settings/Shelly/IpAddresses") == ",".join(others)
	# Other devices are not affected
	assert stack.client.has_name(service_name("acload", ROLE_SWITCH))
	assert stack.device(ROLE_SWITCH, "Reachable") == 1
