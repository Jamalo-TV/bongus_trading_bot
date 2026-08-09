from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from bongus.core.config import FUNDING_PERIODS_PER_YEAR
from bongus.domain.units import (
    FUNDING_REPORTING_PERIODS_PER_YEAR,
    AnnualizedReportingRate,
    EconomicUnitSnapshot,
    RawSettlementRate,
    annualized_reporting_rate,
)
from bongus.engine.cost_model import paired_exact_book_cost_breakdown
from bongus.market_data.funding_calendar import FundingCalendar
from bongus.market_data.depth_tracker import DepthTracker
from bongus.market_data.settlement_model import (
    FundingObservation,
    SettlementForecast,
    SettlementFundingModel,
    SettlementPaymentForecast,
)
from bongus.strategies.decision_engine import (
    DecisionEngine,
    DecisionEngineConfig,
    DecisionRequest,
    canonical_config_hash,
)


UTC = timezone.utc


def _forecast(
    decision_time: datetime,
    *,
    direction: str = "long_spot_short_perp",
    lower_rate: float = 0.003,
    mean_rate: float = 0.004,
    count: int = 2,
) -> SettlementForecast:
    payments = tuple(
        SettlementPaymentForecast(
            symbol="BTCUSDT",
            settlement_time=decision_time + timedelta(hours=8 * (index + 1)),
            mean_rate=mean_rate,
            standard_deviation=0.0005,
            lower_rate=lower_rate,
            upper_rate=mean_rate + 0.001,
            favourable_sign_probability=0.99,
            expected_payment_usd=mean_rate * 2_500.0,
            lower_payment_usd=lower_rate * 2_500.0,
        )
        for index in range(count)
    )
    return SettlementForecast(
        symbol="BTCUSDT",
        decision_time=decision_time,
        direction=direction,  # type: ignore[arg-type]
        interval_hours=8,
        sample_count=100,
        latest_input_time=decision_time,
        payments=payments,
        valid=True,
    )


def _tracker(*, received_at: float = 100.0, thin: bool = False) -> DepthTracker:
    tracker = DepthTracker(clock=lambda: received_at)
    spot_ask_quantity = 5.0 if thin else 100.0
    perp_bid_quantity = 5.0 if thin else 100.0
    tracker.on_l2depth(
        "BTCUSDT",
        "spot",
        bids=[(99.9, 100.0), (99.0, 100.0)],
        asks=[(100.0, spot_ask_quantity), (101.0, 100.0)],
        received_at=received_at,
    )
    tracker.on_l2depth(
        "BTCUSDT",
        "perp",
        bids=[(100.1, perp_bid_quantity), (99.1, 100.0)],
        asks=[(100.2, 100.0), (101.2, 100.0)],
        received_at=received_at,
    )
    return tracker


def _request(
    decision_time: datetime,
    **overrides: object,
) -> DecisionRequest:
    values: dict[str, object] = {
        "symbol": "BTCUSDT",
        "decision_time": decision_time,
        "direction": "long_spot_short_perp",
        "requested_leg_notional_usd": 2_500.0,
        "settlement_forecast": _forecast(decision_time),
        "surface": "shadow",
        "forecast_confidence": 0.95,
        "calendar_authoritative": True,
        "calendar_observed_at": decision_time,
        "spot_filters_valid": True,
        "perp_filters_valid": True,
        "filters_observed_at": decision_time,
        "rate_limit_budget": 20,
        "current_open_slots": 0,
        "current_portfolio_pair_gross_usd": 0.0,
        "current_symbol_pair_gross_usd": 0.0,
        "collateral_available_usd": 2_500.0,
        "margin_available_usd": 1_250.0,
    }
    values.update(overrides)
    return DecisionRequest(**values)  # type: ignore[arg-type]


def test_raw_rate_reporting_unit_is_always_times_1095() -> None:
    assert FUNDING_PERIODS_PER_YEAR == FUNDING_REPORTING_PERIODS_PER_YEAR == 1095
    raw = RawSettlementRate(0.0001)
    assert raw.reporting_annualized.value == pytest.approx(0.1095)
    assert annualized_reporting_rate(raw) == pytest.approx(
        raw.value * FUNDING_REPORTING_PERIODS_PER_YEAR
    )
    assert AnnualizedReportingRate.from_raw(raw).raw_settlement == raw

    units = EconomicUnitSnapshot.matched(
        leg_notional_usd=2_500.0,
        collateral_usd=1_250.0,
        margin_exposure_usd=2_500.0,
    )
    assert units.pair_gross.value == 5_000.0
    assert raw.cashflow_usd(units.perp_leg) == pytest.approx(0.25)


def test_forecast_reporting_conversion_does_not_change_with_calendar_interval() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    model = SettlementFundingModel(uncertainty_floor_rate=1e-9)
    for minutes in (3, 2, 1):
        model.observe(
            FundingObservation(
                symbol="BTCUSDT",
                available_at=now - timedelta(minutes=minutes),
                annualized_rate=0.1095,
            )
        )

    forecasts = []
    for interval_hours in (4, 8):
        calendar = FundingCalendar()
        calendar.update_funding_info(
            [{"symbol": "BTCUSDT", "fundingIntervalHours": interval_hours}],
            observed_at=now,
        )
        calendar.update_premium_index(
            {
                "symbol": "BTCUSDT",
                "nextFundingTime": int((now + timedelta(hours=1)).timestamp() * 1_000),
            },
            observed_at=now,
        )
        forecasts.append(
            model.forecast(
                symbol="BTCUSDT",
                decision_time=now,
                horizon_hours=2,
                notional_usd=2_500.0,
                direction="long_spot_short_perp",
                calendar=calendar,
            )
        )
    assert forecasts[0].payments[0].mean_rate == pytest.approx(0.0001)
    assert forecasts[1].payments[0].mean_rate == pytest.approx(0.0001)


def test_settlement_model_deduplicates_loop_reuse_and_preserves_event_time() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    event_time = now - timedelta(seconds=30)
    observed = FundingObservation(
        symbol="BTCUSDT",
        event_time=event_time,
        available_at=event_time + timedelta(seconds=1),
        annualized_rate=0.1095,
        source_event_id="binance-mark-price:42",
    )
    model = SettlementFundingModel()
    assert model.observe(observed)
    assert not model.observe(observed)
    assert len(model.history_snapshot("BTCUSDT")) == 1
    stored = model.history_snapshot("BTCUSDT")[0]
    assert stored.event_time == event_time
    assert stored.available_at == event_time + timedelta(seconds=1)

    with pytest.raises(ValueError, match="source_event_id collision"):
        model.observe(replace(observed, annualized_rate=0.20))


def test_repeated_decision_cycles_cannot_freshen_old_funding_evidence() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    model = SettlementFundingModel()
    stale_time = now - timedelta(hours=2)
    observation = FundingObservation(
        symbol="BTCUSDT",
        event_time=stale_time,
        available_at=stale_time,
        annualized_rate=0.1095,
        source_event_id="stable-exchange-event",
    )
    for _ in range(100):
        model.observe(observation)
    calendar = FundingCalendar()
    calendar.update_premium_index(
        {
            "symbol": "BTCUSDT",
            "nextFundingTime": int((now + timedelta(hours=1)).timestamp() * 1_000),
        },
        observed_at=now,
    )
    forecast = model.forecast(
        symbol="BTCUSDT",
        decision_time=now,
        horizon_hours=2,
        notional_usd=2_500.0,
        direction="long_spot_short_perp",
        calendar=calendar,
    )
    assert forecast.sample_count == 1
    assert forecast.latest_input_time == stale_time


def test_delayed_funding_delivery_keeps_exchange_event_time_for_staleness() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    stale_event_time = now - timedelta(hours=2)
    model = SettlementFundingModel()
    model.observe(
        FundingObservation(
            symbol="BTCUSDT",
            event_time=stale_event_time,
            available_at=now,
            annualized_rate=0.1095,
            source_event_id="delayed-event",
        )
    )
    calendar = FundingCalendar()
    calendar.update_premium_index(
        {
            "symbol": "BTCUSDT",
            "nextFundingTime": int((now + timedelta(hours=1)).timestamp() * 1_000),
        },
        observed_at=now,
    )
    forecast = model.forecast(
        symbol="BTCUSDT",
        decision_time=now,
        horizon_hours=2,
        notional_usd=2_500.0,
        direction="long_spot_short_perp",
        calendar=calendar,
    )
    assert forecast.latest_input_time == stale_event_time


def test_decisions_reasons_and_config_hash_are_identical_across_modes() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    engine = DecisionEngine()
    tracker = _tracker()
    decisions = [
        engine.decide(
            replace(_request(now), surface=surface),
            depth_tracker=tracker,
            book_check_time=100.0,
        )
        for surface in ("replay", "shadow", "paper", "testnet", "live")
    ]
    assert all(decision == decisions[0] for decision in decisions)
    assert decisions[0].action == "enter"
    assert decisions[0].selected_settlement_count == 2
    assert decisions[0].horizons[0].lower_bound_net_ev_usd < 0.0
    assert decisions[0].horizons[1].lower_bound_net_ev_usd > 0.0
    assert canonical_config_hash(DecisionEngineConfig()) == decisions[0].config_hash
    assert canonical_config_hash(
        DecisionEngineConfig(minimum_lower_bound_edge_bps=1.0)
    ) != decisions[0].config_hash


def test_canonical_engine_owns_surface_independent_top_k_selection() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    engine = DecisionEngine(DecisionEngineConfig(max_slots=2))
    tracker = _tracker()
    base = engine.decide(
        _request(now), depth_tracker=tracker, book_check_time=100.0
    )
    assert base.selected_horizon is not None
    eth = replace(
        base,
        symbol="ETHUSDT",
        selected_horizon=replace(
            base.selected_horizon, lower_bound_net_ev_usd=20.0
        ),
    )
    btc = replace(
        base,
        selected_horizon=replace(
            base.selected_horizon, lower_bound_net_ev_usd=10.0
        ),
    )
    sol = replace(
        base,
        symbol="SOLUSDT",
        selected_horizon=replace(
            base.selected_horizon, lower_bound_net_ev_usd=5.0
        ),
    )

    first = engine.select_entries([btc, sol, eth])
    second = engine.select_entries([eth, btc, sol])
    assert [decision.symbol for decision in first.selected] == ["ETHUSDT", "BTCUSDT"]
    assert first == second
    assert first.rejected["SOLUSDT"] == ("portfolio_slot_competition",)


def test_canonical_selection_applies_safety_exclusions_without_legacy_scores() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    engine = DecisionEngine()
    decision = engine.decide(
        _request(now), depth_tracker=_tracker(), book_check_time=100.0
    )
    selection = engine.select_entries(
        [decision],
        excluded_reasons={"BTCUSDT": ("safety:storage_degraded",)},
    )
    assert selection.selected == ()
    assert selection.rejected["BTCUSDT"] == ("safety:storage_degraded",)


def test_canonical_selection_reserves_aggregate_slots_collateral_and_margin() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    engine = DecisionEngine(DecisionEngineConfig(max_slots=4))
    base = engine.decide(
        _request(now), depth_tracker=_tracker(), book_check_time=100.0
    )
    assert base.selected_horizon is not None
    eth = replace(
        base,
        symbol="ETHUSDT",
        selected_horizon=replace(
            base.selected_horizon,
            lower_bound_net_ev_usd=(
                base.selected_horizon.lower_bound_net_ev_usd + 1.0
            ),
        ),
    )

    selection = engine.select_entries(
        [base, eth],
        open_symbols=("SOLUSDT", "XRPUSDT", "BNBUSDT"),
        available_collateral_usd=2_500.0,
        available_margin_usd=1_250.0,
    )

    assert selection.occupied_slots == 3
    assert selection.available_slots == 1
    assert [decision.symbol for decision in selection.selected] == ["ETHUSDT"]
    assert selection.rejected["BTCUSDT"] == ("portfolio_slot_competition",)

    collateral_limited = engine.select_entries(
        [base, eth],
        available_collateral_usd=2_500.0,
        available_margin_usd=10_000.0,
    )
    assert [decision.symbol for decision in collateral_limited.selected] == ["ETHUSDT"]
    assert collateral_limited.rejected["BTCUSDT"] == (
        "portfolio_collateral_competition",
    )


def test_canonical_selection_fails_closed_on_duplicate_symbol_decisions() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    engine = DecisionEngine()
    decision = engine.decide(
        _request(now), depth_tracker=_tracker(), book_check_time=100.0
    )
    selection = engine.select_entries([decision, decision])
    assert selection.selected == ()
    assert selection.rejected["BTCUSDT"] == ("duplicate_symbol_decision",)


def test_canonical_selection_aggregates_new_pairs_against_portfolio_gross_cap() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    engine = DecisionEngine(
        DecisionEngineConfig(max_slots=4, max_portfolio_pair_gross_usd=10_000.0)
    )
    base = engine.decide(
        _request(now, current_portfolio_pair_gross_usd=5_000.0),
        depth_tracker=_tracker(),
        book_check_time=100.0,
    )
    eth = replace(base, symbol="ETHUSDT")

    selection = engine.select_entries(
        [base, eth],
        current_portfolio_pair_gross_usd=5_000.0,
    )

    assert [decision.symbol for decision in selection.selected] == ["BTCUSDT"]
    assert selection.rejected["ETHUSDT"] == (
        "portfolio_pair_gross_competition",
    )


def test_exact_book_cost_charges_each_leg_spread_and_walk_once() -> None:
    tracker = _tracker(thin=True)
    pair = tracker.executable_pair_capacity(
        "BTCUSDT", 1_000.0, now=100.0
    )
    breakdown = paired_exact_book_cost_breakdown(
        pair,
        spot_spread_bps=tracker.spot_spread_bps("BTCUSDT"),
        perp_spread_bps=tracker.perp_spread_bps("BTCUSDT"),
    )
    assert pair.spot.impact_bps > 0.0
    assert pair.perp.impact_bps > 0.0
    assert breakdown.spot_impact_pct == pytest.approx(
        pair.spot.impact_bps / 10_000.0
    )
    assert breakdown.perp_impact_pct == pytest.approx(
        pair.perp.impact_bps / 10_000.0
    )
    expected_spread = (
        tracker.spot_spread_bps("BTCUSDT")
        + tracker.perp_spread_bps("BTCUSDT")
    ) / 2.0 / 10_000.0
    assert breakdown.spot_spread_pct + breakdown.perp_spread_pct == pytest.approx(
        expected_spread
    )


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"calendar_authoritative": False}, "missing_authoritative_calendar"),
        ({"spot_filters_valid": False}, "spot_filters_unavailable"),
        ({"perp_filters_valid": False}, "perp_filters_unavailable"),
        ({"rate_limit_budget": 0}, "insufficient_rate_limit_budget"),
        ({"collateral_available_usd": 0.0}, "no_executable_account_capacity"),
        ({"current_open_slots": 4}, "slot_capacity_exhausted"),
        ({"forecast_confidence": 0.1}, "low_forecast_confidence"),
    ],
)
def test_candidate_gates_fail_closed(
    overrides: dict[str, object], reason: str
) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    decision = DecisionEngine().decide(
        _request(now, **overrides),
        depth_tracker=_tracker(),
        book_check_time=100.0,
    )
    assert not decision.eligible
    assert reason in decision.reason_codes


def test_missing_and_stale_paired_books_fail_closed() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    engine = DecisionEngine()
    missing = engine.decide(_request(now))
    assert "missing_entry_paired_books" in missing.reason_codes
    assert "missing_exit_paired_books" in missing.reason_codes

    stale = engine.decide(
        _request(now),
        depth_tracker=_tracker(received_at=100.0),
        book_check_time=106.0,
    )
    assert not stale.eligible
    assert "entry_book:spot:stale_book" in stale.reason_codes
    assert "exit_book:perp:stale_book" in stale.reason_codes


def test_reverse_short_spot_is_disabled_even_with_books_and_capacity() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    request = _request(
        now,
        direction="short_spot_long_perp",
        settlement_forecast=_forecast(
            now,
            direction="short_spot_long_perp",
            lower_rate=-0.005,
            mean_rate=-0.004,
        ),
    )
    decision = DecisionEngine().decide(
        request,
        depth_tracker=_tracker(),
        book_check_time=100.0,
    )
    assert not decision.eligible
    assert "reverse_short_spot_disabled" in decision.reason_codes


def test_no_positive_lower_bound_means_no_entry() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    request = _request(
        now,
        settlement_forecast=_forecast(now, lower_rate=0.001, mean_rate=0.0015),
    )
    decision = DecisionEngine().decide(
        request,
        depth_tracker=_tracker(),
        book_check_time=100.0,
    )
    assert not decision.eligible
    assert decision.selected_horizon is not None
    assert decision.selected_horizon.lower_bound_net_ev_usd < 0.0
    assert "non_positive_lower_bound_net_ev" in decision.reason_codes


def test_hazards_and_financing_reduce_each_discrete_horizon() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    engine = DecisionEngine()
    base_request = _request(now)
    base = engine.decide(
        base_request,
        depth_tracker=_tracker(),
        book_check_time=100.0,
    )
    stressed = engine.decide(
        replace(
            base_request,
            missed_settlement_probabilities=(0.25, 0.25),
            operational_hazard_probability=0.05,
            reversal_hazard_bps_per_settlement=2.0,
            operational_hazard_bps_per_settlement=2.0,
            liquidation_tail_bps_per_settlement=1.0,
            borrow_cost_bps_per_hour=0.1,
            collateral_cost_bps_per_hour=0.1,
            capital_opportunity_cost_bps_per_hour=0.1,
        ),
        depth_tracker=_tracker(),
        book_check_time=100.0,
    )
    assert base.selected_horizon is not None
    assert stressed.selected_horizon is not None
    assert (
        stressed.selected_horizon.lower_bound_net_ev_usd
        < base.selected_horizon.lower_bound_net_ev_usd
    )
    assert stressed.selected_horizon.borrow_cost_usd > 0.0
    assert stressed.selected_horizon.operational_hazard_usd > 0.0


def test_hard_caps_only_reduce_and_never_expand_request() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    config = DecisionEngineConfig(
        max_slots=99,
        max_leg_notional_usd=100_000.0,
        max_pair_gross_per_symbol_usd=100_000.0,
        max_portfolio_pair_gross_usd=100_000.0,
        max_leverage=20.0,
    )
    engine = DecisionEngine(config)
    reduced = engine.decide(
        _request(
            now,
            requested_leg_notional_usd=5_000.0,
            collateral_available_usd=10_000.0,
            margin_available_usd=10_000.0,
        ),
        depth_tracker=_tracker(),
        book_check_time=100.0,
    )
    assert reduced.approved_leg_notional_usd == 2_500.0
    assert reduced.economic_units is not None
    assert reduced.economic_units.pair_gross.value == 5_000.0
    assert "notional_reduced_by_cap" in reduced.reason_codes

    small = engine.decide(
        _request(now, requested_leg_notional_usd=1_000.0),
        depth_tracker=_tracker(),
        book_check_time=100.0,
    )
    assert small.approved_leg_notional_usd == 1_000.0

    full = engine.decide(
        _request(now, current_open_slots=4),
        depth_tracker=_tracker(),
        book_check_time=100.0,
    )
    assert not full.eligible
    assert "slot_capacity_exhausted" in full.reason_codes
