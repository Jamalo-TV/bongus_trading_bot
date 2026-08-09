from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
from decimal import Decimal

from bongus.market_data.funding_calendar import FundingCalendar
from bongus.market_data.settlement_model import (
    FundingObservation,
    SettlementFundingModel,
    calibration_report,
)
from bongus.market_data.settlement_lifecycle import (
    FundingSettlementLifecycle,
    PositionEligibilityWindow,
    SettlementRateUpdate,
)
from bongus.portfolio.rotation_policy import (
    IncrementalRotationPolicy,
    RotationAction,
    RotationInputs,
)
from bongus.strategies.hold_exit_policy import (
    DirectionAwareHoldExitPolicy,
    HoldExitAction,
    HoldExitInputs,
)
from bongus.strategies.opportunity_scorer import (
    CandidateEconomics,
    LowerConfidenceNetEVScorer,
)


UTC = timezone.utc
NOW = datetime(2026, 7, 18, 7, 0, tzinfo=UTC)


def calendar() -> FundingCalendar:
    value = FundingCalendar()
    value.update_premium_index(
        {"symbol": "BTCUSDT", "nextFundingTime": int((NOW + timedelta(hours=1)).timestamp() * 1000)},
        observed_at=NOW,
    )
    return value


def model_with_history(*rates: float) -> SettlementFundingModel:
    model = SettlementFundingModel(uncertainty_floor_rate=1e-8)
    for index, rate in enumerate(rates):
        model.observe(
            FundingObservation(
                "BTCUSDT",
                NOW - timedelta(minutes=len(rates) - index),
                rate,
                source_event_id=str(index),
            )
        )
    return model


def test_settlement_forecast_is_discrete_causal_and_calendar_aware() -> None:
    model = model_with_history(0.10, 0.12, 0.14)
    # A future observation must not affect this decision.
    model.observe(FundingObservation("BTCUSDT", NOW + timedelta(minutes=1), -2.0, source_event_id="future"))
    forecast = model.forecast(
        symbol="BTCUSDT",
        decision_time=NOW,
        horizon_hours=9,
        notional_usd=10_000,
        direction="long_spot_short_perp",
        calendar=calendar(),
    )
    assert forecast.valid
    assert forecast.sample_count == 3
    assert len(forecast.payments) == 2
    assert all(payment.settlement_time > NOW for payment in forecast.payments)
    assert forecast.expected_payment_usd > 0
    assert forecast.latest_input_time is not None and forecast.latest_input_time <= NOW


def test_settlement_direction_caps_and_missing_inputs_fail_closed() -> None:
    cal = calendar()
    cal.update_funding_info(
        [{"symbol": "BTCUSDT", "adjustedFundingRateCap": "0.0001", "adjustedFundingRateFloor": "-0.0001"}],
        observed_at=NOW,
    )
    model = model_with_history(1.0, 1.2, 1.4)
    inverse = model.forecast(
        symbol="BTCUSDT",
        decision_time=NOW,
        horizon_hours=2,
        notional_usd=1_000,
        direction="short_spot_long_perp",
        calendar=cal,
    )
    assert inverse.payments[0].mean_rate <= 0.0001
    assert inverse.expected_payment_usd < 0
    missing = SettlementFundingModel().forecast(
        symbol="BTCUSDT",
        decision_time=NOW,
        horizon_hours=2,
        notional_usd=1_000,
        direction="long_spot_short_perp",
        calendar=cal,
    )
    assert not missing.valid
    assert "missing_point_in_time_history" in missing.reason_codes


def forecast_for_score():
    return model_with_history(0.30, 0.31, 0.29, 0.30).forecast(
        symbol="BTCUSDT",
        decision_time=NOW,
        horizon_hours=2,
        notional_usd=10_000,
        direction="long_spot_short_perp",
        calendar=calendar(),
    )


def economics(**overrides):
    values = {
        "symbol": "BTCUSDT",
        "notional_usd": 10_000.0,
        "settlement_forecast": forecast_for_score(),
        "entry_cost_bps": 1.0,
        "exit_cost_bps": 1.0,
        "capacity_usd": 20_000.0,
        "model_confidence": 0.9,
    }
    values.update(overrides)
    return CandidateEconomics(**values)


def test_lcb_score_is_monotone_in_funding_cost_and_capacity() -> None:
    scorer = LowerConfidenceNetEVScorer()
    base = scorer.score(economics())
    costly = scorer.score(economics(entry_cost_bps=20.0))
    capacity_limited = scorer.score(economics(capacity_usd=2_000.0))
    assert base.lower_bound_net_ev_usd > costly.lower_bound_net_ev_usd
    assert capacity_limited.executable_notional_usd == 1_600.0
    assert "capacity_limited" in capacity_limited.reason_codes
    stale = scorer.score(economics(input_age_seconds=60.0, max_input_age_seconds=30.0))
    assert not stale.eligible and "stale_inputs" in stale.reason_codes


def test_rotation_uses_incremental_value_payback_hysteresis_and_partial_capacity() -> None:
    policy = IncrementalRotationPolicy()
    keep = policy.decide(
        RotationInputs(
            "BTCUSDT", "ETHUSDT", 5_000, 20, 25, 2, 2, 1, 5_000, 0.9, 2,
            minimum_hold_hours=8,
            candidate_net_earning_rate_usd_per_hour=2,
        )
    )
    assert keep.action is RotationAction.KEEP
    assert "minimum_hold_not_met" in keep.reason_codes

    partial = policy.decide(
        RotationInputs(
            "BTCUSDT", "ETHUSDT", 5_000, 5, 50, 2, 2, 1, 2_000, 0.9, 10,
            minimum_hold_hours=8,
            candidate_net_earning_rate_usd_per_hour=10,
        )
    )
    assert partial.action is RotationAction.PARTIAL_ROTATE
    assert 0 < partial.rotate_notional_usd <= 2_000

    near_settlement = policy.decide(
        RotationInputs(
            "BTCUSDT", "ETHUSDT", 5_000, 5, 50, 2, 2, 1, 5_000, 0.9, 10,
            candidate_net_earning_rate_usd_per_hour=10,
            seconds_to_current_settlement=60,
            current_settlement_lower_payment_usd=100,
        )
    )
    assert near_settlement.action is RotationAction.KEEP


def hold_inputs(**overrides):
    values = {
        "symbol": "BTCUSDT",
        "direction": "long_spot_short_perp",
        "notional_usd": 10_000.0,
        "expected_future_funding_usd": 20.0,
        "lower_future_funding_usd": 10.0,
        "current_basis_pct": 0.002,
        "expected_exit_basis_pct": 0.001,
        "exit_cost_usd": 2.0,
        "basis_tail_risk_usd": 1.0,
    }
    values.update(overrides)
    return HoldExitInputs(**values)


def test_hold_exit_is_direction_aware_and_entry_locks_never_block_exits() -> None:
    policy = DirectionAwareHoldExitPolicy()
    favourable = policy.decide(hold_inputs())
    assert favourable.action is HoldExitAction.HOLD
    reversed_funding = policy.decide(
        hold_inputs(
            lower_future_funding_usd=-20,
            forecast_favourable_probability=0.1,
            entry_blocked=True,
        )
    )
    assert reversed_funding.action is HoldExitAction.CONTROLLED_EXIT
    assert "entry_lock_ignored_for_exit" in reversed_funding.reason_codes
    emergency = policy.decide(hold_inputs(risk_urgency=1.0, entry_blocked=True))
    assert emergency.action is HoldExitAction.EMERGENCY_EXIT

    stale_economic_exit = policy.decide(
        hold_inputs(
            lower_future_funding_usd=-20,
            forecast_favourable_probability=0.1,
            data_fresh=False,
        )
    )
    assert stale_economic_exit.action is HoldExitAction.HOLD
    assert "stale_data_blocks_economic_exit" in stale_economic_exit.reason_codes
    stale_emergency = policy.decide(
        hold_inputs(data_fresh=False, risk_urgency=1.0)
    )
    assert stale_emergency.action is HoldExitAction.EMERGENCY_EXIT


def test_calibration_report_is_deterministic() -> None:
    payment = forecast_for_score().payments[0]
    report = calibration_report([(payment, payment.mean_rate), (payment, -payment.mean_rate)])
    assert report.sample_count == 2
    assert report.mean_absolute_error >= 0.0
    assert 0.0 <= report.sign_brier_score <= 1.0
    assert math.isfinite(report.interval_coverage)


def test_late_reversal_missed_eligibility_and_four_hour_settlement_lifecycle() -> None:
    cal = FundingCalendar()
    settlement = NOW + timedelta(hours=1)
    cal.update_funding_info(
        [{"symbol": "BTCUSDT", "fundingIntervalHours": 4}],
        observed_at=NOW,
    )
    cal.update_premium_index(
        {
            "symbol": "BTCUSDT",
            "nextFundingTime": int(settlement.timestamp() * 1_000),
        },
        observed_at=NOW,
    )
    assert cal.interval_hours("BTCUSDT") == 4
    assert cal.next_settlement(
        "BTCUSDT", after=settlement
    ) == settlement + timedelta(hours=4)

    lifecycle = FundingSettlementLifecycle()
    lifecycle.add_window(
        PositionEligibilityWindow(
            cycle_id="eligible-cycle",
            symbol="BTCUSDT",
            direction="long_spot_short_perp",
            funding_notional_usd="10000",
            opened_at=settlement - timedelta(minutes=10),
            closed_at=settlement,
        )
    )
    lifecycle.observe_rate(
        SettlementRateUpdate(
            symbol="BTCUSDT",
            settlement_time=settlement,
            available_at=settlement - timedelta(minutes=30),
            raw_rate="0.001",
            source_event_id="preview-positive",
        )
    )
    lifecycle.observe_rate(
        SettlementRateUpdate(
            symbol="BTCUSDT",
            settlement_time=settlement,
            available_at=settlement - timedelta(seconds=1),
            raw_rate="-0.002",
            source_event_id="late-negative",
        )
    )
    # A post-settlement message is future information and cannot change cash.
    lifecycle.observe_rate(
        SettlementRateUpdate(
            symbol="BTCUSDT",
            settlement_time=settlement,
            available_at=settlement + timedelta(seconds=1),
            raw_rate="0.5",
            source_event_id="too-late",
        )
    )

    missing_statement = lifecycle.evaluate(
        symbol="BTCUSDT", settlement_time=settlement
    )
    assert missing_statement.eligible
    assert missing_statement.applied_rate == Decimal("-0.002")
    assert missing_statement.expected_cash_usd == Decimal("-20.000")
    assert missing_statement.credited_cash_usd == Decimal("0")
    assert not missing_statement.reconciled
    assert "rate_reversed_before_settlement" in missing_statement.reason_codes
    assert "missing_exchange_funding_statement" in missing_statement.reason_codes

    reconciled = lifecycle.evaluate(
        symbol="BTCUSDT",
        settlement_time=settlement,
        exchange_amount_usd="-20",
        exchange_event_id="income-123",
    )
    assert reconciled.reconciled
    assert reconciled.credited_cash_usd == Decimal("-20")

    missed = FundingSettlementLifecycle()
    missed.add_window(
        PositionEligibilityWindow(
            cycle_id="filled-after-settlement",
            symbol="BTCUSDT",
            direction="long_spot_short_perp",
            funding_notional_usd="10000",
            opened_at=settlement + timedelta(seconds=1),
        )
    )
    missed.observe_rate(
        SettlementRateUpdate(
            symbol="BTCUSDT",
            settlement_time=settlement,
            available_at=settlement - timedelta(seconds=1),
            raw_rate="0.001",
            source_event_id="eligible-rate",
        )
    )
    missed_result = missed.evaluate(
        symbol="BTCUSDT", settlement_time=settlement
    )
    assert not missed_result.eligible
    assert missed_result.expected_cash_usd == Decimal("0")
    assert missed_result.credited_cash_usd == Decimal("0")
    assert missed_result.reconciled
    assert "ineligible_at_settlement" in missed_result.reason_codes
