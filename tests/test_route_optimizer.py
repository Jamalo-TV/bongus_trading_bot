import math

from bongus.engine.execution_simulator import SimulationScenario, simulate_route
from bongus.engine.route_optimizer import RouteInputs, RouteOptimizer, RoutePolicy


def inputs(**overrides):
    values = {
        "symbol": "BTCUSDT",
        "notional_usd": 2_500.0,
        "spot_spread_bps": 2.0,
        "perp_spread_bps": 2.0,
        "spot_depth_usd": 100_000.0,
        "perp_depth_usd": 100_000.0,
        "book_age_ms": 100,
        "filters_ready": True,
        "maker_fee_bps": 1.0,
        "taker_fee_bps": 5.0,
        "impact_bps": 2.0,
        "max_unhedged_notional_ms": 5_000_000.0,
    }
    values.update(overrides)
    return RouteInputs(**values)


def test_passive_route_wins_when_time_and_fill_probability_are_abundant():
    recommendation = RouteOptimizer().recommend(
        inputs(
            spot_maker_fill_probability=0.95,
            perp_maker_fill_probability=0.95,
            maker_timeout_ms=500,
            seconds_to_settlement=3_600,
        )
    )
    assert recommendation.selected is RoutePolicy.POST_ONLY_DUAL


def test_near_settlement_prefers_fast_route_and_accounts_for_missed_value():
    recommendation = RouteOptimizer().recommend(
        inputs(
            seconds_to_settlement=0.15,
            settlement_value_bps=40.0,
            expected_ack_latency_ms=50,
            maker_timeout_ms=1_000,
        )
    )
    assert recommendation.selected in {
        RoutePolicy.SIMULTANEOUS_IOC,
        RoutePolicy.SLICED_IOC,
    }
    post = next(
        item
        for item in recommendation.estimates
        if item.policy is RoutePolicy.POST_ONLY_DUAL
    )
    assert post.missed_settlement_bps == 40.0


def test_stale_book_unknown_filters_and_capacity_fail_closed():
    recommendation = RouteOptimizer().recommend(
        inputs(book_age_ms=5_000, max_book_age_ms=1_000, filters_ready=False)
    )
    assert recommendation.selected is RoutePolicy.NONE
    assert "stale_book" in recommendation.reason
    assert "filters_unavailable" in recommendation.reason

    no_capacity = RouteOptimizer().recommend(
        inputs(spot_depth_usd=100.0, perp_depth_usd=100.0, max_slices=2)
    )
    assert no_capacity.selected is RoutePolicy.NONE


def test_emergency_reduce_only_is_never_available_for_entry():
    entry = RouteOptimizer().recommend(inputs(emergency=True, is_exit=False))
    emergency = next(
        item
        for item in entry.estimates
        if item.policy is RoutePolicy.EMERGENCY_REDUCE_ONLY
    )
    assert not emergency.feasible
    assert "reduce_only_route_for_exit_only" in emergency.reasons

    exit_recommendation = RouteOptimizer().recommend(
        inputs(emergency=True, is_exit=True, urgency=1.0)
    )
    assert exit_recommendation.selected in {
        RoutePolicy.SIMULTANEOUS_IOC,
        RoutePolicy.SLICED_IOC,
        RoutePolicy.EMERGENCY_REDUCE_ONLY,
    }


def test_hard_hedge_budget_can_reject_slow_maker_routes():
    recommendation = RouteOptimizer().recommend(
        inputs(
            max_unhedged_notional_ms=100_000.0,
            spot_maker_fill_probability=0.5,
            perp_maker_fill_probability=0.5,
            maker_timeout_ms=2_000,
        )
    )
    post = next(
        item
        for item in recommendation.estimates
        if item.policy is RoutePolicy.POST_ONLY_DUAL
    )
    assert not post.feasible
    assert "hedge_risk_budget_exceeded" in post.reasons


def test_seeded_simulator_is_reproducible_and_reports_tail_risk():
    estimate = RouteOptimizer().recommend(inputs()).selected_estimate
    assert estimate is not None and math.isfinite(estimate.total_objective_bps)
    scenario = SimulationScenario(trials=200, seed=7, adverse_tail_probability=0.1)
    first = simulate_route(
        estimate,
        hedge_budget_notional_ms=5_000_000.0,
        scenario=scenario,
    )
    second = simulate_route(
        estimate,
        hedge_budget_notional_ms=5_000_000.0,
        scenario=scenario,
    )
    assert first == second
    assert first.p95_cost_bps >= first.mean_cost_bps

