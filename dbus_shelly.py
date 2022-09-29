VERSION = "0.1"
MAXERROR = 5
MDNS_QUERY_INTERVAL = 60

import sys, os
from time import sleep, time
from argparse import ArgumentParser
import logging
import json
from urllib.parse import urlunparse
from collections.abc import Mapping
from functools import partial

import requests
import dbus
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib, Gio

sys.path.insert(1, os.path.join(os.path.dirname(__file__), 'ext', 'velib_python'))
from vedbus import VeDbusService
from settingsdevice import SettingsDevice

from mdns import MDNS

logger = logging.getLogger()

class Meter(object):
	def __init__(self, get_bus, host):
		self.starting = False
		self.cancel = Gio.Cancellable()
		self.host = host
		self.get_bus = get_bus
		self.service = None
		self.errorcount = MAXERROR
		self.settings = None
		self.position = None
	
	def __hash__(self):
		return hash(self.host)
	
	def __eq__(self, other):
		if isinstance(other, type(self)):
			return self.host == other.host
		if isinstance(other, str):
			return self.host == other

		return False

	@property
	def active(self):
		return self.starting or self.service is not None
	
	def start(self):
		self.cancel.reset()
		f = Gio.File.new_for_uri(urlunparse(
			('http', self.host, '/shelly', '', '', '')))
		f.load_contents_async(
			self.cancel, self.register, None)
		self.starting = True

	def register(self, ob, result, userdata):
		self.starting = False
		try:
			success, content, etag = ob.load_contents_finish(result)
		except GLib.GError:
			logger.exception("Failed to read /status for {}".format(self.host))
			return

		try:
			data = json.loads(content)
		except ValueError:
			logger.exception("Failed to parse JSON for /shelly call")
			return

		name = data['mac']
		bus = self.get_bus()
		path = '/Settings/Devices/shelly_' + name
		SETTINGS = {
			'instance': [path + '/ClassAndVrmInstance', 'grid:40', 0, 0],
		}
		self.settings = SettingsDevice(bus, SETTINGS, self.setting_changed)
		role, instance = self.role_instance

		# Set up the service
		self.service = VeDbusService(
			"com.victronenergy.{}.{}".format(role, name), bus=bus)
		self.service.add_path('/Mgmt/ProcessName', __file__)
		self.service.add_path('/Mgmt/ProcessVersion', VERSION)
		self.service.add_path('/Mgmt/Connection', self.host)
		self.service.add_path('/DeviceInstance', instance)
		self.service.add_path('/ProductId', 0) # TODO allocate one
		self.service.add_path('/ProductName', "Shelly energy meter")
		self.service.add_path('/Connected', 1)

		# role
		self.service.add_path('/AllowedRoles',
			['grid', 'pvinverter', 'genset', 'acload'])
		self.service.add_path('/Role', role, writeable=True,
			onchangecallback=self.role_changed)

		# Position for pvinverter
		if role == 'pvinverter':
			self.position = self.settings.addSetting(path + '/Position', 0, 0, 2)
			self.service.add_path('/Position', self.position.get_value(),
				writeable=True, onchangecallback=self.position_changed)

		# Meter paths
		self.service.add_path('/Ac/Energy/Forward', None)
		self.service.add_path('/Ac/Energy/Reverse', None)
		for prefix in ('/Ac/L{}'.format(x) for x in range(1, 4)):
			self.service.add_path(prefix + '/Voltage', None)
			self.service.add_path(prefix + '/Current', None)
			self.service.add_path(prefix + '/Power', None)
			self.service.add_path(prefix + '/Energy/Forward', None)
			self.service.add_path(prefix + '/Energy/Reverse', None)

		# Start polling
		self.update()

	@property
	def role_instance(self):
		val = self.settings['instance'].split(':')
		return val[0], int(val[1])
	
	def setting_changed(self, name, old, new):
		# Kill service, driver will restart us soon
		self.destroy()
	
	def role_changed(self, path, val):
		if val not in ['grid', 'pvinverter', 'genset', 'acload']:
			return False

		self.settings['instance'] = '%s:%s' % (val, self.role_instance[1])
		self.destroy() # restart
		return True

	def position_changed(self, path, val):
		if not 0 <= val <= 2:
			return False
		self.position.set_value(val)
		return True

	def destroy(self):
		self.cancel.cancel()
		if self.service is not None:
			self.service.__del__()

		self.service = None
		self.settings = None
		self.position = None

	def update(self):
		self.cancel.reset()
		f = Gio.File.new_for_uri(
			urlunparse(('http', self.host, '/status', '', '', '')))
		f.load_contents_async(self.cancel, self.cb, None)
		return False

	def cb(self, ob, result, userdata):
		# If the service was destroyed while a request was in flight,
		# we need to simply ignore the result
		if not self.active:
			return

		try:
			success, content, etag = ob.load_contents_finish(result)
		except GLib.GError as e:
			self.errorcount = max(0, self.errorcount - 1)
			if e.code == Gio.IOErrorEnum.CANCELLED or self.errorcount == 0:
				logger.error("Lost connection to {}".format(self.host))
				self.destroy()
			else:
				GLib.timeout_add(2000, self.update)
			return
		else:
			# Schedule the next fetch, this will only actually happen
			# after cb is completed.
			GLib.timeout_add(500, self.update)
		finally:
			self.cancel.reset()

		try:
			data = json.loads(content)
		except ValueError:
			logger.exception("Failed to parse JSON for /status")
		else:
			try:
				meters = data['emeters']
				forward = 0
				reverse = 0
				for phase, meter in zip(range(1, 4), meters):

					# Reading must be valid
					if not meter['is_valid']: continue

					self.service['/Ac/L{}/Power'.format(phase)] = meter['power']
					self.service['/Ac/L{}/Voltage'.format(phase)] = meter['voltage']

					# Not all meters send current
					if 'current' in meter:
						self.service['/Ac/L{}/Current'.format(phase)] = meter['current']
					else:
						try:
							current = meter['power'] / meter['voltage']
						except ArithmeticError:
							current = None

						self.service['/Ac/L{}/Current'.format(phase)] = current

					self.service['/Ac/L{}/Energy/Forward'.format(phase)] = meter['total']
					self.service['/Ac/L{}/Energy/Reverse'.format(phase)] = meter ['total_returned']

					forward += meter['total']
					reverse += meter['total_returned']
			except KeyError:
				logger.exception("Malformed emeters section in JSON?")
			else:
				# Simple aritmetic total. Vector-energy would have been
				# preferred but we don't have it.
				self.service['/Ac/Energy/Forward'] = forward
				self.service['/Ac/Energy/Reverse'] = reverse

			self.errorcount = MAXERROR

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
