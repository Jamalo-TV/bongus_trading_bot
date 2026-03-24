"""Tests for FundingPredictor — rolling window linear extrapolation of funding rates."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'bongus', 'market_data')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'bongus', 'core')))

from funding_predictor import FundingPredictor


def test_push_sample_and_has_data():
    """has_data returns False before any samples and True after push_sample."""
    predictor = FundingPredictor(window=10)
    assert not predictor.has_data("BTCUSDT")
    predictor.push_sample("BTCUSDT", 0.1)
    assert predictor.has_data("BTCUSDT")
    assert not predictor.has_data("ETHUSDT")


def test_predict_returns_zero_with_no_samples():
    """predict_rate_at_snapshot returns 0.0 when no samples exist for symbol."""
    predictor = FundingPredictor(window=10)
    result = predictor.predict_rate_at_snapshot("BTCUSDT", minutes_to_snapshot=30.0)
    assert result == 0.0


def test_predict_fallback_single_sample():
    """predict_rate_at_snapshot returns the only sample when fewer than 2 exist."""
    predictor = FundingPredictor(window=10)
    predictor.push_sample("BTCUSDT", 0.25)
    result = predictor.predict_rate_at_snapshot("BTCUSDT", minutes_to_snapshot=30.0)
    assert result == 0.25


def test_predict_flat_rate_no_trend():
    """When all samples are identical, prediction equals that rate regardless of time."""
    predictor = FundingPredictor(window=60)
    rate = 0.15
    # Push 60 identical samples (simulating 60s of flat funding)
    for _ in range(60):
        predictor.push_sample("ETHUSDT", rate)
    result = predictor.predict_rate_at_snapshot("ETHUSDT", minutes_to_snapshot=30.0)
    assert abs(result - rate) < 1e-12


def test_predict_declining_trend():
    """TWAP projection blends historical average with current rate.

    For a declining series (0.20 → 0.10) the running TWAP ≈ 0.15.
    With 30 min remaining out of a 480-min epoch, elapsed_fraction = 0.9375,
    so the projection is heavily weighted toward the historical average:
        projected ≈ 0.15 × 0.9375 + 0.10 × 0.0625 ≈ 0.147

    The result must lie between the current rate (0.10) and the TWAP (≈ 0.15),
    which correctly reflects that the exchange will settle near the full-epoch
    average — not the momentary trend.
    """
    predictor = FundingPredictor(window=60)
    # Simulate 60 samples declining linearly from 0.20 down to 0.10
    for i in range(60):
        sample = 0.20 - i * (0.10 / 59)
        predictor.push_sample("SOLUSDT", sample)

    twap_so_far = sum(0.20 - i * (0.10 / 59) for i in range(60)) / 60
    result = predictor.predict_rate_at_snapshot("SOLUSDT", minutes_to_snapshot=30.0)
    # Projection is a convex blend: latest ≤ result ≤ twap_so_far
    assert result >= 0.10 - 1e-9
    assert result <= twap_so_far + 1e-9
