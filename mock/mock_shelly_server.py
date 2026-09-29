#!/usr/bin/env python3
import argparse
import asyncio
import json
import time
from aiohttp import web


class MockShellyDevice:
	def __init__(
		self,
		device_name,
		channel_name,
		model,
		app,
		mac,
		switch_channels=1,
		rgbcct_channels=0,
		voltage=230.0,
		apower=120.0,
		pf=0.98,
		smoke=False,
		flood=False,
		battery_percent=100.0,
		battery_voltage=3.0,
		em=False,
		em1_channels=0,
		pm1_channels=0,
		light_channels=0,
		rgb=False,
		cct=False,
	):
		self.name = device_name
		self.channel_name = channel_name
		self.model = model
		self.app = app
		self.mac = mac.lower()
		self.switch_channels = switch_channels
		self.voltage = float(voltage)
		self.default_apower = float(apower)
		self.pf = float(pf)
		self.smoke = smoke
		self._smoke_alarm = False
		self._smoke_mute = False
		self.flood = flood
		self._flood_alarm = False
		self._flood_mute = False
		self._battery_percent = float(battery_percent)
		self._battery_voltage = float(battery_voltage)
		self._start = time.time()

		self._switch_outputs = [False] * switch_channels
		self._switch_names = [(channel_name or None) if i == 0 else f"Channel {i + 1}" for i in range(switch_channels)]
		self._energy_total = [0.0] * switch_channels
		self._ret_energy_total = [0.0] * switch_channels
		self._last_energy_update = time.time()

		# Energy meters report fixed forward and returned energy counters (Wh), so tests can assert exact values.
		self.em = em
		self.em1_channels = em1_channels
		self.pm1_channels = pm1_channels
		self.energy_wh = 12345.0
		self.ret_energy_wh = 2345.0

		# Dimmable / color outputs, keyed by RPC component key (e.g. "light:0").
		self._lights = {}
		for i in range(light_channels):
			self._lights[f"light:{i}"] = {"output": False, "brightness": 50}
		if rgb:
			self._lights["rgb:0"] = {"output": False, "brightness": 50, "rgb": [255, 0, 0]}
		if cct:
			self._lights["cct:0"] = {"output": False, "brightness": 50, "ct": 3000}

		# Meter channel names, keyed by component key. Id 0 defaults to channel_name.
		self._meter_names = {}

		# Test hooks: methods that return an error, and a log of (time, method) of calls over websocket.
		self.faults = set()
		self.fault_mode = "error"
		self.fault_delay = 0.0
		# Cleared while the device is frozen (see mock_freeze).
		self.running = asyncio.Event()
		self.running.set()
		self.call_log = []
		self.rgbcct_channels = rgbcct_channels
		self._rgbcct = [
			{"output": False, "brightness": 50, "rgb": [255, 255, 255], "ct": 4000, "mode": "rgb"}
			for _ in range(rgbcct_channels)
		]
		self._rgbcct_names = [None] * rgbcct_channels

		self._ws_clients = set()
		self._notify_task = None

	@property
	def device_id(self):
		return f"shellyplus-{self.mac}"

	def _update_energy(self):
		now = time.time()
		dt = max(0.0, now - self._last_energy_update)
		self._last_energy_update = now
		for i in range(self.switch_channels):
			if self._switch_outputs[i]:
				# Wh = W * h
				self._energy_total[i] += (self.default_apower * dt) / 3600.0

	def shelly_info(self):
		return {
			"name": self.name,
			"id": self.device_id,
			"mac": self.mac,
			"model": self.model,
			"app": self.app,
			"gen": 2,
			"fw_id": "20240101_1234",
			"ver": "1.0.0",
			"auth": False,
			"auth_en": False,
			"auth_domain": self.device_id,
		}

	def shelly_get_device_info(self):
		return {
			"name": self.name,
			"id": self.device_id,
			"mac": self.mac,
			"model": self.model,
			"app": self.app,
			"gen": 2,
			"fw_id": "20240101_1234",
			"ver": "1.0.0",
			"auth_en": False,
			"auth_domain": self.device_id,
			"profile": "switch",
		}

	def _switch_status(self, channel):
		self._update_energy()
		output = self._switch_outputs[channel]
		apower = self.default_apower if output else 0.0
		current = apower / self.voltage if self.voltage else 0.0
		return {
			"id": channel,
			"output": output,
			"apower": apower,
			"voltage": self.voltage,
			"current": current,
			"pf": self.pf,
			"aenergy": {"total": self._energy_total[channel]},
			"ret_aenergy": {"total": self._ret_energy_total[channel]},
		}

	def shelly_get_status(self):
		status = {
			"sys": {
				"time": int(time.time()),
				"uptime": int(time.time() - self._start),
			}
		}
		for i in range(self.switch_channels):
			status[f"switch:{i}"] = self._switch_status(i)
		for i in range(self.rgbcct_channels):
			status[f"rgbcct:{i}"] = self._rgbcct_status(i)
		if self.smoke:
			status["smoke:0"] = self.smoke_get_status({"id": 0})
			status["devicepower:0"] = self.devicepower_get_status({"id": 0})
		if self.flood:
			status["flood:0"] = self.flood_get_status({"id": 0})
			status["devicepower:0"] = self.devicepower_get_status({"id": 0})
		if self.em:
			status["em:0"] = self.em_get_status({"id": 0})
			status["emdata:0"] = self.emdata_get_status({"id": 0})
		for i in range(self.em1_channels):
			status[f"em1:{i}"] = self.em1_get_status({"id": i})
			status[f"em1data:{i}"] = self.em1data_get_status({"id": i})
		for i in range(self.pm1_channels):
			status[f"pm1:{i}"] = self.pm1_get_status({"id": i})
		for key in self._lights:
			kind, _, i = key.partition(":")
			status[key] = self.light_get_status(kind, {"id": int(i)})
		return status

	def shelly_get_config(self):
		return {
			"device": {"name": self.name, "profile": "switch"},
			"sys": {"device": {"name": self.name}},
		}

	def shelly_get_components(self, params):
		components = []
		for i in range(self.switch_channels):
			components.append({"key": f"switch:{i}", "status": self._switch_status(i), "config": self.switch_get_config({"id": i})})
		for i in range(self.rgbcct_channels):
			components.append({"key": f"rgbcct:{i}", "status": self._rgbcct_status(i), "config": self.rgbcct_get_config({"id": i})})
		if self.smoke:
			components.append({"key": "smoke:0", "status": self.smoke_get_status({"id": 0}), "config": self.smoke_get_config({"id": 0})})
		if self.flood:
			components.append({"key": "flood:0", "status": self.flood_get_status({"id": 0}), "config": self.flood_get_config({"id": 0})})
		if self.smoke or self.flood:
			components.append({"key": "devicepower:0", "status": self.devicepower_get_status({"id": 0}), "config": self.devicepower_get_config({"id": 0})})
		if self.em:
			components.append({"key": "em:0", "status": self.em_get_status({"id": 0}), "config": self.meter_get_config("em", {"id": 0})})
			components.append({"key": "emdata:0", "status": self.emdata_get_status({"id": 0}), "config": {"id": 0}})
		for i in range(self.em1_channels):
			components.append({"key": f"em1:{i}", "status": self.em1_get_status({"id": i}), "config": self.meter_get_config("em1", {"id": i})})
			components.append({"key": f"em1data:{i}", "status": self.em1data_get_status({"id": i}), "config": {"id": i}})
		for i in range(self.pm1_channels):
			components.append({"key": f"pm1:{i}", "status": self.pm1_get_status({"id": i}), "config": self.meter_get_config("pm1", {"id": i})})
		for key in self._lights:
			kind, _, i = key.partition(":")
			components.append({"key": key, "status": self.light_get_status(kind, {"id": int(i)}), "config": self.light_get_config(kind, {"id": int(i)})})
		offset = int(params.get("offset", 0))
		return {"components": components[offset:], "cfg_rev": 1, "offset": offset, "total": len(components)}

	def sys_get_config(self):
		return {"device": {"name": self.name}}

	def sys_set_config(self, params):
		name = params.get("config", {}).get("device", {}).get("name")
		if name:
			self.name = name
		return {"device": {"name": self.name}}

	def switch_get_status(self, params):
		channel = int(params.get("id", 0))
		if channel < 0 or channel >= self.switch_channels:
			return None
		return self._switch_status(channel)

	def switch_get_config(self, params):
		channel = int(params.get("id", 0))
		if channel < 0 or channel >= self.switch_channels:
			return None
		return {"id": channel, "name": self._switch_names[channel]}

	def switch_set_config(self, params):
		channel = int(params.get("id", 0))
		name = params.get("config", {}).get("name")
		if 0 <= channel < self.switch_channels and name:
			self._switch_names[channel] = name
			self._emit_event(f"switch:{channel}", "config_changed")
		return {"id": channel, "name": self._switch_names[channel]}

	def switch_set(self, params):
		channel = int(params.get("id", 0))
		on = bool(params.get("on", False))
		if 0 <= channel < self.switch_channels:
			self._switch_outputs[channel] = on
			# Push an immediate status update to websocket clients.
			try:
				loop = asyncio.get_running_loop()
				loop.create_task(self.notify_full_status())
			except RuntimeError:
				pass
		return {"id": channel, "on": self._switch_outputs[channel]}

	def _rgbcct_status(self, channel):
		return {"id": channel, **self._rgbcct[channel]}

	def rgbcct_get_status(self, params):
		channel = int(params.get("id", 0))
		if channel < 0 or channel >= self.rgbcct_channels:
			return None
		return self._rgbcct_status(channel)

	def rgbcct_get_config(self, params):
		channel = int(params.get("id", 0))
		if channel < 0 or channel >= self.rgbcct_channels:
			return None
		return {"id": channel, "name": self._rgbcct_names[channel]}

	def rgbcct_set_config(self, params):
		channel = int(params.get("id", 0))
		if channel < 0 or channel >= self.rgbcct_channels:
			return None
		name = params.get("config", {}).get("name")
		if name:
			self._rgbcct_names[channel] = name
		return {"id": channel, "name": self._rgbcct_names[channel]}

	def rgbcct_set(self, params):
		channel = int(params.get("id", 0))
		if channel < 0 or channel >= self.rgbcct_channels:
			return None
		# The API requires at least one of on and brightness.
		if "on" not in params and "brightness" not in params:
			raise ValueError("At least one of 'on' and 'brightness' is required")
		if "mode" in params and params["mode"] not in ("rgb", "cct"):
			raise ValueError(f"Invalid mode: {params['mode']}")
		state = self._rgbcct[channel]
		for key in ("mode", "brightness", "rgb", "ct"):
			if key in params:
				state[key] = params[key]
		if "on" in params:
			state["output"] = bool(params["on"])
		# Push an immediate status update to websocket clients.
		try:
			loop = asyncio.get_running_loop()
			loop.create_task(self.notify_full_status())
		except RuntimeError:
			pass
		return {}

	def smoke_get_status(self, params):
		channel = int(params.get("id", 0))
		if channel != 0 or not self.smoke:
			return None
		return {"id": channel, "alarm": self._smoke_alarm, "mute": self._smoke_mute}

	def smoke_get_config(self, params):
		channel = int(params.get("id", 0))
		if channel != 0 or not self.smoke:
			return None
		return {"id": channel, "name": None}

	def smoke_set_config(self, params):
		channel = int(params.get("id", 0))
		if channel != 0 or not self.smoke:
			return None
		return {"id": channel, "name": None}

	def smoke_mute(self, params):
		channel = int(params.get("id", 0))
		if channel != 0 or not self.smoke:
			return None
		self._smoke_mute = True
		return {}

	def flood_get_status(self, params):
		channel = int(params.get("id", 0))
		if channel != 0 or not self.flood:
			return None
		return {"id": channel, "alarm": self._flood_alarm, "mute": self._flood_mute}

	def flood_get_config(self, params):
		channel = int(params.get("id", 0))
		if channel != 0 or not self.flood:
			return None
		return {"id": channel, "name": None}

	def flood_set_config(self, params):
		channel = int(params.get("id", 0))
		if channel != 0 or not self.flood:
			return None
		return {"id": channel, "name": None}

	def devicepower_get_status(self, params):
		channel = int(params.get("id", 0))
		if channel != 0 or not (self.smoke or self.flood):
			return None
		return {"id": channel, "battery": {"V": self._battery_voltage, "percent": self._battery_percent}}

	def devicepower_get_config(self, params):
		channel = int(params.get("id", 0))
		if channel != 0 or not (self.smoke or self.flood):
			return None
		return {}

	# Test hook, not a real Shelly method: trigger or clear the smoke/flood alarm.
	def mock_set_alarm(self, params):
		alarm = bool(params.get("alarm", True))
		if self.smoke:
			self._smoke_alarm = alarm
		elif self.flood:
			self._flood_alarm = alarm
		else:
			return None
		try:
			asyncio.get_running_loop().create_task(self.notify_full_status())
		except RuntimeError:
			pass
		return {"alarm": alarm}

	def _meter_values(self):
		current = self.default_apower / self.voltage if self.voltage else 0.0
		return self.voltage, current, self.default_apower

	def em_get_status(self, params):
		if int(params.get("id", 0)) != 0 or not self.em:
			return None
		voltage, current, power = self._meter_values()
		status = {"id": 0, "total_current": 3 * current, "total_act_power": 3 * power}
		for p in ("a", "b", "c"):
			status[f"{p}_voltage"] = voltage
			status[f"{p}_current"] = current
			status[f"{p}_act_power"] = power
			status[f"{p}_pf"] = self.pf
		return status

	def emdata_get_status(self, params):
		if int(params.get("id", 0)) != 0 or not self.em:
			return None
		status = {"id": 0, "total_act": 3 * self.energy_wh, "total_act_ret": 3 * self.ret_energy_wh}
		for p in ("a", "b", "c"):
			status[f"{p}_total_act_energy"] = self.energy_wh
			status[f"{p}_total_act_ret_energy"] = self.ret_energy_wh
		return status

	def em1_get_status(self, params):
		channel = int(params.get("id", 0))
		if channel < 0 or channel >= self.em1_channels:
			return None
		voltage, current, power = self._meter_values()
		return {"id": channel, "voltage": voltage, "current": current, "act_power": power, "pf": self.pf}

	def em1data_get_status(self, params):
		channel = int(params.get("id", 0))
		if channel < 0 or channel >= self.em1_channels:
			return None
		return {"id": channel, "total_act_energy": self.energy_wh, "total_act_ret_energy": self.ret_energy_wh}

	def pm1_get_status(self, params):
		channel = int(params.get("id", 0))
		if channel < 0 or channel >= self.pm1_channels:
			return None
		voltage, current, power = self._meter_values()
		return {"id": channel, "voltage": voltage, "current": current, "apower": power, "pf": self.pf,
			"aenergy": {"total": self.energy_wh}, "ret_aenergy": {"total": self.ret_energy_wh}}

	# Shared by EM, EM1 and PM1. kind is the lowercase component name.
	def meter_get_config(self, kind, params):
		channel = int(params.get("id", 0))
		default = (self.channel_name or None) if channel == 0 else None
		return {"id": channel, "name": self._meter_names.get(f"{kind}:{channel}", default)}

	def meter_set_config(self, kind, params):
		channel = int(params.get("id", 0))
		config = params.get("config", {})
		if "name" in config:
			self._meter_names[f"{kind}:{channel}"] = config["name"]
			self._emit_event(f"{kind}:{channel}", "config_changed")
		return {"restart_required": False}

	# Test hook, not a real Shelly method: make methods return an error. An empty list clears all faults.
	# Modes: "error" returns an RPC error, "timeout" never replies, "delay" replies after "delay" seconds.
	def mock_set_faults(self, params):
		self.faults = set(params.get("methods", []))
		self.fault_mode = params.get("mode", "error")
		self.fault_delay = float(params.get("delay", 0.0))
		return {"methods": sorted(self.faults), "mode": self.fault_mode}

	# Test hook, not a real Shelly method: freeze the device, as if it dropped off the network without
	# closing its connections. Open websockets go silent (no replies, no pongs, no notifications) and
	# new connections hang in the handshake. Only Mock.* calls over HTTP still work.
	def mock_freeze(self, params):
		if params.get("frozen", True):
			self.running.clear()
		else:
			self.running.set()
		return {"frozen": not self.running.is_set()}

	# Test hook, not a real Shelly method: [time, method] of the calls made over the websocket (by the driver).
	def mock_get_call_log(self, params):
		log = list(self.call_log)
		if params.get("reset", False):
			self.call_log = []
		return log

	# Shared by Light, RGB and CCT. kind is the lowercase component name.
	def light_get_status(self, kind, params):
		key = f"{kind}:{int(params.get('id', 0))}"
		if key not in self._lights:
			return None
		return {"id": int(params.get("id", 0)), **self._lights[key]}

	def light_get_config(self, kind, params):
		key = f"{kind}:{int(params.get('id', 0))}"
		if key not in self._lights:
			return None
		return {"id": int(params.get("id", 0)), "name": None}

	def light_set(self, kind, params):
		key = f"{kind}:{int(params.get('id', 0))}"
		if key not in self._lights:
			return None
		light = self._lights[key]
		if "on" in params:
			light["output"] = bool(params["on"])
		for field in ("brightness", "rgb", "ct", "white"):
			if field in params and field in light:
				light[field] = params[field]
		try:
			asyncio.get_running_loop().create_task(self.notify_full_status())
		except RuntimeError:
			pass
		return {}

	def list_methods(self):
		return [
			"Shelly.GetDeviceInfo",
			"Shelly.GetStatus",
			"Shelly.GetConfig",
			"Shelly.ListMethods",
			"Shelly.GetComponents",
			"Sys.GetConfig",
			"Sys.SetConfig",
			"Switch.GetStatus",
			"Switch.GetConfig",
			"Switch.SetConfig",
			"Switch.Set",
			"RGBCCT.GetStatus",
			"RGBCCT.GetConfig",
			"RGBCCT.SetConfig",
			"RGBCCT.Set",
			"Smoke.GetStatus",
			"Smoke.GetConfig",
			"Smoke.SetConfig",
			"Smoke.Mute",
			"Flood.GetStatus",
			"Flood.GetConfig",
			"Flood.SetConfig",
			"DevicePower.GetStatus",
			"DevicePower.GetConfig",
			"EM.GetStatus",
			"EM.GetConfig",
			"EM.SetConfig",
			"EMData.GetStatus",
			"EM1.GetStatus",
			"EM1.GetConfig",
			"EM1.SetConfig",
			"EM1Data.GetStatus",
			"PM1.GetStatus",
			"PM1.GetConfig",
			"PM1.SetConfig",
			"Light.GetStatus",
			"Light.GetConfig",
			"Light.Set",
			"RGB.GetStatus",
			"RGB.GetConfig",
			"RGB.Set",
			"CCT.GetStatus",
			"CCT.GetConfig",
			"CCT.Set",
		]

	def _emit_event(self, component, event):
		try:
			asyncio.get_running_loop().create_task(self.notify_event(component, event))
		except RuntimeError:
			pass

	async def notify_event(self, component, event):
		frame = {
			"src": f"shelly-{self.mac}",
			"method": "NotifyEvent",
			"params": {"ts": time.time(), "events": [{"component": component, "event": event, "ts": time.time()}]},
		}
		for ws in set(self._ws_clients):
			if ws.closed:
				self._ws_clients.discard(ws)
				continue
			await ws.send_json(frame)

	async def notify_full_status(self):
		if not self._ws_clients or not self.running.is_set():
			return
		frame = {
			"src": f"shelly-{self.mac}",
			"method": "NotifyFullStatus",
			"params": self.shelly_get_status(),
		}
		for ws in set(self._ws_clients):
			if ws.closed:
				self._ws_clients.discard(ws)
				continue
			await ws.send_json(frame)

	async def start_notifier(self, interval):
		if interval <= 0:
			return

		async def _runner():
			while True:
				await self.notify_full_status()
				await asyncio.sleep(interval)

		self._notify_task = asyncio.create_task(_runner())

	async def stop_notifier(self):
		if self._notify_task:
			self._notify_task.cancel()


def _json_response(result, request_id=None):
	return {"id": request_id, "result": result}


def _json_error(message, request_id=None, code=404):
	return {"id": request_id, "error": {"code": code, "message": message}}


def _vprint(request, message):
	if request.app.get("verbose"):
		print(message)


async def handle_rpc(device, frame, *, verbose=False):
	request_id = frame.get("id")
	method = frame.get("method")
	params = frame.get("params", {}) or {}
	if method in device.faults:
		if device.fault_mode == "timeout":
			return None
		if device.fault_mode == "delay":
			await asyncio.sleep(device.fault_delay)
		else:
			resp = _json_error(f"Mock fault for {method}", request_id, code=-114)
			resp["src"] = f"shelly-{device.mac}"
			return resp
	if verbose:
		print(f"mock rpc method={method} params={params}")

	if method == "Shelly.GetDeviceInfo":
		resp = _json_response(device.shelly_get_device_info(), request_id)
		resp["src"] = f"shelly-{device.mac}"
		return resp
	if method == "Shelly.GetStatus":
		resp = _json_response(device.shelly_get_status(), request_id)
		resp["src"] = f"shelly-{device.mac}"
		return resp
	if method == "Shelly.GetConfig":
		resp = _json_response(device.shelly_get_config(), request_id)
		resp["src"] = f"shelly-{device.mac}"
		return resp
	if method == "Shelly.GetComponents":
		resp = _json_response(device.shelly_get_components(params), request_id)
		resp["src"] = f"shelly-{device.mac}"
		return resp
	if method == "Shelly.ListMethods":
		resp = _json_response({"methods": device.list_methods()}, request_id)
		resp["src"] = f"shelly-{device.mac}"
		return resp
	if method == "Sys.GetConfig":
		resp = _json_response(device.sys_get_config(), request_id)
		resp["src"] = f"shelly-{device.mac}"
		return resp
	if method == "Sys.SetConfig":
		resp = _json_response(device.sys_set_config(params), request_id)
		resp["src"] = f"shelly-{device.mac}"
		return resp
	if method == "Switch.GetStatus":
		resp = _json_response(device.switch_get_status(params), request_id)
		resp["src"] = f"shelly-{device.mac}"
		return resp
	if method == "Switch.GetConfig":
		resp = _json_response(device.switch_get_config(params), request_id)
		resp["src"] = f"shelly-{device.mac}"
		return resp
	if method == "Switch.SetConfig":
		resp = _json_response(device.switch_set_config(params), request_id)
		resp["src"] = f"shelly-{device.mac}"
		return resp
	if method == "Switch.Set":
		resp = _json_response(device.switch_set(params), request_id)
		resp["src"] = f"shelly-{device.mac}"
		return resp
	if method == "RGBCCT.GetStatus":
		resp = _json_response(device.rgbcct_get_status(params), request_id)
		resp["src"] = f"shelly-{device.mac}"
		return resp
	if method == "RGBCCT.GetConfig":
		resp = _json_response(device.rgbcct_get_config(params), request_id)
		resp["src"] = f"shelly-{device.mac}"
		return resp
	if method == "RGBCCT.SetConfig":
		resp = _json_response(device.rgbcct_set_config(params), request_id)
		resp["src"] = f"shelly-{device.mac}"
		return resp
	if method == "RGBCCT.Set":
		try:
			resp = _json_response(device.rgbcct_set(params), request_id)
		except ValueError as e:
			resp = _json_error(str(e), request_id, code=-103)
		resp["src"] = f"shelly-{device.mac}"
		return resp
	if method == "Smoke.GetStatus":
		resp = _json_response(device.smoke_get_status(params), request_id)
		resp["src"] = f"shelly-{device.mac}"
		return resp
	if method == "Smoke.GetConfig":
		resp = _json_response(device.smoke_get_config(params), request_id)
		resp["src"] = f"shelly-{device.mac}"
		return resp
	if method == "Smoke.SetConfig":
		resp = _json_response(device.smoke_set_config(params), request_id)
		resp["src"] = f"shelly-{device.mac}"
		return resp
	if method == "Smoke.Mute":
		resp = _json_response(device.smoke_mute(params), request_id)
		resp["src"] = f"shelly-{device.mac}"
		return resp
	if method == "Flood.GetStatus":
		resp = _json_response(device.flood_get_status(params), request_id)
		resp["src"] = f"shelly-{device.mac}"
		return resp
	if method == "Flood.GetConfig":
		resp = _json_response(device.flood_get_config(params), request_id)
		resp["src"] = f"shelly-{device.mac}"
		return resp
	if method == "Flood.SetConfig":
		resp = _json_response(device.flood_set_config(params), request_id)
		resp["src"] = f"shelly-{device.mac}"
		return resp
	if method == "DevicePower.GetStatus":
		resp = _json_response(device.devicepower_get_status(params), request_id)
		resp["src"] = f"shelly-{device.mac}"
		return resp
	if method == "DevicePower.GetConfig":
		resp = _json_response(device.devicepower_get_config(params), request_id)
		resp["src"] = f"shelly-{device.mac}"
		return resp
	if method == "EM.GetStatus" and device.em:
		resp = _json_response(device.em_get_status(params), request_id)
		resp["src"] = f"shelly-{device.mac}"
		return resp
	if method == "EM.GetConfig" and device.em:
		resp = _json_response(device.meter_get_config("em", params), request_id)
		resp["src"] = f"shelly-{device.mac}"
		return resp
	if method == "EM.SetConfig" and device.em:
		resp = _json_response(device.meter_set_config("em", params), request_id)
		resp["src"] = f"shelly-{device.mac}"
		return resp
	if method == "EMData.GetStatus" and device.em:
		resp = _json_response(device.emdata_get_status(params), request_id)
		resp["src"] = f"shelly-{device.mac}"
		return resp
	if method == "EM1.GetStatus" and device.em1_channels:
		resp = _json_response(device.em1_get_status(params), request_id)
		resp["src"] = f"shelly-{device.mac}"
		return resp
	if method == "EM1.GetConfig" and device.em1_channels:
		resp = _json_response(device.meter_get_config("em1", params), request_id)
		resp["src"] = f"shelly-{device.mac}"
		return resp
	if method == "EM1.SetConfig" and device.em1_channels:
		resp = _json_response(device.meter_set_config("em1", params), request_id)
		resp["src"] = f"shelly-{device.mac}"
		return resp
	if method == "EM1Data.GetStatus" and device.em1_channels:
		resp = _json_response(device.em1data_get_status(params), request_id)
		resp["src"] = f"shelly-{device.mac}"
		return resp
	if method == "PM1.GetStatus" and device.pm1_channels:
		resp = _json_response(device.pm1_get_status(params), request_id)
		resp["src"] = f"shelly-{device.mac}"
		return resp
	if method == "PM1.GetConfig" and device.pm1_channels:
		resp = _json_response(device.meter_get_config("pm1", params), request_id)
		resp["src"] = f"shelly-{device.mac}"
		return resp
	if method == "PM1.SetConfig" and device.pm1_channels:
		resp = _json_response(device.meter_set_config("pm1", params), request_id)
		resp["src"] = f"shelly-{device.mac}"
		return resp
	for component in ("Light", "RGB", "CCT"):
		kind = component.lower()
		if not any(key.startswith(f"{kind}:") for key in device._lights):
			continue
		if method == f"{component}.GetStatus":
			resp = _json_response(device.light_get_status(kind, params), request_id)
			resp["src"] = f"shelly-{device.mac}"
			return resp
		if method in (f"{component}.GetConfig", f"{component}.SetConfig"):
			resp = _json_response(device.light_get_config(kind, params), request_id)
			resp["src"] = f"shelly-{device.mac}"
			return resp
		if method == f"{component}.Set":
			resp = _json_response(device.light_set(kind, params), request_id)
			resp["src"] = f"shelly-{device.mac}"
			return resp
	if method == "Mock.SetFaults":
		resp = _json_response(device.mock_set_faults(params), request_id)
		resp["src"] = f"shelly-{device.mac}"
		return resp
	if method == "Mock.GetCallLog":
		resp = _json_response(device.mock_get_call_log(params), request_id)
		resp["src"] = f"shelly-{device.mac}"
		return resp
	if method == "Mock.Freeze":
		resp = _json_response(device.mock_freeze(params), request_id)
		resp["src"] = f"shelly-{device.mac}"
		return resp
	if method == "Mock.SetAlarm":
		resp = _json_response(device.mock_set_alarm(params), request_id)
		resp["src"] = f"shelly-{device.mac}"
		return resp
	resp = _json_error(f"Unknown method: {method}", request_id)
	resp["src"] = f"shelly-{device.mac}"
	return resp


async def http_rpc_handler(request):
	device = request.app["device"]
	try:
		frame = await request.json()
	except Exception:
		return web.json_response(_json_error("Invalid JSON", None, code=400))
	_vprint(request, f"mock http /rpc frame={frame}")
	if not str(frame.get("method", "")).startswith("Mock."):
		await device.running.wait()
	return web.json_response(await handle_rpc(device, frame, verbose=request.app.get('verbose', False)))


async def ws_rpc_handler(request):
	device = request.app["device"]
	# A frozen device doesn't complete the handshake
	await device.running.wait()
	# Pings are answered here instead of by aiohttp, so a frozen device stops answering them.
	ws = web.WebSocketResponse(autoping=False)
	await ws.prepare(request)
	device._ws_clients.add(ws)
	_vprint(request, "mock ws connected")

	async for msg in ws:
		await device.running.wait()
		if msg.type == web.WSMsgType.PING:
			await ws.pong(msg.data)
		elif msg.type == web.WSMsgType.TEXT:
			try:
				frame = json.loads(msg.data)
			except json.JSONDecodeError:
				await ws.send_json(_json_error("Invalid JSON", None, code=400))
				continue
			_vprint(request, f"mock ws frame={frame}")
			device.call_log.append((time.time(), frame.get("method", "")))
			response = await handle_rpc(device, frame, verbose=request.app.get('verbose', False))
			if response is not None:
				await ws.send_json(response)
		elif msg.type == web.WSMsgType.ERROR:
			break

	device._ws_clients.discard(ws)
	return ws


async def shelly_info_handler(request):
	device = request.app["device"]
	await device.running.wait()
	_vprint(request, "mock http /shelly")
	return web.json_response(device.shelly_info())


def parse_args():
	parser = argparse.ArgumentParser(description="Mock Shelly Gen2 device server.")
	parser.add_argument("--host", default="0.0.0.0")
	parser.add_argument("--port", type=int, default=8022)
	parser.add_argument("--device-name", default="Mocked Shelly device")
	parser.add_argument("--channel-name", default="Channel 1")
	parser.add_argument("--app", default="MockSwitchEM")
	parser.add_argument("--mac", default="aabbccddeeff")
	parser.add_argument("--switch-channels", type=int, default=1)
	parser.add_argument("--rgbcct-channels", type=int, default=0)
	parser.add_argument("--voltage", type=float, default=230.0)
	parser.add_argument("--apower", type=float, default=1000.0)
	parser.add_argument("--pf", type=float, default=0.98)
	parser.add_argument("--smoke", action="store_true")
	parser.add_argument("--flood", action="store_true")
	parser.add_argument("--battery-percent", type=float, default=100.0)
	parser.add_argument("--battery-voltage", type=float, default=3.0)
	parser.add_argument("--em", action="store_true", help="Add a 3-phase EM + EMData component")
	parser.add_argument("--em1-channels", type=int, default=0)
	parser.add_argument("--pm1-channels", type=int, default=0)
	parser.add_argument("--light-channels", type=int, default=0)
	parser.add_argument("--rgb", action="store_true")
	parser.add_argument("--cct", action="store_true")
	parser.add_argument("--notify-interval", type=float, default=5)
	parser.add_argument("--verbose", action="store_true")
	return parser.parse_args()


async def main():
	args = parse_args()
	device = MockShellyDevice(
		device_name=args.device_name,
		channel_name=args.channel_name,
		model="Mock Shelly",	# Hardcoded so clients can identify it as a mock device
		app=args.app,
		mac=args.mac,
		switch_channels=args.switch_channels,
		rgbcct_channels=args.rgbcct_channels,
		voltage=args.voltage,
		apower=args.apower,
		pf=args.pf,
		smoke=args.smoke,
		flood=args.flood,
		battery_percent=args.battery_percent,
		battery_voltage=args.battery_voltage,
		em=args.em,
		em1_channels=args.em1_channels,
		pm1_channels=args.pm1_channels,
		light_channels=args.light_channels,
		rgb=args.rgb,
		cct=args.cct,
	)

	app = web.Application()
	app["device"] = device
	app["verbose"] = args.verbose
	app.add_routes(
		[
			web.get("/shelly", shelly_info_handler),
			web.post("/rpc", http_rpc_handler),
			web.get("/rpc", ws_rpc_handler),
		]
	)

	await device.start_notifier(args.notify_interval)
	runner = web.AppRunner(app)
	await runner.setup()
	site = web.TCPSite(runner, host=args.host, port=args.port)
	await site.start()

	print(f"Mock Shelly running on http://{args.host}:{args.port}")
	try:
		while True:
			await asyncio.sleep(3600)
	except asyncio.CancelledError:
		pass
	finally:
		await device.stop_notifier()
		await runner.cleanup()


if __name__ == "__main__":
	asyncio.run(main())
