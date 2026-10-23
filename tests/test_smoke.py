"""
Basic end-to-end flow: discover through /IpAddresses -> enable -> switch -> disable.

The tests build on each other and must run in file order (pytest's default).
"""
import pytest

from shelly_stack import (make_stack, service_name, SHELLY_SERVICE,
	PRODUCT_ID_SWITCH, PRODUCT_ID_INPUT)

SWITCH_SERIAL = "aabbccdd0001"
SMOKE_SERIAL = "aabbccdd0002"
SWITCH_SERVICE = service_name("acload", SWITCH_SERIAL)
SMOKE_SERVICE = service_name("digitalinput", SMOKE_SERIAL)
SWITCH_APOWER = 1000.0


def setup(s):
	s.add_mock("switch", SWITCH_SERIAL, "--apower", str(SWITCH_APOWER))
	s.add_mock("smoke", SMOKE_SERIAL, "--switch-channels", "0", "--smoke")


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
	yield from make_stack(tmp_path_factory, "smoke", setup)


def test_shelly_service_registered(stack):
	items = stack.client.get_items(SHELLY_SERVICE)
	assert items["/IpAddresses"] == ""
	assert items["/AutoScan"] == 0


def test_manual_ip_discovery(stack):
	c = stack.client
	ips = ",".join(m.endpoint for m in stack.mocks.values())
	assert c.set_value(SHELLY_SERVICE, "/IpAddresses", "s", ips) == 0

	for serial in (SWITCH_SERIAL, SMOKE_SERIAL):
		stack.wait_reachable(serial)
		assert stack.device(serial, "Supported") == 1
		assert stack.device(serial, "DiscoveryType") == "Manual"

	assert stack.device(SWITCH_SERIAL, "1/Type") == "switch"
	assert stack.device(SWITCH_SERIAL, "1/Enabled") == 0
	assert stack.device(SMOKE_SERIAL, "1/Type") == "digitalinput"
	assert stack.setting("/Settings/Shelly/IpAddresses") == ips


def test_smoke_sensor_enabled_by_default(stack):
	items = stack.wait_service(SMOKE_SERVICE)
	assert items["/Serial"] == SMOKE_SERIAL
	assert items["/ProductId"] == PRODUCT_ID_INPUT
	assert items["/ProductName"] == "Shelly input device"
	assert items["/Type"] == 6
	assert items["/Alarm"] == 0
	assert items["/BatteryPercent"] == 100.0
	assert stack.enabled_setting(SMOKE_SERIAL, 1) == 1


def test_smoke_alarm(stack):
	stack.mocks["smoke"].rpc("Mock.SetAlarm", alarm=True)
	stack.wait_value(SMOKE_SERVICE, "/Alarm", 2)
	assert stack.client.get_value(SMOKE_SERVICE, "/State") == 9

	stack.mocks["smoke"].rpc("Mock.SetAlarm", alarm=False)
	stack.wait_value(SMOKE_SERVICE, "/Alarm", 0)
	assert stack.client.get_value(SMOKE_SERVICE, "/State") == 8


def test_enable_switch_channel(stack):
	c = stack.client
	assert not c.has_name(SWITCH_SERVICE)
	stack.set_enabled(SWITCH_SERIAL, 1, 1)

	items = stack.wait_service(SWITCH_SERVICE)
	assert items["/Serial"] == SWITCH_SERIAL
	assert items["/ProductId"] == PRODUCT_ID_SWITCH
	assert items["/Role"] == "acload"
	assert items["/SwitchableOutput/0/State"] == 0
	assert items["/Ac/Power"] == 0
	stack.wait_for(lambda: stack.enabled_setting(SWITCH_SERIAL, 1) == 1, "Enabled setting persisted")


def test_switch_on_from_dbus(stack):
	assert stack.client.set_value(SWITCH_SERVICE, "/SwitchableOutput/0/State", "i", 1) == 0

	stack.wait_for(lambda: stack.mocks["switch"].rpc("Switch.GetStatus", id=0)["output"] is True,
		"mock output on")
	stack.wait_value(SWITCH_SERVICE, "/Ac/Power", SWITCH_APOWER)


def test_switch_off_from_device(stack):
	stack.mocks["switch"].rpc("Switch.Set", id=0, on=False)

	stack.wait_value(SWITCH_SERVICE, "/SwitchableOutput/0/State", 0)
	stack.wait_value(SWITCH_SERVICE, "/Ac/Power", 0)


def test_disable_switch_channel(stack):
	stack.set_enabled(SWITCH_SERIAL, 1, 0)

	stack.wait_service_gone(SWITCH_SERVICE)
	stack.wait_for(lambda: stack.enabled_setting(SWITCH_SERIAL, 1) == 0, "Enabled setting persisted")
	# The other device must be unaffected.
	assert stack.client.has_name(SMOKE_SERVICE)
