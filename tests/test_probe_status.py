"""
How a probe classifies a device, and what the driver does with each outcome.

`_get_device_info` returns a DeviceProbeResult carrying a ProbeStatus, and the connection
manager and the dbus inventory both branch on it:

  REACHABLE      mark_success, 15 minute recheck, device listed and supported
  UNSUPPORTED    stop_connect, listed with /Supported = 0, never retried again
  UNREACHABLE    mark_failure, exponential backoff, /Reachable = 0

These tests pin the outcomes a mock can produce, so a future change to the classification
cannot silently reclassify a device: a reachable device stays supported, an unsupported one is
listed rather than dropped, an unreachable one recovers, and a merely slow one is not written
off as unsupported, which matters because UNSUPPORTED stops the retries for good.

The last test guards this change in particular. Promoting an endpoint to a serial key on the
UNSUPPORTED path means the promotion now runs on a device the driver could not use, so it has
to stay impossible to register one under a placeholder key.
"""
import time

import pytest

from shelly_stack import make_stack, service_name, SHELLY_SERVICE

GOOD = "aabbccdd9401"
UNSUPPORTED = "aabbccdd9402"
FROZEN = "aabbccdd9403"
NO_MAC = "aabbccdd9404"

ONE_SWITCH = ["--switch-channels", "1"]
# A component the driver has no handler for, so the device connects but is not usable.
NO_HANDLER = ["--switch-channels", "0", "--extra-components", "cover:0"]


def setup(s):
	s.add_mock("good", GOOD, *ONE_SWITCH)
	s.add_mock("unsupported", UNSUPPORTED, *NO_HANDLER)
	s.add_mock("frozen", FROZEN, *ONE_SWITCH)
	# Answers RPC but reports an empty mac, so it has no reliable identity.
	s.add_mock("no-mac", NO_MAC, *(ONE_SWITCH + ["--break-mac"]))


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
	yield from make_stack(tmp_path_factory, "probe_status", setup, preseed_ips=True)


def test_reachable_device_is_listed_and_supported(stack):
	stack.wait_for(lambda: stack.device(GOOD, "Reachable") == 1, "reachable")
	assert stack.device(GOOD, "Supported") == 1
	assert stack.device(GOOD, "1/Type") == "switch"


def test_unsupported_device_is_listed_but_not_supported(stack):
	""" A device that answers RPC but exposes nothing the driver handles must be listed, so the
	    user can see it was found, and marked unsupported rather than retried forever. """
	stack.wait_for(lambda: stack.device(UNSUPPORTED, "Supported") == 0,
		"device listed as unsupported")
	inv = stack.client.get_items(SHELLY_SERVICE)
	assert inv[f"/Devices/{UNSUPPORTED}/Model"], "an unsupported device should still report its model"
	# No channels, so nothing to enable.
	assert f"/Devices/{UNSUPPORTED}/1/Type" not in inv


def test_unreachable_device_is_marked_not_reachable(stack):
	""" The mock is killed, so the port refuses connections. """
	name = service_name("acload", GOOD)
	stack.set_enabled(GOOD, 1, 1)
	stack.wait_service(name)
	stack.mocks["good"].stop()
	stack.wait_for(lambda: stack.device(GOOD, "Reachable") == 0, "marked unreachable", timeout=90)
	stack.mocks["good"].start(ONE_SWITCH)
	stack.wait_for(lambda: stack.device(GOOD, "Reachable") == 1, "reachable again", timeout=120)


def test_rpc_timeout_does_not_mark_the_device_unsupported(stack):
	""" A device that stops answering must not be reclassified as unsupported, because
	    UNSUPPORTED stops the retries permanently. """
	mock = stack.mocks["frozen"]
	stack.wait_for(lambda: stack.device(FROZEN, "Reachable") == 1, "reachable first")
	assert stack.device(FROZEN, "Supported") == 1
	mock.rpc("Mock.Freeze", frozen=True)
	try:
		# Whatever else happens, it must still be considered a supported device.
		stack.hold(lambda: stack.device(FROZEN, "Supported") == 1,
			"a frozen device stays supported", 12)
	finally:
		mock.rpc("Mock.Freeze", frozen=False)
	stack.wait_for(lambda: stack.device(FROZEN, "Reachable") == 1, "reachable after thaw", timeout=120)


# --- state transitions and identity ------------------------------------------

def test_unsupported_becomes_reachable_after_a_capability_change(stack):
	""" UNSUPPORTED calls stop_connect, so the device is deliberately not retried. A /Refresh
	    is the documented way back, and after it the device must be picked up properly. """
	assert stack.device(UNSUPPORTED, "Supported") == 0
	mock = stack.mocks["unsupported"]
	mock.stop()
	mock.start(ONE_SWITCH)			# now it has a component the driver handles
	assert stack.client.set_value(SHELLY_SERVICE, "/Refresh", "i", 1) == 0
	stack.wait_for(lambda: stack.device(UNSUPPORTED, "Supported") == 1,
		"unsupported device becomes supported after a refresh", timeout=90)
	stack.wait_for(lambda: stack.device(UNSUPPORTED, "1/Type") == "switch", "channel appears")


def test_reachable_becomes_unsupported_without_leaving_a_service_behind(stack):
	""" A device that loses its supported components must not leave its channel service on the
	    bus, or the rest of Venus keeps seeing a device that no longer works. """
	serial = UNSUPPORTED
	stack.set_enabled(serial, 1, 1)
	name = service_name("acload", serial)
	stack.wait_service(name)

	mock = stack.mocks["unsupported"]
	mock.stop()
	mock.start(NO_HANDLER)			# back to a device with nothing the driver handles
	assert stack.client.set_value(SHELLY_SERVICE, "/Refresh", "i", 1) == 0

	stack.wait_service_gone(name, timeout=120)
	stack.wait_for(lambda: stack.device(serial, "Supported") == 0, "marked unsupported", timeout=90)
	# And nothing stale is left on the bus for it, under either service type it could take.
	for kind in ("acload", "switch", "pvinverter", "genset", "heatpump"):
		assert not stack.client.has_name(service_name(kind, serial)), kind


def test_a_device_reporting_no_mac_is_not_registered_as_unknown(stack):
	""" The serial is the inventory key, and `promote_endpoint_to_serial` is handed
	    `info.get('mac', 'unknown')`, so a device without a usable mac would in principle be
	    keyed on the literal string 'unknown' and collide with every other such device.

	    It does not happen, but not because of that default: ShellyDevice.connect() requires a
	    serial of its own ("Failed to get serial number") and fails first, so such a device is
	    classified UNREACHABLE and never reaches the promotion. This test pins that invariant
	    rather than the mechanism, so it keeps holding if either side changes. """
	# Give the probe time to have tried it.
	stack.hold(lambda: True, "settle", 3)
	inv = stack.client.get_items(SHELLY_SERVICE)
	bogus = [k for k in inv if k.startswith("/Devices/unknown")]
	assert not bogus, bogus
	# It must also not have been registered under the mac it does not report.
	assert not [k for k in inv if k.startswith(f"/Devices/{NO_MAC}")]
