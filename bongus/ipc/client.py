"""Unified IPC client for communicating with the Rust execution engine.

Consolidates:
  - ZMQ PUSH (Python → Rust commands) from RustIPCBridge
  - TCP reader (Rust → Python events) from dashboard/alerter/web_dashboard
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import msgpack
import zmq

logger = logging.getLogger(__name__)

# Default endpoints matching the Rust engine configuration
DEFAULT_COMMAND_ENDPOINT = "tcp://127.0.0.1:5555"
DEFAULT_EVENT_HOST = "127.0.0.1"
DEFAULT_EVENT_PORT = 9000


class ExecutionClient:
    """Bidirectional IPC client for the Rust execution engine.

    Usage (commands):
        client = ExecutionClient()
        client.send_instruction({"symbol": "BTCUSDT", "intent": "ENTER_LONG", ...})
        client.close()

    Usage (events):
        async for event in ExecutionClient.listen_events():
            print(event)
    """

    def __init__(self, command_endpoint: str = DEFAULT_COMMAND_ENDPOINT) -> None:
        self._context = zmq.Context()
        self._socket = self._context.socket(zmq.PUSH)
        self._socket.connect(command_endpoint)
        logger.info("ZMQ PUSH connected to %s", command_endpoint)

    def send_instruction(self, payload: dict[str, Any]) -> None:
        """Send a msgpack-encoded instruction to the Rust engine."""
        packed = msgpack.packb(payload)
        self._socket.send(packed)
        logger.info("Sent instruction to Rust: %s", payload)

    def close(self) -> None:
        self._socket.close()
        self._context.term()

    def __enter__(self) -> ExecutionClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── Event stream (Rust → Python) ────────────────────────────────────

    @staticmethod
    async def listen_events(
        host: str = DEFAULT_EVENT_HOST,
        port: int = DEFAULT_EVENT_PORT,
        reconnect_delay: float = 2.0,
    ) -> AsyncIterator[dict[str, Any]]:
        """Async generator that yields JSON events from the Rust TCP stream.

        Automatically reconnects on disconnect.
        """
        while True:
            try:
                reader, _ = await asyncio.open_connection(host, port)
                logger.info("Connected to Rust event stream at %s:%d", host, port)
                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    text = line.decode("utf-8").strip()
                    if not text:
                        continue
                    try:
                        yield json.loads(text)
                    except json.JSONDecodeError:
                        logger.debug("Non-JSON line from engine: %s", text)
            except OSError as e:
                logger.warning(
                    "Event stream connection error: %s (retrying in %.1fs)",
                    e, reconnect_delay,
                )
            await asyncio.sleep(reconnect_delay)
