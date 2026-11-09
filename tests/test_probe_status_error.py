"""
ProbeStatus.ERROR, reproduced rather than reasoned about.

Two routes lead to ERROR. The explicit one is connect() returning True while the device is
no longer connected by the time _get_device_info checks, i.e. the websocket dropped in
between. The implicit one is DeviceProbeResult's default: any exception raised before a
status is assigned returns ERROR.

This drives the explicit route, deterministically. Shelly.GetComponents is the last RPC of
ShellyDevice.connect(): what follows it, _get_channels_info_from_components, works off the
config already embedded in that response and makes no further call. So a websocket closed
right after it is answered leaves connect() with everything it needs - it returns True - while
the check on the very next line sees a disconnected device.

The mock's --ws-close-after-method does exactly that. Anchoring on the method rather than on a
frame count matters: a count is timing- and version-dependent and silently stops hitting the
window.

Before the fix, ERROR was matched by nothing, so ConnectMeta was left untouched: no attempt
counted, next_retry_at unmoved, the endpoint still due and re-probed on the next heartbeat.
"""
import os
import time

import pytest

from shelly_stack import make_stack, SHELLY_SERVICE

WS_DROP = "aabbccdd9801"

# The last RPC of connect(). Everything after it is computed from that response.
LAST_RPC_OF_CONNECT = "Shelly.GetComponents"


def setup(s):
	s.add_mock("ws-drop", WS_DROP, "--switch-channels", "1",
		"--ws-close-after-method", LAST_RPC_OF_CONNECT)


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
	yield from make_stack(tmp_path_factory, "probe_error", setup, preseed_ips=True)


def _driver_log(stack):
	with open(os.path.join(stack.logdir, "driver.log")) as f:
		return f.read()


def test_the_error_window_is_reachable_and_reported(stack):
	""" The ERROR branch is entered and says so. If a later RPC is ever added to connect()
	    after Shelly.GetComponents, this fails loudly rather than silently testing nothing. """
	stack.wait_for(lambda: "did not complete, retrying in" in _driver_log(stack),
		"the ERROR branch to be entered and logged", timeout=90)


def test_a_failed_probe_is_retried_with_backoff_not_every_heartbeat(stack):
	""" The consequence of the fix: the attempt has to be counted, so successive retries are
	    spaced by the doubling backoff instead of running at the 10 second heartbeat. The log
	    line reports the delay the driver picked, and because every retry hits the same drop
	    the delays must keep growing. """
	import re
	pattern = r"Probe of shelly device \S+ at \S+ did not complete, retrying in (\d+) seconds"
	stack.wait_for(lambda: len(re.findall(pattern, _driver_log(stack))) >= 3,
		"three backoff decisions for the dropped probe", timeout=120)
	delays = [int(d) for d in re.findall(pattern, _driver_log(stack))]
	# Without the fix nothing is logged here at all: the branch does not exist, the attempt is
	# not counted, and the endpoint simply stays due.
	assert delays[0] >= 10, delays
	assert delays[:3] == [10, 20, 40], delays
