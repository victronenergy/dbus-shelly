import asyncio
from functools import partial

from aiovelib.service import Service, IntegerItem, DoubleItem, TextItem
from aiovelib.service import TextArrayItem
from aiovelib.client import Monitor, ServiceHandler
from aiovelib.localsettings import SettingsService as SettingsClient
from aiovelib.localsettings import Setting, SETTINGS_SERVICE

try:
	from dbus_fast.aio import MessageBus
except ImportError:
	from dbus_next.aio import MessageBus

try:
	from dbus_fast.constants import BusType
except ImportError:
	from dbus_next.constants import BusType

OUTPUT_TYPE_MOMENTARY = 0
OUTPUT_TYPE_LATCHING = 1
OUTPUT_TYPE_DIMMABLE = 2

OUTPUT_FUNCTION_ALARM = 0
OUTPUT_FUNCTION_GENSET_START_STOP = 1
OUTPUT_FUNCTION_MANUAL = 2
OUTPUT_FUNCTION_TANK_PUMP = 3
OUTPUT_FUNCTION_TEMPERATURE = 4
OUTPUT_FUNCTION_CONNECTED_GENSET_HELPER_RELAY = 5

MODULE_STATE_CONNECTED = 0x100
MODULE_STATE_OVER_TEMPERATURE = 0x101
MODULE_STATE_TEMPERATURE_WARNING = 0x102
MODULE_STATE_CHANNEL_FAULT = 0x103
MODULE_STATE_CHANNEL_TRIPPED = 0x104
MODULE_STATE_UNDER_VOLTAGE = 0x105

STATUS_OFF = 0x00
STATUS_ON = 0x09
STATUS_OUTPUT_FAULT = 0x08
STATUS_DISABLED = 0x20

class SettingsMonitor(Monitor):
	def __init__(self, bus, **kwargs):
		super().__init__(bus, handlers = {
			'com.victronenergy.settings': SettingsClient
		}, **kwargs)

# Base class for all switching devices.
class SwitchDevice(object):
	service = None
	settings = None

	@classmethod
	async def create(cls, bus_type, product_id, tty="", serial="", version="", connection="", productName="Switching device", processName=""):
		""" Create a new instance of the SwitchDevice class. """
		bus = await MessageBus(bus_type=bus_type).connect()
		self = cls(bus, product_id, tty, serial, version, connection, productName, processName)
		return self

	def __init__(self, bus, product_id, tty, serial, version, connection, productName, processName):
		self._productId = product_id
		self._serial = serial
		self._tty = tty
		self.bus = bus
		self.version = version
		self.connection = connection
		self.productName = productName
		self.processName = processName
		self.serviceName = 'com.victronenergy.switch.%s_%s' % (self._tty, self._serial)

		self.service = Service(bus, self.serviceName)

	async def init(self):
		await self.wait_for_settings()

		self.service.add_item(TextItem('/Mgmt/ProcessName', self.processName))
		self.service.add_item(TextItem('/Mgmt/ProcessVersion', self.version))
		self.service.add_item(TextItem('/Mgmt/Connection', self.connection))
		self.service.add_item(IntegerItem('/ProductId', self._productId))
		self.service.add_item(TextItem('/ProductName', self.productName))
		self.service.add_item(IntegerItem('/Connected', 1))
		self.service.add_item(TextItem('/Serial', self._serial))
		self.service.add_item(TextItem('/CustomName', "", writeable=True, onchange=self._set_customname))
		self.service.add_item(IntegerItem('/State', MODULE_STATE_CONNECTED))
		self.service.add_item(IntegerItem('/DeviceInstance', int(self.settings.get_value(self.settings.alias('deviceinstance')).split(':')[-1])))

	@property
	def customname(self):
		return self.service.get_item("/CustomName").value

	@customname.setter
	def customname(self, v):
		with self.service as s:
			s["/CustomName"] = v or "Switching device"

	async def wait_for_settings(self):
		""" Attempt a connection to localsettings. """
		settingsmonitor = await SettingsMonitor.create(self.bus,
			itemsChanged=self.items_changed)
		self.settings = await asyncio.wait_for(
			settingsmonitor.wait_for_service(SETTINGS_SERVICE), 5)

		await self.settings.add_settings(
			Setting('/Settings/Devices/shelly_%s/ClassAndVrmInstance' % self._serial, "switch:50", alias="deviceinstance"),
			Setting('/Settings/Devices/shelly_%s/CustomName' % self._serial, "", alias="customname"),
		)

	def _set_customname(self, value):
		try:
			cn = self.settings.get_value(self.settings.alias("customname"))
			if cn != value:
				self.settings.set_value_async(self.settings.alias("customname"), value)
			return True
		except:
			return False

	def _value_changed(self, path, value):
		split = path.split('/')
		if len(split) > 3 and split[3] == 'Settings':
			if split[-1] == 'Type':
				if not self._set_channel_type(split[-3], value):
					return False
			setting = split[-1] + '_' + split[-3]
			try:
				self.settings.set_value_async(self.settings.alias(setting), value)
			except :
				return False
			return True

	def _set_channel_type(self, channel, value):
		return (1 << value) & self.service.get_item("/SwitchableOutput/%s/Settings/ValidTypes" % channel).value

	def _set_channel_function(self, channel, value):
		return (1 << value) & self.service.get_item("SwitchableOutput/%s/Settings/ValidFunctions" % channel).value
		
	def items_changed(self, service, values):
		try:
			self.customname = values[self.settings.alias('customname')]
		except :
			pass # Not a customname change

	async def add_output(self, channel, output_type, set_state_cb, name="", customName="", set_dimming_cb=None):
		path_base  = '/SwitchableOutput/%s/' % channel
		self.service.add_item(IntegerItem(path_base + 'State', 0, writeable=True, onchange=set_state_cb))
		self.service.add_item(IntegerItem(path_base + 'Status', 0, writeable=False, text=self._status_text_callback))
		self.service.add_item(TextItem(path_base + 'Name', name, writeable=False))

		if output_type == OUTPUT_TYPE_DIMMABLE:
			self.service.add_item(IntegerItem(path_base + 'Dimming', 0, writeable=True, onchange=set_dimming_cb, text=lambda y: str(y) + '%'))

		# Settings
		validTypesDimmable = 1 << OUTPUT_TYPE_DIMMABLE
		validTypesLatching = 1 << OUTPUT_TYPE_LATCHING
		validTypesMomentary = 1 << OUTPUT_TYPE_MOMENTARY

		self.service.add_item(TextItem(path_base + 'Settings/Group', "", writeable=True, onchange=partial(self._value_changed, path_base + 'Settings/Group')))
		self.service.add_item(TextItem(path_base + 'Settings/CustomName', customName, writeable=True, onchange=partial(self._value_changed, path_base + 'Settings/CustomName')))
		self.service.add_item(IntegerItem(path_base + 'Settings/ShowUIControl', 1, writeable=True, onchange=partial(self._value_changed, path_base + 'Settings/ShowUIControl')))
		self.service.add_item(IntegerItem(path_base + 'Settings/Type', output_type, writeable=True, onchange=partial(self._value_changed, path_base + 'Settings/Type'),
							text=self._type_text_callback))

		self.service.add_item(IntegerItem(path_base + 'Settings/ValidTypes', validTypesDimmable if
							output_type == OUTPUT_TYPE_DIMMABLE else validTypesLatching | validTypesMomentary, 
							writeable=False, text=self._valid_types_text_callback))
		self.service.add_item(IntegerItem(path_base + 'Settings/Function', OUTPUT_FUNCTION_MANUAL, writeable=True,
							onchange=partial(self._set_channel_function, channel), text=self._function_text_callback))
		self.service.add_item(IntegerItem(path_base + 'Settings/ValidFunctions', (1 << OUTPUT_FUNCTION_MANUAL), writeable=False,
							text=self._valid_functions_text_callback))

		await self.settings.add_settings(
			Setting('/Settings/Devices/shelly_%s/%s/Group' % (self._serial, channel), "", alias='Group_%s' % channel),
			Setting('/Settings/Devices/shelly_%s/%s/CustomName' % (self._serial, channel), "", alias='CustomName_%s' % channel),
			Setting('/Settings/Devices/shelly_%s/%s/ShowUIControl' % (self._serial, channel), 1, _min=0, _max=1, alias='ShowUIControl_%s' % channel),
			Setting('/Settings/Devices/shelly_%s/%s/Type' % (self._serial, channel), output_type, _min=0, _max=2, alias='Type_%s' % channel)
		)

		if output_type == OUTPUT_TYPE_DIMMABLE:
			await self.settings.add_settings(
				Setting('/Settings/%s/%s/Dimming' % (self._serial, channel), 0, _min=0, _max=100, alias='dimming_%s' % channel)
			)

		self.restore_settings(channel)

	def restore_settings(self, channel):
		try:
			with self.service as s:
				s['/SwitchableOutput/%s/Settings/Group' % channel] = self.settings.get_value(self.settings.alias('Group_%s' % channel))
				s['/SwitchableOutput/%s/Settings/CustomName' % channel] = self.settings.get_value(self.settings.alias('CustomName_%s' % channel))
				s['/SwitchableOutput/%s/Settings/ShowUIControl' % channel] = self.settings.get_value(self.settings.alias('ShowUIControl_%s' % channel))
				s['/SwitchableOutput/%s/Settings/Type' % channel] = self.settings.get_value(self.settings.alias('Type_%s' % channel))
		except :
			pass

	def _module_state_text_callback(self, value):
		if value == MODULE_STATE_CONNECTED:
			return "Connected"
		if value == MODULE_STATE_OVER_TEMPERATURE:
			return "Over temperature"
		if value == MODULE_STATE_TEMPERATURE_WARNING:
			return "Temperature warning"
		if value == MODULE_STATE_CHANNEL_FAULT:
			return "Channel fault"
		if value == MODULE_STATE_CHANNEL_TRIPPED:
			return "Channel tripped"
		if value == MODULE_STATE_UNDER_VOLTAGE:
			return "Under voltage"
		return "Unknown"

	def _status_text_callback(self, value):
		if value == 0x00:
			return "Off"
		if value == 0x09:
			return "On"
		if value == 0x02:
			return "Tripped"
		if value == 0x04:
			return "Over temperature"
		if value == 0x01:
			return "Powered"
		if value == 0x08:
			return "Output fault"
		if value == 0x10:
			return "Short fault"
		if value == 0x20:
			return "Disabled"
		return "Unknown"

	def _type_text_callback(self, value):
		if value == OUTPUT_TYPE_MOMENTARY:
			return "Momentary"
		if value == OUTPUT_TYPE_LATCHING:
			return "Latching"
		if value == OUTPUT_TYPE_DIMMABLE:
			return "Dimmable"
		return "Unknown"

	def _function_text_callback(self, value):
		if value == OUTPUT_FUNCTION_ALARM:
			return "Alarm"
		if value == OUTPUT_FUNCTION_GENSET_START_STOP:
			return "Genset start stop"
		if value == OUTPUT_FUNCTION_MANUAL:
			return "Manual"
		if value == OUTPUT_FUNCTION_TANK_PUMP:
			return "Tank pump"
		if value == OUTPUT_FUNCTION_TEMPERATURE:
			return "Temperature"
		if value == OUTPUT_FUNCTION_CONNECTED_GENSET_HELPER_RELAY:
			return "Connected genset helper relay"
		return "Unknown"

	def _valid_types_text_callback(self, value):
		str = ""
		if value & (1 << OUTPUT_TYPE_DIMMABLE):
			str += "Dimmable"
		if value & (1 << OUTPUT_TYPE_LATCHING):
			if str:
				str += ", "
			str += "Latching"
		if value & (1 << OUTPUT_TYPE_MOMENTARY):
			if str:
				str += ", "
			str += "Momentary"
		return str

	def _valid_functions_text_callback(self, value):
		str = ""
		if value & (1 << OUTPUT_FUNCTION_ALARM):
			str += "Alarm"
		if value & (1 << OUTPUT_FUNCTION_GENSET_START_STOP):
			if str:
				str += ", "
			str += "Genset start stop"
		if value & (1 << OUTPUT_FUNCTION_MANUAL):
			if str:
				str += ", "
			str += "Manual"
		if value & (1 << OUTPUT_FUNCTION_TANK_PUMP):
			if str:
				str += ", "
			str += "Tank pump"
		if value & (1 << OUTPUT_FUNCTION_TEMPERATURE):
			if str:
				str += ", "
			str += "Temperature"
		if value & (1 << OUTPUT_FUNCTION_CONNECTED_GENSET_HELPER_RELAY):
			if str:
				str += ", "
			str += "Connected genset helper relay"
		return str

	def _handle_changed_value(self, path, oldvalue, newvalue):
		raise NotImplementedError("This method should be overridden in a subclass")
		return True

	def _handle_changed_setting(self, path, oldvalue, newvalue):
		return True