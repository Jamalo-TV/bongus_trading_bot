"""
PortfolioAllocator: slot management, liquidity filtering, and rotation logic
for the delta-neutral funding arbitrage strategy.
"""

import os
import sys
from dataclasses import dataclass, field

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
)
from cost_model import blended_entry_cost, blended_exit_cost


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
    def __init__(self, depth_tracker, funding_ranker):
        self._depth = depth_tracker
        self._funding = funding_ranker

    def decide(self, open_positions: list) -> AllocationDecision:
        target_notional = CAPITAL_PER_SLOT_USD * TARGET_LEVERAGE
        open_symbols = {p.symbol for p in open_positions}

        # Liquidity-filtered ranked candidates
        candidates = []
        for symbol, rate in self._funding.get_ranked():
            if self._depth.get_entry_depth(symbol) >= LIQUIDITY_FILTER_MULTIPLIER * target_notional:
                candidates.append((symbol, rate))

        # Fill empty slots
        enter = []
        available_slots = MAX_CONCURRENT_POSITIONS - len(open_positions)
        for symbol, _rate in candidates:
            if available_slots <= 0:
                break
            if symbol not in open_symbols:
                enter.append((symbol, target_notional))
                open_symbols.add(symbol)
                available_slots -= 1

        # Rotation
        exits = []
        rotation_targets = {}
        for position in open_positions:
            target = self._find_rotation_target(position, candidates, target_notional)
            if target:
                exits.append((position.symbol, f"rotation to {target}"))
                rotation_targets[position.symbol] = target

        exit_symbols = {s for s, _ in exits}
        hold = [p.symbol for p in open_positions if p.symbol not in exit_symbols]
        return AllocationDecision(enter=enter, exit=exits, hold=hold, rotation_targets=rotation_targets)

    def _find_rotation_target(self, position, candidates, target_notional):
        current_exit_depth = self._depth.get_exit_depth(position.symbol)
        for new_symbol, new_rate in candidates:
            if new_symbol == position.symbol:
                continue
            rate_gap = new_rate - position.ann_funding
            if rate_gap <= ROTATION_MIN_GAP_ANN:
                continue
            new_entry_depth = self._depth.get_entry_depth(new_symbol)
            new_exit_depth = self._depth.get_exit_depth(new_symbol)
            total_friction_usd = (
                blended_exit_cost(position.notional_usd, depth_usd=current_exit_depth)
                + blended_entry_cost(target_notional, depth_usd=new_entry_depth)
                + blended_exit_cost(target_notional, depth_usd=new_exit_depth)
            )
            incremental_daily_income = rate_gap * target_notional
            if incremental_daily_income <= 0:
                continue
            payback_days = total_friction_usd / incremental_daily_income
            if payback_days <= ROTATION_MAX_PAYBACK_DAYS:
                return new_symbol
        return None
