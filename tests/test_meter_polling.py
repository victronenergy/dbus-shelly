import asyncio
import os
import sys
import types
import unittest


ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

main_mod = sys.modules["__main__"]
if not hasattr(main_mod, "VERSION"):
	main_mod.VERSION = "test"


def install_stubs():
	aiohttp = types.ModuleType("aiohttp")
	aiohttp.ClientSession = object
	sys.modules["aiohttp"] = aiohttp

	async_timeout = types.ModuleType("async_timeout")

	class Timeout:
		async def __aenter__(self):
			return self

		async def __aexit__(self, *args):
			return False

	async_timeout.timeout = lambda _seconds: Timeout()
	sys.modules["async_timeout"] = async_timeout

	aioshelly = types.ModuleType("aioshelly")
	aioshelly_common = types.ModuleType("aioshelly.common")
	aioshelly_rpc = types.ModuleType("aioshelly.rpc_device")
	aioshelly_exceptions = types.ModuleType("aioshelly.exceptions")

	class ConnectionOptions:
		def __init__(self, *args, **kwargs):
			pass

	class RpcUpdateType:
		STATUS = "status"
		DISCONNECTED = "disconnected"
		EVENT = "event"

	class RpcDevice:
		@classmethod
		async def create(cls, *args, **kwargs):
			return None

	class WsServer:
		pass

	class DeviceConnectionError(Exception):
		pass

	aioshelly_common.ConnectionOptions = ConnectionOptions
	aioshelly_rpc.RpcDevice = RpcDevice
	aioshelly_rpc.RpcUpdateType = RpcUpdateType
	aioshelly_rpc.WsServer = WsServer
	aioshelly_exceptions.DeviceConnectionError = DeviceConnectionError
	sys.modules["aioshelly"] = aioshelly
	sys.modules["aioshelly.common"] = aioshelly_common
	sys.modules["aioshelly.rpc_device"] = aioshelly_rpc
	sys.modules["aioshelly.exceptions"] = aioshelly_exceptions

	dbus_fast = types.ModuleType("dbus_fast")
	dbus_fast_aio = types.ModuleType("dbus_fast.aio")

	class MessageBus:
		def __init__(self, *args, **kwargs):
			pass

		async def connect(self):
			return self

	dbus_fast_aio.MessageBus = MessageBus
	sys.modules["dbus_fast"] = dbus_fast
	sys.modules["dbus_fast.aio"] = dbus_fast_aio

	aiovelib = types.ModuleType("aiovelib")
	aiovelib_localsettings = types.ModuleType("aiovelib.localsettings")
	aiovelib_service = types.ModuleType("aiovelib.service")
	aiovelib_client = types.ModuleType("aiovelib.client")

	class Setting:
		def __init__(self, path, default, _min=None, _max=None, alias=None):
			self.path = path
			self.default = default
			self.alias = alias

	class Item:
		def __init__(self, path, value=None, **kwargs):
			self.path = path
			self.value = value
			self.service = True

		def set_local_value(self, value):
			self.value = value

	class Service:
		pass

	class Monitor:
		@classmethod
		async def create(cls, *args, **kwargs):
			return cls()

		async def wait_for_service(self, *args, **kwargs):
			return None

	aiovelib_localsettings.Setting = Setting
	aiovelib_localsettings.SettingsService = object
	aiovelib_localsettings.SETTINGS_SERVICE = "com.victronenergy.settings"
	aiovelib_service.Service = Service
	for name in ("IntegerItem", "TextItem", "DoubleItem", "IntegerArrayItem", "TextArrayItem"):
		setattr(aiovelib_service, name, Item)
	aiovelib_client.Monitor = Monitor
	sys.modules["aiovelib"] = aiovelib
	sys.modules["aiovelib.localsettings"] = aiovelib_localsettings
	sys.modules["aiovelib.service"] = aiovelib_service
	sys.modules["aiovelib.client"] = aiovelib_client


install_stubs()

import shelly_handlers
from shelly_device import ShellyDevice


class FakeShelly:
	connected = True
	initialized = True
	ip_address = "192.0.2.10"

	async def shutdown(self):
		pass

	def subscribe_updates(self, _callback):
		pass


class FakeSession:
	async def close(self):
		pass


class FakeHandler:
	def __init__(self):
		self.updates = []
		self.meter_updates = []
		self.stopped = False

	def update(self, status, cap=None):
		self.updates.append((status, cap))

	def update_meter_status(self, status, cap=None):
		self.meter_updates.append((status, cap))
		self.update(status, cap)

	async def stop(self):
		self.stopped = True


class FakeChannel:
	def __init__(self):
		self.stopped = False

	async def stop(self):
		self.stopped = True


class FakeService:
	def __init__(self):
		self.values = {}

	def __enter__(self):
		return self

	def __exit__(self, *args):
		return False

	def __setitem__(self, path, value):
		self.values[path] = value


class MeterPollingTests(unittest.IsolatedAsyncioTestCase):
	async def asyncTearDown(self):
		current = asyncio.current_task()
		for task in asyncio.all_tasks():
			if task is current:
				continue
			if getattr(task.get_coro(), "__qualname__", "") != "ShellyDevice._meter_poll_loop":
				continue
			task.cancel()
		await asyncio.sleep(0)

	def make_device(self, interval=0):
		device = ShellyDevice(serial="ABC123", server="192.0.2.10", meter_poll_interval=interval)
		device._shelly_device = FakeShelly()
		return device

	async def test_default_polling_is_disabled(self):
		device = self.make_device()

		device._start_meter_polling()

		self.assertEqual(device._meter_poll_interval, 0)
		self.assertIsNone(device._meter_poll_task)

	async def test_enabled_polling_requests_only_instantaneous_meter_status(self):
		device = self.make_device(interval=1)
		em_handler = FakeHandler()
		em1_handler = FakeHandler()
		pm1_handler = FakeHandler()
		switch_handler = FakeHandler()
		device._channels = {
			0: {
				"ch_type": shelly_handlers.HANDLER_KIND_EM,
				"ch_num": "0",
				"handlers": {"EM": em_handler, "EMData": FakeHandler()},
			},
			1: {
				"ch_type": shelly_handlers.HANDLER_KIND_EM1,
				"ch_num": "1",
				"handlers": {"EM1": em1_handler, "EM1Data": FakeHandler()},
			},
			2: {
				"ch_type": shelly_handlers.HANDLER_KIND_EM1,
				"ch_num": "2",
				"handlers": {"PM1": pm1_handler},
			},
			3: {
				"ch_type": shelly_handlers.HANDLER_KIND_SWITCH,
				"ch_num": "3",
				"handlers": {"Switch": switch_handler},
			},
		}
		calls = []

		async def rpc_call(method, params):
			calls.append((method, params))
			return {"method": method, "id": params["id"]}

		device.rpc_call = rpc_call

		await device._meter_poll_once()

		self.assertEqual(calls, [
			("EM.GetStatus", {"id": 0}),
			("EM1.GetStatus", {"id": 1}),
			("PM1.GetStatus", {"id": 2}),
		])
		self.assertEqual(em_handler.updates, [({"method": "EM.GetStatus", "id": 0}, "em")])
		self.assertEqual(em1_handler.updates, [({"method": "EM1.GetStatus", "id": 1}, "em1")])
		self.assertEqual(pm1_handler.updates, [({"method": "PM1.GetStatus", "id": 2}, "pm1")])
		self.assertEqual(pm1_handler.meter_updates, [({"method": "PM1.GetStatus", "id": 2}, "pm1")])
		self.assertEqual(switch_handler.updates, [])

	async def test_lifecycle_stops_and_resumes_polling(self):
		device = self.make_device(interval=1)

		device._start_meter_polling()
		first_task = device._meter_poll_task
		device._start_meter_polling()
		self.assertIs(device._meter_poll_task, first_task)

		device._stop_meter_polling()
		self.assertIsNone(device._meter_poll_task)
		with self.assertRaises(asyncio.CancelledError):
			await first_task

		handler = FakeHandler()
		channel = FakeChannel()
		device._channels = {
			0: {
				"ch_type": shelly_handlers.HANDLER_KIND_EM,
				"ch_num": "0",
				"handlers": {"EM": handler},
				"channel": channel,
			}
		}
		calls = []

		async def rpc_call(method, params):
			calls.append((method, params))
			return {"ok": True}

		device.rpc_call = rpc_call

		await device._meter_poll_once()
		await device.stop_channel(0)
		await device._meter_poll_once()

		self.assertEqual(calls, [("EM.GetStatus", {"id": 0})])
		self.assertTrue(handler.stopped)
		self.assertTrue(channel.stopped)

		device._channels = {
			0: {
				"ch_type": shelly_handlers.HANDLER_KIND_EM,
				"ch_num": "0",
				"handlers": {"EM": FakeHandler()},
				"channel": FakeChannel(),
			}
		}
		device._shelly_device = FakeShelly()
		device._aiohttp_session = FakeSession()
		reinited = []

		async def ping_shelly():
			device._shelly_device = FakeShelly()
			return True

		async def reinit(ch):
			reinited.append(ch)

		device.ping_shelly = ping_shelly
		device._reinit_channel_and_handlers = reinit

		await device._reconnect()

		self.assertEqual(reinited, [0])
		self.assertIsNotNone(device._meter_poll_task)
		device._stop_meter_polling()

	async def test_failed_rpc_call_does_not_update_handler(self):
		device = self.make_device(interval=1)
		handler = FakeHandler()
		device._channels = {
			0: {
				"ch_type": shelly_handlers.HANDLER_KIND_EM,
				"ch_num": "0",
				"handlers": {"EM": handler},
			}
		}

		async def rpc_call(_method, _params):
			return None

		device.rpc_call = rpc_call

		await device._meter_poll_once()

		self.assertEqual(handler.updates, [])

	async def test_pm1_polling_leaves_energy_counters_on_notification_path(self):
		handler = shelly_handlers.ShellyHandler_em1()
		handler._phase = 1
		handler.service = FakeService()

		status = {
			"voltage": 230,
			"current": 2,
			"apower": 100,
			"pf": 0.9,
			"aenergy": {"total": 1234},
			"ret_aenergy": {"total": 567},
		}

		handler.update_meter_status(status, cap="pm1")

		self.assertEqual(handler.service.values["/Ac/L1/Power"], 100)
		self.assertNotIn("/Ac/L1/Energy/Forward", handler.service.values)
		self.assertNotIn("/Ac/Energy/Forward", handler.service.values)

		handler._em_role = "acload"
		handler.update(status, cap="pm1")

		self.assertEqual(handler.service.values["/Ac/L1/Energy/Forward"], 1.234)
		self.assertEqual(handler.service.values["/Ac/Energy/Forward"], 1.234)


if __name__ == "__main__":
	unittest.main()
