import asyncio
import logging
from asyncio.exceptions import TimeoutError # Deprecated in 3.11

from dbus_next.aio import MessageBus

from __main__ import VERSION
from __main__ import __file__ as MAIN_FILE

from aiovelib.service import Service, IntegerItem, DoubleItem, TextItem
from aiovelib.service import TextArrayItem
from aiovelib.client import Monitor, ServiceHandler
from aiovelib.localsettings import SettingsService, Setting, SETTINGS_SERVICE

logger = logging.getLogger(__name__)

class LocalSettings(SettingsService, ServiceHandler):
	pass
		
# Text formatters
unit_watt = lambda v: "{:.0f}W".format(v)
unit_volt = lambda v: "{:.1f}V".format(v)
unit_amp = lambda v: "{:.1f}A".format(v)
unit_kwh = lambda v: "{:.2f}kWh".format(v)
unit_productid = lambda v: "0x{:X}".format(v)

class Meter(object):
	def __init__(self, bus_type):
		self.bus_type = bus_type
		self.monitor = None
		self.service = None
		self.position = None
		self.destroyed = False

	async def wait_for_settings(self):
		""" Attempt a connection to localsettings. If it does not show
		    up within 5 seconds, return None. """
		try:
			return await asyncio.wait_for(
				self.monitor.wait_for_service(SETTINGS_SERVICE), 5)
		except TimeoutError:
			pass

		return None
	
	def get_settings(self):
		""" Non-async version of the above. Return the settings object
		    if known. Otherwise return None. """
		return self.monitor.get_service(SETTINGS_SERVICE)

	async def start(self, host, port, data):
		try:
			mac = data['result']['mac']
			fw = data['result']['fw_id']
		except KeyError:
			return False

		# Connect to dbus, localsettings
		bus = await MessageBus(bus_type=self.bus_type).connect()
		self.monitor = await Monitor.create(bus, self.settings_changed)

		settingprefix = '/Settings/Devices/shelly_' + mac
		logger.info("Waiting for localsettings")
		settings = await self.wait_for_settings()
		if settings is None:
			logger.error("Failed to connect to localsettings")
			return False

		logger.info("Connected to localsettings")

		await settings.add_settings(
			Setting(settingprefix + "/ClassAndVrmInstance", "grid:40", 0, 0, alias="instance"),
			Setting(settingprefix + '/Position', 0, 0, 2, alias="position")
		)

		# Determine role and instance
		role, instance = self.role_instance(
			settings.get_value(settings.alias("instance")))

		# Set up the service
		self.service = await Service.create(bus, "com.victronenergy.{}.shelly_{}".format(role, mac))

		self.service.add_item(TextItem('/Mgmt/ProcessName', MAIN_FILE))
		self.service.add_item(TextItem('/Mgmt/ProcessVersion', VERSION))
		self.service.add_item(TextItem('/Mgmt/Connection', f"WebSocket {host}:{port}"))
		self.service.add_item(IntegerItem('/DeviceInstance', instance))
		self.service.add_item(IntegerItem('/ProductId', 0xB034, text=unit_productid))
		self.service.add_item(TextItem('/ProductName', "Shelly energy meter"))
		self.service.add_item(TextItem('/FirmwareVersion', fw))
		self.service.add_item(IntegerItem('/Connected', 1))
		self.service.add_item(IntegerItem('/RefreshTime', 100))

		# Role
		self.service.add_item(TextArrayItem('/AllowedRoles',
			['grid', 'pvinverter', 'genset', 'acload']))
		self.service.add_item(TextItem('/Role', role, writeable=True,
			onchange=self.role_changed))

		# Position for pvinverter
		if role == 'pvinverter':
			self.service.add_item(IntegerItem('/Position',
				settings.get_value(settings.alias("position")),
				writeable=True, onchange=self.position_changed))

		# Meter paths
		self.service.add_item(DoubleItem('/Ac/Energy/Forward', None, text=unit_kwh))
		self.service.add_item(DoubleItem('/Ac/Energy/Reverse', None, text=unit_kwh))
		self.service.add_item(DoubleItem('/Ac/Power', None, text=unit_watt))
		for prefix in (f"/Ac/L{x}" for x in range(1, 4)):
			self.service.add_item(DoubleItem(prefix + '/Voltage', None, text=unit_volt))
			self.service.add_item(DoubleItem(prefix + '/Current', None, text=unit_amp))
			self.service.add_item(DoubleItem(prefix + '/Power', None, text=unit_watt))
			self.service.add_item(DoubleItem(prefix + '/Energy/Forward', None, text=unit_kwh))
			self.service.add_item(DoubleItem(prefix + '/Energy/Reverse', None, text=unit_kwh))

		return True

	def destroy(self):
		if self.service is not None:
			self.service.__del__()
		self.service = None
		self.settings = None
		self.destroyed = True
	
	async def update(self, data):
		# NotifyStatus has power, current, voltage and energy values
		if self.service and data.get('method') == 'NotifyStatus':
			try:
				d = data['params']['em:0']
			except KeyError:
				pass
			else:
				with self.service as s:
					s['/Ac/L1/Voltage'] = d["a_voltage"]
					s['/Ac/L2/Voltage'] = d["b_voltage"]
					s['/Ac/L3/Voltage'] = d["c_voltage"]
					s['/Ac/L1/Current'] = d["a_current"]
					s['/Ac/L2/Current'] = d["b_current"]
					s['/Ac/L3/Current'] = d["c_current"]
					s['/Ac/L1/Power'] = d["a_act_power"]
					s['/Ac/L2/Power'] = d["b_act_power"]
					s['/Ac/L3/Power'] = d["c_act_power"]

					s['/Ac/Power'] = d["a_act_power"] + d["b_act_power"] + d["c_act_power"]

			try:
				d = data['params']['emdata:0']
			except KeyError:
				pass
			else:
				with self.service as s:
					s["/Ac/Energy/Forward"] = d["total_act"]
					s["/Ac/Energy/Reverse"] = d["total_act_ret"]
					s["/Ac/L1/Energy/Forward"] = d["a_total_act_energy"]
					s["/Ac/L1/Energy/Reverse"] = d["a_total_act_ret_energy"]
					s["/Ac/L2/Energy/Forward"] = d["b_total_act_energy"]
					s["/Ac/L2/Energy/Reverse"] = d["b_total_act_ret_energy"]
					s["/Ac/L3/Energy/Forward"] = d["c_total_act_energy"]
					s["/Ac/L3/Energy/Reverse"] = d["c_total_act_ret_energy"]

	def role_instance(self, value):
		val = value.split(':')
		return val[0], int(val[1])

	def settings_changed(self, service, values):
		# Kill service, driver will restart us soon
		if service.alias("instance") in values:
			self.destroy()

	def role_changed(self, val):
		if val not in ['grid', 'pvinverter', 'genset', 'acload']:
			return False

		settings = self.get_settings()
		if settings is None:
			return False

		p = settings.alias("instance")
		role, instance = self.role_instance(settings.get_value(p))
		settings.set_value(p, "{}:{}".format(val, instance))

		self.destroy() # restart
		return True

	def position_changed(self, val):
		if not 0 <= val <= 2:
			return False

		settings = self.get_settings()
		if settings is None:
			return False

		settings.set_value(settings.alias("position"), val)
		return True
