"""
Shelly Input components on the Venus generic input API.

An Input handler attaches to the channel whose id matches the component, so input:0 lands on
the service of switch:0 and publishes /GenericInput/0/... next to that channel's
/SwitchableOutput/0/... paths. No separate service is registered.

The four Shelly input types are driven through Mock.SetInput, a test hook on the mock server.
"""
import pytest

from shelly_stack import make_stack, service_name, SHELLY_SERVICE

SWITCH_IN = "aabbccdd7001"
ANALOG_IN = "aabbccdd7002"
COUNT_IN = "aabbccdd7003"
BUTTON_IN = "aabbccdd7004"
NO_MATCH = "aabbccdd7005"

ONE_SWITCH = ["--switch-channels", "1"]


def setup(s):
	s.add_mock("switch-in", SWITCH_IN, *(ONE_SWITCH + ["--input-modes", "switch"]))
	s.add_mock("analog-in", ANALOG_IN, *(ONE_SWITCH + ["--input-modes", "analog"]))
	s.add_mock("count-in", COUNT_IN, *(ONE_SWITCH + ["--input-modes", "count"]))
	s.add_mock("button-in", BUTTON_IN, *(ONE_SWITCH + ["--input-modes", "button"]))
	# Two inputs but only one switch, so input:1 has no channel to attach to.
	s.add_mock("no-match", NO_MATCH, *(ONE_SWITCH + ["--input-modes", "switch,switch"]))


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
	yield from make_stack(tmp_path_factory, "input", setup, preseed_ips=True)


def enable(stack, serial):
	stack.wait_reachable(serial)
	stack.set_enabled(serial, 1, 1)
	return stack.wait_service(service_name("acload", serial))


# --- switch mode -------------------------------------------------------------

def test_switch_input_is_a_discrete_indicator(stack):
	items = enable(stack, SWITCH_IN)
	assert items["/GenericInput/0/Settings/Type"] == 0			# discrete value indicator
	assert items["/GenericInput/0/Settings/ValidTypes"] == 1		# only that type offered
	assert items["/GenericInput/0/Settings/Labels"] == ["/off", "/on"]
	assert items["/GenericInput/0/Name"] == "Input 0"
	assert items["/GenericInput/0/Status"] == 0				# OK
	assert items["/GenericInput/0/Value"] == 0


def test_switch_input_follows_the_device(stack):
	name = service_name("acload", SWITCH_IN)
	stack.mocks["switch-in"].rpc("Mock.SetInput", id=0, state=True)
	stack.wait_value(name, "/GenericInput/0/Value", 1)
	stack.mocks["switch-in"].rpc("Mock.SetInput", id=0, state=False)
	stack.wait_value(name, "/GenericInput/0/Value", 0)


def test_disabled_input_invalidates_the_value(stack):
	# A disabled input reports null, which must become an invalid value rather than a 0.
	name = service_name("acload", SWITCH_IN)
	stack.mocks["switch-in"].rpc("Mock.SetInput", id=0, state=True)
	stack.wait_value(name, "/GenericInput/0/Value", 1)
	stack.mocks["switch-in"].rpc("Mock.SetInput", id=0, enable=False)
	stack.wait_value(name, "/GenericInput/0/Value", None)
	stack.mocks["switch-in"].rpc("Mock.SetInput", id=0, enable=True)
	stack.wait_value(name, "/GenericInput/0/Value", 1)


def test_input_errors_raise_the_status(stack):
	name = service_name("acload", SWITCH_IN)
	stack.mocks["switch-in"].rpc("Mock.SetInput", id=0, errors=["out_of_range"])
	stack.wait_value(name, "/GenericInput/0/Status", 1)			# fault
	stack.mocks["switch-in"].rpc("Mock.SetInput", id=0, errors=[])
	stack.wait_value(name, "/GenericInput/0/Status", 0)


def test_input_shares_the_switch_service(stack):
	# The paths sit on the existing channel service, alongside its switchable output.
	items = stack.client.get_items(service_name("acload", SWITCH_IN))
	assert "/SwitchableOutput/0/State" in items
	assert "/GenericInput/0/Value" in items
	assert not stack.client.has_name(service_name("switch", SWITCH_IN))


# --- analog mode -------------------------------------------------------------

def test_analog_input_has_a_range(stack):
	items = enable(stack, ANALOG_IN)
	assert items["/GenericInput/0/Settings/Type"] == 2			# value indicator with range
	assert items["/GenericInput/0/Settings/RangeMin"] == 0
	assert items["/GenericInput/0/Settings/RangeMax"] == 100
	assert items["/GenericInput/0/Settings/Unit"] == "%"
	assert items["/GenericInput/0/Value"] == pytest.approx(12.5)


def test_analog_input_follows_the_device(stack):
	name = service_name("acload", ANALOG_IN)
	stack.mocks["analog-in"].rpc("Mock.SetInput", id=0, percent=63.5)
	stack.wait_value(name, "/GenericInput/0/Value", pytest.approx(63.5))


# --- count mode --------------------------------------------------------------

def test_count_input_reports_the_total(stack):
	items = enable(stack, COUNT_IN)
	assert items["/GenericInput/0/Settings/Type"] == 1			# value indicator without range
	assert items["/GenericInput/0/Settings/PrimaryLabel"] == "Count"
	assert items["/GenericInput/0/Value"] == 7


def test_count_input_follows_the_device(stack):
	name = service_name("acload", COUNT_IN)
	stack.mocks["count-in"].rpc("Mock.SetInput", id=0, counts=41)
	stack.wait_value(name, "/GenericInput/0/Value", 41)


# --- button mode -------------------------------------------------------------

def test_button_input_has_no_value_only_an_event(stack):
	items = enable(stack, BUTTON_IN)
	# Stateless, so no value is published at all; the GUI draws nothing for it.
	assert "/GenericInput/0/Value" not in items
	assert "/GenericInput/0/Event" in items
	assert items["/GenericInput/0/Settings/Type"] == 0


def test_button_event_toggles_its_low_bit(stack):
	# Bit 0 flips with every new event, bit 1 marks a single tap, so a consumer sees a change
	# even when the same event arrives twice.
	name = service_name("acload", BUTTON_IN)
	mock = stack.mocks["button-in"]
	seen = [stack.client.get_value(name, "/GenericInput/0/Event")]
	for _ in range(3):
		mock.rpc("Input.Trigger", id=0, event_type="single_push")
		stack.wait_for(lambda: stack.client.get_value(name, "/GenericInput/0/Event") != seen[-1],
			"event value to change")
		seen.append(stack.client.get_value(name, "/GenericInput/0/Event"))
	# Every value carries the single-tap bit, and consecutive values differ.
	assert all(v & 0x02 for v in seen[1:]), seen
	assert all(a != b for a, b in zip(seen, seen[1:])), seen


# --- attachment rules --------------------------------------------------------

def test_input_sharing_an_id_attaches_rather_than_taking_a_channel(stack):
	# input:0 shares switch:0's id, so it is added to that channel instead of getting one of
	# its own. input:1 has no channel to share with and is covered in test_input_only.py.
	items = enable(stack, NO_MATCH)
	assert "/GenericInput/0/Value" in items
	assert "/GenericInput/1/Value" not in items
	inventory = stack.client.get_items(SHELLY_SERVICE)
	assert inventory[f"/Devices/{NO_MATCH}/1/Type"] == "switch"
