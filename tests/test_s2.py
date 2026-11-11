"""
S2 resource manager (opportunity load function) on a switch with power
measurement, driven by the CEM from venus-s2-tools (see s2_cem.py).

The driver runs through tests/run_driver.py, which logs when s2python and
shelly_s2_control are actually loaded. The lazy-loading tests rely on that
and on running first: once any channel has loaded S2, it stays loaded.

All mocks are listed in /Settings/Shelly/IpAddresses before the driver starts.
"""
import pytest

from shelly_stack import make_stack, service_name
from s2_cem import Cem, load_cli

LAZY = "aabbccdd3001"
OL = "aabbccdd3002"

APOWER = 1000.0
POWER_SETTING = 1000	# default /S2/0/RmSettings/PowerSetting

OPPORTUNITY_LOAD = 6
MANUAL = 2
THREE_STATE_SWITCH = 9

S2_MODULES = ["s2python", "shelly_s2_control"]

ONE_SWITCH = ["--switch-channels", "1", "--apower", str(APOWER), "--notify-interval", "1"]


def setup(s):
	s.add_mock("lazy", LAZY, *ONE_SWITCH)
	s.add_mock("ol", OL, *ONE_SWITCH)


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
	# Skip before starting anything when the CEM is not available
	load_cli()
	yield from make_stack(tmp_path_factory, "s2", setup, preseed_ips=True,
		driver_env={"SHELLY_TEST_LOG_IMPORTS": ",".join(S2_MODULES)})


def loaded(stack, module):
	return f"TEST-IMPORT: {module}" in stack.driver_log()


def enable_switch(stack, serial):
	stack.wait_reachable(serial)
	stack.set_enabled(serial, 1, 1)
	name = service_name("acload", serial)
	return name, stack.wait_service(name)


def set_function(stack, name, function):
	assert stack.client.set_value(name, "/SwitchableOutput/0/Settings/Function", "i", function) == 0


def set_auto(stack, name, auto):
	assert stack.client.set_value(name, "/SwitchableOutput/0/Auto", "i", auto) == 0


def rm_state(stack, name):
	""" /S2/0/Rm only implements com.victronenergy.S2, not BusItem, so its
	    value is only visible through GetItems on the root. """
	return stack.client.get_items(name).get("/S2/0/Rm")


def wait_rm_ready(stack, name):
	stack.wait_for(lambda: rm_state(stack, name) == "Ready", f"{name}/S2/0/Rm Ready")


def output_on(stack, serial):
	return stack.mocks[serial_to_mock(serial)].rpc("Switch.GetStatus", id=0)["output"]


def serial_to_mock(serial):
	return {LAZY: "lazy", OL: "ol"}[serial]


# Lazy loading

def test_s2_not_loaded_without_opportunity_load(stack):
	name, items = enable_switch(stack, LAZY)
	assert items["/SwitchableOutput/0/Settings/Function"] != OPPORTUNITY_LOAD
	# The function is offered, but S2 support is not loaded for that.
	assert items["/SwitchableOutput/0/Settings/ValidFunctions"] & (1 << OPPORTUNITY_LOAD)
	assert "/S2/0/Rm" not in items
	for module in S2_MODULES:
		assert not loaded(stack, module), f"{module} loaded before any channel uses S2"


def test_s2_loaded_with_opportunity_load(stack):
	name = service_name("acload", LAZY)
	set_function(stack, name, OPPORTUNITY_LOAD)
	wait_rm_ready(stack, name)
	for module in S2_MODULES:
		assert loaded(stack, module)
	assert stack.client.get_value(name, "/SwitchableOutput/0/Settings/Type") == THREE_STATE_SWITCH
	assert stack.client.get_value(name, "/SwitchableOutput/0/Auto") == 0


# Resource manager

@pytest.fixture(scope="module")
def ol_service(stack):
	""" The OL switch, set to the opportunity load function with /Auto on. """
	name, _ = enable_switch(stack, OL)
	set_function(stack, name, OPPORTUNITY_LOAD)
	wait_rm_ready(stack, name)
	set_auto(stack, name, 1)
	stack.wait_value(name, "/SwitchableOutput/0/Auto", 1)
	return name


@pytest.fixture
def cem(stack, ol_service):
	c = Cem(stack.env["DBUS_SESSION_BUS_ADDRESS"], ol_service)
	yield c
	c.close()
	# Leave the output off for the next test. Closing the CEM already does that
	# when OMBC was active (see test_cem_disconnect_turns_output_off).
	stack.wait_for(lambda: not output_on(stack, OL), "OL output off after the test")


def test_rm_details(cem):
	cli = cem.cli
	details = cem.rm_details
	assert set(details.available_control_types) == {
		cli.ControlType.OPERATION_MODE_BASED_CONTROL, cli.ControlType.NOT_CONTROLABLE}
	assert details.manufacturer == "Shelly"
	assert details.serial_number == OL
	assert [q.value for q in details.provides_power_measurement_types] == ["ELECTRIC.POWER.L1"]


def test_auto_off_offers_no_control_only(stack, ol_service):
	set_auto(stack, ol_service, 0)
	try:
		c = Cem(stack.env["DBUS_SESSION_BUS_ADDRESS"], ol_service)
		try:
			assert c.rm_details.available_control_types == [c.cli.ControlType.NOT_CONTROLABLE]
		finally:
			c.close()
	finally:
		set_auto(stack, ol_service, 1)
		stack.wait_value(ol_service, "/SwitchableOutput/0/Auto", 1)


def test_select_ombc(stack, ol_service, cem):
	cem.select_ombc()
	stack.wait_value(ol_service, "/S2/0/Active", 1)
	assert cem.active_mode_label() == "Off"
	on = cem.ombc_mode("On")
	assert on.power_ranges[0].start_of_range == POWER_SETTING
	assert on.power_ranges[0].end_of_range == POWER_SETTING
	off = cem.ombc_mode("Off")
	assert off.power_ranges[0].start_of_range == 0
	assert off.power_ranges[0].end_of_range == 0


def test_start_stop(stack, ol_service, cem):
	cli = cem.cli
	cem.select_ombc()

	assert cem.send_instruction("On").status == cli.ReceptionStatusValues.OK
	stack.wait_for(lambda: output_on(stack, OL), "mock output on")
	stack.wait_value(ol_service, "/SwitchableOutput/0/State", 1)
	stack.wait_for(lambda: cem.active_mode_label() == "On", "OMBC.Status On")

	assert cem.send_instruction("Off").status == cli.ReceptionStatusValues.OK
	stack.wait_for(lambda: not output_on(stack, OL), "mock output off")
	stack.wait_value(ol_service, "/SwitchableOutput/0/State", 0)
	stack.wait_for(lambda: cem.active_mode_label() == "Off", "OMBC.Status Off")


def test_power_measurements(stack, ol_service, cem):
	cem.select_ombc()
	stack.wait_for(lambda: cem.power == 0, "0W power measurement while off")
	assert cem.power_commodity.value == "ELECTRIC.POWER.L1"

	cem.send_instruction("On")
	stack.wait_for(lambda: cem.power == APOWER, f"{APOWER}W power measurement")

	mock = stack.mocks["ol"]
	mock.rpc("Mock.SetPower", apower=1500)
	try:
		stack.wait_for(lambda: cem.power == 1500, "power measurement to follow the device")
	finally:
		mock.rpc("Mock.SetPower", apower=APOWER)

	cem.send_instruction("Off")
	stack.wait_for(lambda: cem.power == 0, "0W power measurement after switching off")


def test_cem_disconnect_turns_output_off(stack, ol_service, cem):
	cem.select_ombc()
	cem.send_instruction("On")
	stack.wait_value(ol_service, "/SwitchableOutput/0/State", 1)

	cem.disconnect()
	stack.wait_value(ol_service, "/S2/0/Active", 0)
	stack.wait_value(ol_service, "/SwitchableOutput/0/State", 0)
	stack.wait_for(lambda: not output_on(stack, OL), "mock output off")


def test_leaving_opportunity_load_sends_zero_power(stack, ol_service, cem):
	""" Runs last: it switches the channel back to manual. """
	cem.select_ombc()
	cem.send_instruction("On")
	stack.wait_for(lambda: cem.power == APOWER, f"{APOWER}W power measurement")

	set_function(stack, ol_service, MANUAL)
	stack.wait_for(lambda: cem.power == 0, "0W power measurement when leaving the opportunity load function")
	stack.wait_for(lambda: rm_state(stack, ol_service) is None, "RM no longer ready")
	assert stack.client.get_value(ol_service, "/SwitchableOutput/0/Settings/Type") != THREE_STATE_SWITCH
