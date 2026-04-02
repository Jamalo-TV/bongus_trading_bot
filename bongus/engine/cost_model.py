"""Shared fee, slippage, and edge-estimation model for Bongus."""

from __future__ import annotations

import math
from dataclasses import dataclass

from bongus.core.config import (
    ACTIONS_PER_ROUND_TRIP,
    DEFAULT_HOLDING_HOURS,
    EXECUTION_QUALITY_TARGET_SLIPPAGE_BPS,
    MAKER_FEE_PERP,
    MAKER_FEE_SPOT,
    MAKER_FILL_PROBABILITY,
    NOTIONAL_PER_TRADE,
    SLIPPAGE_ESTIMATE,
    TAKER_FEE_PERP,
    TAKER_FEE_SPOT,
)


@dataclass(slots=True)
class CostContext:
    size_usd: float = NOTIONAL_PER_TRADE
    depth_usd: float = 500_000.0
    spread_bps: float = 0.0
    maker_fill_probability: float = MAKER_FILL_PROBABILITY
    holding_hours: float = DEFAULT_HOLDING_HOURS


@dataclass(slots=True)
class EdgeEstimate:
    gross_edge_pct: float
    net_edge_pct: float
    predicted_pnl_usd: float
    round_trip_cost_pct: float
    payback_hours: float


def liquidity_adjusted_slippage(requested_notional: float, depth_usd: float) -> float:
    """Estimate per-leg slippage as a fraction of notional."""
    if depth_usd <= 0:
        return SLIPPAGE_ESTIMATE * 5.0
    impact_ratio = max(requested_notional, 0.0) / depth_usd
    return SLIPPAGE_ESTIMATE * (1.0 + math.expm1(impact_ratio * 5.0))


def spread_cross_cost_pct(spread_bps: float, is_maker: bool) -> float:
    if is_maker:
        return max(0.0, spread_bps * 0.05 / 10_000.0)
    return max(0.0, spread_bps / 2.0 / 10_000.0)


def cost_per_leg_spot(
    is_maker: bool = False,
    size_usd: float = NOTIONAL_PER_TRADE,
    depth_usd: float = 500_000.0,
    spread_bps: float = 0.0,
) -> float:
    fee = MAKER_FEE_SPOT if is_maker else TAKER_FEE_SPOT
    slippage = 0.0 if is_maker else liquidity_adjusted_slippage(size_usd, depth_usd)
    return fee + slippage + spread_cross_cost_pct(spread_bps, is_maker)


def cost_per_leg_perp(
    is_maker: bool = False,
    size_usd: float = NOTIONAL_PER_TRADE,
    depth_usd: float = 500_000.0,
    spread_bps: float = 0.0,
) -> float:
    fee = MAKER_FEE_PERP if is_maker else TAKER_FEE_PERP
    slippage = 0.0 if is_maker else liquidity_adjusted_slippage(size_usd, depth_usd)
    return fee + slippage + spread_cross_cost_pct(spread_bps, is_maker)


def action_cost_pct(
    size_usd: float = NOTIONAL_PER_TRADE,
    depth_usd: float = 500_000.0,
    spread_bps: float = 0.0,
) -> float:
    return (
        cost_per_leg_spot(False, size_usd=size_usd, depth_usd=depth_usd, spread_bps=spread_bps)
        + cost_per_leg_perp(False, size_usd=size_usd, depth_usd=depth_usd, spread_bps=spread_bps)
    )


def action_cost_pct_maker(
    size_usd: float = NOTIONAL_PER_TRADE,
    depth_usd: float = 500_000.0,
    spread_bps: float = 0.0,
) -> float:
    return (
        cost_per_leg_spot(True, size_usd=size_usd, depth_usd=depth_usd, spread_bps=spread_bps)
        + cost_per_leg_perp(True, size_usd=size_usd, depth_usd=depth_usd, spread_bps=spread_bps)
    )


def round_trip_cost_pct(
    size_usd: float = NOTIONAL_PER_TRADE,
    depth_usd: float = 500_000.0,
    spread_bps: float = 0.0,
) -> float:
    return action_cost_pct(size_usd=size_usd, depth_usd=depth_usd, spread_bps=spread_bps) * ACTIONS_PER_ROUND_TRIP


def round_trip_cost_pct_maker(
    size_usd: float = NOTIONAL_PER_TRADE,
    depth_usd: float = 500_000.0,
    spread_bps: float = 0.0,
) -> float:
    return action_cost_pct_maker(size_usd=size_usd, depth_usd=depth_usd, spread_bps=spread_bps) * ACTIONS_PER_ROUND_TRIP


def blended_action_cost_pct(
    size_usd: float = NOTIONAL_PER_TRADE,
    depth_usd: float = 500_000.0,
    spread_bps: float = 0.0,
    maker_fill_probability: float = MAKER_FILL_PROBABILITY,
) -> float:
    maker = action_cost_pct_maker(size_usd=size_usd, depth_usd=depth_usd, spread_bps=spread_bps)
    taker = action_cost_pct(size_usd=size_usd, depth_usd=depth_usd, spread_bps=spread_bps)
    return maker_fill_probability * maker + (1.0 - maker_fill_probability) * taker


def blended_round_trip_cost_pct(
    size_usd: float = NOTIONAL_PER_TRADE,
    depth_usd: float = 500_000.0,
    spread_bps: float = 4.0,
    maker_fill_probability: float = MAKER_FILL_PROBABILITY,
) -> float:
    return blended_action_cost_pct(
        size_usd=size_usd,
        depth_usd=depth_usd,
        spread_bps=spread_bps,
        maker_fill_probability=maker_fill_probability,
    ) * ACTIONS_PER_ROUND_TRIP


def entry_cost(notional: float, depth_usd: float = 500_000.0, spread_bps: float = 0.0) -> float:
    return notional * action_cost_pct(size_usd=notional, depth_usd=depth_usd, spread_bps=spread_bps)


def exit_cost(notional: float, depth_usd: float = 500_000.0, spread_bps: float = 0.0) -> float:
    return notional * action_cost_pct(size_usd=notional, depth_usd=depth_usd, spread_bps=spread_bps)


def round_trip_cost(notional: float, depth_usd: float = 500_000.0, spread_bps: float = 0.0) -> float:
    return notional * round_trip_cost_pct(size_usd=notional, depth_usd=depth_usd, spread_bps=spread_bps)


def blended_entry_cost(
    notional: float,
    depth_usd: float = 500_000.0,
    spread_bps: float = 0.0,
    maker_fill_probability: float = MAKER_FILL_PROBABILITY,
) -> float:
    return notional * blended_action_cost_pct(
        size_usd=notional,
        depth_usd=depth_usd,
        spread_bps=spread_bps,
        maker_fill_probability=maker_fill_probability,
    )


def blended_round_trip_cost(
    notional: float,
    depth_usd: float = 500_000.0,
    spread_bps: float = 0.0,
    maker_fill_probability: float = MAKER_FILL_PROBABILITY,
) -> float:
    return notional * blended_round_trip_cost_pct(
        size_usd=notional,
        depth_usd=depth_usd,
        spread_bps=spread_bps,
        maker_fill_probability=maker_fill_probability,
    )


def estimated_funding_capture_pct(annualized_funding: float, holding_hours: float) -> float:
    return abs(annualized_funding) * max(holding_hours, 0.0) / 8760.0


def estimate_trade_edge(
    annualized_funding: float,
    context: CostContext | None = None,
) -> EdgeEstimate:
    context = context or CostContext()
    gross_edge_pct = estimated_funding_capture_pct(annualized_funding, context.holding_hours)
    rt_cost = blended_round_trip_cost_pct(
        size_usd=context.size_usd,
        depth_usd=context.depth_usd,
        spread_bps=context.spread_bps,
        maker_fill_probability=context.maker_fill_probability,
    )
    net_edge_pct = gross_edge_pct - rt_cost
    funding_per_hour = abs(annualized_funding) / 8760.0
    payback_hours = math.inf if funding_per_hour <= 0 else rt_cost / funding_per_hour
    return EdgeEstimate(
        gross_edge_pct=gross_edge_pct,
        net_edge_pct=net_edge_pct,
        predicted_pnl_usd=net_edge_pct * context.size_usd,
        round_trip_cost_pct=rt_cost,
        payback_hours=payback_hours,
    )


def rotation_payback_hours(annualized_funding_delta: float, context: CostContext | None = None) -> float:
    return estimate_trade_edge(annualized_funding_delta, context=context).payback_hours


def quality_score_from_slippage(realized_slippage_bps: float, target_bps: float = EXECUTION_QUALITY_TARGET_SLIPPAGE_BPS) -> float:
    if target_bps <= 0:
        return 0.0
    return max(0.0, 1.0 - max(0.0, realized_slippage_bps) / target_bps)
