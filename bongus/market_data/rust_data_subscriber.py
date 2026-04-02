"""Async dispatcher for Rust telemetry events."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from bongus.ipc.telemetry import TelemetryClient

EventHandler = Callable[[dict[str, Any]], Awaitable[None] | None]


class RustDataSubscriber:
    def __init__(self, client: TelemetryClient | None = None) -> None:
        self.client = client or TelemetryClient()
        self._handlers: dict[str, list[EventHandler]] = {}

    def on(self, event_name: str, handler: EventHandler) -> None:
        self._handlers.setdefault(event_name, []).append(handler)

    async def run(self) -> None:
        async for event in self.client.stream_events():
            if event is None:
                continue
            event_name = str(event.get("event", ""))
            for handler in self._handlers.get(event_name, []):
                result = handler(event)
                if result is not None:
                    await result
