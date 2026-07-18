"""Lower-confidence-bound net economic value scoring."""

from __future__ import annotations

from dataclasses import dataclass, field
import math

from bongus.market_data.settlement_model import SettlementForecast
from bongus.strategies.opportunity_adapters import SHADOW_OPPORTUNITY_ADAPTER
from bongus.strategies.opportunity_kernel import (
    OpportunityEvaluationInput,
    SettlementExpectation,
)


@dataclass(frozen=True, slots=True)
class CandidateEconomics:
    symbol: str
    notional_usd: float
    settlement_forecast: SettlementForecast
    entry_cost_bps: float
    exit_cost_bps: float
    funding_liable_notional_usd: float | None = None
    borrow_cost_bps: float = 0.0
    idle_opportunity_cost_bps: float = 0.0
    basis_expected_pnl_bps: float = 0.0
    basis_risk_bps: float = 0.0
    execution_uncertainty_bps: float = 0.0
    liquidation_tail_bps: float = 0.0
    capacity_usd: float = 0.0
    capacity_haircut: float = 0.80
    correlation_penalty_bps: float = 0.0
    model_confidence: float = 0.0
    input_age_seconds: float = 0.0
    max_input_age_seconds: float = 30.0
    active_baseline_net_edge_bps: float | None = None
    metadata: dict[str, float | int | str | bool] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NetEVScore:
    symbol: str
    eligible: bool
    mean_net_ev_usd: float
    lower_bound_net_ev_usd: float
    mean_net_edge_bps: float
    lower_bound_net_edge_bps: float
    executable_notional_usd: float
    uncertainty_usd: float
    reason_codes: tuple[str, ...]
    components_usd: dict[str, float]
    explanation: str


class LowerConfidenceNetEVScorer:
    """Score realizable dollars after costs, capacity and uncertainty.

    The score is a conservative decision statistic rather than a promise of
    profit.  Missing/stale/non-finite inputs fail closed and remain observable.
    """

    def __init__(self, *, confidence_z: float = 1.6448536269514722) -> None:
        if confidence_z < 0.0 or not math.isfinite(confidence_z):
            raise ValueError("confidence_z must be finite and non-negative")
        self.confidence_z = float(confidence_z)

    @staticmethod
    def _finite_nonnegative(name: str, value: float, reasons: list[str]) -> float:
        numeric = float(value)
        if not math.isfinite(numeric) or numeric < 0.0:
            reasons.append(f"invalid_{name}")
            return 0.0
        return numeric

    def score(self, candidate: CandidateEconomics) -> NetEVScore:
        reasons: list[str] = []
        symbol = candidate.symbol.strip().upper()
        if not symbol:
            reasons.append("missing_symbol")
        notional = self._finite_nonnegative("notional", candidate.notional_usd, reasons)
        if notional <= 0.0:
            reasons.append("non_positive_notional")
        capacity = self._finite_nonnegative("capacity", candidate.capacity_usd, reasons)
        haircut = float(candidate.capacity_haircut)
        if not math.isfinite(haircut) or not 0.0 < haircut <= 1.0:
            reasons.append("invalid_capacity_haircut")
            haircut = 0.0
        executable_notional = min(notional, capacity * haircut)
        if executable_notional <= 0.0:
            reasons.append("no_executable_capacity")
        if executable_notional + 1e-9 < notional:
            reasons.append("capacity_limited")
        if not candidate.settlement_forecast.valid:
            reasons.extend(candidate.settlement_forecast.reason_codes or ("invalid_settlement_forecast",))
        if not candidate.settlement_forecast.payments:
            reasons.append("no_funding_payment_in_horizon")
        input_age = float(candidate.input_age_seconds)
        if not math.isfinite(input_age) or input_age < 0.0:
            reasons.append("invalid_input_age")
        elif input_age > float(candidate.max_input_age_seconds):
            reasons.append("stale_inputs")

        confidence = float(candidate.model_confidence)
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            reasons.append("invalid_model_confidence")
            confidence = 0.0

        bps_values = {
            "entry_cost": candidate.entry_cost_bps,
            "exit_cost": candidate.exit_cost_bps,
            "borrow_cost": candidate.borrow_cost_bps,
            "idle_opportunity_cost": candidate.idle_opportunity_cost_bps,
            "basis_risk": candidate.basis_risk_bps,
            "execution_uncertainty": candidate.execution_uncertainty_bps,
            "liquidation_tail": candidate.liquidation_tail_bps,
            "correlation_penalty": candidate.correlation_penalty_bps,
        }
        clean_bps = {
            name: self._finite_nonnegative(name, value, reasons)
            for name, value in bps_values.items()
        }
        basis_expected = float(candidate.basis_expected_pnl_bps)
        if not math.isfinite(basis_expected):
            reasons.append("invalid_basis_expected_pnl")
            basis_expected = 0.0

        requested_funding_notional = self._finite_nonnegative(
            "funding_liable_notional",
            (
                candidate.funding_liable_notional_usd
                if candidate.funding_liable_notional_usd is not None
                else candidate.notional_usd
            ),
            reasons,
        )
        if requested_funding_notional > notional:
            reasons.append("funding_notional_exceeds_pair_gross")
        scale = executable_notional / notional if notional > 0.0 else 0.0
        executable_funding_notional = requested_funding_notional * scale
        funding_mean = candidate.settlement_forecast.expected_payment_usd * scale
        funding_lower = candidate.settlement_forecast.lower_payment_usd * scale
        funding_uncertainty = max(0.0, funding_mean - funding_lower)

        def dollars(bps: float) -> float:
            return executable_notional * bps / 10_000.0

        # The Phase-3 LCB remains observational, but its mean economics now
        # comes from the same versioned kernel as active/paper/replay.  The
        # scorer adds uncertainty only after the canonical deterministic
        # decomposition has been evaluated.
        non_execution_allowance_usd = sum(
            dollars(clean_bps[name])
            for name in (
                "basis_risk",
                "execution_uncertainty",
                "liquidation_tail",
                "correlation_penalty",
            )
        )
        forecast = candidate.settlement_forecast
        horizon_end = (
            forecast.payments[-1].settlement_time
            if forecast.payments
            else forecast.decision_time
        )
        if horizon_end <= forecast.decision_time:
            horizon_end = forecast.decision_time.replace(
                microsecond=forecast.decision_time.microsecond
            )
            from datetime import timedelta

            horizon_end += timedelta(hours=max(1.0, float(forecast.interval_hours)))
        canonical = SHADOW_OPPORTUNITY_ADAPTER.evaluate(
            OpportunityEvaluationInput(
                symbol=symbol,
                direction=forecast.direction,
                decision_time=forecast.decision_time,
                horizon_end=horizon_end,
                pair_gross_notional_usd=executable_notional,
                funding_liable_notional_usd=executable_funding_notional,
                settlement_interval_hours=float(forecast.interval_hours),
                settlements=tuple(
                    SettlementExpectation(
                        settlement_time=payment.settlement_time,
                        expected_rate=payment.mean_rate,
                        source_event_id=f"forecast:{index}",
                    )
                    for index, payment in enumerate(forecast.payments)
                ),
                entry_execution_cost_pct=clean_bps["entry_cost"] / 10_000.0,
                exit_execution_cost_pct=clean_bps["exit_cost"] / 10_000.0,
                minimum_net_edge_bps=-1_000_000_000.0,
                basis_expected_pnl_usd=dollars(basis_expected),
                borrow_and_financing_usd=dollars(clean_bps["borrow_cost"]),
                capital_cost_usd=dollars(clean_bps["idle_opportunity_cost"]),
                repair_and_failure_allowance_usd=non_execution_allowance_usd,
                calendar_authoritative=forecast.valid,
                calendar_observed_at=forecast.decision_time,
                funding_rate_observed_at=forecast.decision_time,
                max_calendar_age_seconds=1.0,
                max_funding_rate_age_seconds=1.0,
            )
        )
        if not canonical.valid:
            reasons.extend(
                f"opportunity_kernel:{code}" for code in canonical.reason_codes
            )
        components = {
            "funding_mean": canonical.gross_funding_usd,
            "basis_expected": dollars(basis_expected),
            "entry_cost": -canonical.entry_execution_cost_usd,
            "exit_cost": -canonical.exit_execution_cost_usd,
            "borrow_cost": -dollars(clean_bps["borrow_cost"]),
            "idle_opportunity_cost": -dollars(
                clean_bps["idle_opportunity_cost"]
            ),
            "basis_risk": -dollars(clean_bps["basis_risk"]),
            "execution_uncertainty": -dollars(
                clean_bps["execution_uncertainty"]
            ),
            "liquidation_tail": -dollars(clean_bps["liquidation_tail"]),
            "correlation_penalty": -dollars(
                clean_bps["correlation_penalty"]
            ),
        }
        mean_net = canonical.net_ev_usd
        # Forecast intervals are already lower-tail estimates.  Add independent
        # execution/basis/model uncertainty without double-counting expected
        # costs.  Sparse confidence widens the model-error component.
        model_error_bps = abs(
            float(candidate.active_baseline_net_edge_bps or 0.0)
        ) * (1.0 - confidence) * 0.25
        independent_uncertainty = math.sqrt(
            funding_uncertainty**2
            + dollars(clean_bps["basis_risk"]) ** 2
            + dollars(clean_bps["execution_uncertainty"]) ** 2
            + dollars(model_error_bps) ** 2
        )
        lower_bound = mean_net - self.confidence_z * independent_uncertainty
        mean_bps = mean_net / executable_notional * 10_000.0 if executable_notional > 0.0 else -1e9
        lower_bps = (
            lower_bound / executable_notional * 10_000.0
            if executable_notional > 0.0
            else -1e9
        )
        hard_reasons = {
            reason
            for reason in reasons
            if reason not in {"capacity_limited"}
        }
        if lower_bound <= 0.0:
            reasons.append("non_positive_net_ev_lcb")
        eligible = not hard_reasons and lower_bound > 0.0
        explanation = (
            f"{symbol}: LCB ${lower_bound:.2f} ({lower_bps:.2f}bps), "
            f"mean ${mean_net:.2f}, executable ${executable_notional:.2f}; "
            + ("eligible" if eligible else ",".join(dict.fromkeys(reasons)))
        )
        return NetEVScore(
            symbol=symbol,
            eligible=eligible,
            mean_net_ev_usd=mean_net,
            lower_bound_net_ev_usd=lower_bound,
            mean_net_edge_bps=mean_bps,
            lower_bound_net_edge_bps=lower_bps,
            executable_notional_usd=executable_notional,
            uncertainty_usd=independent_uncertainty,
            reason_codes=tuple(dict.fromkeys(reasons)),
            components_usd=components,
            explanation=explanation,
        )
