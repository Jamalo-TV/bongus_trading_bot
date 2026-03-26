"""
PortfolioAllocator: slot management, liquidity filtering, and rotation logic
for the delta-neutral funding arbitrage strategy.
"""

import logging
import os
import sys
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Allow importing cost_model which lives in bongus/engine
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'engine')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'core')))

from config import (
    MAX_CONCURRENT_POSITIONS,
    CAPITAL_PER_SLOT_USD,
    TARGET_LEVERAGE,
    LIQUIDITY_FILTER_MULTIPLIER,
    ROTATION_MIN_GAP_ANN,
    ROTATION_MAX_PAYBACK_DAYS,
    LEVERAGE_TIERS,
    MAX_LEVERAGE,
    MAX_NOTIONAL_PER_TRADE,
)
from cost_model import blended_entry_cost, blended_exit_cost


def get_leverage_for_rate(ann_funding: float) -> float:
    """Return leverage tier for an annualized funding rate magnitude, capped by MAX_LEVERAGE."""
    # Use absolute value to correctly size both long and inverse trades based on magnitude
    mag = abs(ann_funding)
    for threshold, leverage in LEVERAGE_TIERS:
        if mag < threshold:
            return min(leverage, MAX_LEVERAGE)
    return MAX_LEVERAGE


@dataclass
class OpenPosition:
    symbol: str
    notional_usd: float
    ann_funding: float


@dataclass
class AllocationDecision:
    enter: list  # list[tuple[str, float]] — [(symbol, notional_usd)]
    exit: list   # list[tuple[str, str]]  — [(symbol, reason)]
    hold: list   # list[str]
    rotation_targets: dict[str, str] = field(default_factory=dict)  # {exited_symbol -> entry_target}


class PortfolioAllocator:
    def __init__(self, depth_tracker, funding_ranker, capital_per_slot_usd: float = CAPITAL_PER_SLOT_USD):
        self._depth = depth_tracker
        self._funding = funding_ranker
        self._capital_per_slot = capital_per_slot_usd
        self._last_no_candidates_warn: float = 0.0

    def decide(self, open_positions: list) -> AllocationDecision:
        open_symbols = {p.symbol for p in open_positions}

        # Liquidity-filtered ranked candidates (notional computed per-symbol below)
        candidates = []
        for symbol, rate in self._funding.get_ranked():
            symbol_notional = min(self._capital_per_slot * get_leverage_for_rate(rate), MAX_NOTIONAL_PER_TRADE)
            if self._depth.get_entry_depth(symbol) >= LIQUIDITY_FILTER_MULTIPLIER * symbol_notional:
                candidates.append((symbol, rate))

        if not candidates:
            now = time.monotonic()
            if now - self._last_no_candidates_warn >= 60:
                self._last_no_candidates_warn = now
                ranked = self._funding.get_ranked()
                if ranked:
                    sample = [(s, f"depth={self._depth.get_entry_depth(s):.0f} need={min(self._capital_per_slot * get_leverage_for_rate(r), MAX_NOTIONAL_PER_TRADE) * LIQUIDITY_FILTER_MULTIPLIER:.0f}") for s, r in ranked[:5]]
                    logger.warning("No liquidity-eligible candidates — top-5: %s", sample)
                else:
                    logger.warning("No liquidity-eligible candidates — funding ranker returned empty list (stale?)")

        # Rotation (evaluated before slot fill so targets are excluded from fresh entries)
        exits = []
        rotation_targets = {}
        for position in open_positions:
            pos_notional = min(self._capital_per_slot * get_leverage_for_rate(position.ann_funding), MAX_NOTIONAL_PER_TRADE)
            target = self._find_rotation_target(position, candidates, pos_notional)
            if target:
                exits.append((position.symbol, f"rotation to {target}"))
                rotation_targets[position.symbol] = target

        # Exclude rotation targets from fresh slot fills — they will be entered
        # via the exit-confirmed path in live_trader_v2, not as new slots.
        rotation_target_symbols = set(rotation_targets.values())

        # Fill empty slots
        enter = []
        available_slots = MAX_CONCURRENT_POSITIONS - len(open_positions)
        for symbol, rate in candidates:
            if available_slots <= 0:
                break
            if symbol not in open_symbols and symbol not in rotation_target_symbols:
                target_notional = min(self._capital_per_slot * get_leverage_for_rate(rate), MAX_NOTIONAL_PER_TRADE)
                enter.append((symbol, target_notional))
                open_symbols.add(symbol)
                available_slots -= 1

        exit_symbols = {s for s, _ in exits}
        hold = [p.symbol for p in open_positions if p.symbol not in exit_symbols]
        return AllocationDecision(enter=enter, exit=exits, hold=hold, rotation_targets=rotation_targets)

    def _find_rotation_target(self, position, candidates, target_notional):
        current_exit_depth = self._depth.get_exit_depth(position.symbol)
        for new_symbol, new_rate in candidates:
            if new_symbol == position.symbol:
                continue
            rate_gap = abs(new_rate) - abs(position.ann_funding)
            if rate_gap <= ROTATION_MIN_GAP_ANN:
                continue
            new_entry_depth = self._depth.get_entry_depth(new_symbol)
            new_exit_depth = self._depth.get_exit_depth(new_symbol)
            total_friction_usd = (
                blended_exit_cost(position.notional_usd, depth_usd=current_exit_depth)
                + blended_entry_cost(target_notional, depth_usd=new_entry_depth)
                + blended_exit_cost(target_notional, depth_usd=new_exit_depth)
            )
            # rate_gap is annualized; convert to daily income for payback calculation
            incremental_daily_income = (rate_gap / 365) * target_notional
            if incremental_daily_income <= 0:
                continue
            payback_days = total_friction_usd / incremental_daily_income
            if payback_days <= ROTATION_MAX_PAYBACK_DAYS:
                return new_symbol
        return None
