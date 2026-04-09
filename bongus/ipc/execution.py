"""ZeroMQ client for pushing order intents into the Rust engine."""

from __future__ import annotations

from typing import Any

import msgpack
import zmq

from bongus.core.config import EXECUTION_SEND_TIMEOUT_MS


class ExecutionClient:
    def __init__(self, endpoint: str = "tcp://127.0.0.1:5555", send_timeout_ms: int = EXECUTION_SEND_TIMEOUT_MS):
        self.endpoint = endpoint
        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.PUSH)
        self.socket.setsockopt(zmq.SNDTIMEO, send_timeout_ms)
        self.socket.setsockopt(zmq.LINGER, 0)
        self.socket.connect(self.endpoint)

    def send_order_intent(self, payload: dict[str, Any]) -> bool:
        try:
            self.socket.send(msgpack.packb(payload), zmq.NOBLOCK)
            return True
        except zmq.ZMQError:
            return False

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
