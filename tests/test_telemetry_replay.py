from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any, cast

import msgpack
import pytest

from bongus.engine.state_store import StateReader, StateWriter

from bongus.ipc.telemetry import (
    DEFAULT_PRIMARY_CONSUMER_ID,
    TELEMETRY_SCHEMA_VERSION,
    TelemetryAcknowledgementError,
    TelemetryClient,
    TelemetryDelivery,
    TelemetryProtocolError,
    durable_telemetry_sequence,
)
from bongus.market_data.rust_data_subscriber import (
    RustDataSubscriber,
    TelemetrySequenceGap,
)


def _durable_event(sequence: int, *, replay: bool = False) -> dict[str, Any]:
    return {
        "event": "OrderUpdate",
        "symbol": "BTCUSDT",
        "status": "FILLED",
        "filled_qty": 0.1,
        "client_order_id": f"order-{sequence}",
        "telemetry_schema_version": TELEMETRY_SCHEMA_VERSION,
        "telemetry_sequence": sequence,
        "telemetry_ack_required": True,
        "telemetry_replay": replay,
    }


class _RecordingWriter:
    def __init__(
        self,
        *,
        fail_drain: bool = False,
        on_ack: Callable[[int], None] | None = None,
        order: list[str] | None = None,
    ) -> None:
        self.payloads: list[bytes] = []
        self.fail_drain = fail_drain
        self.on_ack = on_ack
        self.order = order
        self.closed = False

    def write(self, payload: bytes) -> None:
        if self.order is not None:
            self.order.append("ack_write")
        self.payloads.append(payload)

    async def drain(self) -> None:
        if self.fail_drain:
            raise ConnectionResetError("simulated ACK loss")
        if self.on_ack is not None:
            self.on_ack(self.ack_sequences[-1])

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    @property
    def ack_sequences(self) -> list[int]:
        return [
            int(json.loads(payload.decode("utf-8"))["high_water_sequence"])
            for payload in self.payloads
        ]


def _delivery(
    event: dict[str, Any],
    writer: _RecordingWriter,
    *,
    consumer_id: str | None = DEFAULT_PRIMARY_CONSUMER_ID,
) -> TelemetryDelivery:
    return TelemetryDelivery(
        event=event,
        sequence=durable_telemetry_sequence(event),
        _writer=writer,
        _consumer_id=consumer_id,
        _write_lock=asyncio.Lock(),
    )


def test_ack_wire_contract_is_exact_idempotent_and_newline_delimited() -> None:
    async def scenario() -> None:
        writer = _RecordingWriter()
        delivery = _delivery(_durable_event(42, replay=True), writer)

        assert await delivery.acknowledge() is True
        assert await delivery.acknowledge() is False
        assert len(writer.payloads) == 1
        assert writer.payloads[0].endswith(b"\n")
        assert json.loads(writer.payloads[0]) == {
            "event": "TelemetryAck",
            "schema_version": 1,
            "consumer_id": "python-live-trader",
            "high_water_sequence": 42,
        }

    asyncio.run(scenario())


def test_observer_delivery_cannot_advance_the_durable_cursor() -> None:
    async def scenario() -> None:
        writer = _RecordingWriter()
        delivery = _delivery(_durable_event(1), writer, consumer_id=None)
        with pytest.raises(TelemetryAcknowledgementError, match="observer"):
            await delivery.acknowledge()
        assert writer.payloads == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "updates, message",
    [
        ({"telemetry_schema_version": 2}, "unsupported"),
        ({"telemetry_sequence": 0}, "sequence"),
        ({"telemetry_sequence": True}, "sequence"),
        ({"telemetry_replay": "yes"}, "replay"),
        ({"telemetry_ack_required": 1}, "ack_required"),
    ],
)
def test_invalid_durable_metadata_fails_closed(
    updates: dict[str, Any], message: str
) -> None:
    event = _durable_event(1)
    event.update(updates)
    with pytest.raises(TelemetryProtocolError, match=message):
        durable_telemetry_sequence(event)


def test_legacy_and_ephemeral_events_remain_unsequenced() -> None:
    assert durable_telemetry_sequence({"event": "MarkPrice", "mark_price": 1.0}) is None
    assert (
        durable_telemetry_sequence(
            {"event": "LegacyDiagnostic", "telemetry_ack_required": False}
        )
        is None
    )


def test_subscriber_uses_the_rust_primary_consumer_environment(monkeypatch) -> None:
    monkeypatch.setenv("EXECUTION_TELEMETRY_PRIMARY_CONSUMER_ID", "primary-trader-a")
    assert RustDataSubscriber()._consumer_id == "primary-trader-a"
    assert RustDataSubscriber(consumer_id="explicit-primary")._consumer_id == "explicit-primary"


def test_observer_stream_decodes_durable_event_without_sending_ack(monkeypatch) -> None:
    class _Reader:
        def __init__(self) -> None:
            self.sent = False

        async def read(self, _size: int) -> bytes:
            if not self.sent:
                self.sent = True
                return cast(bytes, msgpack.packb(_durable_event(1), use_bin_type=True))
            await asyncio.Future()
            return b""

    async def scenario() -> None:
        reader = _Reader()
        writer = _RecordingWriter()

        async def open_connection(*_args: Any, **_kwargs: Any) -> tuple[_Reader, _RecordingWriter]:
            return reader, writer

        monkeypatch.setattr(asyncio, "open_connection", open_connection)
        stream = TelemetryClient().stream_events()
        event = await anext(stream)
        assert event is not None
        assert event["telemetry_sequence"] == 1
        assert writer.payloads == []
        await stream.aclose()
        assert writer.payloads == []
        assert writer.closed is True

    asyncio.run(scenario())


def test_handler_completes_before_ack_and_failure_sends_no_ack() -> None:
    async def scenario() -> None:
        order: list[str] = []
        writer = _RecordingWriter(order=order)
        subscriber = RustDataSubscriber()

        async def succeeds(_event: dict[str, Any]) -> None:
            order.append("dispatch_commit")

        subscriber.on("OrderUpdate", succeeds)
        await subscriber._process_delivery(_delivery(_durable_event(1), writer))
        assert order == ["dispatch_commit", "ack_write"]

        failed_writer = _RecordingWriter()
        failed_subscriber = RustDataSubscriber()

        async def fails(_event: dict[str, Any]) -> None:
            raise RuntimeError("database commit failed")

        failed_subscriber.on("OrderUpdate", fails)
        with pytest.raises(RuntimeError, match="database commit failed"):
            await failed_subscriber._process_delivery(
                _delivery(_durable_event(1), failed_writer)
            )
        assert failed_writer.payloads == []
        assert failed_subscriber._telemetry_high_water is None

    asyncio.run(scenario())


def test_ack_loss_replay_is_suppressed_without_repeating_handler() -> None:
    async def scenario() -> None:
        calls: list[int] = []
        subscriber = RustDataSubscriber()

        async def handler(event: dict[str, Any]) -> None:
            calls.append(int(event["telemetry_sequence"]))

        subscriber.on("OrderUpdate", handler)
        failed_writer = _RecordingWriter(fail_drain=True)
        with pytest.raises(TelemetryAcknowledgementError, match="ACK loss"):
            await subscriber._process_delivery(_delivery(_durable_event(1), failed_writer))
        assert calls == [1]
        assert subscriber._telemetry_high_water == 1

        subscriber._set_connection_state(True)
        replay_writer = _RecordingWriter()
        await subscriber._process_delivery(
            _delivery(_durable_event(1, replay=True), replay_writer)
        )
        assert calls == [1]
        assert replay_writer.ack_sequences == [1]

    asyncio.run(scenario())


class _AttemptClient:
    """Deterministic connection attempts for subscriber reconnect tests."""

    reconnect_delay = 0.0

    def __init__(self, attempts: list[list[TelemetryDelivery]]) -> None:
        self._attempts = attempts
        self.connection_count = 0

    def stream_deliveries(
        self,
        *,
        consumer_id: str | None = None,
        on_connection_state: Callable[[bool], Any] | None = None,
    ) -> AsyncGenerator[TelemetryDelivery, None]:
        async def generate() -> AsyncGenerator[TelemetryDelivery, None]:
            attempt = self.connection_count
            self.connection_count += 1
            if on_connection_state is not None:
                result = on_connection_state(True)
                if inspect.isawaitable(result):
                    await result
            try:
                if attempt < len(self._attempts):
                    for delivery in self._attempts[attempt]:
                        yield delivery
                else:
                    await asyncio.Future()
            finally:
                if on_connection_state is not None:
                    result = on_connection_state(False)
                    if inspect.isawaitable(result):
                        await result

        return generate()


def test_handler_failure_closes_connection_and_replays_before_ack() -> None:
    async def scenario() -> None:
        acknowledged = asyncio.Event()
        first_writer = _RecordingWriter()
        second_writer = _RecordingWriter(
            on_ack=lambda sequence: acknowledged.set() if sequence == 1 else None
        )
        client = _AttemptClient(
            [
                [_delivery(_durable_event(1), first_writer)],
                [_delivery(_durable_event(1, replay=True), second_writer)],
            ]
        )
        subscriber = RustDataSubscriber(client=cast(TelemetryClient, client))
        calls = 0

        async def handler(_event: dict[str, Any]) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("first commit failed")

        subscriber.on("OrderUpdate", handler)
        task = asyncio.create_task(subscriber.run())
        await asyncio.wait_for(acknowledged.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert client.connection_count >= 2
        assert calls == 2
        assert first_writer.payloads == []
        assert second_writer.ack_sequences == [1]

    asyncio.run(scenario())


def test_gap_forces_reconnect_then_replay_fills_gap_without_duplicate_dispatch() -> None:
    async def scenario() -> None:
        completed = asyncio.Event()
        first_writer = _RecordingWriter()
        second_writer = _RecordingWriter(
            on_ack=lambda sequence: completed.set() if sequence == 3 else None
        )
        client = _AttemptClient(
            [
                [
                    _delivery(_durable_event(1), first_writer),
                    _delivery(_durable_event(3), first_writer),
                ],
                [
                    _delivery(_durable_event(1, replay=True), second_writer),
                    _delivery(_durable_event(2, replay=True), second_writer),
                    _delivery(_durable_event(3, replay=True), second_writer),
                ],
            ]
        )
        subscriber = RustDataSubscriber(client=cast(TelemetryClient, client))
        dispatched: list[int] = []

        async def handler(event: dict[str, Any]) -> None:
            dispatched.append(int(event["telemetry_sequence"]))

        subscriber.on("OrderUpdate", handler)
        task = asyncio.create_task(subscriber.run())
        await asyncio.wait_for(completed.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert client.connection_count >= 2
        assert dispatched == [1, 2, 3]
        assert first_writer.ack_sequences == [1]
        assert second_writer.ack_sequences == [1, 2, 3]

    asyncio.run(scenario())


def test_regression_on_one_connection_fails_even_when_sequence_was_dispatched() -> None:
    async def scenario() -> None:
        subscriber = RustDataSubscriber()
        subscriber._set_connection_state(True)
        writer = _RecordingWriter()
        await subscriber._process_delivery(_delivery(_durable_event(2), writer))
        with pytest.raises(TelemetrySequenceGap, match="regressed"):
            await subscriber._process_delivery(
                _delivery(_durable_event(1, replay=True), writer)
            )

    asyncio.run(scenario())


def test_order_callback_receives_durable_replay_identity() -> None:
    received: dict[str, Any] = {}

    def callback(
        symbol: str,
        status: str,
        filled_qty: float,
        client_order_id: str,
        **kwargs: Any,
    ) -> None:
        assert (symbol, status, filled_qty, client_order_id) == (
            "BTCUSDT",
            "FILLED",
            0.1,
            "order-9",
        )
        received.update(kwargs)

    event = _durable_event(9, replay=True)
    RustDataSubscriber(on_order_update=callback)._dispatch(event)

    assert received["telemetry_schema_version"] == 1
    assert received["telemetry_sequence"] == 9
    assert received["telemetry_ack_required"] is True
    assert received["telemetry_replay"] is True


def test_process_restart_requires_transactional_handler_dedupe() -> None:
    """Subscriber memory cannot close the commit-before-ACK crash window.

    A new process correctly receives the replay. Its durable handler must use
    the forwarded telemetry sequence (or a stronger exchange event identity)
    in the same transaction as the business effect.
    """

    async def scenario() -> None:
        durable_seen: set[int] = set()
        callback_invocations: list[int] = []
        business_effects: list[int] = []

        async def transactional_handler(event: dict[str, Any]) -> None:
            sequence = int(event["telemetry_sequence"])
            callback_invocations.append(sequence)
            if sequence in durable_seen:
                return
            durable_seen.add(sequence)
            business_effects.append(sequence)

        for replay in (False, True):
            subscriber = RustDataSubscriber()
            subscriber.on("OrderUpdate", transactional_handler)
            await subscriber._process_delivery(
                _delivery(_durable_event(77, replay=replay), _RecordingWriter())
            )

        assert callback_invocations == [77, 77]
        assert business_effects == [77]

    asyncio.run(scenario())


def test_sqlite_receipt_survives_restart_and_rejects_sequence_conflicts(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    event = {"event": "OrderUpdate", "symbol": "BTCUSDT", "status": "FILLED"}
    writer = StateWriter(str(database))
    try:
        assert writer.begin_durable_telemetry(
            sequence=77,
            schema_version=1,
            event=event,
        )
        # A crash before completion must re-run the callback.
        assert writer.begin_durable_telemetry(
            sequence=77,
            schema_version=1,
            event=event,
        )
        writer.complete_durable_telemetry(77)
    finally:
        writer.close()

    restarted = StateWriter(str(database))
    try:
        assert not restarted.begin_durable_telemetry(
            sequence=77,
            schema_version=1,
            event=event,
        )
        with pytest.raises(ValueError, match="identity conflict"):
            restarted.begin_durable_telemetry(
                sequence=77,
                schema_version=1,
                event={**event, "status": "CANCELED"},
            )
    finally:
        restarted.close()


def test_raw_execution_telemetry_sequence_is_unique(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    writer = StateWriter(str(database))
    reader = StateReader(str(database))
    payload = {
        "symbol": "BTCUSDT",
        "status": "FILLED",
        "filled_qty": 1.0,
        "telemetry_schema_version": 1,
        "telemetry_sequence": 9,
    }
    try:
        writer.record_execution_event(payload)
        writer.record_execution_event({**payload, "telemetry_replay": True})
        rows = reader.get_execution_events(limit=10)
        assert len(rows) == 1
        assert rows[0]["telemetry_sequence"] == 9
    finally:
        reader.close()
        writer.close()
