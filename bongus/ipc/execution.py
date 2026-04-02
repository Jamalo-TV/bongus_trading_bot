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

    def send_order_intent(self, payload: dict[str, Any]) -> None:
        self.socket.send(msgpack.packb(payload))

    def close(self) -> None:
        self.socket.close()
        self.context.term()
