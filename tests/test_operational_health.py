from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bongus.engine.backup_set import VerifiedBackupSet, create_verified_backup_set
from bongus.engine.split_state_store import SplitStateWriter
from bongus.monitoring.progress_contract import (
    REQUIRED_PROGRESS_LOOPS,
    progress_loop_deadlines,
)
from scripts import check_operational_health as health
from tests.rust_recovery_support import FakeRustRecoveryHarness, rust_create_kwargs


def _write_backup_evidence(
    directory: Path,
) -> VerifiedBackupSet:
    data_root = directory.parent
    data_root.mkdir(exist_ok=True)
    writer = SplitStateWriter(
        state_path=str(data_root / "state.db"),
        audit_path=str(data_root / "audit.db"),
        research_path=str(data_root / "research.db"),
    )
    writer.close()
    (data_root / "live_config.json").write_text('{"pause_new_entries":true}\n', encoding="utf-8")
    rust_harness = FakeRustRecoveryHarness(data_root / "test-rust-recovery")
    return create_verified_backup_set(
        data_root,
        directory,
        **rust_create_kwargs(rust_harness),
    )


def _write_heartbeat(
    path: Path,
    *,
    updated_at: datetime,
    loop_heartbeat_ages: dict[str, float] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "pid": 1234,
                "session_id": "session-health-test",
                "runtime_mode": "SAFE_MODE",
                "updated_at": updated_at.isoformat(),
                "loop_heartbeat_ages": loop_heartbeat_ages or {name: 1.0 for name in REQUIRED_PROGRESS_LOOPS},
                "loop_heartbeat_deadlines": progress_loop_deadlines(),
            }
        ),
        encoding="utf-8",
    )


def _write_offsite_receipt(path: Path, *, completed_at: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rust_members = {
        name: {
            "restore_relative_path": restore_path,
            "sha256": "7" * 64,
            "size_bytes": 0,
        }
        for name, restore_path in {
            "execution_state": "execution_state.jsonl",
            "intent_journal": "execution_intents.jsonl",
            "telemetry_journal": "execution_telemetry.jsonl",
            "telemetry_ack_cursor": "execution_telemetry.jsonl.cursor.a",
            "private_cursor_spot": "private_stream_cursors/spot.jsonl",
            "private_cursor_futures": "private_stream_cursors/futures.jsonl",
        }.items()
    }
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "evidence_kind": "encrypted_offsite_backup_receipt",
                "completed_at": completed_at.isoformat(),
                "encrypted": True,
                "offsite": True,
                "repository_id_sha256": "a" * 64,
                "restic_binary_sha256": "6" * 64,
                "restic_version": "0.18.1",
                "repository_locator_sha256": "9" * 64,
                "repository_pin_verified": True,
                "snapshot_id": "b" * 64,
                "backup_set_id": "20260815T120000.000000Z-" + "e" * 32,
                "backup_set_completed_at": completed_at.isoformat(),
                "backup_set_manifest_sha256": "f" * 64,
                "backup_set_source_skew_seconds": 1.0,
                "mutable_rust_runtime_included": True,
                "restart_requires_exchange_reconciliation": True,
                "rust_recovery_generation": {
                    "created_at_ms": int(completed_at.timestamp() * 1_000),
                    "generation_id": "test-rust-generation",
                    "manifest_sha256": "8" * 64,
                    "manifest_size_bytes": 100,
                    "member_count": 6,
                    "members": rust_members,
                    "restore_policy": "empty_runtime_then_signed_reconciliation",
                    "total_size_bytes": 100,
                },
                "recovery_files": {"live_config.json": {"sha256": "f" * 64, "size_bytes": 10}},
                "source_backups": {
                    source_name: {
                        "source_created_at": completed_at.isoformat(),
                        "source_manifest_sha256": "c" * 64,
                        "source_backup_sha256": "d" * 64,
                    }
                    for source_name in ("state.db", "audit.db", "research.db")
                },
            }
        ),
        encoding="utf-8",
    )


def _write_retention_receipt(path: Path, *, completed_at: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "evidence_kind": "encrypted_offsite_retention_receipt",
                "completed_at": completed_at.isoformat(),
                "repository_id_sha256": "a" * 64,
                "restic_binary_sha256": "6" * 64,
                "restic_version": "0.18.1",
                "repository_backend": "s3",
                "repository_pin_verified": True,
                "policy": {
                    "keep_within": "24h",
                    "keep_daily": 30,
                    "keep_weekly": 12,
                    "keep_monthly": 12,
                },
                "stable_grouping": "tags",
                "maintenance_identity_separated": True,
                "maximum_duration_seconds": 240.0,
                "prune_completed": True,
            }
        ),
        encoding="utf-8",
    )


def _chrony_tracking(offset_seconds: float, *, leap_status: str = "Normal") -> str:
    direction = "fast" if offset_seconds >= 0.0 else "slow"
    return "\n".join(
        [
            "Reference ID    : 7F7F0101 (time-source)",
            "Stratum         : 3",
            f"System time     : {abs(offset_seconds):.9f} seconds {direction} of NTP time",
            f"Leap status     : {leap_status}",
        ]
    )


def test_backup_age_probe_is_read_only_and_accepts_fresh_verified_generation(
    tmp_path: Path,
) -> None:
    backup_set = _write_backup_evidence(tmp_path / "backups")
    now = backup_set.completed_at + timedelta(minutes=15)
    tracked_files = tuple(path for path in backup_set.manifest_path.parent.iterdir())
    before = {path.name: (path.stat().st_size, path.stat().st_mtime_ns) for path in tracked_files}

    check = health.check_backup_age(
        tmp_path / "backups",
        now=now,
        max_age_seconds=1_200.0,
    )

    after = {path.name: (path.stat().st_size, path.stat().st_mtime_ns) for path in tracked_files}
    assert check.status == health.PASS
    assert check.observed["database_payload_hashes_reverified"] is False
    assert before == after


def test_backup_age_probe_fails_when_latest_generation_exceeds_twenty_minutes(
    tmp_path: Path,
) -> None:
    backup_set = _write_backup_evidence(tmp_path / "backups")
    now = backup_set.completed_at + timedelta(minutes=21)

    check = health.check_backup_age(
        tmp_path / "backups",
        now=now,
        max_age_seconds=1_200.0,
    )

    assert check.status == health.CRITICAL
    assert check.observed["age_seconds"] >= 1_260.0


def test_backup_age_probe_requires_state_audit_and_research_generations(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "backups"
    backup_set = _write_backup_evidence(directory)
    now = backup_set.completed_at + timedelta(minutes=1)
    backup_set.backups["research.db"].manifest_path.unlink()

    check = health.check_backup_age(
        directory,
        now=now,
        max_age_seconds=1_200.0,
    )

    assert check.status == health.CRITICAL
    assert "complete verified split-store" in check.summary


def test_backup_age_probe_rejects_an_incomplete_manifest(tmp_path: Path) -> None:
    now = datetime(2026, 8, 15, 12, 30, tzinfo=timezone.utc)
    backup_directory = tmp_path / "backups"
    generation = backup_directory / "backup-set.incomplete"
    generation.mkdir(parents=True)
    (generation / "backup-set.incomplete.json").write_text("{}\n", encoding="utf-8")

    check = health.check_backup_age(
        backup_directory,
        now=now,
        max_age_seconds=1_200.0,
    )

    assert check.status == health.CRITICAL
    assert "complete verified split-store" in check.summary
    assert check.observed["invalid_manifests"]


def test_independent_heartbeat_requires_each_loop_and_advances_frozen_ages(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 15, 12, 30, tzinfo=timezone.utc)
    heartbeat = tmp_path / "runtime" / "runtime_heartbeat.json"
    _write_heartbeat(heartbeat, updated_at=now - timedelta(seconds=1))

    fresh = health.check_independent_heartbeat(
        heartbeat,
        now=now,
        max_age_seconds=125.0,
    )
    assert fresh.status == health.PASS

    _write_heartbeat(heartbeat, updated_at=now - timedelta(seconds=120))
    frozen = health.check_independent_heartbeat(
        heartbeat,
        now=now,
        max_age_seconds=125.0,
    )
    assert frozen.status == health.CRITICAL
    assert "liveness_loop" in frozen.observed["stale_loops"]

    _write_heartbeat(heartbeat, updated_at=now - timedelta(seconds=126))
    missed = health.check_independent_heartbeat(
        heartbeat,
        now=now,
        max_age_seconds=125.0,
    )
    assert missed.status == health.CRITICAL
    assert "two one-minute windows" in missed.summary

    _write_heartbeat(
        heartbeat,
        updated_at=now - timedelta(seconds=1),
        loop_heartbeat_ages={name: 121.0 if name == "trading_loop" else 1.0 for name in REQUIRED_PROGRESS_LOOPS},
    )
    stale_progress = health.check_independent_heartbeat(
        heartbeat,
        now=now,
        max_age_seconds=125.0,
    )
    assert stale_progress.status == health.CRITICAL
    assert stale_progress.observed["stale_loops"] == ["trading_loop"]

    _write_heartbeat(
        heartbeat,
        updated_at=now - timedelta(seconds=1),
        loop_heartbeat_ages={"liveness_loop": 1.0},
    )
    missing = health.check_independent_heartbeat(
        heartbeat,
        now=now,
        max_age_seconds=125.0,
    )
    assert missing.status == health.CRITICAL
    assert "exact required loop set" in missing.observed["error"]


def test_offsite_receipt_is_required_fresh_encrypted_and_remote(tmp_path: Path) -> None:
    now = datetime(2026, 8, 15, 12, 30, tzinfo=timezone.utc)
    receipt = tmp_path / "offsite" / "latest.json"
    _write_offsite_receipt(receipt, completed_at=now - timedelta(minutes=19))

    fresh = health.check_offsite_backup_receipt(
        receipt,
        now=now,
        max_age_seconds=1_200.0,
    )
    assert fresh.status == health.PASS

    _write_offsite_receipt(receipt, completed_at=now - timedelta(minutes=21))
    stale = health.check_offsite_backup_receipt(
        receipt,
        now=now,
        max_age_seconds=1_200.0,
    )
    assert stale.status == health.CRITICAL

    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["encrypted"] = False
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    unencrypted = health.check_offsite_backup_receipt(
        receipt,
        now=now,
        max_age_seconds=1_200.0,
    )
    assert unencrypted.status == health.CRITICAL

    _write_offsite_receipt(receipt, completed_at=now - timedelta(minutes=1))
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    for source in payload["source_backups"].values():
        source["source_created_at"] = (now - timedelta(hours=2)).isoformat()
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    stale_sources = health.check_offsite_backup_receipt(
        receipt,
        now=now,
        max_age_seconds=1_200.0,
    )
    assert stale_sources.status == health.CRITICAL


def test_offsite_retention_receipt_requires_bounded_current_prune(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 15, 12, 30, tzinfo=timezone.utc)
    receipt = tmp_path / "offsite" / "retention-latest.json"
    _write_retention_receipt(receipt, completed_at=now - timedelta(hours=47))
    assert (
        health.check_offsite_retention_receipt(
            receipt,
            now=now,
            max_age_seconds=172_800.0,
        ).status
        == health.PASS
    )

    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["policy"]["keep_daily"] = 31
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    assert (
        health.check_offsite_retention_receipt(
            receipt,
            now=now,
            max_age_seconds=172_800.0,
        ).status
        == health.CRITICAL
    )

    _write_retention_receipt(receipt, completed_at=now - timedelta(hours=1))
    assert (
        health.check_offsite_retention_receipt(
            receipt,
            now=now,
            max_age_seconds=172_800.0,
            expected_repository_id="b" * 64,
        ).status
        == health.CRITICAL
    )


@pytest.mark.parametrize(
    ("offset_seconds", "leap_status", "expected"),
    [
        (0.050, "Normal", health.PASS),
        (0.101, "Normal", health.WARNING),
        (-0.251, "Normal", health.CRITICAL),
        (0.001, "Not synchronised", health.CRITICAL),
    ],
)
def test_chrony_thresholds_are_fail_closed(
    offset_seconds: float,
    leap_status: str,
    expected: str,
) -> None:
    check = health.check_chrony_tracking(
        _chrony_tracking(offset_seconds, leap_status=leap_status),
        warning_offset_ms=100.0,
        critical_offset_ms=250.0,
    )

    assert check.status == expected


def test_cli_emits_one_read_only_machine_result(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    now = datetime.now(timezone.utc)
    backup_directory = tmp_path / "backups"
    _write_backup_evidence(backup_directory)
    heartbeat = tmp_path / "runtime" / "runtime_heartbeat.json"
    _write_heartbeat(heartbeat, updated_at=now - timedelta(seconds=1))
    offsite_receipt = tmp_path / "offsite" / "latest.json"
    _write_offsite_receipt(offsite_receipt, completed_at=now - timedelta(minutes=1))
    retention_receipt = tmp_path / "offsite" / "retention-latest.json"
    _write_retention_receipt(
        retention_receipt,
        completed_at=now - timedelta(hours=1),
    )
    tracking = tmp_path / "chrony-tracking.txt"
    tracking.write_text(_chrony_tracking(0.050), encoding="utf-8")
    before_names = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    exit_code = health.main(
        [
            "--backup-directory",
            str(backup_directory),
            "--heartbeat-path",
            str(heartbeat),
            "--offsite-receipt-path",
            str(offsite_receipt),
            "--offsite-retention-receipt-path",
            str(retention_receipt),
            "--chrony-tracking-file",
            str(tracking),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    after_names = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    assert exit_code == 0
    assert payload["overall_status"] == health.PASS
    assert payload["read_only"] is True
    assert {item["name"] for item in payload["checks"]} == {
        "backup_age",
        "clock_health",
        "independent_heartbeat",
        "offsite_backup",
        "offsite_retention",
    }
    assert before_names == after_names
