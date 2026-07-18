from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from bongus.engine.database_backup import (
    BackupError,
    create_verified_backup,
    restore_verified_backup,
    run_restore_drill,
    verify_backup,
)


def _create_database(path: Path, values: tuple[str, ...] = ("one", "two")) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=7")
        connection.execute("PRAGMA application_id=1112495955")
        connection.execute(
            "CREATE TABLE events (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.executemany("INSERT INTO events(value) VALUES (?)", ((value,) for value in values))
        connection.commit()


def _values(path: Path) -> list[str]:
    with sqlite3.connect(path) as connection:
        return [str(row[0]) for row in connection.execute("SELECT value FROM events ORDER BY id")]


def test_online_backup_includes_committed_wal_and_verifies(tmp_path: Path) -> None:
    source = tmp_path / "state.db"
    _create_database(source)
    writer = sqlite3.connect(source)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("INSERT INTO events(value) VALUES ('wal-row')")
        writer.commit()

        result = create_verified_backup(source, tmp_path / "backups")
    finally:
        writer.close()

    assert result.backup_path.exists()
    assert result.manifest_path.exists()
    assert result.manifest.integrity_check == "ok"
    assert result.manifest.schema_user_version == 7
    assert result.manifest.application_id == 1112495955
    assert result.manifest.table_row_counts == {"events": 3}
    assert _values(result.backup_path) == ["one", "two", "wal-row"]
    assert verify_backup(result.manifest_path).manifest.sha256 == result.manifest.sha256


def test_checksum_tamper_is_rejected_before_restore(tmp_path: Path) -> None:
    source = tmp_path / "state.db"
    _create_database(source)
    result = create_verified_backup(source, tmp_path / "backups")

    with result.backup_path.open("ab") as handle:
        handle.write(b"tamper")

    with pytest.raises(BackupError, match="size mismatch|checksum mismatch"):
        verify_backup(result.manifest_path)
    with pytest.raises(BackupError):
        restore_verified_backup(result.manifest_path, tmp_path / "restored.db")


def test_manifest_path_traversal_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "state.db"
    _create_database(source)
    result = create_verified_backup(source, tmp_path / "backups")
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    payload["backup_filename"] = "../state.db"
    result.manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(BackupError, match="unsafe backup filename"):
        verify_backup(result.manifest_path)


def test_restore_new_database_and_drill_match_manifest(tmp_path: Path) -> None:
    source = tmp_path / "state.db"
    _create_database(source, ("alpha", "beta", "gamma"))
    backup = create_verified_backup(source, tmp_path / "backups")

    restored = restore_verified_backup(backup.manifest_path, tmp_path / "restored.db")
    drill = run_restore_drill(backup.manifest_path, tmp_path / "drill")

    assert _values(restored.restored_path) == ["alpha", "beta", "gamma"]
    assert _values(drill.restored_path) == ["alpha", "beta", "gamma"]
    assert restored.table_row_counts == backup.manifest.table_row_counts
    assert drill.table_row_counts == backup.manifest.table_row_counts


def test_restore_refuses_implicit_overwrite_and_preserves_pre_restore_backup(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _create_database(source, ("new",))
    _create_database(target, ("old",))
    backup = create_verified_backup(source, tmp_path / "backups")

    with pytest.raises(BackupError, match="pass replace=True"):
        restore_verified_backup(backup.manifest_path, target)
    with pytest.raises(BackupError, match="confirm_quiesced"):
        restore_verified_backup(backup.manifest_path, target, replace=True)

    result = restore_verified_backup(
        backup.manifest_path,
        target,
        replace=True,
        confirm_quiesced=True,
    )

    assert _values(target) == ["new"]
    assert result.pre_restore_backup_path is not None
    assert _values(result.pre_restore_backup_path) == ["old"]


def test_restore_refuses_active_writer_lock(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _create_database(source, ("new",))
    _create_database(target, ("old",))
    backup = create_verified_backup(source, tmp_path / "backups")
    writer = sqlite3.connect(target, isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        with pytest.raises(BackupError, match="not quiesced"):
            restore_verified_backup(
                backup.manifest_path,
                target,
                replace=True,
                confirm_quiesced=True,
            )
        with pytest.raises(BackupError, match="not quiesced"):
            restore_verified_backup(
                backup.manifest_path,
                target,
                replace=True,
                confirm_quiesced=True,
                quarantine_corrupt_target=True,
            )
    finally:
        writer.execute("ROLLBACK")
        writer.close()


@pytest.mark.parametrize("corruption", ["primary", "wal"])
def test_explicit_corrupt_target_quarantine_preserves_evidence_and_restores(
    tmp_path: Path,
    corruption: str,
) -> None:
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _create_database(source, ("recovered",))
    _create_database(target, ("old",))
    backup = create_verified_backup(source, tmp_path / "backups")

    corrupt_path = target if corruption == "primary" else Path(f"{target}-wal")
    corrupt_path.write_bytes(b"not-a-sqlite-file")
    with pytest.raises(BackupError):
        restore_verified_backup(
            backup.manifest_path,
            target,
            replace=True,
            confirm_quiesced=True,
        )

    restored = restore_verified_backup(
        backup.manifest_path,
        target,
        replace=True,
        confirm_quiesced=True,
        quarantine_corrupt_target=True,
    )
    assert _values(target) == ["recovered"]
    assert restored.pre_restore_backup_path is None
    assert restored.quarantined_corrupt_files
    assert all(path.exists() for path in restored.quarantined_corrupt_files)
    assert any(path.name == corrupt_path.name for path in restored.quarantined_corrupt_files)


def test_cli_backup_and_verify_emit_machine_readable_output(tmp_path: Path) -> None:
    source = tmp_path / "state.db"
    _create_database(source)
    project_root = Path(__file__).resolve().parents[1]
    command = [
        sys.executable,
        str(project_root / "backup_db.py"),
        "backup",
        "--source",
        str(source),
        "--destination",
        str(tmp_path / "backups"),
    ]

    completed = subprocess.run(command, cwd=project_root, check=True, capture_output=True, text=True)
    payload = json.loads(completed.stdout)
    verified = subprocess.run(
        [sys.executable, str(project_root / "backup_db.py"), "verify", payload["manifest_path"]],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    )

    assert payload["status"] == "verified"
    assert json.loads(verified.stdout)["status"] == "verified"

    evidence_path = tmp_path / "restore-drill-evidence.json"
    drilled = subprocess.run(
        [
            sys.executable,
            str(project_root / "backup_db.py"),
            "drill",
            payload["manifest_path"],
            "--directory",
            str(tmp_path / "drills"),
            "--evidence-output",
            str(evidence_path),
        ],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    )
    drill_payload = json.loads(drilled.stdout)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert drill_payload["status"] == "restore_drill_passed"
    assert evidence["machine_attestation"]["attested"] is True
    assert evidence["source_backup_sha256"] == evidence["restored_sha256"]
    assert evidence["table_row_counts"] == payload["table_row_counts"]
