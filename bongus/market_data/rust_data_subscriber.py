"""Rust telemetry subscriber with both callback and event-handler APIs."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from bongus.ipc.telemetry import (
    DEFAULT_PRIMARY_CONSUMER_ID,
    TelemetryClient,
    TelemetryDelivery,
)

logger = logging.getLogger(__name__)

EventHandler = Callable[[dict[str, Any]], Awaitable[None] | None]


class TelemetrySequenceGap(RuntimeError):
    """A durable event was missing or regressed on one connection."""


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
        on_order_rejected: Callable[..., None] | None = None,
        on_connection_state: Callable[[bool], None] | None = None,
        client: TelemetryClient | None = None,
        consumer_id: str | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self.client = client or TelemetryClient(
            host=host,
            port=port,
            read_timeout=10.0,
        )
        self._consumer_id = (
            os.environ.get(
                "EXECUTION_TELEMETRY_PRIMARY_CONSUMER_ID",
                DEFAULT_PRIMARY_CONSUMER_ID,
            )
            if consumer_id is None
            else consumer_id
        )
        self._handlers: dict[str, list[EventHandler]] = {}
        self._on_depth = on_depth
        self._on_order_update = on_order_update
        self._on_mark_price = on_mark_price
        self._on_heartbeat_ack = on_heartbeat_ack
        self._on_volume_bar = on_volume_bar
        self._on_order_rejected = on_order_rejected
        self._on_connection_state = on_connection_state
        self._connected_event = asyncio.Event()
        self._telemetry_high_water: int | None = None
        self._connection_last_sequence: int | None = None

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
        """Dispatch sequentially and ACK only committed durable events."""

        while True:
            deliveries = self.client.stream_deliveries(
                consumer_id=self._consumer_id,
                on_connection_state=self._set_connection_state,
            )
            try:
                async for delivery in deliveries:
                    await self._process_delivery(delivery)
            except asyncio.CancelledError:
                with suppress(Exception):
                    await deliveries.aclose()
                raise
            except Exception as exc:
                # Closing this connection forces Rust to replay from its last
                # durable ACK. A failed handler or sequence gap is never ACKed.
                logger.exception(
                    "Telemetry dispatch failed; reconnecting without ACK: %s",
                    exc,
                )
                with suppress(Exception):
                    await deliveries.aclose()
                delay = max(0.0, float(getattr(self.client, "reconnect_delay", 2.0)))
                if delay:
                    await asyncio.sleep(delay)
            finally:
                self._connected_event.clear()

    def _set_connection_state(self, connected: bool) -> None:
        # Monotonicity is connection-local; duplicate suppression uses the
        # process-lifetime dispatched high-water across reconnects.
        self._connection_last_sequence = None
        if connected:
            self._connected_event.set()
        else:
            self._connected_event.clear()
        if self._on_connection_state is not None:
            self._on_connection_state(bool(connected))

    async def _process_delivery(self, delivery: TelemetryDelivery) -> None:
        sequence = delivery.sequence
        if sequence is None:
            await self._dispatch_event(delivery.event)
            return

        connection_last = self._connection_last_sequence
        if connection_last is not None and sequence < connection_last:
            raise TelemetrySequenceGap(
                "durable telemetry sequence regressed on one connection: "
                f"{sequence} < {connection_last}"
            )

        high_water = self._telemetry_high_water
        if high_water is not None and sequence <= high_water:
            # Rust may replay a successfully dispatched event if the prior ACK
            # did not reach its durable cursor. Do not repeat the side effect;
            # re-ACK only the sequence this connection has actually delivered.
            self._connection_last_sequence = sequence
            await delivery.acknowledge()
            return

        if high_water is not None and sequence != high_water + 1:
            raise TelemetrySequenceGap(
                f"durable telemetry gap: expected {high_water + 1}, received {sequence}"
            )

        await self._dispatch_event(delivery.event)
        # Mark dispatch before attempting the socket write. If ACK delivery
        # fails, the next replay is suppressed and safely re-ACKed.
        self._telemetry_high_water = sequence
        self._connection_last_sequence = sequence
        await delivery.acknowledge()

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
                cumulative_filled_qty=event.get("cumulative_filled_qty"),
                client_order_id=event.get("client_order_id", ""),
                avg_fill_price=event.get("avg_fill_price"),
                last_fill_price=event.get("last_fill_price"),
                cumulative_quote_qty=event.get("cumulative_quote_qty"),
                commission=event.get("commission"),
                commission_asset=event.get("commission_asset"),
                realized_pnl=event.get("realized_pnl"),
                maker=event.get("maker"),
                execution_type=event.get("execution_type"),
                event_time_ms=event.get("event_time_ms"),
                maker_fills=event.get("maker_fills"),
                taker_fills=event.get("taker_fills"),
                spot_fill_price=event.get("spot_fill_price"),
                perp_fill_price=event.get("perp_fill_price"),
                market=event.get("market"),
                side=event.get("side"),
                order_id=event.get("order_id"),
                trade_id=event.get("trade_id"),
                account_id=event.get("account_id"),
                environment=event.get("environment"),
                strategy_id=event.get("strategy_id"),
                cycle_id=event.get("cycle_id"),
                intent_id=event.get("intent_id"),
                leg_id=event.get("leg_id"),
                config_version_hash=event.get("config_version_hash"),
                telemetry_schema_version=event.get("telemetry_schema_version"),
                telemetry_sequence=event.get("telemetry_sequence"),
                telemetry_ack_required=event.get("telemetry_ack_required"),
                telemetry_replay=event.get("telemetry_replay"),
            )
        elif event_name == "MarkPrice" and self._on_mark_price is not None:
            self._on_mark_price(
                symbol=str(event.get("symbol", "")).upper(),
                mark_price=event.get("mark_price", 0.0),
                next_funding_rate=event.get("next_funding_rate", 0.0),
                next_funding_time_ms=event.get("next_funding_time_ms"),
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
        elif event_name == "OrderRejected" and self._on_order_rejected is not None:
            self._on_order_rejected(
                symbol=str(event.get("symbol", "")).upper(),
                intent=event.get("intent", ""),
                intent_id=event.get("intent_id"),
                reason=event.get("reason", ""),
            )
