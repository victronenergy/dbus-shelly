"""
Sleepy (battery-powered) devices: Smoke and Flood. They spend most of their
time in deep sleep with WiFi off, and only connect briefly when they wake up,
e.g. to report an alarm. A sleeping device is emulated by killing its mock,
a wake-up by restarting it.

Expected behaviour (shelly_device.py / discovery.py): when a sleepy device
drops, the driver runs the same basic reconnect loop as for other devices
(3 pings, then CONNECTION_RETRIES connection attempts). When that fails, its
service stays registered with its last known values and /Connected=0
(ShellyDevice.mark_disconnected), instead of being torn down. When the device
wakes up after that, it announces itself over mDNS again, and that makes the
driver reconnect straight away (ShellyDeviceCache.add).

Sleepy devices rely on mDNS, so the devices are discovered through simulated
mDNS announcements (Stack.announce), not through /IpAddresses.

Every scenario uses its own mock device, so they don't influence each other.
"""
import time

import pytest

from shelly_stack import make_stack, service_name, PRODUCT_ID_INPUT

SLEEP = "aabbccdd3001"
WAKE_SHORT = "aabbccdd3002"
WAKE_LONG_SMOKE = "aabbccdd3003"
WAKE_LONG_FLOOD = "aabbccdd3004"

SMOKE = ["--switch-channels", "0", "--smoke"]
FLOOD = ["--switch-channels", "0", "--flood"]

# Time a mock is down for a reconnect that should happen within the basic reconnect loop.
SHORT_OUTAGE = 1
# Upper bound for the basic reconnect loop against a closed port: 3 pings 1s apart,
# then connection attempts that fail at once.
RECONNECT_LOOP = 15
# How long a device that announces itself after waking up may take to be reconnected.
WAKE_TIMEOUT = 15


def setup(s):
	s.add_mock("sleep", SLEEP, *SMOKE)
	s.add_mock("wake-short", WAKE_SHORT, *SMOKE)
	s.add_mock("wake-long-smoke", WAKE_LONG_SMOKE, *SMOKE)
	s.add_mock("wake-long-flood", WAKE_LONG_FLOOD, *FLOOD)


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
	yield from make_stack(tmp_path_factory, "sleepy", setup, fake_mdns=True)


def wait_sensor(stack, mock_name):
	""" Announce the sensor. Sleepy sensors are enabled by default; wait for the service to be up. """
	mock = stack.mocks[mock_name]
	stack.announce(mock)
	serial = mock.serial
	stack.wait_reachable(serial)
	name = service_name("digitalinput", serial)
	stack.wait_service(name)
	return name


def wake(stack, mock, alarm=False):
	""" Restart the mock, optionally with its alarm raised, and let it announce itself. """
	mock.start()
	if alarm:
		mock.rpc("Mock.SetAlarm", alarm=True)
	stack.announce(mock)


def wait_asleep(stack, name):
	""" Wait until the driver gave up on the basic reconnect loop for a sleeping device. """
	stack.wait_value(name, "/Connected", 0, timeout=RECONNECT_LOOP)


def test_sleep_keeps_service_with_last_values(stack):
	name = wait_sensor(stack, "sleep")
	mock = stack.mocks["sleep"]
	mock.rpc("Mock.SetAlarm", alarm=True)
	stack.wait_value(name, "/Alarm", 2)
	owner_changes = len(stack.client.owner_changes(name))

	mock.stop()
	wait_asleep(stack, name)

	# The service stays, with its last known values, and is not dropped later on either.
	stack.hold(lambda: stack.client.has_name(name) and stack.client.get_value(name, "/Connected") == 0,
		f"{name} to stay registered while asleep", 5)
	items = stack.client.get_items(name)
	assert items["/ProductId"] == PRODUCT_ID_INPUT
	assert items["/Alarm"] == 2
	assert items["/State"] == 9
	assert stack.client.owner_changes(name)[owner_changes:] == [], "service must stay registered while asleep"
	# The channel stays enabled
	assert stack.enabled_setting(SLEEP, 1) == 1
	assert stack.device(SLEEP, "1/Enabled") == 1


def test_wake_within_reconnect_loop(stack):
	name = wait_sensor(stack, "wake-short")
	mock = stack.mocks["wake-short"]
	owner_changes = len(stack.client.owner_changes(name))

	mock.stop()
	time.sleep(SHORT_OUTAGE)
	wake(stack, mock, alarm=True)	# Woken up by an alarm

	stack.wait_value(name, "/Alarm", 2)
	assert stack.client.get_value(name, "/State") == 9
	assert stack.client.get_value(name, "/Connected") == 1
	assert stack.client.owner_changes(name)[owner_changes:] == [], "service must stay registered during the reconnect"


@pytest.mark.parametrize("mock_name, serial", [
	("wake-long-smoke", WAKE_LONG_SMOKE),
	("wake-long-flood", WAKE_LONG_FLOOD),
])
def test_wake_after_reconnect_loop(stack, mock_name, serial):
	name = wait_sensor(stack, mock_name)
	mock = stack.mocks[mock_name]
	owner_changes = len(stack.client.owner_changes(name))

	mock.stop()
	wait_asleep(stack, name)

	wake(stack, mock, alarm=True)	# Woken up by an alarm

	stack.wait_value(name, "/Connected", 1, timeout=WAKE_TIMEOUT)
	stack.wait_value(name, "/Alarm", 2)
	assert stack.client.get_value(name, "/State") == 9
	assert stack.client.owner_changes(name)[owner_changes:] == [], "service must stay registered while asleep"

	# Back to sleep after reporting, and the alarm clears on the next wake-up.
	mock.stop()
	wait_asleep(stack, name)
	assert stack.client.get_value(name, "/Alarm") == 2

	wake(stack, mock)
	stack.wait_value(name, "/Connected", 1, timeout=WAKE_TIMEOUT)
	stack.wait_value(name, "/Alarm", 0)
	assert stack.client.get_value(name, "/State") == 8
