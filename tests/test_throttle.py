"""
Brute-force protection (firmware 2.0.0+): after too many failed logins, a Shelly
answers requests that need authentication with 429 Too many requests for a while.
Every login attempt while throttled may extend that, so the driver must back off
for a long time instead of retrying with its normal backoff.

Nonce slots (firmware 2.0.0+): with all slots in use, a Shelly refuses a new
login (nonce) within 2 seconds of the previous one with a 429. Starting a device
right after probing it must therefore not log in a second time.

The driver runs with a fake mDNS browser and scaled timeouts; the wait for a
throttled device is set to THROTTLED_RETRY seconds (doubling once). Mocks start
out throttled with --throttle-seconds.
"""
import hashlib
import time

import pytest

from shelly_stack import make_stack, service_name

PASSWORD = "secret"
TIME_SCALE = 0.1
THROTTLED_RETRY = 6
THROTTLE = 10	# How long a mock throttles

AT_STARTUP = "aabbccdd5001"
ON_RECONNECT = "aabbccdd5002"
NONCE_SLOTS = "aabbccdd5003"
NOT_ENABLED = "aabbccdd5004"

STATUS_READY = 1
STATUS_ENABLED = 2
STATUS_TOO_MANY_LOGIN_ATTEMPTS = -5


def ha1(serial, password):
	return hashlib.sha256(f"admin:shellyplus-{serial}:{password}".encode()).hexdigest()


def setup(s):
	s.add_mock("at-startup", AT_STARTUP, "--password", PASSWORD, "--throttle-seconds", str(THROTTLE))
	s.add_mock("on-reconnect", ON_RECONNECT, "--password", PASSWORD)
	s.add_mock("nonce-slots", NONCE_SLOTS, "--password", PASSWORD, "--nonce-throttle-seconds", "5")
	s.add_mock("not-enabled", NOT_ENABLED, "--password", PASSWORD)
	# Correct passwords are stored and the channels are enabled, the devices are only throttled.
	for serial in (AT_STARTUP, ON_RECONNECT, NONCE_SLOTS, NOT_ENABLED):
		s.initial_settings[f"/Settings/Devices/shelly_{serial}/Password"] = f"sha256:{ha1(serial, PASSWORD)}"
	for serial in (AT_STARTUP, ON_RECONNECT, NONCE_SLOTS):
		s.initial_settings[f"/Settings/Devices/shelly_{serial}/1/Enabled"] = 1
	s.driver_env["SHELLY_TEST_THROTTLED_RETRY_SECONDS"] = str(THROTTLED_RETRY)


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
	yield from make_stack(tmp_path_factory, "throttle", setup, fake_mdns=True, time_scale=TIME_SCALE)


def auth_stats(stack, name):
	return stack.mocks[name].rpc("Mock.GetAuthStats")


def throttled_requests(stack, name):
	return auth_stats(stack, name)["throttled"]


def test_throttled_at_startup(stack):
	mock = stack.mocks["at-startup"]
	stack.announce(mock)
	stack.wait_connection_status(AT_STARTUP, STATUS_TOO_MANY_LOGIN_ATTEMPTS)
	assert stack.device(AT_STARTUP, "PasswordProtected") == 1
	attempts = throttled_requests(stack, "at-startup")
	assert attempts == 1

	# No retries before the wait is over, also not when the device announces itself again.
	stack.announce(mock)
	time.sleep(THROTTLED_RETRY - 2)
	assert throttled_requests(stack, "at-startup") == attempts

	# Once the device stops throttling, the device is connected and its enabled channel started.
	stack.wait_service(service_name("acload", AT_STARTUP), connected=False)
	stack.wait_connection_status(AT_STARTUP, STATUS_ENABLED)
	# Retried once while still throttled, with a longer wait after that.
	assert throttled_requests(stack, "at-startup") <= 2


def test_throttled_on_reconnect(stack):
	mock = stack.mocks["on-reconnect"]
	stack.announce(mock)
	name = service_name("acload", ON_RECONNECT)
	stack.wait_service(name)
	stack.wait_connection_status(ON_RECONNECT, STATUS_ENABLED)

	# The device restarts and throttles logins: the reconnect is not retried right away.
	mock.stop()
	mock.start(["--password", PASSWORD, "--throttle-seconds", str(THROTTLE)])
	stack.wait_connection_status(ON_RECONNECT, STATUS_TOO_MANY_LOGIN_ATTEMPTS)
	stack.wait_service_gone(name)
	assert throttled_requests(stack, "on-reconnect") == 1
	time.sleep(THROTTLED_RETRY - 2)
	assert throttled_requests(stack, "on-reconnect") == 1

	stack.wait_service(name, connected=False)
	stack.wait_connection_status(ON_RECONNECT, STATUS_ENABLED)
	assert throttled_requests(stack, "on-reconnect") <= 2


def test_started_with_the_probe_connection(stack):
	# The device refuses a second login within 5 seconds: the enabled channel only starts when
	# the device is started on the connection of the probe.
	stack.announce(stack.mocks["nonce-slots"])
	stack.wait_service(service_name("acload", NONCE_SLOTS))
	stack.wait_connection_status(NONCE_SLOTS, STATUS_ENABLED)
	stats = auth_stats(stack, "nonce-slots")
	assert stats["throttled"] == 0
	assert stats["challenges"] == 1
	assert stats["connections"] == 1


def test_unused_probe_connection_closed(stack):
	# Without an enabled channel, the connection of the probe is closed after a while.
	stack.announce(stack.mocks["not-enabled"])
	stack.wait_connection_status(NOT_ENABLED, STATUS_READY)
	assert auth_stats(stack, "not-enabled")["connections"] == 1
	deadline = time.time() + 30
	while auth_stats(stack, "not-enabled")["connections"] and time.time() < deadline:
		time.sleep(0.5)
	assert auth_stats(stack, "not-enabled")["connections"] == 0
