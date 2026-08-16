"""Tests for cost_model.py"""

from decimal import Decimal

import pytest

from bongus.core.config import (
    MAKER_FEE_PERP,
    MAKER_FEE_SPOT,
    NOTIONAL_PER_TRADE,
    SLIPPAGE_ESTIMATE,
    TAKER_FEE_PERP,
    TAKER_FEE_SPOT,
)
from bongus.engine.cost_model import (
    PerLegSpreadBps,
    action_cost_pct,
    blended_round_trip_cost_pct,
    cost_per_leg_perp,
    cost_per_leg_spot,
    exact_adverse_markout_bps,
    liquidity_adjusted_slippage,
    paired_action_cost_breakdown,
    paired_round_trip_cost_breakdown,
    round_trip_cost,
    round_trip_cost_pct,
    unhedged_notional_milliseconds,
)


def test_exact_tca_helpers_do_not_round_decimal_evidence():
    integral = unhedged_notional_milliseconds(
        spot_gross_quantity="2.00000001",
        perp_gross_quantity="2",
        reference_price="50000.12345678",
        elapsed_milliseconds="1250.5",
    )
    assert integral == Decimal("0.62525154382703390")
    assert unhedged_notional_milliseconds(
        spot_quantity="2.00000001",
        perp_quantity="2",
        reference_price="50000.12345678",
        elapsed_milliseconds="1250.5",
    ) == integral
    assert exact_adverse_markout_bps(
        side="BUY",
        fill_price="100.1",
        future_mid="99.9",
    ) == Decimal("20000") / Decimal("1001")
    with pytest.raises(ValueError, match="side"):
        exact_adverse_markout_bps(
            side="UNKNOWN",
            fill_price="100",
            future_mid="101",
        )


def test_cost_per_leg_spot_taker():
    """Spot taker cost = spot taker fee + slippage."""
    # size_usd=depth_usd → impact_ratio=1.0 → slippage = SLIPPAGE_ESTIMATE * sqrt(1.0) = SLIPPAGE_ESTIMATE
    cost = cost_per_leg_spot(is_maker=False, size_usd=100_000.0, depth_usd=100_000.0)
    assert abs(cost - (TAKER_FEE_SPOT + SLIPPAGE_ESTIMATE)) < 1e-12


def test_cost_per_leg_perp_taker():
    """Perp taker cost = perp taker fee + slippage."""
    # size_usd=depth_usd → impact_ratio=1.0 → slippage = SLIPPAGE_ESTIMATE * sqrt(1.0) = SLIPPAGE_ESTIMATE
    cost = cost_per_leg_perp(is_maker=False, size_usd=100_000.0, depth_usd=100_000.0)
    assert abs(cost - (TAKER_FEE_PERP + SLIPPAGE_ESTIMATE)) < 1e-12


def test_cost_per_leg_maker_no_slippage():
    """Maker legs have no slippage."""
    spot_cost = cost_per_leg_spot(is_maker=True)
    perp_cost = cost_per_leg_perp(is_maker=True)
    assert abs(spot_cost - MAKER_FEE_SPOT) < 1e-12
    assert abs(perp_cost - MAKER_FEE_PERP) < 1e-12


def test_action_cost_is_spot_plus_perp():
    """Action cost = spot leg + perp leg (taker)."""
    size = NOTIONAL_PER_TRADE
    depth = 500_000.0
    expected = (
        cost_per_leg_spot(is_maker=False, size_usd=size, depth_usd=depth)
        + cost_per_leg_perp(is_maker=False, size_usd=size, depth_usd=depth)
    )
    assert abs(action_cost_pct(size_usd=size, depth_usd=depth) - expected) < 1e-12


def test_round_trip_is_two_actions():
    """Round trip = 2 actions."""
    size = NOTIONAL_PER_TRADE
    depth = 500_000.0
    expected = action_cost_pct(size_usd=size, depth_usd=depth) * 2
    assert abs(round_trip_cost_pct(size_usd=size, depth_usd=depth) - expected) < 1e-12


def test_blended_cheaper_than_taker():
    """Blended cost should be less than pure taker cost."""
    taker = round_trip_cost_pct()
    blended = blended_round_trip_cost_pct()
    assert blended < taker


def test_dollar_costs_scale_with_notional():
    """Dollar costs should scale with notional."""
    n1 = 10_000
    n2 = 20_000
    c1 = round_trip_cost(n1, depth_usd=1_000_000.0)
    c2 = round_trip_cost(n2, depth_usd=1_000_000.0)
    assert c2 > c1


def test_slippage_increases_with_size():
    """Slippage should increase with larger order size relative to depth."""
    small = liquidity_adjusted_slippage(1_000.0, 500_000.0)
    large = liquidity_adjusted_slippage(100_000.0, 500_000.0)
    assert large > small


def test_blended_exit_cost_returns_positive_dollar_amount():
    """blended_exit_cost must return a positive USD cost."""
    from cost_model import blended_exit_cost
    cost = blended_exit_cost(5_000.0, depth_usd=1_000_000.0)
    assert cost > 0.0, f"Expected positive cost, got {cost}"
    assert cost < 5_000.0 * 0.05, f"Cost {cost} seems too high for $5k notional"


def test_blended_exit_cost_is_less_than_round_trip():
    """blended_exit_cost (one action) must be less than round_trip_cost (two actions)."""
    from cost_model import blended_exit_cost, round_trip_cost
    notional = 5_000.0
    depth = 1_000_000.0
    exit_cost = blended_exit_cost(notional, depth_usd=depth)
    rt_cost = round_trip_cost(notional, depth_usd=depth)
    assert exit_cost < rt_cost, f"One-way exit {exit_cost} should be less than round-trip {rt_cost}"


def test_paired_breakdown_attributes_each_spread_once() -> None:
    breakdown = paired_action_cost_breakdown(
        size_usd=1_000.0,
        spot_depth_usd=1_000_000.0,
        perp_depth_usd=1_000_000.0,
        spot_spread_bps=2.0,
        perp_spread_bps=6.0,
        spot_maker_fill_probability=0.0,
        perp_maker_fill_probability=0.0,
    )
    assert breakdown.spot_spread_pct == pytest.approx(1.0 / 10_000.0)
    assert breakdown.perp_spread_pct == pytest.approx(3.0 / 10_000.0)
    assert breakdown.spot_spread_pct + breakdown.perp_spread_pct == pytest.approx(4.0 / 10_000.0)
    assert breakdown.total_pct == pytest.approx(
        breakdown.spot_fee_pct
        + breakdown.perp_fee_pct
        + breakdown.spot_spread_pct
        + breakdown.perp_spread_pct
        + breakdown.spot_impact_pct
        + breakdown.perp_impact_pct
    )


def test_paired_breakdown_size_and_depth_impact_is_leg_specific() -> None:
    breakdown = paired_action_cost_breakdown(
        size_usd=10_000.0,
        spot_depth_usd=10_000.0,
        perp_depth_usd=1_000_000.0,
        spot_maker_fill_probability=0.0,
        perp_maker_fill_probability=0.0,
    )
    assert breakdown.spot_impact_pct > breakdown.perp_impact_pct


def test_paired_round_trip_counts_four_friction_legs_exactly_once() -> None:
    spreads = PerLegSpreadBps.from_values(
        spot_bps=Decimal("2"),
        perp_bps=Decimal("6"),
    )

    breakdown = paired_round_trip_cost_breakdown(
        size_usd=1_000.0,
        entry_spreads=spreads,
        exit_spreads=spreads,
        entry_spot_depth_usd=1_000_000.0,
        entry_perp_depth_usd=1_000_000.0,
        exit_spot_depth_usd=1_000_000.0,
        exit_perp_depth_usd=1_000_000.0,
        entry_spot_maker_fill_probability=0.0,
        entry_perp_maker_fill_probability=0.0,
        exit_spot_maker_fill_probability=0.0,
        exit_perp_maker_fill_probability=0.0,
    )

    # Four crossings: spot entry, perp entry, spot exit, perp exit. Each taker
    # crossing pays its own half-spread exactly once: 1 + 3 + 1 + 3 = 8 bps.
    assert breakdown.entry.spot_spread_pct == pytest.approx(1.0 / 10_000.0)
    assert breakdown.entry.perp_spread_pct == pytest.approx(3.0 / 10_000.0)
    assert breakdown.exit.spot_spread_pct == pytest.approx(1.0 / 10_000.0)
    assert breakdown.exit.perp_spread_pct == pytest.approx(3.0 / 10_000.0)
    assert breakdown.total_spread_pct == pytest.approx(8.0 / 10_000.0)
    assert breakdown.total_pct == pytest.approx(
        breakdown.entry.total_pct + breakdown.exit.total_pct
    )


def test_combined_spread_compatibility_split_conserves_the_aggregate() -> None:
    spreads = PerLegSpreadBps.from_combined_evenly(Decimal("8.0"))

    assert spreads.spot_bps == Decimal("4.0")
    assert spreads.perp_bps == Decimal("4.0")
    assert spreads.combined_bps == Decimal("8.0")


@pytest.mark.parametrize("invalid", [Decimal("NaN"), Decimal("Infinity"), Decimal("-0.1")])
def test_per_leg_spread_rejects_unknown_or_negative_values(invalid: Decimal) -> None:
    with pytest.raises(ValueError, match="finite non-negative"):
        PerLegSpreadBps.from_values(spot_bps=invalid, perp_bps=Decimal("1"))
