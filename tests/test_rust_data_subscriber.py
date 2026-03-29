"""Tests for RustDataSubscriber._dispatch method."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'bongus', 'market_data')))

from rust_data_subscriber import RustDataSubscriber


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
            }
        )

    sub = RustDataSubscriber(on_order_update=on_order_update)
    sub._dispatch({
        "event": "OrderUpdate",
        "symbol": "ETHUSDT",
        "status": "FILLED",
        "filled_qty": 1.5,
        "client_order_id": "abc123",
        "avg_fill_price": 2450.25,
        "maker": True,
    })

    assert received["symbol"] == "ETHUSDT"
    assert received["status"] == "FILLED"
    assert received["filled_qty"] == 1.5
    assert received["client_order_id"] == "abc123"
    assert received["avg_fill_price"] == 2450.25
    assert received["maker"] is True


def test_dispatch_unknown_event_does_not_crash():
    sub = RustDataSubscriber()
    sub._dispatch({"event": "UnknownEvent", "data": "whatever"})  # must not raise


def test_dispatch_no_callbacks_does_not_crash():
    sub = RustDataSubscriber()  # no callbacks registered
    sub._dispatch({"event": "L2Depth", "symbol": "X", "market": "spot", "bids": [], "asks": []})
