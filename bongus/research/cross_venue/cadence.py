"""Preregistered collection cadence and finalized-funding evidence gates."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

from bongus.research.cross_venue.schema import (
    CanonicalAsset,
    FundingSettlement,
    Venue,
    epoch_nanoseconds,
)
from bongus.research.cross_venue.storage import canonical_json_bytes

NANOSECONDS_PER_SECOND: Final[int] = 1_000_000_000
NANOSECONDS_PER_MINUTE: Final[int] = 60 * NANOSECONDS_PER_SECOND
NANOSECONDS_PER_HOUR: Final[int] = 60 * NANOSECONDS_PER_MINUTE

CadenceMode = Literal["periodic", "event_driven", "pilot"]


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _ratio(numerator: int, denominator: int) -> Decimal:
    return Decimal(numerator) / Decimal(denominator) if denominator > 0 else Decimal("0")


@dataclass(frozen=True, slots=True)
class CadenceContract:
    name: str
    datasets: tuple[str, ...]
    mode: CadenceMode
    normal_interval_ns: int | None
    maximum_lateness_ns: int
    burst_interval_ns: int | None = None
    burst_window_ns: int | None = None
    on_change: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _required_text(self.name, "name"))
        if not self.datasets or any(not isinstance(value, str) or not value.strip() for value in self.datasets):
            raise ValueError("cadence contract requires named datasets")
        if len(self.datasets) != len(set(self.datasets)):
            raise ValueError("cadence datasets must be unique")
        if self.mode not in ("periodic", "event_driven", "pilot"):
            raise ValueError("unknown cadence mode")
        for name in (
            "normal_interval_ns",
            "maximum_lateness_ns",
            "burst_interval_ns",
            "burst_window_ns",
        ):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
                raise ValueError(f"{name} must be a positive exact integer")
        if self.mode in ("periodic", "pilot") and self.normal_interval_ns is None:
            raise ValueError("periodic and pilot cadence require an interval")
        if (self.burst_interval_ns is None) != (self.burst_window_ns is None):
            raise ValueError("burst interval and window must be specified together")

    def allowed_lateness_ns(self, *, burst: bool) -> int:
        if burst and self.burst_interval_ns is not None:
            return min(self.maximum_lateness_ns, self.burst_interval_ns)
        return self.maximum_lateness_ns


COLLECTION_CADENCE: Final[tuple[CadenceContract, ...]] = (
    CadenceContract(
        "bbo_approximately_1s",
        ("bbo",),
        "periodic",
        NANOSECONDS_PER_SECOND,
        2 * NANOSECONDS_PER_SECOND,
    ),
    CadenceContract(
        "reference_and_funding_1_to_5s",
        ("funding_quotes", "mark_index_oracle_prices", "reference_context"),
        "periodic",
        5 * NANOSECONDS_PER_SECOND,
        5 * NANOSECONDS_PER_SECOND,
        on_change=True,
    ),
    CadenceContract(
        "top20_normal_30s_burst_1s",
        ("top20_book",),
        "periodic",
        30 * NANOSECONDS_PER_SECOND,
        30 * NANOSECONDS_PER_SECOND,
        burst_interval_ns=NANOSECONDS_PER_SECOND,
        burst_window_ns=5 * NANOSECONDS_PER_MINUTE,
    ),
    CadenceContract(
        "final_funding_each_settlement",
        ("final_funding_settlements",),
        "event_driven",
        None,
        5 * NANOSECONDS_PER_MINUTE,
    ),
    CadenceContract(
        "metadata_and_fees_daily_or_change",
        ("contract_metadata", "funding_intervals", "fee_profiles"),
        "periodic",
        24 * NANOSECONDS_PER_HOUR,
        24 * NANOSECONDS_PER_HOUR,
        on_change=True,
    ),
    CadenceContract(
        "storage_sizing_pilot_48h",
        ("storage_health",),
        "pilot",
        48 * NANOSECONDS_PER_HOUR,
        48 * NANOSECONDS_PER_HOUR,
    ),
)

_CADENCE_BY_DATASET: Final[dict[str, CadenceContract]] = {
    dataset: contract for contract in COLLECTION_CADENCE for dataset in contract.datasets
}


def cadence_for_dataset(dataset: str) -> CadenceContract:
    try:
        return _CADENCE_BY_DATASET[_required_text(dataset, "dataset")]
    except KeyError as exc:
        raise ValueError(f"dataset has no preregistered cadence: {dataset}") from exc


@dataclass(frozen=True, slots=True)
class CadenceAnchor:
    anchor_id: str
    dataset: str
    venue: Venue
    canonical_asset: CanonicalAsset | Literal["UNIVERSE"]
    scheduled_time_ns: int
    burst: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "anchor_id", _required_text(self.anchor_id, "anchor_id"))
        cadence_for_dataset(self.dataset)
        if not isinstance(self.venue, Venue):
            raise TypeError("cadence venue must use the fixed Venue enum")
        if not isinstance(self.canonical_asset, CanonicalAsset) and self.canonical_asset != "UNIVERSE":
            raise TypeError("cadence asset must use the fixed universe or UNIVERSE")
        object.__setattr__(
            self,
            "scheduled_time_ns",
            epoch_nanoseconds(self.scheduled_time_ns, "scheduled_time_ns"),
        )
        if not isinstance(self.burst, bool):
            raise TypeError("burst must be boolean")


@dataclass(frozen=True, slots=True)
class CadenceObservation:
    anchor_id: str
    event_id: str
    capture_time_ns: int
    receive_time_ns: int
    available_time_ns: int
    quality_flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("anchor_id", "event_id"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        capture = epoch_nanoseconds(self.capture_time_ns, "capture_time_ns")
        receive = epoch_nanoseconds(self.receive_time_ns, "receive_time_ns")
        available = epoch_nanoseconds(self.available_time_ns, "available_time_ns")
        if not capture <= receive <= available:
            raise ValueError("cadence timestamps must satisfy capture <= receive <= availability")
        object.__setattr__(self, "capture_time_ns", capture)
        object.__setattr__(self, "receive_time_ns", receive)
        object.__setattr__(self, "available_time_ns", available)
        flags = tuple(sorted({_required_text(flag, "quality flag") for flag in self.quality_flags}))
        object.__setattr__(self, "quality_flags", flags)


@dataclass(frozen=True, slots=True)
class CadenceAudit:
    scheduled_anchors: int
    observed_anchors: int
    timely_anchors: int
    missing_anchor_ids: tuple[str, ...]
    late_anchor_ids: tuple[str, ...]
    quality_flagged_anchor_ids: tuple[str, ...]
    duplicate_anchor_ids: tuple[str, ...]

    @property
    def coverage_fraction(self) -> Decimal:
        return _ratio(self.observed_anchors, self.scheduled_anchors)

    @property
    def timely_fraction(self) -> Decimal:
        return _ratio(self.timely_anchors, self.observed_anchors)

    @property
    def passes_99_percent_gate(self) -> bool:
        threshold = Decimal("0.99")
        return (
            self.scheduled_anchors > 0
            and self.coverage_fraction >= threshold
            and self.timely_fraction >= threshold
            and not self.quality_flagged_anchor_ids
            and not self.duplicate_anchor_ids
        )

    @property
    def report_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self)).hexdigest()


def audit_cadence(
    anchors: tuple[CadenceAnchor, ...],
    observations: tuple[CadenceObservation, ...],
) -> CadenceAudit:
    anchor_ids = tuple(anchor.anchor_id for anchor in anchors)
    if len(anchor_ids) != len(set(anchor_ids)):
        raise ValueError("scheduled cadence anchor IDs must be unique")
    grouped: dict[str, list[CadenceObservation]] = {}
    for observation in observations:
        grouped.setdefault(observation.anchor_id, []).append(observation)
    unknown = set(grouped) - set(anchor_ids)
    if unknown:
        raise ValueError(f"observations reference unknown cadence anchors: {sorted(unknown)}")
    missing: list[str] = []
    late: list[str] = []
    flagged: list[str] = []
    duplicates: list[str] = []
    timely = 0
    observed = 0
    for anchor in anchors:
        values = grouped.get(anchor.anchor_id, ())
        if not values:
            missing.append(anchor.anchor_id)
            continue
        observed += 1
        if len(values) != 1:
            duplicates.append(anchor.anchor_id)
            continue
        observation = values[0]
        if observation.capture_time_ns < anchor.scheduled_time_ns:
            late.append(anchor.anchor_id)
            continue
        deadline = anchor.scheduled_time_ns + cadence_for_dataset(anchor.dataset).allowed_lateness_ns(
            burst=anchor.burst
        )
        if observation.available_time_ns > deadline:
            late.append(anchor.anchor_id)
            continue
        if observation.quality_flags:
            flagged.append(anchor.anchor_id)
            continue
        timely += 1
    return CadenceAudit(
        len(anchors),
        observed,
        timely,
        tuple(sorted(missing)),
        tuple(sorted(late)),
        tuple(sorted(flagged)),
        tuple(sorted(duplicates)),
    )


@dataclass(frozen=True, slots=True)
class DecisionAnchorEvidence:
    anchor_id: str
    canonical_asset: CanonicalAsset
    decision_time_ns: int
    binance_available_time_ns: int | None
    hyperliquid_available_time_ns: int | None
    freshness_limit_ns: int
    skew_limit_ns: int
    quality_flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "anchor_id", _required_text(self.anchor_id, "anchor_id"))
        if not isinstance(self.canonical_asset, CanonicalAsset):
            raise TypeError("decision anchor asset must use the fixed universe")
        object.__setattr__(
            self,
            "decision_time_ns",
            epoch_nanoseconds(self.decision_time_ns, "decision_time_ns"),
        )
        for name in ("binance_available_time_ns", "hyperliquid_available_time_ns"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, epoch_nanoseconds(value, name))
        for name in ("freshness_limit_ns", "skew_limit_ns"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative exact integer")
        flags = tuple(sorted({_required_text(flag, "quality flag") for flag in self.quality_flags}))
        object.__setattr__(self, "quality_flags", flags)


@dataclass(frozen=True, slots=True)
class DecisionAnchorAudit:
    anchors: int
    complete_anchors: int
    fresh_anchors: int
    missing_anchor_ids: tuple[str, ...]
    stale_or_skewed_anchor_ids: tuple[str, ...]
    future_join_anchor_ids: tuple[str, ...]
    quality_flagged_anchor_ids: tuple[str, ...]

    @property
    def coverage_fraction(self) -> Decimal:
        return _ratio(self.complete_anchors, self.anchors)

    @property
    def fresh_fraction(self) -> Decimal:
        return _ratio(self.fresh_anchors, self.complete_anchors)

    @property
    def passes_data_gate(self) -> bool:
        threshold = Decimal("0.99")
        return (
            self.anchors > 0
            and self.coverage_fraction >= threshold
            and self.fresh_fraction >= threshold
            and not self.future_join_anchor_ids
            and not self.quality_flagged_anchor_ids
        )

    @property
    def report_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self)).hexdigest()


def audit_decision_anchors(anchors: tuple[DecisionAnchorEvidence, ...]) -> DecisionAnchorAudit:
    ids = tuple(anchor.anchor_id for anchor in anchors)
    if len(ids) != len(set(ids)):
        raise ValueError("decision anchor IDs must be unique")
    missing: list[str] = []
    stale: list[str] = []
    future: list[str] = []
    flagged: list[str] = []
    complete = 0
    fresh = 0
    for anchor in anchors:
        binance = anchor.binance_available_time_ns
        hyperliquid = anchor.hyperliquid_available_time_ns
        if binance is None or hyperliquid is None:
            missing.append(anchor.anchor_id)
            continue
        complete += 1
        if binance > anchor.decision_time_ns or hyperliquid > anchor.decision_time_ns:
            future.append(anchor.anchor_id)
            continue
        if anchor.quality_flags:
            flagged.append(anchor.anchor_id)
            continue
        ages = (anchor.decision_time_ns - binance, anchor.decision_time_ns - hyperliquid)
        skew = abs(binance - hyperliquid)
        if max(ages) > anchor.freshness_limit_ns or skew > anchor.skew_limit_ns:
            stale.append(anchor.anchor_id)
            continue
        fresh += 1
    return DecisionAnchorAudit(
        len(anchors),
        complete,
        fresh,
        tuple(sorted(missing)),
        tuple(sorted(stale)),
        tuple(sorted(future)),
        tuple(sorted(flagged)),
    )


FundingKey = tuple[Venue, CanonicalAsset, str, int]


def _funding_key(value: FundingSettlement) -> FundingKey:
    return (value.venue, value.canonical_asset, value.contract_id, value.settlement_time_ns)


def _funding_economics(value: FundingSettlement) -> tuple[object, ...]:
    return (
        value.rate,
        value.settlement_price,
        value.price_kind,
        value.contract_multiplier,
    )


def _key_text(value: FundingKey) -> str:
    return f"{value[0].value}:{value[1].value}:{value[2]}:{value[3]}"


@dataclass(frozen=True, slots=True)
class FundingReconciliationReport:
    sampled_history_events: int
    matched_events: int
    missing_keys: tuple[str, ...]
    unexpected_keys: tuple[str, ...]
    mismatched_keys: tuple[str, ...]
    conflicting_collected_keys: tuple[str, ...]
    conflicting_history_keys: tuple[str, ...]

    @property
    def reconciled_fraction(self) -> Decimal:
        return _ratio(self.matched_events, self.sampled_history_events)

    @property
    def passes_100_percent_gate(self) -> bool:
        return (
            self.sampled_history_events > 0
            and self.reconciled_fraction == Decimal("1")
            and not self.missing_keys
            and not self.unexpected_keys
            and not self.mismatched_keys
            and not self.conflicting_collected_keys
            and not self.conflicting_history_keys
        )

    @property
    def report_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self)).hexdigest()


def _index_funding(
    values: tuple[FundingSettlement, ...],
) -> tuple[dict[FundingKey, FundingSettlement], tuple[str, ...]]:
    indexed: dict[FundingKey, FundingSettlement] = {}
    conflicts: set[str] = set()
    event_ids: set[str] = set()
    for value in values:
        if value.event_id in event_ids:
            conflicts.add(f"event_id:{value.event_id}")
        event_ids.add(value.event_id)
        key = _funding_key(value)
        existing = indexed.get(key)
        if existing is not None and _funding_economics(existing) != _funding_economics(value):
            conflicts.add(_key_text(key))
        indexed[key] = value
    return indexed, tuple(sorted(conflicts))


def reconcile_finalized_funding(
    collected: tuple[FundingSettlement, ...],
    venue_history: tuple[FundingSettlement, ...],
) -> FundingReconciliationReport:
    collected_index, collected_conflicts = _index_funding(collected)
    history_index, history_conflicts = _index_funding(venue_history)
    collected_keys = set(collected_index)
    history_keys = set(history_index)
    missing = history_keys - collected_keys
    unexpected = collected_keys - history_keys
    mismatched = {
        key
        for key in collected_keys & history_keys
        if _funding_economics(collected_index[key]) != _funding_economics(history_index[key])
    }
    matched = len((collected_keys & history_keys) - mismatched)
    return FundingReconciliationReport(
        sampled_history_events=len(history_index),
        matched_events=matched,
        missing_keys=tuple(sorted(_key_text(key) for key in missing)),
        unexpected_keys=tuple(sorted(_key_text(key) for key in unexpected)),
        mismatched_keys=tuple(sorted(_key_text(key) for key in mismatched)),
        conflicting_collected_keys=collected_conflicts,
        conflicting_history_keys=history_conflicts,
    )


__all__ = [
    "COLLECTION_CADENCE",
    "CadenceAnchor",
    "CadenceAudit",
    "CadenceContract",
    "CadenceMode",
    "CadenceObservation",
    "DecisionAnchorAudit",
    "DecisionAnchorEvidence",
    "FundingReconciliationReport",
    "NANOSECONDS_PER_HOUR",
    "NANOSECONDS_PER_MINUTE",
    "NANOSECONDS_PER_SECOND",
    "audit_cadence",
    "audit_decision_anchors",
    "cadence_for_dataset",
    "reconcile_finalized_funding",
]
