"""Tests for strategy.py – entry/exit signals, position state, yield accrual."""

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import polars as pl

from strategy import run_strategy
from config import (
    ENTRY_PREMIUM_THRESHOLD,
    FUNDING_PERIODS_PER_YEAR,
    TAKER_FEE,
)


def _make_df(
    n: int,
    funding_rate: float,
    spot_price: float = 100.0,
    premium_pct: float = 0.002,
    start_hour: int = 0,
) -> pl.DataFrame:
    """Helper to build a small aligned DataFrame with controllable params."""
    timestamps = [
        datetime(2025, 1, 1, start_hour, m, tzinfo=timezone.utc)
        for m in range(n)
    ]
    perp_price = spot_price * (1 + premium_pct)
    return pl.DataFrame({
        "timestamp": timestamps,
        "spot_close": [spot_price] * n,
        "perp_close": [perp_price] * n,
        "funding_rate": [funding_rate] * n,
        # First row is a snapshot (minute 0)
        "funding_snapshot": [i == 0 for i in range(n)],
    })


def test_entry_signal_fires():
    """Entry should fire when funding is high and premium exists."""
    min_ann = TAKER_FEE * 3 * FUNDING_PERIODS_PER_YEAR
    # Funding rate that annualizes above the threshold
    rate = (min_ann + 0.10) / FUNDING_PERIODS_PER_YEAR
    df = _make_df(5, funding_rate=rate, premium_pct=ENTRY_PREMIUM_THRESHOLD + 0.001)
    result = run_strategy(df)
    assert result["in_position"].any(), "Expected at least one row in position"


def test_no_entry_when_funding_low():
    """If funding is below threshold, no trade should open."""
    from config import ENTRY_ANN_FUNDING_THRESHOLD
    rate = (ENTRY_ANN_FUNDING_THRESHOLD - 0.05) / FUNDING_PERIODS_PER_YEAR
    df = _make_df(5, funding_rate=rate, premium_pct=0.002)
    result = run_strategy(df)
    assert not result["in_position"].any(), "Should not enter on low funding"


def test_no_entry_when_no_premium():
    """If perp is not at a premium, no entry even with high funding."""
    min_ann = TAKER_FEE * 3 * FUNDING_PERIODS_PER_YEAR
    rate = (min_ann + 0.10) / FUNDING_PERIODS_PER_YEAR
    df = _make_df(5, funding_rate=rate, premium_pct=0.0)  # no premium
    result = run_strategy(df)
    assert not result["in_position"].any(), "Should not enter without premium"


def test_no_double_entry():
    """Once in position, a second entry signal should not create a new trade."""
    min_ann = TAKER_FEE * 3 * FUNDING_PERIODS_PER_YEAR
    rate = (min_ann + 0.10) / FUNDING_PERIODS_PER_YEAR
    df = _make_df(10, funding_rate=rate, premium_pct=ENTRY_PREMIUM_THRESHOLD + 0.001)
    result = run_strategy(df)

    trade_ids = result.filter(pl.col("trade_id") > 0)["trade_id"].unique()
    assert trade_ids.len() == 1, f"Expected 1 trade, got {trade_ids.len()}"


def test_exit_fires_on_discount():
    """Position should close when perp trades at a discount."""
    min_ann = TAKER_FEE * 3 * FUNDING_PERIODS_PER_YEAR
    high_rate = (min_ann + 0.10) / FUNDING_PERIODS_PER_YEAR
    n = 10
    timestamps = [
        datetime(2025, 1, 1, 0, m, tzinfo=timezone.utc) for m in range(n)
    ]
    spot = [100.0] * n
    # First 5 rows: premium → entry;  last 5 rows: discount → exit
    perp = [100.2] * 5 + [99.8] * 5
    funding = [high_rate] * n
    snapshot = [i == 0 for i in range(n)]

    df = pl.DataFrame({
        "timestamp": timestamps,
        "spot_close": spot,
        "perp_close": perp,
        "funding_rate": funding,
        "funding_snapshot": snapshot,
    })
    result = run_strategy(df)

    # Some rows should be in position, but not the last ones
    in_pos = result["in_position"].to_list()
    assert any(in_pos[:5]), "Should enter on premium rows"
    # After exit signal fires, remaining rows should not be in position
    # (exact row depends on logic, but position should end)
    assert not all(in_pos), "Should exit at some point when discount appears"


def test_yield_accrual_only_at_snapshots():
    """Funding should only accrue on snapshot rows."""
    min_ann = TAKER_FEE * 3 * FUNDING_PERIODS_PER_YEAR
    rate = (min_ann + 0.10) / FUNDING_PERIODS_PER_YEAR
    n = 5
    timestamps = [
        datetime(2025, 1, 1, 0, m, tzinfo=timezone.utc) for m in range(n)
    ]
    df = pl.DataFrame({
        "timestamp": timestamps,
        "spot_close": [100.0] * n,
        "perp_close": [100.3] * n,  # 0.3% premium
        "funding_rate": [rate] * n,
        # Only minute 0 is a snapshot
        "funding_snapshot": [True, False, False, False, False],
    })
    result = run_strategy(df)
    in_pos = result.filter(pl.col("in_position"))

    if in_pos.height > 0:
        # Cumulative yield should equal rate (from the single snapshot)
        max_yield = in_pos["cumulative_yield"].max()
        assert max_yield is not None
        assert abs(max_yield - rate) < 1e-10, (
            f"Expected yield ≈ {rate}, got {max_yield}"
        )
