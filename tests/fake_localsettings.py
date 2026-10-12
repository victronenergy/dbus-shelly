#!/usr/bin/env python3
"""
Minimal in-memory stand-in for com.victronenergy.settings (localsettings).

Implements only what aiovelib's SettingsService and Monitor use:
  - AddSettings on / (interface com.victronenergy.Settings)
  - GetItems on / (interface com.victronenergy.BusItem)
  - GetValue / GetText / SetValue on each setting path
and emits ItemsChanged on / when a value changes.

Values are not persisted. Pass --initial '{"/Settings/...": value}' to
pre-seed settings, which then take precedence over the AddSettings default.
"""
import argparse
import asyncio
import json
import signal

from dbus_fast import Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.constants import BusType

SETTINGS_SERVICE = "com.victronenergy.settings"
SETTINGS_IFACE = "com.victronenergy.Settings"
BUSITEM_IFACE = "com.victronenergy.BusItem"


def make_variant(value):
	if isinstance(value, float):
		return Variant("d", value)
	if isinstance(value, int):
		return Variant("i", value)
	return Variant("s", str(value))


def coerce(variant, signature):
	""" Convert an incoming variant to the type the setting was created with. """
	try:
		if signature == "i":
			return Variant("i", int(variant.value))
		if signature == "d":
			return Variant("d", float(variant.value))
		if signature == "s":
			return Variant("s", str(variant.value))
	except (TypeError, ValueError):
		return None
	return variant


class FakeSettings:
	def __init__(self, bus, initial):
		self.bus = bus
		self.values = {p: make_variant(v) for p, v in initial.items()}

	def _text(self, v):
		return Variant("s", str(v.value))

	def _items_changed(self, path):
		v = self.values[path]
		self.bus.send(Message(
			message_type=MessageType.SIGNAL,
			path="/",
			interface=BUSITEM_IFACE,
			member="ItemsChanged",
			signature="a{sa{sv}}",
			body=[{path: {"Value": v, "Text": self._text(v)}}]))

	def _error(self, msg, name, text):
		return Message.new_error(msg, name, text)

	def handle(self, msg):
		if msg.message_type != MessageType.METHOD_CALL:
			return None
		if msg.interface not in (SETTINGS_IFACE, BUSITEM_IFACE, None):
			return None

		if msg.path == "/" and msg.member == "AddSettings":
			result = []
			for s in msg.body[0]:
				path = s["path"].value
				default = s["default"]
				if path not in self.values:
					self.values[path] = default
				else:
					# Keep the pre-seeded value, but in the type of the default
					self.values[path] = coerce(self.values[path], default.signature) or default
				result.append({
					"path": Variant("s", path),
					"error": Variant("i", 0),
					"value": self.values[path],
				})
			return Message.new_method_return(msg, "aa{sv}", [result])

		if msg.path == "/" and msg.member == "GetItems":
			return Message.new_method_return(msg, "a{sa{sv}}", [
				{p: {"Value": v, "Text": self._text(v)} for p, v in self.values.items()}])

		if msg.path not in self.values:
			return self._error(msg, "org.freedesktop.DBus.Error.UnknownObject", msg.path)

		if msg.member == "GetValue":
			return Message.new_method_return(msg, "v", [self.values[msg.path]])

		if msg.member == "GetText":
			return Message.new_method_return(msg, "s", [str(self.values[msg.path].value)])

		if msg.member == "SetValue":
			new = coerce(msg.body[0], self.values[msg.path].signature)
			if new is None:
				return Message.new_method_return(msg, "i", [-1])
			changed = new.value != self.values[msg.path].value
			self.values[msg.path] = new
			reply = Message.new_method_return(msg, "i", [0])
			if changed:
				self._items_changed(msg.path)
			return reply

		return None


async def main(args):
	initial = json.loads(args.initial) if args.initial else {}
	bus = await MessageBus(bus_type=BusType.SESSION).connect()
	settings = FakeSettings(bus, initial)
	bus.add_message_handler(settings.handle)
	await bus.request_name(SETTINGS_SERVICE)
	print("fake localsettings ready", flush=True)

	stop = asyncio.Event()
	loop = asyncio.get_running_loop()
	for sig in (signal.SIGINT, signal.SIGTERM):
		loop.add_signal_handler(sig, stop.set)
	await stop.wait()
	bus.disconnect()


if __name__ == "__main__":
	parser = argparse.ArgumentParser(description="Fake com.victronenergy.settings for tests")
	parser.add_argument("--initial", help="JSON object of pre-seeded setting values")
	asyncio.run(main(parser.parse_args()))
