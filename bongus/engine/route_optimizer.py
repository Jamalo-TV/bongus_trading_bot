"""Conservative route selection for two-leg hedge execution.

The optimizer is intentionally deterministic and unit-explicit.  It compares
implementation shortfall, missed-settlement value and unhedged notional-time;
live activation remains separately governed, so this module can run in shadow
without changing order flow.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math


class RoutePolicy(StrEnum):
    POST_ONLY_DUAL = "post_only_dual"
    MAKER_LEAD_IOC = "maker_lead_ioc"
    SIMULTANEOUS_IOC = "simultaneous_ioc"
    SLICED_IOC = "sliced_ioc"
    EMERGENCY_REDUCE_ONLY = "emergency_reduce_only"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class RouteInputs:
    symbol: str
    notional_usd: float
    spot_spread_bps: float
    perp_spread_bps: float
    spot_depth_usd: float
    perp_depth_usd: float
    book_age_ms: int
    filters_ready: bool
    is_exit: bool = False
    emergency: bool = False
    urgency: float = 0.0
    seconds_to_settlement: float = math.inf
    settlement_value_bps: float = 0.0
    volatility_bps_per_second: float = 0.0
    adverse_markout_bps: float = 0.0
    spot_maker_fill_probability: float = 0.5
    perp_maker_fill_probability: float = 0.5
    maker_fee_bps: float = 2.0
    taker_fee_bps: float = 5.0
    impact_bps: float = 0.0
    expected_ack_latency_ms: int = 100
    maker_timeout_ms: int = 1_500
    max_book_age_ms: int = 1_000
    max_unhedged_notional_ms: float = 5_000_000.0
    max_slices: int = 4


@dataclass(frozen=True, slots=True)
class RouteEstimate:
    policy: RoutePolicy
    feasible: bool
    expected_cost_bps: float
    missed_settlement_bps: float
    total_objective_bps: float
    hedge_risk_notional_ms: float
    expected_completion_ms: int
    slices: int
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RouteRecommendation:
    symbol: str
    selected: RoutePolicy
    estimates: tuple[RouteEstimate, ...]
    reason: str

    @property
    def selected_estimate(self) -> RouteEstimate | None:
        return next((item for item in self.estimates if item.policy is self.selected), None)


def _bounded_probability(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


class RouteOptimizer:
    """Score the supported route policies under a hard hedge-risk budget."""

    @staticmethod
    def _validate(inputs: RouteInputs) -> tuple[str, ...]:
        reasons: list[str] = []
        if not inputs.symbol.strip():
            reasons.append("missing_symbol")
        for name, value in (
            ("notional_usd", inputs.notional_usd),
            ("spot_spread_bps", inputs.spot_spread_bps),
            ("perp_spread_bps", inputs.perp_spread_bps),
            ("spot_depth_usd", inputs.spot_depth_usd),
            ("perp_depth_usd", inputs.perp_depth_usd),
            ("volatility_bps_per_second", inputs.volatility_bps_per_second),
            ("adverse_markout_bps", inputs.adverse_markout_bps),
            ("impact_bps", inputs.impact_bps),
        ):
            if not math.isfinite(float(value)) or float(value) < 0.0:
                reasons.append(f"invalid_{name}")
        if inputs.notional_usd <= 0.0:
            reasons.append("non_positive_notional")
        if inputs.book_age_ms < 0 or inputs.book_age_ms > inputs.max_book_age_ms:
            reasons.append("stale_book")
        if not inputs.filters_ready:
            reasons.append("filters_unavailable")
        return tuple(dict.fromkeys(reasons))

    @staticmethod
    def _missed_settlement(inputs: RouteInputs, completion_ms: int) -> float:
        if not math.isfinite(inputs.seconds_to_settlement):
            return 0.0
        seconds = max(0.0, inputs.seconds_to_settlement)
        if completion_ms <= seconds * 1_000.0:
            return 0.0
        return max(0.0, inputs.settlement_value_bps)

    @staticmethod
    def _capacity_impact(inputs: RouteInputs, slices: int = 1) -> tuple[float, bool]:
        minimum_depth = min(inputs.spot_depth_usd, inputs.perp_depth_usd)
        child_notional = inputs.notional_usd / max(1, slices)
        if minimum_depth <= 0.0 or child_notional > minimum_depth:
            return math.inf, False
        participation = child_notional / minimum_depth
        return inputs.impact_bps * math.sqrt(max(0.0, participation)), True

    def estimate(self, inputs: RouteInputs) -> tuple[RouteEstimate, ...]:
        global_reasons = self._validate(inputs)
        urgency = _bounded_probability(inputs.urgency)
        spot_fill = _bounded_probability(inputs.spot_maker_fill_probability)
        perp_fill = _bounded_probability(inputs.perp_maker_fill_probability)
        one_leg_probability = spot_fill * (1.0 - perp_fill) + perp_fill * (1.0 - spot_fill)
        both_fill_probability = spot_fill * perp_fill
        pair_half_spread = max(0.0, inputs.spot_spread_bps + inputs.perp_spread_bps) / 2.0
        cross_cost = 2.0 * inputs.taker_fee_bps + pair_half_spread
        ack_ms = max(1, int(inputs.expected_ack_latency_ms))
        maker_timeout_ms = max(ack_ms, int(inputs.maker_timeout_ms))

        estimates: list[RouteEstimate] = []
        pair_depth_reasons = (
            ("zero_depth",)
            if inputs.spot_depth_usd <= 0.0 or inputs.perp_depth_usd <= 0.0
            else ()
        )

        def append(
            policy: RoutePolicy,
            cost_bps: float,
            completion_ms: int,
            hedge_risk: float,
            *,
            slices: int = 1,
            route_reasons: tuple[str, ...] = (),
            capacity_ok: bool = True,
        ) -> None:
            reasons = list(global_reasons) + list(route_reasons)
            if not capacity_ok:
                reasons.append("insufficient_executable_capacity")
            if hedge_risk > inputs.max_unhedged_notional_ms:
                reasons.append("hedge_risk_budget_exceeded")
            missed = self._missed_settlement(inputs, completion_ms)
            feasible = not reasons and math.isfinite(cost_bps)
            estimates.append(
                RouteEstimate(
                    policy=policy,
                    feasible=feasible,
                    expected_cost_bps=max(0.0, cost_bps) if math.isfinite(cost_bps) else math.inf,
                    missed_settlement_bps=missed,
                    total_objective_bps=(max(0.0, cost_bps) + missed)
                    if math.isfinite(cost_bps)
                    else math.inf,
                    hedge_risk_notional_ms=max(0.0, hedge_risk),
                    expected_completion_ms=completion_ms,
                    slices=slices,
                    reasons=tuple(dict.fromkeys(reasons)),
                )
            )

        impact, capacity_ok = self._capacity_impact(inputs)
        post_completion = maker_timeout_ms + ack_ms
        post_fallback_probability = 1.0 - both_fill_probability
        post_cost = (
            2.0 * inputs.maker_fee_bps
            + inputs.adverse_markout_bps
            + post_fallback_probability * (cross_cost + impact)
            + urgency * post_fallback_probability * inputs.settlement_value_bps
        )
        append(
            RoutePolicy.POST_ONLY_DUAL,
            post_cost,
            post_completion,
            inputs.notional_usd * maker_timeout_ms * one_leg_probability,
            capacity_ok=capacity_ok,
            route_reasons=pair_depth_reasons
            + (("emergency_requires_taker",) if inputs.emergency else ()),
        )

        lead_fill = max(spot_fill, perp_fill)
        lead_completion = maker_timeout_ms + 2 * ack_ms
        lead_cost = (
            inputs.maker_fee_bps
            + inputs.taker_fee_bps
            + pair_half_spread / 2.0
            + inputs.adverse_markout_bps
            + (1.0 - lead_fill) * (cross_cost + impact)
        )
        append(
            RoutePolicy.MAKER_LEAD_IOC,
            lead_cost,
            lead_completion,
            inputs.notional_usd * (maker_timeout_ms + ack_ms) * lead_fill,
            capacity_ok=capacity_ok,
            route_reasons=pair_depth_reasons
            + (("emergency_requires_taker",) if inputs.emergency else ()),
        )

        simultaneous_completion = 2 * ack_ms
        append(
            RoutePolicy.SIMULTANEOUS_IOC,
            cross_cost + impact,
            simultaneous_completion,
            inputs.notional_usd * ack_ms * 0.05,
            capacity_ok=capacity_ok,
            route_reasons=pair_depth_reasons,
        )

        slices = max(2, min(max(2, inputs.max_slices), int(math.ceil(inputs.notional_usd / max(1.0, min(inputs.spot_depth_usd, inputs.perp_depth_usd) * 0.35)))))
        sliced_impact, sliced_capacity_ok = self._capacity_impact(inputs, slices=slices)
        sliced_completion = slices * 2 * ack_ms
        append(
            RoutePolicy.SLICED_IOC,
            cross_cost + sliced_impact + inputs.volatility_bps_per_second * sliced_completion / 1_000.0 * 0.25,
            sliced_completion,
            inputs.notional_usd * ack_ms * 0.05 / math.sqrt(slices),
            slices=slices,
            capacity_ok=sliced_capacity_ok,
            route_reasons=pair_depth_reasons,
        )

        emergency_reasons: list[str] = []
        if not inputs.is_exit:
            emergency_reasons.append("reduce_only_route_for_exit_only")
        if not inputs.emergency:
            emergency_reasons.append("emergency_not_requested")
        # This route deliberately repairs/reduces the perpetual leg first.  A
        # withdrawn spot book must not prevent liquidation-risk reduction, but
        # absent perpetual liquidity still fails closed.
        emergency_capacity_ok = (
            inputs.perp_depth_usd > 0.0
            and inputs.notional_usd <= inputs.perp_depth_usd
        )
        emergency_impact = math.inf
        if inputs.perp_depth_usd > 0.0:
            emergency_impact = inputs.impact_bps * math.sqrt(
                inputs.notional_usd / inputs.perp_depth_usd
            )
        if inputs.perp_depth_usd <= 0.0:
            emergency_reasons.append("zero_perp_depth")
        append(
            RoutePolicy.EMERGENCY_REDUCE_ONLY,
            cross_cost
            + emergency_impact
            + max(5.0, inputs.volatility_bps_per_second),
            ack_ms,
            inputs.notional_usd * ack_ms * 0.02,
            capacity_ok=emergency_capacity_ok,
            route_reasons=tuple(emergency_reasons),
        )
        return tuple(estimates)

    def recommend(self, inputs: RouteInputs) -> RouteRecommendation:
        estimates = self.estimate(inputs)
        feasible = [item for item in estimates if item.feasible]
        if not feasible:
            blockers = sorted({reason for item in estimates for reason in item.reasons})
            return RouteRecommendation(
                symbol=inputs.symbol.upper(),
                selected=RoutePolicy.NONE,
                estimates=estimates,
                reason=",".join(blockers) or "no_feasible_route",
            )
        chosen = min(
            feasible,
            key=lambda item: (
                item.total_objective_bps,
                item.hedge_risk_notional_ms,
                item.expected_completion_ms,
                item.policy.value,
            ),
        )
        return RouteRecommendation(
            symbol=inputs.symbol.upper(),
            selected=chosen.policy,
            estimates=estimates,
            reason="minimum_total_cost_within_hedge_budget",
        )
