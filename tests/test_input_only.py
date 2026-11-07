"""
Input components on devices where they do not share an id with another channel.

This needs the primary/secondary handler split: an Input takes a channel of its own only when
no earlier channel already uses its id, and is added to that channel otherwise. Before the
input kind existed, a device with nothing but inputs was rejected as unsupported.

A separate module from test_input.py because each Stack starts its own driver, and the legacy
websocket server always binds port 8000.
"""
import pytest

from shelly_stack import make_stack, service_name, SHELLY_SERVICE

INPUT_ONLY = "aabbccdd7006"
SURPLUS = "aabbccdd7007"


def setup(s):
	# No switch, no meter: nothing but inputs, like an i4.
	s.add_mock("input-only", INPUT_ONLY, "--switch-channels", "0", "--input-modes", "switch,switch,analog,count")
	# One switch but two inputs: input:0 shares switch:0's id, input:1 has none to share.
	s.add_mock("surplus", SURPLUS, "--switch-channels", "1", "--input-modes", "switch,switch")


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
	yield from make_stack(tmp_path_factory, "input_only", setup, preseed_ips=True)


def test_input_only_device_is_supported(stack):
	# Before the input kind existed this device was rejected outright as unsupported.
	stack = stack
	stack.wait_reachable(INPUT_ONLY)
	inventory = stack.client.get_items(SHELLY_SERVICE)
	assert stack.device(INPUT_ONLY, "Supported") == 1
	# One channel per input, all of them the new input kind.
	for idx in (1, 2, 3, 4):
		assert inventory[f"/Devices/{INPUT_ONLY}/{idx}/Type"] == "input"
	assert f"/Devices/{INPUT_ONLY}/5/Type" not in inventory


def test_input_only_channel_registers_its_own_service(stack):
	stack = stack
	stack.set_enabled(INPUT_ONLY, 1, 1)
	name = service_name("switch", INPUT_ONLY, 0)
	items = stack.wait_service(name)
	assert items["/GenericInput/0/Settings/Type"] == 0
	assert items["/GenericInput/0/Value"] == 0
	assert items["/ProductName"] == "Shelly input device"
	# It is an input, so it has nothing switchable on it.
	assert not any(p.startswith("/SwitchableOutput/") for p in items)
	stack.mocks["input-only"].rpc("Mock.SetInput", id=0, state=True)
	stack.wait_value(name, "/GenericInput/0/Value", 1)


def test_input_only_analog_and_count_channels(stack):
	stack = stack
	stack.set_enabled(INPUT_ONLY, 3, 1)		# analog, id 2
	stack.set_enabled(INPUT_ONLY, 4, 1)		# count, id 3
	analog = stack.wait_service(service_name("switch", INPUT_ONLY, 2))
	assert analog["/GenericInput/2/Settings/Type"] == 2
	assert analog["/GenericInput/2/Settings/RangeMax"] == 100
	count = stack.wait_service(service_name("switch", INPUT_ONLY, 3))
	assert count["/GenericInput/3/Settings/Type"] == 1
	assert count["/GenericInput/3/Value"] == 7


def test_input_sharing_a_channel_id_still_attaches(stack):
	# Regression guard: input:0 must keep attaching to switch:0 rather than taking a channel.
	stack = stack
	stack.wait_reachable(SURPLUS)
	inventory = stack.client.get_items(SHELLY_SERVICE)
	assert inventory[f"/Devices/{SURPLUS}/1/Type"] == "switch"
	assert inventory[f"/Devices/{SURPLUS}/2/Type"] == "input"
	stack.set_enabled(SURPLUS, 1, 1)
	items = stack.wait_service(service_name("acload", SURPLUS, 0))
	assert "/SwitchableOutput/0/State" in items
	assert "/GenericInput/0/Value" in items


def test_surplus_input_gets_its_own_channel(stack):
	# input:1 has no switch:1, so it becomes a channel in its own right.
	stack = stack
	stack.set_enabled(SURPLUS, 2, 1)
	items = stack.wait_service(service_name("switch", SURPLUS, 1))
	assert items["/GenericInput/1/Value"] == 0
	stack.mocks["surplus"].rpc("Mock.SetInput", id=1, state=True)
	stack.wait_value(service_name("switch", SURPLUS, 1), "/GenericInput/1/Value", 1)
