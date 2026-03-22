"""Tests for cost_model.py"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'bongus')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'bongus', 'engine')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'bongus', 'core')))

from cost_model import (
    action_cost_pct,
    cost_per_leg_spot,
    cost_per_leg_perp,
    entry_cost,
    exit_cost,
    round_trip_cost,
    round_trip_cost_pct,
    blended_round_trip_cost_pct,
    liquidity_adjusted_slippage,
)

from config import (
    SLIPPAGE_ESTIMATE,
    TAKER_FEE_SPOT,
    TAKER_FEE_PERP,
    MAKER_FEE_SPOT,
    MAKER_FEE_PERP,
    NOTIONAL_PER_TRADE,
)


def test_cost_per_leg_spot_taker():
    """Spot taker cost = spot taker fee + slippage."""
    cost = cost_per_leg_spot(is_maker=False, size_usd=0.0, depth_usd=100_000.0)
    # size_usd=0 → impact_ratio=0 → slippage = SLIPPAGE_ESTIMATE * exp(0) = SLIPPAGE_ESTIMATE
    assert abs(cost - (TAKER_FEE_SPOT + SLIPPAGE_ESTIMATE)) < 1e-12


def test_cost_per_leg_perp_taker():
    """Perp taker cost = perp taker fee + slippage."""
    cost = cost_per_leg_perp(is_maker=False, size_usd=0.0, depth_usd=100_000.0)
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
