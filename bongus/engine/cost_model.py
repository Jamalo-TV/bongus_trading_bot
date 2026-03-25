"""
Realistic cost model for the delta-neutral funding arbitrage strategy.

Each "action" (open or close) involves TWO legs:
    - Spot: market buy / sell
    - Perp: market sell / buy

Each leg incurs a fee + slippage estimate (slippage only for taker fills).

A full round trip = open + close = 4 legs total.

Includes blended maker/taker cost functions since the Rust chase system
tries limit orders first and falls back to market orders.
"""

import math

from config import (
    ACTIONS_PER_ROUND_TRIP,
    MAKER_FEE_PERP,
    MAKER_FEE_SPOT,
    MAKER_FILL_PROBABILITY,
    NOTIONAL_PER_TRADE,
    SLIPPAGE_ESTIMATE,
    TAKER_FEE_PERP,
    TAKER_FEE_SPOT,
)

# ── Dynamic Liquidity Adjustment ─────────────────────────────────────────

def liquidity_adjusted_slippage(requested_notional: float, depth_usd: float) -> float:
    """
    Slippage scales with order size relative to L2 top-of-book depth using a
    square-root market impact model, capped at impact_ratio=1.0.

    The previous exp(ratio * 10) model produced astronomically large values
    (e.g. 440% slippage at impact_ratio=1.0) which caused the rotation payback
    calculation to refuse valid opportunities.

    Square-root impact is the standard empirical model for limit-order-book
    markets and stays bounded: at ratio=1.0 slippage = SLIPPAGE_ESTIMATE * 1.0,
    at ratio=0.25 slippage = SLIPPAGE_ESTIMATE * 0.5.
    """
    if depth_usd <= 0:
        return SLIPPAGE_ESTIMATE * 5.0

    impact_ratio = min(requested_notional / depth_usd, 1.0)
    return SLIPPAGE_ESTIMATE * math.sqrt(impact_ratio)


# ── Per-Leg Costs ────────────────────────────────────────────────────────

def cost_per_leg_spot(
    is_maker: bool = False,
    size_usd: float = NOTIONAL_PER_TRADE,
    depth_usd: float = 500_000.0,
) -> float:
    """Fractional cost for the spot leg."""
    fee = MAKER_FEE_SPOT if is_maker else TAKER_FEE_SPOT
    slippage = 0.0 if is_maker else liquidity_adjusted_slippage(size_usd, depth_usd)
    return fee + slippage


def cost_per_leg_perp(
    is_maker: bool = False,
    size_usd: float = NOTIONAL_PER_TRADE,
    depth_usd: float = 500_000.0,
) -> float:
    """Fractional cost for the perp leg."""
    fee = MAKER_FEE_PERP if is_maker else TAKER_FEE_PERP
    slippage = 0.0 if is_maker else liquidity_adjusted_slippage(size_usd, depth_usd)
    return fee + slippage


# ── Action Costs (one open OR one close = spot + perp) ─────────────────

def action_cost_pct(
    size_usd: float = NOTIONAL_PER_TRADE,
    depth_usd: float = 500_000.0,
) -> float:
    """Fractional cost for one action (open OR close), both legs, taker only."""
    return (
        cost_per_leg_spot(is_maker=False, size_usd=size_usd, depth_usd=depth_usd)
        + cost_per_leg_perp(is_maker=False, size_usd=size_usd, depth_usd=depth_usd)
    )


def action_cost_pct_maker() -> float:
    """Fractional cost for one action, both legs, maker only."""
    return cost_per_leg_spot(is_maker=True) + cost_per_leg_perp(is_maker=True)


# ── Round-Trip Costs ───────────────────────────────────────────────────

def round_trip_cost_pct(
    size_usd: float = NOTIONAL_PER_TRADE,
    depth_usd: float = 500_000.0,
) -> float:
    """Total fractional cost for a full round trip (open + close), taker only."""
    return action_cost_pct(size_usd=size_usd, depth_usd=depth_usd) * ACTIONS_PER_ROUND_TRIP


def round_trip_cost_pct_maker() -> float:
    """Total fractional cost for a full round trip, maker only."""
    return action_cost_pct_maker() * ACTIONS_PER_ROUND_TRIP


# ── Blended Costs (realistic expectation) ────────────────────────────────

def blended_action_cost_pct(
    size_usd: float = NOTIONAL_PER_TRADE,
    depth_usd: float = 500_000.0,
) -> float:
    """
    Blended cost per action accounting for maker fill probability.
    The Rust chase system tries limit first (maker) then falls back to market (taker).
    """
    return (
        MAKER_FILL_PROBABILITY * action_cost_pct_maker()
        + (1 - MAKER_FILL_PROBABILITY) * action_cost_pct(size_usd=size_usd, depth_usd=depth_usd)
    )


def blended_round_trip_cost_pct(
    size_usd: float = NOTIONAL_PER_TRADE,
    depth_usd: float = 500_000.0,
) -> float:
    """Blended round trip cost (maker probability weighted)."""
    return blended_action_cost_pct(size_usd=size_usd, depth_usd=depth_usd) * ACTIONS_PER_ROUND_TRIP


# ── Dollar Costs ─────────────────────────────────────────────────────────

def entry_cost(notional: float, depth_usd: float = 500_000.0) -> float:
    """Dollar cost to open the hedge (spot long + perp short)."""
    return notional * action_cost_pct(size_usd=notional, depth_usd=depth_usd)


def exit_cost(notional: float, depth_usd: float = 500_000.0) -> float:
    """Dollar cost to close the hedge (spot sell + perp buy)."""
    return notional * action_cost_pct(size_usd=notional, depth_usd=depth_usd)


def round_trip_cost(notional: float, depth_usd: float = 500_000.0) -> float:
    """Total dollar cost for open + close."""
    return notional * round_trip_cost_pct(size_usd=notional, depth_usd=depth_usd)


def blended_entry_cost(notional: float, depth_usd: float = 500_000.0) -> float:
    """Dollar cost to open, blended maker/taker."""
    return notional * blended_action_cost_pct(size_usd=notional, depth_usd=depth_usd)


def blended_exit_cost(notional: float, depth_usd: float = 500_000.0) -> float:
    """Dollar cost to close one position (spot sell + perp cover), blended maker/taker.

    Mirrors blended_entry_cost. depth_usd=500_000.0 default is kept for backtest
    compatibility — live_trader_v2.py always passes real depth from DepthTracker.
    """
    return notional * blended_action_cost_pct(size_usd=notional, depth_usd=depth_usd)


def blended_round_trip_cost(notional: float, depth_usd: float = 500_000.0) -> float:
    """Total dollar cost for open + close, blended."""
    return notional * blended_round_trip_cost_pct(size_usd=notional, depth_usd=depth_usd)
