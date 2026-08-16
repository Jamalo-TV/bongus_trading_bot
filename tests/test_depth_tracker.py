"""Tests for DepthTracker — 4-cache per-symbol depth tracking."""
import os
import sys
from typing import Any

import pytest

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


def test_mid_prices_and_basis_are_computed_from_top_of_book():
    """Tracker exposes spot/perp mids and current basis from best bid/ask."""
    t = DepthTracker()
    t.on_l2depth("BTCUSDT", "spot", [(99.9, 10.0)], [(100.1, 10.0)])
    t.on_l2depth("BTCUSDT", "perp", [(100.2, 10.0)], [(100.4, 10.0)])

    spot_mid = t.spot_mid_price("BTCUSDT")
    perp_mid = t.perp_mid_price("BTCUSDT")
    basis = t.basis_pct("BTCUSDT")
    assert spot_mid is not None
    assert perp_mid is not None
    assert basis is not None
    assert abs(spot_mid - 100.0) < 1e-9
    assert abs(perp_mid - 100.3) < 1e-9
    assert abs(basis - 0.003) < 1e-9


def test_entry_spread_exposes_legs_and_combined_scanner_metric() -> None:
    t = DepthTracker()
    t.on_l2depth("BTCUSDT", "spot", [(99.99, 10.0)], [(100.01, 10.0)])
    t.on_l2depth("BTCUSDT", "perp", [(100.17, 10.0)], [(100.23, 10.0)])

    spot_spread_bps, perp_spread_bps = t.entry_leg_spreads_bps("BTCUSDT")

    assert spot_spread_bps == pytest.approx(t.spot_spread_bps("BTCUSDT"))
    assert perp_spread_bps == pytest.approx(t.perp_spread_bps("BTCUSDT"))
    assert t.entry_spread_bps("BTCUSDT") == pytest.approx(
        spot_spread_bps + perp_spread_bps
    )


def test_exchange_event_age_is_not_reset_by_arrival_time() -> None:
    tracker = DepthTracker(clock=lambda: 50.0, wall_clock=lambda: 1_000.0)
    common_timing: dict[str, Any] = {
        "exchange_event_time_ms": 995_000,
        "receive_time_ms": 1_000_000,
        "process_time_ms": 1_000_000,
        "persist_time_ms": 1_000_000,
        "final_update_id": 10,
        "is_snapshot": True,
        "sequence_contiguous": True,
    }
    tracker.on_l2depth(
        "BTCUSDT",
        "spot",
        [(99.9, 10.0)],
        [(100.0, 10.0)],
        connection_id="spot-1",
        **common_timing,
    )
    tracker.on_l2depth(
        "BTCUSDT",
        "perp",
        [(100.1, 10.0)],
        [(100.2, 10.0)],
        connection_id="perp-1",
        **common_timing,
    )

    assert tracker.entry_data_age_seconds("BTCUSDT") == pytest.approx(5.0)
    capacity = tracker.executable_pair_capacity(
        "BTCUSDT",
        100.0,
        max_age_seconds=4.0,
    )
    assert not capacity.fully_executable
    assert "spot:stale_book" in capacity.rejection_reasons
    assert "perp:stale_book" in capacity.rejection_reasons


def test_strict_timing_fails_closed_when_envelope_is_missing() -> None:
    tracker = DepthTracker(wall_clock=lambda: 1_000.0)
    tracker.on_l2depth(
        "BTCUSDT", "spot", [(99.9, 10.0)], [(100.0, 10.0)]
    )
    tracker.on_l2depth(
        "BTCUSDT", "perp", [(100.1, 10.0)], [(100.2, 10.0)]
    )

    assert tracker.entry_data_age_seconds("BTCUSDT") == float("inf")
    assert not tracker.has_entry_book("BTCUSDT")
    capacity = tracker.executable_pair_capacity("BTCUSDT", 100.0)
    assert "spot:missing_connection_id" in capacity.rejection_reasons
    assert "perp:missing_exchange_event_time" in capacity.rejection_reasons


def test_spot_snapshot_can_use_receive_time_but_futures_requires_exchange_time() -> None:
    tracker = DepthTracker(wall_clock=lambda: 1_000.0)
    timing: dict[str, Any] = {
        "connection_id": "public-connection-1",
        "receive_time_ms": 999_900,
        "process_time_ms": 999_950,
        "persist_time_ms": None,
        "final_update_id": 10,
        "is_snapshot": True,
        "sequence_contiguous": True,
    }
    tracker.on_l2depth(
        "BTCUSDT",
        "spot",
        [(99.9, 10.0)],
        [(100.0, 10.0)],
        exchange_event_time_ms=None,
        **timing,
    )
    tracker.on_l2depth(
        "BTCUSDT",
        "perp",
        [(100.1, 10.0)],
        [(100.2, 10.0)],
        exchange_event_time_ms=None,
        **timing,
    )

    spot_capacity = tracker.executable_leg_capacity(
        "BTCUSDT", "spot", "buy", 100.0
    )
    perp_capacity = tracker.executable_leg_capacity(
        "BTCUSDT", "perp", "sell", 100.0
    )

    assert spot_capacity.fully_executable
    assert spot_capacity.book_age_seconds == pytest.approx(0.1)
    assert not perp_capacity.fully_executable
    assert "missing_exchange_event_time" in perp_capacity.rejection_reasons


def test_depth_continuity_gap_invalidates_a_recent_book() -> None:
    tracker = DepthTracker(wall_clock=lambda: 1_000.0)
    base: dict[str, Any] = {
        "connection_id": "spot-1",
        "receive_time_ms": 999_900,
        "process_time_ms": 999_950,
        "persist_time_ms": 1_000_000,
        "sequence_contiguous": True,
    }
    tracker.on_l2depth(
        "BTCUSDT",
        "spot",
        [(99.9, 10.0)],
        [(100.0, 10.0)],
        exchange_event_time_ms=999_800,
        final_update_id=10,
        is_snapshot=True,
        **base,
    )
    tracker.on_l2depth(
        "BTCUSDT",
        "spot",
        [(99.9, 10.0)],
        [(100.0, 10.0)],
        exchange_event_time_ms=999_850,
        first_update_id=12,
        previous_final_update_id=10,
        final_update_id=12,
        is_snapshot=False,
        **base,
    )

    capacity = tracker.executable_leg_capacity(
        "BTCUSDT", "spot", "buy", 100.0
    )
    assert not capacity.fully_executable
    assert "depth_update_range_gap" in capacity.rejection_reasons


def test_execution_book_snapshot_captures_bbo_lineage_and_unknowns() -> None:
    tracker = DepthTracker(wall_clock=lambda: 1_000.0)
    missing = tracker.execution_book_snapshot("btcusdt", "spot", "buy")
    assert missing.bid is None
    assert missing.mid is None
    assert missing.executable_depth_usd is None
    assert not missing.complete

    tracker.on_l2depth(
        "BTCUSDT",
        "spot",
        [(99.0, 2.0)],
        [(101.0, 3.0)],
        connection_id="spot-epoch-7",
        exchange_event_time_ms=None,
        receive_time_ms=999_900,
        process_time_ms=999_950,
        persist_time_ms=None,
        final_update_id=42,
        is_snapshot=True,
        sequence_contiguous=True,
    )
    captured = tracker.execution_book_snapshot("btcusdt", "spot", "buy")
    assert captured.symbol == "BTCUSDT"
    assert captured.bid == 99.0
    assert captured.ask == 101.0
    assert captured.mid == 100.0
    assert captured.executable_price == 101.0
    assert captured.executable_depth_usd == 303.0
    assert captured.event_age_seconds == pytest.approx(0.1)
    assert captured.connection_id == "spot-epoch-7"
    assert captured.final_update_id == 42
    assert captured.complete


def test_rest_snapshot_backfills_depth_and_basis_when_ws_is_unavailable():
    """REST snapshots should supply enough data for basis/depth gating."""
    t = DepthTracker()
    t.set_rest_snapshot(
        "BTCUSDT",
        spot_depth_usd=250_000.0,
        perp_depth_usd=300_000.0,
        spot_bid_price=99.9,
        spot_ask_price=100.1,
        perp_bid_price=100.2,
        perp_ask_price=100.4,
    )

    assert abs(t.get_entry_depth("BTCUSDT") - 250_000.0) < 1e-9
    assert abs(t.get_exit_depth("BTCUSDT") - 250_000.0) < 1e-9
    assert abs(t.spot_mid_price("BTCUSDT") - 100.0) < 1e-9
    assert abs(t.perp_mid_price("BTCUSDT") - 100.3) < 1e-9
    basis_pct = t.basis_pct("BTCUSDT")
    assert basis_pct is not None
    assert abs(basis_pct - 0.003) < 1e-9
