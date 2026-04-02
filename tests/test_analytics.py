"""Tests for analytics.py."""

from datetime import datetime, timezone

import polars as pl

from bongus.core.config import NOTIONAL_PER_TRADE
from bongus.engine.analytics import compute_portfolio_stats, compute_trade_summary


def _make_mock_trades() -> pl.DataFrame:
    """Creates a mock strategy DataFrame with annotated trades."""
    timestamps = [
        datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc),
        datetime(2025, 1, 1, 1, 0, tzinfo=timezone.utc), # 1 hour duration
        datetime(2025, 1, 2, 0, 0, tzinfo=timezone.utc),
        datetime(2025, 1, 2, 2, 0, tzinfo=timezone.utc), # 2 hour duration
        datetime(2025, 1, 3, 0, 0, tzinfo=timezone.utc), # Not in a trade
    ]

    return pl.DataFrame({
        "timestamp": timestamps,
        "trade_id": [1, 1, 2, 2, 0],
        "spot_entry_price": [100.0, 100.0, 200.0, 200.0, None],
        "perp_entry_price": [101.0, 101.0, 202.0, 202.0, None],
        "spot_close": [100.0, 105.0, 200.0, 190.0, 100.0],
        "perp_close": [101.0, 103.0, 202.0, 195.0, 100.0],
        "cumulative_yield": [0.0, 0.05, 0.0, 0.02, 0.0],
    })

def test_compute_trade_summary_empty():
    empty_df = _make_mock_trades().clear()
    summary = compute_trade_summary(empty_df)

    assert summary.is_empty()
    assert "trade_id" in summary.columns
    assert "entry_time" in summary.columns
    assert "duration_hours" in summary.columns
    assert "net_pnl_usd" in summary.columns

def test_compute_trade_summary_logic():
    df = _make_mock_trades()
    summary = compute_trade_summary(df)

    assert summary.height == 2

    # Trade 1
    t1 = summary.filter(pl.col("trade_id") == 1).row(0, named=True)
    assert t1["duration_hours"] == 1.0
    assert t1["spot_entry_price"] == 100.0
    assert t1["perp_entry_price"] == 101.0
    assert t1["spot_exit_price"] == 105.0
    assert t1["perp_exit_price"] == 103.0
    assert t1["gross_yield_pct"] == 0.05

    # Basis PnL calculation check:
    # Spot PnL = (105.0 - 100.0) / 100.0 = 0.05
    # Perp PnL = (101.0 - 103.0) / 101.0 = -0.01980198...
    # Basis PnL = 0.05 + (-0.01980198...) = 0.03019801...
    assert abs(t1["basis_pnl_pct"] - (0.05 - 2/101)) < 1e-6

    assert t1["net_pnl_pct"] == t1["gross_yield_pct"] + t1["basis_pnl_pct"] - t1["fees_pct"]
    assert t1["net_pnl_usd"] == t1["net_pnl_pct"] * NOTIONAL_PER_TRADE
    assert t1["annualized_return_pct"] == t1["net_pnl_pct"] / 1.0 * 8760.0

def test_compute_portfolio_stats_empty():
    empty_summary = compute_trade_summary(_make_mock_trades().clear())
    stats = compute_portfolio_stats(empty_summary)

    assert stats["total_trades"] == 0
    assert stats["win_rate"] == 0.0
    assert stats["total_net_pnl_usd"] == 0.0

def test_compute_portfolio_stats_logic():
    df = _make_mock_trades()
    summary = compute_trade_summary(df)
    stats = compute_portfolio_stats(summary)

    assert stats["total_trades"] == 2
    assert stats["avg_duration_hours"] == 1.5

    net_pnl_list = summary["net_pnl_pct"].to_list()
    expected_winners = sum(1 for pnl in net_pnl_list if pnl > 0)
    expected_losers = 2 - expected_winners

    assert stats["winners"] == expected_winners
    assert stats["losers"] == expected_losers
    assert stats["win_rate"] == expected_winners / 2.0

    assert abs(stats["total_net_pnl_usd"] - summary["net_pnl_usd"].sum()) < 1e-6
