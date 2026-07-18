"""Incremental expected-value rotation and partial-rebalance policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math


class RotationAction(StrEnum):
    KEEP = "keep"
    PARTIAL_ROTATE = "partial_rotate"
    FULL_ROTATE = "full_rotate"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class RotationInputs:
    current_symbol: str
    candidate_symbol: str
    current_notional_usd: float
    current_remaining_lcb_usd: float
    candidate_lcb_usd_at_current_size: float
    current_close_cost_usd: float
    candidate_open_cost_usd: float
    transition_loss_usd: float
    candidate_executable_capacity_usd: float
    candidate_confidence: float
    held_hours: float
    minimum_hold_hours: float = 8.0
    minimum_incremental_value_usd: float = 2.0
    hysteresis_usd: float = 1.0
    max_payback_hours: float = 8.0
    candidate_net_earning_rate_usd_per_hour: float = 0.0
    seconds_to_current_settlement: float = math.inf
    current_settlement_lower_payment_usd: float = 0.0
    cooldown_active: bool = False
    pending_transition: bool = False
    previous_recommendation: RotationAction = RotationAction.KEEP
    max_partial_fraction: float = 0.50


@dataclass(frozen=True, slots=True)
class RotationDecision:
    action: RotationAction
    rotate_notional_usd: float
    incremental_value_usd: float
    payback_hours: float
    reason_codes: tuple[str, ...]
    explanation: str


class IncrementalRotationPolicy:
    """Compare keep versus switch value after every transition cost."""

    def decide(self, inputs: RotationInputs) -> RotationDecision:
        reasons: list[str] = []
        numeric_nonnegative = {
            "current_notional": inputs.current_notional_usd,
            "current_close_cost": inputs.current_close_cost_usd,
            "candidate_open_cost": inputs.candidate_open_cost_usd,
            "transition_loss": inputs.transition_loss_usd,
            "candidate_capacity": inputs.candidate_executable_capacity_usd,
            "candidate_confidence": inputs.candidate_confidence,
            "held_hours": inputs.held_hours,
            "minimum_hold_hours": inputs.minimum_hold_hours,
            "minimum_incremental_value": inputs.minimum_incremental_value_usd,
            "hysteresis": inputs.hysteresis_usd,
            "max_payback_hours": inputs.max_payback_hours,
        }
        for name, value in numeric_nonnegative.items():
            if not math.isfinite(float(value)) or float(value) < 0.0:
                reasons.append(f"invalid_{name}")
        if not inputs.current_symbol.strip() or not inputs.candidate_symbol.strip():
            reasons.append("missing_symbol")
        if inputs.current_symbol.upper() == inputs.candidate_symbol.upper():
            reasons.append("same_symbol")
        if not 0.0 <= inputs.candidate_confidence <= 1.0:
            reasons.append("invalid_candidate_confidence")
        if not 0.0 < inputs.max_partial_fraction <= 1.0:
            reasons.append("invalid_partial_fraction")
        if inputs.cooldown_active:
            reasons.append("cooldown_active")
        if inputs.pending_transition:
            reasons.append("transition_already_pending")

        transition_cost = (
            max(0.0, inputs.current_close_cost_usd)
            + max(0.0, inputs.candidate_open_cost_usd)
            + max(0.0, inputs.transition_loss_usd)
        )
        incremental = (
            inputs.candidate_lcb_usd_at_current_size
            - inputs.current_remaining_lcb_usd
            - transition_cost
        )
        near_settlement = (
            math.isfinite(inputs.seconds_to_current_settlement)
            and inputs.seconds_to_current_settlement <= 15 * 60
            and inputs.current_settlement_lower_payment_usd > 0.0
        )
        if near_settlement:
            incremental -= inputs.current_settlement_lower_payment_usd
            reasons.append("protect_imminent_current_settlement")
        if inputs.held_hours < inputs.minimum_hold_hours:
            reasons.append("minimum_hold_not_met")

        earning_rate = max(0.0, inputs.candidate_net_earning_rate_usd_per_hour)
        payback = transition_cost / earning_rate if earning_rate > 0.0 else math.inf
        if payback > inputs.max_payback_hours:
            reasons.append("payback_too_slow")

        required_value = inputs.minimum_incremental_value_usd + inputs.hysteresis_usd
        if inputs.previous_recommendation is not RotationAction.KEEP:
            # A previously recommended switch must deteriorate beyond the
            # hysteresis band before flipping back, suppressing one-cycle noise.
            required_value = max(0.0, inputs.minimum_incremental_value_usd - inputs.hysteresis_usd)
        if incremental < required_value:
            reasons.append("incremental_value_below_hysteresis")

        blockers = {
            reason
            for reason in reasons
            if reason
            in {
                "missing_symbol",
                "same_symbol",
                "invalid_current_notional",
                "invalid_candidate_capacity",
                "invalid_candidate_confidence",
                "invalid_partial_fraction",
                "cooldown_active",
                "transition_already_pending",
                "minimum_hold_not_met",
                "payback_too_slow",
                "protect_imminent_current_settlement",
                "incremental_value_below_hysteresis",
            }
            or reason.startswith("invalid_")
        }
        if blockers:
            action = RotationAction.BLOCKED if any(
                reason.startswith("invalid_") or reason in {"missing_symbol", "transition_already_pending"}
                for reason in blockers
            ) else RotationAction.KEEP
            return RotationDecision(
                action=action,
                rotate_notional_usd=0.0,
                incremental_value_usd=incremental,
                payback_hours=payback,
                reason_codes=tuple(dict.fromkeys(reasons)),
                explanation=(
                    f"keep {inputs.current_symbol}: incremental ${incremental:.2f}, "
                    f"payback {payback:.2f}h; {','.join(dict.fromkeys(reasons))}"
                ),
            )

        maximum = min(inputs.current_notional_usd, inputs.candidate_executable_capacity_usd)
        if maximum <= 0.0:
            return RotationDecision(
                RotationAction.BLOCKED,
                0.0,
                incremental,
                payback,
                ("no_executable_capacity",),
                "candidate has no executable capacity",
            )
        fraction = min(
            1.0,
            max(
                0.0,
                (incremental - inputs.minimum_incremental_value_usd)
                / max(abs(inputs.current_remaining_lcb_usd), transition_cost, 1.0),
            ),
        )
        if maximum + 1e-9 < inputs.current_notional_usd or fraction < 0.90:
            fraction = min(inputs.max_partial_fraction, max(0.10, fraction))
            rotate_notional = maximum * fraction
            action = RotationAction.PARTIAL_ROTATE
        else:
            rotate_notional = maximum
            action = RotationAction.FULL_ROTATE
        return RotationDecision(
            action=action,
            rotate_notional_usd=rotate_notional,
            incremental_value_usd=incremental,
            payback_hours=payback,
            reason_codes=("positive_incremental_net_ev",),
            explanation=(
                f"{action.value} ${rotate_notional:.2f} from {inputs.current_symbol} "
                f"to {inputs.candidate_symbol}: incremental ${incremental:.2f}, "
                f"payback {payback:.2f}h"
            ),
        )
