"""
Password protected devices: /Devices/<serial>/ConnectionStatus, /PasswordProtected
and /Password, and how the driver deals with missing, wrong and changed passwords.

The mocks enforce digest authentication on the websocket like firmware 2.0.0+
(random nonces, increasing nc) or, with --legacy-auth, like older firmware.
Every scenario uses its own mock device, so they don't influence each other.
All mocks are listed in /Settings/Shelly/IpAddresses before the driver starts.
"""
import hashlib
import time

import pytest

from shelly_stack import make_stack, service_name

PASSWORD = "secret"

NO_PASSWORD = "aabbccdd3001"
WRONG_THEN_RIGHT = "aabbccdd3002"
LEGACY = "aabbccdd3003"
HA1_INPUT = "aabbccdd3004"
CHANGED_ON_DEVICE = "aabbccdd3005"
AUTH_DISABLED = "aabbccdd3006"
STORED = "aabbccdd3007"
UNPROTECTED = "aabbccdd3008"
WRONG_WHILE_RUNNING = "aabbccdd3009"

STATUS_READY = 1
STATUS_ENABLED = 2
STATUS_AUTH_FAILED = -1
STATUS_PASSWORD_REQUIRED = -2


def ha1(serial, password):
	# The realm is the device's auth_domain, which the mock sets to its id.
	return hashlib.sha256(f"admin:shellyplus-{serial}:{password}".encode()).hexdigest()


def setup(s):
	s.add_mock("no-password", NO_PASSWORD, "--password", PASSWORD)
	s.add_mock("wrong-then-right", WRONG_THEN_RIGHT, "--password", PASSWORD)
	s.add_mock("legacy", LEGACY, "--password", PASSWORD, "--legacy-auth")
	s.add_mock("ha1-input", HA1_INPUT, "--password", PASSWORD)
	s.add_mock("changed-on-device", CHANGED_ON_DEVICE, "--password", PASSWORD)
	s.add_mock("auth-disabled", AUTH_DISABLED, "--password", PASSWORD)
	s.add_mock("stored", STORED, "--password", PASSWORD)
	s.add_mock("unprotected", UNPROTECTED)
	s.add_mock("wrong-while-running", WRONG_WHILE_RUNNING, "--password", PASSWORD)
	# Password stored before the driver starts, e.g. by an earlier run. Its channel is enabled as well.
	s.initial_settings[f"/Settings/Devices/shelly_{STORED}/Password"] = f"sha256:{ha1(STORED, PASSWORD)}"
	s.initial_settings[f"/Settings/Devices/shelly_{STORED}/1/Enabled"] = 1


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
	yield from make_stack(tmp_path_factory, "auth", setup, preseed_ips=True)


def auth_failures(stack, name):
	return stack.mocks[name].rpc("Mock.GetAuthStats")["failures"]


def password_setting(stack, serial):
	return stack.setting(f"/Settings/Devices/shelly_{serial}/Password")


def enable_and_wait(stack, serial):
	stack.set_enabled(serial, 1, 1)
	stack.wait_service(service_name("acload", serial))
	stack.wait_connection_status(serial, STATUS_ENABLED)


def test_unprotected_device(stack):
	stack.wait_connection_status(UNPROTECTED, STATUS_READY)
	assert stack.device(UNPROTECTED, "PasswordProtected") == 0
	assert stack.device(UNPROTECTED, "Password") == ""
	enable_and_wait(stack, UNPROTECTED)


def test_password_required(stack):
	stack.wait_connection_status(NO_PASSWORD, STATUS_PASSWORD_REQUIRED)
	assert stack.device(NO_PASSWORD, "PasswordProtected") == 1
	assert stack.device(NO_PASSWORD, "Password") == ""
	assert stack.device(NO_PASSWORD, "Model") == "MockSwitchEM"
	# Without a valid password it is unknown whether the device is supported, so there are no channels.
	with pytest.raises(RuntimeError):
		stack.device(NO_PASSWORD, "1/Enabled")
	# A protected device's password can't be cleared.
	stack.set_password(NO_PASSWORD, "")
	assert stack.device(NO_PASSWORD, "ConnectionStatus") == STATUS_PASSWORD_REQUIRED
	# Requests without credentials are challenged, they don't count as failed logins.
	assert auth_failures(stack, "no-password") == 0


def test_wrong_then_right_password(stack):
	stack.wait_connection_status(WRONG_THEN_RIGHT, STATUS_PASSWORD_REQUIRED)

	stack.set_password(WRONG_THEN_RIGHT, "wrong")
	stack.wait_connection_status(WRONG_THEN_RIGHT, STATUS_AUTH_FAILED)
	assert stack.device(WRONG_THEN_RIGHT, "Password") == "*****"
	failures = auth_failures(stack, "wrong-then-right")
	assert failures == 1
	# A wrong password is not retried: failed logins trigger the device's brute-force protection.
	time.sleep(12)
	assert auth_failures(stack, "wrong-then-right") == failures
	assert stack.device(WRONG_THEN_RIGHT, "ConnectionStatus") == STATUS_AUTH_FAILED

	stack.set_password(WRONG_THEN_RIGHT, PASSWORD)
	stack.wait_connection_status(WRONG_THEN_RIGHT, STATUS_READY)
	assert stack.device(WRONG_THEN_RIGHT, "1/Type") == "switch"
	enable_and_wait(stack, WRONG_THEN_RIGHT)


def test_only_ha1_is_stored(stack):
	stack.wait_connection_status(WRONG_THEN_RIGHT, STATUS_ENABLED)
	stored = password_setting(stack, WRONG_THEN_RIGHT)
	assert stored == f"sha256:{ha1(WRONG_THEN_RIGHT, PASSWORD)}"
	assert PASSWORD not in stored


def test_legacy_firmware(stack):
	stack.wait_connection_status(LEGACY, STATUS_PASSWORD_REQUIRED)
	stack.set_password(LEGACY, PASSWORD)
	stack.wait_connection_status(LEGACY, STATUS_READY)
	enable_and_wait(stack, LEGACY)


def test_ha1_as_password(stack):
	stack.wait_connection_status(HA1_INPUT, STATUS_PASSWORD_REQUIRED)
	stack.set_password(HA1_INPUT, f"sha256:{ha1(HA1_INPUT, PASSWORD)}")
	stack.wait_connection_status(HA1_INPUT, STATUS_READY)


def test_stored_password_at_startup(stack):
	# The device is added by IP, so its serial (and its password) are only known after connecting.
	stack.wait_service(service_name("acload", STORED))
	stack.wait_connection_status(STORED, STATUS_ENABLED)
	assert auth_failures(stack, "stored") == 0


def test_password_changed_on_device(stack):
	mock = stack.mocks["changed-on-device"]
	stack.wait_connection_status(CHANGED_ON_DEVICE, STATUS_PASSWORD_REQUIRED)
	stack.set_password(CHANGED_ON_DEVICE, PASSWORD)
	stack.wait_connection_status(CHANGED_ON_DEVICE, STATUS_READY)
	enable_and_wait(stack, CHANGED_ON_DEVICE)
	name = service_name("acload", CHANGED_ON_DEVICE)

	# The device restarts with a new password, so the driver's reconnect is rejected.
	mock.stop()
	mock.start(["--password", "new-secret"])
	stack.wait_connection_status(CHANGED_ON_DEVICE, STATUS_AUTH_FAILED)
	stack.wait_service_gone(name)
	# The channel stays enabled in the settings, so it starts again once the new password is entered.
	assert stack.enabled_setting(CHANGED_ON_DEVICE, 1) == 1
	failures = auth_failures(stack, "changed-on-device")
	assert failures <= 2
	time.sleep(12)
	assert auth_failures(stack, "changed-on-device") == failures

	stack.set_password(CHANGED_ON_DEVICE, "new-secret")
	stack.wait_service(name)
	stack.wait_connection_status(CHANGED_ON_DEVICE, STATUS_ENABLED)


def test_wrong_password_entered_while_running(stack):
	stack.wait_connection_status(WRONG_WHILE_RUNNING, STATUS_PASSWORD_REQUIRED)
	stack.set_password(WRONG_WHILE_RUNNING, PASSWORD)
	stack.wait_connection_status(WRONG_WHILE_RUNNING, STATUS_READY)
	enable_and_wait(stack, WRONG_WHILE_RUNNING)
	name = service_name("acload", WRONG_WHILE_RUNNING)

	# Entering the correct password again keeps the device running.
	stack.set_password(WRONG_WHILE_RUNNING, f"sha256:{ha1(WRONG_WHILE_RUNNING, PASSWORD)}")
	stack.hold(lambda: stack.device(WRONG_WHILE_RUNNING, "ConnectionStatus") == STATUS_ENABLED, "device enabled", 3)
	assert auth_failures(stack, "wrong-while-running") == 0

	# The running connection is still authenticated with the old password, but the new one is checked right away.
	stack.set_password(WRONG_WHILE_RUNNING, "wrong")
	stack.wait_connection_status(WRONG_WHILE_RUNNING, STATUS_AUTH_FAILED)
	stack.wait_service_gone(name)
	assert auth_failures(stack, "wrong-while-running") == 1
	assert stack.enabled_setting(WRONG_WHILE_RUNNING, 1) == 1

	stack.set_password(WRONG_WHILE_RUNNING, PASSWORD)
	stack.wait_service(name)
	stack.wait_connection_status(WRONG_WHILE_RUNNING, STATUS_ENABLED)


def test_authentication_disabled_on_device(stack):
	mock = stack.mocks["auth-disabled"]
	stack.wait_connection_status(AUTH_DISABLED, STATUS_PASSWORD_REQUIRED)
	stack.set_password(AUTH_DISABLED, PASSWORD)
	stack.wait_connection_status(AUTH_DISABLED, STATUS_READY)
	enable_and_wait(stack, AUTH_DISABLED)

	# The device sends a sys config_changed event when authentication is enabled or disabled.
	mock.rpc("Shelly.SetAuth", user="admin", realm=f"shellyplus-{AUTH_DISABLED}", ha1=None)
	stack.wait_for(lambda: stack.device(AUTH_DISABLED, "PasswordProtected") == 0, "PasswordProtected == 0")
	assert stack.device(AUTH_DISABLED, "ConnectionStatus") == STATUS_ENABLED

	# The password of an unprotected device can be cleared.
	stack.set_password(AUTH_DISABLED, "")
	stack.wait_for(lambda: password_setting(stack, AUTH_DISABLED) == "", "password setting cleared")
	assert stack.device(AUTH_DISABLED, "Password") == ""
