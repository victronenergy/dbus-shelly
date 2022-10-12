VERSION = "0.1"
MDNS_QUERY_INTERVAL = 60

import sys, os
from time import time
from argparse import ArgumentParser
import logging

import dbus
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

sys.path.insert(1, os.path.join(os.path.dirname(__file__), 'ext', 'velib_python'))
from settingsdevice import SettingsDevice

from meter import Meter
from mdns import MDNS

logger = logging.getLogger()

class Driver(object):

	def __init__(self, get_bus):
		self.get_bus = get_bus
		self.meters = {}

		# Connect to localsettings
		SETTINGS = {
			'devices': ['/Settings/Shelly/Devices', '', 0, 0],
			'scanmdns': ['/Settings/Shelly/ScanMdns', 1, 0, 1]
		}
		logger.info('Waiting for localsettings')
		self.settings = SettingsDevice(get_bus(), SETTINGS,
				self.setting_changed, timeout=10)

		# MDNS scan, if enabled
		self.mdns = None
		self.mdns_query_time = time()
		if self.settings['scanmdns'] == 1:
			self.mdns = MDNS()
			self.mdns.start()
			self.mdns.req()

		# Start known devices
		self.set_meters('', self.settings['devices'])

	def setting_changed(self, name, old, new):
		if name == 'devices':
			self.set_meters(old, new)

	def set_meters(self, old, new):
		old = set(filter(None, old.split(',')))
		new = set(filter(None, new.split(',')))
		cur = set(self.meters.keys())
		rem = old - new

		for h in rem & cur:
			m = self.meters[h]
			del self.meters[h]
			m.destroy()

		for h in new - cur:
			self.meters[h] = m = Meter(self.get_bus, h)

	def update(self):
		try:
			for m in self.meters.values():
				if not m.active:
					logger.info("Starting shelly meter at {}".format(m.host))
					m.start()
		except:
			logger.exception("update")

		# Send new MDNS query every minute
		now = time()
		if self.mdns is not None and now - self.mdns_query_time > MDNS_QUERY_INTERVAL:
			self.mdns_query_time = now
			self.mdns.req()

		# If mdns scanning is on, then connect to meters found
		if self.mdns is not None:
			for h in self.mdns.get_devices() - set(self.meters.keys()):
				self.meters[h] = Meter(self.get_bus, h)

		return True

def main():
	parser = ArgumentParser(description=sys.argv[0])
	parser.add_argument('--dbus', help='dbus bus to use, defaults to system',
		default='system')
	parser.add_argument('--debug', help='Turn on debug logging',
		default=False, action='store_true')
	args = parser.parse_args()

	logging.basicConfig(format='%(levelname)-8s %(message)s',
			level=(logging.DEBUG if args.debug else logging.INFO))

	_get_bus = {
		'system': dbus.Bus.get_system,
		'session': dbus.Bus.get_session
	}.get(args.dbus, dbus.Bus.get_session)

	get_bus = lambda: _get_bus(private=True)

	DBusGMainLoop(set_as_default=True)

	driver = Driver(get_bus)
	driver.update()
	GLib.timeout_add(5000, driver.update)

	mainloop = GLib.MainLoop()
	try:
		mainloop.run()
	except KeyboardInterrupt:
		pass

if __name__ == "__main__":
	main()
