"""Rust telemetry subscriber with both callback and event-handler APIs."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from bongus.ipc.telemetry import TelemetryClient

logger = logging.getLogger(__name__)

EventHandler = Callable[[dict[str, Any]], Awaitable[None] | None]


class RustDataSubscriber:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9000,
        on_depth: Callable[..., None] | None = None,
        on_order_update: Callable[..., None] | None = None,
        on_mark_price: Callable[..., None] | None = None,
        on_heartbeat_ack: Callable[..., None] | None = None,
        on_volume_bar: Callable[..., None] | None = None,
        client: TelemetryClient | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self.client = client or TelemetryClient(host=host, port=port)
        self._handlers: dict[str, list[EventHandler]] = {}
        self._on_depth = on_depth
        self._on_order_update = on_order_update
        self._on_mark_price = on_mark_price
        self._on_heartbeat_ack = on_heartbeat_ack
        self._on_volume_bar = on_volume_bar
        self._connected_event = asyncio.Event()

    @property
    def is_connected(self) -> bool:
        return self._connected_event.is_set()

    async def wait_until_connected(self, timeout: float | None = None) -> bool:
        try:
            if timeout is None:
                await self._connected_event.wait()
            else:
                await asyncio.wait_for(self._connected_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def on(self, event_name: str, handler: EventHandler) -> None:
        self._handlers.setdefault(event_name, []).append(handler)

    async def run(self) -> None:
        try:
            if any(
                callback is not None
                for callback in (
                    self._on_depth,
                    self._on_order_update,
                    self._on_mark_price,
                    self._on_heartbeat_ack,
                    self._on_volume_bar,
                )
            ):
                await self._run_callback_mode()
            else:
                self._connected_event.set()
                async for event in self.client.stream_events():
                    if event is None:
                        continue
                    await self._dispatch_event(event)
        finally:
            self._connected_event.clear()

    async def _run_callback_mode(self) -> None:
        reader, writer = await asyncio.open_connection(self._host, self._port)
        self._connected_event.set()
        try:
            while True:
                line = await reader.readline()
                if not line:
                    logger.warning("Rust engine closed connection")
                    return
                try:
                    event = json.loads(line.decode())
                except json.JSONDecodeError:
                    continue
                await self._dispatch_event(event)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _dispatch_event(self, event: dict[str, Any]) -> None:
        event_name = str(event.get("event", ""))
        for handler in self._handlers.get(event_name, []):
            result = handler(event)
            if result is not None:
                await result
        self._dispatch(event)

    def _dispatch(self, event: dict[str, Any]) -> None:
        event_name = str(event.get("event", ""))
        if event_name == "L2Depth" and self._on_depth is not None:
            self._on_depth(
                symbol=str(event.get("symbol", "")).upper(),
                market=event.get("market", ""),
                bids=event.get("bids", []),
                asks=event.get("asks", []),
            )
        elif event_name == "OrderUpdate" and self._on_order_update is not None:
            self._on_order_update(
                symbol=str(event.get("symbol", "")).upper(),
                status=event.get("status", ""),
                filled_qty=event.get("filled_qty", 0.0),
                client_order_id=event.get("client_order_id", ""),
                avg_fill_price=event.get("avg_fill_price"),
                last_fill_price=event.get("last_fill_price"),
                cumulative_quote_qty=event.get("cumulative_quote_qty"),
                commission=event.get("commission"),
                commission_asset=event.get("commission_asset"),
                realized_pnl=event.get("realized_pnl"),
                maker=event.get("maker"),
                execution_type=event.get("execution_type"),
                spot_fill_price=event.get("spot_fill_price"),
                perp_fill_price=event.get("perp_fill_price"),
            )
        elif event_name == "MarkPrice" and self._on_mark_price is not None:
            self._on_mark_price(
                symbol=str(event.get("symbol", "")).upper(),
                mark_price=event.get("mark_price", 0.0),
                next_funding_rate=event.get("next_funding_rate", 0.0),
            )
        elif event_name == "HeartbeatAck" and self._on_heartbeat_ack is not None:
            self._on_heartbeat_ack(
                heartbeat_id=event.get("heartbeat_id"),
                status=event.get("status", ""),
                ts_ms=event.get("ts_ms"),
            )
        elif event_name == "VolumeBar" and self._on_volume_bar is not None:
            self._on_volume_bar(
                symbol=str(event.get("symbol", "")).upper(),
                minute_start_ms=event.get("minute_start_ms"),
                notional_usd=event.get("notional_usd", 0.0),
            )
