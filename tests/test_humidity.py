"""
Humidity components. Venus has no humidity service, so /Humidity is part of the
com.victronenergy.temperature service. A Humidity component that shares its id with
another channel is part of that channel and only adds /Humidity to it, e.g. a DHT22 on a
Sensor Add-on (temperature:100 and humidity:100), or humidity:0 next to switch:0. A
Humidity component with an id of its own gets its own temperature service.

All mocks are listed in /Settings/Shelly/IpAddresses before the driver starts.
"""
import pytest
from pytest import approx

from shelly_stack import make_stack, service_name, SHELLY_SERVICE, PRODUCT_ID_INPUT

DHT22 = "aabbccdd5001"
STANDALONE = "aabbccdd5002"
SHARED = "aabbccdd5003"
BATTERY = "aabbccdd5004"


def setup(s):
	s.add_mock("dht22", DHT22, "--temperature-ids", "100", "--humidity-ids", "100")
	s.add_mock("standalone", STANDALONE, "--humidity-ids", "100")
	s.add_mock("shared", SHARED, "--humidity-ids", "0")
	s.add_mock("battery", BATTERY, "--switch-channels", "0", "--temperature-ids", "0", "--humidity-ids", "0",
		"--devicepower", "--battery-percent", "80", "--battery-voltage", "2.9")


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
	yield from make_stack(tmp_path_factory, "humidity", setup, preseed_ips=True)


def has_channel(stack, serial, index):
	return f"/Devices/{serial}/{index}/Type" in stack.client.get_items(SHELLY_SERVICE)


def enable(stack, serial, index):
	stack.wait_reachable(serial)
	stack.set_enabled(serial, index, 1)


def test_dht22_is_part_of_temperature_channel(stack):
	stack.wait_reachable(DHT22)
	assert stack.device(DHT22, "1/Type") == "switch"
	assert stack.device(DHT22, "2/Type") == "temperature"
	assert not has_channel(stack, DHT22, 3)
	enable(stack, DHT22, 2)
	items = stack.wait_service(service_name("temperature", DHT22, 100))
	assert items["/Temperature"] == approx(21.5)
	assert items["/Humidity"] == approx(55.0)
	assert items["/Status"] == 0


def test_dht22_humidity_update(stack):
	enable(stack, DHT22, 2)
	name = service_name("temperature", DHT22, 100)
	stack.wait_service(name)
	mock = stack.mocks["dht22"]
	try:
		mock.rpc("Mock.SetHumidity", id=100, rh=72.5)
		stack.wait_value(name, "/Humidity", 72.5)
	finally:
		mock.rpc("Mock.SetHumidity", id=100, rh=55.0)
	stack.wait_value(name, "/Humidity", 55.0)


def test_dht22_switch_has_no_humidity(stack):
	# switch:0 has no humidity:0, so the add-on sensor doesn't attach to it.
	enable(stack, DHT22, 1)
	items = stack.wait_service(service_name("acload", DHT22))
	assert "/Humidity" not in items


def test_standalone_discovered(stack):
	stack.wait_reachable(STANDALONE)
	assert stack.device(STANDALONE, "Supported") == 1
	assert stack.device(STANDALONE, "1/Type") == "switch"
	assert stack.device(STANDALONE, "2/Type") == "humidity"
	assert not has_channel(stack, STANDALONE, 3)
	# Not a sleepy device, so the humidity channel is not enabled by default.
	assert stack.enabled_setting(STANDALONE, 2) == 0


def test_standalone_service(stack):
	enable(stack, STANDALONE, 2)
	name = service_name("temperature", STANDALONE, 100)
	items = stack.wait_service(name)
	assert items["/ProductId"] == PRODUCT_ID_INPUT
	assert items["/ProductName"] == "Shelly input device"
	assert items["/Humidity"] == approx(55.0)
	assert items["/Temperature"] is None
	assert items["/Status"] == 0
	assert items["/CustomName"] == "Mock standalone - channel 100"
	assert "/State" not in items
	assert "/TemperatureType" not in items
	assert stack.setting(f"/Settings/Devices/shelly_{STANDALONE}_100/ClassAndVrmInstance").startswith("temperature:")


def test_standalone_update_and_errors(stack):
	enable(stack, STANDALONE, 2)
	name = service_name("temperature", STANDALONE, 100)
	stack.wait_service(name)
	mock = stack.mocks["standalone"]
	try:
		mock.rpc("Mock.SetHumidity", id=100, rh=40.0)
		stack.wait_value(name, "/Humidity", 40.0)
		assert stack.client.get_value(name, "/Status") == 0

		# A sensor that can't be read reports rh null and an error.
		mock.rpc("Mock.SetHumidity", id=100, rh=None, errors=["read"])
		stack.wait_value(name, "/Status", 4)	# Unknown
		assert stack.client.get_value(name, "/Humidity") is None
	finally:
		mock.rpc("Mock.SetHumidity", id=100, rh=55.0)
	stack.wait_value(name, "/Status", 0)


def test_standalone_custom_name(stack):
	enable(stack, STANDALONE, 2)
	name = service_name("temperature", STANDALONE, 100)
	stack.wait_service(name)
	assert stack.client.set_value(name, "/CustomName", "s", "Cabin") == 0
	stack.wait_value(name, "/CustomName", "Cabin")
	assert stack.mocks["standalone"].rpc("Humidity.GetConfig", id=100)["name"] == "Cabin"


def test_shared_id_is_part_of_switch_channel(stack):
	stack.wait_reachable(SHARED)
	assert stack.device(SHARED, "1/Type") == "switch"
	assert not has_channel(stack, SHARED, 2)
	enable(stack, SHARED, 1)
	items = stack.wait_service(service_name("acload", SHARED))
	assert items["/Humidity"] == approx(55.0)
	assert not stack.client.has_name(service_name("temperature", SHARED))


def test_battery_device_enabled_by_default(stack):
	# A device with temperature, humidity and DevicePower components (e.g. an H&T) is sleepy, so its
	# temperature channel is enabled by default and gets the humidity and battery paths.
	stack.wait_reachable(BATTERY)
	assert stack.device(BATTERY, "Supported") == 1
	assert stack.device(BATTERY, "1/Type") == "temperature"
	assert not has_channel(stack, BATTERY, 2)
	items = stack.wait_service(service_name("temperature", BATTERY))
	assert stack.enabled_setting(BATTERY, 1) == 1
	assert items["/Temperature"] == approx(21.5)
	assert items["/Humidity"] == approx(55.0)
	assert items["/BatteryPercent"] == approx(80.0)
	assert items["/BatteryVoltage"] == approx(2.9)
