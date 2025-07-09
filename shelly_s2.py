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

class ShellyChannelWithRm(base.ShellyChannel):

	@property
	def power(self):
		return self.service.get_item(f'/Ac/L1/Power').value or 0

	@property
	def s2_active(self):
		return self.service.get_item(f'/Devices/{self._channel}/S2/S2Active').value or 0

	@s2_active.setter
	def s2_active(self, value):
		self.service.get_item(f'/Devices/{self._channel}/S2/S2Active')._set_value(value)

	@property
	def has_rm(self):
		return self.service.get_item("/Devices/{}/S2".format(self._channel)) is not None

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self._control_type = None
		self.rm_item = None

	async def add_output(self, channel, output_type, set_state_cb, valid_functions, name=None, customName=None, set_dimming_cb=None):
		valid_functions |= (1 << OutputFunction.S2_RM)
		await super().add_output(channel, output_type, set_state_cb, valid_functions, name, customName, set_dimming_cb)

		function = self.service.get_item(f'/SwitchableOutput/{channel}/Settings/Function').value
		if function and function == OutputFunction.S2_RM:
			await self.add_rm_to_service(channel)
			logger.debug("Setting output state off initially")
			await set_state_cb(self.service.get_item(f'/SwitchableOutput/{channel}/State'), 0)
			logger.info("S2 Resource Manager added to service")

	def update(self, status_json):
		super().update(status_json)
		if self.has_rm:
			# Pull relevant values from the device and forward to the control type
			self._control_type.values_changed({'Status': self.status,'Power': self.power,})

	async def on_channel_function_changed(self, channel, function):
		if function == OutputFunction.S2_RM:
			await self.add_rm_to_service(channel)
		elif self.has_rm:
			logger.info("Cannot remove S2 item from service, restart the service.")

	async def _s2_value_changed(self, path, item, value):
		split = path.split('/')
		if len(split) > 3 and split[3] == 'S2':
			setting = f'{split[-1]}_{self._serial}_{split[-3]}'
			try:
				await self.settings.set_value(self.settings.alias(setting), value)
			except:
				return

			item.set_local_value(value)

	async def add_rm_to_service(self, channel):
		if self.has_rm:
			logger.info("S2 Resource Manager already exists for channel device %s channel %s",self.serial, channel)
			return

		logger.info("Enabling S2 Resource Manager for device %s channel %s", self.serial, channel)

		path_base = "/Devices/{}/S2/".format(channel)
		self.service.add_item(IntegerItem(path_base + 'S2Active', 0))
		self.service.add_item(IntegerItem(path_base + 'ConsumerType', 0, writeable=True, onchange=partial(self._s2_value_changed, path_base + 'ConsumerType')))
		self.service.add_item(IntegerItem(path_base + 'Priority', 0, writeable=True, onchange=partial(self._s2_value_changed, path_base + 'Priority')))
		self.service.add_item(IntegerItem(path_base + 'Phase', 1, writeable=True, onchange=partial(self._s2_value_changed, path_base + 'Phase')))

		# Add settings paths
		settings_base = self._settings_base + f'{channel}/S2/'
		await self.settings.add_settings(
			Setting(settings_base + 'ConsumerType', 0, alias=f'ConsumerType_{self._serial}_{channel}'),
			Setting(settings_base + 'Priority', 0, alias=f'Priority_{self._serial}_{channel}'),
			Setting(settings_base + 'Phase', 1, _min=1, _max=3, alias=f'Phase_{self._serial}_{channel}')
		)

		# Restore settings
		try:
			with self.service as s:
				s[path_base + 'ConsumerType'] = self.settings.get_value(self.settings.alias(f'ConsumerType_{self._serial}_{channel}'))
				s[path_base + 'Priority'] = self.settings.get_value(self.settings.alias(f'Priority_{self._serial}_{channel}'))
				s[path_base + 'Phase'] = self.settings.get_value(self.settings.alias(f'Phase_{self._serial}_{channel}'))
		except Exception as e:
			logger.error("Failed to restore settings for S2 Resource Manager: %s", e)

		# Get channel custom name, if not available use the default name
		name = self.service.get_item(f'/SwitchableOutput/{channel}/Settings/CustomName').value or \
			self.service.get_item(f'/SwitchableOutput/{channel}/Name').value

		self._rm_details = AssetDetails(
			resource_id=uuid.uuid4(),
			provides_forecast=False,
			provides_power_measurements=[CommodityQuantity.ELECTRIC_POWER_L1],
			instruction_processing_delay=Duration(0),
			roles=[Role(role=RoleType.ENERGY_CONSUMER, commodity='ELECTRICITY')],
			name=name,
			manufacturer="Shelly",
			firmware_version=VERSION,
			serial_number=self._serial
		)

		self._control_type = SwitchDeviceControlType(self)
		self.rm_item = S2ResourceManagerItem(
			'/Devices/{}/S2'.format(self._channel),
			control_types=[self._control_type],
			asset_details=self._rm_details)

		self.service.add_item(self.rm_item)


class SwitchDeviceControlType(OMBCControlType):
	MINIMUM_POWER_CHANGE = 0.05 # change in percentage before a power measurement is sent

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
				start_of_range=self._switch_item.power,
				end_of_range=self._switch_item.power,
				commodity_quantity=CommodityQuantity.ELECTRIC_POWER_L1
			)]
		)

		self.op_mode_off = OMBCOperationMode(
			id=str(self._id_off),
			diagnostic_label="Off",
			abnormal_condition_only=False,
			power_ranges=[PowerRange(
				start_of_range=0,
				end_of_range=0,
				commodity_quantity=CommodityQuantity.ELECTRIC_POWER_L1
			)]
		)

		# Opportunity loads controlled by shelly can be turned on and off at will, so no hysteresis required.
		self.on_timer = Timer(id=uuid.uuid4(), diagnostic_label="On Hysteresis", duration=0)
		self.off_timer = Timer(id=uuid.uuid4(), diagnostic_label="Off Hysteresis", duration=0)

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
		if not self._active:
			return
		if 'Status' in values and self._status != values['Status']:
			# Status has changed, update the HEMS
			self._status = values['Status']
			task = asyncio.create_task(self.send_status())
			background_tasks.add(task)
			task.add_done_callback(background_tasks.discard)

		if 'Power' in values:
			power = values['Power']
			if power > self._previous_power * (1 + self.MINIMUM_POWER_CHANGE) or \
					power < self._previous_power * (1 - self.MINIMUM_POWER_CHANGE):
				# Power has changed significantly, send a power measurement
				self._previous_power = power
				task = asyncio.create_task(self.send_power_measurement())
				background_tasks.add(task)
				task.add_done_callback(background_tasks.discard)

	def handle_instruction(self, conn, msg, send_okay):
		logger.debug("Handle instruction: %s", msg)
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

		exec_time = msg.execution_time
		seconds = (datetime.datetime.now(timezone.utc) - datetime.datetime.strptime(exec_time, "%Y-%m-%d %H:%M:%S.%f%z")).total_seconds()
		task = asyncio.create_task(
			self._set_operation_mode(op_id, max(0, seconds))
		)
		background_tasks.add(task)
		task.add_done_callback(background_tasks.discard)

	async def _set_operation_mode(self, op_id, wait):
		if (wait):
			logger.debug("Waiting for %d seconds before setting operation mode to %s", wait, "on" if op_id == self._id_on else "off")
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
					operation_mode_factor=1,
					previous_operation_mode_id=str(self._previous_operation_mode) if self._previous_operation_mode is not None else None,
					transition_timestamp=datetime.datetime.now(timezone.utc) if self._previous_operation_mode is not None else None
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
			await self.rm_item.send_msg_and_await_reception_status(
				PowerMeasurement(
					message_id=uuid.uuid4(),
					measurement_timestamp=datetime.datetime.now(timezone.utc),
					values=[
						PowerValue(
							commodity_quantity=CommodityQuantity.ELECTRIC_POWER_L1,
							value=self._switch_item.power
						)
					]
				)
			)
		except Exception as e:
			logger.error("Failed to send power measurement: %s", e)
