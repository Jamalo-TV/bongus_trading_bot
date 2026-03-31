"""
PortfolioAllocator: slot management, liquidity filtering, and rotation logic
for the delta-neutral funding arbitrage strategy.

Phase 2: Added Kelly-based dynamic sizing for optimal risk-adjusted position sizing.
"""

import logging
import math
import sqlite3
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

from bongus.core.config import (
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
from bongus.engine.cost_model import blended_entry_cost, blended_exit_cost

# ── Kelly Criterion (Phase 2) ─────────────────────────────────────────────────

# Kelly fraction for bet sizing: f* = (bp - q) / b
# Where b = odds received, p = win probability, q = lose probability
# Use fractional Kelly (0.5) for risk management
KELLY_FRACTION = 0.50  # Half-Kelly for safety
MIN_KELLY_FRACTION = 0.10  # Minimum 10% of full Kelly
MAX_KELLY_FRACTION = 1.00  # Cap at full Kelly

# Minimum trades needed before trusting Kelly calculation
MIN_TRADES_FOR_KELLY = 5


def calculate_kelly_fraction(win_rate: float, avg_win: float, avg_loss: float) -> float:
    """
    Calculate optimal Kelly fraction for position sizing.
    
    Args:
        win_rate: Percentage of winning trades (0.0 to 1.0)
        avg_win: Average profit when winning (positive value)
        avg_loss: Average loss when losing (positive value)
    
    Returns:
        Kelly fraction capped between MIN_KELLY_FRACTION and MAX_KELLY_FRACTION
    """
    if avg_loss <= 0 or win_rate <= 0:
        return KELLY_FRACTION  # Default to conservative

    b = avg_win / avg_loss  # Odds ratio
    p = win_rate
    q = 1 - p

    # Full Kelly: f* = (bp - q) / b
    kelly = (b * p - q) / b

    # Negative Kelly means the strategy has no positive edge — size to zero.
    # Do NOT clamp a negative value to MIN_KELLY_FRACTION; that would force
    # continued trading on a proven loss-making regime.
    if kelly <= 0:
        logger.warning("Kelly fraction is non-positive (%.4f) — strategy shows no edge; defaulting to minimum sizing", kelly)
        return MIN_KELLY_FRACTION

    # Apply fractional Kelly for risk management, then clamp to [min, max].
    # Multiply before clamping so the constants reflect the actual returned range.
    kelly = kelly * KELLY_FRACTION
    return max(MIN_KELLY_FRACTION, min(MAX_KELLY_FRACTION, kelly))


def get_trade_statistics(db_path: str = "state.db") -> tuple[float, float, float]:
    """
    Extract win rate and average win/loss from trade history.
    
    Returns:
        tuple of (win_rate, avg_win, avg_loss)
    """
    try:
        with sqlite3.connect(db_path, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT net_pnl_usd
                FROM trade_history
                ORDER BY exit_time DESC
                LIMIT 100
            """)
            rows = cursor.fetchall()

        if len(rows) < MIN_TRADES_FOR_KELLY:
            return 0.5, 1.0, 1.0  # Default conservative values
        
        pnls = [row[0] for row in rows]
        wins = [p for p in pnls if p > 0]
        losses = [abs(p) for p in pnls if p <= 0]
        
        win_rate = len(wins) / len(pnls) if pnls else 0.5
        avg_win = sum(wins) / len(wins) if wins else 1.0
        avg_loss = sum(losses) / len(losses) if losses else 1.0
        
        logger.debug(
            "Trade stats: win_rate=%.2f%%, avg_win=$%.2f, avg_loss=$%.2f (n=%d)",
            win_rate * 100, avg_win, avg_loss, len(pnls)
        )
        
        return win_rate, avg_win, avg_loss
    except Exception as e:
        logger.warning("Could not read trade statistics: %s", e)
        return 0.5, 1.0, 1.0  # Default conservative values


def get_leverage_for_rate(ann_funding: float) -> float:
    """Return leverage tier for positive annualized funding, capped by MAX_LEVERAGE."""
    mag = max(0.0, ann_funding)
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
    rotation_notionals: dict[str, float] = field(default_factory=dict)  # {exited_symbol -> target_notional_usd}


class PortfolioAllocator:
    def __init__(self, depth_tracker, funding_ranker, capital_per_slot_usd: float = CAPITAL_PER_SLOT_USD):
        self._depth = depth_tracker
        self._funding = funding_ranker
        self._capital_per_slot = capital_per_slot_usd
        self._last_no_candidates_warn: float = 0.0
        self._kelly_fraction: float = KELLY_FRACTION  # Phase 2: Kelly-based sizing
        self._last_kelly_update: float = 0.0
        self._kelly_update_interval: float = 3600.0  # Recalculate Kelly every hour

    def _update_kelly_fraction(self) -> None:
        """Update Kelly fraction from trade history (called periodically)."""
        now = time.monotonic()
        if now - self._last_kelly_update < self._kelly_update_interval:
            return
        
        try:
            win_rate, avg_win, avg_loss = get_trade_statistics()
            self._kelly_fraction = calculate_kelly_fraction(win_rate, avg_win, avg_loss)
            self._last_kelly_update = now
            logger.info(
                "Updated Kelly fraction: %.2f%% (win_rate=%.1f%%, avg_win=$%.2f, avg_loss=$%.2f)",
                self._kelly_fraction * 100, win_rate * 100, avg_win, avg_loss
            )
        except Exception as e:
            logger.warning("Failed to update Kelly fraction: %s", e)

    def decide(
        self,
        open_positions: list,
        blocked_symbols: set[str] | None = None,
        *,
        notional_scale: float = 1.0,
        rotation_min_gap_ann: float = ROTATION_MIN_GAP_ANN,
    ) -> AllocationDecision:
        # Phase 2: Update Kelly fraction periodically
        self._update_kelly_fraction()

        blocked_symbols = blocked_symbols or set()
        notional_scale = max(0.1, float(notional_scale))
        
        open_symbols = {p.symbol for p in open_positions}

        # Liquidity-filtered ranked candidates (notional computed per-symbol below)
        candidates = []
        for symbol, rate in self._funding.get_ranked():
            if symbol in blocked_symbols:
                continue
            if rate <= 0.0:
                continue
            # Phase 2: Apply Kelly fraction to base notional
            kelly_notional = self._capital_per_slot * self._kelly_fraction * notional_scale
            symbol_notional = min(kelly_notional * get_leverage_for_rate(rate), MAX_NOTIONAL_PER_TRADE)
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
        rotation_notionals = {}
        for position in open_positions:
            target = self._find_rotation_target(
                position,
                candidates,
                rotation_min_gap_ann=rotation_min_gap_ann,
                notional_scale=notional_scale,
            )
            if target:
                target_symbol, target_notional = target
                exits.append((position.symbol, f"rotation to {target_symbol}"))
                rotation_targets[position.symbol] = target_symbol
                rotation_notionals[position.symbol] = target_notional

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
                # Phase 2: Apply Kelly fraction to target notional
                kelly_notional = self._capital_per_slot * self._kelly_fraction * notional_scale
                target_notional = min(kelly_notional * get_leverage_for_rate(rate), MAX_NOTIONAL_PER_TRADE)
                enter.append((symbol, target_notional))
                logger.debug("Kelly sizing: base=%.0f, kelly=%.2f%%, final=%.0f for %s",
                            self._capital_per_slot, self._kelly_fraction * 100, target_notional, symbol)
                open_symbols.add(symbol)
                available_slots -= 1

        exit_symbols = {s for s, _ in exits}
        hold = [p.symbol for p in open_positions if p.symbol not in exit_symbols]
        return AllocationDecision(
            enter=enter,
            exit=exits,
            hold=hold,
            rotation_targets=rotation_targets,
            rotation_notionals=rotation_notionals,
        )

    def _find_rotation_target(
        self,
        position,
        candidates,
        *,
        rotation_min_gap_ann: float,
        notional_scale: float,
    ):
        current_exit_depth = self._depth.get_exit_depth(position.symbol)
        for new_symbol, new_rate in candidates:
            if new_symbol == position.symbol:
                continue
            rate_gap = new_rate - position.ann_funding
            if rate_gap <= rotation_min_gap_ann:
                continue
            kelly_notional = self._capital_per_slot * self._kelly_fraction * notional_scale
            target_notional = min(
                kelly_notional * get_leverage_for_rate(new_rate),
                MAX_NOTIONAL_PER_TRADE,
            )
            new_entry_depth = self._depth.get_entry_depth(new_symbol)
            # Marginal rotation cost = exit current position + enter new position.
            # The future exit of the new position is a sunk cost that would be
            # incurred regardless of whether we rotate, so excluding it gives the
            # correct incremental friction for the rotation payback calculation.
            total_friction_usd = (
                blended_exit_cost(position.notional_usd, depth_usd=current_exit_depth)
                + blended_entry_cost(target_notional, depth_usd=new_entry_depth)
            )
            # rate_gap is annualized; convert to daily income for payback calculation
            incremental_daily_income = (rate_gap / 365) * target_notional
            if incremental_daily_income <= 0:
                continue
            payback_days = total_friction_usd / incremental_daily_income
            if payback_days <= ROTATION_MAX_PAYBACK_DAYS:
                return new_symbol, target_notional
        return None
