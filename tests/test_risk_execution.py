"""Tests for risk engine and execution routing."""

from bongus.risk.engine import RiskEngine, RiskLimits, RiskState
from bongus.risk.execution import OrderIntent, VenueQuote, route_order


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
    assert "max drawdown breached" in decision.reasons


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
