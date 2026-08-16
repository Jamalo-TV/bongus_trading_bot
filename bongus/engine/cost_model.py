"""Shared fee, slippage, and edge-estimation model for Bongus."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from bongus.market_data.depth_tracker import ExecutablePairCapacity


@dataclass(slots=True)
class CostContext:
    """Legacy symmetric-leg cost context.

    ``spread_bps`` is one representative *per-leg* spread. It must not be a
    combined spot-plus-perpetual metric.
    """

    size_usd: float = NOTIONAL_PER_TRADE
    depth_usd: float = 500_000.0
    spread_bps: float = 0.0
    maker_fill_probability: float = MAKER_FILL_PROBABILITY
    holding_hours: float = DEFAULT_HOLDING_HOURS


DecimalInput = Decimal | int | float | str


def _non_negative_decimal(value: DecimalInput, *, field_name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a finite non-negative number")
    try:
        normalized = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite non-negative number") from exc
    if not normalized.is_finite() or normalized < 0:
        raise ValueError(f"{field_name} must be a finite non-negative number")
    return normalized


def unhedged_notional_milliseconds(
    *,
    spot_gross_quantity: DecimalInput | None = None,
    perp_gross_quantity: DecimalInput | None = None,
    spot_quantity: DecimalInput | None = None,
    perp_quantity: DecimalInput | None = None,
    reference_price: DecimalInput,
    elapsed_milliseconds: DecimalInput,
) -> Decimal:
    """Integrate the observed pair imbalance over one exact time interval.

    ``spot_quantity``/``perp_quantity`` are compatibility aliases. The gross
    names make the economic meaning explicit for normalized TCA evidence.
    """

    def resolve_quantity(
        gross: DecimalInput | None,
        compatibility: DecimalInput | None,
        *,
        field_name: str,
    ) -> Decimal:
        if gross is None and compatibility is None:
            raise TypeError(f"{field_name} is required")
        if gross is None:
            assert compatibility is not None
            return _non_negative_decimal(compatibility, field_name=field_name)
        normalized = _non_negative_decimal(gross, field_name=field_name)
        if compatibility is not None and normalized != _non_negative_decimal(
            compatibility,
            field_name=field_name,
        ):
            raise ValueError(f"conflicting {field_name} aliases")
        return normalized

    spot = resolve_quantity(
        spot_gross_quantity,
        spot_quantity,
        field_name="spot_gross_quantity",
    )
    perp = resolve_quantity(
        perp_gross_quantity,
        perp_quantity,
        field_name="perp_gross_quantity",
    )
    price = _non_negative_decimal(reference_price, field_name="reference_price")
    elapsed = _non_negative_decimal(
        elapsed_milliseconds, field_name="elapsed_milliseconds"
    )
    return abs(spot - perp) * price * elapsed


def exact_adverse_markout_bps(
    *,
    side: str,
    fill_price: DecimalInput,
    future_mid: DecimalInput,
) -> Decimal:
    """Return signed adverse selection in bps using Decimal arithmetic."""

    fill = _non_negative_decimal(fill_price, field_name="fill_price")
    mark = _non_negative_decimal(future_mid, field_name="future_mid")
    if fill <= 0 or mark <= 0:
        raise ValueError("fill_price and future_mid must be positive")
    normalized_side = str(side or "").strip().upper()
    if normalized_side == "BUY":
        return (fill - mark) / fill * Decimal("10000")
    if normalized_side == "SELL":
        return (mark - fill) / fill * Decimal("10000")
    raise ValueError("side must be BUY or SELL")


@dataclass(frozen=True, slots=True)
class PerLegSpreadBps:
    """Executable top-of-book spreads, never a combined pair metric.

    Decimal storage keeps the spot/perpetual attribution exact until the
    existing float-based fee and impact model is invoked.
    """

    spot_bps: Decimal
    perp_bps: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "spot_bps",
            _non_negative_decimal(self.spot_bps, field_name="spot_bps"),
        )
        object.__setattr__(
            self,
            "perp_bps",
            _non_negative_decimal(self.perp_bps, field_name="perp_bps"),
        )

    @classmethod
    def from_values(
        cls,
        *,
        spot_bps: DecimalInput,
        perp_bps: DecimalInput,
    ) -> "PerLegSpreadBps":
        return cls(
            spot_bps=_non_negative_decimal(spot_bps, field_name="spot_bps"),
            perp_bps=_non_negative_decimal(perp_bps, field_name="perp_bps"),
        )

    @classmethod
    def from_combined_evenly(cls, combined_bps: DecimalInput) -> "PerLegSpreadBps":
        """Conserve an aggregate spread when only legacy evidence exists.

        This compatibility conversion deliberately splits the aggregate in
        half. Passing the aggregate unchanged for both legs would double it.
        New executable paths should always use :meth:`from_values` instead.
        """

        combined = _non_negative_decimal(combined_bps, field_name="combined_bps")
        half = combined / Decimal(2)
        return cls(spot_bps=half, perp_bps=half)

    @property
    def combined_bps(self) -> Decimal:
        """Return the additive pair metric used by scanner gates and logs."""

        return self.spot_bps + self.perp_bps


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

    Both modeled and exact-book builders use this additive schema.  Legacy
    aggregate cost functions remain available for compatibility.
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


@dataclass(frozen=True, slots=True)
class PairedRoundTripCostBreakdown:
    """Entry and exit attribution for the four executable friction legs."""

    entry: PairedActionCostBreakdown
    exit: PairedActionCostBreakdown

    @property
    def total_pct(self) -> float:
        return self.entry.total_pct + self.exit.total_pct

    @property
    def total_spread_pct(self) -> float:
        return (
            self.entry.spot_spread_pct
            + self.entry.perp_spread_pct
            + self.exit.spot_spread_pct
            + self.exit.perp_spread_pct
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


def paired_round_trip_cost_breakdown(
    *,
    size_usd: float = NOTIONAL_PER_TRADE,
    entry_spreads: PerLegSpreadBps,
    exit_spreads: PerLegSpreadBps,
    entry_spot_depth_usd: float = 500_000.0,
    entry_perp_depth_usd: float = 500_000.0,
    exit_spot_depth_usd: float = 500_000.0,
    exit_perp_depth_usd: float = 500_000.0,
    entry_spot_maker_fill_probability: float = MAKER_FILL_PROBABILITY,
    entry_perp_maker_fill_probability: float = MAKER_FILL_PROBABILITY,
    exit_spot_maker_fill_probability: float = MAKER_FILL_PROBABILITY,
    exit_perp_maker_fill_probability: float = MAKER_FILL_PROBABILITY,
) -> PairedRoundTripCostBreakdown:
    """Cost one paired entry and exit with four explicit leg spreads.

    Requiring :class:`PerLegSpreadBps` for each action prevents a combined
    spot-plus-perpetual scanner metric from being interpreted as both legs.
    """

    entry = paired_action_cost_breakdown(
        size_usd=size_usd,
        spot_depth_usd=entry_spot_depth_usd,
        perp_depth_usd=entry_perp_depth_usd,
        spot_spread_bps=float(entry_spreads.spot_bps),
        perp_spread_bps=float(entry_spreads.perp_bps),
        spot_maker_fill_probability=entry_spot_maker_fill_probability,
        perp_maker_fill_probability=entry_perp_maker_fill_probability,
    )
    exit_cost = paired_action_cost_breakdown(
        size_usd=size_usd,
        spot_depth_usd=exit_spot_depth_usd,
        perp_depth_usd=exit_perp_depth_usd,
        spot_spread_bps=float(exit_spreads.spot_bps),
        perp_spread_bps=float(exit_spreads.perp_bps),
        spot_maker_fill_probability=exit_spot_maker_fill_probability,
        perp_maker_fill_probability=exit_perp_maker_fill_probability,
    )
    return PairedRoundTripCostBreakdown(entry=entry, exit=exit_cost)


def paired_exact_book_cost_breakdown(
    pair_capacity: "ExecutablePairCapacity",
    *,
    spot_spread_bps: float,
    perp_spread_bps: float,
    spot_fee_pct: float = TAKER_FEE_SPOT,
    perp_fee_pct: float = TAKER_FEE_PERP,
) -> PairedActionCostBreakdown:
    """Cost one taker pair from independently walked executable books.

    ``ExecutablePairCapacity`` already contains the exact VWAP impact of each
    leg relative to its own best executable quote.  The helper therefore adds
    each leg's half-spread and fee exactly once and does not invoke the legacy
    square-root depth approximation.
    """

    values = {
        "spot_spread_bps": spot_spread_bps,
        "perp_spread_bps": perp_spread_bps,
        "spot_fee_pct": spot_fee_pct,
        "perp_fee_pct": perp_fee_pct,
        "spot_impact_bps": pair_capacity.spot.impact_bps,
        "perp_impact_bps": pair_capacity.perp.impact_bps,
    }
    if any(not math.isfinite(float(value)) for value in values.values()):
        raise ValueError("exact paired-book costs must be finite")
    if any(float(value) < 0.0 for value in values.values()):
        raise ValueError("exact paired-book costs must be non-negative")
    if not pair_capacity.fully_executable:
        raise ValueError("exact paired-book cost requires both legs to be executable")
    if pair_capacity.executable_notional_usd <= 0.0:
        raise ValueError("exact paired-book cost requires positive executable notional")

    return PairedActionCostBreakdown(
        spot_fee_pct=float(spot_fee_pct),
        perp_fee_pct=float(perp_fee_pct),
        spot_spread_pct=spread_cross_cost_pct(float(spot_spread_bps), False),
        perp_spread_pct=spread_cross_cost_pct(float(perp_spread_bps), False),
        spot_impact_pct=float(pair_capacity.spot.impact_bps) / 10_000.0,
        perp_impact_pct=float(pair_capacity.perp.impact_bps) / 10_000.0,
    )


def liquidity_adjusted_slippage(
    requested_notional: float,
    depth_usd: float,
    base_slippage: float | None = None,
) -> float:
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
    """Legacy symmetric-leg action cost.

    ``spread_bps`` is applied independently to both legs and therefore must be
    a representative per-leg spread, never an already-combined pair spread.
    """

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
    """Blend maker/taker cost using one representative per-leg spread.

    Prefer :func:`paired_action_cost_breakdown` when spot and perpetual
    spreads are independently observable.
    """

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


def quality_score_from_slippage(
    realized_slippage_bps: float,
    target_bps: float = EXECUTION_QUALITY_TARGET_SLIPPAGE_BPS,
) -> float:
    if target_bps <= 0:
        return 0.0
    return max(0.0, 1.0 - max(0.0, realized_slippage_bps) / target_bps)
