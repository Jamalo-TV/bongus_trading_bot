import logging
from typing import Any, Dict

import msgpack
import zmq

logger = logging.getLogger(__name__)

_SEND_TIMEOUT_MS = 500  # block at most 500ms before declaring Rust engine unreachable


class ExecutionClient:
    """Client for pushing trade instructions to the Rust Execution Engine."""

    def __init__(self, endpoint: str = "tcp://127.0.0.1:5555"):
        self.endpoint = endpoint
        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.PUSH)
        self.socket.setsockopt(zmq.SNDTIMEO, _SEND_TIMEOUT_MS)
        self.socket.setsockopt(zmq.LINGER, 0)  # don't block on close either
        self.socket.connect(self.endpoint)

    def send_order_intent(self, payload: Dict[str, Any]) -> bool:
        """
        Sends an order intent to the execution engine.

        Returns True on success, False if the Rust engine is not consuming
        (e.g. crashed). The caller should treat False as a critical alert —
        the order was NOT sent.
        """
        try:
            self.socket.send(msgpack.packb(payload), zmq.NOBLOCK)
            return True
        except zmq.Again:
            logger.critical(
                "ZMQ send timed out for %s %s — Rust engine may be down. Order NOT sent.",
                payload.get("intent"), payload.get("symbol"),
            )
            return False

    def send_heartbeat(self, heartbeat_id: str) -> bool:
        return self.send_order_intent(
            {
                "symbol": "SYSTEM",
                "intent": "HEARTBEAT",
                "quantity": 0.0,
                "urgency": 0.0,
                "max_slippage_bps": 0.0,
                "exposure_scale": 0.0,
                "heartbeat_id": heartbeat_id,
            }
        )

    def close(self) -> None:
        """Closes the socket and context."""
        self.socket.close()
        self.context.term()
