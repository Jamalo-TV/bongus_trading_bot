"""Tests for RustDataSubscriber._dispatch method."""

import asyncio
import os
import sys

import msgpack

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'bongus', 'market_data')))

from rust_data_subscriber import RustDataSubscriber


def test_connection_state_callback_tracks_each_transport_epoch():
    observed = []
    sub = RustDataSubscriber(on_connection_state=observed.append)

    sub._set_connection_state(True)
    sub._set_connection_state(False)

    assert observed == [True, False]
    assert not sub.is_connected


def test_dispatch_l2depth_calls_on_depth():
    received = {}

    def on_depth(symbol, market, bids, asks):
        received.update({"symbol": symbol, "market": market, "bids": bids, "asks": asks})

    sub = RustDataSubscriber(on_depth=on_depth)
    sub._dispatch({
        "event": "L2Depth",
        "symbol": "BTCUSDT",
        "market": "perp",
        "bids": [[50000.0, 1.0]],
        "asks": [[50100.0, 0.5]],
        "connection_id": "legacy-callback-compatible",
        "exchange_event_time_ms": 1_000,
        "receive_time_ms": 1_001,
        "process_time_ms": 1_002,
        "persist_time_ms": 1_003,
        "final_update_id": 1,
        "is_snapshot": True,
        "sequence_contiguous": True,
    })

    assert received["symbol"] == "BTCUSDT"
    assert received["market"] == "perp"
    assert received["bids"] == [[50000.0, 1.0]]


def test_dispatch_l2depth_forwards_complete_timing_and_sequence_envelope():
    received = {}

    def on_depth(**kwargs):
        received.update(kwargs)

    event = {
        "event": "L2Depth",
        "symbol": "BTCUSDT",
        "market": "spot",
        "bids": [[100.0, 2.0]],
        "asks": [[100.1, 2.0]],
        "connection_id": "spot-connection-7",
        "exchange_event_time_ms": None,
        "receive_time_ms": 1_010,
        "process_time_ms": 1_020,
        "persist_time_ms": None,
        "first_update_id": 40,
        "final_update_id": 42,
        "previous_final_update_id": 39,
        "is_snapshot": True,
        "sequence_contiguous": True,
    }

    RustDataSubscriber(on_depth=on_depth)._dispatch(event)

    for field in (
        "connection_id",
        "exchange_event_time_ms",
        "receive_time_ms",
        "process_time_ms",
        "persist_time_ms",
        "first_update_id",
        "final_update_id",
        "previous_final_update_id",
        "is_snapshot",
        "sequence_contiguous",
    ):
        assert received[field] == event[field]


def test_dispatch_mark_price_forwards_timing_and_update_metadata():
    received = {}

    def on_mark_price(**kwargs):
        received.update(kwargs)

    event = {
        "event": "MarkPrice",
        "symbol": "ETHUSDT",
        "mark_price": 2_500.0,
        "next_funding_rate": 0.0001,
        "next_funding_time_ms": 10_000,
        "connection_id": "mark-connection-3",
        "exchange_event_time_ms": 2_000,
        "receive_time_ms": 2_010,
        "process_time_ms": 2_020,
        "persist_time_ms": None,
        "final_update_id": 99,
        "sequence_contiguous": True,
    }

    RustDataSubscriber(on_mark_price=on_mark_price)._dispatch(event)

    assert received["connection_id"] == "mark-connection-3"
    assert received["exchange_event_time_ms"] == 2_000
    assert received["receive_time_ms"] == 2_010
    assert received["process_time_ms"] == 2_020
    assert received["persist_time_ms"] is None
    assert received["final_update_id"] == 99
    assert received["sequence_contiguous"] is True


def test_dispatch_order_update_calls_on_order_update():
    received = {}

    def on_order_update(symbol, status, filled_qty, client_order_id, **kwargs):
        received.update(
            {
                "symbol": symbol,
                "status": status,
                "filled_qty": filled_qty,
                "client_order_id": client_order_id,
                "avg_fill_price": kwargs.get("avg_fill_price"),
                "maker": kwargs.get("maker"),
                "event_time_ms": kwargs.get("event_time_ms"),
                "cumulative_filled_qty": kwargs.get("cumulative_filled_qty"),
                "market": kwargs.get("market"),
                "side": kwargs.get("side"),
                "order_id": kwargs.get("order_id"),
                "trade_id": kwargs.get("trade_id"),
                "cycle_id": kwargs.get("cycle_id"),
                "leg_id": kwargs.get("leg_id"),
            }
        )

    sub = RustDataSubscriber(on_order_update=on_order_update)
    sub._dispatch({
        "event": "OrderUpdate",
        "symbol": "ETHUSDT",
        "status": "FILLED",
        "filled_qty": 1.5,
        "cumulative_filled_qty": 2.5,
        "client_order_id": "abc123",
        "avg_fill_price": 2450.25,
        "maker": True,
        "event_time_ms": 1_735_680_000_123,
        "market": "perp",
        "side": "SELL",
        "order_id": 77,
        "trade_id": 88,
        "cycle_id": "cycle-1",
        "leg_id": "perp-1",
    })

    assert received["symbol"] == "ETHUSDT"
    assert received["status"] == "FILLED"
    assert received["filled_qty"] == 1.5
    assert received["client_order_id"] == "abc123"
    assert received["avg_fill_price"] == 2450.25
    assert received["maker"] is True
    assert received["event_time_ms"] == 1_735_680_000_123
    assert received["cumulative_filled_qty"] == 2.5
    assert received["market"] == "perp"
    assert received["side"] == "SELL"
    assert received["order_id"] == 77
    assert received["trade_id"] == 88
    assert received["cycle_id"] == "cycle-1"
    assert received["leg_id"] == "perp-1"


def test_dispatch_unknown_event_does_not_crash():
    sub = RustDataSubscriber()
    sub._dispatch({"event": "UnknownEvent", "data": "whatever"})  # must not raise


def test_terminal_summary_forwards_exact_per_leg_inventory_and_fee_evidence():
    received = {}

    def on_order_update(**kwargs):
        received.update(kwargs)

    event = {
        "event": "OrderUpdate",
        "symbol": "BTCUSDT",
        "status": "FILLED",
        "filled_qty": 1.0,
        "client_order_id": "spot-cid",
        "schema_version": 3,
        "terminal_summary_version": 3,
        "requested_quantity_decimal": "1",
        "normalized_common_entry_quantity_decimal": "1",
        "spot_cumulative_filled_quantity_decimal": "1",
        "futures_cumulative_filled_quantity_decimal": "1",
        "actual_spot_inventory_decimal": "0.999",
        "actual_futures_inventory_decimal": "1",
        "exit_spot_quantity_decimal": "0",
        "exit_futures_quantity_decimal": "0",
        "spot_vwap_decimal": "50000",
        "futures_vwap_decimal": "50010",
        "risk_adjusted_requested_quantity_decimal": "1",
        "spot_target_quantity_decimal": "1",
        "futures_target_quantity_decimal": "1",
        "spot_generations": [{"client_order_id": "spot-cid", "status": "FILLED"}],
        "futures_generations": [{"client_order_id": "futures-cid", "status": "FILLED"}],
        "commissions": [
            {"asset": "BTC", "amount": "0.001", "identity": "spot:BTCUSDT:1:2"}
        ],
        "commission_status": "VALUED_OR_ZERO",
        "spot_final_status": "FILLED",
        "futures_final_status": "FILLED",
        "connection_id": "private-connection-1",
        "exchange_event_time_ms": 100,
        "receive_time_ms": 110,
        "process_time_ms": 120,
        "persist_time_ms": 123,
    }

    RustDataSubscriber(on_order_update=on_order_update)._dispatch(event)

    assert received["spot_cumulative_filled_qty"] == "1"
    assert received["perp_cumulative_filled_qty"] == "1"
    assert received["actual_spot_inventory"] == "0.999"
    assert received["actual_futures_inventory"] == "1"
    assert received["commissions"] == event["commissions"]
    assert received["commission_status"] == "VALUED_OR_ZERO"
    assert received["connection_id"] == "private-connection-1"
    assert received["exchange_event_time_ms"] == 100
    assert received["receive_time_ms"] == 110
    assert received["process_time_ms"] == 120
    assert received["persist_time_ms"] == 123
    assert received["schema_version"] == 3
    assert received["requested_quantity_decimal"] == "1"
    assert received["normalized_common_entry_quantity_decimal"] == "1"
    assert received["actual_spot_inventory_decimal"] == "0.999"
    assert received["actual_futures_inventory_decimal"] == "1"
    assert received["exit_spot_quantity_decimal"] == "0"
    assert received["exit_futures_quantity_decimal"] == "0"
    assert received["spot_cumulative_filled_quantity_decimal"] == "1"
    assert received["futures_cumulative_filled_quantity_decimal"] == "1"
    assert received["spot_vwap_decimal"] == "50000"
    assert received["futures_vwap_decimal"] == "50010"
    assert received["risk_adjusted_requested_quantity_decimal"] == "1"
    assert received["spot_target_quantity_decimal"] == "1"
    assert received["futures_target_quantity_decimal"] == "1"
    assert received["futures_generations"] == event["futures_generations"]
    assert received["futures_final_status"] == "FILLED"


def test_dispatch_no_callbacks_does_not_crash():
    sub = RustDataSubscriber()  # no callbacks registered
    sub._dispatch({"event": "L2Depth", "symbol": "X", "market": "spot", "bids": [], "asks": []})


def test_event_handler_receives_depth_sequence_metadata():
    received = {}

    async def handler(event):
        received.update(event)

    sub = RustDataSubscriber()
    sub.on("L2Depth", handler)
    asyncio.run(
        sub._dispatch_event(
            {
                "event": "L2Depth",
                "symbol": "BTCUSDT",
                "market": "perp",
                "bids": [],
                "asks": [],
                "first_update_id": 10,
                "final_update_id": 12,
                "previous_final_update_id": 9,
                "sequence_contiguous": True,
            }
        )
    )
    assert received["final_update_id"] == 12
    assert received["sequence_contiguous"] is True


def test_callback_mode_reconnects_after_connection_refused(monkeypatch):
    received = {}
    packed_event = msgpack.packb(
        {
            "event": "L2Depth",
            "symbol": "BTCUSDT",
            "market": "perp",
            "bids": [[50000.0, 1.0]],
            "asks": [[50100.0, 0.5]],
        },
        use_bin_type=True,
    )

    class _FakeReader:
        def __init__(self, chunks):
            self._chunks = list(chunks)

        async def read(self, _size):
            if self._chunks:
                return self._chunks.pop(0)
            await asyncio.sleep(0)
            return b""

    class _FakeWriter:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

        async def wait_closed(self):
            return None

    attempts = {"count": 0}
    original_sleep = asyncio.sleep
    connected = asyncio.Event()

    async def fake_open_connection(*_args, **_kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise ConnectionRefusedError
        if attempts["count"] == 2:
            return _FakeReader([packed_event, b""]), _FakeWriter()
        await connected.wait()
        return _FakeReader([]), _FakeWriter()

    async def fast_sleep(_delay):
        await original_sleep(0)

    def on_depth(symbol, market, bids, asks):
        received.update({"symbol": symbol, "market": market, "bids": bids, "asks": asks})
        connected.set()

    sub = RustDataSubscriber(on_depth=on_depth)
    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    async def runner():
        task = asyncio.create_task(sub.run())
        await asyncio.wait_for(connected.wait(), timeout=1)
        await original_sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(runner())

    assert attempts["count"] >= 2
    assert received["symbol"] == "BTCUSDT"
    assert received["market"] == "perp"
    assert received["bids"] == [[50000.0, 1.0]]
