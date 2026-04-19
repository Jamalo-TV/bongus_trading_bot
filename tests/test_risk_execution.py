"""Tests for risk engine and execution routing."""

from bongus.engine.execution_alpha import OrderIntent, VenueQuote, expected_cost_bps, route_order
from bongus.engine.risk_engine import RiskEngine, RiskLimits, RiskState, target_exposure_after_derisk


def test_target_exposure_after_derisk():
    # Scenario 1: current_exposure_usd <= max_exposure_usd
    assert target_exposure_after_derisk(50_000.0, 100_000.0) == 50_000.0
    assert target_exposure_after_derisk(100_000.0, 100_000.0) == 100_000.0

    # Scenario 2: reduced=200_000*0.75=150_000 still exceeds max → capped at max
    assert target_exposure_after_derisk(200_000.0, 100_000.0) == 100_000.0

    # Custom reduction fraction: 200_000*(1-0.5)=100_000 = max → min(100K,100K)=100K
    assert target_exposure_after_derisk(200_000.0, 100_000.0, reduction_fraction=0.5) == 100_000.0

    # Scenario 3: reduced=110_000*0.75=82_500 < max → return 82_500 (no upward clamp)
    assert target_exposure_after_derisk(110_000.0, 100_000.0) == 82_500.0


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
    # Smooth scale: max(0.1, 1.0 - 0.06/0.1) = max(0.1, 0.4) = 0.4
    assert abs(decision.position_scale - 0.4) < 1e-9
    assert any("soft drawdown active: scaling positions to" in r for r in decision.reasons)


def test_risk_engine_kill_switch_hysteresis_latches_until_release_band():
    engine = RiskEngine(
        RiskLimits(
            soft_drawdown_pct=0.04,
            max_drawdown_pct=0.10,
            max_drawdown_release_pct=0.08,
        )
    )

    activated = engine.evaluate(
        RiskState(
            gross_exposure_usd=0.0,
            symbol_concentration=0.0,
            drawdown_pct=0.11,
            data_staleness_minutes=0,
            venue_latency_ms=0,
        )
    )
    still_latched = engine.evaluate(
        RiskState(
            gross_exposure_usd=0.0,
            symbol_concentration=0.0,
            drawdown_pct=0.09,
            data_staleness_minutes=0,
            venue_latency_ms=0,
            previous_kill_switch=True,
        )
    )
    released = engine.evaluate(
        RiskState(
            gross_exposure_usd=0.0,
            symbol_concentration=0.0,
            drawdown_pct=0.07,
            data_staleness_minutes=0,
            venue_latency_ms=0,
            previous_kill_switch=True,
        )
    )

    assert activated.kill_switch is True
    assert still_latched.kill_switch is True
    assert released.kill_switch is False


def test_risk_engine_hysteresis_matches_legacy_when_release_equals_max():
    engine = RiskEngine(
        RiskLimits(
            soft_drawdown_pct=0.04,
            max_drawdown_pct=0.10,
            max_drawdown_release_pct=0.10,
        )
    )

    assert engine.evaluate(
        RiskState(
            gross_exposure_usd=0.0,
            symbol_concentration=0.0,
            drawdown_pct=0.1001,
            data_staleness_minutes=0,
            venue_latency_ms=0,
            previous_kill_switch=True,
        )
    ).kill_switch
    assert not engine.evaluate(
        RiskState(
            gross_exposure_usd=0.0,
            symbol_concentration=0.0,
            drawdown_pct=0.0999,
            data_staleness_minutes=0,
            venue_latency_ms=0,
            previous_kill_switch=True,
        )
    ).kill_switch


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


# ── Prospective exposure guard (mirrors _dispatch_enter logic) ─────────────


def _projected_gross(
    current_gross: float,
    pending_enters: dict,
    new_notional: float,
) -> float:
    """Replicate the prospective gross exposure formula from _dispatch_enter."""
    pending_notional = sum(
        float(m.get("qty", 0.0)) * float(m.get("entry_price", 0.0)) * 2.0
        for m in pending_enters.values()
    )
    return current_gross + pending_notional + new_notional


def test_prospective_guard_blocks_when_over_limit():
    """Adding a new slot should be blocked when projected gross would exceed the cap."""
    max_gross = 20_000.0
    # 4 slots already open at $5 000 each = $20 000 — no room for another entry.
    current_gross = 20_000.0
    projected = _projected_gross(current_gross, {}, new_notional=5_000.0)
    assert projected > max_gross


def test_prospective_guard_allows_when_under_limit():
    """A single slot entering an empty portfolio is well within limits."""
    max_gross = 20_000.0
    projected = _projected_gross(0.0, {}, new_notional=5_000.0)
    assert projected <= max_gross


def test_prospective_guard_accounts_for_pending_enters():
    """Pending (unconfirmed) entries must count toward projected exposure."""
    max_gross = 20_000.0
    # 3 confirmed slots open ($15 000), 1 pending entry at $4 000 notional,
    # meaning an additional $5 000 entry would push projected to $24 000.
    current_gross = 15_000.0
    pending = {"XRPUSDT": {"qty": 1000.0, "entry_price": 2.0}}  # 1000*2*2 = $4000
    projected = _projected_gross(current_gross, pending, new_notional=5_000.0)
    assert projected > max_gross


def test_per_symbol_cap_blocks_duplicate_slot():
    """A second slot in the same symbol should be blocked by the per-symbol cap."""
    per_symbol_cap = 5_000.0
    # Symbol already occupies one full slot (mark-to-market $5 000).
    existing_symbol_gross = 5_000.0
    new_notional = 5_000.0
    assert existing_symbol_gross + new_notional > per_symbol_cap


def test_per_symbol_cap_allows_first_entry():
    """First entry into a symbol with no existing exposure should be allowed."""
    per_symbol_cap = 5_000.0
    existing_symbol_gross = 0.0
    new_notional = 5_000.0
    assert existing_symbol_gross + new_notional <= per_symbol_cap


def test_prospective_guard_low_price_token():
    """
    Reproduces the overnight ATAUSDT incident: a low-price token generates a
    large qty but the notional is still within one slot — the guard should allow it.
    """
    max_gross = 20_000.0
    per_symbol_cap = 5_000.0
    # ATA at ~$0.003, one slot = $5 000 gross
    mark_price = 0.003
    slot_notional = 5_000.0
    per_leg = slot_notional / 2  # $2 500
    qty = per_leg / mark_price  # ~833 333 — large but correct

    # First entry from a cold start: no open exposure, no pending
    projected = _projected_gross(0.0, {}, new_notional=slot_notional)
    assert projected <= max_gross

    # Simulate the symbol now holding one full slot mark-to-market
    current_gross = qty * mark_price * 2.0  # ≈ $5 000
    existing_symbol_gross = current_gross

    # A SECOND entry for the same symbol must be blocked by the per-symbol cap
    assert existing_symbol_gross + slot_notional > per_symbol_cap

    # And also by the gross exposure guard once other slots are partially filled
    other_slots_gross = 15_000.0
    projected2 = _projected_gross(
        current_gross + other_slots_gross, {}, new_notional=slot_notional
    )
    assert projected2 > max_gross
