"""Tests for cost_model.py"""

from cost_model import (
    action_cost_pct,
    cost_per_leg,
    entry_cost,
    exit_cost,
    round_trip_cost,
    round_trip_cost_pct,
)

from config import SLIPPAGE_ESTIMATE, TAKER_FEE


def test_cost_per_leg():
    assert cost_per_leg() == TAKER_FEE + SLIPPAGE_ESTIMATE


def test_action_cost_is_two_legs():
    assert action_cost_pct() == cost_per_leg() * 2


def test_round_trip_cost_is_four_legs():
    expected = 4 * (TAKER_FEE + SLIPPAGE_ESTIMATE)
    assert abs(round_trip_cost_pct() - expected) < 1e-12


def test_round_trip_cost_value():
    # 4 × (0.0004 + 0.0002) = 0.0024 = 0.24%
    assert abs(round_trip_cost_pct() - 0.0024) < 1e-12


def test_dollar_costs_scale_linearly():
    notional = 10_000
    assert abs(entry_cost(notional) - notional * action_cost_pct()) < 1e-8
    assert abs(exit_cost(notional) - notional * action_cost_pct()) < 1e-8
    assert abs(round_trip_cost(notional) - notional * round_trip_cost_pct()) < 1e-8


def test_dollar_costs_double_with_notional():
    c1 = round_trip_cost(5_000)
    c2 = round_trip_cost(10_000)
    assert abs(c2 - 2 * c1) < 1e-8
