"""Direction-aware hold/exit valuation for open funding-arbitrage pairs."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math
from typing import Literal


Direction = Literal["long_spot_short_perp", "short_spot_long_perp"]


class HoldExitAction(StrEnum):
    HOLD = "hold"
    CONTROLLED_EXIT = "controlled_exit"
    EMERGENCY_EXIT = "emergency_exit"
    MANUAL_REVIEW = "manual_review"


@dataclass(frozen=True, slots=True)
class HoldExitInputs:
    symbol: str
    direction: Direction
    notional_usd: float
    expected_future_funding_usd: float
    lower_future_funding_usd: float
    current_basis_pct: float
    expected_exit_basis_pct: float
    exit_cost_usd: float
    borrow_cost_usd: float = 0.0
    basis_tail_risk_usd: float = 0.0
    seconds_to_settlement: float = math.inf
    settlement_survival_probability: float = 1.0
    imminent_settlement_payment_usd: float = 0.0
    forecast_favourable_probability: float = 0.5
    risk_urgency: float = 0.0
    hedge_mismatch_usd: float = 0.0
    maximum_hedge_mismatch_usd: float = 0.0
    data_fresh: bool = True
    exit_executable: bool = True
    entry_blocked: bool = False
    minimum_hold_advantage_usd: float = 0.0


@dataclass(frozen=True, slots=True)
class HoldExitDecision:
    action: HoldExitAction
    hold_value_usd: float
    exit_value_usd: float
    incremental_hold_value_usd: float
    urgency: float
    reason_codes: tuple[str, ...]
    explanation: str


class DirectionAwareHoldExitPolicy:
    def decide(self, inputs: HoldExitInputs) -> HoldExitDecision:
        reasons: list[str] = []
        if not inputs.symbol.strip():
            reasons.append("missing_symbol")
        if inputs.direction not in ("long_spot_short_perp", "short_spot_long_perp"):
            reasons.append("unknown_direction")
        for name, value in (
            ("notional", inputs.notional_usd),
            ("exit_cost", inputs.exit_cost_usd),
            ("borrow_cost", inputs.borrow_cost_usd),
            ("basis_tail_risk", inputs.basis_tail_risk_usd),
            ("hedge_mismatch", inputs.hedge_mismatch_usd),
            ("maximum_hedge_mismatch", inputs.maximum_hedge_mismatch_usd),
        ):
            if not math.isfinite(float(value)) or float(value) < 0.0:
                reasons.append(f"invalid_{name}")
        if not 0.0 <= inputs.settlement_survival_probability <= 1.0:
            reasons.append("invalid_settlement_survival_probability")
        if not 0.0 <= inputs.forecast_favourable_probability <= 1.0:
            reasons.append("invalid_forecast_probability")

        direction_sign = 1.0 if inputs.direction == "long_spot_short_perp" else -1.0
        # Closing a long-spot/short-perp pair benefits if positive basis
        # converges downward; the inverse pair benefits if it converges upward.
        basis_convergence_usd = (
            direction_sign
            * (inputs.current_basis_pct - inputs.expected_exit_basis_pct)
            * inputs.notional_usd
        )
        survival_payment = (
            inputs.imminent_settlement_payment_usd
            * inputs.settlement_survival_probability
        )
        hold_value = (
            inputs.lower_future_funding_usd
            + basis_convergence_usd
            + survival_payment
            - inputs.borrow_cost_usd
            - inputs.basis_tail_risk_usd
            - inputs.exit_cost_usd
        )
        exit_value = -inputs.exit_cost_usd
        incremental = hold_value - exit_value

        urgency = min(1.0, max(0.0, inputs.risk_urgency))
        if inputs.maximum_hedge_mismatch_usd > 0.0:
            urgency = max(
                urgency,
                min(1.0, inputs.hedge_mismatch_usd / inputs.maximum_hedge_mismatch_usd),
            )
        if inputs.forecast_favourable_probability < 0.35:
            urgency = max(urgency, 0.60)
            reasons.append("funding_reversal_likely")
        if not inputs.data_fresh:
            urgency = max(urgency, 0.75)
            reasons.append("stale_decision_inputs")
        if inputs.entry_blocked:
            # Entry locks are recorded but intentionally never block a
            # beneficial/risk-reducing exit.
            reasons.append("entry_lock_ignored_for_exit")

        invalid = any(reason.startswith("invalid_") or reason in {"missing_symbol", "unknown_direction"} for reason in reasons)
        if invalid:
            action = HoldExitAction.MANUAL_REVIEW
        elif urgency >= 0.95:
            action = HoldExitAction.EMERGENCY_EXIT
            reasons.append("risk_urgency_emergency")
        elif not inputs.exit_executable:
            action = HoldExitAction.MANUAL_REVIEW
            reasons.append("exit_not_executable")
        elif incremental < inputs.minimum_hold_advantage_usd or urgency >= 0.60:
            action = HoldExitAction.CONTROLLED_EXIT
            reasons.append("exit_lcb_dominates_hold")
        else:
            action = HoldExitAction.HOLD
            reasons.append("hold_lcb_dominates_exit")

        return HoldExitDecision(
            action=action,
            hold_value_usd=hold_value,
            exit_value_usd=exit_value,
            incremental_hold_value_usd=incremental,
            urgency=urgency,
            reason_codes=tuple(dict.fromkeys(reasons)),
            explanation=(
                f"{inputs.symbol} {action.value}: hold ${hold_value:.2f}, "
                f"exit ${exit_value:.2f}, delta ${incremental:.2f}, urgency {urgency:.2f}"
            ),
        )
