from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import backup_db
import bongus.engine.database_backup as database_backup_module
from bongus.engine.database_backup import (
    DEFAULT_BACKUP_BUDGET_BYTES,
    DEFAULT_PEAK_HEADROOM_BYTES,
    BackupError,
    create_verified_backup,
    prune_verified_backups,
    restore_verified_backup,
    run_restore_drill,
    verify_backup,
)


def _create_database(path: Path, values: tuple[str, ...] = ("one", "two")) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=7")
        connection.execute("PRAGMA application_id=1112495955")
        connection.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        connection.executemany("INSERT INTO events(value) VALUES (?)", ((value,) for value in values))
        connection.commit()


def _values(path: Path) -> list[str]:
    with sqlite3.connect(path) as connection:
        return [str(row[0]) for row in connection.execute("SELECT value FROM events ORDER BY id")]


def _symlink_file_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target.resolve())
    except OSError as exc:
        pytest.skip(f"file symlinks unavailable on this Windows account: {exc}")


def test_default_budget_covers_current_large_state_image_with_reserve() -> None:
    current_state_db_bytes = 5_133_869_056
    current_wal_bytes = 700_432
    current_state_image_bytes = current_state_db_bytes + current_wal_bytes

    assert DEFAULT_BACKUP_BUDGET_BYTES >= 8_000_000_000
    assert DEFAULT_BACKUP_BUDGET_BYTES - current_state_image_bytes >= DEFAULT_PEAK_HEADROOM_BYTES


def test_post_publication_prune_failure_never_revokes_new_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "state.db"
    _create_database(source)

    def fail_prune(*_args: object, **_kwargs: object) -> tuple[Path, ...]:
        raise OSError("injected old-generation cleanup failure")

    monkeypatch.setattr(
        database_backup_module,
        "prune_verified_backups",
        fail_prune,
    )
    published = create_verified_backup(source, tmp_path / "backups")
    assert verify_backup(published.manifest_path).manifest.sha256 == published.manifest.sha256


def test_cli_defaults_follow_data_root_and_expose_backup_safety_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BONGUS_DATA_ROOT", str(tmp_path))

    args = backup_db._parser().parse_args(["backup"])

    assert args.source == tmp_path / "state.db"
    assert args.destination == tmp_path / "backups"
    assert args.backup_budget_bytes == DEFAULT_BACKUP_BUDGET_BYTES
    assert args.required_headroom_bytes == DEFAULT_PEAK_HEADROOM_BYTES
    assert args.retention_count == 1


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


def test_verified_backup_reads_ignore_untrusted_sqlite_sidecars(tmp_path: Path) -> None:
    source = tmp_path / "state.db"
    _create_database(source)
    source_writer = sqlite3.connect(source)
    try:
        assert source_writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        source_writer.execute("INSERT INTO events(value) VALUES ('committed-wal')")
        source_writer.commit()
        result = create_verified_backup(source, tmp_path / "backups")
    finally:
        source_writer.close()

    sidecar_writer = sqlite3.connect(result.backup_path)
    restored_path = tmp_path / "restored.db"
    try:
        assert sidecar_writer.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        sidecar_writer.execute("INSERT INTO events(value) VALUES ('untrusted-sidecar')")
        sidecar_writer.commit()
        assert Path(f"{result.backup_path}-wal").stat().st_size > 0

        verified = verify_backup(result.manifest_path)
        restored = restore_verified_backup(result.manifest_path, restored_path)
    finally:
        sidecar_writer.close()

    assert verified.manifest.table_row_counts == {"events": 3}
    assert restored.table_row_counts == {"events": 3}
    assert _values(restored_path) == ["one", "two", "committed-wal"]


def test_backup_fails_before_copy_when_peak_space_is_unavailable(
    tmp_path: Path,
) -> None:
    source = tmp_path / "state.db"
    destination = tmp_path / "backups"
    _create_database(source)

    def low_space(_path: Path) -> SimpleNamespace:
        return SimpleNamespace(total=16_000_000_000, used=15_999_999_999, free=1)

    with pytest.raises(BackupError, match="insufficient peak space"):
        create_verified_backup(
            source,
            destination,
            required_headroom_bytes=512_000_000,
            disk_usage_probe=low_space,
        )

    assert not list(destination.glob("*.db"))
    assert not list(destination.glob("*.manifest.json"))


def test_generational_retention_keeps_newest_verified_backups_only(
    tmp_path: Path,
) -> None:
    source = tmp_path / "state.db"
    destination = tmp_path / "backups"
    _create_database(source)

    created = [create_verified_backup(source, destination, retention_count=2) for _ in range(3)]
    manifests = sorted(destination.glob("*.db.manifest.json"))

    assert len(manifests) == 2
    assert not created[0].manifest_path.exists()
    assert created[-1].manifest_path.exists()
    assert all(verify_backup(path).manifest.integrity_check == "ok" for path in manifests)

    invalid = destination / "invalid.db.manifest.json"
    invalid.write_text("not-json", encoding="utf-8")
    prune_verified_backups(destination, retention_count=1, source_name=source.name)
    remaining = list(destination.glob("state.*.db.manifest.json"))
    assert len(remaining) == 1
    assert verify_backup(remaining[0]).manifest.integrity_check == "ok"
    assert invalid.exists()


def test_pruning_rejects_linked_manifest_without_unlinking_external_backup(
    tmp_path: Path,
) -> None:
    source = tmp_path / "state.db"
    destination = tmp_path / "backups"
    outside = tmp_path / "outside"
    _create_database(source)
    external = create_verified_backup(source, outside, label="external")
    create_verified_backup(source, destination, label="internal")
    linked_manifest = destination / "linked-external.db.manifest.json"
    _symlink_file_or_skip(linked_manifest, external.manifest_path)

    with pytest.raises(BackupError, match="non-link/reparse"):
        verify_backup(linked_manifest)

    assert prune_verified_backups(destination, retention_count=1) == ()
    assert external.backup_path.exists()
    assert external.manifest_path.exists()
    assert linked_manifest.is_symlink()


def test_pruning_rejects_linked_backup_without_unlinking_external_target(
    tmp_path: Path,
) -> None:
    source = tmp_path / "state.db"
    destination = tmp_path / "backups"
    outside = tmp_path / "outside"
    _create_database(source)
    external = create_verified_backup(source, outside, label="external")
    create_verified_backup(source, destination, label="internal")

    copied_manifest = destination / "linked-payload.db.manifest.json"
    copied_manifest.write_bytes(external.manifest_path.read_bytes())
    linked_backup = destination / external.manifest.backup_filename
    _symlink_file_or_skip(linked_backup, external.backup_path)

    with pytest.raises(BackupError, match="non-link/reparse"):
        verify_backup(copied_manifest)

    assert prune_verified_backups(destination, retention_count=1) == ()
    assert external.backup_path.exists()
    assert external.manifest_path.exists()
    assert copied_manifest.exists()
    assert linked_backup.is_symlink()


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
    assert payload["backup_budget_bytes"] == DEFAULT_BACKUP_BUDGET_BYTES
    assert payload["required_headroom_bytes"] == DEFAULT_PEAK_HEADROOM_BYTES
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
