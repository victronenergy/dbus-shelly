#!/usr/bin/env python3
import argparse
import json
import os
import signal
import subprocess
import sys
import time


def parse_args():
	parser = argparse.ArgumentParser(description="Run multiple mock Shelly devices.")
	parser.add_argument(
		"--config",
		default=os.path.join(os.path.dirname(__file__), "mock_devices.json"),
		help="Path to mock devices JSON config.",
	)
	parser.add_argument("--verbose", action="store_true", help="Enable mock server logs.")
	return parser.parse_args()


def load_config(path):
	with open(path, "r", encoding="utf-8") as f:
		return json.load(f)


def build_cmd(script_path, device, verbose):
	cmd = [
		sys.executable,
		script_path,
		"--port",
		str(device["port"]),
		"--name",
		device["name"],
		"--model",
		device.get("model", "SHELLY-PLUS-MOCK"),
		"--app",
		device.get("app", "MockSwitchEM"),
		"--mac",
		device["mac"],
		"--switch-channels",
		str(device.get("switch_channels", 1)),
		"--apower",
		str(device.get("apower", 1000.0)),
	]
	if verbose:
		cmd.append("--verbose")
	return cmd


def terminate_all(procs):
	for p in procs:
		if p.poll() is None:
			p.terminate()
	time.sleep(0.5)
	for p in procs:
		if p.poll() is None:
			p.kill()

def _dbus_get_ipaddresses():
	try:
		out = subprocess.check_output(
			["dbus", "-y", "com.victronenergy.shelly", "/IpAddresses", "GetValue"],
			text=True,
			stderr=subprocess.DEVNULL,
		)
		return out.strip().strip("'")
	except Exception:
		return None

def _dbus_set_ipaddresses(value):
	subprocess.check_call(
		["dbus", "-y", "com.victronenergy.shelly", "/IpAddresses", "SetValue", value],
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
	)

def _dbus_refresh():
	subprocess.check_call(
		["dbus", "-y", "com.victronenergy.shelly", "/Refresh", "SetValue", "1"],
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
	)

def _wait_for_shelly_service(timeout_s=20):
	deadline = time.time() + timeout_s
	while time.time() < deadline:
		if _dbus_get_ipaddresses() is not None:
			return True
		time.sleep(0.5)
	return False


def main():
	args = parse_args()
	config = load_config(args.config)
	devices = config.get("devices", [])
	if not devices:
		print("No devices found in config.")
		return 1

	script_path = os.path.join(os.path.dirname(__file__), "mock_shelly_server.py")
	procs = []

	for device in devices:
		cmd = build_cmd(script_path, device, args.verbose)
		print(f"Starting {device['name']} on port {device['port']}...")
		procs.append(subprocess.Popen(cmd))

	ip_list = ",".join([f"127.0.0.1:{d['port']}" for d in devices])
	if _wait_for_shelly_service():
		current = _dbus_get_ipaddresses() or ""
		current_set = {v for v in current.split(",") if v}
		desired_set = set(ip_list.split(","))
		merged = ",".join(sorted(current_set | desired_set))
		if merged != current:
			_dbus_set_ipaddresses(merged)
			print("Updated /IpAddresses to:", merged)
		else:
			print("IpAddresses already contains mock devices.")
		time.sleep(1)
		_dbus_refresh()
		print("Triggered /Refresh")
	else:
		print("Shelly dbus service not available; set /IpAddresses manually:", ip_list)

	def _handle_signal(signum, _frame):
		print(f"Received signal {signum}, stopping mocks...")
		terminate_all(procs)
		sys.exit(0)

	signal.signal(signal.SIGINT, _handle_signal)
	signal.signal(signal.SIGTERM, _handle_signal)

	try:
		while True:
			for p in procs:
				if p.poll() is not None:
					raise RuntimeError("A mock server exited unexpectedly.")
			time.sleep(1)
	except KeyboardInterrupt:
		pass
	finally:
		terminate_all(procs)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
