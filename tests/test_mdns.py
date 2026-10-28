"""
Devices found over mDNS are announced by hostname (e.g. shellyplus1pm-<mac>.local).
The driver resolves the hostname and lists the device's IP address in /Ip.

The driver runs with a fake mDNS browser (see tests/run_driver.py). Mocks are
announced as localhost:<port>, which resolves to 127.0.0.1.
"""
import pytest

from shelly_stack import make_stack

SWITCH = "aabbccdd4001"
PROTECTED = "aabbccdd4002"

STATUS_READY = 1
STATUS_PASSWORD_REQUIRED = -2


def setup(s):
	s.add_mock("switch", SWITCH)
	s.add_mock("protected", PROTECTED, "--password", "secret")


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
	yield from make_stack(tmp_path_factory, "mdns", setup, fake_mdns=True)


def test_ip_listed_for_hostname(stack):
	mock = stack.mocks["switch"]
	stack.announce(mock, hostname="localhost")
	stack.wait_connection_status(SWITCH, STATUS_READY)
	assert stack.device(SWITCH, "Ip") == f"127.0.0.1:{mock.port}"


def test_ip_listed_for_password_protected_device(stack):
	# The connection is closed after the failed login, the IP address is still listed.
	mock = stack.mocks["protected"]
	stack.announce(mock, hostname="localhost")
	stack.wait_connection_status(PROTECTED, STATUS_PASSWORD_REQUIRED)
	assert stack.device(PROTECTED, "Ip") == f"127.0.0.1:{mock.port}"
