"""
Without s2python, the driver still runs and switches work, but the
opportunity load function is not offered.

The driver runs through tests/run_driver.py, which makes s2python look not
installed.
"""
import pytest

from shelly_stack import make_stack, service_name

SWITCH = "aabbccdd3101"

OPPORTUNITY_LOAD = 6


def setup(s):
	s.add_mock("switch", SWITCH, "--switch-channels", "1")


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
	yield from make_stack(tmp_path_factory, "s2-unavailable", setup, preseed_ips=True,
		driver_env={"SHELLY_TEST_BLOCK_IMPORTS": "s2python"})


def test_no_opportunity_load_without_s2python(stack):
	stack.wait_reachable(SWITCH)
	stack.set_enabled(SWITCH, 1, 1)
	name = service_name("acload", SWITCH)
	items = stack.wait_service(name)
	assert "opportunity loads function won't be available" in stack.driver_log()
	assert not items["/SwitchableOutput/0/Settings/ValidFunctions"] & (1 << OPPORTUNITY_LOAD)
	assert "/S2/0/Rm" not in items

	# The switch itself still works
	assert stack.client.set_value(name, "/SwitchableOutput/0/State", "i", 1) == 0
	stack.wait_for(lambda: stack.mocks["switch"].rpc("Switch.GetStatus", id=0)["output"], "mock output on")
