"""Tamper-evident paper/testnet soak observations and derived gate reports.

The journal deliberately stores observations rather than mutable counters.  A
report is always rebuilt from the verified hash chain, so elapsed time cannot
be supplied by a caller or inferred from file modification times.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence


ZERO_HASH = "0" * 64
SCHEMA_VERSION = 1


class SoakJournalError(ValueError):
    """Raised when the soak journal is malformed, discontinuous, or tampered."""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
        default=str,
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise SoakJournalError("observed_at must be a non-empty ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SoakJournalError(f"invalid observed_at timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise SoakJournalError("observed_at must include a timezone")
    return parsed.astimezone(timezone.utc)


def _record_hash(record: Mapping[str, Any]) -> str:
    unsigned = dict(record)
    unsigned.pop("record_sha256", None)
    return hashlib.sha256(canonical_bytes(unsigned)).hexdigest()


def verify_journal(directory: Path) -> list[dict[str, Any]]:
    """Read and fully verify a journal in sequence order."""

    if not directory.exists():
        return []
    paths = sorted(path for path in directory.glob("*.json") if path.is_file())
    records: list[dict[str, Any]] = []
    previous_hash = ZERO_HASH
    previous_time: datetime | None = None
    for expected_sequence, path in enumerate(paths, start=1):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SoakJournalError(f"cannot decode journal record {path.name}") from exc
        if not isinstance(record, dict):
            raise SoakJournalError(f"journal record {path.name} is not an object")
        if record.get("schema_version") != SCHEMA_VERSION:
            raise SoakJournalError(f"unsupported schema in {path.name}")
        if record.get("sequence") != expected_sequence:
            raise SoakJournalError(
                f"journal sequence discontinuity: expected {expected_sequence}, "
                f"observed {record.get('sequence')!r}"
            )
        observed_hash = record.get("record_sha256")
        calculated_hash = _record_hash(record)
        if observed_hash != calculated_hash:
            raise SoakJournalError(f"content hash mismatch in {path.name}")
        expected_name = f"{expected_sequence:08d}_{calculated_hash}.json"
        if path.name != expected_name:
            raise SoakJournalError(
                f"journal filename does not bind sequence and hash: {path.name}"
            )
        if record.get("previous_record_sha256") != previous_hash:
            raise SoakJournalError(f"broken previous-record link in {path.name}")
        observed_time = _parse_time(record.get("observed_at"))
        if previous_time is not None and observed_time <= previous_time:
            raise SoakJournalError("journal timestamps must be strictly increasing")
        previous_time = observed_time
        previous_hash = calculated_hash
        records.append(record)
    return records


@contextmanager
def _journal_lock(directory: Path) -> Iterator[None]:
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / ".append.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise SoakJournalError(
            f"journal append lock already exists: {lock}; inspect before removing it"
        ) from exc
    try:
        os.close(descriptor)
        yield
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


def append_observation(
    directory: Path,
    *,
    observed_at: datetime,
    environment: str,
    facts: Mapping[str, Any],
    source_refs: Sequence[Mapping[str, str]] = (),
) -> tuple[dict[str, Any], Path]:
    """Append one immutable observation after validating the complete chain."""

    normalized_environment = environment.strip().lower()
    if normalized_environment not in {"paper", "testnet"}:
        raise SoakJournalError("soak evidence is restricted to paper or testnet")
    if observed_at.tzinfo is None:
        raise SoakJournalError("observed_at must include a timezone")
    normalized_time = observed_at.astimezone(timezone.utc)

    with _journal_lock(directory):
        records = verify_journal(directory)
        if records and normalized_time <= _parse_time(records[-1]["observed_at"]):
            raise SoakJournalError("new observation time must follow the journal head")
        previous_hash = records[-1]["record_sha256"] if records else ZERO_HASH
        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "sequence": len(records) + 1,
            "event_type": "soak_observation",
            "observed_at": normalized_time.isoformat(),
            "environment": normalized_environment,
            "previous_record_sha256": previous_hash,
            "facts": dict(facts),
            "source_refs": [dict(ref) for ref in source_refs],
        }
        record["record_sha256"] = _record_hash(record)
        destination = directory / (
            f"{record['sequence']:08d}_{record['record_sha256']}.json"
        )
        temporary = directory / f".{destination.name}.tmp"
        with temporary.open("xb") as handle:
            handle.write(canonical_bytes(record) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(destination)
        return record, destination


def derive_metrics(
    records: Sequence[Mapping[str, Any]], *, max_observation_gap_seconds: float
) -> dict[str, Any]:
    """Derive Section K metrics without substituting missing observations."""

    if max_observation_gap_seconds <= 0:
        raise SoakJournalError("max_observation_gap_seconds must be positive")
    if not records:
        return {
            "consecutive_unattended_days": 0.0,
            "decision_service_readiness_pct": None,
            "critical_reconciliation_invariant_incidents": 0,
            "injected_gaps_detected_replayed_pct": None,
            "routine_auto_recovery_within_slo_pct": None,
            "unresolved_alerts": None,
        }

    times = [_parse_time(record.get("observed_at")) for record in records]
    ready_samples = 0
    critical_issue_ids: set[str] = set()
    gaps_injected = 0
    gaps_detected_replayed = 0
    recoveries_attempted = 0
    recoveries_within_slo = 0
    for record in records:
        facts = record.get("facts")
        facts = facts if isinstance(facts, dict) else {}
        decision = facts.get("decision_service")
        decision = decision if isinstance(decision, dict) else {}
        if decision.get("ready") is True:
            ready_samples += 1
        reconciliation = facts.get("reconciliation")
        reconciliation = reconciliation if isinstance(reconciliation, dict) else {}
        issue_ids = reconciliation.get("critical_issue_ids")
        if isinstance(issue_ids, list):
            critical_issue_ids.update(str(item) for item in issue_ids if str(item))
        fault = facts.get("fault_injection")
        fault = fault if isinstance(fault, dict) else {}
        gaps_injected += max(0, int(fault.get("gaps_injected") or 0))
        gaps_detected_replayed += max(
            0, int(fault.get("gaps_detected_replayed") or 0)
        )
        recovery = facts.get("routine_recovery")
        recovery = recovery if isinstance(recovery, dict) else {}
        recoveries_attempted += max(0, int(recovery.get("attempted") or 0))
        recoveries_within_slo += max(0, int(recovery.get("within_slo") or 0))

    # Only a contiguous, currently-green suffix counts.  The first sample has
    # zero duration; no report can mint historical soak time from one sample.
    suffix_start = len(records) - 1
    last_facts = records[-1].get("facts")
    last_facts = last_facts if isinstance(last_facts, dict) else {}
    last_eligible = last_facts.get("unattended_eligible") is True
    if last_eligible:
        while suffix_start > 0:
            current_facts = records[suffix_start - 1].get("facts")
            current_facts = current_facts if isinstance(current_facts, dict) else {}
            gap = (times[suffix_start] - times[suffix_start - 1]).total_seconds()
            if (
                current_facts.get("unattended_eligible") is not True
                or gap > max_observation_gap_seconds
            ):
                break
            suffix_start -= 1
        consecutive_seconds = (times[-1] - times[suffix_start]).total_seconds()
    else:
        consecutive_seconds = 0.0

    unresolved = last_facts.get("unresolved_alerts")
    unresolved_count = (
        max(0, int(unresolved))
        if isinstance(unresolved, (int, float)) and not isinstance(unresolved, bool)
        else None
    )
    return {
        "consecutive_unattended_days": round(consecutive_seconds / 86_400.0, 9),
        "decision_service_readiness_pct": round(
            100.0 * ready_samples / len(records), 9
        ),
        "critical_reconciliation_invariant_incidents": len(critical_issue_ids),
        "injected_gaps_detected_replayed_pct": (
            round(100.0 * gaps_detected_replayed / gaps_injected, 9)
            if gaps_injected
            else None
        ),
        "routine_auto_recovery_within_slo_pct": (
            round(100.0 * recoveries_within_slo / recoveries_attempted, 9)
            if recoveries_attempted
            else None
        ),
        "unresolved_alerts": unresolved_count,
    }


def build_report_bundle(
    records: Sequence[Mapping[str, Any]],
    *,
    journal_directory: Path,
    output_directory: Path,
    generated_at: datetime,
    max_observation_gap_seconds: float,
) -> tuple[dict[str, Any], Path]:
    """Write four immutable evidence views plus a machine-readable bundle."""

    if not records:
        raise SoakJournalError("at least one verified observation is required")
    output_directory.mkdir(parents=True, exist_ok=True)
    timestamp = generated_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    metrics = derive_metrics(
        records, max_observation_gap_seconds=max_observation_gap_seconds
    )
    head = str(records[-1]["record_sha256"])
    common = {
        "schema_version": SCHEMA_VERSION,
        "evidence_kind": "paper_testnet_soak",
        "generated_at": generated_at.astimezone(timezone.utc).isoformat(),
        "journal": {
            "directory": str(journal_directory.resolve()),
            "record_count": len(records),
            "head_sha256": head,
            "first_observed_at": records[0]["observed_at"],
            "last_observed_at": records[-1]["observed_at"],
            "max_observation_gap_seconds": max_observation_gap_seconds,
            "chain_verified": True,
        },
        "metrics": metrics,
    }
    views: dict[str, dict[str, Any]] = {
        "unattended_soak": {
            **common,
            "view": "unattended_soak",
            "observations": list(records),
        },
        "readiness_report": {
            **common,
            "view": "readiness_report",
            "decision_service_readiness_pct": metrics[
                "decision_service_readiness_pct"
            ],
        },
        "incident_log": {
            **common,
            "view": "incident_log",
            "latest_unresolved_alerts": metrics["unresolved_alerts"],
            "critical_reconciliation_invariant_incidents": metrics[
                "critical_reconciliation_invariant_incidents"
            ],
        },
        "fault_injection": {
            **common,
            "view": "fault_injection",
            "injected_gaps_detected_replayed_pct": metrics[
                "injected_gaps_detected_replayed_pct"
            ],
            "routine_auto_recovery_within_slo_pct": metrics[
                "routine_auto_recovery_within_slo_pct"
            ],
            "missing_measurements_are_not_assumed_successful": True,
        },
    }
    refs: list[dict[str, str]] = []
    for kind, payload in views.items():
        path = output_directory / f"soak_{kind}_{timestamp}_{head[:12]}.json"
        _write_new_json(path, payload)
        refs.append(
            {"kind": kind, "uri": str(path.resolve()), "sha256": sha256_file(path)}
        )
    bundle: dict[str, Any] = {
        **common,
        "machine_attestation": {
            "attested": True,
            "basis": "derived only from a fully verified append-only hash chain",
            "criteria_passed": all(
                (
                    metrics["consecutive_unattended_days"] >= 30.0,
                    isinstance(metrics["decision_service_readiness_pct"], (int, float))
                    and metrics["decision_service_readiness_pct"] >= 99.5,
                    metrics["critical_reconciliation_invariant_incidents"] == 0,
                    metrics["injected_gaps_detected_replayed_pct"] == 100.0,
                    isinstance(metrics["routine_auto_recovery_within_slo_pct"], (int, float))
                    and metrics["routine_auto_recovery_within_slo_pct"] >= 95.0,
                    metrics["unresolved_alerts"] == 0,
                )
            ),
        },
        "evidence_refs": refs,
    }
    bundle_path = output_directory / f"soak_bundle_{timestamp}_{head[:12]}.json"
    _write_new_json(bundle_path, bundle)
    return bundle, bundle_path


def _write_new_json(path: Path, value: Mapping[str, Any]) -> None:
    data = json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
