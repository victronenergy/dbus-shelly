from __future__ import annotations

import sys
import os
import argparse
import asyncio
from functools import partial
import logging
import uuid
import aiohttp
import datetime
import pytz

#aiovelib
sys.path.insert(1, os.path.join(os.path.dirname(__file__), 'ext', 'aiovelib'))
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

try:
	from dbus_fast.aio import MessageBus
except ImportError:
	from dbus_next.aio import MessageBus

try:
	from dbus_fast.constants import BusType
except ImportError:
	from dbus_next.constants import BusType

from switch_device import SwitchDevice, OutputFunction, STATUS_ON, STATUS_OFF

logger = logging.getLogger('switch-device-rm')
logger.setLevel(logging.DEBUG)
background_tasks = set()

class SwitchDeviceWithRm(SwitchDevice):
	_values_changed_callback = None

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.CHANNEL_VALID_FUNCTIONS |= (1 << OutputFunction.S2_RM)

	def set_values_changed_callback(self, callback):
		self._values_changed_callback = callback

	def initialize_channel(self, channel):
		function = self.service.get_item("/SwitchableOutput/{}/Settings/Function".format(channel)).value
		if function and function == OutputFunction.S2_RM:
			task = self._runningloop.create_task(self.add_rm_to_service(channel))
			background_tasks.add(task)
			task.add_done_callback(background_tasks.discard)

	def on_channel_values_changed(self, channel, values):
		if self._values_changed_callback is not None:
			self._values_changed_callback(values)

	def on_channel_function_changed(self, channel, function):
		if function == OutputFunction.S2_RM:
			task = self._runningloop.create_task(self.add_rm_to_service(channel))
			background_tasks.add(task)
			task.add_done_callback(background_tasks.discard)
		elif self.has_rm(channel):
			logger.info("Cannot remove S2 item from service, restart the service.")

	async def add_rm_to_service(self, channel):
		if self.has_rm(channel):
			logger.info("S2 Resource Manager already exists for channel %s", channel)
			return
		logger.info("Enabling S2 Resource Manager for channel %s", channel)
		self.service.add_item(SwitchResourceManager(self, channel))

	def has_rm(self, channel):
		return self.service.get_item("/Devices/{}/S2".format(channel)) is not None

class SwitchDeviceControlType(OMBCControlType):
	_id_off = uuid.uuid4()
	_id_on = uuid.uuid4()
	_previous_operation_mode = None
	_active = False
	_status = None

	@property
	def active(self):
		return self._active

	def __init__(self, rm_item: S2ResourceManagerItem):
		self._loop = asyncio.get_event_loop()
		self._rm_item = rm_item
		self._ombc_system_description = OMBCSystemDescription(
			message_id=uuid.uuid4(),
			valid_from=datetime.datetime.now(tz=pytz.UTC),
			operation_modes=[
				OMBCOperationMode(
					id=self._id_off,
					diagnostic_label="off",
					power_ranges=[
						PowerRange(
							start_of_range=0,
							end_of_range=0,
							commodity_quantity=CommodityQuantity.ELECTRIC_POWER_L1,
						)
					],
					abnormal_condition_only=False
				),
				OMBCOperationMode(
					id=self._id_on,
					diagnostic_label="on",
					power_ranges=[
						PowerRange(
							start_of_range=1000,
							end_of_range=1000,
							commodity_quantity=CommodityQuantity.ELECTRIC_POWER_L1,
						)
					],
					abnormal_condition_only=False
				)
			],
			transitions=[
				Transition(
					id=uuid.uuid4(),
					from_=self._id_off,
					to=self._id_on,
					start_timers=[],
					blocking_timers=[],
					abnormal_condition_only=False
				),
				Transition(
					id=uuid.uuid4(),
					from_=self._id_on,
					to=self._id_off,
					start_timers=[],
					blocking_timers=[],
					abnormal_condition_only=False
				)
			],
			timers=[
				Timer(
					id=uuid.uuid4(),
					duration=Duration(0),
				)
			]
		)

	def values_changed(self, values):
		if 'Status' in values and self._status != values['Status']:
			self._status = values['Status']
			task = asyncio.get_event_loop().create_task(self.send_status())
			background_tasks.add(task)
			task.add_done_callback(background_tasks.discard)

	def handle_instruction(self, conn, msg, send_okay):
		logger.info("Handle instruction: %s", msg)
		if not isinstance(msg, OMBCInstruction):
			logger.error("Received message is not an OMBCInstruction: %s", msg)
			return

		if not self._active:
			logger.warning("OMBCControlTypeSwitch is not active, ignoring instruction")
			return

		op_id = msg.operation_mode_id
		if op_id not in (self._id_off, self._id_on):
			logger.error("Received unknown operation mode ID: %s", op_id)
			return

		exec_time = msg.execution_time
		seconds = (datetime.datetime.now(tz=pytz.UTC) - datetime.datetime.strptime(exec_time, "%Y-%m-%d %H:%M:%S.%f%z")).total_seconds()
		task = self._loop.create_task(
			self._set_operation_mode(op_id, 0 if seconds <= 0 else seconds)
		)
		background_tasks.add(task)
		task.add_done_callback(background_tasks.discard)

	async def _set_operation_mode(self, op_id, wait):
		if (wait):
			logger.info("Waiting for %d seconds before setting operation mode to %s", wait, "on" if op_id == self._id_on else "off")
			await asyncio.sleep(wait)
		self._rm_item.set_switch_state(1 if op_id == self._id_on else 0)
		self._status = STATUS_ON if op_id == self._id_on else STATUS_OFF
		logger.info("Set operation mode to %s", "on" if op_id == self._id_on else "off")

	def activate(self, conn):
		logger.info("Activate OMBCControlTypeSwitch")

		try:
			self._rm_item.send_msg_and_await_reception_status_sync(self._ombc_system_description)
		except Exception as e:
			logger.error("Failed to send OMBCSystemDescription: %s", e)
			return

		# Set initial state
		self._rm_item.set_switch_state(0)
		logger.info("Initial switch state set to off")
		task = self._loop.create_task(self.send_status())
		background_tasks.add(task)
		task.add_done_callback(background_tasks.discard)
		logger.info("Initial status sent")
		self._active = True
		logger.info("OMBCControlTypeSwitch activated")

	async def send_status(self):
		if not self._active:
			return

		operation_mode = self._id_on if self._status == STATUS_ON else self._id_off
		try:
			await self._rm_item.send_msg_and_await_reception_status(
				OMBCStatus(
					message_id=uuid.uuid4(),
					active_operation_mode_id=str(self._id_on if self._status == STATUS_ON else self._id_off),
					operation_mode_factor=1,
					previous_operation_mode_id=str(self._previous_operation_mode) if self._previous_operation_mode is not None else None,
					transition_timestamp=datetime.datetime.now(tz=pytz.UTC) if self._previous_operation_mode is not None else None
				)
			)
		except Exception as e:
			logger.error("Failed to send status: %s", e)
		finally:
			self._previous_operation_mode = operation_mode

	def deactivate(self, conn):
		logger.info("Deactivate OMBCControlTypeSwitch")
		self._active = False

class SwitchResourceManager(S2ResourceManagerItem):
	MINIMUM_POWER_CHANGE = 0.05 # change in percentage before a power measurement is sent
	_switch = None
	_channel = None
	_previous_power = 0
	_control_type = None

	def __init__(self, switch, channel):
		#self._bus_type = bus_type
		self._switch = switch
		self._channel = channel

		self._rm_details = AssetDetails(
			resource_id=uuid.uuid4(),
			provides_forecast=False,
			provides_power_measurements=[CommodityQuantity.ELECTRIC_POWER_L1],
			instruction_processing_delay=Duration(0),
			roles=[Role(role=RoleType.ENERGY_CONSUMER, commodity='ELECTRICITY')],
			name="Victron Switchable output",
			manufacturer="Victron",
			firmware_version="1.0", #TBD
			serial_number=None
		)

		self._switch.set_values_changed_callback(self.on_values_changed)
		self._control_type = SwitchDeviceControlType(self)
		super().__init__("/Devices/{}/S2".format(self._channel), control_types=[self._control_type], asset_details=self._rm_details)

	def set_switch_state(self, state):
		self._switch.set_state_cb(self._channel, state)

	def on_values_changed(self, values):
		if 'P' in values:
			power = values['P']
			if power > self._previous_power * 1 + self.MINIMUM_POWER_CHANGE or \
					power < self._previous_power * 1 - self.MINIMUM_POWER_CHANGE:
				self._previous_power = power
				loop = asyncio.get_event_loop()
				task = loop.create_task(self.send_power_measurement())
				background_tasks.add(task)
				task.add_done_callback(background_tasks.discard)

		self._control_type.values_changed(values)

	async def send_power_measurement(self):
		if not self._control_type.active:
			return
		try:
			await self.send_msg_and_await_reception_status(
				PowerMeasurement(
					message_id=uuid.uuid4(),
					measurement_timestamp=datetime.datetime.now(tz=pytz.UTC),
					values=[
						PowerValue(
							commodity_quantity=CommodityQuantity.ELECTRIC_POWER_L1,
							value=self._switch.get_power(self._channel),
						)
					]
				)
			)
		except Exception as e:
			logger.error("Failed to send power measurement: %s", e)