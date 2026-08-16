"""Deterministic B5 statistics, robustness diagnostics, and verdict logic."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final, Literal

from bongus.research.cross_venue.evaluation import (
    EvaluationProtocol,
    EvaluationWindow,
    OpportunityOutcome,
    PurgedWalkForwardEvaluator,
    ScenarioMetrics,
    WalkForwardEvaluationReport,
    evaluate_sensitivity_metrics,
    load_evaluation_fixture,
)
from bongus.research.cross_venue.schema import (
    CanonicalAsset,
    epoch_nanoseconds,
    exact_decimal,
    exact_wire,
    nonnegative_decimal,
    positive_decimal,
)
from bongus.research.cross_venue.storage import canonical_json_bytes

NANOSECONDS_PER_DAY: Final[int] = 86_400_000_000_000
BOOTSTRAP_SAMPLES: Final[int] = 2_000
BOOTSTRAP_SEED: Final[str] = "binance-hyperliquid-v1-deterministic-block-bootstrap"
ONE_SIDED_ALPHA_PERCENT: Final[int] = 5

BlockKind = Literal["daily", "weekly"]
VerdictStatus = Literal[
    "invalid_dataset",
    "abandon_optimistic_oracle",
    "collector_qa_only",
    "collecting_forward_oos",
    "fail_and_archive",
    "economically_weak_archive",
    "continue_to_180_days",
    "inconclusive_archive",
    "viable",
    "strong",
]


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _sha256_text(value: str, field_name: str) -> str:
    normalized = _required_text(value, field_name).casefold()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError(f"{field_name} must be a SHA-256 hex digest")
    return normalized


def _exact_bool(value: bool, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{field_name} must be boolean")
    return value


@dataclass(frozen=True, slots=True)
class DailyEvidenceObservation:
    """One asset-day attribution row; all assets on a day form one block."""

    event_id: str
    canonical_asset: CanonicalAsset
    utc_day_start_ns: int
    net_pnl_usd: Decimal
    total_reserved_capital_days: Decimal
    funding_minus_cost_usd: Decimal
    binance_only_net_pnl_usd: Decimal
    available_time_ns: int
    quality_flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _required_text(self.event_id, "event_id"))
        if not isinstance(self.canonical_asset, CanonicalAsset):
            raise TypeError("daily evidence asset must use the fixed universe")
        day_start = epoch_nanoseconds(self.utc_day_start_ns, "utc_day_start_ns")
        available = epoch_nanoseconds(self.available_time_ns, "available_time_ns")
        if day_start % NANOSECONDS_PER_DAY != 0:
            raise ValueError("utc_day_start_ns must be aligned to 00:00 UTC")
        if available < day_start + NANOSECONDS_PER_DAY:
            raise ValueError("daily evidence cannot be available before its UTC day closes")
        object.__setattr__(self, "utc_day_start_ns", day_start)
        object.__setattr__(self, "available_time_ns", available)
        for name in ("net_pnl_usd", "funding_minus_cost_usd", "binance_only_net_pnl_usd"):
            object.__setattr__(self, name, exact_decimal(getattr(self, name), name))
        object.__setattr__(
            self,
            "total_reserved_capital_days",
            positive_decimal(self.total_reserved_capital_days, "total_reserved_capital_days"),
        )
        flags = tuple(sorted({_required_text(flag, "quality flag") for flag in self.quality_flags}))
        object.__setattr__(self, "quality_flags", flags)

    @property
    def utc_month(self) -> str:
        return datetime.fromtimestamp(self.utc_day_start_ns // 1_000_000_000, tz=UTC).strftime("%Y-%m")


@dataclass(frozen=True, slots=True)
class BootstrapEstimate:
    block_kind: BlockKind
    blocks: int
    samples: int
    point_simple_annualized_return: Decimal
    one_sided_95_lcb: Decimal
    sample_sha256: str


def _metric(observations: Sequence[DailyEvidenceObservation]) -> Decimal:
    net = sum((value.net_pnl_usd for value in observations), Decimal("0"))
    capital_days = sum((value.total_reserved_capital_days for value in observations), Decimal("0"))
    if capital_days <= 0:
        raise ValueError("bootstrap observations require positive total capital-days")
    return net / capital_days * Decimal("365")


def _block_key(observation: DailyEvidenceObservation, block_kind: BlockKind) -> str:
    if block_kind == "daily":
        return str(observation.utc_day_start_ns)
    instant = datetime.fromtimestamp(observation.utc_day_start_ns // 1_000_000_000, tz=UTC)
    iso_year, iso_week, _weekday = instant.isocalendar()
    return f"{iso_year:04d}-W{iso_week:02d}"


def deterministic_block_bootstrap(
    observations: Sequence[DailyEvidenceObservation],
    *,
    block_kind: BlockKind,
    samples: int = BOOTSTRAP_SAMPLES,
    seed: str = BOOTSTRAP_SEED,
) -> BootstrapEstimate:
    values = tuple(sorted(observations, key=lambda value: (value.utc_day_start_ns, value.event_id)))
    if not values:
        raise ValueError("block bootstrap requires daily evidence")
    if block_kind not in ("daily", "weekly"):
        raise ValueError("block_kind must be daily or weekly")
    if isinstance(samples, bool) or not isinstance(samples, int) or samples < 100:
        raise ValueError("bootstrap samples must be an exact integer of at least 100")
    normalized_seed = _required_text(seed, "seed")
    event_ids = tuple(value.event_id for value in values)
    if len(event_ids) != len(set(event_ids)):
        raise ValueError("daily evidence event IDs must be unique")
    if any(value.quality_flags for value in values):
        raise ValueError("quality-flagged daily evidence cannot be bootstrapped")
    grouped: dict[str, list[DailyEvidenceObservation]] = {}
    for value in values:
        grouped.setdefault(_block_key(value, block_kind), []).append(value)
    blocks = tuple(tuple(grouped[key]) for key in sorted(grouped))
    estimates: list[Decimal] = []
    for iteration in range(samples):
        sampled: list[DailyEvidenceObservation] = []
        for draw in range(len(blocks)):
            digest = hashlib.sha256(f"{normalized_seed}|{block_kind}|{iteration}|{draw}".encode("utf-8")).digest()
            sampled.extend(blocks[int.from_bytes(digest[:8], "big") % len(blocks)])
        estimates.append(_metric(sampled))
    ordered_estimates = tuple(sorted(estimates))
    lower_index = max(0, (ONE_SIDED_ALPHA_PERCENT * samples + 99) // 100 - 1)
    sample_hash = hashlib.sha256(canonical_json_bytes(ordered_estimates)).hexdigest()
    return BootstrapEstimate(
        block_kind,
        len(blocks),
        samples,
        _metric(values),
        ordered_estimates[lower_index],
        sample_hash,
    )


@dataclass(frozen=True, slots=True)
class ExclusionMetric:
    excluded: str
    observations: int
    simple_annualized_return: Decimal | None


@dataclass(frozen=True, slots=True)
class RobustnessDiagnostics:
    primary_net_pnl_usd: Decimal
    binance_only_net_pnl_usd: Decimal
    funding_minus_cost_usd: Decimal
    top_five_profit_contribution_fraction: Decimal | None
    leave_one_symbol_out: tuple[ExclusionMetric, ...]
    leave_one_month_out: tuple[ExclusionMetric, ...]
    sensitivity_metrics: tuple[ScenarioMetrics, ...]

    @property
    def leave_one_symbol_positive(self) -> bool:
        return bool(self.leave_one_symbol_out) and all(
            value.simple_annualized_return is not None and value.simple_annualized_return > 0
            for value in self.leave_one_symbol_out
        )

    @property
    def leave_one_month_positive(self) -> bool:
        return len(self.leave_one_month_out) >= 2 and all(
            value.simple_annualized_return is not None and value.simple_annualized_return > 0
            for value in self.leave_one_month_out
        )

    @property
    def concentration_passed(self) -> bool:
        return (
            self.top_five_profit_contribution_fraction is not None
            and self.top_five_profit_contribution_fraction < Decimal("0.30")
        )

    @property
    def stress_matrix_positive(self) -> bool:
        stressed = tuple(value for value in self.sensitivity_metrics if value.scenario_name != "baseline")
        return bool(stressed) and all(value.total_net_pnl_usd > 0 for value in stressed)


def _exclusion_metric(
    excluded: str,
    values: Sequence[DailyEvidenceObservation],
) -> ExclusionMetric:
    observations = tuple(values)
    return ExclusionMetric(
        excluded,
        len(observations),
        _metric(observations) if observations else None,
    )


def robustness_diagnostics(
    daily_observations: Sequence[DailyEvidenceObservation],
    outcomes: Sequence[OpportunityOutcome],
) -> RobustnessDiagnostics:
    daily = tuple(daily_observations)
    episodes = tuple(outcomes)
    if not daily or not episodes:
        raise ValueError("robustness diagnostics require daily evidence and episode outcomes")
    episode_ids = tuple(value.event_id for value in episodes)
    if len(episode_ids) != len(set(episode_ids)):
        raise ValueError("episode outcome IDs must be unique")
    if any(value.quality_flags for value in episodes):
        raise ValueError("quality-flagged outcomes cannot enter robustness diagnostics")
    primary_net = sum((value.net_pnl_usd for value in daily), Decimal("0"))
    binance_only = sum((value.binance_only_net_pnl_usd for value in daily), Decimal("0"))
    funding_minus_cost = sum((value.funding_minus_cost_usd for value in daily), Decimal("0"))
    positive_events = sorted(
        (value.baseline_net_pnl_usd for value in episodes if value.baseline_net_pnl_usd > 0),
        reverse=True,
    )
    episode_net = sum((value.baseline_net_pnl_usd for value in episodes), Decimal("0"))
    concentration = None
    if episode_net > 0:
        concentration = sum(positive_events[:5], Decimal("0")) / episode_net
    leave_symbol = tuple(
        _exclusion_metric(
            asset.value,
            tuple(value for value in daily if value.canonical_asset is not asset),
        )
        for asset in CanonicalAsset
    )
    months = tuple(sorted({value.utc_month for value in daily}))
    leave_month = tuple(
        _exclusion_metric(month, tuple(value for value in daily if value.utc_month != month)) for month in months
    )
    return RobustnessDiagnostics(
        primary_net,
        binance_only,
        funding_minus_cost,
        concentration,
        leave_symbol,
        leave_month,
        evaluate_sensitivity_metrics(episodes),
    )


@dataclass(frozen=True, slots=True)
class VerdictEvidence:
    complete_utc_days: int
    sealed_final_days: int
    storage_sizing_pilot_hours: int
    optimistic_oracle_net_pnl_usd: Decimal
    max_drawdown_fraction: Decimal
    required_depth_multiple: Decimal
    dataset_sha256: str
    input_report_sha256: str
    dataset_integrity_passed: bool
    scheduled_cadence_passed: bool
    decision_anchor_gate_passed: bool
    funding_reconciliation_passed: bool
    replay_hash_reproduced: bool
    policy_frozen_before_oos: bool
    stress_inputs_complete: bool
    liquidation_survival_passed: bool
    secondary_family_correction_applied: bool

    def __post_init__(self) -> None:
        for name in ("complete_utc_days", "sealed_final_days", "storage_sizing_pilot_hours"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative exact integer")
        object.__setattr__(
            self,
            "optimistic_oracle_net_pnl_usd",
            exact_decimal(self.optimistic_oracle_net_pnl_usd, "optimistic_oracle_net_pnl_usd"),
        )
        object.__setattr__(
            self,
            "max_drawdown_fraction",
            nonnegative_decimal(self.max_drawdown_fraction, "max_drawdown_fraction"),
        )
        object.__setattr__(
            self,
            "required_depth_multiple",
            nonnegative_decimal(self.required_depth_multiple, "required_depth_multiple"),
        )
        object.__setattr__(self, "dataset_sha256", _sha256_text(self.dataset_sha256, "dataset_sha256"))
        object.__setattr__(
            self,
            "input_report_sha256",
            _sha256_text(self.input_report_sha256, "input_report_sha256"),
        )
        for name in (
            "dataset_integrity_passed",
            "scheduled_cadence_passed",
            "decision_anchor_gate_passed",
            "funding_reconciliation_passed",
            "replay_hash_reproduced",
            "policy_frozen_before_oos",
            "stress_inputs_complete",
            "liquidation_survival_passed",
            "secondary_family_correction_applied",
        ):
            object.__setattr__(self, name, _exact_bool(getattr(self, name), name))

    @property
    def immutable_data_gates_passed(self) -> bool:
        return (
            self.dataset_integrity_passed
            and self.scheduled_cadence_passed
            and self.decision_anchor_gate_passed
            and self.funding_reconciliation_passed
            and self.replay_hash_reproduced
            and self.policy_frozen_before_oos
        )


@dataclass(frozen=True, slots=True)
class ResearchVerdict:
    status: VerdictStatus
    simple_annualized_estimate: Decimal
    conservative_one_sided_95_lcb: Decimal
    reasons: tuple[str, ...]
    grants_live_authority: Literal[False] = False


def preregistered_verdict(
    daily_bootstrap: BootstrapEstimate,
    weekly_bootstrap: BootstrapEstimate,
    diagnostics: RobustnessDiagnostics,
    evidence: VerdictEvidence,
) -> ResearchVerdict:
    if daily_bootstrap.block_kind != "daily" or weekly_bootstrap.block_kind != "weekly":
        raise ValueError("verdict requires both daily and weekly block bootstrap estimates")
    estimate = daily_bootstrap.point_simple_annualized_return
    if estimate != weekly_bootstrap.point_simple_annualized_return:
        raise ValueError("daily and weekly bootstrap point estimates must match")
    lower_bound = min(daily_bootstrap.one_sided_95_lcb, weekly_bootstrap.one_sided_95_lcb)

    def verdict(status: VerdictStatus, *reasons: str) -> ResearchVerdict:
        return ResearchVerdict(status, estimate, lower_bound, tuple(reasons))

    if not evidence.immutable_data_gates_passed:
        return verdict("invalid_dataset", "immutable data, cadence, causality, funding, or replay gate failed")
    if evidence.optimistic_oracle_net_pnl_usd <= 0:
        return verdict("abandon_optimistic_oracle", "optimistic ex-post oracle is non-positive after costs")
    if evidence.complete_utc_days < 14:
        return verdict("collector_qa_only", "fewer than 14 complete UTC days; performance claims are forbidden")
    if evidence.complete_utc_days < 90:
        return verdict("collecting_forward_oos", "minimum 90-day forward OOS window is incomplete")
    if evidence.sealed_final_days < 30:
        return verdict("fail_and_archive", "the final 30 days were not sealed")
    if lower_bound <= 0:
        return verdict("fail_and_archive", "one-sided 95 percent lower confidence bound is non-positive")
    if estimate < Decimal("0.05"):
        return verdict("economically_weak_archive", "annualized estimate is below five percent")
    if lower_bound < Decimal("0.05"):
        if evidence.complete_utc_days < 180:
            return verdict("continue_to_180_days", "estimate exceeds five percent but its lower bound does not")
        return verdict("inconclusive_archive", "180 days completed without a five-percent lower bound")

    failed_robustness: list[str] = []
    if diagnostics.primary_net_pnl_usd <= 0:
        failed_robustness.append("primary portfolio did not beat no-trade")
    if diagnostics.primary_net_pnl_usd <= diagnostics.binance_only_net_pnl_usd:
        failed_robustness.append("primary portfolio did not beat Binance-only")
    if diagnostics.funding_minus_cost_usd <= 0:
        failed_robustness.append("funding minus cost is non-positive without favorable basis")
    if not diagnostics.leave_one_symbol_positive:
        failed_robustness.append("leave-one-symbol-out is not uniformly positive")
    if not diagnostics.leave_one_month_positive:
        failed_robustness.append("leave-one-month-out is not uniformly positive")
    if not diagnostics.concentration_passed:
        failed_robustness.append("best five events contribute at least thirty percent of profit")
    if not diagnostics.stress_matrix_positive or not evidence.stress_inputs_complete:
        failed_robustness.append("mandatory stress matrix is incomplete or non-positive")
    if evidence.max_drawdown_fraction > Decimal("0.10"):
        failed_robustness.append("max drawdown exceeds ten percent")
    if evidence.required_depth_multiple < Decimal("5"):
        failed_robustness.append("required depth is below five times target size")
    if not evidence.liquidation_survival_passed:
        failed_robustness.append("primary leverage did not survive liquidation stress")
    if evidence.storage_sizing_pilot_hours < 48:
        failed_robustness.append("48-hour storage sizing pilot is incomplete")
    if not evidence.secondary_family_correction_applied:
        failed_robustness.append("secondary hypothesis family correction is missing")
    if failed_robustness:
        return verdict("fail_and_archive", *failed_robustness)
    if lower_bound > Decimal("0.12"):
        return verdict("strong", "all gates pass and robust lower bound exceeds twelve percent")
    return verdict("viable", "all gates pass with a robust five-to-twelve-percent result")


@dataclass(frozen=True, slots=True)
class EvidenceEvaluationReport:
    protocol_id: str
    protocol_sha256: str
    preregistration_sha256: str
    dataset_sha256: str
    input_report_sha256: str
    walk_forward: WalkForwardEvaluationReport
    daily_bootstrap: BootstrapEstimate
    weekly_bootstrap: BootstrapEstimate
    diagnostics: RobustnessDiagnostics
    verdict: ResearchVerdict

    def _payload(self) -> Mapping[str, object]:
        return {
            "protocol_id": self.protocol_id,
            "protocol_sha256": self.protocol_sha256,
            "preregistration_sha256": self.preregistration_sha256,
            "dataset_sha256": self.dataset_sha256,
            "input_report_sha256": self.input_report_sha256,
            "walk_forward": self.walk_forward.as_wire(),
            "daily_bootstrap": self.daily_bootstrap,
            "weekly_bootstrap": self.weekly_bootstrap,
            "diagnostics": self.diagnostics,
            "verdict": self.verdict,
        }

    @property
    def report_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self._payload())).hexdigest()

    def as_wire(self) -> Mapping[str, object]:
        wire = exact_wire(self._payload())
        if not isinstance(wire, Mapping):
            raise TypeError("evidence report must encode as an object")
        return {**wire, "report_sha256": self.report_sha256}


def evaluate_research_evidence(
    *,
    daily_observations: Sequence[DailyEvidenceObservation],
    outcomes: Sequence[OpportunityOutcome],
    windows: Sequence[EvaluationWindow],
    evidence: VerdictEvidence,
    protocol: EvaluationProtocol | None = None,
) -> EvidenceEvaluationReport:
    frozen_protocol = protocol or EvaluationProtocol()
    observed_days = {value.utc_day_start_ns for value in daily_observations}
    if len(observed_days) != evidence.complete_utc_days:
        raise ValueError("complete_utc_days must equal the immutable daily evidence blocks")
    daily = deterministic_block_bootstrap(daily_observations, block_kind="daily")
    weekly = deterministic_block_bootstrap(daily_observations, block_kind="weekly")
    diagnostics = robustness_diagnostics(daily_observations, outcomes)
    walk_forward = PurgedWalkForwardEvaluator(frozen_protocol).evaluate(tuple(outcomes), tuple(windows))
    verdict = preregistered_verdict(daily, weekly, diagnostics, evidence)
    return EvidenceEvaluationReport(
        frozen_protocol.protocol_id,
        frozen_protocol.protocol_sha256,
        frozen_protocol.preregistration_sha256,
        evidence.dataset_sha256,
        evidence.input_report_sha256,
        walk_forward,
        daily,
        weekly,
        diagnostics,
        verdict,
    )


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a JSON object")
    return value


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    return _required_text(value, field_name)


def _sequence(value: object, field_name: str) -> Sequence[object]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ValueError(f"{field_name} must be a JSON array")
    return value


def _integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{field_name} must be an exact integer")
    result = int(value)
    if not isinstance(value, int) and value.strip() != str(result):
        raise ValueError(f"{field_name} must be an integer string")
    return result


def _decimal(value: object, field_name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, str, int)):
        raise ValueError(f"{field_name} must be an exact decimal")
    return exact_decimal(value, field_name)


def _boolean(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be boolean")
    return value


def load_evidence_fixture(
    path: str | Path,
) -> tuple[
    tuple[DailyEvidenceObservation, ...],
    tuple[OpportunityOutcome, ...],
    tuple[EvaluationWindow, ...],
    VerdictEvidence,
]:
    fixture_path = Path(path).resolve()
    payload = json.loads(
        fixture_path.read_text(encoding="utf-8"),
        parse_float=Decimal,
        parse_int=int,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"non-finite JSON: {value}")),
    )
    root = _mapping(payload, "evidence fixture")
    daily: list[DailyEvidenceObservation] = []
    for index, value in enumerate(_sequence(root.get("daily_observations"), "daily_observations")):
        row = _mapping(value, f"daily_observations[{index}]")
        daily.append(
            DailyEvidenceObservation(
                event_id=_text(row.get("event_id"), "event_id"),
                canonical_asset=CanonicalAsset(_text(row.get("canonical_asset"), "asset")),
                utc_day_start_ns=_integer(row.get("utc_day_start_ns"), "utc_day_start_ns"),
                net_pnl_usd=_decimal(row.get("net_pnl_usd"), "net_pnl_usd"),
                total_reserved_capital_days=_decimal(
                    row.get("total_reserved_capital_days"),
                    "total_reserved_capital_days",
                ),
                funding_minus_cost_usd=_decimal(
                    row.get("funding_minus_cost_usd"),
                    "funding_minus_cost_usd",
                ),
                binance_only_net_pnl_usd=_decimal(
                    row.get("binance_only_net_pnl_usd"),
                    "binance_only_net_pnl_usd",
                ),
                available_time_ns=_integer(row.get("available_time_ns"), "available_time_ns"),
                quality_flags=tuple(
                    _text(item, "quality flag") for item in _sequence(row.get("quality_flags", []), "quality_flags")
                ),
            )
        )
    evidence_row = _mapping(root.get("verdict_evidence"), "verdict_evidence")
    evidence = VerdictEvidence(
        complete_utc_days=_integer(evidence_row.get("complete_utc_days"), "complete_utc_days"),
        sealed_final_days=_integer(evidence_row.get("sealed_final_days"), "sealed_final_days"),
        storage_sizing_pilot_hours=_integer(
            evidence_row.get("storage_sizing_pilot_hours"),
            "storage_sizing_pilot_hours",
        ),
        optimistic_oracle_net_pnl_usd=_decimal(
            evidence_row.get("optimistic_oracle_net_pnl_usd"),
            "optimistic_oracle_net_pnl_usd",
        ),
        max_drawdown_fraction=_decimal(
            evidence_row.get("max_drawdown_fraction"),
            "max_drawdown_fraction",
        ),
        required_depth_multiple=_decimal(
            evidence_row.get("required_depth_multiple"),
            "required_depth_multiple",
        ),
        dataset_sha256=_text(evidence_row.get("dataset_sha256"), "dataset_sha256"),
        input_report_sha256=_text(evidence_row.get("input_report_sha256"), "input_report_sha256"),
        dataset_integrity_passed=_boolean(
            evidence_row.get("dataset_integrity_passed"),
            "dataset_integrity_passed",
        ),
        scheduled_cadence_passed=_boolean(
            evidence_row.get("scheduled_cadence_passed"),
            "scheduled_cadence_passed",
        ),
        decision_anchor_gate_passed=_boolean(
            evidence_row.get("decision_anchor_gate_passed"),
            "decision_anchor_gate_passed",
        ),
        funding_reconciliation_passed=_boolean(
            evidence_row.get("funding_reconciliation_passed"),
            "funding_reconciliation_passed",
        ),
        replay_hash_reproduced=_boolean(
            evidence_row.get("replay_hash_reproduced"),
            "replay_hash_reproduced",
        ),
        policy_frozen_before_oos=_boolean(
            evidence_row.get("policy_frozen_before_oos"),
            "policy_frozen_before_oos",
        ),
        stress_inputs_complete=_boolean(
            evidence_row.get("stress_inputs_complete"),
            "stress_inputs_complete",
        ),
        liquidation_survival_passed=_boolean(
            evidence_row.get("liquidation_survival_passed"),
            "liquidation_survival_passed",
        ),
        secondary_family_correction_applied=_boolean(
            evidence_row.get("secondary_family_correction_applied"),
            "secondary_family_correction_applied",
        ),
    )
    outcomes, windows = load_evaluation_fixture(fixture_path)
    return tuple(daily), outcomes, windows, evidence


def write_evidence_report(report: EvidenceEvaluationReport, path: str | Path) -> Path:
    output = Path(path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    encoded = canonical_json_bytes(report.as_wire()) + b"\n"
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, output)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
    return output


def verify_evidence_report(path: str | Path) -> Mapping[str, object]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    root = _mapping(payload, "evidence report")
    claimed = _sha256_text(str(root.get("report_sha256", "")), "report_sha256")
    body = {key: value for key, value in root.items() if key != "report_sha256"}
    actual = hashlib.sha256(canonical_json_bytes(body)).hexdigest()
    if claimed != actual:
        raise ValueError("evidence report hash mismatch")
    return root


__all__ = [
    "BOOTSTRAP_SAMPLES",
    "BOOTSTRAP_SEED",
    "BlockKind",
    "BootstrapEstimate",
    "DailyEvidenceObservation",
    "EvidenceEvaluationReport",
    "ExclusionMetric",
    "ONE_SIDED_ALPHA_PERCENT",
    "ResearchVerdict",
    "RobustnessDiagnostics",
    "VerdictEvidence",
    "VerdictStatus",
    "deterministic_block_bootstrap",
    "evaluate_research_evidence",
    "load_evidence_fixture",
    "preregistered_verdict",
    "robustness_diagnostics",
    "verify_evidence_report",
    "write_evidence_report",
]
