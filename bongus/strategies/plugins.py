"""Isolated shadow strategy plugins with explicit per-strategy risk budgets."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import hashlib
import json
import math
from typing import Protocol, Sequence

from bongus.market_data.settlement_model import SettlementForecast


class PluginAction(StrEnum):
    OBSERVE = "observe"
    ENTER = "enter"
    EXIT = "exit"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class StrategyRiskBudget:
    strategy_id: str
    max_gross_notional_usd: float
    max_position_notional_usd: float
    max_expected_loss_usd: float
    max_cvar_usd: float
    max_concurrent_positions: int = 1
    allowed_venues: tuple[str, ...] = ("binance",)

    def __post_init__(self) -> None:
        if not self.strategy_id.strip():
            raise ValueError("strategy_id is required")
        numeric = (
            self.max_gross_notional_usd,
            self.max_position_notional_usd,
            self.max_expected_loss_usd,
            self.max_cvar_usd,
        )
        if any(not math.isfinite(value) or value <= 0.0 for value in numeric):
            raise ValueError("strategy risk limits must be finite and positive")
        if self.max_concurrent_positions <= 0 or not self.allowed_venues:
            raise ValueError("strategy requires position and venue limits")


@dataclass(frozen=True, slots=True)
class StrategyContext:
    decision_time: datetime
    symbol: str
    venue: str
    requested_notional_usd: float
    executable_capacity_usd: float
    entry_exit_cost_usd: float
    basis_pct: float
    expected_exit_basis_pct: float
    basis_cvar_usd: float
    seconds_to_settlement: float
    settlement_forecast: SettlementForecast | None
    config_hash: str
    model_hash: str


@dataclass(frozen=True, slots=True)
class StrategyProposal:
    proposal_id: str
    strategy_id: str
    symbol: str
    action: PluginAction
    direction: str
    target_notional_usd: float
    expected_net_value_usd: float
    lower_bound_net_value_usd: float
    expected_loss_usd: float
    cvar_usd: float
    shadow_only: bool
    reason_codes: tuple[str, ...]
    expires_at: str
    metadata: dict[str, float | int | str | bool] = field(default_factory=dict)


class StrategyPlugin(Protocol):
    strategy_id: str

    def evaluate(
        self,
        context: StrategyContext,
        budget: StrategyRiskBudget,
    ) -> StrategyProposal:
        ...


def _proposal(
    *,
    context: StrategyContext,
    strategy_id: str,
    action: PluginAction,
    direction: str,
    target_notional_usd: float,
    expected_net_value_usd: float,
    lower_bound_net_value_usd: float,
    expected_loss_usd: float,
    cvar_usd: float,
    reasons: Sequence[str],
    expires_at: datetime,
    metadata: dict[str, float | int | str | bool] | None = None,
) -> StrategyProposal:
    payload = {
        "strategy_id": strategy_id,
        "symbol": context.symbol.upper(),
        "decision_time": context.decision_time.astimezone(timezone.utc).isoformat(),
        "action": action.value,
        "direction": direction,
        "target": target_notional_usd,
        "config_hash": context.config_hash,
        "model_hash": context.model_hash,
    }
    proposal_id = "plugin-" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:24]
    return StrategyProposal(
        proposal_id,
        strategy_id,
        context.symbol.upper(),
        action,
        direction,
        target_notional_usd,
        expected_net_value_usd,
        lower_bound_net_value_usd,
        expected_loss_usd,
        cvar_usd,
        True,
        tuple(dict.fromkeys(reasons)),
        expires_at.astimezone(timezone.utc).isoformat(),
        metadata or {},
    )


class FundingCalendarOptimizationPlugin:
    strategy_id = "funding-calendar-v1"

    def __init__(
        self,
        *,
        minimum_eligibility_seconds: float = 120.0,
        maximum_lead_seconds: float = 3_600.0,
        minimum_lcb_usd: float = 1.0,
    ) -> None:
        self.minimum_eligibility_seconds = minimum_eligibility_seconds
        self.maximum_lead_seconds = maximum_lead_seconds
        self.minimum_lcb_usd = minimum_lcb_usd

    def evaluate(
        self, context: StrategyContext, budget: StrategyRiskBudget
    ) -> StrategyProposal:
        reasons: list[str] = []
        if budget.strategy_id != self.strategy_id:
            reasons.append("risk_budget_strategy_mismatch")
        if context.venue.lower() not in budget.allowed_venues:
            reasons.append("venue_not_allowed")
        if context.settlement_forecast is None or not context.settlement_forecast.valid:
            reasons.append("settlement_forecast_unavailable")
            expected_payment = 0.0
            lower_payment = 0.0
        else:
            expected_payment = context.settlement_forecast.expected_payment_usd
            lower_payment = context.settlement_forecast.lower_payment_usd
        if context.seconds_to_settlement < self.minimum_eligibility_seconds:
            reasons.append("insufficient_settlement_eligibility_buffer")
        if context.seconds_to_settlement > self.maximum_lead_seconds:
            reasons.append("too_early_for_calendar_window")
        target = min(
            max(0.0, context.requested_notional_usd),
            max(0.0, context.executable_capacity_usd) * 0.80,
            budget.max_position_notional_usd,
            budget.max_gross_notional_usd,
        )
        scale = target / max(context.requested_notional_usd, 1e-9)
        expected_net = expected_payment * scale - context.entry_exit_cost_usd
        lower_net = lower_payment * scale - context.entry_exit_cost_usd
        expected_loss = max(0.0, -lower_net)
        if target <= 0.0:
            reasons.append("no_executable_capacity")
        if lower_net < self.minimum_lcb_usd:
            reasons.append("calendar_net_lcb_below_threshold")
        if expected_loss > budget.max_expected_loss_usd:
            reasons.append("expected_loss_budget")
        action = PluginAction.ENTER if not reasons else PluginAction.REJECT
        return _proposal(
            context=context,
            strategy_id=self.strategy_id,
            action=action,
            direction=(
                context.settlement_forecast.direction
                if context.settlement_forecast is not None
                else ""
            ),
            target_notional_usd=target if not reasons else 0.0,
            expected_net_value_usd=expected_net,
            lower_bound_net_value_usd=lower_net,
            expected_loss_usd=expected_loss,
            cvar_usd=max(expected_loss, context.basis_cvar_usd),
            reasons=reasons or ("calendar_lcb_positive",),
            expires_at=context.decision_time
            + timedelta(seconds=max(0.0, context.seconds_to_settlement)),
            metadata={"seconds_to_settlement": context.seconds_to_settlement},
        )


class BasisConvergencePlugin:
    strategy_id = "basis-convergence-v1"

    def __init__(
        self,
        *,
        minimum_basis_abs_pct: float = 0.002,
        minimum_lcb_usd: float = 1.0,
        confidence_haircut: float = 0.50,
        maximum_holding_hours: float = 24.0,
    ) -> None:
        self.minimum_basis_abs_pct = minimum_basis_abs_pct
        self.minimum_lcb_usd = minimum_lcb_usd
        self.confidence_haircut = confidence_haircut
        self.maximum_holding_hours = maximum_holding_hours

    def evaluate(
        self, context: StrategyContext, budget: StrategyRiskBudget
    ) -> StrategyProposal:
        reasons: list[str] = []
        if budget.strategy_id != self.strategy_id:
            reasons.append("risk_budget_strategy_mismatch")
        if context.venue.lower() not in budget.allowed_venues:
            reasons.append("venue_not_allowed")
        target = min(
            max(0.0, context.requested_notional_usd),
            max(0.0, context.executable_capacity_usd) * 0.50,
            budget.max_position_notional_usd,
            budget.max_gross_notional_usd,
        )
        if abs(context.basis_pct) < self.minimum_basis_abs_pct:
            reasons.append("basis_below_threshold")
        direction = (
            "long_spot_short_perp"
            if context.basis_pct > context.expected_exit_basis_pct
            else "short_spot_long_perp"
        )
        expected_convergence = abs(context.basis_pct - context.expected_exit_basis_pct) * target
        expected_net = expected_convergence - context.entry_exit_cost_usd
        lower_net = (
            expected_convergence * self.confidence_haircut
            - context.entry_exit_cost_usd
            - context.basis_cvar_usd
        )
        expected_loss = max(0.0, context.basis_cvar_usd + context.entry_exit_cost_usd)
        if target <= 0.0:
            reasons.append("no_executable_capacity")
        if lower_net < self.minimum_lcb_usd:
            reasons.append("basis_net_lcb_below_threshold")
        if expected_loss > budget.max_expected_loss_usd:
            reasons.append("expected_loss_budget")
        if context.basis_cvar_usd > budget.max_cvar_usd:
            reasons.append("basis_cvar_budget")
        action = PluginAction.ENTER if not reasons else PluginAction.REJECT
        return _proposal(
            context=context,
            strategy_id=self.strategy_id,
            action=action,
            direction=direction,
            target_notional_usd=target if not reasons else 0.0,
            expected_net_value_usd=expected_net,
            lower_bound_net_value_usd=lower_net,
            expected_loss_usd=expected_loss,
            cvar_usd=context.basis_cvar_usd,
            reasons=reasons or ("basis_convergence_lcb_positive",),
            expires_at=context.decision_time
            + timedelta(hours=self.maximum_holding_hours),
            metadata={
                "entry_basis_pct": context.basis_pct,
                "expected_exit_basis_pct": context.expected_exit_basis_pct,
            },
        )


class StrategyPluginRegistry:
    def __init__(self) -> None:
        self._plugins: dict[str, StrategyPlugin] = {}
        self._budgets: dict[str, StrategyRiskBudget] = {}

    def register(self, plugin: StrategyPlugin, budget: StrategyRiskBudget) -> None:
        strategy_id = plugin.strategy_id.strip()
        if strategy_id in self._plugins:
            raise ValueError("strategy plugin id is already registered")
        if budget.strategy_id != strategy_id:
            raise ValueError("strategy plugin and risk budget ids must match")
        self._plugins[strategy_id] = plugin
        self._budgets[strategy_id] = budget

    def evaluate(
        self,
        contexts: Sequence[StrategyContext],
        *,
        current_gross_by_strategy: dict[str, float] | None = None,
        current_positions_by_strategy: dict[str, int] | None = None,
    ) -> tuple[StrategyProposal, ...]:
        gross = dict(current_gross_by_strategy or {})
        positions = dict(current_positions_by_strategy or {})
        proposals: list[StrategyProposal] = []
        for strategy_id, plugin in sorted(self._plugins.items()):
            budget = self._budgets[strategy_id]
            for context in contexts:
                proposal = plugin.evaluate(context, budget)
                reasons = list(proposal.reason_codes)
                if proposal.action is PluginAction.ENTER:
                    if (
                        gross.get(strategy_id, 0.0) + proposal.target_notional_usd
                        > budget.max_gross_notional_usd
                    ):
                        reasons.append("strategy_gross_budget")
                    if positions.get(strategy_id, 0) >= budget.max_concurrent_positions:
                        reasons.append("strategy_position_count_budget")
                if reasons != list(proposal.reason_codes):
                    proposal = replace(
                        proposal,
                        action=PluginAction.REJECT,
                        target_notional_usd=0.0,
                        reason_codes=tuple(dict.fromkeys(reasons)),
                    )
                elif proposal.action is PluginAction.ENTER:
                    gross[strategy_id] = (
                        gross.get(strategy_id, 0.0) + proposal.target_notional_usd
                    )
                    positions[strategy_id] = positions.get(strategy_id, 0) + 1
                proposals.append(proposal)
        return tuple(proposals)
