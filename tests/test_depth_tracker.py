"""Tests for DepthTracker — 4-cache per-symbol depth tracking."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'bongus', 'market_data')))

from depth_tracker import DepthTracker


def _make_levels(base_price: float, qty: float, n: int = 5) -> list:
    """Helper: create N levels at base_price each with qty."""
    return [(base_price, qty)] * n


def test_initial_state_all_zero():
    """Fresh tracker has zero depth for any symbol."""
    t = DepthTracker()
    assert t.spot_ask_depth("BTCUSDT") == 0.0
    assert t.perp_bid_depth("BTCUSDT") == 0.0
    assert t.get_entry_depth("BTCUSDT") == 0.0
    assert t.get_exit_depth("ETHUSDT") == 0.0


def test_spot_depth_updates_on_spot_event():
    """Spot L2Depth event updates spot caches only."""
    t = DepthTracker()
    bids = _make_levels(50_000.0, 2.0)  # 5 levels × 50_000 × 2 = 500_000 USD bid
    asks = _make_levels(50_100.0, 1.0)  # 5 levels × 50_100 × 1 = 250_500 USD ask
    t.on_l2depth("BTCUSDT", "spot", bids, asks)

    assert abs(t.spot_bid_depth("BTCUSDT") - 500_000.0) < 1.0
    assert abs(t.spot_ask_depth("BTCUSDT") - 250_500.0) < 1.0
    # Perp caches remain zero
    assert t.perp_bid_depth("BTCUSDT") == 0.0
    assert t.perp_ask_depth("BTCUSDT") == 0.0


def test_perp_depth_updates_on_perp_event():
    """Perp L2Depth event updates perp caches only."""
    t = DepthTracker()
    bids = _make_levels(50_000.0, 3.0)  # 750_000 USD bid
    asks = _make_levels(50_050.0, 2.0)  # 500_500 USD ask
    t.on_l2depth("BTCUSDT", "perp", bids, asks)

    assert abs(t.perp_bid_depth("BTCUSDT") - 750_000.0) < 1.0
    assert abs(t.perp_ask_depth("BTCUSDT") - 500_500.0) < 1.0
    # Spot caches remain zero
    assert t.spot_bid_depth("BTCUSDT") == 0.0


def test_get_entry_depth_is_min_of_spot_ask_and_perp_bid():
    """Entry depth = min(spot_ask, perp_bid) — the bottleneck for entering long spot + short perp.

    spot: bids=_make_levels(3000, 10) [ignored for entry], asks=_make_levels(3005, 2) → spot_ask = 5*3005*2 = 30_050
    perp: bids=_make_levels(3000, 5) → perp_bid = 5*3000*5 = 75_000, asks ignored for entry
    entry = min(30_050, 75_000) = 30_050
    """
    t = DepthTracker()
    t.on_l2depth("ETHUSDT", "spot", _make_levels(3000.0, 10.0), _make_levels(3005.0, 2.0))
    t.on_l2depth("ETHUSDT", "perp", _make_levels(3000.0, 5.0), _make_levels(3005.0, 8.0))

    assert abs(t.spot_ask_depth("ETHUSDT") - 30_050.0) < 1.0
    assert abs(t.perp_bid_depth("ETHUSDT") - 75_000.0) < 1.0
    assert abs(t.get_entry_depth("ETHUSDT") - 30_050.0) < 1.0  # min of the two


def test_get_exit_depth_is_min_of_spot_bid_and_perp_ask():
    """Exit depth = min(spot_bid, perp_ask) — the bottleneck for exiting long spot + short perp.

    spot: bids=_make_levels(150, 100) → spot_bid = 5*150*100 = 75_000, asks ignored for exit
    perp: bids ignored for exit, asks=_make_levels(150.5, 30) → perp_ask = 5*150.5*30 = 22_575
    exit = min(75_000, 22_575) = 22_575
    """
    t = DepthTracker()
    t.on_l2depth("SOLUSDT", "spot", _make_levels(150.0, 100.0), _make_levels(150.5, 50.0))
    t.on_l2depth("SOLUSDT", "perp", _make_levels(150.0, 200.0), _make_levels(150.5, 30.0))

    assert abs(t.spot_bid_depth("SOLUSDT") - 75_000.0) < 1.0
    assert abs(t.perp_ask_depth("SOLUSDT") - 22_575.0) < 1.0
    assert abs(t.get_exit_depth("SOLUSDT") - 22_575.0) < 1.0  # min of the two


def test_multiple_symbols_are_independent():
    """Updates to one symbol do not affect another."""
    t = DepthTracker()
    t.on_l2depth("BTCUSDT", "perp", _make_levels(50_000.0, 1.0), _make_levels(50_100.0, 1.0))
    assert t.perp_bid_depth("ETHUSDT") == 0.0


def test_depth_uses_top_20_levels_only():
    """Only the first 20 levels are summed; extra levels are ignored."""
    t = DepthTracker()
    # 30 levels — only first 20 should count
    bids = [(100.0, 1.0)] * 30
    t.on_l2depth("DOGEUSDT", "spot", bids, [])
    expected = 100.0 * 1.0 * 20  # 2000.0
    assert abs(t.spot_bid_depth("DOGEUSDT") - expected) < 1e-9
