"""
Devices found over mDNS are announced by hostname (e.g. shellyplus1pm-<mac>.local).
The driver resolves the hostname and lists the device's IP address in /Ip.

The driver runs with a fake mDNS browser (see tests/run_driver.py). Mocks are
announced as localhost:<port>, which resolves to 127.0.0.1.
"""
import pytest

from shelly_stack import make_stack

SWITCH = "aabbccdd4001"


def setup(s):
	s.add_mock("switch", SWITCH)


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
	yield from make_stack(tmp_path_factory, "mdns", setup, fake_mdns=True)


def test_ip_listed_for_hostname(stack):
	mock = stack.mocks["switch"]
	stack.announce(mock, hostname="localhost")
	stack.wait_reachable(SWITCH)
	assert stack.device(SWITCH, "Ip") == f"127.0.0.1:{mock.port}"
