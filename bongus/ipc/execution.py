from typing import Any, Dict

import msgpack
import zmq


class ExecutionClient:
    """Client for pushing trade instructions to the Rust Execution Engine."""

    def __init__(self, endpoint: str = "tcp://127.0.0.1:5555"):
        self.endpoint = endpoint
        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.PUSH)
        self.socket.connect(self.endpoint)

    def send_order_intent(self, payload: Dict[str, Any]) -> None:
        """
        Sends an order intent to the execution engine.
        Payload format typically includes: intent (e.g. 'Enter', 'Exit'),
        symbol, max_slippage_bps, exposure_scale, etc.
        """
        self.socket.send(msgpack.packb(payload))

    def close(self) -> None:
        """Closes the socket and context."""
        self.socket.close()
        self.context.term()
