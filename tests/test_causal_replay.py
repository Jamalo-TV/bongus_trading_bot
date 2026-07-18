"""Golden event trace shared by strategy lifecycle and economic analytics."""

from datetime import datetime, timezone

import polars as pl

from bongus.core.config import (
    FUNDING_PERIODS_PER_YEAR,
    NOTIONAL_PER_TRADE,
)
from bongus.engine.analytics import compute_trade_summary
from bongus.engine.cost_model import blended_round_trip_cost_pct
from bongus.strategies.strategy import StrategyParameters, run_strategy


def test_golden_causal_trade_trace_and_pair_gross_ledger():
    rate = 0.22 / FUNDING_PERIODS_PER_YEAR
    timestamps = [
        datetime(2025, 1, 1, 7, 0, tzinfo=timezone.utc),
        datetime(2025, 1, 1, 7, 1, tzinfo=timezone.utc),
        datetime(2025, 1, 1, 8, 0, tzinfo=timezone.utc),
        datetime(2025, 1, 1, 8, 1, tzinfo=timezone.utc),
        datetime(2025, 1, 1, 9, 0, tzinfo=timezone.utc),
        datetime(2025, 1, 1, 9, 1, tzinfo=timezone.utc),
    ]
    market = pl.DataFrame({
        "timestamp": timestamps,
        "spot_close": [100.0, 100.0, 100.0, 100.2, 100.5, 101.0],
        "perp_close": [100.5, 100.5, 100.5, 100.4, 100.3, 100.0],
        "funding_rate": [rate, rate, rate, rate, 0.0, 0.0],
        "funding_snapshot": [False, False, True, False, False, False],
    })

    replay = run_strategy(
        market,
        parameters=StrategyParameters(entry_premium_threshold=0.001),
    )
    trades = compute_trade_summary(replay)

    assert replay["entry_filled"].to_list() == [False, True, False, False, False, False]
    assert replay["exit_filled"].to_list() == [False, False, False, False, False, True]
    assert replay["funding_eligible"].to_list() == [False, False, True, True, True, True]
    assert trades.height == 1

    trade = trades.row(0, named=True)
    one_leg_funding = rate
    expected_funding_on_pair_gross = one_leg_funding / 2.0
    expected_basis_on_pair_gross = 1.5 / 200.5
    expected_fees_on_pair_gross = blended_round_trip_cost_pct(
        size_usd=NOTIONAL_PER_TRADE / 2.0
    ) / 2.0
    expected_net = (
        expected_funding_on_pair_gross
        + expected_basis_on_pair_gross
        - expected_fees_on_pair_gross
    )

    assert trade["entry_time"] == timestamps[1]
    assert trade["exit_time"] == timestamps[5]
    assert abs(trade["funding_yield_perp_pct"] - one_leg_funding) < 1e-12
    assert abs(trade["gross_yield_pct"] - expected_funding_on_pair_gross) < 1e-12
    assert abs(trade["basis_pnl_pct"] - expected_basis_on_pair_gross) < 1e-12
    assert abs(trade["fees_pct"] - expected_fees_on_pair_gross) < 1e-12
    assert abs(trade["net_pnl_pct"] - expected_net) < 1e-12
    assert abs(trade["net_pnl_usd"] - expected_net * NOTIONAL_PER_TRADE) < 1e-9
