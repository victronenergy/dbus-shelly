"""
Resource cleanup: a channel must not leave anything behind on D-Bus when it stops.

Every ShellyChannel opens its own D-Bus connection (ShellyChannel.create), and the
localsettings client is created on that same connection. Stopping the channel closes
the aiovelib Service, which unexports the objects and releases the well-known name --
but a released name is not a closed connection. These tests count the connections on
the private bus, so a connection that outlives its channel shows up as a leak.

Channels are stopped on every disable, on every role change (the channel is restarted
so the service type can change) and whenever a device is torn down, so anything leaked
here accumulates for the lifetime of the driver process.
"""
import pytest

from shelly_stack import make_stack, service_name

DISABLE_CYCLE = "aabbccdd5001"
ROLE_CYCLE = "aabbccdd5002"
TWO_CHANNEL = "aabbccdd5003"

ONE_SWITCH = ["--switch-channels", "1"]
TWO_SWITCHES = ["--switch-channels", "2"]
EM1_ONLY = ["--switch-channels", "0", "--em1-channels", "1"]

# How many enable/disable cycles to run. One cycle is enough to show a leak; several
# make it unambiguous that it accumulates rather than being a one-off offset.
CYCLES = 4


def setup(s):
	s.add_mock("disable-cycle", DISABLE_CYCLE, *ONE_SWITCH)
	s.add_mock("role-cycle", ROLE_CYCLE, *EM1_ONLY)
	s.add_mock("two-channel", TWO_CHANNEL, *TWO_SWITCHES)


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
	yield from make_stack(tmp_path_factory, "leaks", setup, preseed_ips=True)


def _enable(stack, serial, index=1):
	stack.set_enabled(serial, index, 1)


def _disable(stack, serial, index=1):
	stack.set_enabled(serial, index, 0)


def test_channel_disable_releases_its_dbus_connection(stack):
	""" One enable/disable cycle must return the bus to the state it started in. """
	stack.wait_for(lambda: stack.device(DISABLE_CYCLE, "Reachable") == 1, "device reachable")
	before = stack.client.connection_count()

	_enable(stack, DISABLE_CYCLE)
	name = service_name("acload", DISABLE_CYCLE)
	stack.wait_service(name)
	with_channel = stack.client.connection_count()
	assert with_channel > before, "enabling a channel should open at least one connection"

	_disable(stack, DISABLE_CYCLE)
	stack.wait_service_gone(name)
	# The name is gone; the connection must be gone too.
	stack.wait_for(lambda: stack.client.connection_count() == before,
		f"connection count back to {before} (was {with_channel} with the channel up)")


def test_repeated_enable_disable_does_not_accumulate_connections(stack):
	""" The actual regression guard: N cycles must not cost N connections. """
	stack.wait_for(lambda: stack.device(DISABLE_CYCLE, "Reachable") == 1, "device reachable")
	name = service_name("acload", DISABLE_CYCLE)
	baseline = stack.client.connection_count()

	for _ in range(CYCLES):
		_enable(stack, DISABLE_CYCLE)
		stack.wait_service(name)
		_disable(stack, DISABLE_CYCLE)
		stack.wait_service_gone(name)

	stack.wait_for(lambda: stack.client.connection_count() == baseline,
		f"connection count back to {baseline} after {CYCLES} enable/disable cycles")


def test_role_change_does_not_leak(stack):
	""" A role change restarts the channel to swap the service type, so it stops a
	    channel without the user disabling anything. """
	_enable(stack, ROLE_CYCLE)
	acload = service_name("acload", ROLE_CYCLE)
	genset = service_name("genset", ROLE_CYCLE)
	stack.wait_service(acload)
	baseline = stack.client.connection_count()

	for target, old in ((genset, acload), (acload, genset)):
		role = target.split(".")[2]
		assert stack.client.set_value(old, "/Role", "s", role) == 0
		stack.wait_service_gone(old)
		stack.wait_service(target)

	stack.wait_for(lambda: stack.client.connection_count() == baseline,
		f"connection count back to {baseline} after two role changes")


def test_sibling_channel_survives_a_channel_stop(stack):
	""" Guards the assumption the fix relies on: each channel owns its own connection,
	    so disconnecting one must not disturb another. """
	for index in (1, 2):
		_enable(stack, TWO_CHANNEL, index)
	names = [service_name("acload", TWO_CHANNEL, i) for i in (0, 1)]
	for name in names:
		stack.wait_service(name)

	_disable(stack, TWO_CHANNEL, 1)
	stack.wait_service_gone(names[0])

	# The surviving channel must still answer reads and writes.
	assert stack.client.get_value(names[1], "/Connected") == 1
	assert stack.client.set_value(names[1], "/SwitchableOutput/1/State", "i", 1) == 0
	stack.wait_for(lambda: stack.mocks["two-channel"].rpc("Switch.GetStatus", id=1)["output"] is True,
		"sibling channel still switches")
