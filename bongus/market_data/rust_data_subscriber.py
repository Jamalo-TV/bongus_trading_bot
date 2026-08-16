"""Rust telemetry subscriber with both callback and event-handler APIs."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from bongus.ipc.protocol import validate_terminal_order_event
from bongus.ipc.telemetry import (
    DEFAULT_PRIMARY_CONSUMER_ID,
    TelemetryClient,
    TelemetryDelivery,
)

logger = logging.getLogger(__name__)

EventHandler = Callable[[dict[str, Any]], Awaitable[None] | None]
DurableReceiptAppender = Callable[[dict[str, Any]], bool]
DurableReceiptCompleter = Callable[[int], None]
DurableReceiptLoader = Callable[[], list[dict[str, Any]]]


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
        durable_receipt_append: DurableReceiptAppender | None = None,
        durable_receipt_complete: DurableReceiptCompleter | None = None,
        durable_receipt_loader: DurableReceiptLoader | None = None,
        projection_retry_delay: float = 0.25,
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
        receipt_callbacks = (
            durable_receipt_append,
            durable_receipt_complete,
            durable_receipt_loader,
        )
        if any(callback is not None for callback in receipt_callbacks) and not all(
            callback is not None for callback in receipt_callbacks
        ):
            raise ValueError(
                "durable receipt append, completion, and loader callbacks "
                "must be configured together"
            )
        self._durable_receipt_append = durable_receipt_append
        self._durable_receipt_complete = durable_receipt_complete
        self._durable_receipt_loader = durable_receipt_loader
        self._projection_retry_delay = max(0.0, float(projection_retry_delay))
        self._projection_queue: asyncio.Queue[dict[str, Any]] | None = None
        self._projection_task: asyncio.Task[None] | None = None
        self._projection_enqueued: set[int] = set()

    @property
    def is_connected(self) -> bool:
        return self._connected_event.is_set()

    @property
    def projection_backlog(self) -> int:
        """Count durable events not yet projection-checkpointed."""

        return len(self._projection_enqueued)

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
        """Append and ACK critical events, then project them in journal order."""

        if self._durable_receipt_loader is not None:
            await self.recover_pending_projections()
        try:
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
                    # A raw durability failure or sequence gap occurs before
                    # ACK and therefore forces Rust replay. Projection failures
                    # are retried independently from committed raw receipts.
                    logger.exception(
                        "Telemetry receipt failed; reconnecting without ACK: %s",
                        exc,
                    )
                    with suppress(Exception):
                        await deliveries.aclose()
                    delay = max(
                        0.0,
                        float(getattr(self.client, "reconnect_delay", 2.0)),
                    )
                    if delay:
                        await asyncio.sleep(delay)
                finally:
                    self._connected_event.clear()
        finally:
            await self._stop_projection_worker()

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

    def _ensure_projection_worker(self) -> asyncio.Queue[dict[str, Any]]:
        queue = self._projection_queue
        if queue is None:
            queue = asyncio.Queue()
            self._projection_queue = queue
        task = self._projection_task
        if task is None or task.done():
            self._projection_task = asyncio.create_task(
                self._run_projection_worker(),
                name="rust_telemetry_projection",
            )
        return queue

    def _enqueue_projection(self, event: dict[str, Any]) -> None:
        sequence = int(event["telemetry_sequence"])
        if sequence in self._projection_enqueued:
            return
        self._projection_enqueued.add(sequence)
        self._ensure_projection_worker().put_nowait(dict(event))

    async def recover_pending_projections(self) -> None:
        """Queue committed receipts left incomplete by a prior process."""

        loader = self._durable_receipt_loader
        if loader is None:
            return
        for event in loader():
            self._enqueue_projection(event)

    async def wait_for_projection_idle(
        self,
        *,
        timeout: float | None = None,
    ) -> None:
        """Wait until every queued durable projection is checkpointed."""

        queue = self._projection_queue
        if queue is None:
            return
        waiter = queue.join()
        if timeout is None:
            await waiter
        else:
            await asyncio.wait_for(waiter, timeout=max(0.0, float(timeout)))

    async def _stop_projection_worker(self) -> None:
        task = self._projection_task
        self._projection_task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        queue = self._projection_queue
        if queue is not None:
            while True:
                try:
                    event = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                self._projection_enqueued.discard(
                    int(event["telemetry_sequence"])
                )
                queue.task_done()

    async def _run_projection_worker(self) -> None:
        queue = self._projection_queue
        if queue is None:
            return
        while True:
            event = await queue.get()
            sequence = int(event["telemetry_sequence"])
            try:
                projected = False
                while not projected:
                    try:
                        await self._dispatch_event(event)
                        projected = True
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception(
                            "Durable telemetry projection %d failed; "
                            "retaining ordered receipt for retry",
                            sequence,
                        )
                        await asyncio.sleep(self._projection_retry_delay)

                completed = False
                while not completed:
                    try:
                        completer = self._durable_receipt_complete
                        if completer is None:
                            raise RuntimeError(
                                "durable projection completion callback is unavailable"
                            )
                        completer(sequence)
                        completed = True
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # Projection may have committed already. Retry only the
                        # checkpoint, never the business callback, within this
                        # process. A process death is recovered idempotently
                        # from the still-PROCESSING raw receipt.
                        logger.exception(
                            "Durable telemetry checkpoint %d failed; retrying",
                            sequence,
                        )
                        await asyncio.sleep(self._projection_retry_delay)
            finally:
                self._projection_enqueued.discard(sequence)
                queue.task_done()

    async def _process_delivery(self, delivery: TelemetryDelivery) -> None:
        # Protocol-v3 terminal exposure records are validated before raw append
        # and before ACK. Invalid terminal semantics therefore remain replayable
        # in Rust instead of entering authoritative Python history.
        validate_terminal_order_event(delivery.event)
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
            # Rust may replay a raw receipt if the prior ACK did not reach its
            # durable cursor. The original receipt is already queued or fully
            # projected, so only re-ACK it here.
            self._connection_last_sequence = sequence
            await delivery.acknowledge()
            return

        if high_water is not None and sequence != high_water + 1:
            raise TelemetrySequenceGap(
                f"durable telemetry gap: expected {high_water + 1}, received {sequence}"
            )

        appender = self._durable_receipt_append
        if appender is not None:
            # This commit is intentionally the only work before ACK. Economic,
            # lifecycle and REST-enrichment handlers run on the ordered worker
            # after the receipt is safely replayable from local storage.
            should_project = appender(dict(delivery.event))
            self._telemetry_high_water = sequence
            self._connection_last_sequence = sequence
            try:
                await delivery.acknowledge()
            finally:
                # Even ACK loss must not strand an already committed receipt in
                # this process; Rust replay is separately duplicate-suppressed.
                if should_project:
                    self._enqueue_projection(delivery.event)
            return

        # Backward-compatible boundary for observer/tests without a configured
        # raw receipt store. Production LiveTrader always configures one.
        await self._dispatch_event(delivery.event)
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

    @staticmethod
    def _invoke_market_callback(
        callback: Callable[..., None],
        payload: dict[str, Any],
    ) -> None:
        """Forward the full envelope without breaking legacy callbacks.

        Signature filtering is performed before invocation, so a ``TypeError``
        raised inside the callback is never mistaken for an old callback shape.
        """

        try:
            parameters = inspect.signature(callback).parameters.values()
        except (TypeError, ValueError):
            callback(**payload)
            return
        if any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        ):
            callback(**payload)
            return
        accepted_names = {
            parameter.name
            for parameter in parameters
            if parameter.kind
            in {
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            }
        }
        callback(
            **{
                name: value
                for name, value in payload.items()
                if name in accepted_names
            }
        )

    def _dispatch(self, event: dict[str, Any]) -> None:
        event_name = str(event.get("event", ""))
        if event_name == "L2Depth" and self._on_depth is not None:
            self._invoke_market_callback(
                self._on_depth,
                {
                    "symbol": str(event.get("symbol", "")).upper(),
                    "market": event.get("market", ""),
                    "bids": event.get("bids", []),
                    "asks": event.get("asks", []),
                    "connection_id": event.get("connection_id"),
                    "exchange_event_time_ms": event.get(
                        "exchange_event_time_ms"
                    ),
                    "receive_time_ms": event.get("receive_time_ms"),
                    "process_time_ms": event.get("process_time_ms"),
                    "persist_time_ms": event.get("persist_time_ms"),
                    "first_update_id": event.get("first_update_id"),
                    "last_update_id": event.get("last_update_id"),
                    "final_update_id": event.get("final_update_id"),
                    "previous_final_update_id": event.get(
                        "previous_final_update_id"
                    ),
                    "is_snapshot": event.get("is_snapshot"),
                    "sequence_contiguous": event.get("sequence_contiguous"),
                },
            )
        elif event_name == "OrderUpdate" and self._on_order_update is not None:
            self._on_order_update(
                schema_version=event.get("schema_version"),
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
                connection_id=event.get("connection_id"),
                exchange_event_time_ms=event.get("exchange_event_time_ms"),
                maker_fills=event.get("maker_fills"),
                taker_fills=event.get("taker_fills"),
                spot_fill_price=event.get("spot_fill_price"),
                perp_fill_price=event.get("perp_fill_price"),
                terminal_summary_version=event.get("terminal_summary_version"),
                filled_qty_decimal=event.get("filled_qty_decimal"),
                requested_quantity=event.get("requested_quantity_decimal"),
                requested_quantity_decimal=event.get(
                    "requested_quantity_decimal"
                ),
                risk_adjusted_requested_quantity=event.get(
                    "risk_adjusted_requested_quantity_decimal"
                ),
                risk_adjusted_requested_quantity_decimal=event.get(
                    "risk_adjusted_requested_quantity_decimal"
                ),
                common_quantity=event.get(
                    "normalized_common_entry_quantity_decimal"
                ),
                normalized_common_entry_quantity_decimal=event.get(
                    "normalized_common_entry_quantity_decimal"
                ),
                spot_target_quantity=event.get("spot_target_quantity_decimal"),
                perp_target_quantity=event.get("futures_target_quantity_decimal"),
                spot_target_quantity_decimal=event.get(
                    "spot_target_quantity_decimal"
                ),
                futures_target_quantity_decimal=event.get(
                    "futures_target_quantity_decimal"
                ),
                spot_cumulative_filled_qty=event.get(
                    "spot_cumulative_filled_quantity_decimal"
                ),
                perp_cumulative_filled_qty=event.get(
                    "futures_cumulative_filled_quantity_decimal"
                ),
                actual_spot_inventory=event.get(
                    "actual_spot_inventory_decimal"
                ),
                actual_futures_inventory=event.get(
                    "actual_futures_inventory_decimal"
                ),
                actual_spot_inventory_decimal=event.get(
                    "actual_spot_inventory_decimal"
                ),
                actual_futures_inventory_decimal=event.get(
                    "actual_futures_inventory_decimal"
                ),
                exit_spot_quantity_decimal=event.get(
                    "exit_spot_quantity_decimal"
                ),
                exit_futures_quantity_decimal=event.get(
                    "exit_futures_quantity_decimal"
                ),
                spot_cumulative_filled_quantity_decimal=event.get(
                    "spot_cumulative_filled_quantity_decimal"
                ),
                futures_cumulative_filled_quantity_decimal=event.get(
                    "futures_cumulative_filled_quantity_decimal"
                ),
                spot_vwap_decimal=event.get("spot_vwap_decimal"),
                futures_vwap_decimal=event.get("futures_vwap_decimal"),
                spot_generations=event.get("spot_generations"),
                perp_generations=event.get("futures_generations"),
                futures_generations=event.get("futures_generations"),
                spot_final_status=event.get("spot_final_status"),
                perp_final_status=event.get("futures_final_status"),
                futures_final_status=event.get("futures_final_status"),
                commissions=event.get("commissions"),
                commission_assets=event.get("commission_assets"),
                commission_status=event.get("commission_status"),
                unvalued_commission_assets=event.get(
                    "unvalued_commission_assets"
                ),
                deadline_classification=event.get("deadline_classification"),
                receive_time_ms=event.get("receive_time_ms"),
                process_time_ms=event.get("process_time_ms"),
                persist_time_ms=event.get("persist_time_ms"),
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
                terminal_sequence=event.get("terminal_sequence"),
                terminal_watermark=event.get("terminal_watermark"),
            )
        elif event_name == "MarkPrice" and self._on_mark_price is not None:
            self._invoke_market_callback(
                self._on_mark_price,
                {
                    "symbol": str(event.get("symbol", "")).upper(),
                    "mark_price": event.get("mark_price", 0.0),
                    "next_funding_rate": event.get("next_funding_rate", 0.0),
                    "next_funding_time_ms": event.get("next_funding_time_ms"),
                    "connection_id": event.get("connection_id"),
                    "exchange_event_time_ms": event.get(
                        "exchange_event_time_ms"
                    ),
                    "receive_time_ms": event.get("receive_time_ms"),
                    "process_time_ms": event.get("process_time_ms"),
                    "persist_time_ms": event.get("persist_time_ms"),
                    "first_update_id": event.get("first_update_id"),
                    "last_update_id": event.get("last_update_id"),
                    "final_update_id": event.get("final_update_id"),
                    "previous_final_update_id": event.get(
                        "previous_final_update_id"
                    ),
                    "is_snapshot": event.get("is_snapshot"),
                    "sequence_contiguous": event.get("sequence_contiguous"),
                },
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
