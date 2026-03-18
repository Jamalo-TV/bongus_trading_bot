"""
Realistic cost model for the delta-neutral funding arbitrage strategy.

Each "action" (open or close) involves TWO legs:
    - Spot: market buy / sell
    - Perp: market sell / buy

Each leg incurs a taker fee + slippage estimate.

A full round trip = open + close = 4 legs total.
"""

import math
from config import (
    TAKER_FEE,
    MAKER_FEE,  # Add MAKER_FEE in config.py
    SLIPPAGE_ESTIMATE,
    LEGS_PER_ACTION,
    ACTIONS_PER_ROUND_TRIP,
)

# New dynamic liquidity adjustment
def liquidity_adjusted_slippage(requested_notional: float, depth_usd: float) -> float:
    """
    Slippage scales non-linearly with order size relative to L2 top-of-book depth.
    If requested notional is large compared to depth_usd, slippage explodes.
    """
    if depth_usd <= 0:
        return SLIPPAGE_ESTIMATE * 5.0
    
    impact_ratio = requested_notional / depth_usd
    return SLIPPAGE_ESTIMATE * (1.0 + math.exp(impact_ratio * 10) - 1.0)

def cost_per_leg(is_maker: bool = True, size_usd: float = 1000.0, depth_usd: float = 100000.0) -> float:
    """Fractional cost per leg depending on maker vs taker and depth impact."""
    fee = MAKER_FEE if is_maker else TAKER_FEE
    slippage = 0.0 if is_maker else liquidity_adjusted_slippage(size_usd, depth_usd)
    return fee + slippage


def action_cost_pct() -> float:
    """Fractional cost for one action (open OR close), both legs."""
    return cost_per_leg() * LEGS_PER_ACTION


def round_trip_cost_pct() -> float:
    """Total fractional cost for a full round trip (open + close)."""
    return action_cost_pct() * ACTIONS_PER_ROUND_TRIP


def entry_cost(notional: float) -> float:
    """Dollar cost to open the hedge (spot long + perp short)."""
    return notional * action_cost_pct()


def exit_cost(notional: float) -> float:
    """Dollar cost to close the hedge (spot sell + perp buy)."""
    return notional * action_cost_pct()


def round_trip_cost(notional: float) -> float:
    """Total dollar cost for open + close."""
    return notional * round_trip_cost_pct()
