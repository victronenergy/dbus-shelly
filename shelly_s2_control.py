from __future__ import annotations

import sys
import os
import asyncio
import logging
import uuid
from datetime import datetime, timezone

#aiovelib
sys.path.insert(1, os.path.join(os.path.dirname(__file__), 'ext', 'aiovelib'))
from aiovelib.s2 import S2ResourceManagerItem
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

from utils import STATUS_ON

logger = logging.getLogger('switch-device-rm')
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

class ShellyOMBC(OMBCControlType):

	@property
	def enabled(self):
		return self._enabled

	@enabled.setter
	def enabled(self, value):
		self._enabled = value

	@property
	def active(self):
		return self._active

	def __init__(self, switch_item):
		self._id_off = uuid.uuid4()
		self._id_on = uuid.uuid4()
		self._id_on_off = uuid.uuid4()
		self._id_off_on = uuid.uuid4()
		self._previous_operation_mode = None
		self._enabled = False
		self._active = False
		self._status = None
		self._switch_item = switch_item
		self._status_queue = asyncio.Queue()
		self._status_queue_stop = object()
		self._status_worker_task = None
		self._power_queue = asyncio.Queue()
		self._power_queue_stop = object()
		self._power_worker_task = None

	def _start_worker(self, task_attr, worker_method_name):
		"""Generic worker startup. Called only from awaited activate() context."""
		task = getattr(self, task_attr)
		if task is None or task.done():
			worker_coro = getattr(self, worker_method_name)
			setattr(self, task_attr, asyncio.create_task(worker_coro()))

	def _start_status_worker(self):
		self._start_worker('_status_worker_task', '_status_sender_worker')

	def _start_power_worker(self):
		self._start_worker('_power_worker_task', '_power_sender_worker')

	async def _stop_worker(self, task_attr, queue_attr, stop_sentinel, worker_name):
		"""Generic worker stopping. Called only from awaited deactivate() context."""
		task = getattr(self, task_attr)
		if task is None:
			return

		if not task.done():
			queue = getattr(self, queue_attr)
			queue.put_nowait(stop_sentinel)
			try:
				await task
			except Exception as e:
				logger.error("%s worker stopped with error: %s", worker_name, e)

		setattr(self, task_attr, None)

	async def _stop_status_worker(self):
		await self._stop_worker('_status_worker_task', '_status_queue', self._status_queue_stop, 'Status')

	async def _stop_power_worker(self):
		await self._stop_worker('_power_worker_task', '_power_queue', self._power_queue_stop, 'Power')

	async def _sender_worker(self, queue, stop_sentinel, send_coro):
		"""Generic sender worker that processes items from a queue."""
		while True:
			item = await queue.get()
			if item is stop_sentinel:
				return
			await send_coro(item)

	async def _status_sender_worker(self):
		await self._sender_worker(self._status_queue, self._status_queue_stop, self.send_status)

	async def _power_sender_worker(self):
		await self._sender_worker(self._power_queue, self._power_queue_stop, self.send_power_measurement)

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
		if not self._active:
			return

		# Let status update pass when control type is disabled because in that case the state won't change but the HEMS still needs to be notified.
		if 'Status' in values and (self._status != values['Status'] or not self._enabled):
			# Status has changed, update the HEMS
			self._status = values['Status']
			self._status_queue.put_nowait(self._status)

		if 'Power' in values:
			# we cannot monitor for a significant power change here.
			# Sometimes the shelly reports 2 or 3 watt as last state before beeing off,
			# this would then get "stuck", cause the 0 is no longer transfered.

			# TODO: Reduce load by preventing (nearly) equal power measurements to be sent.
			self._power_queue.put_nowait(values['Power'])

	async def handle_instruction(self, conn, msg, send_okay):
		if not isinstance(msg, OMBCInstruction):
			logger.error("Received message is not an OMBCInstruction: %s", msg)
			return

		if not self._active:
			return

		op_id = msg.operation_mode_id
		if op_id not in (self._id_off, self._id_on):
			logger.error("Received unknown operation mode ID: %s", op_id)
			return

		if not self._enabled:
			# OMBC control type is not enabled, but HEMS may not be aware of that. Keep S2 message flow going but do not change the state.
			op_id = self._id_on if self._switch_item.state == 1 else self._id_off
			logger.warning("Received OMBCInstruction while control type is not enabled, state transition will be ignored")

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

		async def _force_delayed_update_after_mode_change():
			# Make sure the update is done before the next iteration of OL (5 seconds)
			await asyncio.sleep(2)
			await self._switch_item.force_update()

		# Refresh device status after each mode change so update() can publish a measurement when needed.
		# After turning on/off the output, the Shelly will send a status update immediately, but with the old power measurement.
		# This will lead OL to believe the load isn't consuming its nominal power, while the multi will report the increased consumption.
		# To solve this, manually request a status update after 2 seconds.
		task = asyncio.create_task(_force_delayed_update_after_mode_change())
		background_tasks.add(task)
		task.add_done_callback(background_tasks.discard)

	async def activate(self, conn):
		logger.debug("Activate OMBCControlTypeSwitch")
		if self._switch_item.rm_item is None:
			logger.error("Switch item does not have a Resource Manager item, cannot activate OMBCControlTypeSwitch")
			return

		self._switch_item.s2_active = 1
		# Initialize status. further updates to _status will only be done by values_changed.
		self._status = self._switch_item.status

		# Start workers before setting _active
		self._start_status_worker()
		self._status_queue.put_nowait(self._status)
		self._start_power_worker()
		# Set _active only after workers are guaranteed to be running
		self._active = True
		
		# Send system description after workers are running and _active is set
		msg = await self.send_system_description()
		if (msg is None) or (msg.status != ReceptionStatusValues.OK):
			logger.error("Failed to activate OMBC control type, reception status message: %s", msg)
			await self.deactivate(conn)
			return

	async def deactivate(self, conn):
		if self._switch_item.rm_item is None:
			logger.error("Switch item does not have a Resource Manager item, cannot deactivate OMBCControlTypeSwitch")
			return
		# Set _active to False immediately to stop new items being queued to workers
		self._active = False
		logger.debug("Deactivate OMBCControlTypeSwitch")
		await self._stop_status_worker()
		await self._stop_power_worker()
		self._switch_item.s2_active = 0

	async def send_system_description(self):
		# Defensive check: background tasks may run after deactivation starts
		if not self._active:
			logger.warning("OMBCControlTypeSwitch is not active, cannot send system description")
			return

		self._make_system_description()
		try:
			return await self._switch_item._send_s2_message(self.system_description)
		except Exception as e:
			logger.error("Failed to send OMBCSystemDescription: %s", e)
			return

	async def send_status(self, status):
		logger.debug("Sending status for OMBCControlTypeSwitch, current status: %s", status)
		operation_mode = self._id_on if status == STATUS_ON else self._id_off
		try:
			return await self._switch_item._send_s2_message(
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

	async def send_power_measurement(self, power):
		try:
			logger.debug("Sending Power Measurement {}={}W".format(self._switch_item.rm_item.asset_details.name, power))
			await self._switch_item._send_s2_message(
				PowerMeasurement(
					message_id=uuid.uuid4(),
					measurement_timestamp=datetime.now(timezone.utc),
					values=[
						PowerValue(
							commodity_quantity= phase_setting_to_commodity(self._switch_item.phase),
							value=power
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

	async def activate(self, conn):
		logger.info("NOCTRL activated.")
		self.system_description=None
		self.on_id=None
		self.off_id=None

	async def deactivate(self, conn):
		logger.info("NOCTRL deactivated.")
