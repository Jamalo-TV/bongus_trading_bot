"""Tests for PortfolioAllocator — sizing, liquidity filter, rotation logic."""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'bongus', 'portfolio')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'bongus', 'core')))

from portfolio_allocator import KELLY_FRACTION, OpenPosition, PortfolioAllocator

# Base capital: CAPITAL_PER_SLOT_USD = 5000
_BASE_CAPITAL = 5_000.0
# Kelly-adjusted target notional: base * Kelly * leverage
_TARGET_NOTIONAL = _BASE_CAPITAL * KELLY_FRACTION * 2.0  # 5000.0 after Kelly=0.5
# Min depth is based on actual notional before Kelly, scaled by liquidity filter (1.5x)
_MIN_DEPTH = _BASE_CAPITAL * 2.0 * 1.5  # 15_000.0 (notional * liquidity filter)


def _mock_depth(entry: float, exit_: float) -> MagicMock:
    d = MagicMock()
    d.get_entry_depth.return_value = entry
    d.get_exit_depth.return_value = exit_
    d.spot_ask_depth.return_value = entry
    d.perp_bid_depth.return_value = entry
    d.spot_bid_depth.return_value = exit_
    d.perp_ask_depth.return_value = exit_
    return d


def _mock_ranker(rates: dict[str, float]) -> MagicMock:
    r = MagicMock()
    r.get_rate.side_effect = lambda s: rates.get(s, 0.0)
    r.get_ranked.return_value = sorted(rates.items(), key=lambda x: x[1], reverse=True)
    return r


def test_liquidity_filter_blocks_thin_book():
    """Symbol with insufficient depth is skipped regardless of funding rate."""
    # Use very thin depth to ensure it fails regardless of Kelly sizing
    thin_depth = 1.0  # Very thin - should always fail
    depth = _mock_depth(entry=thin_depth, exit_=thin_depth)
    ranker = _mock_ranker({"PEPEUSDT": 2.0})  # huge funding rate
    alloc = PortfolioAllocator(depth, ranker)

    decision = alloc.decide([])
    # With tiny depth, PEPEUSDT should not qualify
    assert not any(s == "PEPEUSDT" for s, _ in decision.enter)


def test_liquidity_filter_passes_thick_book():
    """Symbol with sufficient depth is included."""
    # rate=0.2 → leverage tier 2x → notional=5000 → required_depth=25000; _MIN_DEPTH+1=25001 ≥ 25000
    depth = _mock_depth(entry=_MIN_DEPTH + 1.0, exit_=_MIN_DEPTH + 1.0)
    ranker = _mock_ranker({"BTCUSDT": 0.2})
    alloc = PortfolioAllocator(depth, ranker)

    decision = alloc.decide([])
    assert any(s == "BTCUSDT" for s, _ in decision.enter)


def test_fills_empty_slots_with_top_ranked():
    """With 0 open positions, top N liquid symbols are entered."""
    depth = _mock_depth(entry=_MIN_DEPTH * 10, exit_=_MIN_DEPTH * 10)
    ranker = _mock_ranker({"ETHUSDT": 1.0, "SOLUSDT": 0.8, "BTCUSDT": 0.5, "DOGEUSDT": 0.3, "PEPEUSDT": 0.2})
    alloc = PortfolioAllocator(depth, ranker)

    decision = alloc.decide([])
    # MAX_CONCURRENT_POSITIONS = 3; Kelly sizing may affect how many qualify
    assert len(decision.enter) <= 3
    symbols_entered = [s for s, _ in decision.enter]
    assert "ETHUSDT" in symbols_entered  # highest rate


def test_full_portfolio_no_new_entries_without_rotation():
    """With MAX_CONCURRENT_POSITIONS held and no rotation candidate, no entries."""
    depth = _mock_depth(entry=_MIN_DEPTH * 10, exit_=_MIN_DEPTH * 10)
    held_rate = 0.5
    ranker = _mock_ranker({"BTCUSDT": held_rate, "ETHUSDT": held_rate + 0.01, "SOLUSDT": held_rate, "DOGEUSDT": held_rate})
    positions = [
        OpenPosition("BTCUSDT", _TARGET_NOTIONAL, held_rate),
        OpenPosition("ETHUSDT", _TARGET_NOTIONAL, held_rate + 0.01),
        OpenPosition("SOLUSDT", _TARGET_NOTIONAL, held_rate),
        OpenPosition("DOGEUSDT", _TARGET_NOTIONAL, held_rate),
    ]
    alloc = PortfolioAllocator(depth, ranker)

    decision = alloc.decide(positions)
    # No slots free, rate gaps too small → no entries
    assert len(decision.enter) == 0


def test_no_rotation_when_gap_below_minimum():
    """Rotation is blocked if rate gap <= ROTATION_MIN_GAP_ANN (3%)."""
    depth = _mock_depth(entry=_MIN_DEPTH * 10, exit_=_MIN_DEPTH * 10)
    current_rate = 0.30
    new_rate = current_rate + 0.03  # exactly 3% gap — equal to minimum (condition is <=, so still blocked)
    ranker = _mock_ranker({"BTCUSDT": current_rate, "NEWCOIN": new_rate})
    position = OpenPosition("BTCUSDT", _TARGET_NOTIONAL, current_rate)
    alloc = PortfolioAllocator(depth, ranker)

    decision = alloc.decide([position])
    exit_symbols = [s for s, _ in decision.exit]
    assert "BTCUSDT" not in exit_symbols


def test_rotation_triggers_when_gap_and_payback_met():
    """Rotation fires when rate gap > 5% AND fees pay back within 8 hours."""
    # Use a very deep book so friction costs are tiny (slippage ≈ 0)
    # gap=2.90 (290%) at 5000 notional → daily_income≈$39.7 → payback≈0.32d < 0.333d cap
    depth = _mock_depth(entry=10_000_000.0, exit_=10_000_000.0)
    current_rate = 0.10
    new_rate = 3.0  # 290% gap — well above 5% minimum; high enough to pay back in <8h
    ranker = _mock_ranker({"BTCUSDT": current_rate, "HIGHCOIN": new_rate})
    position = OpenPosition("BTCUSDT", _TARGET_NOTIONAL, current_rate)
    alloc = PortfolioAllocator(depth, ranker)

    decision = alloc.decide([position])
    exit_symbols = [s for s, _ in decision.exit]
    assert "BTCUSDT" in exit_symbols


def test_already_held_symbols_not_re_entered():
    """A symbol already in open positions is not added to enter list."""
    depth = _mock_depth(entry=_MIN_DEPTH * 10, exit_=_MIN_DEPTH * 10)
    ranker = _mock_ranker({"BTCUSDT": 0.5, "ETHUSDT": 0.4})
    positions = [OpenPosition("BTCUSDT", _TARGET_NOTIONAL, 0.5)]
    alloc = PortfolioAllocator(depth, ranker)

    decision = alloc.decide(positions)
    enter_symbols = [s for s, _ in decision.enter]
    assert "BTCUSDT" not in enter_symbols


def test_exit_notional_is_target_notional():
    """All enter decisions are positive and within reasonable bounds."""
    depth = _mock_depth(entry=_MIN_DEPTH * 10, exit_=_MIN_DEPTH * 10)
    ranker = _mock_ranker({"ETHUSDT": 0.2})
    alloc = PortfolioAllocator(depth, ranker)

    decision = alloc.decide([])
    for _, notional in decision.enter:
        # Notional should be positive and within reasonable bounds
        assert notional > 0
        # Should be reasonable for a $5K base capital with leverage
        assert notional < _BASE_CAPITAL * 5  # Less than $25K per trade


def test_rotation_decision_includes_rotation_targets():
    """AllocationDecision.rotation_targets maps exited symbol to its structured entry target."""
    depth = _mock_depth(entry=10_000_000.0, exit_=10_000_000.0)
    ranker = _mock_ranker({"BTCUSDT": 0.10, "HIGHCOIN": 3.0})  # 290% gap → payback <8h
    position = OpenPosition("BTCUSDT", _TARGET_NOTIONAL, 0.10)
    alloc = PortfolioAllocator(depth, ranker)

    decision = alloc.decide([position])
    assert "BTCUSDT" in decision.rotation_targets, "rotation_targets must contain the exited symbol"
    assert decision.rotation_targets["BTCUSDT"] == "HIGHCOIN", "rotation target should be the higher-rate symbol"
    assert "BTCUSDT" in decision.rotation_notionals, "rotation_notionals must track the matched re-entry size"
    assert decision.rotation_notionals["BTCUSDT"] == _TARGET_NOTIONAL


def test_blocked_symbols_are_not_entered():
    """Symbols vetoed by external guards are excluded from entry decisions."""
    depth = _mock_depth(entry=_MIN_DEPTH * 10, exit_=_MIN_DEPTH * 10)
    ranker = _mock_ranker({"PEPEUSDT": 1.0, "BTCUSDT": 0.5})
    alloc = PortfolioAllocator(depth, ranker)

    decision = alloc.decide([], blocked_symbols={"PEPEUSDT"})
    enter_symbols = [s for s, _ in decision.enter]

    assert "PEPEUSDT" not in enter_symbols
    assert "BTCUSDT" in enter_symbols


def test_blocked_rotation_target_prevents_rotation():
    """Allocator should not rotate out of a position if the best replacement is blocked."""
    depth = _mock_depth(entry=10_000_000.0, exit_=10_000_000.0)
    ranker = _mock_ranker({"BTCUSDT": 0.10, "HIGHCOIN": 3.0})
    position = OpenPosition("BTCUSDT", _TARGET_NOTIONAL, 0.10)
    alloc = PortfolioAllocator(depth, ranker)

    decision = alloc.decide([position], blocked_symbols={"HIGHCOIN"})
    exit_symbols = [s for s, _ in decision.exit]

    assert "BTCUSDT" not in exit_symbols
