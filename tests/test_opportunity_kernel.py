from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

from bongus.strategies.opportunity_adapters import (
    LIVE_OPPORTUNITY_ADAPTER,
    PAPER_OPPORTUNITY_ADAPTER,
    REPLAY_OPPORTUNITY_ADAPTER,
    SHADOW_OPPORTUNITY_ADAPTER,
    apply_replay_settlement_cashflows,
)
from bongus.strategies.opportunity_kernel import (
    OPPORTUNITY_KERNEL_VERSION,
    OpportunityEvaluationInput,
    SettlementExpectation,
    evaluate_opportunity,
)


UTC = timezone.utc
NOW = datetime(2026, 7, 18, 7, 0, tzinfo=UTC)


def canonical_input(**overrides) -> OpportunityEvaluationInput:
    values = {
        "symbol": "BTCUSDT",
        "direction": "long_spot_short_perp",
        "decision_time": NOW,
        "horizon_end": NOW + timedelta(hours=9),
        "pair_gross_notional_usd": 20_000.0,
        "funding_liable_notional_usd": 10_000.0,
        "settlement_interval_hours": 4.0,
        "settlements": (
            SettlementExpectation(NOW + timedelta(hours=1), 0.001),
            SettlementExpectation(NOW + timedelta(hours=5), 0.0005, 0.8),
        ),
        "entry_execution_cost_pct": 0.0001,
        "exit_execution_cost_pct": 0.0002,
        "minimum_net_edge_bps": 1.0,
        "calendar_authoritative": True,
        "calendar_observed_at": NOW - timedelta(minutes=5),
        "funding_rate_observed_at": NOW - timedelta(seconds=30),
        "max_calendar_age_seconds": 3_600.0,
        "max_funding_rate_age_seconds": 120.0,
    }
    values.update(overrides)
    return OpportunityEvaluationInput(**values)


def test_golden_economics_are_identical_across_all_surface_adapters() -> None:
    inputs = canonical_input()
    evaluations = [
        adapter.evaluate(inputs)
        for adapter in (
            REPLAY_OPPORTUNITY_ADAPTER,
            SHADOW_OPPORTUNITY_ADAPTER,
            PAPER_OPPORTUNITY_ADAPTER,
            LIVE_OPPORTUNITY_ADAPTER,
        )
    ]

    assert evaluations[1:] == [evaluations[0]] * 3
    evaluation = evaluations[0]
    assert evaluation.kernel_version == OPPORTUNITY_KERNEL_VERSION
    assert evaluation.valid and evaluation.eligible
    assert evaluation.settlement_count == 2
    assert evaluation.gross_funding_usd == pytest.approx(14.0)
    assert evaluation.total_cost_usd == pytest.approx(3.0)
    assert evaluation.net_ev_usd == pytest.approx(11.0)
    assert evaluation.net_edge_bps == pytest.approx(11.0)
    assert evaluation.net_edge_pair_gross_bps == pytest.approx(5.5)


def test_discrete_payment_is_not_prorated_one_second_before_or_after() -> None:
    just_before = datetime(2026, 7, 18, 7, 59, 59, tzinfo=UTC)
    before = canonical_input(
        decision_time=just_before,
        horizon_end=just_before + timedelta(hours=4),
        settlements=(SettlementExpectation(datetime(2026, 7, 18, 8, 0, tzinfo=UTC), 0.001),),
        calendar_observed_at=just_before - timedelta(minutes=1),
        funding_rate_observed_at=just_before - timedelta(seconds=1),
        entry_execution_cost_pct=0.0,
        exit_execution_cost_pct=0.0,
        minimum_net_edge_bps=0.0,
    )
    just_after = datetime(2026, 7, 18, 8, 0, 1, tzinfo=UTC)
    after = replace(
        before,
        decision_time=just_after,
        horizon_end=just_after + timedelta(hours=4),
        settlements=(SettlementExpectation(datetime(2026, 7, 18, 12, 0, tzinfo=UTC), 0.001),),
        calendar_observed_at=just_after - timedelta(minutes=1),
        funding_rate_observed_at=just_after - timedelta(seconds=1),
    )

    before_result = evaluate_opportunity(before)
    after_result = evaluate_opportunity(after)

    assert before_result.gross_funding_usd == pytest.approx(10.0)
    assert after_result.gross_funding_usd == pytest.approx(10.0)
    assert before_result.gross_funding_edge_bps == pytest.approx(10.0)
    assert after_result.gross_funding_edge_bps == pytest.approx(10.0)


def test_missing_stale_future_or_non_authoritative_metadata_fails_closed() -> None:
    missing = evaluate_opportunity(
        canonical_input(
            calendar_authoritative=False,
            calendar_observed_at=None,
            funding_rate_observed_at=None,
        )
    )
    assert not missing.valid and not missing.eligible
    assert "missing_authoritative_settlement_metadata" in missing.reason_codes
    assert "missing_settlement_metadata_timestamp" in missing.reason_codes
    assert "missing_funding_rate_timestamp" in missing.reason_codes

    stale = evaluate_opportunity(
        canonical_input(calendar_observed_at=NOW - timedelta(hours=2))
    )
    assert not stale.valid and "stale_settlement_metadata" in stale.reason_codes

    future = evaluate_opportunity(
        canonical_input(funding_rate_observed_at=NOW + timedelta(seconds=1))
    )
    assert not future.valid and "future_funding_rate" in future.reason_codes


def test_direction_eligibility_and_full_costs_are_explicit() -> None:
    inverse = evaluate_opportunity(
        canonical_input(direction="short_spot_long_perp")
    )
    assert inverse.valid
    assert not inverse.eligible
    assert inverse.gross_funding_usd == pytest.approx(-14.0)
    assert "non_positive_directional_funding" in inverse.reason_codes

    missed = evaluate_opportunity(
        canonical_input(
            settlements=(
                SettlementExpectation(NOW + timedelta(hours=1), 0.01, 0.0),
            )
        )
    )
    assert missed.gross_funding_usd == 0.0
    assert not missed.eligible


def test_replay_adapter_accrues_only_exact_eligible_settlements() -> None:
    frame = pl.DataFrame(
        {
            "funding_eligible": [False, True, True, True],
            "funding_snapshot": [False, False, True, True],
            "funding_rate": [0.01, 0.01, 0.001, -0.002],
        }
    )

    result = apply_replay_settlement_cashflows(frame)

    assert result["_funding_accrual"].to_list() == [0.0, 0.0, 0.001, -0.002]
