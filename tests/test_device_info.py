"""
Device identification paths on a channel service.

Shelly.GetDeviceInfo is already fetched on every connect and cached as
ShellyDevice.shelly_info, so the firmware version is in hand before any channel is
created. Only the legacy websocket path (meter.py) used to publish it.
"""
import pytest

from shelly_stack import make_stack, service_name

FW = "aabbccdd6001"
FW_OTA = "aabbccdd6002"
FW_RECONNECT = "aabbccdd6003"

ONE_SWITCH = ["--switch-channels", "1"]

INITIAL_FW = "1.4.4"
UPDATED_FW = "1.5.0"


def setup(s):
	s.add_mock("fw", FW, *(ONE_SWITCH + ["--firmware-version", INITIAL_FW]))
	s.add_mock("fw-ota", FW_OTA, *(ONE_SWITCH + ["--firmware-version", INITIAL_FW]))
	s.add_mock("fw-reconnect", FW_RECONNECT, *(ONE_SWITCH + ["--firmware-version", INITIAL_FW]))


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
	yield from make_stack(tmp_path_factory, "device_info", setup, preseed_ips=True)


def test_channel_publishes_firmware_version(stack):
	stack.set_enabled(stack_serial := FW, 1, 1)
	items = stack.wait_service(service_name("acload", stack_serial))
	assert items["/FirmwareVersion"] == INITIAL_FW


def test_firmware_version_alongside_the_other_identification_paths(stack):
	items = stack.client.get_items(service_name("acload", FW))
	# The firmware version is an addition to this set, not a replacement for any of it.
	assert items["/Serial"] == FW
	assert items["/ProductName"]
	assert items["/ShellyModel"] == "Mock Shelly"
	assert items["/FirmwareVersion"] == INITIAL_FW


def test_firmware_version_follows_the_device_after_a_channel_restart(stack):
	""" A channel restart re-reads the cached device info, so an updated firmware
	    version is picked up. """
	name = service_name("acload", FW_OTA)
	stack.set_enabled(FW_OTA, 1, 1)
	assert stack.wait_service(name)["/FirmwareVersion"] == INITIAL_FW

	# Simulate an OTA: the device comes back reporting a new version.
	mock = stack.mocks["fw-ota"]
	mock.stop()
	mock.start(ONE_SWITCH + ["--firmware-version", UPDATED_FW])

	stack.set_enabled(FW_OTA, 1, 0)
	stack.wait_service_gone(name)
	stack.set_enabled(FW_OTA, 1, 1)
	assert stack.wait_service(name)["/FirmwareVersion"] == UPDATED_FW


def test_firmware_version_refreshes_on_reconnect(stack):
	""" An OTA update happens while the channel is enabled: the device drops, comes back
	    on the same channel object (ainit() does not run again) and must still report the
	    version it now runs. """
	name = service_name("acload", FW_RECONNECT)
	stack.set_enabled(FW_RECONNECT, 1, 1)
	assert stack.wait_service(name)["/FirmwareVersion"] == INITIAL_FW

	mock = stack.mocks["fw-reconnect"]
	mock.stop()
	mock.start(ONE_SWITCH + ["--firmware-version", UPDATED_FW])

	stack.wait_for(lambda: stack.client.get_value(name, "/Connected") == 1, "reconnected")
	stack.wait_value(name, "/FirmwareVersion", UPDATED_FW)
