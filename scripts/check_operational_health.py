"""Read-only backup, clock, and independent heartbeat validation.

This command never writes state, starts services, sends alerts, or contacts an
exchange.  It emits one JSON document and uses exit status 0/1/2 for
PASS/WARNING/CRITICAL so systemd or a separate host monitor can page according
to operator policy.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import stat
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from bongus.engine.backup_set import BACKUP_SET_FORMAT, REQUIRED_DATABASES, verify_backup_set
from bongus.engine.database_backup import BackupError
from bongus.monitoring.progress_contract import (
    REQUIRED_PROGRESS_LOOPS,
    effective_reported_loop_ages,
    progress_loop_deadlines,
)

PASS = "PASS"
WARNING = "WARNING"
CRITICAL = "CRITICAL"
_STATUS_RANK = {PASS: 0, WARNING: 1, CRITICAL: 2}
_REPARSE_POINT_ATTRIBUTE = 0x0400
_CHRONY_SYSTEM_TIME = re.compile(
    r"^System time\s*:\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s+seconds\s+"
    r"(fast|slow)\s+of\s+NTP time\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_CHRONY_LEAP_STATUS = re.compile(
    r"^Leap status\s*:\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_CHRONY_STRATUM = re.compile(
    r"^Stratum\s*:\s*(\d+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass(frozen=True, slots=True)
class OperationalCheck:
    name: str
    status: str
    summary: str
    observed: dict[str, Any]
    required: dict[str, Any]


def _utc_timestamp(value: Any, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _regular_unlinked_file(path: Path, description: str) -> Path:
    candidate = path.absolute()
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise ValueError(f"{description} is unavailable: {candidate}") from exc
    is_reparse = bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT_ATTRIBUTE)
    if candidate.is_symlink() or is_reparse or not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{description} must be a regular non-link file: {candidate}")
    return candidate.resolve(strict=True)


def _regular_unlinked_directory(path: Path, description: str) -> Path:
    candidate = path.absolute()
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise ValueError(f"{description} is unavailable: {candidate}") from exc
    is_reparse = bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT_ATTRIBUTE)
    if candidate.is_symlink() or is_reparse or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"{description} must be a regular non-link directory: {candidate}")
    return candidate.resolve(strict=True)


def check_backup_age(
    backup_directory: Path,
    *,
    now: datetime,
    max_age_seconds: float,
    required_source_names: Sequence[str] = REQUIRED_DATABASES,
) -> OperationalCheck:
    required_sources = tuple(sorted(set(required_source_names)))
    required = {
        "maximum_age_seconds": float(max_age_seconds),
        "manifest_format": BACKUP_SET_FORMAT,
        "complete_verified_set_present": True,
        "required_source_names": list(required_sources),
    }
    try:
        directory = _regular_unlinked_directory(
            backup_directory,
            "backup directory",
        )
        valid = []
        invalid: list[str] = []
        for manifest_candidate in sorted(directory.glob("backup-set.*/backup-set.*.json")):
            try:
                valid.append(verify_backup_set(manifest_candidate, deep=False))
            except (BackupError, OSError, ValueError) as exc:
                invalid.append(f"{manifest_candidate.name}: {exc}")
        if not valid:
            return OperationalCheck(
                name="backup_age",
                status=CRITICAL,
                summary="a complete verified split-store backup set is missing",
                observed={
                    "backup_directory": str(directory),
                    "invalid_manifests": invalid,
                },
                required=required,
            )
        latest = max(valid, key=lambda item: (item.completed_at, item.set_id))
        if set(latest.backups) != set(required_sources):
            return OperationalCheck(
                name="backup_age",
                status=CRITICAL,
                summary="the latest backup set does not contain every required source",
                observed={
                    "backup_directory": str(directory),
                    "set_id": latest.set_id,
                    "source_names": sorted(latest.backups),
                },
                required=required,
            )
        source_observations = {
            source_name: {
                "manifest_path": str(result.manifest_path),
                "backup_path": str(result.backup_path),
                "created_at": result.manifest.created_at,
                "age_seconds": (
                    now.astimezone(timezone.utc) - _utc_timestamp(result.manifest.created_at, "created_at")
                ).total_seconds(),
            }
            for source_name, result in sorted(latest.backups.items())
        }
        oldest_source = max(
            source_observations,
            key=lambda source_name: source_observations[source_name]["age_seconds"],
        )
        age_seconds = float(source_observations[oldest_source]["age_seconds"])
        if any(float(observation["age_seconds"]) < -5.0 for observation in source_observations.values()):
            status = CRITICAL
            summary = "a required backup timestamp is in the future"
        elif any(float(observation["age_seconds"]) > max_age_seconds for observation in source_observations.values()):
            status = CRITICAL
            summary = "a required verified backup is stale"
        elif invalid:
            status = WARNING
            summary = "latest backup set is fresh, but invalid set manifests exist"
        else:
            status = PASS
            summary = "latest complete backup set is within the age limit"
        return OperationalCheck(
            name="backup_age",
            status=status,
            summary=summary,
            observed={
                "backup_directory": str(directory),
                "set_id": latest.set_id,
                "set_manifest_path": str(latest.manifest_path),
                "set_completed_at": latest.completed_at.isoformat(),
                "source_skew_seconds": latest.source_skew_seconds,
                "total_size_bytes": latest.total_size_bytes,
                "sources": source_observations,
                "oldest_source_name": oldest_source,
                "age_seconds": age_seconds,
                "valid_set_count": len(valid),
                "invalid_manifests": invalid,
                "manifest_hashes_reverified": True,
                "database_payload_hashes_reverified": False,
            },
            required=required,
        )
    except (OSError, ValueError) as exc:
        return OperationalCheck(
            name="backup_age",
            status=CRITICAL,
            summary="backup age could not be established",
            observed={"error": str(exc)},
            required=required,
        )


def check_offsite_backup_receipt(
    receipt_path: Path,
    *,
    now: datetime,
    max_age_seconds: float,
) -> OperationalCheck:
    required = {
        "maximum_age_seconds": float(max_age_seconds),
        "encrypted": True,
        "offsite": True,
        "snapshot_id_present": True,
        "source_hashes_present": True,
        "required_source_names": ["audit.db", "research.db", "state.db"],
        "complete_backup_set_bound": True,
        "coherent_rust_journal_snapshot_bound": True,
        "repository_config_id_pin_verified": True,
        "restic_binary_identity_pin_verified": True,
    }
    try:
        path = _regular_unlinked_file(receipt_path, "offsite backup receipt")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("offsite receipt root is not an object")
        if payload.get("schema_version") != 1:
            raise ValueError("offsite receipt schema_version must be 1")
        if payload.get("evidence_kind") != "encrypted_offsite_backup_receipt":
            raise ValueError("offsite receipt evidence_kind is invalid")
        if payload.get("encrypted") is not True or payload.get("offsite") is not True:
            raise ValueError("offsite receipt must attest encrypted remote storage")
        completed_at = _utc_timestamp(payload.get("completed_at"), "completed_at")
        age_seconds = (now.astimezone(timezone.utc) - completed_at).total_seconds()
        repository_id = str(payload.get("repository_id_sha256") or "").casefold()
        if re.fullmatch(r"[0-9a-f]{64}", repository_id) is None:
            raise ValueError("offsite receipt contains a malformed SHA-256 digest")
        restic_binary_sha256 = str(payload.get("restic_binary_sha256") or "").casefold()
        if re.fullmatch(r"[0-9a-f]{64}", restic_binary_sha256) is None:
            raise ValueError("offsite receipt lacks a pinned Restic executable hash")
        restic_version = str(payload.get("restic_version") or "")
        if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", restic_version) is None:
            raise ValueError("offsite receipt lacks a pinned final Restic version")
        if payload.get("repository_pin_verified") is not True:
            raise ValueError("offsite receipt lacks a verified Restic repository pin")
        repository_locator_hash = str(payload.get("repository_locator_sha256") or "").casefold()
        if re.fullmatch(r"[0-9a-f]{64}", repository_locator_hash) is None:
            raise ValueError("offsite receipt contains a malformed repository locator hash")
        backup_set_id = str(payload.get("backup_set_id") or "")
        if re.fullmatch(r"[0-9]{8}T[0-9]{6}\.[0-9]{6}Z-[0-9a-f]{32}", backup_set_id) is None:
            raise ValueError("offsite receipt backup-set identity is malformed")
        backup_set_hash = str(payload.get("backup_set_manifest_sha256") or "").casefold()
        if re.fullmatch(r"[0-9a-f]{64}", backup_set_hash) is None:
            raise ValueError("offsite receipt backup-set hash is malformed")
        backup_set_completed_at = _utc_timestamp(payload.get("backup_set_completed_at"), "backup_set_completed_at")
        if backup_set_completed_at > completed_at:
            raise ValueError("offsite backup set cannot postdate its upload receipt")
        if (now.astimezone(timezone.utc) - backup_set_completed_at).total_seconds() > max_age_seconds:
            raise ValueError("offsite receipt binds a stale backup set")
        if payload.get("mutable_rust_runtime_included") is not True:
            raise ValueError("offsite receipt lacks a coherent Rust journal/cursor snapshot")
        if payload.get("restart_requires_exchange_reconciliation") is not True:
            raise ValueError("offsite receipt must require signed reconciliation after restore")
        raw_rust = payload.get("rust_recovery_generation")
        rust_keys = {
            "created_at_ms",
            "generation_id",
            "manifest_sha256",
            "manifest_size_bytes",
            "member_count",
            "members",
            "restore_policy",
            "total_size_bytes",
        }
        if not isinstance(raw_rust, dict) or set(raw_rust) != rust_keys:
            raise ValueError("offsite Rust recovery evidence is malformed")
        rust_generation_id = str(raw_rust.get("generation_id") or "")
        if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", rust_generation_id) is None:
            raise ValueError("offsite Rust recovery generation ID is invalid")
        rust_manifest_hash = str(raw_rust.get("manifest_sha256") or "").casefold()
        if re.fullmatch(r"[0-9a-f]{64}", rust_manifest_hash) is None:
            raise ValueError("offsite Rust recovery manifest hash is malformed")
        rust_created_at_ms = raw_rust.get("created_at_ms")
        if isinstance(rust_created_at_ms, bool) or not isinstance(rust_created_at_ms, int) or rust_created_at_ms <= 0:
            raise ValueError("offsite Rust recovery timestamp is invalid")
        rust_created_at = datetime.fromtimestamp(
            rust_created_at_ms / 1_000.0,
            tz=timezone.utc,
        )
        rust_age_seconds = (now.astimezone(timezone.utc) - rust_created_at).total_seconds()
        if rust_created_at > completed_at or rust_age_seconds < -5.0 or rust_age_seconds > max_age_seconds:
            raise ValueError("offsite Rust recovery generation is stale or future-dated")
        raw_rust_members = raw_rust.get("members")
        required_rust_members = {
            "execution_state",
            "intent_journal",
            "telemetry_journal",
            "telemetry_ack_cursor",
            "private_cursor_spot",
            "private_cursor_futures",
        }
        if (
            raw_rust.get("member_count") != len(required_rust_members)
            or not isinstance(raw_rust_members, dict)
            or set(raw_rust_members) != required_rust_members
            or raw_rust.get("restore_policy") != "empty_runtime_then_signed_reconciliation"
        ):
            raise ValueError("offsite Rust recovery member/policy contract is invalid")
        rust_member_total = 0
        for rust_member_name, raw_member in raw_rust_members.items():
            if not isinstance(raw_member, dict) or set(raw_member) != {
                "restore_relative_path",
                "sha256",
                "size_bytes",
            }:
                raise ValueError(f"offsite Rust member {rust_member_name} is malformed")
            member_hash = str(raw_member.get("sha256") or "").casefold()
            member_size = raw_member.get("size_bytes")
            restore_path = raw_member.get("restore_relative_path")
            if (
                re.fullmatch(r"[0-9a-f]{64}", member_hash) is None
                or isinstance(member_size, bool)
                or not isinstance(member_size, int)
                or member_size < 0
                or not isinstance(restore_path, str)
                or not restore_path
                or Path(restore_path).is_absolute()
                or ".." in Path(restore_path).parts
            ):
                raise ValueError(f"offsite Rust member {rust_member_name} evidence is invalid")
            rust_member_total += member_size
        rust_manifest_size = raw_rust.get("manifest_size_bytes")
        rust_total_size = raw_rust.get("total_size_bytes")
        if (
            isinstance(rust_manifest_size, bool)
            or not isinstance(rust_manifest_size, int)
            or rust_manifest_size <= 0
            or isinstance(rust_total_size, bool)
            or not isinstance(rust_total_size, int)
            or rust_total_size != rust_manifest_size + rust_member_total
        ):
            raise ValueError("offsite Rust recovery size accounting is inconsistent")
        raw_recovery_files = payload.get("recovery_files")
        if not isinstance(raw_recovery_files, dict) or "live_config.json" not in raw_recovery_files:
            raise ValueError("offsite receipt lacks bound recovery configuration")
        raw_sources = payload.get("source_backups")
        if not isinstance(raw_sources, dict) or set(raw_sources) != {
            "state.db",
            "audit.db",
            "research.db",
        }:
            raise ValueError("offsite receipt must bind all operational databases")
        source_backups: dict[str, dict[str, Any]] = {}
        for source_name, raw_source in raw_sources.items():
            if not isinstance(raw_source, dict):
                raise ValueError("offsite source backup record must be an object")
            manifest_hash = str(raw_source.get("source_manifest_sha256") or "").casefold()
            backup_hash = str(raw_source.get("source_backup_sha256") or "").casefold()
            if any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in (manifest_hash, backup_hash)):
                raise ValueError("offsite source record contains a malformed hash")
            source_created_at = _utc_timestamp(
                raw_source.get("source_created_at"),
                "source_created_at",
            )
            if source_created_at > completed_at:
                raise ValueError("offsite source backup cannot postdate its upload receipt")
            source_age_seconds = (now.astimezone(timezone.utc) - source_created_at).total_seconds()
            if source_age_seconds < -5.0 or source_age_seconds > max_age_seconds:
                raise ValueError("offsite receipt binds a stale or future source backup")
            source_backups[str(source_name)] = {
                "source_manifest_sha256": manifest_hash,
                "source_backup_sha256": backup_hash,
                "source_created_at": source_created_at.isoformat(),
                "source_age_seconds": source_age_seconds,
            }
        snapshot_id = str(payload.get("snapshot_id") or "").casefold()
        if re.fullmatch(r"[0-9a-f]{64}", snapshot_id) is None:
            raise ValueError("offsite receipt snapshot_id is malformed")
        if age_seconds < -5.0:
            status = CRITICAL
            summary = "offsite backup receipt timestamp is in the future"
        elif age_seconds > max_age_seconds:
            status = CRITICAL
            summary = "encrypted offsite backup receipt is stale"
        else:
            status = PASS
            summary = "encrypted offsite backup receipt is within the age limit"
        return OperationalCheck(
            name="offsite_backup",
            status=status,
            summary=summary,
            observed={
                "receipt_path": str(path),
                "completed_at": completed_at.isoformat(),
                "age_seconds": age_seconds,
                "snapshot_id": snapshot_id,
                "repository_id_sha256": repository_id,
                "restic_binary_sha256": restic_binary_sha256,
                "restic_version": restic_version,
                "repository_locator_sha256": repository_locator_hash,
                "backup_set_id": backup_set_id,
                "backup_set_manifest_sha256": backup_set_hash,
                "rust_recovery_generation_id": rust_generation_id,
                "rust_recovery_manifest_sha256": rust_manifest_hash,
                "rust_recovery_age_seconds": rust_age_seconds,
                "source_backups": source_backups,
            },
            required=required,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return OperationalCheck(
            name="offsite_backup",
            status=CRITICAL,
            summary="encrypted offsite backup receipt could not be validated",
            observed={"error": str(exc)},
            required=required,
        )


def check_offsite_retention_receipt(
    receipt_path: Path,
    *,
    now: datetime,
    max_age_seconds: float,
    expected_repository_id: str | None = None,
) -> OperationalCheck:
    required_policy = {
        "keep_within": "24h",
        "keep_daily": 30,
        "keep_weekly": 12,
        "keep_monthly": 12,
    }
    required = {
        "maximum_age_seconds": float(max_age_seconds),
        "repository_config_id_pin_verified": True,
        "restic_binary_identity_pin_verified": True,
        "prune_completed": True,
        "maintenance_identity_separated": True,
        "stable_grouping": "tags",
        "maximum_duration_seconds": 240.0,
        "policy": required_policy,
    }
    try:
        path = _regular_unlinked_file(
            receipt_path,
            "offsite retention receipt",
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("offsite retention receipt root is not an object")
        if payload.get("schema_version") != 1 or payload.get("evidence_kind") != "encrypted_offsite_retention_receipt":
            raise ValueError("offsite retention receipt identity is invalid")
        if payload.get("repository_pin_verified") is not True:
            raise ValueError("offsite retention repository pin is unverified")
        if payload.get("prune_completed") is not True:
            raise ValueError("offsite retention prune is incomplete")
        if payload.get("policy") != required_policy:
            raise ValueError("offsite retention policy differs from the bounded policy")
        if payload.get("maintenance_identity_separated") is not True:
            raise ValueError("offsite retention does not attest a separate delete identity")
        if payload.get("stable_grouping") != "tags":
            raise ValueError("offsite retention does not use stable tag grouping")
        if payload.get("maximum_duration_seconds") != 240.0:
            raise ValueError("offsite retention duration bound is invalid")
        repository_id = str(payload.get("repository_id_sha256") or "").casefold()
        if re.fullmatch(r"[0-9a-f]{64}", repository_id) is None:
            raise ValueError("offsite retention repository ID is malformed")
        restic_binary_sha256 = str(payload.get("restic_binary_sha256") or "").casefold()
        if re.fullmatch(r"[0-9a-f]{64}", restic_binary_sha256) is None:
            raise ValueError("offsite retention receipt lacks a pinned Restic executable hash")
        restic_version = str(payload.get("restic_version") or "")
        if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", restic_version) is None:
            raise ValueError("offsite retention receipt lacks a pinned final Restic version")
        if expected_repository_id is not None and repository_id != expected_repository_id:
            raise ValueError("offsite upload and retention receipts bind different repositories")
        completed_at = _utc_timestamp(payload.get("completed_at"), "completed_at")
        age_seconds = (now.astimezone(timezone.utc) - completed_at).total_seconds()
        if age_seconds < -5.0 or age_seconds > max_age_seconds:
            raise ValueError("offsite retention maintenance is stale or future-dated")
        return OperationalCheck(
            name="offsite_retention",
            status=PASS,
            summary="encrypted-offsite retention maintenance is current",
            observed={
                "receipt_path": str(path),
                "completed_at": completed_at.isoformat(),
                "age_seconds": age_seconds,
                "repository_id_sha256": repository_id,
                "restic_binary_sha256": restic_binary_sha256,
                "restic_version": restic_version,
                "policy": required_policy,
            },
            required=required,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return OperationalCheck(
            name="offsite_retention",
            status=CRITICAL,
            summary="encrypted-offsite retention could not be validated",
            observed={"error": str(exc)},
            required=required,
        )


def check_independent_heartbeat(
    heartbeat_path: Path,
    *,
    now: datetime,
    max_age_seconds: float,
) -> OperationalCheck:
    expected_deadlines = progress_loop_deadlines(os.getenv("BONGUS_TRADING_LOOP_MAX_AGE_SECONDS"))
    required = {
        "maximum_age_seconds": float(max_age_seconds),
        "session_id_present": True,
        "pid_positive": True,
        "required_progress_loops": list(REQUIRED_PROGRESS_LOOPS),
        "loop_deadlines_seconds": expected_deadlines,
    }
    try:
        path = _regular_unlinked_file(heartbeat_path, "runtime heartbeat")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("runtime heartbeat root is not an object")
        timestamp_value = payload.get("updated_at") or payload.get("loop_last_alive_at")
        updated_at = _utc_timestamp(timestamp_value, "heartbeat updated_at")
        age_seconds = (now.astimezone(timezone.utc) - updated_at).total_seconds()
        session_id = str(payload.get("session_id") or "").strip()
        try:
            pid = int(payload.get("pid") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("heartbeat pid must be a positive integer") from exc
        if not session_id:
            raise ValueError("heartbeat session_id is missing")
        if pid <= 0:
            raise ValueError("heartbeat pid must be positive")
        loop_ages = payload.get("loop_heartbeat_ages")
        if not isinstance(loop_ages, dict) or not loop_ages:
            raise ValueError("heartbeat independent progress map is missing")
        if set(loop_ages) != set(REQUIRED_PROGRESS_LOOPS):
            raise ValueError("heartbeat progress map lacks an exact required loop set")
        reported_deadlines = payload.get("loop_heartbeat_deadlines")
        if reported_deadlines != expected_deadlines:
            raise ValueError("heartbeat loop deadlines do not match the shared contract")
        normalized_loop_ages = effective_reported_loop_ages(
            loop_ages,
            report_staleness_seconds=max(0.0, age_seconds),
        )
        stale_loops = sorted(name for name, age in normalized_loop_ages.items() if age > expected_deadlines[name])

        if age_seconds < -5.0:
            status = CRITICAL
            summary = "runtime heartbeat timestamp is in the future"
        elif age_seconds > max_age_seconds:
            status = CRITICAL
            summary = "independent runtime heartbeat missed two one-minute windows"
        elif stale_loops:
            status = CRITICAL
            summary = "one or more independent runtime progress loops are stale"
        else:
            status = PASS
            summary = "independent runtime heartbeat is fresh"
        return OperationalCheck(
            name="independent_heartbeat",
            status=status,
            summary=summary,
            observed={
                "heartbeat_path": str(path),
                "updated_at": updated_at.isoformat(),
                "age_seconds": age_seconds,
                "pid": pid,
                "session_id": session_id,
                "runtime_mode": payload.get("runtime_mode"),
                "loop_heartbeat_ages": normalized_loop_ages,
                "loop_heartbeat_deadlines": expected_deadlines,
                "stale_loops": stale_loops,
            },
            required=required,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return OperationalCheck(
            name="independent_heartbeat",
            status=CRITICAL,
            summary="independent runtime heartbeat could not be validated",
            observed={"error": str(exc)},
            required=required,
        )


def check_chrony_tracking(
    tracking_output: str,
    *,
    warning_offset_ms: float,
    critical_offset_ms: float,
) -> OperationalCheck:
    required = {
        "synchronized": True,
        "warning_above_offset_ms": float(warning_offset_ms),
        "critical_above_offset_ms": float(critical_offset_ms),
    }
    system_match = _CHRONY_SYSTEM_TIME.search(tracking_output)
    leap_match = _CHRONY_LEAP_STATUS.search(tracking_output)
    stratum_match = _CHRONY_STRATUM.search(tracking_output)
    if system_match is None or leap_match is None or stratum_match is None:
        return OperationalCheck(
            name="clock_health",
            status=CRITICAL,
            summary="chrony tracking output is incomplete",
            observed={"tracking_output": tracking_output[-2_000:]},
            required=required,
        )

    seconds = float(system_match.group(1))
    direction = system_match.group(2).lower()
    signed_seconds = seconds if direction == "fast" else -seconds
    offset_ms = abs(signed_seconds) * 1_000.0
    leap_status = leap_match.group(1).strip()
    stratum = int(stratum_match.group(1))
    synchronized = leap_status.casefold() == "normal" and stratum > 0
    if not synchronized:
        status = CRITICAL
        summary = "chrony reports an unsynchronized clock"
    elif not math.isfinite(offset_ms) or offset_ms > critical_offset_ms:
        status = CRITICAL
        summary = "clock offset exceeds the entry-block threshold"
    elif offset_ms > warning_offset_ms:
        status = WARNING
        summary = "clock offset exceeds the warning threshold"
    else:
        status = PASS
        summary = "chrony is synchronized within the warning threshold"
    return OperationalCheck(
        name="clock_health",
        status=status,
        summary=summary,
        observed={
            "signed_offset_ms": signed_seconds * 1_000.0,
            "absolute_offset_ms": offset_ms,
            "leap_status": leap_status,
            "stratum": stratum,
            "synchronized": synchronized,
        },
        required=required,
    )


def _read_chrony_tracking(
    *,
    tracking_file: Path | None,
    chronyc_binary: str,
    timeout_seconds: float,
) -> str:
    if tracking_file is not None:
        path = _regular_unlinked_file(tracking_file, "chrony tracking file")
        return path.read_text(encoding="utf-8")
    try:
        completed = subprocess.run(
            [chronyc_binary, "-n", "tracking"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"chronyc tracking failed: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise ValueError(f"chronyc tracking exited {completed.returncode}: {detail[:500]}")
    return completed.stdout


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0.0:
        raise argparse.ArgumentTypeError("value must be positive and finite")
    return parsed


def _parser() -> argparse.ArgumentParser:
    data_root = Path(os.getenv("BONGUS_DATA_ROOT", str(Path(__file__).resolve().parents[1]))).resolve()
    parser = argparse.ArgumentParser(description="Read-only operational backup, clock, and heartbeat validation")
    parser.add_argument(
        "--backup-directory",
        type=Path,
        default=data_root / "backups",
    )
    parser.add_argument(
        "--heartbeat-path",
        type=Path,
        default=data_root / "runtime" / "runtime_heartbeat.json",
    )
    parser.add_argument(
        "--offsite-receipt-path",
        type=Path,
        default=data_root / "offsite" / "upload" / "latest.json",
    )
    parser.add_argument(
        "--offsite-retention-receipt-path",
        type=Path,
        default=data_root / "offsite" / "maintenance" / "latest.json",
    )
    parser.add_argument(
        "--max-backup-age-seconds",
        type=_positive_float,
        default=900.0,
    )
    parser.add_argument(
        "--max-heartbeat-age-seconds",
        type=_positive_float,
        default=125.0,
    )
    parser.add_argument(
        "--max-offsite-age-seconds",
        type=_positive_float,
        default=900.0,
    )
    parser.add_argument(
        "--max-offsite-retention-age-seconds",
        type=_positive_float,
        default=172_800.0,
    )
    parser.add_argument(
        "--clock-warning-offset-ms",
        type=_positive_float,
        default=100.0,
    )
    parser.add_argument(
        "--clock-critical-offset-ms",
        type=_positive_float,
        default=250.0,
    )
    parser.add_argument("--chronyc-binary", default="chronyc")
    parser.add_argument(
        "--chrony-tracking-file",
        type=Path,
        help="read captured chronyc tracking output instead of invoking chronyc",
    )
    parser.add_argument(
        "--chrony-timeout-seconds",
        type=_positive_float,
        default=5.0,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.clock_critical_offset_ms <= args.clock_warning_offset_ms:
        parser.error("critical clock offset must exceed warning clock offset")

    now = datetime.now(timezone.utc)
    offsite_check = check_offsite_backup_receipt(
        args.offsite_receipt_path,
        now=now,
        max_age_seconds=args.max_offsite_age_seconds,
    )
    expected_repository_id = (
        str(offsite_check.observed.get("repository_id_sha256")) if offsite_check.status == PASS else None
    )
    checks = [
        check_backup_age(
            args.backup_directory,
            now=now,
            max_age_seconds=args.max_backup_age_seconds,
        ),
        check_independent_heartbeat(
            args.heartbeat_path,
            now=now,
            max_age_seconds=args.max_heartbeat_age_seconds,
        ),
        offsite_check,
        check_offsite_retention_receipt(
            args.offsite_retention_receipt_path,
            now=now,
            max_age_seconds=args.max_offsite_retention_age_seconds,
            expected_repository_id=expected_repository_id,
        ),
    ]
    try:
        tracking_output = _read_chrony_tracking(
            tracking_file=args.chrony_tracking_file,
            chronyc_binary=args.chronyc_binary,
            timeout_seconds=args.chrony_timeout_seconds,
        )
        checks.append(
            check_chrony_tracking(
                tracking_output,
                warning_offset_ms=args.clock_warning_offset_ms,
                critical_offset_ms=args.clock_critical_offset_ms,
            )
        )
    except (OSError, ValueError) as exc:
        checks.append(
            OperationalCheck(
                name="clock_health",
                status=CRITICAL,
                summary="chrony tracking could not be read",
                observed={"error": str(exc)},
                required={
                    "synchronized": True,
                    "warning_above_offset_ms": args.clock_warning_offset_ms,
                    "critical_above_offset_ms": args.clock_critical_offset_ms,
                },
            )
        )

    overall = max(checks, key=lambda item: _STATUS_RANK[item.status]).status
    payload = {
        "schema_version": 1,
        "generated_at": now.isoformat(),
        "overall_status": overall,
        "read_only": True,
        "checks": [asdict(check) for check in checks],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return _STATUS_RANK[overall]


if __name__ == "__main__":
    raise SystemExit(main())
