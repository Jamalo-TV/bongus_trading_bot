"""Smoke tests for walk-forward validator."""

from datetime import datetime, timedelta, timezone

import polars as pl

from bongus.research.walk_forward import AcceptanceGates, run_walk_forward_validation


def _sample_df(rows: int = 10000) -> pl.DataFrame:
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    timestamps = [start + timedelta(minutes=i) for i in range(rows)]
    spot = [100 + i * 0.0001 for i in range(rows)]
    perp = [s * 1.001 for s in spot]
    funding = [0.0004 for _ in range(rows)]
    snapshots = [t.minute == 0 and t.hour in {0, 8, 16} for t in timestamps]

    return pl.DataFrame(
        {
            "timestamp": timestamps,
            "spot_close": spot,
            "perp_close": perp,
            "funding_rate": funding,
            "funding_snapshot": snapshots,
        }
    )


def test_walk_forward_returns_summary():
    df = _sample_df()
    summary = run_walk_forward_validation(
        df,
        gates=AcceptanceGates(
            min_avg_oos_edge=-1.0,
            min_windows_passing=1,
            min_trades_per_window=1,
            min_signal_to_noise=-10.0,
        ),
        train_rows=4000,
        test_rows=1000,
        step_rows=1000,
    )

    assert "windows" in summary
    assert "windows_passing" in summary
    assert "accepted" in summary
    assert summary["windows"] > 0
