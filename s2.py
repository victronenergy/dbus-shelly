import time
import math
import json
import uuid
import logging
import asyncio

from aiovelib.s2 import S2ServerItem


from typing import Optional, List, Awaitable

from s2python.common import (
	ReceptionStatusValues,
	ReceptionStatus,
	Handshake,
	EnergyManagementRole,
	HandshakeResponse,
	SelectControlType,
)
from s2python.reception_status_awaiter import ReceptionStatusAwaiter
from s2python.s2_control_type import S2ControlType
from s2python.s2_parser import S2Parser
from s2python.s2_validation_error import S2ValidationError
from s2python.message import S2Message
from s2python.version import S2_VERSION


from s2python.s2_connection import MessageHandlers, AssetDetails


logger = logging.getLogger(__name__)


class S2ResourceManagerItem(S2ServerItem):

	_received_messages: asyncio.Queue
	_restart_connection_event: asyncio.Event

	reception_status_awaiter: ReceptionStatusAwaiter
	s2_parser: S2Parser
	control_types: List[S2ControlType]
	role: EnergyManagementRole
	asset_details: AssetDetails

	_handlers: MessageHandlers
	_current_control_type: Optional[S2ControlType]

	def __init__(self, path,
				 control_types: List[S2ControlType],
				 asset_details: AssetDetails):
		super().__init__(path)

		self.role = EnergyManagementRole.RM
		self.reception_status_awaiter = ReceptionStatusAwaiter()
		self.s2_parser = S2Parser()
		self._handlers = MessageHandlers()
		self._current_control_type = None

		self.control_types = control_types
		self.asset_details = asset_details

		self._handlers.register_handler(SelectControlType, self.handle_select_control_type_as_rm)
		self._handlers.register_handler(Handshake, self.handle_handshake)
		self._handlers.register_handler(HandshakeResponse, self.handle_handshake_response_as_rm)

	async def _create_connection(self, client_id: str, keep_alive_interval: int):
		await super()._create_connection(client_id, keep_alive_interval)
		self._runningloop.create_task(self._connect_and_run())

	async def _connect_and_run(self) -> None:
		self._received_messages = asyncio.Queue()
		self._restart_connection_event = asyncio.Event()

		logger.debug("Connecting as S2 resource manager.")

		async def wait_till_disconnect() -> None:
			await self._s2_dbus_disconnect_event.wait()

		async def wait_till_connection_restart() -> None:
			await self._restart_connection_event.wait()

		background_tasks = [
			self._runningloop.create_task(wait_till_disconnect()),
			self._runningloop.create_task(self._connect_as_rm()),
			self._runningloop.create_task(wait_till_connection_restart()),
		]

		(done, pending) = await asyncio.wait(
			background_tasks, return_when=asyncio.FIRST_COMPLETED
		)
		if self._current_control_type:
			self._current_control_type.deactivate(self)
			self._current_control_type = None

		for task in done:
			try:
				await task
			except asyncio.CancelledError:
				pass
			except Exception as e:
				logger.info(f"The other party closed the connection: {e}")

		for task in pending:
			try:
				task.cancel()
				await task
			except asyncio.CancelledError:
				pass

		self._destroy_connection()
		logger.debug("Finished S2 connection eventloop.")

	async def _on_s2_message(self, message):
		try:
			s2_msg: S2Message = self.s2_parser.parse_as_any_message(message)
		except json.JSONDecodeError:
			await self._send_and_forget(
				ReceptionStatus(
					subject_message_id=uuid.UUID("00000000-0000-0000-0000-000000000000"),
					status=ReceptionStatusValues.INVALID_DATA,
					diagnostic_label="Not valid json.",
				)
			)
		except S2ValidationError as e:
			json_msg = json.loads(message)
			message_id = json_msg.get("message_id")
			if message_id:
				await self.respond_with_reception_status(
					subject_message_id=message_id,
					status=ReceptionStatusValues.INVALID_MESSAGE,
					diagnostic_label=str(e),
				)
			else:
				await self.respond_with_reception_status(
					subject_message_id=uuid.UUID("00000000-0000-0000-0000-000000000000"),
					status=ReceptionStatusValues.INVALID_DATA,
					diagnostic_label="Message appears valid json but could not find a message_id field.",
				)
		else:
			logger.debug("Received message %s", s2_msg.to_json())

			if isinstance(s2_msg, ReceptionStatus):
				logger.debug(
					"Message is a reception status for %s so registering in cache.",
					s2_msg.subject_message_id,
				)
				await self.reception_status_awaiter.receive_reception_status(s2_msg)
			else:
				await self._received_messages.put(s2_msg)

	async def _connect_as_rm(self) -> None:
		await self.send_msg_and_await_reception_status(
			Handshake(
				message_id=uuid.uuid4(),
				role=self.role,
				supported_protocol_versions=[S2_VERSION],
			)
		)
		logger.debug(
			"Send handshake to CEM, expecting Handshake and HandshakeResponse."
		)

		await self._handle_received_messages()

	async def handle_handshake(
		self, _: "S2ResourceManagerItem", message: S2Message, send_okay: Awaitable[None]
	) -> None:
		if not isinstance(message, Handshake):
			logger.error(
				"Handler for Handshake received a message of the wrong type: %s",
				type(message),
			)
			return

		logger.debug(
			"%s supports S2 protocol versions: %s",
			message.role,
			message.supported_protocol_versions,
		)
		await send_okay

	async def handle_handshake_response_as_rm(
		self, _: "S2ResourceManagerItem", message: S2Message, send_okay: Awaitable[None]
	) -> None:
		if not isinstance(message, HandshakeResponse):
			logger.error(
				"Handler for HandshakeResponse received a message of the wrong type: %s",
				type(message),
			)
			return

		logger.debug("Received HandshakeResponse %s", message.to_json())

		logger.debug(
			"CEM selected to use version %s", message.selected_protocol_version
		)
		await send_okay
		logger.debug("Handshake complete. Sending first ResourceManagerDetails.")

		await self.send_resource_manager_details()

	async def handle_select_control_type_as_rm(
		self, _: "S2ResourceManagerItem", message: S2Message, send_okay: Awaitable[None]
	) -> None:
		if not isinstance(message, SelectControlType):
			logger.error(
				"Handler for SelectControlType received a message of the wrong type: %s",
				type(message),
			)
			return

		await send_okay

		logger.debug(
			"CEM selected control type %s. Activating control type.",
			message.control_type,
		)

		control_types_by_protocol_name = {
			c.get_protocol_control_type(): c for c in self.control_types
		}
		selected_control_type: Optional[S2ControlType] = (
			control_types_by_protocol_name.get(message.control_type)
		)

		if self._current_control_type is not None:
			if asyncio.iscoroutinefunction(self._current_control_type.deactivate):
				await self._current_control_type.deactivate(self)
			else:
				await self._runningloop.run_in_executor(
					None, self._current_control_type.deactivate, self
				)

		self._current_control_type = selected_control_type

		if self._current_control_type is not None:
			if asyncio.iscoroutinefunction(self._current_control_type.activate):
				await self._current_control_type.activate(self)
			else:
				await self._runningloop.run_in_executor(
					None, self._current_control_type.activate, self
				)
			self._current_control_type.register_handlers(self._handlers)

	async def _send_and_forget(self, s2_msg: S2Message) -> None:
		if not self.is_connected:
			raise RuntimeError(
				"Cannot send messages if client connection is not yet established."
			)

		json_msg = s2_msg.to_json()
		logger.debug("Sending message %s", json_msg)

		try:
			self._send_message(json_msg)
		except Exception as e:
			logger.exception("Unable to send message %s due to %s", s2_msg, str(e))
			self._restart_connection_event.set()

	async def respond_with_reception_status(
		self, subject_message_id: uuid.UUID, status: ReceptionStatusValues, diagnostic_label: str
	) -> None:
		logger.debug(
			"Responding to message %s with status %s", subject_message_id, status
		)
		await self._send_and_forget(
			ReceptionStatus(
				subject_message_id=subject_message_id,
				status=status,
				diagnostic_label=diagnostic_label,
			)
		)

	def respond_with_reception_status_sync(
		self, subject_message_id: uuid.UUID, status: ReceptionStatusValues, diagnostic_label: str
	) -> None:
		asyncio.run_coroutine_threadsafe(
			self.respond_with_reception_status(
				subject_message_id, status, diagnostic_label
			),
			self._runningloop,
		).result()

	async def send_msg_and_await_reception_status(
		self,
		s2_msg: S2Message,
		timeout_reception_status: float = 5.0,
		raise_on_error: bool = True,
	) -> ReceptionStatus:
		await self._send_and_forget(s2_msg)
		logger.debug(
			"Waiting for ReceptionStatus for %s %s seconds",
			s2_msg.message_id,  # type: ignore[attr-defined, union-attr]
			timeout_reception_status,
		)
		try:
			reception_status = await self.reception_status_awaiter.wait_for_reception_status(
				s2_msg.message_id, timeout_reception_status  # type: ignore[attr-defined, union-attr]
			)
		except TimeoutError:
			logger.error(
				"Did not receive a reception status on time for %s",
				s2_msg.message_id,  # type: ignore[attr-defined, union-attr]
			)
			self._restart_connection_event.set()
			raise

		if reception_status.status != ReceptionStatusValues.OK and raise_on_error:
			raise RuntimeError(
				f"ReceptionStatus was not OK but rather {reception_status.status}"
			)

		return reception_status

	def send_msg_and_await_reception_status_sync(
		self,
		s2_msg: S2Message,
		timeout_reception_status: float = 5.0,
		raise_on_error: bool = True,
	) -> ReceptionStatus:
		return asyncio.run_coroutine_threadsafe(
			self.send_msg_and_await_reception_status(
				s2_msg, timeout_reception_status, raise_on_error
			),
			self._runningloop,
		).result()

	async def _handle_received_messages(self) -> None:
		while True:
			msg = await self._received_messages.get()
			await self._handlers.handle_message(self, msg)

	async def send_resource_manager_details(
		self,
		control_types: List[S2ControlType] = None,
		asset_details: AssetDetails = None
	) -> ReceptionStatus:
		if control_types is not None:
			self.control_types = control_types
		if asset_details is not None:
			self.asset_details = asset_details
		reception_status: ReceptionStatus = await self.send_msg_and_await_reception_status(
			self.asset_details.to_resource_manager_details(self.control_types)
		)
		return reception_status

	def send_resource_manager_details_sync(
		self,
		control_types: List[S2ControlType] = None,
		asset_details: AssetDetails = None
	) -> ReceptionStatus:
		return asyncio.run_coroutine_threadsafe(
			self.send_resource_manager_details(
				control_types, asset_details
			),
			self._runningloop,
		).result()