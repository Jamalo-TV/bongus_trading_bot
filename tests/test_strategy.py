"""Causality and signal tests for the funding strategy kernel."""

from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

from bongus.core.config import (
    ENTRY_ANN_FUNDING_THRESHOLD,
    ENTRY_PREMIUM_THRESHOLD,
    FUNDING_PERIODS_PER_YEAR,
)
from bongus.strategies.strategy import run_strategy


def _make_df(
    n: int,
    funding_rate: float,
    spot_price: float = 100.0,
    premium_pct: float = 0.002,
    start_hour: int = 0,
) -> pl.DataFrame:
    timestamps = [
        datetime(2025, 1, 1, start_hour, minute, tzinfo=timezone.utc)
        for minute in range(n)
    ]
    perp_price = spot_price * (1 + premium_pct)
    return pl.DataFrame({
        "timestamp": timestamps,
        "spot_close": [spot_price] * n,
        "perp_close": [perp_price] * n,
        "funding_rate": [funding_rate] * n,
        "funding_snapshot": [index == 0 for index in range(n)],
    })


def _high_rate() -> float:
    return (ENTRY_ANN_FUNDING_THRESHOLD + 0.10) / FUNDING_PERIODS_PER_YEAR


def test_entry_signal_fires():
    result = run_strategy(
        _make_df(
            5,
            funding_rate=_high_rate(),
            premium_pct=ENTRY_PREMIUM_THRESHOLD + 0.001,
        )
    )
    assert result["in_position"].any()


def test_no_entry_when_funding_low():
    rate = (ENTRY_ANN_FUNDING_THRESHOLD - 0.05) / FUNDING_PERIODS_PER_YEAR
    result = run_strategy(_make_df(5, funding_rate=rate, premium_pct=0.002))
    assert not result["in_position"].any()


def test_no_entry_when_no_premium():
    result = run_strategy(_make_df(5, funding_rate=_high_rate(), premium_pct=0.0))
    assert not result["in_position"].any()


def test_no_double_entry():
    result = run_strategy(
        _make_df(
            10,
            funding_rate=_high_rate(),
            premium_pct=ENTRY_PREMIUM_THRESHOLD + 0.001,
        )
    )
    trade_ids = result.filter(pl.col("trade_id") > 0)["trade_id"].unique()
    assert trade_ids.len() == 1


def test_exit_fires_on_discount():
    start = datetime(2025, 1, 1, 1, 0, tzinfo=timezone.utc)
    df = pl.DataFrame({
        "timestamp": [start + timedelta(minutes=i) for i in range(10)],
        "spot_close": [100.0] * 10,
        "perp_close": [100.2] * 5 + [99.8] * 5,
        "funding_rate": [_high_rate()] * 10,
        "funding_snapshot": [False] * 10,
    })
    result = run_strategy(df)
    assert result["in_position"].head(5).any()
    assert result["exit_filled"].any()


def test_basis_deviation_stop_forces_exit():
    start = datetime(2025, 1, 1, 1, 0, tzinfo=timezone.utc)
    df = pl.DataFrame({
        "timestamp": [start + timedelta(minutes=i) for i in range(10)],
        "spot_close": [100.0] * 10,
        "perp_close": [100.2] * 5 + [100.8] * 5,
        "funding_rate": [_high_rate()] * 5 + [0.0] * 5,
        "funding_snapshot": [False] * 10,
    })
    result = run_strategy(df)
    assert result["basis_stop_triggered"].any()
    assert result["exit_filled"].sum() == 1
    assert result.filter(pl.col("trade_id") > 0)["trade_id"].n_unique() == 1


def test_yield_accrual_only_after_position_crosses_snapshot():
    timestamps = [
        datetime(2025, 1, 1, 23, 0, tzinfo=timezone.utc),
        datetime(2025, 1, 2, 0, 0, tzinfo=timezone.utc),
        datetime(2025, 1, 2, 8, 0, tzinfo=timezone.utc),
    ]
    df = pl.DataFrame({
        "timestamp": timestamps,
        "spot_close": [100.0] * 3,
        "perp_close": [100.3] * 3,
        "funding_rate": [_high_rate()] * 3,
        "funding_snapshot": [False, True, True],
    })
    result = run_strategy(df)

    expected = _high_rate()
    assert result["entry_filled"].to_list() == [False, True, False]
    assert result["funding_eligible"].to_list() == [False, False, True]
    assert result["cumulative_yield"].to_list()[1] == 0.0
    assert abs(result["cumulative_yield"].to_list()[2] - expected) < 1e-10


def test_signal_fills_at_next_eligible_price_not_same_bar():
    df = pl.DataFrame({
        "timestamp": [
            datetime(2025, 1, 1, 1, minute, tzinfo=timezone.utc)
            for minute in range(3)
        ],
        "spot_close": [100.0, 110.0, 111.0],
        "perp_close": [100.3, 110.4, 111.4],
        "funding_rate": [_high_rate()] * 3,
        "funding_snapshot": [False] * 3,
    })
    result = run_strategy(df)

    assert result["raw_entry"][0]
    assert not result["in_position"][0]
    assert result["entry_filled"][1]
    assert result["spot_entry_price"][1] == 110.0
    assert result["perp_entry_price"][1] == 110.4


def test_exit_signal_fills_on_following_quote():
    start = datetime(2025, 1, 1, 1, 0, tzinfo=timezone.utc)
    df = pl.DataFrame({
        "timestamp": [start + timedelta(minutes=i) for i in range(5)],
        "spot_close": [100.0, 101.0, 102.0, 103.0, 104.0],
        "perp_close": [100.3, 101.3, 102.3, 103.3, 104.3],
        "funding_rate": [_high_rate(), _high_rate(), 0.0, 0.0, 0.0],
        "funding_snapshot": [False] * 5,
    })
    result = run_strategy(df)

    assert result["raw_exit"][2]
    assert result["in_position"][2]
    assert not result["exit_filled"][2]
    assert result["exit_filled"][3]
    assert result.filter(pl.col("exit_filled"))["spot_close"].item() == 103.0


def test_minutes_to_snapshot_do_not_overflow_int8():
    df = pl.DataFrame({
        "timestamp": [
            datetime(2025, 1, 1, 2, 8, tzinfo=timezone.utc),
            datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc),
            datetime(2025, 1, 1, 16, 0, tzinfo=timezone.utc),
        ],
        "spot_close": [100.0] * 3,
        "perp_close": [100.3] * 3,
        "funding_rate": [_high_rate()] * 3,
        "funding_snapshot": [False] * 3,
    })
    result = run_strategy(df)
    assert result["minutes_to_next_snapshot"].to_list() == [352, 240, 480]


def test_favorable_basis_convergence_does_not_trigger_stop():
    start = datetime(2025, 1, 1, 1, 0, tzinfo=timezone.utc)
    df = pl.DataFrame({
        "timestamp": [start + timedelta(minutes=i) for i in range(8)],
        "spot_close": [100.0] * 8,
        "perp_close": [100.5, 100.5, 100.5] + [100.1] * 5,
        "funding_rate": [_high_rate()] * 8,
        "funding_snapshot": [False] * 8,
    })
    result = run_strategy(df)

    assert result["in_position"].any()
    assert not result["basis_stop_triggered"].any()
    assert not result["exit_filled"].any()


def test_persistent_entry_signal_does_not_reenter_after_basis_stop():
    start = datetime(2025, 1, 1, 1, 0, tzinfo=timezone.utc)
    df = pl.DataFrame({
        "timestamp": [start + timedelta(minutes=i) for i in range(10)],
        "spot_close": [100.0] * 10,
        "perp_close": [100.3, 100.3, 100.3] + [101.0] * 7,
        "funding_rate": [_high_rate()] * 10,
        "funding_snapshot": [False] * 10,
    })
    result = run_strategy(df)

    assert result["basis_stop_triggered"].any()
    assert result["exit_filled"].sum() == 1
    assert result.filter(pl.col("trade_id") > 0)["trade_id"].n_unique() == 1


def test_strategy_rejects_noncausal_timestamp_order():
    df = _make_df(3, funding_rate=_high_rate()).reverse()

    with pytest.raises(ValueError, match="sorted"):
        run_strategy(df)
