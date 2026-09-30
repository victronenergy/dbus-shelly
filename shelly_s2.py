from __future__ import annotations

import sys
import os
import asyncio
import logging
import importlib.util
from functools import partial
import uuid
from datetime import datetime, timezone

#aiovelib
sys.path.insert(1, os.path.join(os.path.dirname(__file__), 'ext', 'aiovelib'))
from aiovelib.service import IntegerItem
from aiovelib.localsettings import Setting

# s2python (and aiovelib.s2, which depends on it) is only imported from
# shelly_s2_control once S2 is actually enabled, to keep its memory
# footprint out of the service when no opportunity loads are configured.
# Still fail here if it is missing, so shelly_handlers can disable S2 up front.
if importlib.util.find_spec('s2python') is None:
	raise ImportError("s2python not available")

from __main__ import VERSION
from utils import formatters as fmt, OutputFunction, OutputType

logger = logging.getLogger('switch-device-rm')
background_tasks = set()
_s2_control_loader = None

async def _load_s2_control():
	'''
		Import shelly_s2_control (and s2python) in a worker thread, so the event loop keeps running.
		All callers share one import, so channels enabling S2 at the same time wait for the same load.
	'''
	global _s2_control_loader
	if _s2_control_loader is None:
		_s2_control_loader = asyncio.ensure_future(asyncio.to_thread(importlib.import_module, 'shelly_s2_control'))
	return await _s2_control_loader

class ShellyHandlerS2Mixin():

	@property
	def power(self):
		return (self.service.get_item(f'/Ac/L1/Power').value or 0) + (self.service.get_item(f'/Ac/L2/Power').value or 0) + (self.service.get_item(f'/Ac/L3/Power').value or 0)

	@property
	def on_hysteresis(self):
		return self.service.get_item(f'/S2/0/RmSettings/OnHysteresis').value or 0

	@property
	def off_hysteresis(self):
		return self.service.get_item(f'/S2/0/RmSettings/OffHysteresis').value or 0

	@property
	def power_setting(self):
		return self.service.get_item(f'/S2/0/RmSettings/PowerSetting').value or 0

	@property
	def phase(self):
		return self.service.get_item(f'/PhaseSetting').value or 0

	@property
	def s2_active(self):
		return self.service.get_item(f'/S2/0/Active').value or 0

	@s2_active.setter
	def s2_active(self, value):
		item = self.service.get_item(f'/S2/0/Active')
		if item:
			item.set_local_value(value)

	@property
	def has_rm(self):
		return self.service.get_item("/S2/0/Rm") is not None

	async def ainit(self):
		self._ol_supported = False
		await super().ainit()
		self.rm_item = None
		self._ol_supported = self._has_em and self._em_role != 'pvinverter'

		if not self._ol_supported:
			return

		# Created on first use in enable_rm(), so s2python is only loaded when needed.
		self._control_type_ombc = None
		self._control_type_noctrl = None

		# Indicates if the RM is enabled, i.e., the OMBC control type has been offered to HEMS.
		# Whether the OMBC control type is actually activated by the HEMS is determined by self._control_type_ombc.active.
		self._rm_enabled = False

		# Lock to serialize S2 message sends to CEM
		self._s2_message_lock = asyncio.Lock()

		self._valid_functions_mask |= (1 << OutputFunction.OPPORTUNITY_LOAD)
		with self.service as s:
			s[f'/SwitchableOutput/{self._channel_id}/Settings/ValidFunctions'] = self._valid_functions_mask

		# Setup channel function
		self.on_channel_function_changed(self._channel_id, self._function)

	def on_channel_function_changed(self, channel, value):
		if not self._ol_supported:
			return
		self._function = value
		asyncio.create_task(self._handle_channel_function_changed(channel, value))

	async def _handle_channel_function_changed(self, channel, value):
		if value == OutputFunction.OPPORTUNITY_LOAD:
			# Disallow the 'pvinverter' role when the function is set to OL.
			self.set_allowed_roles([role for role in self.allowed_em_roles if role != 'pvinverter'])

			# Check the /Group name. If it is empty, set it to "Opportunity Loads" by default.
			# This does NOT write "Opportunity Loads" to localsettings, it only sets the value of the dbus item.
			# When /Group is written from the GUI/dbus, it will be stored in localsettings and also to the dbus item.
			group_item = self.service.get_item(f'/SwitchableOutput/{channel}/Settings/Group')
			if group_item and not group_item.value:
				group_item.set_local_value("Opportunity Loads")

			# Set type to three-state switch
			await self._set_type_to_three_state_switch(True)

			# Get current value of the /Auto path
			auto_item = self.service.get_item(f'/SwitchableOutput/{channel}/Auto')
			auto_value = auto_item.value if auto_item else 0

			logger.info(f"Enabling S2 Resource Manager for device {self._serial}, channel {channel}")
			await self.enable_rm(channel, auto_value)

		else:
			# Restore allowed EM roles when the function is no longer OL.
			self.set_allowed_roles(self.allowed_em_roles + (['pvinverter'] if 'pvinverter' not in self.allowed_em_roles else []))
			# Restore group name from settings to be sure
			# If the group name was empty before the function was set to OL, it will be restored to an empty string here.
			group = self.settings.get_value(self.settings.alias(f'Group_{self._serial}_{self._channel_id}'))
			group_item = self.service.get_item(f'/SwitchableOutput/{channel}/Settings/Group')
			if group_item:
				group_item.set_local_value(group)

			# Disable RM if it was running
			if self._rm_enabled:
				await self.disable_rm()
			# Set type back to default
			await self._set_type_to_three_state_switch(False)

	async def enable_rm(self, channel, enabled):
		if self._control_type_ombc is None:
			try:
				await _load_s2_control()
			except Exception:
				logger.exception("Failed to load S2 support, cannot enable S2 Resource Manager for device %s", self._serial)
				return
			from shelly_s2_control import ShellyOMBC, ShellyNOCTRL
			self._control_type_ombc = ShellyOMBC(self)
			self._control_type_noctrl = ShellyNOCTRL(self)

		control_types = [self._control_type_noctrl]
		if enabled:
			self._control_type_ombc.enabled = True
			control_types.append(self._control_type_ombc)
		else:
			self._control_type_ombc.enabled = False

		logger.info(f"Shelly RM: Device {self._serial}, channel {channel}, offering control types: {[type(ct).__name__ for ct in control_types]}")
		# Paths not yet present.
		if not self.has_rm:
			await self._add_rm_to_service(channel, control_types)
		else:
			# Update rm details
			self._update_asset_details()

		# Explicitly set initial values to force an items changed
		power_setting = self.settings.get_value(self.settings.alias(f'PowerSetting_{self._serial}_{channel}'))
		on_hysteresis = self.settings.get_value(self.settings.alias(f'OnHysteresis_{self._serial}_{channel}'))
		off_hysteresis = self.settings.get_value(self.settings.alias(f'OffHysteresis_{self._serial}_{channel}'))
		with self.service as s:
			s['/S2/0/Active'] = 0
			s['/S2/0/RmSettings/PowerSetting'] = power_setting
			s['/S2/0/RmSettings/OnHysteresis'] = on_hysteresis
			s['/S2/0/RmSettings/OffHysteresis'] = off_hysteresis

		# Let the CEM know the RM is ready to connect.
		await self.rm_item.set_ready(True, control_types=control_types, asset_details=self._rm_details)
		if self.rm_item.is_connected:
			await self.rm_item.send_resource_manager_details(control_types=control_types, asset_details=self._rm_details)

		self._rm_enabled = True

	async def _auto_changed(self, item, value):
		if not self._ol_supported:
			return
		# Store setting
		setting = f'Auto_{self._serial}_{self._channel_id}'
		try:
			await self.settings.set_value(self.settings.alias(setting), value)
			await self.enable_rm(self._channel_id, value)
		except:
			return
		item.set_local_value(value)

	async def _set_type_to_three_state_switch(self, enabled):
		if enabled:
			if self.settings.alias(f'Auto_{self._serial}_{self._channel_id}') is None:
				await self.settings.add_settings(Setting(f'{self._settings_base}{self._channel_id}/Auto', 0,
											 _min=0, _max=1, alias=f"Auto_{self._serial}_{self._channel_id}"))
			if self.service.get_item(f'/SwitchableOutput/{self._channel_id}/Auto') is None:
				# Add Auto item
				self.service.add_item(IntegerItem(f'/SwitchableOutput/{self._channel_id}/Auto', None, writeable=True, onchange=self._auto_changed))

			init_val = self.settings.get_value(self.settings.alias(f'Auto_{self._serial}_{self._channel_id}')) or 0
			# This sends an itemschanged, to make the GUI aware of it.
			with self.service as s:
				s[f'/SwitchableOutput/{self._channel_id}/Auto'] = init_val
		else:
			try:
				with self.service as s:
					s[f'/SwitchableOutput/{self._channel_id}/Auto'] = None
			except KeyError:
				# Item did not exist, ignore
				pass

		with self.service as s:
			s[f'/SwitchableOutput/{self._channel_id}/Settings/ValidTypes'] = (1 << OutputType.THREE_STATE_SWITCH) if enabled else self._valid_types_mask

		# Set type and invoke the callback
		item = self.service.get_item(f'/SwitchableOutput/{self._channel_id}/Settings/Type')
		if item:
			item.set_value(OutputType.THREE_STATE_SWITCH if enabled else self._default_output_type)

	async def disable_rm(self):
		from shelly_s2_control import PowerMeasurement, PowerValue, phase_setting_to_commodity
		logger.info("Disabling S2 Resource Manager for device %s", self._serial)
		# Let the HEMS know the RM is disabled by updating the allowed control types to only NoControl.
		#        For now, let's simply completly disconnect and see if that works out fine.
		try:
			#make sure to send a 0 power measurement to EMS as well.
			await self._send_s2_message(
				PowerMeasurement(
					message_id=uuid.uuid4(),
					measurement_timestamp=datetime.now(timezone.utc),
					values=[
						PowerValue(
							commodity_quantity = phase_setting_to_commodity(self.phase),
							value=0
						)
					]
				)
			)
		except:
			# Will throw when the HEMS is not connected, but will still update the available control types.
			# So next time HEMS connects, it will only be offered the NoControl control type.
			pass
		finally:
			# Close S2 connection
			# Updating the allowed control types is probably not needed here since the S2 connection will be closed anyways.
			await self.rm_item.set_ready(False, control_types=[self._control_type_noctrl], asset_details=self._rm_details)
			if self.rm_item.is_connected:
				await self.rm_item.send_resource_manager_details(control_types=[self._control_type_noctrl], asset_details=self._rm_details)

			# Clear S2 paths
			with self.service as s:
				s['/S2/0/Active'] = None
				s['/S2/0/RmSettings/PowerSetting'] = None
				s['/S2/0/RmSettings/OnHysteresis'] = None
				s['/S2/0/RmSettings/OffHysteresis'] = None

		self._rm_enabled = False

	def update(self, status_json, cap=None):
		super().update(status_json, cap)
		if not self._ol_supported:
			return
		if self._rm_enabled and self._control_type_ombc is not None and self._control_type_ombc.active:
			# Pull relevant values from the device and forward to the control type
			self._control_type_ombc.values_changed({'Status': self.status,'Power': self.power,})

	async def _value_changed(self, path, item, value):
		ret = await super()._value_changed(path, item, value)

		if not self._ol_supported:
			return
		if path.endswith('/PhaseSetting') and ret and self._rm_enabled and self._control_type_ombc.active:
			logger.info("Phase setting changed, updating OMBC system description")
			task = asyncio.create_task(self._control_type_ombc.send_system_description())
			background_tasks.add(task)
			task.add_done_callback(background_tasks.discard)

	def custom_name_changed(self, value):
		# Update asset details when the RM is enabled, regardless of what control types are offered (i.e. /Auto is enabled or not).
		if self.has_rm and self._rm_enabled:
			self._update_asset_details(value)
			task = asyncio.create_task(self.send_resource_manager_details())
			background_tasks.add(task)
			task.add_done_callback(background_tasks.discard)

	async def _s2_value_changed(self, path, item, value):
		split = path.split('/')
		if len(split) > 2 and split[1] == 'S2':
			setting = f'{split[-1]}_{self._serial}_{self._channel_id}'
			try:
				await self.settings.set_value(self.settings.alias(setting), value)
			except:
				return

			item.set_local_value(value)

			# Update OMBC system description when (relevant)power setting changes.
			relevant_settings = ["PowerSetting", "OnHysteresis", "OffHysteresis"]
			if split[-1] in relevant_settings and self._rm_enabled and self._control_type_ombc.active:
				logger.info(f"setting changed: {split[-1]}, updating OMBC system description")
				task = asyncio.create_task(self._control_type_ombc.send_system_description())
				background_tasks.add(task)
				task.add_done_callback(background_tasks.discard)

	async def _add_rm_to_service(self, channel, control_types):
		# Paths will be added once when the function is set to S2 resource manager.
		# After that, enabling/disabling the RM will only update the allowed control types.
		from shelly_s2_control import S2ResourceManagerItem
		logger.info("Adding S2 Resource Manager paths to service")

		# Add settings paths
		settings_base = self._settings_base + f'{channel}/S2/'
		await self.settings.add_settings(
			Setting(settings_base + 'PowerSetting', 1000, alias=f'PowerSetting_{self._serial}_{channel}'),
			Setting(settings_base + 'Phase', 1, _min=0, _max=3, alias=f'Phase_{self._serial}_{channel}'), #Phase 0 will map to a 3 phased symmetric load.
			Setting(settings_base + 'OnHysteresis', 30, _min=0, _max=999999, alias=f'OnHysteresis_{self._serial}_{channel}'),
			Setting(settings_base + 'OffHysteresis', 30, _min=0, _max=999999, alias=f'OffHysteresis_{self._serial}_{channel}')
		)

		path_base = "/S2/0/"
		path_base_settings = "/S2/0/RmSettings/"
		self.service.add_item(IntegerItem(path_base + 'Active'))
		self.service.add_item(IntegerItem(path_base_settings + 'PowerSetting', writeable=True, onchange=partial(self._s2_value_changed, path_base_settings + 'PowerSetting'), text=fmt['watt']))
		self.service.add_item(IntegerItem(path_base_settings + 'OnHysteresis', writeable=True, onchange=partial(self._s2_value_changed, path_base_settings + 'OnHysteresis')))
		self.service.add_item(IntegerItem(path_base_settings + 'OffHysteresis', writeable=True, onchange=partial(self._s2_value_changed, path_base_settings + 'OffHysteresis')))

		self._update_asset_details()

		self.rm_item = S2ResourceManagerItem(
			'/S2/0/Rm',
			control_types=control_types,
			asset_details=self._rm_details
		)

		self.service.add_item(self.rm_item)

	def _update_asset_details(self, name=None):
		from shelly_s2_control import AssetDetails, Duration, Role, RoleType, phase_setting_to_commodity
		if name is None:
			# Get channel custom name, if not available use the default name
			name = self.service.get_item(f'/SwitchableOutput/{self._channel_id}/Settings/CustomName').value or \
				self.service.get_item(f'/SwitchableOutput/{self._channel_id}/Name').value

		self._rm_details = AssetDetails(
			resource_id=uuid.uuid4(),
			provides_forecast=False,
			provides_power_measurements=[phase_setting_to_commodity(self.phase or 0)],
			instruction_processing_delay=Duration(0),
			roles=[Role(role=RoleType.ENERGY_CONSUMER, commodity='ELECTRICITY')],
			name=name,
			manufacturer="Shelly",
			firmware_version=VERSION,
			serial_number=self._serial
		)

	async def send_resource_manager_details(self):
		if self.rm_item and self.rm_item.is_connected:
			try:
				await self.rm_item.send_resource_manager_details(control_types=[self._control_type_ombc, self._control_type_noctrl], asset_details=self._rm_details)
			except Exception as e:
				logger.error("Failed to send resource manager details: %s", e)

	async def _send_s2_message(self, msg):
		"""Send S2 message with lock to serialize concurrent sends to CEM."""
		async with self._s2_message_lock:
			return await self.rm_item.send_msg_and_await_reception_status(msg)
