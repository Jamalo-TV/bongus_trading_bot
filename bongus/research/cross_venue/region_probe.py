"""Hash-chained evidence and deterministic Germany/France region selection.

This module is deliberately transport-agnostic.  Public network measurement is
implemented by :mod:`region_probe_network`; fixture ingestion, verification and
aggregation remain fully offline and deterministic here.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, fields
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import cast

from bongus.research.cross_venue.schema import (
    Venue,
    deterministic_event_id,
    epoch_nanoseconds,
    exact_wire,
)
from bongus.research.cross_venue.storage import canonical_json_bytes

PROBE_PROTOCOL_VERSION = "cross-venue-region-probe-v1"
NANOSECONDS_PER_SECOND = 1_000_000_000
NANOSECONDS_PER_HOUR = 3_600 * NANOSECONDS_PER_SECOND
MINIMUM_PROBE_DURATION_NS = 48 * NANOSECONDS_PER_HOUR
MAXIMUM_PROBE_DURATION_NS = 72 * NANOSECONDS_PER_HOUR
MAXIMUM_VENUE_WINDOW_SKEW_NS = 5 * 60 * NANOSECONDS_PER_SECOND
MAXIMUM_METRIC_EDGE_GAP_NS = NANOSECONDS_PER_HOUR
_ZERO_CHAIN_HASH = "0" * 64
_CHAIN_VERSION = 1


class RegionProbeError(RuntimeError):
    """Region evidence is malformed, conflicting, incomplete, or corrupted."""


class ProbeRegion(StrEnum):
    GERMANY = "germany"
    FRANCE = "france"


class ProbeMetric(StrEnum):
    RUN_START = "run_start"
    RUN_END = "run_end"
    REST_RTT = "rest_rtt"
    WS_EVENT_AGE = "ws_event_age"
    WS_JITTER = "ws_jitter"
    MESSAGE_WINDOW = "message_window"
    RECONNECT = "reconnect"
    GAP_RECOVERY = "gap_recovery"


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _sha256_text(value: str, field_name: str) -> str:
    normalized = _required_text(value, field_name).casefold()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError(f"{field_name} must be a SHA-256 hex digest")
    return normalized


def _nonnegative_integer(value: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be an exact non-negative integer")
    return value


def _optional_epoch(value: int | None, field_name: str) -> int | None:
    return None if value is None else epoch_nanoseconds(value, field_name)


@dataclass(frozen=True, slots=True)
class ProbeObservation:
    event_id: str
    run_id: str
    region: ProbeRegion
    probe_host_id: str
    venue: Venue
    metric: ProbeMetric
    capture_time_ns: int
    receive_time_ns: int
    available_time_ns: int
    code_sha256: str
    configuration_sha256: str
    value_ns: int | None = None
    source_event_time_ns: int | None = None
    expected_messages: int = 0
    received_messages: int = 0
    reconnect_count: int = 0
    gaps_detected: int = 0
    gaps_recovered: int = 0
    connection_id: str = "none"
    sequence_id: str = "none"
    quality_flags: tuple[str, ...] = ()
    protocol_version: str = PROBE_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.protocol_version != PROBE_PROTOCOL_VERSION:
            raise ValueError("probe observation protocol version is not fixed v1")
        if not isinstance(self.region, ProbeRegion) or not isinstance(self.venue, Venue):
            raise TypeError("probe region and venue must use fixed enums")
        if not isinstance(self.metric, ProbeMetric):
            raise TypeError("probe metric must use the fixed enum")
        for name in ("run_id", "probe_host_id", "connection_id", "sequence_id"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        capture = epoch_nanoseconds(self.capture_time_ns, "capture_time_ns")
        receive = epoch_nanoseconds(self.receive_time_ns, "receive_time_ns")
        available = epoch_nanoseconds(self.available_time_ns, "available_time_ns")
        if not capture <= receive <= available:
            raise ValueError("probe timestamps must satisfy capture <= receive <= availability")
        object.__setattr__(self, "capture_time_ns", capture)
        object.__setattr__(self, "receive_time_ns", receive)
        object.__setattr__(self, "available_time_ns", available)
        object.__setattr__(
            self,
            "source_event_time_ns",
            _optional_epoch(self.source_event_time_ns, "source_event_time_ns"),
        )
        if self.value_ns is not None:
            object.__setattr__(self, "value_ns", _nonnegative_integer(self.value_ns, "value_ns"))
        for name in (
            "expected_messages",
            "received_messages",
            "reconnect_count",
            "gaps_detected",
            "gaps_recovered",
        ):
            object.__setattr__(self, name, _nonnegative_integer(getattr(self, name), name))
        object.__setattr__(self, "code_sha256", _sha256_text(self.code_sha256, "code_sha256"))
        object.__setattr__(
            self,
            "configuration_sha256",
            _sha256_text(self.configuration_sha256, "configuration_sha256"),
        )
        flags = tuple(sorted({_required_text(flag, "quality flag") for flag in self.quality_flags}))
        object.__setattr__(self, "quality_flags", flags)
        self._validate_metric_contract()
        expected_event_id = self.expected_event_id
        if self.event_id != expected_event_id:
            raise ValueError("probe event_id does not match its exact immutable content")

    def _validate_metric_contract(self) -> None:
        counters = (
            self.expected_messages,
            self.received_messages,
            self.reconnect_count,
            self.gaps_detected,
            self.gaps_recovered,
        )
        if self.metric in {ProbeMetric.RUN_START, ProbeMetric.RUN_END}:
            if self.value_ns is not None or self.source_event_time_ns is not None or any(counters):
                raise ValueError("run boundary observations cannot carry measurements")
        elif self.metric is ProbeMetric.REST_RTT:
            if self.value_ns is None or self.value_ns <= 0 or self.source_event_time_ns is not None or any(counters):
                raise ValueError("REST RTT requires one positive duration only")
        elif self.metric is ProbeMetric.WS_EVENT_AGE:
            if self.value_ns is None or self.source_event_time_ns is None or any(counters):
                raise ValueError("WS event age requires a source time and one duration")
            if self.value_ns != max(0, self.receive_time_ns - self.source_event_time_ns):
                raise ValueError("WS event age must equal receive time minus exchange event time")
        elif self.metric is ProbeMetric.WS_JITTER:
            if self.value_ns is None or self.source_event_time_ns is not None or any(counters):
                raise ValueError("WS jitter requires one exact duration only")
        elif self.metric is ProbeMetric.MESSAGE_WINDOW:
            if (
                self.value_ns is not None
                or self.source_event_time_ns is not None
                or self.expected_messages <= 0
                or self.received_messages > self.expected_messages
                or any(counters[2:])
            ):
                raise ValueError("message window requires exact expected/received counts")
        elif self.metric is ProbeMetric.RECONNECT:
            if (
                self.value_ns is not None
                or self.source_event_time_ns is not None
                or self.reconnect_count <= 0
                or any((self.expected_messages, self.received_messages, self.gaps_detected, self.gaps_recovered))
            ):
                raise ValueError("reconnect observation requires a positive reconnect count")
        elif self.metric is ProbeMetric.GAP_RECOVERY:
            if (
                self.source_event_time_ns is not None
                or self.gaps_detected <= 0
                or self.gaps_recovered > self.gaps_detected
                or any((self.expected_messages, self.received_messages, self.reconnect_count))
            ):
                raise ValueError("gap recovery requires exact detected/recovered counts")
            if (self.gaps_recovered > 0) != (self.value_ns is not None):
                raise ValueError("recovered gaps require an exact recovery duration")

    @property
    def unsigned_payload(self) -> Mapping[str, object]:
        return cast(
            Mapping[str, object],
            exact_wire({field.name: getattr(self, field.name) for field in fields(self) if field.name != "event_id"}),
        )

    @property
    def expected_event_id(self) -> str:
        digest = hashlib.sha256(canonical_json_bytes(self.unsigned_payload)).hexdigest()
        return deterministic_event_id("region-probe", digest)

    @property
    def as_wire(self) -> Mapping[str, object]:
        return {"event_id": self.event_id, **self.unsigned_payload}

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        region: ProbeRegion,
        probe_host_id: str,
        venue: Venue,
        metric: ProbeMetric,
        capture_time_ns: int,
        receive_time_ns: int,
        available_time_ns: int,
        code_sha256: str,
        configuration_sha256: str,
        value_ns: int | None = None,
        source_event_time_ns: int | None = None,
        expected_messages: int = 0,
        received_messages: int = 0,
        reconnect_count: int = 0,
        gaps_detected: int = 0,
        gaps_recovered: int = 0,
        connection_id: str = "none",
        sequence_id: str = "none",
        quality_flags: tuple[str, ...] = (),
    ) -> ProbeObservation:
        values = {
            "run_id": run_id,
            "region": region,
            "probe_host_id": probe_host_id,
            "venue": venue,
            "metric": metric,
            "capture_time_ns": capture_time_ns,
            "receive_time_ns": receive_time_ns,
            "available_time_ns": available_time_ns,
            "code_sha256": code_sha256,
            "configuration_sha256": configuration_sha256,
            "value_ns": value_ns,
            "source_event_time_ns": source_event_time_ns,
            "expected_messages": expected_messages,
            "received_messages": received_messages,
            "reconnect_count": reconnect_count,
            "gaps_detected": gaps_detected,
            "gaps_recovered": gaps_recovered,
            "connection_id": connection_id,
            "sequence_id": sequence_id,
            "quality_flags": quality_flags,
        }
        unsigned = cast(Mapping[str, object], exact_wire({**values, "protocol_version": PROBE_PROTOCOL_VERSION}))
        digest = hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()
        return cls(
            event_id=deterministic_event_id("region-probe", digest),
            **values,
        )


def _integer_from_wire(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise RegionProbeError(f"{field_name} must be an exact integer")
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RegionProbeError(f"{field_name} must be an exact integer") from exc
    if not isinstance(value, int) and value.strip() != str(parsed):
        raise RegionProbeError(f"{field_name} must be an integer string")
    return parsed


def _event_from_wire(value: object) -> ProbeObservation:
    if not isinstance(value, Mapping):
        raise RegionProbeError("probe event must be an object")
    flags = value.get("quality_flags", ())
    if isinstance(flags, (str, bytes, bytearray)) or not isinstance(flags, Sequence):
        raise RegionProbeError("quality_flags must be an array")
    optional_value = value.get("value_ns")
    optional_source = value.get("source_event_time_ns")
    try:
        return ProbeObservation(
            event_id=_required_text(cast(str, value.get("event_id")), "event_id"),
            run_id=_required_text(cast(str, value.get("run_id")), "run_id"),
            region=ProbeRegion(value.get("region")),
            probe_host_id=_required_text(cast(str, value.get("probe_host_id")), "probe_host_id"),
            venue=Venue(value.get("venue")),
            metric=ProbeMetric(value.get("metric")),
            capture_time_ns=_integer_from_wire(value.get("capture_time_ns"), "capture_time_ns"),
            receive_time_ns=_integer_from_wire(value.get("receive_time_ns"), "receive_time_ns"),
            available_time_ns=_integer_from_wire(value.get("available_time_ns"), "available_time_ns"),
            code_sha256=_required_text(cast(str, value.get("code_sha256")), "code_sha256"),
            configuration_sha256=_required_text(
                cast(str, value.get("configuration_sha256")),
                "configuration_sha256",
            ),
            value_ns=(None if optional_value is None else _integer_from_wire(optional_value, "value_ns")),
            source_event_time_ns=(
                None if optional_source is None else _integer_from_wire(optional_source, "source_event_time_ns")
            ),
            expected_messages=_integer_from_wire(value.get("expected_messages", 0), "expected_messages"),
            received_messages=_integer_from_wire(value.get("received_messages", 0), "received_messages"),
            reconnect_count=_integer_from_wire(value.get("reconnect_count", 0), "reconnect_count"),
            gaps_detected=_integer_from_wire(value.get("gaps_detected", 0), "gaps_detected"),
            gaps_recovered=_integer_from_wire(value.get("gaps_recovered", 0), "gaps_recovered"),
            connection_id=_required_text(cast(str, value.get("connection_id", "none")), "connection_id"),
            sequence_id=_required_text(cast(str, value.get("sequence_id", "none")), "sequence_id"),
            quality_flags=tuple(_required_text(cast(str, flag), "quality flag") for flag in flags),
            protocol_version=_required_text(
                cast(str, value.get("protocol_version")),
                "protocol_version",
            ),
        )
    except (TypeError, ValueError) as exc:
        raise RegionProbeError(f"invalid probe event: {exc}") from exc


@dataclass(frozen=True, slots=True)
class ProbeLogVerification:
    path: Path
    events: tuple[ProbeObservation, ...]
    final_chain_sha256: str
    file_sha256: str

    @property
    def report_sha256(self) -> str:
        return hashlib.sha256(
            canonical_json_bytes(
                {
                    "event_ids": tuple(event.event_id for event in self.events),
                    "final_chain_sha256": self.final_chain_sha256,
                    "file_sha256": self.file_sha256,
                }
            )
        ).hexdigest()


def _load_exact_json(data: bytes, context: str) -> object:
    try:
        return json.loads(
            data.decode("utf-8"),
            parse_float=Decimal,
            parse_int=int,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RegionProbeError(f"invalid exact JSON in {context}") from exc


def verify_probe_log(path: str | Path) -> ProbeLogVerification:
    raw_path = Path(path).expanduser().absolute()
    if raw_path.is_symlink() or not raw_path.is_file():
        raise RegionProbeError("probe evidence must be an existing non-linked file")
    resolved = raw_path.resolve()
    raw_bytes = resolved.read_bytes()
    previous = _ZERO_CHAIN_HASH
    previous_available = -1
    events: list[ProbeObservation] = []
    identities: dict[str, ProbeObservation] = {}
    for line_number, line in enumerate(raw_bytes.splitlines(), start=1):
        if not line or len(line) > 1_000_000:
            raise RegionProbeError(f"invalid probe evidence line {line_number}")
        row = _load_exact_json(line, f"line {line_number}")
        if not isinstance(row, Mapping) or set(row) != {
            "chain_version",
            "previous_record_sha256",
            "event",
            "record_sha256",
        }:
            raise RegionProbeError(f"invalid chain envelope at line {line_number}")
        if row["chain_version"] != _CHAIN_VERSION or row["previous_record_sha256"] != previous:
            raise RegionProbeError(f"broken probe hash chain at line {line_number}")
        unsigned = {
            "chain_version": _CHAIN_VERSION,
            "previous_record_sha256": previous,
            "event": row["event"],
        }
        expected_hash = hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()
        if row["record_sha256"] != expected_hash:
            raise RegionProbeError(f"probe record hash mismatch at line {line_number}")
        event = _event_from_wire(row["event"])
        if event.available_time_ns < previous_available:
            raise RegionProbeError(f"probe evidence is not availability-ordered at line {line_number}")
        if event.event_id in identities:
            raise RegionProbeError(f"duplicate probe event ID at line {line_number}: {event.event_id}")
        identities[event.event_id] = event
        events.append(event)
        previous_available = event.available_time_ns
        previous = expected_hash
    return ProbeLogVerification(
        path=resolved,
        events=tuple(events),
        final_chain_sha256=previous,
        file_sha256=hashlib.sha256(raw_bytes).hexdigest(),
    )


class AppendOnlyProbeLog:
    """Single-writer hash-chained NDJSON log with fsync on every append."""

    def __init__(self, path: str | Path) -> None:
        raw_path = Path(path).expanduser().absolute()
        if raw_path.exists() and raw_path.is_symlink():
            raise RegionProbeError("probe evidence path cannot be a symbolic link")
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        self.path = raw_path.resolve()
        self._events: dict[str, ProbeObservation] = {}
        self._last_hash = _ZERO_CHAIN_HASH
        self._last_available_time_ns = -1
        self._size = 0
        if self.path.exists():
            verification = verify_probe_log(self.path)
            self._events = {event.event_id: event for event in verification.events}
            self._last_hash = verification.final_chain_sha256
            self._last_available_time_ns = verification.events[-1].available_time_ns if verification.events else -1
            self._size = self.path.stat().st_size

    def append(self, event: ProbeObservation) -> bool:
        existing = self._events.get(event.event_id)
        if existing is not None:
            if existing != event:
                raise RegionProbeError(f"conflicting immutable probe event: {event.event_id}")
            return False
        if event.available_time_ns < self._last_available_time_ns:
            raise RegionProbeError("probe evidence appends must be ordered by availability")
        current_size = self.path.stat().st_size if self.path.exists() else 0
        if current_size != self._size:
            raise RegionProbeError("probe evidence changed outside the single writer")
        unsigned = {
            "chain_version": _CHAIN_VERSION,
            "previous_record_sha256": self._last_hash,
            "event": event.as_wire,
        }
        record_hash = hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()
        encoded = canonical_json_bytes({**unsigned, "record_sha256": record_hash}) + b"\n"
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_BINARY", 0)
        descriptor = os.open(self.path, flags, 0o600)
        try:
            written = os.write(descriptor, encoded)
            if written != len(encoded):
                raise RegionProbeError("short append to probe evidence")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._events[event.event_id] = event
        self._last_hash = record_hash
        self._last_available_time_ns = event.available_time_ns
        self._size += len(encoded)
        return True

    def append_many(self, events: Iterable[ProbeObservation]) -> int:
        return sum(self.append(event) for event in events)

    def verify(self) -> ProbeLogVerification:
        return verify_probe_log(self.path)


@dataclass(frozen=True, slots=True)
class MetricDistribution:
    count: int
    minimum_ns: int
    p50_ns: int
    p95_ns: int
    p99_ns: int
    maximum_ns: int


def _distribution(values: Sequence[int]) -> MetricDistribution | None:
    if not values:
        return None
    ordered = tuple(sorted(_nonnegative_integer(value, "metric sample") for value in values))

    def percentile(percent: int) -> int:
        rank = max(1, (percent * len(ordered) + 99) // 100)
        return ordered[rank - 1]

    return MetricDistribution(
        count=len(ordered),
        minimum_ns=ordered[0],
        p50_ns=percentile(50),
        p95_ns=percentile(95),
        p99_ns=percentile(99),
        maximum_ns=ordered[-1],
    )


@dataclass(frozen=True, slots=True)
class VenueProbeSummary:
    venue: Venue
    start_time_ns: int | None
    end_time_ns: int | None
    duration_ns: int | None
    rest_rtt: MetricDistribution | None
    ws_event_age: MetricDistribution | None
    ws_jitter: MetricDistribution | None
    gap_recovery: MetricDistribution | None
    message_windows: int
    expected_messages: int
    received_messages: int
    loss_fraction: Decimal | None
    reconnect_count: int
    reconnects_per_hour: Decimal | None
    gaps_detected: int
    gaps_recovered: int
    worst_latency_p99_ns: int | None
    eligible: bool
    incomplete_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RegionProbeSummary:
    region: ProbeRegion
    run_id: str | None
    probe_host_id: str | None
    code_sha256: str | None
    configuration_sha256: str | None
    venues: tuple[VenueProbeSummary, ...]
    worst_venue_p99_ns: int | None
    eligible: bool
    incomplete_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProbeEvidenceReference:
    evidence_file_sha256: str
    evidence_chain_sha256: str
    evidence_verification_sha256: str
    event_count: int


@dataclass(frozen=True, slots=True)
class RegionSelectionReport:
    evidence_inputs: tuple[ProbeEvidenceReference, ...]
    regions: tuple[RegionProbeSummary, ...]
    selected_region: ProbeRegion | None
    selected_worst_venue_p99_ns: int | None
    selection_rule: str
    duration_gate: str
    status: str
    incomplete_reasons: tuple[str, ...]
    grants_live_authority: bool = False

    @property
    def unsigned_payload(self) -> Mapping[str, object]:
        return cast(Mapping[str, object], exact_wire(self))

    @property
    def report_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.unsigned_payload)).hexdigest()

    @property
    def as_wire(self) -> Mapping[str, object]:
        return {**self.unsigned_payload, "report_sha256": self.report_sha256}


def _venue_summary(events: Sequence[ProbeObservation], venue: Venue) -> VenueProbeSummary:
    relevant = tuple(event for event in events if event.venue is venue)
    reasons: list[str] = []
    starts = tuple(event.capture_time_ns for event in relevant if event.metric is ProbeMetric.RUN_START)
    ends = tuple(event.capture_time_ns for event in relevant if event.metric is ProbeMetric.RUN_END)
    if len(starts) != 1 or len(ends) != 1:
        reasons.append("requires_exactly_one_run_start_and_end")
        start = starts[0] if len(starts) == 1 else None
        end = ends[0] if len(ends) == 1 else None
    else:
        start, end = starts[0], ends[0]
    duration = end - start if start is not None and end is not None and end >= start else None
    if duration is None or not MINIMUM_PROBE_DURATION_NS <= duration <= MAXIMUM_PROBE_DURATION_NS:
        reasons.append("duration_outside_inclusive_48_to_72_hours")

    rest = _distribution(tuple(cast(int, event.value_ns) for event in relevant if event.metric is ProbeMetric.REST_RTT))
    ages = _distribution(
        tuple(cast(int, event.value_ns) for event in relevant if event.metric is ProbeMetric.WS_EVENT_AGE)
    )
    jitter = _distribution(
        tuple(cast(int, event.value_ns) for event in relevant if event.metric is ProbeMetric.WS_JITTER)
    )
    recoveries = _distribution(
        tuple(
            cast(int, event.value_ns)
            for event in relevant
            if event.metric is ProbeMetric.GAP_RECOVERY and event.value_ns is not None
        )
    )
    for name, distribution in (
        ("rest_rtt", rest),
        ("ws_event_age", ages),
        ("ws_jitter", jitter),
        ("gap_recovery", recoveries),
    ):
        if distribution is None:
            reasons.append(f"missing_{name}_samples")

    if start is not None and end is not None and end >= start:
        for metric in (
            ProbeMetric.REST_RTT,
            ProbeMetric.WS_EVENT_AGE,
            ProbeMetric.WS_JITTER,
            ProbeMetric.MESSAGE_WINDOW,
        ):
            times = tuple(event.available_time_ns for event in relevant if event.metric is metric)
            if (
                not times
                or min(times) > start + MAXIMUM_METRIC_EDGE_GAP_NS
                or max(times) < end - MAXIMUM_METRIC_EDGE_GAP_NS
            ):
                reasons.append(f"{metric.value}_does_not_span_probe_window")

    windows = tuple(event for event in relevant if event.metric is ProbeMetric.MESSAGE_WINDOW)
    expected = sum(event.expected_messages for event in windows)
    received = sum(event.received_messages for event in windows)
    if not windows or expected <= 0:
        reasons.append("missing_message_windows")
        loss = None
    else:
        loss = Decimal(expected - received) / Decimal(expected)
    reconnects = sum(event.reconnect_count for event in relevant if event.metric is ProbeMetric.RECONNECT)
    reconnects_per_hour = (
        Decimal(reconnects) * Decimal(NANOSECONDS_PER_HOUR) / Decimal(duration)
        if duration is not None and duration > 0
        else None
    )
    gaps_detected = sum(event.gaps_detected for event in relevant if event.metric is ProbeMetric.GAP_RECOVERY)
    gaps_recovered = sum(event.gaps_recovered for event in relevant if event.metric is ProbeMetric.GAP_RECOVERY)
    if gaps_detected <= 0:
        reasons.append("missing_gap_recovery_exercise")
    elif gaps_recovered != gaps_detected:
        reasons.append("unrecovered_gaps")
    flags = sorted({flag for event in relevant for flag in event.quality_flags})
    if flags:
        reasons.append("quality_flags:" + ",".join(flags))
    worst = max(rest.p99_ns, ages.p99_ns) if rest is not None and ages is not None else None
    return VenueProbeSummary(
        venue=venue,
        start_time_ns=start,
        end_time_ns=end,
        duration_ns=duration,
        rest_rtt=rest,
        ws_event_age=ages,
        ws_jitter=jitter,
        gap_recovery=recoveries,
        message_windows=len(windows),
        expected_messages=expected,
        received_messages=received,
        loss_fraction=loss,
        reconnect_count=reconnects,
        reconnects_per_hour=reconnects_per_hour,
        gaps_detected=gaps_detected,
        gaps_recovered=gaps_recovered,
        worst_latency_p99_ns=worst,
        eligible=not reasons,
        incomplete_reasons=tuple(sorted(set(reasons))),
    )


def _region_summary(events: Sequence[ProbeObservation], region: ProbeRegion) -> RegionProbeSummary:
    relevant = tuple(event for event in events if event.region is region)
    reasons: list[str] = []
    run_ids = sorted({event.run_id for event in relevant})
    host_ids = sorted({event.probe_host_id for event in relevant})
    code_hashes = sorted({event.code_sha256 for event in relevant})
    configuration_hashes = sorted({event.configuration_sha256 for event in relevant})
    if len(run_ids) != 1:
        reasons.append("requires_exactly_one_run_id")
    if len(host_ids) != 1:
        reasons.append("requires_exactly_one_probe_host")
    if len(code_hashes) != 1:
        reasons.append("mixed_code_hashes")
    if len(configuration_hashes) != 1:
        reasons.append("mixed_configuration_hashes")
    venue_summaries = tuple(_venue_summary(relevant, venue) for venue in Venue)
    if any(not summary.eligible for summary in venue_summaries):
        reasons.append("venue_evidence_incomplete")
    starts = tuple(summary.start_time_ns for summary in venue_summaries if summary.start_time_ns is not None)
    ends = tuple(summary.end_time_ns for summary in venue_summaries if summary.end_time_ns is not None)
    if (
        len(starts) != len(tuple(Venue))
        or len(ends) != len(tuple(Venue))
        or max(starts) - min(starts) > MAXIMUM_VENUE_WINDOW_SKEW_NS
        or max(ends) - min(ends) > MAXIMUM_VENUE_WINDOW_SKEW_NS
    ):
        reasons.append("venue_probe_windows_not_aligned_within_five_minutes")
    worst_values = tuple(
        summary.worst_latency_p99_ns for summary in venue_summaries if summary.worst_latency_p99_ns is not None
    )
    worst = max(worst_values) if len(worst_values) == len(tuple(Venue)) else None
    return RegionProbeSummary(
        region=region,
        run_id=run_ids[0] if len(run_ids) == 1 else None,
        probe_host_id=host_ids[0] if len(host_ids) == 1 else None,
        code_sha256=code_hashes[0] if len(code_hashes) == 1 else None,
        configuration_sha256=configuration_hashes[0] if len(configuration_hashes) == 1 else None,
        venues=venue_summaries,
        worst_venue_p99_ns=worst,
        eligible=not reasons,
        incomplete_reasons=tuple(sorted(set(reasons))),
    )


def evaluate_region_evidence(
    verification: ProbeLogVerification | Sequence[ProbeLogVerification],
) -> RegionSelectionReport:
    verifications = (verification,) if isinstance(verification, ProbeLogVerification) else tuple(verification)
    if not verifications:
        raise RegionProbeError("region selection requires at least one verified evidence log")
    events_by_id: dict[str, ProbeObservation] = {}
    for item in verifications:
        for event in item.events:
            previous = events_by_id.get(event.event_id)
            if previous is not None:
                if previous != event:
                    raise RegionProbeError(f"conflicting event across probe logs: {event.event_id}")
                raise RegionProbeError(f"duplicate event across probe logs: {event.event_id}")
            events_by_id[event.event_id] = event
    events = tuple(sorted(events_by_id.values(), key=lambda event: (event.available_time_ns, event.event_id)))
    regions = tuple(_region_summary(events, region) for region in ProbeRegion)
    reasons: list[str] = []
    if any(not region.eligible for region in regions):
        reasons.append("both_regions_require_complete_eligible_evidence")
    code_hashes = {region.code_sha256 for region in regions if region.code_sha256 is not None}
    configuration_hashes = {
        region.configuration_sha256 for region in regions if region.configuration_sha256 is not None
    }
    if len(code_hashes) != 1:
        reasons.append("regions_must_use_identical_code")
    if len(configuration_hashes) != 1:
        reasons.append("regions_must_use_identical_probe_configuration")
    host_ids = {region.probe_host_id for region in regions if region.probe_host_id is not None}
    if len(host_ids) != len(tuple(ProbeRegion)):
        reasons.append("regions_require_distinct_probe_hosts")
    selected: ProbeRegion | None = None
    selected_score: int | None = None
    if not reasons:
        order = {ProbeRegion.GERMANY: 0, ProbeRegion.FRANCE: 1}
        best = min(
            regions,
            key=lambda item: (
                cast(int, item.worst_venue_p99_ns),
                order[item.region],
            ),
        )
        selected = best.region
        selected_score = best.worst_venue_p99_ns
    return RegionSelectionReport(
        evidence_inputs=tuple(
            sorted(
                (
                    ProbeEvidenceReference(
                        evidence_file_sha256=item.file_sha256,
                        evidence_chain_sha256=item.final_chain_sha256,
                        evidence_verification_sha256=item.report_sha256,
                        event_count=len(item.events),
                    )
                    for item in verifications
                ),
                key=lambda item: item.evidence_verification_sha256,
            )
        ),
        regions=regions,
        selected_region=selected,
        selected_worst_venue_p99_ns=selected_score,
        selection_rule=(
            "minimize max_over_venues(max(rest_rtt_p99_ns,ws_event_age_p99_ns)); Germany wins an exact tie"
        ),
        duration_gate="each venue in each region must contain one inclusive 48h-to-72h run",
        status="selected" if selected is not None else "evidence_incomplete",
        incomplete_reasons=tuple(sorted(set(reasons))),
        grants_live_authority=False,
    )


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def write_region_selection_report(report: RegionSelectionReport, path: str | Path) -> Path:
    output = Path(path).expanduser().absolute()
    if output.exists() and output.is_symlink():
        raise RegionProbeError("region report path cannot be a symbolic link")
    encoded = canonical_json_bytes(report.as_wire) + b"\n"
    if output.exists():
        if output.read_bytes() != encoded:
            raise RegionProbeError("refusing to overwrite a different immutable region report")
    else:
        _atomic_write(output, encoded)
    return output.resolve()


def verify_region_selection_report(path: str | Path) -> Mapping[str, object]:
    report_path = Path(path).expanduser().absolute()
    if report_path.is_symlink() or not report_path.is_file():
        raise RegionProbeError("region selection report is missing or linked")
    payload = _load_exact_json(report_path.read_bytes(), "region selection report")
    if not isinstance(payload, Mapping):
        raise RegionProbeError("region selection report must be an object")
    declared = payload.get("report_sha256")
    unsigned = {key: value for key, value in payload.items() if key != "report_sha256"}
    actual = hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()
    if declared != actual:
        raise RegionProbeError("region selection report hash mismatch")
    return payload


def load_probe_fixture(path: str | Path) -> tuple[ProbeObservation, ...]:
    fixture_path = Path(path).expanduser().absolute()
    if fixture_path.is_symlink() or not fixture_path.is_file():
        raise RegionProbeError("probe fixture must be an existing non-linked file")
    root = _load_exact_json(fixture_path.read_bytes(), "probe fixture")
    values: object = root.get("observations") if isinstance(root, Mapping) else root
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, Sequence):
        raise RegionProbeError("probe fixture observations must be an array")
    result: list[ProbeObservation] = []
    for index, value in enumerate(values):
        if not isinstance(value, Mapping):
            raise RegionProbeError(f"probe fixture observation {index} must be an object")
        if "event_id" in value:
            result.append(_event_from_wire(value))
            continue
        # Construct through the factory so hand-authored fixtures do not need
        # to duplicate the content-derived event ID.
        flags = value.get("quality_flags", ())
        if isinstance(flags, (str, bytes, bytearray)) or not isinstance(flags, Sequence):
            raise RegionProbeError("quality_flags must be an array")
        optional_value = value.get("value_ns")
        optional_source = value.get("source_event_time_ns")
        result.append(
            ProbeObservation.create(
                run_id=_required_text(cast(str, value.get("run_id")), "run_id"),
                region=ProbeRegion(value.get("region")),
                probe_host_id=_required_text(
                    cast(str, value.get("probe_host_id")),
                    "probe_host_id",
                ),
                venue=Venue(value.get("venue")),
                metric=ProbeMetric(value.get("metric")),
                capture_time_ns=_integer_from_wire(value.get("capture_time_ns"), "capture_time_ns"),
                receive_time_ns=_integer_from_wire(value.get("receive_time_ns"), "receive_time_ns"),
                available_time_ns=_integer_from_wire(value.get("available_time_ns"), "available_time_ns"),
                code_sha256=_required_text(cast(str, value.get("code_sha256")), "code_sha256"),
                configuration_sha256=_required_text(
                    cast(str, value.get("configuration_sha256")),
                    "configuration_sha256",
                ),
                value_ns=(None if optional_value is None else _integer_from_wire(optional_value, "value_ns")),
                source_event_time_ns=(
                    None if optional_source is None else _integer_from_wire(optional_source, "source_event_time_ns")
                ),
                expected_messages=_integer_from_wire(
                    value.get("expected_messages", 0),
                    "expected_messages",
                ),
                received_messages=_integer_from_wire(
                    value.get("received_messages", 0),
                    "received_messages",
                ),
                reconnect_count=_integer_from_wire(
                    value.get("reconnect_count", 0),
                    "reconnect_count",
                ),
                gaps_detected=_integer_from_wire(value.get("gaps_detected", 0), "gaps_detected"),
                gaps_recovered=_integer_from_wire(
                    value.get("gaps_recovered", 0),
                    "gaps_recovered",
                ),
                connection_id=_required_text(
                    cast(str, value.get("connection_id", "none")),
                    "connection_id",
                ),
                sequence_id=_required_text(
                    cast(str, value.get("sequence_id", "none")),
                    "sequence_id",
                ),
                quality_flags=tuple(_required_text(cast(str, flag), "quality flag") for flag in flags),
            )
        )
    return tuple(result)


__all__ = [
    "AppendOnlyProbeLog",
    "MAXIMUM_PROBE_DURATION_NS",
    "MINIMUM_PROBE_DURATION_NS",
    "MetricDistribution",
    "PROBE_PROTOCOL_VERSION",
    "ProbeEvidenceReference",
    "ProbeLogVerification",
    "ProbeMetric",
    "ProbeObservation",
    "ProbeRegion",
    "RegionProbeError",
    "RegionProbeSummary",
    "RegionSelectionReport",
    "VenueProbeSummary",
    "evaluate_region_evidence",
    "load_probe_fixture",
    "verify_probe_log",
    "verify_region_selection_report",
    "write_region_selection_report",
]
