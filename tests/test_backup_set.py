from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any

import pytest

import bongus.engine.backup_set as backup_set_module
from bongus.engine.backup_set import (
    DEFAULT_SET_BUDGET_BYTES,
    MAX_RECOVERY_CONFIGURATION_BYTES,
    BackupError,
    cleanup_abandoned_staging,
    restore_backup_set_to_empty_directory,
    verify_backup_set,
)
from bongus.engine.backup_set import (
    create_verified_backup_set as _create_verified_backup_set,
)
from bongus.engine.rust_recovery import MAX_RUST_RECOVERY_GENERATION_BYTES
from bongus.engine.split_state_store import (
    SPLIT_ROLE_DATABASE_MAX_BYTES,
    SPLIT_ROLE_WAL_MAX_BYTES,
    SplitStateReader,
    SplitStateWriter,
)
from tests.rust_recovery_support import FakeRustRecoveryHarness, rust_create_kwargs
import tests.rust_recovery_support as rust_recovery_support


def create_verified_backup_set(
    data_root: Path,
    backup_directory: Path,
    **kwargs: Any,
):
    harness = FakeRustRecoveryHarness(data_root / "test-rust-recovery")
    return _create_verified_backup_set(
        data_root,
        backup_directory,
        **rust_create_kwargs(harness),
        **kwargs,
    )


def _split_data_root(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    writer = SplitStateWriter(
        state_path=str(root / "state.db"),
        audit_path=str(root / "audit.db"),
        research_path=str(root / "research.db"),
    )
    writer.close()
    (root / "live_config.json").write_text('{"pause_new_entries":true}\n', encoding="utf-8")
    return root


def test_complete_set_budget_covers_every_enforced_writer_wal_and_recovery_cap() -> None:
    maximum_bound_set_bytes = (
        sum(SPLIT_ROLE_DATABASE_MAX_BYTES.values())
        + 3 * SPLIT_ROLE_WAL_MAX_BYTES
        + MAX_RUST_RECOVERY_GENERATION_BYTES
        + MAX_RECOVERY_CONFIGURATION_BYTES
    )

    assert maximum_bound_set_bytes == 7_701_052_672
    assert maximum_bound_set_bytes <= DEFAULT_SET_BUDGET_BYTES


def test_complete_set_is_atomic_deep_verifiable_and_clean_restorable(
    tmp_path: Path,
) -> None:
    root = _split_data_root(tmp_path)
    created = create_verified_backup_set(root, root / "backups")

    assert created.manifest_path.parent.name == f"backup-set.{created.set_id}"
    assert set(created.backups) == {"state.db", "audit.db", "research.db"}
    assert set(created.recovery_files) == {"live_config.json"}
    assert verify_backup_set(created.manifest_path).set_id == created.set_id
    rust_harness = FakeRustRecoveryHarness(root / "test-rust-recovery")
    restored = restore_backup_set_to_empty_directory(
        created.manifest_path,
        tmp_path / "restored",
        rust_execution_binary=rust_harness.binary,
        rust_command_runner=rust_harness.runner,
    )
    assert {path.name for path in restored} == {
        "state.db",
        "audit.db",
        "research.db",
        "live_config.json",
        "manifest.json",
        "execution_state.jsonl",
        "execution_intents.jsonl",
        "execution_telemetry.jsonl",
        "execution_telemetry.jsonl.cursor.a",
        "spot.jsonl",
        "futures.jsonl",
    }
    reader = SplitStateReader(
        state_path=str(tmp_path / "restored" / "state.db"),
        audit_path=str(tmp_path / "restored" / "audit.db"),
        research_path=str(tmp_path / "restored" / "research.db"),
    )
    reader.close()


def test_backup_tree_peak_budget_rejects_before_allocating_a_generation(
    tmp_path: Path,
) -> None:
    root = _split_data_root(tmp_path)
    with pytest.raises(BackupError, match="publication peak"):
        create_verified_backup_set(
            root,
            root / "backups",
            backup_tree_budget_bytes=1,
        )
    assert not list((root / "backups").glob("*backup-set*"))


def test_failed_second_set_preserves_the_previous_complete_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _split_data_root(tmp_path)
    previous = create_verified_backup_set(root, root / "backups")
    original = backup_set_module.create_verified_backup

    def fail_on_audit(source: Path, *args: Any, **kwargs: Any):
        if Path(source).name == "audit.db":
            raise BackupError("injected member failure")
        return original(source, *args, **kwargs)

    monkeypatch.setattr(backup_set_module, "create_verified_backup", fail_on_audit)
    with pytest.raises(BackupError, match="injected member failure"):
        create_verified_backup_set(root, root / "backups")

    assert verify_backup_set(previous.manifest_path).set_id == previous.set_id
    assert not list((root / "backups").glob(".backup-set-staging.*"))


def test_gc_failure_never_revokes_the_newly_published_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _split_data_root(tmp_path)
    create_verified_backup_set(root, root / "backups")
    original_prune = backup_set_module.prune_backup_sets
    prune_calls = 0

    def fail_gc(*args: Any, **kwargs: Any) -> tuple[Path, ...]:
        nonlocal prune_calls
        prune_calls += 1
        if prune_calls == 1:
            return original_prune(*args, **kwargs)
        raise BackupError("injected GC failure")

    with monkeypatch.context() as scoped:
        scoped.setattr(backup_set_module, "prune_backup_sets", fail_gc)
        newest = create_verified_backup_set(root, root / "backups")
        assert verify_backup_set(newest.manifest_path).set_id == newest.set_id
        assert len(list((root / "backups").glob("backup-set.*"))) == 2

    final = create_verified_backup_set(root, root / "backups")
    assert verify_backup_set(final.manifest_path).set_id == final.set_id
    assert len(list((root / "backups").glob("backup-set.*"))) == 1


def test_interrupted_gc_is_resumed_without_leaking_a_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _split_data_root(tmp_path)
    create_verified_backup_set(root, root / "backups")
    original_remove = backup_set_module._remove_owned_gc_directory

    with monkeypatch.context() as scoped:

        def interrupt_gc(candidate: Path) -> tuple[Path, ...]:
            raise OSError(f"injected GC interruption for {candidate.name}")

        scoped.setattr(backup_set_module, "_remove_owned_gc_directory", interrupt_gc)
        newest = create_verified_backup_set(root, root / "backups")
        assert verify_backup_set(newest.manifest_path).set_id == newest.set_id
        assert list((root / "backups").glob(".backup-set-gc.*"))

    assert backup_set_module._remove_owned_gc_directory is original_remove
    final = create_verified_backup_set(root, root / "backups")
    assert verify_backup_set(final.manifest_path).set_id == final.set_id
    assert not list((root / "backups").glob(".backup-set-gc.*"))


def test_directory_publish_failure_cleans_owned_staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _split_data_root(tmp_path)
    original_replace = backup_set_module.os.replace

    def fail_generation_publish(source: Any, destination: Any) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        if source_path.name.startswith(".backup-set-staging.") and destination_path.name.startswith("backup-set."):
            raise OSError("injected directory publish failure")
        original_replace(source, destination)

    monkeypatch.setattr(backup_set_module.os, "replace", fail_generation_publish)
    with pytest.raises(OSError, match="directory publish failure"):
        create_verified_backup_set(root, root / "backups")
    assert not list((root / "backups").glob(".backup-set-staging.*"))


def test_abandoned_owned_staging_cleanup_accepts_only_exact_members(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "backups"
    staging = directory / ".backup-set-staging.deadbeef"
    staging.mkdir(parents=True)
    old = datetime.now(timezone.utc) - timedelta(minutes=2)
    (staging / ".backup-set-generation-v1").write_text(old.isoformat() + "\n", encoding="ascii")
    (staging / "live_config.json").write_text("{}\n", encoding="utf-8")
    (staging / ".state.interrupted.db.tmp").write_bytes(b"partial")

    removed = cleanup_abandoned_staging(directory, now=datetime.now(timezone.utc))
    assert removed
    assert not staging.exists()


def test_old_empty_power_loss_marker_is_safely_reclaimed(tmp_path: Path) -> None:
    directory = tmp_path / "backups"
    staging = directory / ".backup-set-staging.deadbeef"
    staging.mkdir(parents=True)
    marker = staging / ".backup-set-generation-v1"
    marker.write_bytes(b"")
    (staging / "live_config.json").write_text("{}\n", encoding="utf-8")
    old_timestamp = (datetime.now(timezone.utc) - timedelta(minutes=2)).timestamp()
    backup_set_module.os.utime(marker, (old_timestamp, old_timestamp))

    removed = cleanup_abandoned_staging(
        directory,
        now=datetime.now(timezone.utc),
    )
    assert removed
    assert not staging.exists()


def test_shallow_health_verification_does_not_rescan_database_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _split_data_root(tmp_path)
    created = create_verified_backup_set(root, root / "backups")

    def forbidden_deep_scan(*args: object, **kwargs: object):
        raise AssertionError("database payload was deep-scanned")

    monkeypatch.setattr(backup_set_module, "verify_backup", forbidden_deep_scan)
    assert verify_backup_set(created.manifest_path, deep=False).set_id == created.set_id
    with pytest.raises(AssertionError, match="deep-scanned"):
        verify_backup_set(created.manifest_path, deep=True)


@pytest.mark.parametrize(
    ("start_us", "end_us", "recorded_ms", "accepted"),
    [
        (123_456, 456_789, 123, True),
        (123_456, 456_789, 456, True),
        (123_456, 456_789, 122, False),
        (123_456, 456_789, 457, False),
        (123_000, 123_000, 123, True),
        (123_000, 123_000, 122, False),
        (123_999, 124_000, 123, True),
        (999_999, 999_999, 1_000, False),
    ],
)
def test_rust_generation_window_uses_only_the_recorded_millisecond_precision(
    start_us: int, end_us: int, recorded_ms: int, accepted: bool,
) -> None:
    base = datetime(2030, 1, 1, tzinfo=timezone.utc)
    created_at_ms = 1_893_456_000_000 + recorded_ms
    start, end = base.replace(microsecond=start_us), base.replace(microsecond=end_us)
    if accepted:
        backup_set_module._validate_rust_generation_timestamp(created_at_ms, start, end)
    else:
        with pytest.raises(BackupError, match="outside the set window"):
            backup_set_module._validate_rust_generation_timestamp(created_at_ms, start, end)


def test_backup_creation_accepts_same_millisecond_and_verifier_rejects_older_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _split_data_root(tmp_path)
    started_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).replace(microsecond=123_456)
    completed_at = started_at + timedelta(minutes=2)
    times = iter((started_at, started_at, completed_at))  # cleanup, start, completion

    class BackupClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(times, completed_at)

    delta = started_at - datetime(1970, 1, 1, tzinfo=timezone.utc)
    rust_ms = delta.days * 86_400_000 + delta.seconds * 1_000 + 123
    monkeypatch.setattr(backup_set_module, "datetime", BackupClock)
    monkeypatch.setattr(rust_recovery_support.time, "time_ns", lambda: rust_ms * 1_000_000)
    created = create_verified_backup_set(root, root / "backups")
    assert created.rust_recovery_generation.created_at_ms == rust_ms
    assert verify_backup_set(created.manifest_path).set_id == created.set_id

    payload = json.loads(created.manifest_path.read_text(encoding="utf-8"))
    payload["started_at"] = (started_at + timedelta(milliseconds=1)).isoformat()
    created.manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BackupError, match="Rust recovery generation timestamp is outside"):
        verify_backup_set(created.manifest_path)
