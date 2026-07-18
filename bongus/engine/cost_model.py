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


@dataclass(frozen=True, slots=True)
class PairedActionCostBreakdown:
    """Per-leg cost attribution for one paired spot/perpetual action.

    This additive API intentionally leaves the legacy cost functions unchanged
    until calibrated shadow results pass the Phase 2 promotion gate.
    """

    spot_fee_pct: float
    perp_fee_pct: float
    spot_spread_pct: float
    perp_spread_pct: float
    spot_impact_pct: float
    perp_impact_pct: float

    @property
    def total_pct(self) -> float:
        return (
            self.spot_fee_pct
            + self.perp_fee_pct
            + self.spot_spread_pct
            + self.perp_spread_pct
            + self.spot_impact_pct
            + self.perp_impact_pct
        )


def paired_action_cost_breakdown(
    *,
    size_usd: float = NOTIONAL_PER_TRADE,
    spot_depth_usd: float = 500_000.0,
    perp_depth_usd: float = 500_000.0,
    spot_spread_bps: float = 0.0,
    perp_spread_bps: float = 0.0,
    spot_maker_fill_probability: float = MAKER_FILL_PROBABILITY,
    perp_maker_fill_probability: float = MAKER_FILL_PROBABILITY,
) -> PairedActionCostBreakdown:
    """Attribute fees, spread and impact to independently executable legs."""

    numeric_values = {
        "size_usd": size_usd,
        "spot_depth_usd": spot_depth_usd,
        "perp_depth_usd": perp_depth_usd,
        "spot_spread_bps": spot_spread_bps,
        "perp_spread_bps": perp_spread_bps,
        "spot_maker_fill_probability": spot_maker_fill_probability,
        "perp_maker_fill_probability": perp_maker_fill_probability,
    }
    if any(not math.isfinite(float(value)) for value in numeric_values.values()):
        raise ValueError("paired cost inputs must be finite")
    if size_usd <= 0.0:
        raise ValueError("size_usd must be positive")
    if spot_depth_usd < 0.0 or perp_depth_usd < 0.0:
        raise ValueError("depth must be non-negative")
    if spot_spread_bps < 0.0 or perp_spread_bps < 0.0:
        raise ValueError("spread must be non-negative")
    if not 0.0 <= spot_maker_fill_probability <= 1.0:
        raise ValueError("spot maker probability must be between 0 and 1")
    if not 0.0 <= perp_maker_fill_probability <= 1.0:
        raise ValueError("perp maker probability must be between 0 and 1")

    spot_taker_probability = 1.0 - spot_maker_fill_probability
    perp_taker_probability = 1.0 - perp_maker_fill_probability
    return PairedActionCostBreakdown(
        spot_fee_pct=(
            spot_maker_fill_probability * MAKER_FEE_SPOT
            + spot_taker_probability * TAKER_FEE_SPOT
        ),
        perp_fee_pct=(
            perp_maker_fill_probability * MAKER_FEE_PERP
            + perp_taker_probability * TAKER_FEE_PERP
        ),
        spot_spread_pct=(
            spot_maker_fill_probability * spread_cross_cost_pct(spot_spread_bps, True)
            + spot_taker_probability * spread_cross_cost_pct(spot_spread_bps, False)
        ),
        perp_spread_pct=(
            perp_maker_fill_probability * spread_cross_cost_pct(perp_spread_bps, True)
            + perp_taker_probability * spread_cross_cost_pct(perp_spread_bps, False)
        ),
        spot_impact_pct=(
            spot_taker_probability * liquidity_adjusted_slippage(size_usd, spot_depth_usd)
        ),
        perp_impact_pct=(
            perp_taker_probability * liquidity_adjusted_slippage(size_usd, perp_depth_usd)
        ),
    )


def liquidity_adjusted_slippage(requested_notional: float, depth_usd: float, base_slippage: float | None = None) -> float:
    """Estimate per-leg slippage as a fraction of notional."""
    base = base_slippage if base_slippage is not None else SLIPPAGE_ESTIMATE
    if depth_usd <= 0:
        return base * 5.0
    impact_ratio = max(requested_notional, 0.0) / depth_usd
    return base * math.sqrt(max(impact_ratio, 0.0))


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


def blended_exit_cost(
    notional: float,
    depth_usd: float = 500_000.0,
    spread_bps: float = 0.0,
    maker_fill_probability: float = MAKER_FILL_PROBABILITY,
) -> float:
    return blended_entry_cost(
        notional,
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
