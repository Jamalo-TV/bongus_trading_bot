"""
Realistic cost model for the delta-neutral funding arbitrage strategy.

Each "action" (open or close) involves TWO legs:
    - Spot: market buy / sell
    - Perp: market sell / buy

Each leg incurs a taker fee + slippage estimate.

A full round trip = open + close = 4 legs total.

Includes blended maker/taker cost functions since the Rust chase system
tries limit orders first and falls back to market orders.
"""

import math

from config import (
    ACTIONS_PER_ROUND_TRIP,
    LEGS_PER_ACTION,
    MAKER_FEE,
    MAKER_FILL_PROBABILITY,
    SLIPPAGE_ESTIMATE,
    TAKER_FEE,
)

# ── Dynamic Liquidity Adjustment ─────────────────────────────────────────

def liquidity_adjusted_slippage(requested_notional: float, depth_usd: float) -> float:
    """
    Slippage scales non-linearly with order size relative to L2 top-of-book depth.
    If requested notional is large compared to depth_usd, slippage explodes.
    """
    if depth_usd <= 0:
        return SLIPPAGE_ESTIMATE * 5.0

    impact_ratio = requested_notional / depth_usd
    return SLIPPAGE_ESTIMATE * (1.0 + math.exp(impact_ratio * 10) - 1.0)


# ── Per-Leg Costs ────────────────────────────────────────────────────────

def cost_per_leg(is_maker: bool = False, size_usd: float = 0.0, depth_usd: float = 100_000.0) -> float:
    """Fractional cost per leg depending on maker vs taker and depth impact."""
    fee = MAKER_FEE if is_maker else TAKER_FEE
    slippage = 0.0 if is_maker else liquidity_adjusted_slippage(size_usd, depth_usd)
    return fee + slippage


# ── Pure Taker Costs (worst case) ────────────────────────────────────────

def action_cost_pct() -> float:
    """Fractional cost for one action (open OR close), both legs, taker only."""
    return cost_per_leg() * LEGS_PER_ACTION


def round_trip_cost_pct() -> float:
    """Total fractional cost for a full round trip (open + close), taker only."""
    return action_cost_pct() * ACTIONS_PER_ROUND_TRIP


# ── Pure Maker Costs (best case) ─────────────────────────────────────────

def action_cost_pct_maker() -> float:
    """Fractional cost for one action, both legs, maker only (with rebate)."""
    return cost_per_leg(is_maker=True) * LEGS_PER_ACTION


def round_trip_cost_pct_maker() -> float:
    """Total fractional cost for a full round trip, maker only."""
    return action_cost_pct_maker() * ACTIONS_PER_ROUND_TRIP


# ── Blended Costs (realistic expectation) ────────────────────────────────

def blended_action_cost_pct() -> float:
    """
    Blended cost per action accounting for maker fill probability.
    The Rust chase system tries limit first (maker) then falls back to market (taker).
    """
    return (
        MAKER_FILL_PROBABILITY * action_cost_pct_maker()
        + (1 - MAKER_FILL_PROBABILITY) * action_cost_pct()
    )


def blended_round_trip_cost_pct() -> float:
    """Blended round trip cost (maker probability weighted)."""
    return blended_action_cost_pct() * ACTIONS_PER_ROUND_TRIP


# ── Dollar Costs ─────────────────────────────────────────────────────────

def entry_cost(notional: float) -> float:
    """Dollar cost to open the hedge (spot long + perp short)."""
    return notional * action_cost_pct()


def exit_cost(notional: float) -> float:
    """Dollar cost to close the hedge (spot sell + perp buy)."""
    return notional * action_cost_pct()


def round_trip_cost(notional: float) -> float:
    """Total dollar cost for open + close."""
    return notional * round_trip_cost_pct()


def blended_entry_cost(notional: float) -> float:
    """Dollar cost to open, blended maker/taker."""
    return notional * blended_action_cost_pct()


def blended_round_trip_cost(notional: float) -> float:
    """Total dollar cost for open + close, blended."""
    return notional * blended_round_trip_cost_pct()
