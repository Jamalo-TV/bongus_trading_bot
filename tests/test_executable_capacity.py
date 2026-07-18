from __future__ import annotations

import math

import pytest

from bongus.market_data.depth_tracker import DepthTracker


def _seed_pair(tracker: DepthTracker, *, received_at: float = 100.0) -> None:
    tracker.on_l2depth(
        "BTCUSDT",
        "spot",
        bids=[(99.9, 10.0), (99.8, 10.0)],
        asks=[(100.0, 1.0), (101.0, 2.0)],
        received_at=received_at,
    )
    tracker.on_l2depth(
        "BTCUSDT",
        "perp",
        bids=[(100.2, 1.0), (100.0, 1.0)],
        asks=[(100.3, 10.0), (100.4, 10.0)],
        received_at=received_at,
    )


def test_size_aware_leg_walk_reports_vwap_worst_price_and_impact() -> None:
    tracker = DepthTracker(clock=lambda: 100.0)
    _seed_pair(tracker)

    result = tracker.executable_leg_capacity("BTCUSDT", "spot", "buy", 201.0)

    assert result.fully_executable
    assert result.executable_notional_usd == pytest.approx(201.0)
    assert result.available_notional_usd == pytest.approx(302.0)
    assert result.base_quantity == pytest.approx(2.0)
    assert result.average_price == pytest.approx(100.5)
    assert result.worst_price == pytest.approx(101.0)
    assert result.impact_bps == pytest.approx(50.0)
    assert result.rejection_reasons == ()


def test_pair_capacity_is_bottleneck_and_does_not_treat_partial_as_permission() -> None:
    tracker = DepthTracker(clock=lambda: 100.0)
    _seed_pair(tracker)

    result = tracker.executable_pair_capacity("BTCUSDT", 250.0)

    assert result.spot.side == "buy"
    assert result.perp.side == "sell"
    assert not result.fully_executable
    assert result.available_notional_usd == pytest.approx(200.2)
    assert result.executable_notional_usd == pytest.approx(200.2)
    assert result.spot.fully_executable
    assert not result.perp.fully_executable
    assert "perp:insufficient_displayed_depth" in result.rejection_reasons


@pytest.mark.parametrize(
    ("direction", "operation", "spot_side", "perp_side"),
    [
        ("long_spot_short_perp", "entry", "buy", "sell"),
        ("long_spot_short_perp", "exit", "sell", "buy"),
        ("short_spot_long_perp", "entry", "sell", "buy"),
        ("short_spot_long_perp", "exit", "buy", "sell"),
    ],
)
def test_pair_direction_maps_each_leg_explicitly(
    direction: str,
    operation: str,
    spot_side: str,
    perp_side: str,
) -> None:
    tracker = DepthTracker(clock=lambda: 100.0)
    _seed_pair(tracker)
    result = tracker.executable_pair_capacity(  # type: ignore[arg-type]
        "BTCUSDT",
        50.0,
        direction=direction,
        operation=operation,
    )
    assert result.spot.side == spot_side
    assert result.perp.side == perp_side
    assert result.fully_executable


def test_stale_book_fails_closed_even_though_displayed_levels_remain() -> None:
    tracker = DepthTracker(clock=lambda: 110.1)
    _seed_pair(tracker, received_at=100.0)

    result = tracker.executable_pair_capacity("BTCUSDT", 50.0, max_age_seconds=10.0)

    assert not result.fully_executable
    assert result.executable_notional_usd == 0.0
    assert "spot:stale_book" in result.rejection_reasons
    assert "perp:stale_book" in result.rejection_reasons


def test_clock_rollback_cannot_make_book_appear_fresh() -> None:
    tracker = DepthTracker(clock=lambda: 99.0)
    _seed_pair(tracker, received_at=100.0)
    result = tracker.executable_pair_capacity("BTCUSDT", 50.0)
    assert not result.fully_executable
    assert "spot:book_clock_invalid" in result.rejection_reasons
    assert "perp:book_clock_invalid" in result.rejection_reasons


@pytest.mark.parametrize(
    ("bids", "asks", "reason"),
    [
        ([(101.0, 1.0)], [(100.0, 1.0)], "crossed_book"),
        ([(99.0, 0.0)], [(100.0, 1.0)], "bid_zero_or_negative_level"),
        ([(99.0, 1.0)], [(100.0, 0.0)], "ask_zero_or_negative_level"),
        ([(98.0, 1.0), (99.0, 1.0)], [(100.0, 1.0)], "bids_unsorted"),
        ([(99.0, 1.0)], [(101.0, 1.0), (100.0, 1.0)], "asks_unsorted"),
    ],
)
def test_invalid_update_replaces_prior_good_executable_book(
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
    reason: str,
) -> None:
    tracker = DepthTracker(clock=lambda: 101.0)
    _seed_pair(tracker, received_at=100.0)
    tracker.on_l2depth("BTCUSDT", "spot", bids, asks, received_at=101.0)

    result = tracker.executable_leg_capacity("BTCUSDT", "spot", "buy", 10.0)

    assert not result.fully_executable
    assert reason in result.rejection_reasons
    assert tracker.spot_ask_depth("BTCUSDT") != pytest.approx(302.0)


def test_rest_aggregate_depth_is_never_claimed_as_executable_book() -> None:
    tracker = DepthTracker(clock=lambda: 100.0)
    tracker.set_rest_snapshot(
        "BTCUSDT",
        spot_depth_usd=1_000_000.0,
        perp_depth_usd=1_000_000.0,
        spot_bid_price=99.0,
        spot_ask_price=100.0,
        perp_bid_price=100.0,
        perp_ask_price=101.0,
    )
    assert tracker.get_entry_depth("BTCUSDT") == 1_000_000.0

    result = tracker.executable_pair_capacity("BTCUSDT", 100.0)

    assert not result.fully_executable
    assert result.executable_notional_usd == 0.0
    assert "spot:missing_book" in result.rejection_reasons


def test_capacity_invariants_hold_across_requested_sizes() -> None:
    tracker = DepthTracker(clock=lambda: 100.0)
    tracker.on_l2depth(
        "ETHUSDT",
        "spot",
        bids=[(99.0, 10.0)],
        asks=[(100.0, 1.0), (101.0, 2.0), (103.0, 3.0)],
        received_at=100.0,
    )
    prior_executed = -1.0
    prior_average = 0.0
    for requested in [0.01, 10.0, 100.0, 150.0, 302.0, 500.0, 1_000.0]:
        result = tracker.executable_leg_capacity("ETHUSDT", "spot", "buy", requested)
        assert 0.0 <= result.executable_notional_usd <= result.available_notional_usd
        assert result.executable_notional_usd <= requested + 1e-9
        assert result.executable_notional_usd >= prior_executed
        assert result.average_price >= prior_average
        assert result.worst_price + 1e-12 >= result.average_price
        assert result.impact_bps >= 0.0 and math.isfinite(result.impact_bps)
        if result.fully_executable:
            assert result.executable_notional_usd == pytest.approx(requested)
        else:
            assert "insufficient_displayed_depth" in result.rejection_reasons
        prior_executed = result.executable_notional_usd
        prior_average = result.average_price
