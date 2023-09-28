import asyncio
import logging

from dbus_next.aio import MessageBus

from __main__ import VERSION
from __main__ import __file__ as MAIN_FILE

from aiovelib.service import Service, IntegerItem, DoubleItem, TextItem
from aiovelib.service import TextArrayItem
from aiovelib.client import Monitor, ServiceHandler
from aiovelib.localsettings import SettingsService, Setting, SETTINGS_SERVICE

DBUS_SETTING_PREFIX = '/Settings/Devices/'

logger = logging.getLogger(__name__)

class LocalSettings(SettingsService, ServiceHandler):
	pass
		
# Text formatters
unit_watt = lambda v: f"{v:.0f}W"
unit_volt = lambda v: f"{v:.1f}V"
unit_amp = lambda v: f"{v:.1f}A"
unit_kwh = lambda v: f"{v:.2f}kWh"
unit_productid = lambda v: f"0x{v:X}"

class Meter(object):

	DBUS_SETTING_PREFIX = '/Settings/Devices/'

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
			logger.info(f"Received DeviceInfo {data}")
			mac = data['result']['mac']
			device_id = data['result']['id']
			app_name = data['result']['app']
			device_name = data['result'].get("name", app_name)
			# fw = data['result']['fw_id']
			fw = data['result']['ver'] # Use software version in place of full firmware
			logger.info(f"Setup meter for device {device_name} {fw}")
		except KeyError:
			return False

		# Connect to dbus, localsettings
		bus = await MessageBus(bus_type=self.bus_type).connect()
		self.monitor = await Monitor.create(bus, self.settings_changed)

		settingprefix =  f"{self.DBUS_SETTING_PREFIX}{device_id.replace('-', '_')}"
		logger.info("Waiting for localsettings")
		settings = await self.wait_for_settings()
		if settings is None:
			logger.error("Failed to connect to localsettings")
			return False

		logger.info("Connected to localsettings")

		await settings.add_settings(
			Setting(f"{settingprefix}/ClassAndVrmInstance", "grid:40", 0, 0, alias="instance"),
			Setting(f"{settingprefix}/Position", 0, 0, 2, alias="position")
		)

		# Determine role and instance
		role, instance = self.role_instance(
			settings.get_value(settings.alias("instance")))

		# Set up the service
		self.service = await Service.create(bus, f"com.victronenergy.{role}.shelly_{mac}")

		self.service.add_item(TextItem('/Mgmt/ProcessName', MAIN_FILE))
		self.service.add_item(TextItem('/Mgmt/ProcessVersion', VERSION))
		self.service.add_item(TextItem('/Mgmt/Connection', f"WebSocket {host}:{port}"))
		self.service.add_item(IntegerItem('/DeviceInstance', instance))
		self.service.add_item(IntegerItem('/ProductId', 0xB034, text=unit_productid))
		self.service.add_item(TextItem('/ProductName', f"Shelly {app_name}"))
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
		if app_name.endswith("3EM"):
			logger.info("Setup 3-phase metrics")
			for prefix in (f"/Ac/L{x}" for x in range(1, 4)):
				self.service.add_item(DoubleItem(f"{prefix}/Voltage", None, text=unit_volt))
				self.service.add_item(DoubleItem(f"{prefix}/Current", None, text=unit_amp))
				self.service.add_item(DoubleItem(f"{prefix}/Power", None, text=unit_watt))
				self.service.add_item(DoubleItem(f"{prefix}/Energy/Forward", None, text=unit_kwh))
				self.service.add_item(DoubleItem(f"{prefix}/Energy/Reverse", None, text=unit_kwh))
		else:
			logger.info("Setup single phase metrics")
			self.service.add_item(DoubleItem("/Ac/L1/Voltage", None, text=unit_volt))
			self.service.add_item(DoubleItem("/Ac/L1/Current", None, text=unit_amp))
			self.service.add_item(DoubleItem("/Ac/L1/Power", None, text=unit_watt))

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
			except KeyError:
				pass

			try:
				d = data['params']['emdata:0']
				with self.service as s:
					s["/Ac/Energy/Forward"] = round(d["total_act"]/1000, 1)
					s["/Ac/Energy/Reverse"] = round(d["total_act_ret"]/1000, 1)
					s["/Ac/L1/Energy/Forward"] = round(d["a_total_act_energy"]/1000, 1)
					s["/Ac/L1/Energy/Reverse"] = round(d["a_total_act_ret_energy"]/1000, 1)
					s["/Ac/L2/Energy/Forward"] = round(d["b_total_act_energy"]/1000, 1)
					s["/Ac/L2/Energy/Reverse"] = round(d["b_total_act_ret_energy"]/1000, 1)
					s["/Ac/L3/Energy/Forward"] = round(d["c_total_act_energy"]/1000, 1)
					s["/Ac/L3/Energy/Reverse"] = round(d["c_total_act_ret_energy"]/1000, 1)

			except KeyError:
				pass

			try: # plus 1PM
				d = data['params']['switch:0']["aenergy"]
				with self.service as s:
					s["/Ac/Energy/Forward"] = round(d["total"]/1000, 1)
			except KeyError:
				pass

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
		settings.set_value(p, f"{val}:{instance}")

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
