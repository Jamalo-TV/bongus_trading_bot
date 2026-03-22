"""Tests for risk engine and execution routing."""

from execution_alpha import OrderIntent, VenueQuote, route_order, expected_cost_bps
from risk_engine import RiskEngine, RiskLimits, RiskState


def test_route_order_returns_plan():
    quotes = [
        VenueQuote(
            venue="a",
            bid=100.0,
            ask=100.02,
            depth_usd=2_000_000,
            fee_bps=6.0,
            latency_ms=40,
            reliability=0.995,
        ),
        VenueQuote(
            venue="b",
            bid=99.99,
            ask=100.03,
            depth_usd=500_000,
            fee_bps=5.0,
            latency_ms=200,
            reliability=0.96,
        ),
    ]
    intent = OrderIntent(symbol="BTCUSDT", side="buy", quantity=20_000, urgency=0.6, max_slippage_bps=8)
    plan = route_order(intent, quotes)

    assert plan.venue in {"a", "b"}
    assert plan.expected_cost_bps > 0
    assert 0.0 <= plan.fill_probability <= 1.0


def test_risk_engine_kill_switch_on_drawdown():
    engine = RiskEngine(
        RiskLimits(
            max_gross_exposure_usd=100_000,
            max_symbol_concentration=0.6,
            soft_drawdown_pct=0.05,
            max_drawdown_pct=0.1,
            max_data_staleness_minutes=10,
            max_latency_ms=200,
        )
    )
    state = RiskState(
        gross_exposure_usd=90_000,
        symbol_concentration=0.5,
        drawdown_pct=0.2,
        data_staleness_minutes=2,
        venue_latency_ms=20,
    )

    decision = engine.evaluate(state)
    assert not decision.allow_new_risk
    assert decision.derisk_required
    assert decision.kill_switch
    assert any("max drawdown breached" in r for r in decision.reasons)


def test_risk_engine_soft_drawdown_downscaling():
    engine = RiskEngine(
        RiskLimits(
            max_gross_exposure_usd=100_000,
            max_symbol_concentration=0.6,
            soft_drawdown_pct=0.05,
            max_drawdown_pct=0.1,
            max_data_staleness_minutes=10,
            max_latency_ms=200,
        )
    )
    state = RiskState(
        gross_exposure_usd=50_000,
        symbol_concentration=0.5,
        drawdown_pct=0.06,  # Above soft limit, below hard limit
        data_staleness_minutes=2,
        venue_latency_ms=20,
    )

    decision = engine.evaluate(state)
    assert decision.allow_new_risk  # Still allowed to risk
    assert not decision.derisk_required  # Not strictly forced to derisk other than scaling
    assert not decision.kill_switch
    assert decision.position_scale == 0.5
    assert "soft drawdown active: halving leverage" in decision.reasons


def test_expected_cost_bps_market_no_slippage():
    # depth_usd > quantity => no slippage
    intent = OrderIntent(symbol="BTCUSDT", side="buy", quantity=100.0, urgency=0.5, max_slippage_bps=10.0)
    quote = VenueQuote(venue="a", bid=100.0, ask=100.04, depth_usd=200.0, fee_bps=5.0, latency_ms=10, reliability=1.0)

    # spread_bps = (100.04 - 100.0) / 100.02 * 10000 = 0.04 / 100.02 * 10000 = 3.999200159968006
    # slip_bps = 0.0 (since 200 / 100 = 2.0 -> min(1.0, 2.0) = 1.0 -> 1.0 - 1.0 = 0.0)
    # crossing_cost = spread / 2.0 + 0 = 1.999600079984003
    # urgency_penalty = 0.5 * 2.0 = 1.0
    # expected_cost = 5.0 + 1.999600079984003 + 1.0 = 7.999600079984003

    cost = expected_cost_bps(intent, quote, "market")
    assert abs(cost - 7.9996) < 1e-4

def test_expected_cost_bps_market_with_slippage():
    # depth_usd < quantity => slippage penalty applies
    intent = OrderIntent(symbol="BTCUSDT", side="buy", quantity=200.0, urgency=0.5, max_slippage_bps=10.0)
    quote = VenueQuote(venue="a", bid=100.0, ask=100.04, depth_usd=50.0, fee_bps=5.0, latency_ms=10, reliability=1.0)

    # slip_bps = (1.0 - min(1.0, 50.0 / 200.0)) * 8.0 = (1.0 - 0.25) * 8.0 = 0.75 * 8.0 = 6.0
    # spread_bps = 3.999200159968006
    # crossing_cost = 3.999200159968006 / 2.0 + 6.0 = 7.999600079984003
    # urgency_penalty = 0.5 * 2.0 = 1.0
    # expected_cost = 5.0 + 7.999600079984003 + 1.0 = 13.999600079984003

    cost = expected_cost_bps(intent, quote, "market")
    assert abs(cost - 13.9996) < 1e-4

def test_expected_cost_bps_limit_spread_greater_than_threshold():
    # limit order with spread * 0.15 > 0.1
    # spread_bps = 3.9992, spread * 0.15 = 0.59988 > 0.1
    intent = OrderIntent(symbol="BTCUSDT", side="buy", quantity=100.0, urgency=0.1, max_slippage_bps=10.0)
    quote = VenueQuote(venue="a", bid=100.0, ask=100.04, depth_usd=200.0, fee_bps=5.0, latency_ms=10, reliability=1.0)

    # crossing_cost = max(0.1, 3.999200159968006 * 0.15) = 0.599880023995201
    # urgency_penalty = 0.1 * 2.0 = 0.2
    # expected_cost = 5.0 + 0.599880023995201 + 0.2 = 5.799880023995201

    cost = expected_cost_bps(intent, quote, "limit")
    assert abs(cost - 5.79988) < 1e-4

def test_expected_cost_bps_limit_spread_less_than_threshold():
    # limit order with spread * 0.15 < 0.1
    # To get spread * 0.15 < 0.1 => spread < 0.666
    # bid=100.0, ask=100.005 => spread_bps = 0.005 / 100.0025 * 10000 = 0.49998
    intent = OrderIntent(symbol="BTCUSDT", side="buy", quantity=100.0, urgency=0.1, max_slippage_bps=10.0)
    quote = VenueQuote(venue="a", bid=100.0, ask=100.005, depth_usd=200.0, fee_bps=5.0, latency_ms=10, reliability=1.0)

    # spread_bps = 0.4999875...
    # spread * 0.15 = 0.074998 < 0.1 -> crossing_cost = 0.1
    # urgency_penalty = 0.1 * 2.0 = 0.2
    # expected_cost = 5.0 + 0.1 + 0.2 = 5.3

    cost = expected_cost_bps(intent, quote, "limit")
    assert abs(cost - 5.3) < 1e-4

def test_expected_cost_bps_urgency_penalty_scaling():
    intent1 = OrderIntent(symbol="BTCUSDT", side="buy", quantity=100.0, urgency=0.0, max_slippage_bps=10.0)
    intent2 = OrderIntent(symbol="BTCUSDT", side="buy", quantity=100.0, urgency=1.0, max_slippage_bps=10.0)
    quote = VenueQuote(venue="a", bid=100.0, ask=100.04, depth_usd=200.0, fee_bps=5.0, latency_ms=10, reliability=1.0)

    cost1 = expected_cost_bps(intent1, quote, "market")
    cost2 = expected_cost_bps(intent2, quote, "market")

    # difference should be exactly urgency_penalty(1.0) - urgency_penalty(0.0) = 2.0 - 0.0 = 2.0
    assert abs((cost2 - cost1) - 2.0) < 1e-8
