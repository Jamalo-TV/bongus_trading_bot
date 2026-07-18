"""Pure, versioned opportunity-economics kernel.

The kernel has no clock, exchange, database, or runtime-mode dependencies.  An
adapter must provide the exact prospective settlement instants and the
point-in-time rate available at the decision.  This prevents live and research
surfaces from silently replacing a discrete funding payment with continuously
prorated annualized carry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Literal


OPPORTUNITY_KERNEL_VERSION = "opportunity-economics-v1"
FundingDirection = Literal["long_spot_short_perp", "short_spot_long_perp"]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("kernel timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class SettlementExpectation:
    """One discrete funding cash flow known prospectively at decision time."""

    settlement_time: datetime
    expected_rate: float
    eligibility_probability: float = 1.0
    source_event_id: str = ""


@dataclass(frozen=True, slots=True)
class OpportunityEvaluationInput:
    """Fully explicit input contract shared by every runtime surface.

    Cost percentages are the sum of both matched-leg costs, expressed against
    ``funding_liable_notional_usd``.  Pair-gross metrics are also returned so
    callers cannot accidentally mix one-leg and two-leg denominators.
    """

    symbol: str
    direction: FundingDirection
    decision_time: datetime
    horizon_end: datetime
    pair_gross_notional_usd: float
    funding_liable_notional_usd: float
    settlement_interval_hours: float
    settlements: tuple[SettlementExpectation, ...]
    entry_execution_cost_pct: float
    exit_execution_cost_pct: float
    minimum_net_edge_bps: float = 0.0
    basis_expected_pnl_usd: float = 0.0
    borrow_and_financing_usd: float = 0.0
    capital_cost_usd: float = 0.0
    repair_and_failure_allowance_usd: float = 0.0
    calendar_authoritative: bool = False
    calendar_observed_at: datetime | None = None
    funding_rate_observed_at: datetime | None = None
    max_calendar_age_seconds: float = 0.0
    max_funding_rate_age_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class OpportunityEvaluation:
    kernel_version: str
    symbol: str
    direction: FundingDirection
    valid: bool
    eligible: bool
    reason_codes: tuple[str, ...]
    settlement_count: int
    first_settlement_time: datetime | None
    last_settlement_time: datetime | None
    gross_funding_usd: float
    gross_funding_edge_bps: float
    entry_execution_cost_usd: float
    exit_execution_cost_usd: float
    total_cost_usd: float
    net_ev_usd: float
    net_edge_bps: float
    net_edge_pair_gross_bps: float
    pair_gross_notional_usd: float
    funding_liable_notional_usd: float
    evaluation_horizon_hours: float


def _finite(value: float) -> bool:
    return math.isfinite(float(value))


def evaluate_opportunity(inputs: OpportunityEvaluationInput) -> OpportunityEvaluation:
    """Evaluate exact settlement cash flows and full round-trip economics.

    Economic rejection (for example, edge below the configured minimum) is
    separate from input validity.  Missing, stale, future-dated, unordered, or
    non-authoritative inputs fail closed.
    """

    reasons: list[str] = []
    symbol = inputs.symbol.strip().upper()
    if not symbol:
        reasons.append("missing_symbol")
    if inputs.direction not in (
        "long_spot_short_perp",
        "short_spot_long_perp",
    ):
        reasons.append("unknown_direction")

    try:
        decision_time = _utc(inputs.decision_time)
        horizon_end = _utc(inputs.horizon_end)
    except ValueError:
        decision_time = datetime.min.replace(tzinfo=timezone.utc)
        horizon_end = decision_time
        reasons.append("invalid_decision_timestamps")

    if horizon_end <= decision_time:
        reasons.append("invalid_horizon")

    scalar_values = {
        "pair_gross_notional": inputs.pair_gross_notional_usd,
        "funding_liable_notional": inputs.funding_liable_notional_usd,
        "settlement_interval_hours": inputs.settlement_interval_hours,
        "entry_execution_cost": inputs.entry_execution_cost_pct,
        "exit_execution_cost": inputs.exit_execution_cost_pct,
        "minimum_net_edge": inputs.minimum_net_edge_bps,
        "basis_expected_pnl": inputs.basis_expected_pnl_usd,
        "borrow_and_financing": inputs.borrow_and_financing_usd,
        "capital_cost": inputs.capital_cost_usd,
        "repair_and_failure_allowance": inputs.repair_and_failure_allowance_usd,
        "max_calendar_age": inputs.max_calendar_age_seconds,
        "max_funding_rate_age": inputs.max_funding_rate_age_seconds,
    }
    for name, value in scalar_values.items():
        if not _finite(value):
            reasons.append(f"invalid_{name}")

    pair_gross = (
        max(0.0, float(inputs.pair_gross_notional_usd))
        if _finite(inputs.pair_gross_notional_usd)
        else 0.0
    )
    liable_notional = (
        max(0.0, float(inputs.funding_liable_notional_usd))
        if _finite(inputs.funding_liable_notional_usd)
        else 0.0
    )
    if pair_gross <= 0.0:
        reasons.append("non_positive_pair_gross_notional")
    if liable_notional <= 0.0:
        reasons.append("non_positive_funding_liable_notional")
    if liable_notional > pair_gross:
        reasons.append("funding_notional_exceeds_pair_gross")
    if not _finite(inputs.settlement_interval_hours) or not (
        0.0 < float(inputs.settlement_interval_hours) <= 24.0
    ):
        reasons.append("invalid_settlement_interval")

    for name, value in (
        ("entry_execution_cost", inputs.entry_execution_cost_pct),
        ("exit_execution_cost", inputs.exit_execution_cost_pct),
        ("borrow_and_financing", inputs.borrow_and_financing_usd),
        ("capital_cost", inputs.capital_cost_usd),
        ("repair_and_failure_allowance", inputs.repair_and_failure_allowance_usd),
    ):
        if _finite(value) and float(value) < 0.0:
            reasons.append(f"negative_{name}")

    if not inputs.calendar_authoritative:
        reasons.append("missing_authoritative_settlement_metadata")

    def validate_observation(
        value: datetime | None,
        maximum_age_seconds: float,
        *,
        missing_code: str,
        stale_code: str,
        future_code: str,
    ) -> None:
        if value is None:
            reasons.append(missing_code)
            return
        try:
            observed_at = _utc(value)
        except ValueError:
            reasons.append(missing_code)
            return
        age_seconds = (decision_time - observed_at).total_seconds()
        if age_seconds < -1e-6:
            reasons.append(future_code)
        if (
            not _finite(maximum_age_seconds)
            or float(maximum_age_seconds) <= 0.0
            or age_seconds > float(maximum_age_seconds)
        ):
            reasons.append(stale_code)

    validate_observation(
        inputs.calendar_observed_at,
        inputs.max_calendar_age_seconds,
        missing_code="missing_settlement_metadata_timestamp",
        stale_code="stale_settlement_metadata",
        future_code="future_settlement_metadata",
    )
    validate_observation(
        inputs.funding_rate_observed_at,
        inputs.max_funding_rate_age_seconds,
        missing_code="missing_funding_rate_timestamp",
        stale_code="stale_funding_rate",
        future_code="future_funding_rate",
    )

    direction_sign = 1.0 if inputs.direction == "long_spot_short_perp" else -1.0
    gross_funding_usd = 0.0
    valid_times: list[datetime] = []
    previous_time: datetime | None = None
    for settlement in inputs.settlements:
        try:
            settlement_time = _utc(settlement.settlement_time)
        except ValueError:
            reasons.append("invalid_settlement_timestamp")
            continue
        if settlement_time <= decision_time or settlement_time > horizon_end:
            reasons.append("settlement_outside_horizon")
        if previous_time is not None and settlement_time <= previous_time:
            reasons.append("unordered_or_duplicate_settlement")
        previous_time = settlement_time
        valid_times.append(settlement_time)

        rate = float(settlement.expected_rate)
        probability = float(settlement.eligibility_probability)
        if not _finite(rate):
            reasons.append("invalid_settlement_rate")
            continue
        if not _finite(probability) or not 0.0 <= probability <= 1.0:
            reasons.append("invalid_settlement_eligibility")
            continue
        gross_funding_usd += direction_sign * rate * probability * liable_notional

    if not inputs.settlements:
        reasons.append("no_settlement_in_horizon")

    entry_cost_usd = (
        liable_notional * max(0.0, float(inputs.entry_execution_cost_pct))
        if _finite(inputs.entry_execution_cost_pct)
        else 0.0
    )
    exit_cost_usd = (
        liable_notional * max(0.0, float(inputs.exit_execution_cost_pct))
        if _finite(inputs.exit_execution_cost_pct)
        else 0.0
    )
    other_cost_usd = sum(
        max(0.0, float(value)) if _finite(value) else 0.0
        for value in (
            inputs.borrow_and_financing_usd,
            inputs.capital_cost_usd,
            inputs.repair_and_failure_allowance_usd,
        )
    )
    total_cost_usd = entry_cost_usd + exit_cost_usd + other_cost_usd
    basis_expected_pnl_usd = (
        float(inputs.basis_expected_pnl_usd)
        if _finite(inputs.basis_expected_pnl_usd)
        else 0.0
    )
    net_ev_usd = gross_funding_usd + basis_expected_pnl_usd - total_cost_usd
    gross_funding_edge_bps = (
        gross_funding_usd / liable_notional * 10_000.0
        if liable_notional > 0.0
        else -math.inf
    )
    net_edge_bps = (
        net_ev_usd / liable_notional * 10_000.0
        if liable_notional > 0.0
        else -math.inf
    )
    net_edge_pair_gross_bps = (
        net_ev_usd / pair_gross * 10_000.0 if pair_gross > 0.0 else -math.inf
    )

    # Input validity excludes decision-only economic outcomes.
    invalid_reasons = tuple(dict.fromkeys(reasons))
    valid = not invalid_reasons
    minimum_edge = (
        float(inputs.minimum_net_edge_bps)
        if _finite(inputs.minimum_net_edge_bps)
        else math.inf
    )
    if valid and net_edge_bps < minimum_edge:
        reasons.append("net_edge_below_minimum")
    if valid and gross_funding_usd <= 0.0:
        reasons.append("non_positive_directional_funding")
    reason_codes = tuple(dict.fromkeys(reasons))
    eligible = valid and net_edge_bps >= minimum_edge and gross_funding_usd > 0.0
    horizon_hours = max(
        0.0, (horizon_end - decision_time).total_seconds() / 3_600.0
    )
    return OpportunityEvaluation(
        kernel_version=OPPORTUNITY_KERNEL_VERSION,
        symbol=symbol,
        direction=inputs.direction,
        valid=valid,
        eligible=eligible,
        reason_codes=reason_codes,
        settlement_count=len(valid_times),
        first_settlement_time=valid_times[0] if valid_times else None,
        last_settlement_time=valid_times[-1] if valid_times else None,
        gross_funding_usd=gross_funding_usd,
        gross_funding_edge_bps=gross_funding_edge_bps,
        entry_execution_cost_usd=entry_cost_usd,
        exit_execution_cost_usd=exit_cost_usd,
        total_cost_usd=total_cost_usd,
        net_ev_usd=net_ev_usd,
        net_edge_bps=net_edge_bps,
        net_edge_pair_gross_bps=net_edge_pair_gross_bps,
        pair_gross_notional_usd=pair_gross,
        funding_liable_notional_usd=liable_notional,
        evaluation_horizon_hours=horizon_hours,
    )
