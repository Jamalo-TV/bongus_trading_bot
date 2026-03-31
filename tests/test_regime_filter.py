"""Tests for the basis-aware regime filter."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from bongus.market_data.depth_tracker import DepthTracker
from bongus.portfolio.regime_filter import RegimeFilter


def _feed_sample(
    tracker: DepthTracker,
    regime_filter: RegimeFilter,
    *,
    symbol: str,
    spot_mid: float,
    perp_mid: float,
    qty: float = 100.0,
    ann_funding: float | None = 0.1,
    minute_volume: float | None = None,
) -> None:
    tracker.on_l2depth(
        symbol,
        "spot",
        [(spot_mid - 0.1, qty)] * 5,
        [(spot_mid + 0.1, qty)] * 5,
    )
    tracker.on_l2depth(
        symbol,
        "perp",
        [(perp_mid - 0.1, qty)] * 5,
        [(perp_mid + 0.1, qty)] * 5,
    )
    regime_filter.on_depth_update(symbol)
    regime_filter.on_mark_price(symbol, perp_mid, ann_funding)
    if minute_volume is not None:
        regime_filter.on_volume_bar(symbol, minute_volume)


def test_regime_filter_allows_when_samples_are_insufficient():
    tracker = DepthTracker()
    regime_filter = RegimeFilter(tracker)

    _feed_sample(tracker, regime_filter, symbol="BTCUSDT", spot_mid=100.0, perp_mid=100.05)
    decision = regime_filter.evaluate("BTCUSDT")

    assert decision.allow_entry is True


def test_regime_filter_blocks_basis_outlier():
    tracker = DepthTracker()
    regime_filter = RegimeFilter(tracker)

    for _ in range(20):
        _feed_sample(tracker, regime_filter, symbol="BTCUSDT", spot_mid=100.0, perp_mid=100.05)

    _feed_sample(tracker, regime_filter, symbol="BTCUSDT", spot_mid=100.0, perp_mid=100.40)
    decision = regime_filter.evaluate("BTCUSDT")

    assert decision.allow_entry is False
    assert any("basis z-score" in reason for reason in decision.reasons)


def test_regime_filter_blocks_price_shock():
    tracker = DepthTracker()
    regime_filter = RegimeFilter(tracker)

    for _ in range(20):
        _feed_sample(tracker, regime_filter, symbol="ETHUSDT", spot_mid=100.0, perp_mid=100.05)

    _feed_sample(tracker, regime_filter, symbol="ETHUSDT", spot_mid=100.0, perp_mid=102.0)
    decision = regime_filter.evaluate("ETHUSDT")

    assert decision.allow_entry is False
    assert any("price shock" in reason for reason in decision.reasons)


def test_regime_filter_blocks_depth_collapse():
    tracker = DepthTracker()
    regime_filter = RegimeFilter(tracker)

    for _ in range(20):
        _feed_sample(tracker, regime_filter, symbol="SOLUSDT", spot_mid=100.0, perp_mid=100.05, qty=100.0)

    _feed_sample(tracker, regime_filter, symbol="SOLUSDT", spot_mid=100.0, perp_mid=100.05, qty=20.0)
    decision = regime_filter.evaluate("SOLUSDT")

    assert decision.allow_entry is False
    assert any("depth ratio" in reason for reason in decision.reasons)


def test_regime_filter_blocks_funding_dispersion():
    tracker = DepthTracker()
    regime_filter = RegimeFilter(tracker)

    for _ in range(20):
        _feed_sample(
            tracker,
            regime_filter,
            symbol="BTCUSDT",
            spot_mid=100.0,
            perp_mid=100.05,
            ann_funding=0.10,
        )

    _feed_sample(
        tracker,
        regime_filter,
        symbol="BTCUSDT",
        spot_mid=100.0,
        perp_mid=100.05,
        ann_funding=0.50,
    )
    decision = regime_filter.evaluate("BTCUSDT")

    assert decision.allow_entry is False
    assert any("funding dispersion" in reason for reason in decision.reasons)


def test_regime_filter_blocks_volume_spike():
    tracker = DepthTracker()
    regime_filter = RegimeFilter(tracker)

    for _ in range(20):
        _feed_sample(
            tracker,
            regime_filter,
            symbol="DOGEUSDT",
            spot_mid=100.0,
            perp_mid=100.05,
            minute_volume=1_000.0,
        )

    _feed_sample(
        tracker,
        regime_filter,
        symbol="DOGEUSDT",
        spot_mid=100.0,
        perp_mid=100.05,
        minute_volume=7_500.0,
    )
    decision = regime_filter.evaluate("DOGEUSDT")

    assert decision.allow_entry is False
    assert any("volume spike" in reason for reason in decision.reasons)
