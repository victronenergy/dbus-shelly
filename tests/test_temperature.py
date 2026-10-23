"""
Temperature components. A Temperature component gets its own com.victronenergy.temperature
service when its id is not used by another channel, e.g. the probes of a Sensor Add-on
(temperature:100 and up) next to switch:0. When it shares its id with another channel
(switch:0 and temperature:0), it is part of that channel and only adds /Temperature to it.

All mocks are listed in /Settings/Shelly/IpAddresses before the driver starts.
"""
import pytest
from pytest import approx

from shelly_stack import make_stack, service_name, SHELLY_SERVICE, PRODUCT_ID_INPUT

ADDON = "aabbccdd4001"
SHARED = "aabbccdd4002"
BATTERY = "aabbccdd4003"

# The mock reports 21.5 + (id % 100) degrees.
ADDON_TEMPERATURES = {100: 21.5, 101: 22.5}


def setup(s):
	s.add_mock("addon", ADDON, "--temperature-ids", "100,101")
	s.add_mock("shared", SHARED, "--temperature-ids", "0")
	s.add_mock("battery", BATTERY, "--switch-channels", "0", "--temperature-ids", "0", "--devicepower",
		"--battery-percent", "80", "--battery-voltage", "2.9")


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
	yield from make_stack(tmp_path_factory, "temperature", setup, preseed_ips=True)


def has_channel(stack, serial, index):
	return f"/Devices/{serial}/{index}/Type" in stack.client.get_items(SHELLY_SERVICE)


def enable(stack, serial, index):
	stack.wait_reachable(serial)
	stack.set_enabled(serial, index, 1)


def test_addon_discovered(stack):
	stack.wait_reachable(ADDON)
	assert stack.device(ADDON, "Supported") == 1
	assert stack.device(ADDON, "1/Type") == "switch"
	assert stack.device(ADDON, "2/Type") == "temperature"
	assert stack.device(ADDON, "3/Type") == "temperature"
	assert not has_channel(stack, ADDON, 4)
	# Not a sleepy device, so the temperature channels are not enabled by default.
	assert stack.enabled_setting(ADDON, 2) == 0
	assert stack.enabled_setting(ADDON, 3) == 0


@pytest.mark.parametrize("index, cid", [(2, 100), (3, 101)])
def test_addon_temperature_service(stack, index, cid):
	enable(stack, ADDON, index)
	name = service_name("temperature", ADDON, cid)
	items = stack.wait_service(name)
	assert items["/ProductId"] == PRODUCT_ID_INPUT
	assert items["/ProductName"] == "Shelly input device"
	assert items["/Temperature"] == approx(ADDON_TEMPERATURES[cid])
	assert items["/Status"] == 0
	assert items["/TemperatureType"] == 2	# Generic
	assert items["/CustomName"] == f"Mock addon - channel {cid}"
	assert "/State" not in items
	assert stack.setting(f"/Settings/Devices/shelly_{ADDON}_{cid}/ClassAndVrmInstance").startswith("temperature:")


def test_addon_switch_has_no_temperature(stack):
	# switch:0 has no temperature:0, so the add-on probes don't attach to it.
	enable(stack, ADDON, 1)
	items = stack.wait_service(service_name("acload", ADDON))
	assert "/Temperature" not in items


def test_addon_temperature_update_and_errors(stack):
	enable(stack, ADDON, 2)
	name = service_name("temperature", ADDON, 100)
	stack.wait_service(name)
	mock = stack.mocks["addon"]
	try:
		mock.rpc("Mock.SetTemperature", id=100, tC=30.25)
		stack.wait_value(name, "/Temperature", 30.25)
		assert stack.client.get_value(name, "/Status") == 0

		# A probe that can't be read reports tC null and an error.
		mock.rpc("Mock.SetTemperature", id=100, tC=None, errors=["read"])
		stack.wait_value(name, "/Status", 4)	# Unknown
		assert stack.client.get_value(name, "/Temperature") is None
	finally:
		mock.rpc("Mock.SetTemperature", id=100, tC=ADDON_TEMPERATURES[100])
	stack.wait_value(name, "/Status", 0)


def test_addon_temperature_type(stack):
	enable(stack, ADDON, 2)
	name = service_name("temperature", ADDON, 100)
	stack.wait_service(name)
	assert stack.client.set_value(name, "/TemperatureType", "i", 4) == 0	# Outdoor
	stack.wait_value(name, "/TemperatureType", 4)
	stack.wait_for(lambda: stack.setting(f"/Settings/Devices/shelly_{ADDON}_100/TemperatureType") == 4,
		"TemperatureType setting")
	# Out of range values are ignored.
	stack.client.set_value(name, "/TemperatureType", "i", 7)
	stack.hold(lambda: stack.client.get_value(name, "/TemperatureType") == 4, "TemperatureType to stay 4", 1)


def test_addon_temperature_custom_name(stack):
	enable(stack, ADDON, 3)
	name = service_name("temperature", ADDON, 101)
	stack.wait_service(name)
	assert stack.client.set_value(name, "/CustomName", "s", "Engine room") == 0
	stack.wait_value(name, "/CustomName", "Engine room")
	assert stack.mocks["addon"].rpc("Temperature.GetConfig", id=101)["name"] == "Engine room"


def test_shared_id_is_part_of_switch_channel(stack):
	stack.wait_reachable(SHARED)
	assert stack.device(SHARED, "1/Type") == "switch"
	assert not has_channel(stack, SHARED, 2)
	enable(stack, SHARED, 1)
	items = stack.wait_service(service_name("acload", SHARED))
	assert items["/Temperature"] == approx(21.5)
	assert not stack.client.has_name(service_name("temperature", SHARED))


def test_battery_device_enabled_by_default(stack):
	# A device with only a temperature sensor and a DevicePower component (e.g. an H&T) is sleepy,
	# so its temperature channel is enabled by default and gets the battery paths.
	stack.wait_reachable(BATTERY)
	assert stack.device(BATTERY, "Supported") == 1
	assert stack.device(BATTERY, "1/Type") == "temperature"
	name = service_name("temperature", BATTERY)
	items = stack.wait_service(name)
	assert stack.enabled_setting(BATTERY, 1) == 1
	assert items["/Temperature"] == approx(21.5)
	assert items["/BatteryPercent"] == approx(80.0)
	assert items["/BatteryVoltage"] == approx(2.9)
