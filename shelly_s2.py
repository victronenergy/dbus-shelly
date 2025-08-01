from __future__ import annotations

import sys
import os
import asyncio
import logging
from functools import partial
import uuid
from datetime import datetime, timezone

#aiovelib
sys.path.insert(1, os.path.join(os.path.dirname(__file__), 'ext', 'aiovelib'))
from aiovelib.service import Service, IntegerItem, TextItem
from aiovelib.localsettings import Setting

from s2 import S2ResourceManagerItem
from s2python.s2_control_type import NoControlControlType, OMBCControlType
from s2python.s2_connection import AssetDetails
from s2python.generated.gen_s2 import CommodityQuantity, RoleType
from s2python.common.power_range import PowerRange
from s2python.common.transition import Transition
from s2python.common.timer import Timer

from s2python.common import (
	ReceptionStatusValues,
	ReceptionStatus,
	Handshake,
	EnergyManagementRole,
	Role,
	HandshakeResponse,
	ResourceManagerDetails,
	Duration,
	PowerMeasurement,
	PowerValue,
	Currency,
	SelectControlType,
)

from s2python.ombc import (
	OMBCInstruction,
	OMBCOperationMode,
	OMBCTimerStatus,
	OMBCStatus,
	OMBCSystemDescription,
)

from __main__ import VERSION
import base
from switch import OutputFunction
from utils import STATUS_OFF, STATUS_ON, formatters as fmt

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

class ShellyChannelWithRm(base.ShellyChannel):

	@property
	def power(self):
		return (self.service.get_item(f'/Ac/L1/Power').value or 0) + (self.service.get_item(f'/Ac/L2/Power').value or 0) + (self.service.get_item(f'/Ac/L3/Power').value or 0)
	
	@property
	def phase_setting(self):
		service_item = self.service.get_item(f'/Devices/{self._channel}/S2/Phase') or None
		return service_item.value if service_item is not None else None
	
	@property
	def on_hysteresis(self):
		return self.service.get_item(f'/Devices/{self._channel}/S2/OnHysteresis').value or 0
	
	@property
	def off_hysteresis(self):
		return self.service.get_item(f'/Devices/{self._channel}/S2/OffHysteresis').value or 0

	@property
	def power_setting(self):
		return self.service.get_item(f'/Devices/{self._channel}/S2/PowerSetting').value or 0

	@property
	def s2_active(self):
		return self.service.get_item(f'/Devices/{self._channel}/S2/Active').value or 0

	@s2_active.setter
	def s2_active(self, value):
		self.service.get_item(f'/Devices/{self._channel}/S2/Active').set_local_value(value)

	@property
	def has_rm(self):
		return self.service.get_item("/Devices/{}/S2".format(self._channel)) is not None

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.rm_item = None
		self._control_type_ombc = None
		self._control_type_noctrl = None

		# Indicates if the RM is enabled, i.e., the OMBC control type has been offered to HEMS.
		# Whether the OMBC control type is actually activated by the HEMS is determined by self._control_type_ombc.active.
		self._rm_enabled = False

	async def enable_rm(self, channel):
		if not self.has_rm:
			# Paths not yet present, add them to the service.
			await self._add_rm_to_service(channel)
			logger.info("Enabled S2 Resource Manager for device %s (%s)", self.serial, self._rm_details.name)
		else:
			# Let the HEMS know the RM is enabled by updating the allowed control types.
			try:
				await self.rm_item.send_resource_manager_details(control_types=[self._control_type_noctrl, self._control_type_ombc], asset_details=self._rm_details)
			except:
				pass
		self._rm_enabled = True

	async def disable_rm(self, channel):
		logger.info("Disabling S2 Resource Manager for device %s", self.serial)
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
							commodity_quantity = phase_setting_to_commodity(self.phase_setting),
							value=0
						)
					]
				)
			)
		except:
			# Will throw when the HEMS is not connected, but will still update the available control types.
			# So next time HEMS connects, it will only be offered the NoControl control type.
			pass
		self._rm_enabled = False

	async def add_output(self, channel, output_type, set_state_cb, valid_functions, name=None, customName=None, set_dimming_cb=None):
		valid_functions |= (1 << OutputFunction.S2_RM)
		await super().add_output(channel, output_type, set_state_cb, valid_functions, name, customName, set_dimming_cb)

		function = self.service.get_item(f'/SwitchableOutput/{channel}/Settings/Function').value
		if function and function == OutputFunction.S2_RM:
			await self.enable_rm(channel)
			#FIXME: Better read the current state, and just report that, so a restart of S2 just continues where it was.
			logger.debug("Setting output state off initially")
			await set_state_cb(self.service.get_item(f'/SwitchableOutput/{channel}/State'), 0)
			logger.info("S2 Resource Manager added to service")

	def update(self, status_json):
		super().update(status_json, self.phase_setting)
		if self._rm_enabled and self._control_type_ombc is not None and self._control_type_ombc.active:
			# Pull relevant values from the device and forward to the control type
			self._control_type_ombc.values_changed({'Status': self.status,'Power': self.power,})

	async def on_channel_function_changed(self, channel, function):
		#FIXME: Switching from 2 to 6 during runtime does not work. (Nothing happens, requires service restart to take effect)
		if function == OutputFunction.S2_RM:
			await self.enable_rm(channel)
		elif self._rm_enabled:
			await self.disable_rm(channel)

	async def _s2_value_changed(self, path, item, value):
		split = path.split('/')
		if len(split) > 3 and split[3] == 'S2':
			setting = f'{split[-1]}_{self._serial}_{split[-3]}'
			try:
				await self.settings.set_value(self.settings.alias(setting), value)
			except:
				return

			item.set_local_value(value)

			# Update OMBC system description when (relevant)power setting changes.
			relevant_settings = ["PowerSetting", "Phase", "OnHysteresis", "OffHysteresis"]
			if split[-1] in relevant_settings and self._rm_enabled and self._control_type_ombc.active:
				logger.debug("Power setting changed, updating OMBC system description")
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
			Setting(settings_base + 'ConsumerType', 0, alias=f'ConsumerType_{self._serial}_{channel}'),
			Setting(settings_base + 'Priority', 0, alias=f'Priority_{self._serial}_{channel}'),
			Setting(settings_base + 'Phase', 1, _min=0, _max=3, alias=f'Phase_{self._serial}_{channel}'), #Phase 0 will map to a 3 phased symmetric load.
			Setting(settings_base + 'OnHysteresis', 30, _min=0, _max=999999, alias=f'OnHysteresis_{self._serial}_{channel}'),
			Setting(settings_base + 'OffHysteresis', 30, _min=0, _max=999999, alias=f'OffHysteresis_{self._serial}_{channel}')
		)

		power_setting = self.settings.get_value(self.settings.alias(f'PowerSetting_{self._serial}_{channel}'))
		consumertype_setting = self.settings.get_value(self.settings.alias(f'ConsumerType_{self._serial}_{channel}'))
		priority_setting = self.settings.get_value(self.settings.alias(f'Priority_{self._serial}_{channel}'))
		phase_setting = self.settings.get_value(self.settings.alias(f'Phase_{self._serial}_{channel}'))
		on_hysteresis = self.settings.get_value(self.settings.alias(f'OnHysteresis_{self._serial}_{channel}'))
		off_hysteresis = self.settings.get_value(self.settings.alias(f'OffHysteresis_{self._serial}_{channel}'))
		
		path_base = "/Devices/{}/S2/".format(channel)
		self.service.add_item(IntegerItem(path_base + 'Active', 0))
		self.service.add_item(IntegerItem(path_base + 'PowerSetting', power_setting, writeable=True, onchange=partial(self._s2_value_changed, path_base + 'PowerSetting'), text=fmt['watt']))
		self.service.add_item(IntegerItem(path_base + 'ConsumerType', consumertype_setting, writeable=True, onchange=partial(self._s2_value_changed, path_base + 'ConsumerType')))
		self.service.add_item(IntegerItem(path_base + 'Priority', priority_setting, writeable=True, onchange=partial(self._s2_value_changed, path_base + 'Priority')))
		self.service.add_item(IntegerItem(path_base + 'Phase', phase_setting, writeable=True, onchange=partial(self._s2_value_changed, path_base + 'Phase')))
		self.service.add_item(IntegerItem(path_base + 'OnHysteresis', on_hysteresis, writeable=True, onchange=partial(self._s2_value_changed, path_base + 'OnHysteresis')))
		self.service.add_item(IntegerItem(path_base + 'OffHysteresis', off_hysteresis, writeable=True, onchange=partial(self._s2_value_changed, path_base + 'OffHysteresis')))

		# Get channel custom name, if not available use the default name
		name = self.service.get_item(f'/SwitchableOutput/{channel}/Settings/CustomName').value or \
			self.service.get_item(f'/SwitchableOutput/{channel}/Name').value

		# TODO: Update the asset details when the device custom name changes?
		logger.info("Setting up phase as {}".format(phase_setting))
		self._rm_details = AssetDetails(
			resource_id=uuid.uuid4(),
			provides_forecast=False,
			provides_power_measurements=[phase_setting_to_commodity(phase_setting or 0)],
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
			'/Devices/{}/S2'.format(self._channel),
			control_types=[self._control_type_noctrl, self._control_type_ombc],
			asset_details=self._rm_details
		)
		
		self.service.add_item(self.rm_item)

		#FIXME: When the S2 paths are added after the service was already registered, itemschanged will not be sent automatically.
		# This may cause the HEMS to not pick up the new paths, and thus not attempt to connect to the RM.

class ShellyOMBC(OMBCControlType):
	@property
	def active(self):
		return self._active

	def __init__(self, switch_item: ShellyChannelWithRm):
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
				commodity_quantity=phase_setting_to_commodity(self._switch_item.phase_setting)
			)]
		)

		self.op_mode_off = OMBCOperationMode(
			id=str(self._id_off),
			diagnostic_label="Off",
			abnormal_condition_only=False,
			power_ranges=[PowerRange(
				start_of_range=0,
				end_of_range=0,
				commodity_quantity=phase_setting_to_commodity(self._switch_item.phase_setting)
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
							commodity_quantity= phase_setting_to_commodity(self._switch_item.phase_setting),
							value=self._switch_item.power
						)
					]
				)
			)
		except Exception as e:
			logger.error("Failed to send power measurement: %s", e)


class ShellyNOCTRL(NoControlControlType):

	def __init__(self, switch_item: ShellyChannelWithRm):
		self._switch_item = switch_item
		super().__init__()

	def activate(self, conn):
		logger.info("NOCTRL activated.")
		self.system_description=None
		self.on_id=None
		self.off_id=None
		return super().activate(conn)

	def deactivate(self, conn):
		logger.info("NOCTRL deactivated.")
		return super().deactivate(conn)
