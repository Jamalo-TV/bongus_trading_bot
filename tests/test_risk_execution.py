"""Tests for risk engine and execution routing."""

from execution_alpha import OrderIntent, VenueQuote, route_order
from risk_engine import RiskEngine, RiskLimits, RiskState, target_exposure_after_derisk


def test_target_exposure_after_derisk():
    # Scenario 1: current_exposure_usd <= max_exposure_usd
    assert target_exposure_after_derisk(50_000.0, 100_000.0) == 50_000.0
    assert target_exposure_after_derisk(100_000.0, 100_000.0) == 100_000.0

    # Scenario 2: current_exposure_usd > max_exposure_usd and reduced exposure is > max_exposure_usd
    # Default reduction_fraction is 0.25. So 200_000.0 * 0.75 = 150_000.0
    assert target_exposure_after_derisk(200_000.0, 100_000.0) == 150_000.0

    # Custom reduction fraction: 200_000 * (1 - 0.5) = 100_000.0
    assert target_exposure_after_derisk(200_000.0, 100_000.0, reduction_fraction=0.5) == 100_000.0

    # Scenario 3: current_exposure_usd > max_exposure_usd and reduced exposure is < max_exposure_usd
    # 110_000.0 * 0.75 = 82_500.0, which is < 100_000.0. So it should return 100_000.0
    assert target_exposure_after_derisk(110_000.0, 100_000.0) == 100_000.0


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
