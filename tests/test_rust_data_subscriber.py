import msgpack
"""Tests for RustDataSubscriber._dispatch method."""
import asyncio
import os
import sys

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
    })

    assert received["symbol"] == "BTCUSDT"
    assert received["market"] == "perp"
    assert received["bids"] == [[50000.0, 1.0]]


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
