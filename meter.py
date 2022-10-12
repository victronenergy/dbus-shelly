MAXERROR = 5

from __main__ import VERSION
from __main__ import __file__ as MAIN_FILE

import logging
from urllib.parse import urlunparse
import threading

from gi.repository import GLib, Gio
import requests

from vedbus import VeDbusService
from settingsdevice import SettingsDevice

logger = logging.getLogger()

class AsyncHttpRequest(threading.Thread):
	def __init__(self, session, url, cb, errhandler, *args, **kwargs):
		super(AsyncHttpRequest, self).__init__(*args, **kwargs)
		self.session = session
		self.url = url
		self.cb = cb
		self.error = errhandler

	def run(self):
		try:
			r = self.session.get(self.url, timeout=2)
		except OSError as e:
			if self.error is not None:
				GLib.idle_add(self.error, e)
			else:
				logger.error("Unable to fetch {}".format(self.url))
		else:
			GLib.idle_add(self.cb, r)

class Meter(object):
	def __init__(self, get_bus, host):
		self.starting = False
		self.host = host
		self.get_bus = get_bus
		self.service = None
		self.errorcount = MAXERROR
		self.settings = None
		self.position = None
		self.session = requests.Session()
	
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
		AsyncHttpRequest(self.session,
			urlunparse(('http', self.host, '/shelly', '', '', '')),
			self.register, self.start_error).start()
		self.starting = True

	def start_error(self, e):
		logger.error("Failed to read /shelly for {}".format(self.host))
		self.starting = False

	def register(self, response):
		self.starting = False
		try:
			data = response.json()
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
			"com.victronenergy.{}.shelly_{}".format(role, name), bus=bus)
		self.service.add_path('/Mgmt/ProcessName', MAIN_FILE)
		self.service.add_path('/Mgmt/ProcessVersion', VERSION)
		self.service.add_path('/Mgmt/Connection', self.host)
		self.service.add_path('/DeviceInstance', instance)
		self.service.add_path('/ProductId', 0xB034)
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
		self.service.add_path('/Ac/Power', None)
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
		if self.service is not None:
			self.service.__del__()

		self.service = None
		self.settings = None
		self.position = None

	def update(self):
		AsyncHttpRequest(self.session,
			urlunparse(('http', self.host, '/status', '', '', '')),
			self.cb, self.update_error).start()
		return False

	def update_error(self, e):
		self.errorcount = max(0, self.errorcount - 1)
		if self.errorcount == 0:
			logger.error("Lost connection to {}".format(self.host))
			self.destroy()
		else:
			GLib.timeout_add(2000, self.update)

	def cb(self, response):
		# If the service was destroyed while a request was in flight,
		# we need to simply ignore the result
		if not self.active:
			return

		# Schedule the next fetch, this will only actually happen
		# after cb is completed. Do it now so it doesn't get lost
		# due to an exception below
		GLib.timeout_add(500, self.update)

		try:
			data = response.json()
		except ValueError:
			logger.exception("Failed to parse JSON for /status")
		else:
			try:
				meters = data['emeters']
				forward = 0
				reverse = 0
				power = None
				for phase, meter in zip(range(1, 4), meters):

					# Reading must be valid
					if not meter['is_valid']: continue

					power = meter['power'] + (0 if power is None else power)
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
				self.service['/Ac/Power'] = power
				# Simple aritmetic total. Vector-energy would have been
				# preferred but we don't have it.
				self.service['/Ac/Energy/Forward'] = forward
				self.service['/Ac/Energy/Reverse'] = reverse

			self.errorcount = MAXERROR
