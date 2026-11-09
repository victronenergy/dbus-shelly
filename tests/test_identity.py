"""
Device identity: the inventory is keyed by serial, which is the device's mac.

An endpoint starts keyed by host and is promoted to a serial key once a probe reads the mac.
`promote_endpoint_to_serial` merges into an existing serial-keyed entry rather than adding a
second one, which is what stops the same device appearing twice when it is both entered
manually by IP and announced over mDNS.

The flip side is that two physically distinct devices reporting the same mac are
indistinguishable here, because the mac is the only identity the protocol offers. That is
covered below as a documented limitation, not as something the driver can fix.
"""
import time

import pytest

from shelly_stack import make_stack, service_name, SHELLY_SERVICE

BOTH_SOURCES = "aabbccdd9501"
CLONE = "aabbccdd9502"

ONE_SWITCH = ["--switch-channels", "1"]


def setup(s):
	s.add_mock("both-sources", BOTH_SOURCES, *ONE_SWITCH)
	# Two separate processes on two ports, deliberately reporting the same mac.
	s.add_mock("clone-a", CLONE, *ONE_SWITCH)
	s.add_mock("clone-b", CLONE, *ONE_SWITCH)


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
	# fake_mdns lets announce() simulate an mDNS announcement; preseed_ips adds every mock to
	# /IpAddresses as well, so each device is reachable from both sources at once.
	yield from make_stack(tmp_path_factory, "identity", setup, preseed_ips=True, fake_mdns=True)


def _entries(stack, serial):
	inv = stack.client.get_items(SHELLY_SERVICE)
	return sorted({k.split("/")[2] for k in inv if k.startswith(f"/Devices/{serial}")})


def test_one_device_from_manual_ip_and_mdns_is_listed_once(stack):
	""" The same device entered by IP and announced over mDNS must produce one entry, not two. """
	stack.wait_for(lambda: stack.device(BOTH_SOURCES, "Reachable") == 1, "reachable via manual IP")
	assert _entries(stack, BOTH_SOURCES) == [BOTH_SOURCES]

	# Now announce the very same device over mDNS as well.
	stack.announce(stack.mocks["both-sources"])
	time.sleep(8)

	assert _entries(stack, BOTH_SOURCES) == [BOTH_SOURCES]
	inv = stack.client.get_items(SHELLY_SERVICE)
	# Exactly one channel, not one per discovery source.
	chans = sorted(k for k in inv if k.startswith(f"/Devices/{BOTH_SOURCES}/") and k.endswith("/Type"))
	assert chans == [f"/Devices/{BOTH_SOURCES}/1/Type"], chans
	# And the entry records whichever source last reached it, without duplicating the device.
	assert stack.device(BOTH_SOURCES, "DiscoveryType") in ("Manual", "mDNS")


def test_enabling_the_channel_registers_a_single_service(stack):
	""" A merged entry must not end up with two services for one physical device. """
	stack.set_enabled(BOTH_SOURCES, 1, 1)
	name = service_name("acload", BOTH_SOURCES)
	stack.wait_service(name)
	for kind in ("switch", "pvinverter", "genset", "heatpump"):
		assert not stack.client.has_name(service_name(kind, BOTH_SOURCES)), kind


def test_two_devices_claiming_the_same_mac_collapse_into_one_entry(stack):
	""" Documented limitation: the mac is the only identity available, so two devices that
	    report the same one cannot be told apart. They merge, and the recorded host is
	    whichever answered last. The driver must at least stay stable and not duplicate. """
	stack.wait_for(lambda: stack.device(CLONE, "Reachable") == 1, "a clone is reachable", timeout=90)
	assert _entries(stack, CLONE) == [CLONE]

	recorded = stack.device(CLONE, "Ip")
	endpoints = {stack.mocks["clone-a"].endpoint, stack.mocks["clone-b"].endpoint}
	assert recorded in endpoints, (recorded, endpoints)

	# It stays a single entry rather than oscillating into two.
	stack.hold(lambda: _entries(stack, CLONE) == [CLONE], "one entry for the two clones", 10)
