"""ZeroMQ client for pushing order intents into the Rust engine."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import msgpack
import zmq

from bongus.core.config import EXECUTION_SEND_TIMEOUT_MS
from bongus.ipc.protocol import (
    CONFIG_SYNC_INTENT,
    DURABLE_COMMAND_INTENTS,
    LEGACY_SAFE_INTENTS,
    ExecutionProtocolError,
)

if TYPE_CHECKING:
    from bongus.engine.state_store import StateWriter

logger = logging.getLogger(__name__)


class ExecutionClient:
    def __init__(
        self,
        endpoint: str = "tcp://127.0.0.1:5555",
        send_timeout_ms: int = EXECUTION_SEND_TIMEOUT_MS,
        *,
        state_writer: StateWriter | None = None,
        producer_id: str = "python-live-trader",
        command_ttl_ms: int = 30_000,
        command_context: dict[str, Any] | Callable[[], dict[str, Any]] | None = None,
    ):
        self.endpoint = endpoint
        self.state_writer = state_writer
        self.producer_id = producer_id
        self.command_ttl_ms = max(1, int(command_ttl_ms))
        self.command_context = command_context
        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.PUSH)
        self.socket.setsockopt(zmq.SNDTIMEO, send_timeout_ms)
        self.socket.setsockopt(zmq.LINGER, 0)
        self.socket.connect(self.endpoint)

    def send_order_intent(self, payload: dict[str, Any]) -> bool:
        intent = str(payload.get("intent") or "").upper()
        if intent in DURABLE_COMMAND_INTENTS:
            if self.state_writer is None:
                logger.error(
                    "Refusing durable %s without a durable execution outbox",
                    intent,
                )
                return False
            try:
                context = (
                    self.command_context()
                    if callable(self.command_context)
                    else dict(self.command_context or {})
                )
                command_payload = dict(context)
                command_payload.update(payload)
                command_payload.setdefault("cycle_id", str(payload.get("intent_id") or ""))
                envelope = self.state_writer.reserve_execution_command(
                    command_payload,
                    producer_id=self.producer_id,
                    ttl_ms=self.command_ttl_ms,
                )
            except (ExecutionProtocolError, ValueError):
                logger.exception("Refusing invalid or conflicting execution command")
                return False
            return self._send_envelope(envelope)

        # Explicit, documented compatibility path. Unknown intents are left
        # unchanged so the Rust engine can emit a fail-closed rejection, but
        # they can never pass the risk-command parser there.
        if intent not in LEGACY_SAFE_INTENTS:
            logger.warning("Sending legacy unknown intent %r for fail-closed rejection", intent)
        return self._send_raw(payload)

    def send_config_sync(
        self,
        *,
        intent_id: str,
        canonical_json: str,
        config_version_hash: str,
        cycle_id: str | None = None,
    ) -> bool:
        """Send one exact effective-config snapshot through the durable outbox.

        Callers should obtain ``canonical_json`` and ``config_version_hash``
        from one ``ConfigManager.canonical_snapshot()`` result.  A unique
        ``intent_id`` per engine synchronization attempt is intentional: a
        restarted Rust process must apply the bytes again rather than merely
        replaying a terminal receipt from its prior in-memory consensus state.
        """

        payload: dict[str, Any] = {
            "intent": CONFIG_SYNC_INTENT,
            "intent_id": str(intent_id),
            "cycle_id": str(cycle_id or intent_id),
            "config_version_hash": str(config_version_hash),
            "config_canonical_json": str(canonical_json),
        }
        return self.send_order_intent(payload)

    def _send_raw(self, payload: dict[str, Any]) -> bool:
        try:
            self.socket.send(msgpack.packb(payload), zmq.NOBLOCK)
            return True
        except zmq.ZMQError:
            return False

    def _send_envelope(self, envelope: dict[str, Any]) -> bool:
        intent_id = str(envelope.get("intent_id") or "")
        try:
            self.socket.send(msgpack.packb(envelope), zmq.NOBLOCK)
        except zmq.ZMQError as exc:
            if self.state_writer is not None:
                self.state_writer.mark_execution_command_send_failed(intent_id, str(exc))
            return False
        if self.state_writer is not None:
            self.state_writer.mark_execution_command_sent(intent_id)
        return True

    def handle_ack(self, event: dict[str, Any]) -> bool:
        """Reconcile one Rust lifecycle ACK into the durable outbox."""

        if self.state_writer is None:
            return False
        return self.state_writer.apply_execution_command_ack(event)

    def replay_pending(self, *, now_ms: int | None = None) -> dict[str, int]:
        """Replay exact non-terminal envelopes after a client restart.

        Expired commands remain in the outbox as ``SEND_FAILED`` and are never
        transmitted. The Rust receipt journal makes non-expired replay safe.
        """

        if self.state_writer is None:
            return {"sent": 0, "expired": 0, "failed": 0}
        current_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
        result = {"sent": 0, "expired": 0, "failed": 0}
        for row in self.state_writer.get_replayable_execution_commands():
            envelope = dict(row["envelope"])
            intent_id = str(envelope.get("intent_id") or "")
            if int(envelope.get("deadline_at_ms") or 0) <= current_ms:
                self.state_writer.mark_execution_command_send_failed(
                    intent_id, "expired_before_replay"
                )
                result["expired"] += 1
                continue
            if self._send_envelope(envelope):
                result["sent"] += 1
            else:
                result["failed"] += 1
        return result

    def send_heartbeat(self, heartbeat_id: str) -> bool:
        return self.send_order_intent({"intent": "HEARTBEAT", "heartbeat_id": heartbeat_id})

    def restore_position_tracking(
        self,
        *,
        symbol: str,
        direction: str,
        qty: float,
        spot_entry_price: float,
        perp_entry_price: float,
        spot_mark_price: float,
        perp_mark_price: float,
        spot_quantity: float | None = None,
        perp_quantity: float | None = None,
    ) -> bool:
        payload: dict[str, Any] = {
            "intent": "RESTORE_POSITION",
            "symbol": symbol,
            "direction": direction,
            "quantity": qty,
            "spot_entry_price": spot_entry_price,
            "perp_entry_price": perp_entry_price,
            "spot_mark_price": spot_mark_price,
            "perp_mark_price": perp_mark_price,
        }
        if spot_quantity is not None:
            payload["spot_quantity"] = spot_quantity
        if perp_quantity is not None:
            payload["perp_quantity"] = perp_quantity
        return self.send_order_intent(payload)

    def close(self) -> None:
        self.socket.close()
        self.context.term()
