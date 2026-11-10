"""
A channel whose handlers fail to initialise must be removed, and must recover on its own.

_init_channel_and_handlers calls stop_channel when a handler's ainit() returns False, and that
teardown can raise: set_service_name runs from the handlers' ainit, so a channel whose handlers
failed never got a service name, and aiovelib's Service.close() finishes with
release_name(self.name), which raises on None. The raise lands before stop_channel's
"del self._channels[ch]".

What is left is a channel entry whose service has had every item unexported. start_channel
returns early for a channel already in _channels, so the driver's own channel-enable retry
reports success without ever registering a service, and every reconnect writes /Connected to
that emptied service and raises KeyError.

Two things get a channel into that state, and both were seen on a GX with a Shelly H&T Gen3,
whose temperature and humidity channels are enabled by default since v2.20 because the device
is sleepy: a handler's ainit() returning False, and the init task being cancelled before it
finishes. What they share, and what matters here, is that the channel is torn down before
set_service_name ran.

This drives the first of the two, because it is the one a mock can produce: --fault-methods
makes Temperature.GetStatus answer with an RPC error. It has to be a startup argument rather
than the Mock.SetFaults runtime hook, since the channel is enabled by default and is already
being started before a test could issue an RPC.
"""
import os

import pytest

from shelly_stack import make_stack, service_name, SHELLY_SERVICE

SLEEPY = "aabbccdd9701"
# A sleepy device with one temperature component and no switch: one channel, index 1, enabled
# by default because the device is battery powered.
TEMP_CHANNEL = 1
TEMP_ID = 0


def setup(s):
	s.add_mock("sleepy", SLEEPY, "--switch-channels", "0", "--temperature-ids", str(TEMP_ID),
		"--devicepower", "--fault-methods", "Temperature.GetStatus")


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
	yield from make_stack(tmp_path_factory, "channel_init_failure", setup, preseed_ips=True)


def _log(stack):
	with open(os.path.join(stack.logdir, "driver.log")) as f:
		return f.read()


def test_the_channel_is_enabled_by_default_and_its_init_fails(stack):
	""" Establishes the starting conditions, so a later change that stops the channel from
	    being auto-enabled, or stops the init from failing, fails here instead of silently
	    making the rest of this file vacuous. """
	stack.wait_reachable(SLEEPY)
	assert stack.device(SLEEPY, f"{TEMP_CHANNEL}/Type") == "input"
	assert stack.enabled_setting(SLEEPY, TEMP_CHANNEL) == 1
	stack.wait_for(lambda: "Failed to initialize channel" in _log(stack),
		"the channel init to fail", timeout=60)


def test_the_channel_recovers_once_the_component_answers_again(stack):
	""" The point of removing the entry. With a channel left behind, start_channel returns
	    early and the retry reports success without registering anything, so the device never
	    appears however long you wait. """
	stack.mocks["sleepy"].rpc("Mock.SetFaults", methods=[])
	name = service_name("temperature", SLEEPY, TEMP_ID)
	# The channel-enable retry backs off from 10 s and doubles, so allow several rounds.
	stack.wait_for(lambda: stack.client.has_name(name), name, timeout=300)
	stack.wait_for(lambda: stack.client.get_value(name, "/Connected") == 1,
		f"{name} connected", timeout=60)
	assert stack.client.get_value(name, "/Temperature") is not None


def test_the_failed_teardown_left_nothing_broken_behind(stack):
	""" The two symptoms the leftover entry produced. """
	log = _log(stack)
	assert "KeyError: '/Connected'" not in log
	assert "Error in channel operation worker" not in log
