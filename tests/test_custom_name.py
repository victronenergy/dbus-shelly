"""
Channel custom name: fetched from <Component>.GetConfig, falling back to
"<device name> - channel <id>" when the channel has no name, and to
"Unknown shelly device - channel <id>" when the device info can't be fetched
either. In that last case the driver retries every 2s, at most 10 times.

Every scenario runs on a plain switch and on a device with only a PM1
component (e.g. a Plus PM Mini), each with its own mock.

"Not responding" is simulated by the mock returning an RPC error for
<Component>.GetConfig and Shelly.GetDeviceInfo. (A timeout instead would also
make the driver start its reconnect logic, which is a different scenario.)
The name fetch is triggered by a role change, which restarts the channel while
the device stays connected.
"""
import time

import pytest

from shelly_stack import make_stack, service_name

RETRY_INTERVAL = 2
MAX_RETRIES = 10
UNKNOWN_NAME = "Unknown shelly device - channel 0"

KINDS = {
	# kind: (mock args, RPC component, D-Bus paths that carry the name, serial prefix)
	"switch": (["--switch-channels", "1"], "Switch", ["/CustomName", "/SwitchableOutput/0/Settings/CustomName"], "aabbccdd31"),
	"pm1": (["--switch-channels", "0", "--pm1-channels", "1"], "PM1", ["/CustomName"], "aabbccdd32"),
}

SCENARIOS = {
	# scenario: (serial suffix, channel name on the device)
	"named": ("01", "Kitchen"),
	"unnamed": ("02", ""),
	"fault": ("03", "Pantry"),
	"recover": ("04", "Cellar"),
}


def serial(kind, scenario):
	return KINDS[kind][3] + SCENARIOS[scenario][0]


def mock_name(kind, scenario):
	return f"{kind}-{scenario}"


def setup(s):
	for kind, (args, _, _, _) in KINDS.items():
		for scenario, (_, channel_name) in SCENARIOS.items():
			s.add_mock(mock_name(kind, scenario), serial(kind, scenario), *args, "--channel-name", channel_name)


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
	yield from make_stack(tmp_path_factory, "customname", setup, preseed_ips=True)


def enable(stack, kind, scenario):
	sn = serial(kind, scenario)
	stack.wait_reachable(sn)
	stack.set_enabled(sn, 1, 1)
	name = service_name("acload", sn)
	stack.wait_service(name)
	return name


def wait_name(stack, kind, service, expected, timeout=10):
	for path in KINDS[kind][2]:
		stack.wait_value(service, path, expected, timeout=timeout)


def device_name(stack, kind, scenario):
	return stack.mocks[mock_name(kind, scenario)].rpc(f"{KINDS[kind][1]}.GetConfig", id=0)["name"]


def restart_channel(stack, kind, scenario):
	""" Restart the channel with a role change; returns the new service name. """
	sn = serial(kind, scenario)
	old, new = service_name("acload", sn), service_name("heatpump", sn)
	assert stack.client.set_value(old, "/Role", "s", "heatpump") == 0
	stack.wait_service_gone(old)
	stack.wait_service(new)
	return new


def attempt_waves(log, method):
	""" Group the calls of method into waves: calls less than 1s apart belong to
	    one name fetch attempt. Returns the start time of each wave. """
	times = sorted(t for t, m in log if m == method)
	waves = []
	for t in times:
		if not waves or t - last >= 1:
			waves.append(t)
		last = t
	return waves


@pytest.mark.parametrize("kind", KINDS)
def test_name_from_device(stack, kind):
	service = enable(stack, kind, "named")
	wait_name(stack, kind, service, "Kitchen")


@pytest.mark.parametrize("kind", KINDS)
def test_unnamed_channel_uses_device_name(stack, kind):
	service = enable(stack, kind, "unnamed")
	wait_name(stack, kind, service, f"Mock {mock_name(kind, 'unnamed')} - channel 0")


@pytest.mark.parametrize("kind", KINDS)
def test_rename_from_dbus(stack, kind):
	service = service_name("acload", serial(kind, "named"))
	assert stack.client.set_value(service, "/CustomName", "s", "Garage") == 0
	stack.wait_for(lambda: device_name(stack, kind, "named") == "Garage", "name to be set on the device", timeout=10)
	wait_name(stack, kind, service, "Garage")


@pytest.mark.parametrize("kind", KINDS)
def test_rename_on_device(stack, kind):
	# As when renaming in the Shelly app: the device sends a config_changed event.
	service = service_name("acload", serial(kind, "named"))
	stack.mocks[mock_name(kind, "named")].rpc(f"{KINDS[kind][1]}.SetConfig", id=0, config={"name": "Shed"})
	wait_name(stack, kind, service, "Shed")


@pytest.mark.parametrize("kind", KINDS)
def test_no_response_retries_then_falls_back(stack, kind):
	enable(stack, kind, "fault")
	mock = stack.mocks[mock_name(kind, "fault")]
	mock.rpc("Mock.SetFaults", methods=[f"{KINDS[kind][1]}.GetConfig", "Shelly.GetDeviceInfo"])
	mock.rpc("Mock.GetCallLog", reset=True)
	try:
		service = restart_channel(stack, kind, "fault")
		wait_name(stack, kind, service, UNKNOWN_NAME)

		# Fail fast if the retries stop early
		stack.wait_for(lambda: len(attempt_waves(mock.rpc("Mock.GetCallLog"), "Shelly.GetDeviceInfo")) >= 3,
			"at least 2 retries", timeout=3 * RETRY_INTERVAL + 2)
		# The first attempt plus MAX_RETRIES retries, RETRY_INTERVAL apart.
		stack.wait_for(lambda: len(attempt_waves(mock.rpc("Mock.GetCallLog"), "Shelly.GetDeviceInfo")) >= 1 + MAX_RETRIES,
			f"at least {MAX_RETRIES} retries", timeout=(MAX_RETRIES + 5) * RETRY_INTERVAL)
		waves = attempt_waves(mock.rpc("Mock.GetCallLog"), "Shelly.GetDeviceInfo")
		assert all(b - a >= RETRY_INTERVAL - 0.5 for a, b in zip(waves, waves[1:])), waves

		# And then it stops retrying, keeping the fallback name.
		time.sleep(2 * RETRY_INTERVAL + 1)
		assert len(attempt_waves(mock.rpc("Mock.GetCallLog"), "Shelly.GetDeviceInfo")) == 1 + MAX_RETRIES
		wait_name(stack, kind, service, UNKNOWN_NAME, timeout=1)
		# The channel itself keeps working
		assert stack.client.get_value(service, "/Connected") == 1
		assert stack.client.get_value(service, "/Ac/Power") is not None
	finally:
		mock.rpc("Mock.SetFaults", methods=[])


@pytest.mark.parametrize("kind", KINDS)
def test_no_response_then_recovers(stack, kind):
	enable(stack, kind, "recover")
	mock = stack.mocks[mock_name(kind, "recover")]
	mock.rpc("Mock.SetFaults", methods=[f"{KINDS[kind][1]}.GetConfig", "Shelly.GetDeviceInfo"])
	try:
		service = restart_channel(stack, kind, "recover")
		wait_name(stack, kind, service, UNKNOWN_NAME)
	finally:
		mock.rpc("Mock.SetFaults", methods=[])
	# The next retry picks up the real name.
	wait_name(stack, kind, service, "Cellar", timeout=3 * RETRY_INTERVAL)
