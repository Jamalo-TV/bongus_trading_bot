"""Tests for CorrelationBreaker state machine.

States are mutually exclusive and collectively exhaustive:
  CLEAR:     negative_ratio < 0.50  (includes 0%)
  HALTED:    0.50 <= negative_ratio < 1.00
  EMERGENCY: negative_ratio == 1.00
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'bongus', 'portfolio')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'bongus', 'core')))

from correlation_breaker import CorrelationBreaker

_THRESHOLD = 0.005  # EXIT_ANN_FUNDING_THRESHOLD from config


def _above(n: int) -> dict[str, float]:
    """n positions all above threshold."""
    return {f"SYM{i}": _THRESHOLD + 0.05 for i in range(n)}


def _below(n: int, start: int = 0) -> dict[str, float]:
    """n positions all below threshold."""
    return {f"SYM{i}": _THRESHOLD - 0.001 for i in range(start, start + n)}


def test_empty_portfolio_is_clear():
    """No positions → CLEAR with allow_new_entries=True."""
    breaker = CorrelationBreaker()
    decision = breaker.evaluate({})
    assert decision.state == "CLEAR"
    assert decision.allow_new_entries is True
    assert decision.positions_to_exit == []


def test_all_above_threshold_is_clear():
    """0% negative → CLEAR."""
    breaker = CorrelationBreaker()
    decision = breaker.evaluate(_above(4))
    assert decision.state == "CLEAR"
    assert decision.allow_new_entries is True


def test_40_percent_negative_is_still_clear():
    """2/5 = 40% negative → CLEAR (below 50% halt threshold)."""
    breaker = CorrelationBreaker()
    positions = {**_above(3), **_below(2, start=3)}
    decision = breaker.evaluate(positions)
    assert decision.state == "CLEAR"
    assert decision.allow_new_entries is True


def test_50_percent_negative_is_halted():
    """2/4 = 50% negative → HALTED."""
    breaker = CorrelationBreaker()
    positions = {**_above(2), **_below(2, start=2)}
    decision = breaker.evaluate(positions)
    assert decision.state == "HALTED"
    assert decision.allow_new_entries is False
    assert decision.positions_to_exit == []  # HALTED does not force exits


def test_75_percent_negative_is_halted():
    """3/4 = 75% negative → HALTED (not EMERGENCY)."""
    breaker = CorrelationBreaker()
    positions = {**_above(1), **_below(3, start=1)}
    decision = breaker.evaluate(positions)
    assert decision.state == "HALTED"


def test_100_percent_negative_is_emergency():
    """4/4 = 100% negative → EMERGENCY."""
    breaker = CorrelationBreaker()
    positions = _below(4)
    decision = breaker.evaluate(positions)
    assert decision.state == "EMERGENCY"
    assert decision.allow_new_entries is False
    assert set(decision.positions_to_exit) == set(positions.keys())


def test_emergency_positions_to_exit_is_all_symbols():
    """EMERGENCY: positions_to_exit contains every open symbol."""
    breaker = CorrelationBreaker()
    positions = _below(3)
    decision = breaker.evaluate(positions)
    assert sorted(decision.positions_to_exit) == sorted(positions.keys())


def test_single_position_negative_is_emergency():
    """1/1 = 100% → EMERGENCY, not CLEAR."""
    breaker = CorrelationBreaker()
    decision = breaker.evaluate({"BTCUSDT": _THRESHOLD - 0.001})
    assert decision.state == "EMERGENCY"


def test_single_position_positive_is_clear():
    """1/1 all positive → CLEAR."""
    breaker = CorrelationBreaker()
    decision = breaker.evaluate({"BTCUSDT": _THRESHOLD + 0.05})
    assert decision.state == "CLEAR"
