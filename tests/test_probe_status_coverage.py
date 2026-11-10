"""
Every ProbeStatus must be acted on.

ProbeStatus.ERROR was produced in two places and matched by nothing, so a probe that returned
it left the endpoint's ConnectMeta untouched: no mark_success, no mark_failure, no
stop_connect. The endpoint therefore stayed due and was re-probed on the next heartbeat
without the attempt being counted, so the backoff never advanced for it.

test_probe_status_error.py drives that scenario end to end. This file guards the wider class
of bug -- a status with no consumer -- and reads the source rather than the behaviour,
deliberately: it keeps working if the dispatch is restructured, and it fails if a future
status is added without being handled anywhere.
"""
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _source():
	with open(os.path.join(REPO, "discovery.py")) as f:
		return f.read()


def _declared_statuses(src):
	block = re.search(r"class ProbeStatus\(str, Enum\):(.*?)\n\n", src, re.S)
	assert block, "ProbeStatus enum not found"
	return set(re.findall(r"^\t([A-Z_]+) = ", block.group(1), re.M))


def test_probe_status_enum_is_as_expected():
	""" Pins the set, so adding one is a deliberate act that updates this test too. """
	assert _declared_statuses(_source()) == {
		"REACHABLE", "RECONNECTING", "UNSUPPORTED", "UNREACHABLE", "ERROR"}


def test_every_probe_status_is_handled_in_attempt_connect():
	""" _attempt_connect decides what happens to the retry state for each outcome. A status it
	    does not mention leaves ConnectMeta untouched, which is the bug this guards. """
	src = _source()
	start = src.index("async def _attempt_connect")
	end = src.index("class ManualIpDiscovery")
	body = src[start:end]
	unhandled = sorted(s for s in _declared_statuses(src) if f"ProbeStatus.{s}" not in body)
	assert not unhandled, (
		f"these probe statuses are produced but never acted on in _attempt_connect: {unhandled}. "
		"A status that is not mentioned there leaves the endpoint's ConnectMeta unchanged, so it "
		"stays due and is re-probed every heartbeat without counting the attempt.")
