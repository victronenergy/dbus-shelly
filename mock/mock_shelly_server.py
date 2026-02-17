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
		voltage=230.0,
		apower=120.0,
		pf=0.98,
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
		self._start = time.time()

		self._switch_outputs = [False] * switch_channels
		self._switch_names = [channel_name if i == 0 else f"Channel {i + 1}" for i in range(switch_channels)]
		self._energy_total = [0.0] * switch_channels
		self._ret_energy_total = [0.0] * switch_channels
		self._last_energy_update = time.time()

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
		return status

	def shelly_get_config(self):
		return {
			"device": {"name": self.name, "profile": "switch"},
			"sys": {"device": {"name": self.name}},
		}

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

	def list_methods(self):
		return [
			"Shelly.GetDeviceInfo",
			"Shelly.GetStatus",
			"Shelly.GetConfig",
			"Shelly.ListMethods",
			"Sys.GetConfig",
			"Sys.SetConfig",
			"Switch.GetStatus",
			"Switch.GetConfig",
			"Switch.SetConfig",
			"Switch.Set",
		]

	async def notify_full_status(self):
		if not self._ws_clients:
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
	return web.json_response(await handle_rpc(device, frame, verbose=request.app.get('verbose', False)))


async def ws_rpc_handler(request):
	device = request.app["device"]
	ws = web.WebSocketResponse()
	await ws.prepare(request)
	device._ws_clients.add(ws)
	_vprint(request, "mock ws connected")

	async for msg in ws:
		if msg.type == web.WSMsgType.TEXT:
			try:
				frame = json.loads(msg.data)
			except json.JSONDecodeError:
				await ws.send_json(_json_error("Invalid JSON", None, code=400))
				continue
			_vprint(request, f"mock ws frame={frame}")
			response = await handle_rpc(device, frame, verbose=request.app.get('verbose', False))
			await ws.send_json(response)
		elif msg.type == web.WSMsgType.ERROR:
			break

	device._ws_clients.discard(ws)
	return ws


async def shelly_info_handler(request):
	device = request.app["device"]
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
	parser.add_argument("--voltage", type=float, default=230.0)
	parser.add_argument("--apower", type=float, default=1000.0)
	parser.add_argument("--pf", type=float, default=0.98)
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
		voltage=args.voltage,
		apower=args.apower,
		pf=args.pf,
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
