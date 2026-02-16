from __future__ import annotations

import sys
import os
import asyncio
import logging
from functools import partial
import uuid
from datetime import datetime, timezone
import weakref

try:
	from dbus_fast.aio import MessageBus
	from dbus_fast import Variant
except ImportError:
	from dbus_next import MessageBus, Variant

#aiovelib
sys.path.insert(1, os.path.join(os.path.dirname(__file__), 'ext', 'aiovelib'))
from aiovelib.service import IntegerItem
from aiovelib.localsettings import Setting
from aiovelib.client import Monitor, ServiceHandler, Service as Client

from s2 import S2ResourceManagerItem
from s2python.s2_control_type import NoControlControlType, OMBCControlType
from s2python.s2_asset_details import AssetDetails
from s2python.generated.gen_s2 import CommodityQuantity, RoleType
from s2python.common.power_range import PowerRange
from s2python.common.transition import Transition
from s2python.common.timer import Timer

from s2python.common import (
	ReceptionStatusValues,
	Role,
	Duration,
	PowerMeasurement,
	PowerValue,
)

from s2python.ombc import (
	OMBCInstruction,
	OMBCOperationMode,
	OMBCStatus,
	OMBCSystemDescription,
)

from __main__ import VERSION
from utils import STATUS_ON, formatters as fmt, OutputFunction, OutputType

logger = logging.getLogger('switch-device-rm')
logger.setLevel(logging.DEBUG)
background_tasks = set()

def phase_setting_to_commodity(phase:int)-> CommodityQuantity:
	'''
		translates the phase setting 0-3 to the CommodityQuanity
	'''
	if phase == 0: return CommodityQuantity.ELECTRIC_POWER_3_PHASE_SYMMETRIC
	if phase == 1: return CommodityQuantity.ELECTRIC_POWER_L1
	if phase == 2: return CommodityQuantity.ELECTRIC_POWER_L2
	if phase == 3: return CommodityQuantity.ELECTRIC_POWER_L3

	#invalid? Default to 0. That's the smallest error on every phase.
	return CommodityQuantity.ELECTRIC_POWER_3_PHASE_SYMMETRIC

class ShellyHandlerS2Mixin():
	# Reference to the ServiceMonitor instance
	monitor: ServiceMonitor = None

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
		self.rm_item = None
		self._control_type_ombc = None
		self._control_type_noctrl = None

		# Indicates if the RM is enabled, i.e., the OMBC control type has been offered to HEMS.
		# Whether the OMBC control type is actually activated by the HEMS is determined by self._control_type_ombc.active.
		self._rm_enabled = False

		# OpportunityLoads needs energy metering capability.
		if await self.em_supported():
			self._valid_functions_mask |= (1 << OutputFunction.OPPORTUNITY_LOAD)
		else:
			self._valid_functions_mask &= ~(1 << OutputFunction.OPPORTUNITY_LOAD)

		await super().ainit()

	def on_channel_function_changed(self, channel, value):
		self._function = value
		if value == OutputFunction.OPPORTUNITY_LOAD:
			# Enable S2 RM
			asyncio.create_task(self.enable_rm(channel))
		else:
			# Disable S2 RM
			if self._rm_enabled:
				asyncio.create_task(self.disable_rm(channel))

	async def enable_rm(self, channel):
		logger.info("Enabling S2 Resource Manager for device %s", self._serial)
		if not self.has_rm:
			# Paths not yet present, add them to the service.
			await self._add_rm_to_service(channel)

			#FIXME: Better read the current state, and just report that, so a restart of S2 just continues where it was.
			logger.debug("Setting output state off initially")
			self.state = 0

		else:
			# Let the HEMS know the RM is enabled by updating the allowed control types.
			try:
				await self.rm_item.send_resource_manager_details(control_types=[self._control_type_noctrl, self._control_type_ombc], asset_details=self._rm_details)
			except:
				pass

		# Set switch type to Three-state switch (9)
		await self._set_three_state_switch(True)
		self._rm_enabled = True

	async def _auto_changed(self, item, value):
		# Store setting
		setting = f'Auto_{self._serial}_{self._channel_id}'
		try:
			await self.settings.set_value(self.settings.alias(setting), value)
		except:
			return
		item.set_local_value(value)

	async def _set_three_state_switch(self, enabled):
		if enabled:
			if self.settings.alias(f'Auto_{self._serial}_{self._channel_id}') is None:
				await self.settings.add_settings(Setting(f'{self._settings_base}{self._channel_id}/Auto', 0,
											 _min=0, _max=1, alias=f"Auto_{self._serial}_{self._channel_id}"))
			if self.service.get_item(f'/SwitchableOutput/{self._channel_id}/Auto') is None:
				# Add Auto item
				init_val = self.settings.get_value(self.settings.alias(f'Auto_{self._serial}_{self._channel_id}')) or 0
				self.service.add_item(IntegerItem(f'/SwitchableOutput/{self._channel_id}/Auto', None, writeable=True, onchange=self._auto_changed))

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

	async def disable_rm(self, channel):
		logger.info("Disabling S2 Resource Manager for device %s", self._serial)
		# Let the HEMS know the RM is disabled by updating the allowed control types to only NoControl.
		#        For now, let's simply completly disconnect and see if that works out fine.
		try:
			await self.rm_item.send_resource_manager_details(control_types=[self._control_type_noctrl], asset_details=self._rm_details)

			#make sure to send a 0 power measurement to EMS as well.
			await self.rm_item.send_msg_and_await_reception_status(
				PowerMeasurement(
					message_id=uuid.uuid4(),
					measurement_timestamp=datetime.now(timezone.utc),
					values=[
						PowerValue(
							commodity_quantity = phase_setting_to_commodity(self._phase),
							value=0
						)
					]
				)
			)
		except:
			# Will throw when the HEMS is not connected, but will still update the available control types.
			# So next time HEMS connects, it will only be offered the NoControl control type.
			pass

		# Set switch type back to the default type.
		await self._set_three_state_switch(False)
		self._rm_enabled = False

	def update(self, status_json, cap=None):
		super().update(status_json, cap)
		if self._rm_enabled and self._control_type_ombc is not None and self._control_type_ombc.active:
			# Pull relevant values from the device and forward to the control type
			self._control_type_ombc.values_changed({'Status': self.status,'Power': self.power,})

	async def _value_changed(self, path, item, value):
		ret = await super()._value_changed(path, item, value)

		if path.endswith('/PhaseSetting') and ret and self._rm_enabled and self._control_type_ombc.active:
			logger.debug("Phase setting changed, updating OMBC system description")
			task = asyncio.create_task(self._control_type_ombc.send_system_description())
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
				logger.debug(f"setting changed: {split[-1]}, updating OMBC system description")
				task = asyncio.create_task(self._control_type_ombc.send_system_description())
				background_tasks.add(task)
				task.add_done_callback(background_tasks.discard)

	async def _add_rm_to_service(self, channel):
		# Paths will be added once when the function is set to S2 resource manager.
		# After that, enabling/disabling the RM will only update the allowed control types.
		logger.info("Adding S2 Resource Manager paths to service")

		# Add settings paths
		settings_base = self._settings_base + f'{channel}/S2/'
		await self.settings.add_settings(
			Setting(settings_base + 'PowerSetting', 1000, alias=f'PowerSetting_{self._serial}_{channel}'),
			Setting(settings_base + 'Priority', 0, alias=f'Priority_{self._serial}_{channel}'),
			Setting(settings_base + 'Phase', 1, _min=0, _max=3, alias=f'Phase_{self._serial}_{channel}'), #Phase 0 will map to a 3 phased symmetric load.
			Setting(settings_base + 'OnHysteresis', 30, _min=0, _max=999999, alias=f'OnHysteresis_{self._serial}_{channel}'),
			Setting(settings_base + 'OffHysteresis', 30, _min=0, _max=999999, alias=f'OffHysteresis_{self._serial}_{channel}')
		)

		priority_setting = self.settings.get_value(self.settings.alias(f'Priority_{self._serial}_{channel}'))
		power_setting = self.settings.get_value(self.settings.alias(f'PowerSetting_{self._serial}_{channel}'))
		on_hysteresis = self.settings.get_value(self.settings.alias(f'OnHysteresis_{self._serial}_{channel}'))
		off_hysteresis = self.settings.get_value(self.settings.alias(f'OffHysteresis_{self._serial}_{channel}'))

		path_base = "/S2/0/"
		path_base_settings = "/S2/0/RmSettings/"
		self.service.add_item(IntegerItem(path_base + 'Active'))
		self.service.add_item(IntegerItem(path_base + 'Priority', writeable=True, onchange=partial(self._s2_value_changed, path_base + 'Priority')))
		self.service.add_item(IntegerItem(path_base_settings + 'PowerSetting', writeable=True, onchange=partial(self._s2_value_changed, path_base_settings + 'PowerSetting'), text=fmt['watt']))
		self.service.add_item(IntegerItem(path_base_settings + 'OnHysteresis', writeable=True, onchange=partial(self._s2_value_changed, path_base_settings + 'OnHysteresis')))
		self.service.add_item(IntegerItem(path_base_settings + 'OffHysteresis', writeable=True, onchange=partial(self._s2_value_changed, path_base_settings + 'OffHysteresis')))

		# Explicitly set initial values to force an items changed
		with self.service as s:
			s[path_base + 'Active'] = 0
			s[path_base + 'Priority'] = priority_setting
			s[path_base_settings + 'PowerSetting'] = power_setting
			s[path_base_settings + 'OnHysteresis'] = on_hysteresis
			s[path_base_settings + 'OffHysteresis'] = off_hysteresis

		# Get channel custom name, if not available use the default name
		name = self.service.get_item(f'/SwitchableOutput/{channel}/Settings/CustomName').value or \
			self.service.get_item(f'/SwitchableOutput/{channel}/Name').value

		# TODO: Update the asset details when the device custom name changes?
		logger.info("Setting up phase as {}".format(self._phase))
		self._rm_details = AssetDetails(
			resource_id=uuid.uuid4(),
			provides_forecast=False,
			provides_power_measurements=[phase_setting_to_commodity(self._phase or 0)],
			instruction_processing_delay=Duration(0),
			roles=[Role(role=RoleType.ENERGY_CONSUMER, commodity='ELECTRICITY')],
			name=name,
			manufacturer="Shelly",
			firmware_version=VERSION,
			serial_number=self._serial
		)

		self._control_type_ombc = ShellyOMBC(self)
		self._control_type_noctrl = ShellyNOCTRL(self)
		self.rm_item = S2ResourceManagerItem(
			'/S2/0/Rm',
			control_types=[self._control_type_noctrl, self._control_type_ombc],
			asset_details=self._rm_details
		)

		self.service.add_item(self.rm_item)

class ShellyOMBC(OMBCControlType):
	@property
	def active(self):
		return self._active

	def __init__(self, switch_item):
		self._id_off = uuid.uuid4()
		self._id_on = uuid.uuid4()
		self._id_on_off = uuid.uuid4()
		self._id_off_on = uuid.uuid4()
		self._previous_operation_mode = None
		self._previous_power = 0
		self._active = False
		self._status = None
		self._switch_item = switch_item

	def _make_system_description(self):
		#First, create the system description required. It contains 2 controltypes (On / Off)
		#and the proper transitions accordings to desired On/Off delays.
		self.op_mode_on = OMBCOperationMode(
			id=str(self._id_on),
			diagnostic_label="On",
			abnormal_condition_only=False,
			power_ranges=[PowerRange(
				start_of_range=self._switch_item.power_setting,
				end_of_range=self._switch_item.power_setting,
				commodity_quantity=phase_setting_to_commodity(self._switch_item.phase)
			)]
		)

		self.op_mode_off = OMBCOperationMode(
			id=str(self._id_off),
			diagnostic_label="Off",
			abnormal_condition_only=False,
			power_ranges=[PowerRange(
				start_of_range=0,
				end_of_range=0,
				commodity_quantity=phase_setting_to_commodity(self._switch_item.phase)
			)]
		)

		# User can configure desired On/Off Hysteresis to avoid certain consumers turning on/off to frequently
		# and eventually cause damage. These limits will be obeyed by the EMS, eventually not in offgrid cases,
		# when an overload situation happens.
		self.on_timer = Timer(id=uuid.uuid4(), diagnostic_label="On Hysteresis", duration=(self._switch_item.on_hysteresis or 0) * 1000)
		self.off_timer = Timer(id=uuid.uuid4(), diagnostic_label="Off Hysteresis", duration=(self._switch_item.off_hysteresis or 0) * 1000)

		self.transition_to_on = Transition(
			id=str(self._id_off_on),
			from_=self.op_mode_off.id,
			to=self.op_mode_on.id,
			start_timers=[self.off_timer.id],
			blocking_timers=[self.on_timer.id],
			transition_duration=None, # Negligible transition duration
			abnormal_condition_only=False
		)

		self.transition_to_off = Transition(
			id=str(self._id_on_off),
			from_=self.op_mode_on.id,
			to=self.op_mode_off.id,
			start_timers=[self.on_timer.id],
			blocking_timers=[self.off_timer.id],
			transition_duration=None, # Negligible transition duration
			abnormal_condition_only=False
		)

		self.system_description = OMBCSystemDescription(
			message_id=uuid.uuid4(),
			valid_from=datetime.now(timezone.utc),
			operation_modes=[self.op_mode_on, self.op_mode_off],
			transitions=[self.transition_to_on, self.transition_to_off],
			timers=[self.on_timer, self.off_timer]
		)

	def values_changed(self, values):
		#Ensure a 0 power package is transfered, even if the device isn't active anymore.
		if not self._active and self._previous_power == 0:
			return

		if 'Status' in values and self._status != values['Status']:
			# Status has changed, update the HEMS
			self._status = values['Status']
			task = asyncio.create_task(self.send_status())
			background_tasks.add(task)
			task.add_done_callback(background_tasks.discard)

		if 'Power' in values:
			power = values['Power']
			# we cannot monitor for a significant power change here.
			# Sometimes the shelly reports 2 or 3 watt as last state before beeing off,
			# this would then get "stuck", cause the 0 is no longer transfered.
			self._previous_power = power
			task = asyncio.create_task(self.send_power_measurement())
			background_tasks.add(task)
			task.add_done_callback(background_tasks.discard)

	async def handle_instruction(self, conn, msg, send_okay):
		if not isinstance(msg, OMBCInstruction):
			logger.error("Received message is not an OMBCInstruction: %s", msg)
			return

		if not self._active:
			logger.error("OMBCControlTypeSwitch is not active, ignoring instruction")
			return

		op_id = msg.operation_mode_id
		if op_id not in (self._id_off, self._id_on):
			logger.error("Received unknown operation mode ID: %s", op_id)
			return

		logger.info("Op-Id selected by EMS: {}".format(op_id))
		seconds = (msg.execution_time - datetime.now(timezone.utc)).total_seconds()
		task = asyncio.create_task(self._set_operation_mode(op_id, max(0, seconds)))
		background_tasks.add(task)
		task.add_done_callback(background_tasks.discard)
		await send_okay

	async def _set_operation_mode(self, op_id, wait):
		if (wait):
			logger.debug("Waiting for %f seconds before setting operation mode to %s", wait, "on" if op_id == self._id_on else "off")
			await asyncio.sleep(wait)

		self._switch_item.state = (1 if op_id == self._id_on else 0)
		logger.debug("Set operation mode to %s", "on" if op_id == self._id_on else "off")
		# Don't set _status here. It will be updated by values_changed when its done. Status message will then also be sent to HEMS.

	async def activate(self, conn):
		logger.debug("Activate OMBCControlTypeSwitch")
		if self._switch_item.rm_item is None:
			logger.error("Switch item does not have a Resource Manager item, cannot activate OMBCControlTypeSwitch")
			return

		self._active = True
		self._switch_item.s2_active = 1
		# Initialize status. further updates to _status will only be done by values_changed.
		self._status = self._switch_item.status

		# Send system description and status to the HEMS
		msg = await self.send_system_description()
		if (msg is None) or (msg.status != ReceptionStatusValues.OK):
			logger.error("Failed to activate OMBC control type, reception status message: %s", msg)
			self.deactivate(conn)
			return
		await self.send_status()

	def deactivate(self, conn):
		if self._switch_item.rm_item is None:
			logger.error("Switch item does not have a Resource Manager item, cannot deactivate OMBCControlTypeSwitch")
			return
		logger.debug("Deactivate OMBCControlTypeSwitch")
		self._active = False
		self._switch_item.s2_active = 0

	async def send_system_description(self):
		if not self._active:
			logger.warning("OMBCControlTypeSwitch is not active, cannot send system description")
			return

		self._make_system_description()
		try:
			return await self._switch_item.rm_item.send_msg_and_await_reception_status(self.system_description)
		except Exception as e:
			logger.error("Failed to send OMBCSystemDescription: %s", e)
			return

	async def send_status(self):
		if not self._active:
			return

		logger.debug("Sending status for OMBCControlTypeSwitch, current status: %s", self._status)
		operation_mode = self._id_on if self._status == STATUS_ON else self._id_off
		try:
			return await self._switch_item.rm_item.send_msg_and_await_reception_status(
				OMBCStatus(
					message_id=uuid.uuid4(),
					active_operation_mode_id=str(operation_mode),
					operation_mode_factor=1, #FIXME: This needs to report the factor requested by OMBC-Instruction.
					previous_operation_mode_id=str(self._previous_operation_mode) if self._previous_operation_mode is not None else None,
					transition_timestamp=datetime.now(timezone.utc)
				)
			)
		except Exception as e:
			logger.error("Failed to send status: %s", e)
		finally:
			self._previous_operation_mode = operation_mode

	async def send_power_measurement(self):
		if not self._active:
			return
		try:
			logger.info("Sending Power Measurement {}={}W".format(self._switch_item.rm_item.asset_details.name, self._switch_item.power))
			await self._switch_item.rm_item.send_msg_and_await_reception_status(
				PowerMeasurement(
					message_id=uuid.uuid4(),
					measurement_timestamp=datetime.now(timezone.utc),
					values=[
						PowerValue(
							commodity_quantity= phase_setting_to_commodity(self._switch_item.phase),
							value=self._switch_item.power
						)
					]
				)
			)
		except Exception as e:
			logger.error("Failed to send power measurement: %s", e)


class ShellyNOCTRL(NoControlControlType):

	def __init__(self, switch_item):
		self._switch_item = switch_item
		super().__init__()

	def activate(self, conn):
		logger.info("NOCTRL activated.")
		self.system_description=None
		self.on_id=None
		self.off_id=None

	def deactivate(self, conn):
		logger.info("NOCTRL deactivated.")
