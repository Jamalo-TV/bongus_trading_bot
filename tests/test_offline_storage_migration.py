from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
import sqlite3

import pytest

from bongus.engine.database_backup import create_verified_backup
from bongus.engine.cooldown_manager import CooldownManager
from bongus.engine.offline_storage_migration import (
    MANIFEST_FILENAME,
    MIGRATION_FORMAT,
    MigrationError,
    SchemaRoutingError,
    SourceNotQuiescentError,
    execute_migration,
    main,
    preflight_migration,
)
from bongus.engine.state_store import StateWriter
from bongus.market_data.feed_recovery import FeedCursorStore
from bongus.portfolio.capital_reservations import CapitalReservationBook


@dataclass(frozen=True)
class _Usage:
    free: int


def _create_source(path: Path, *, marker: str = "source") -> None:
    writer = StateWriter(str(path))
    try:
        # These schemas are installed by LiveTrader around StateWriter and are
        # part of the production monolith even though they live in focused
        # subsystem modules.
        CooldownManager(connection=writer.conn).close()
        FeedCursorStore(connection=writer.conn).close()
        CapitalReservationBook(connection=writer.conn).close()
        writer.conn.execute(
            """
            INSERT INTO positions
                (symbol, side, spot_entry, perp_entry, qty, updated_at)
            VALUES (?, 'LONG_SPOT_SHORT_PERP', 100.0, 100.1, 1.25, ?)
            """,
            (f"{marker.upper()}USDT", "2026-08-09T00:00:00+00:00"),
        )
        writer.conn.execute(
            """
            INSERT INTO execution_events
                (symbol, client_order_id, status, event_time, raw_payload)
            VALUES (?, ?, 'FILLED', ?, ?)
            """,
            (
                f"{marker.upper()}USDT",
                f"order-{marker}",
                "2026-08-09T00:00:01+00:00",
                json.dumps({"marker": marker}, sort_keys=True),
            ),
        )
        writer.conn.execute(
            """
            INSERT INTO candidate_snapshots
                (cycle_id, symbol, snapshot_time, direction, accepted,
                 status, cluster, rank, rejection_reasons, metrics_json)
            VALUES (?, ?, ?, 'LONG_SPOT_SHORT_PERP', 1,
                    'ACCEPTED', 'fixture', 1, '[]', ?)
            """,
            (
                f"cycle-{marker}",
                f"{marker.upper()}USDT",
                "2026-08-09T00:00:02+00:00",
                json.dumps({"marker": marker}, sort_keys=True),
            ),
        )
        writer.conn.execute(
            """
            INSERT INTO capital_reservations
                (reservation_id, request_hash, purpose, symbol, cycle_id,
                 spot_cash_usd, futures_margin_usd, fees_usd,
                 pair_gross_increment_usd, config_version, state,
                 metadata_json, created_at, updated_at)
            VALUES (?, ?, 'ENTRY', ?, ?, '100', '50', '1', '200',
                    'fixture', 'ACTIVE', '{}', ?, ?)
            """,
            (
                f"reservation-{marker}",
                "a" * 64,
                f"{marker.upper()}USDT",
                f"cycle-{marker}",
                "2026-08-09T00:00:03+00:00",
                "2026-08-09T00:00:03+00:00",
            ),
        )
        writer.conn.execute(
            """
            INSERT INTO capital_reservation_events
                (reservation_id, event_time, prior_state, next_state, reason, evidence_json)
            VALUES (?, ?, '', 'ACTIVE', 'fixture', '{}')
            """,
            (f"reservation-{marker}", "2026-08-09T00:00:03+00:00"),
        )
        writer.conn.commit()
    finally:
        writer.close()
    # Model the operator's required offline/quiescent boundary.  StateWriter
    # uses WAL at runtime; the fixture explicitly performs a clean checkpoint
    # and switches to DELETE before any migration identity is recorded.
    with sqlite3.connect(path) as connection:
        assert str(connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).lower() == "delete"
        connection.commit()
    assert not Path(f"{path}-wal").exists()
    assert not Path(f"{path}-shm").exists()


def _backup(source: Path, root: Path) -> Path:
    result = create_verified_backup(
        source,
        root / "backups",
        required_headroom_bytes=0,
        backup_budget_bytes=100_000_000,
        retention_count=1,
    )
    return result.manifest_path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rows(path: Path, table: str) -> int:
    uri = path.resolve().as_posix()
    with sqlite3.connect(f"file:{uri}?mode=ro&immutable=1", uri=True) as connection:
        return int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def _tables(path: Path) -> set[str]:
    uri = path.resolve().as_posix()
    with sqlite3.connect(f"file:{uri}?mode=ro&immutable=1", uri=True) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }


def _assert_destination_pragmas(path: Path) -> None:
    uri = path.resolve().as_posix()
    with sqlite3.connect(f"file:{uri}?mode=ro&immutable=1", uri=True) as connection:
        assert int(connection.execute("PRAGMA auto_vacuum").fetchone()[0]) == 2
        assert str(connection.execute("PRAGMA quick_check").fetchone()[0]).lower() == "ok"
        assert str(connection.execute("PRAGMA integrity_check").fetchone()[0]).lower() == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_execute_publishes_verified_split_without_mutating_source(tmp_path: Path) -> None:
    source = tmp_path / "state.db"
    output = tmp_path / "split"
    _create_source(source)
    manifest = _backup(source, tmp_path)
    source_before = (source.stat().st_size, source.stat().st_mtime_ns, _sha256(source))

    result = execute_migration(
        source,
        manifest,
        output,
        required_headroom_bytes=0,
    )

    assert result.output_directory == output
    assert result.manifest_path == output / MANIFEST_FILENAME
    assert set(path.name for path in result.destination_paths.values()) == {
        "state.db",
        "audit.db",
        "research.db",
    }
    assert source_before == (source.stat().st_size, source.stat().st_mtime_ns, _sha256(source))
    assert not Path(f"{source}-wal").exists()
    assert not Path(f"{source}-shm").exists()

    state = output / "state.db"
    audit = output / "audit.db"
    research = output / "research.db"
    assert _rows(state, "positions") == 1
    assert "positions" not in _tables(audit)
    assert _rows(audit, "execution_events") == 1
    assert "execution_events" not in _tables(state)
    assert _rows(research, "candidate_snapshots") == 0
    for path in (state, audit, research):
        _assert_destination_pragmas(path)
        assert not Path(f"{path}-wal").exists()
        assert not Path(f"{path}-shm").exists()

    payload = json.loads((output / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert payload["format"] == MIGRATION_FORMAT
    assert payload["publication"]["source_was_modified"] is False
    assert payload["authoritative_backup"]["canonical_source_match"] is True
    assert payload["routes"]["positions"]["database"] == "state.db"
    assert payload["routes"]["execution_events"]["database"] == "audit.db"
    assert payload["routes"]["candidate_snapshots"]["database"] == "research.db"
    omitted = {item["table"] for item in payload["omissions"]}
    assert "candidate_snapshots" in omitted
    assert payload["routes"]["candidate_snapshots"]["source_row_count"] == 1
    assert payload["routes"]["candidate_snapshots"]["retained"] is False


def test_execute_can_retain_legacy_tier_c_rows(tmp_path: Path) -> None:
    source = tmp_path / "state.db"
    output = tmp_path / "split"
    _create_source(source)
    manifest = _backup(source, tmp_path)

    execute_migration(
        source,
        manifest,
        output,
        retain_research=True,
        required_headroom_bytes=0,
    )

    assert _rows(output / "research.db", "candidate_snapshots") == 1
    payload = json.loads((output / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert payload["omissions"] == []
    assert payload["routes"]["candidate_snapshots"]["retained"] is True
    assert (
        payload["destinations"]["research.db"]["tables"]["candidate_snapshots"]
        ["content_sha256"]
        == payload["source"]["tables"]["candidate_snapshots"]["content_sha256"]
    )


def test_dry_run_is_read_only_and_does_not_create_output(tmp_path: Path) -> None:
    source = tmp_path / "state.db"
    output = tmp_path / "split"
    _create_source(source)
    manifest = _backup(source, tmp_path)
    source_hash = _sha256(source)

    report = preflight_migration(
        source,
        manifest,
        output,
        required_headroom_bytes=0,
    )

    assert report.to_dict()["status"] == "READY"
    assert not output.exists()
    assert _sha256(source) == source_hash


def test_module_cli_supports_dry_run_without_publication(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "state.db"
    output = tmp_path / "split"
    _create_source(source)
    manifest = _backup(source, tmp_path)

    code = main(
        [
            "dry-run",
            "--source",
            str(source),
            "--backup-manifest",
            str(manifest),
            "--output",
            str(output),
            "--required-headroom-bytes",
            "0",
        ]
    )

    assert code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "READY"
    assert not output.exists()


def test_preflight_refuses_insufficient_peak_headroom(tmp_path: Path) -> None:
    source = tmp_path / "state.db"
    output = tmp_path / "split"
    _create_source(source)
    manifest = _backup(source, tmp_path)

    with pytest.raises(MigrationError, match="insufficient peak space"):
        preflight_migration(
            source,
            manifest,
            output,
            required_headroom_bytes=512_000_000,
            disk_usage_probe=lambda _path: _Usage(free=1),
        )
    assert not output.exists()


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_preflight_refuses_source_sqlite_sidecars(tmp_path: Path, suffix: str) -> None:
    source = tmp_path / "state.db"
    output = tmp_path / "split"
    _create_source(source)
    manifest = _backup(source, tmp_path)
    Path(f"{source}{suffix}").write_bytes(b"not-quiescent")

    with pytest.raises(SourceNotQuiescentError, match="not quiescent"):
        preflight_migration(source, manifest, output, required_headroom_bytes=0)
    assert not output.exists()


def test_preflight_refuses_tampered_verified_backup(tmp_path: Path) -> None:
    source = tmp_path / "state.db"
    output = tmp_path / "split"
    _create_source(source)
    manifest = _backup(source, tmp_path)
    backup_payload = json.loads(manifest.read_text(encoding="utf-8"))
    backup_path = manifest.parent / backup_payload["backup_filename"]
    with backup_path.open("r+b") as handle:
        handle.seek(max(100, backup_path.stat().st_size // 2))
        byte = handle.read(1)
        handle.seek(-1, 1)
        handle.write(bytes([(byte[0] if byte else 0) ^ 0x01]))

    with pytest.raises(MigrationError, match="not independently verified"):
        preflight_migration(source, manifest, output, required_headroom_bytes=0)
    assert not output.exists()


def test_preflight_refuses_verified_but_mismatched_backup(tmp_path: Path) -> None:
    source_dir = tmp_path / "primary"
    other_dir = tmp_path / "other"
    source_dir.mkdir()
    other_dir.mkdir()
    source = source_dir / "state.db"
    other = other_dir / "state.db"
    output = tmp_path / "split"
    _create_source(source, marker="primary")
    _create_source(other, marker="different")
    manifest = _backup(other, other_dir)

    with pytest.raises(MigrationError, match="does not canonically match"):
        preflight_migration(source, manifest, output, required_headroom_bytes=0)
    assert not output.exists()


def test_preflight_refuses_corrupt_source(tmp_path: Path) -> None:
    source = tmp_path / "state.db"
    output = tmp_path / "split"
    _create_source(source)
    manifest = _backup(source, tmp_path)
    with source.open("r+b") as handle:
        handle.seek(0)
        handle.write(b"not a sqlite database")

    with pytest.raises(MigrationError, match="SQLite validation failed"):
        preflight_migration(source, manifest, output, required_headroom_bytes=0)
    assert not output.exists()


def test_copy_failure_never_publishes_partial_directory(tmp_path: Path) -> None:
    source = tmp_path / "state.db"
    output = tmp_path / "split"
    _create_source(source)
    manifest = _backup(source, tmp_path)

    def fail(stage: str) -> None:
        if stage.startswith("after_table:"):
            raise RuntimeError("injected crash")

    with pytest.raises(RuntimeError, match="injected crash"):
        execute_migration(
            source,
            manifest,
            output,
            required_headroom_bytes=0,
            fault_injector=fail,
        )

    assert not output.exists()
    assert not list(tmp_path.glob(".split.migration-*"))
    assert source.exists()


def test_failure_after_full_validation_still_does_not_publish(tmp_path: Path) -> None:
    source = tmp_path / "state.db"
    output = tmp_path / "split"
    _create_source(source)
    manifest = _backup(source, tmp_path)

    def fail(stage: str) -> None:
        if stage == "before_publish":
            raise RuntimeError("injected publication crash")

    with pytest.raises(RuntimeError, match="injected publication crash"):
        execute_migration(
            source,
            manifest,
            output,
            required_headroom_bytes=0,
            fault_injector=fail,
        )
    assert not output.exists()
    assert not list(tmp_path.glob(".split.migration-*"))


def test_output_created_by_race_is_preserved_and_not_replaced(tmp_path: Path) -> None:
    source = tmp_path / "state.db"
    output = tmp_path / "split"
    _create_source(source)
    manifest = _backup(source, tmp_path)

    def create_racing_output(stage: str) -> None:
        if stage == "before_publish":
            output.mkdir()
            (output / "operator-data.txt").write_text("preserve", encoding="utf-8")

    with pytest.raises(MigrationError, match="output path appeared|refusing to overwrite"):
        execute_migration(
            source,
            manifest,
            output,
            required_headroom_bytes=0,
            fault_injector=create_racing_output,
        )
    assert (output / "operator-data.txt").read_text(encoding="utf-8") == "preserve"
    assert not list(tmp_path.glob(".split.migration-*"))


def test_source_change_during_copy_is_detected_before_publication(tmp_path: Path) -> None:
    source = tmp_path / "state.db"
    output = tmp_path / "split"
    _create_source(source)
    manifest = _backup(source, tmp_path)

    def mutate_source(stage: str) -> None:
        if stage == "before_final_source_check":
            with sqlite3.connect(source) as connection:
                connection.execute(
                    "INSERT INTO risk_state(key, value, updated_at) VALUES ('race', '1', 'now')"
                )
                connection.commit()

    with pytest.raises(SourceNotQuiescentError, match="changed during migration"):
        execute_migration(
            source,
            manifest,
            output,
            required_headroom_bytes=0,
            fault_injector=mutate_source,
        )
    assert not output.exists()


def test_existing_output_is_never_overwritten(tmp_path: Path) -> None:
    source = tmp_path / "state.db"
    output = tmp_path / "split"
    output.mkdir()
    sentinel = output / "operator-data.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    _create_source(source)
    manifest = _backup(source, tmp_path)

    with pytest.raises(MigrationError, match="refusing to overwrite"):
        execute_migration(source, manifest, output, required_headroom_bytes=0)
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_unclassified_table_fails_closed_before_staging(tmp_path: Path) -> None:
    source = tmp_path / "state.db"
    output = tmp_path / "split"
    _create_source(source)
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE unexpected_runtime_state(id INTEGER PRIMARY KEY)")
        connection.commit()
    manifest = _backup(source, tmp_path)

    with pytest.raises(SchemaRoutingError, match="unclassified tables"):
        preflight_migration(source, manifest, output, required_headroom_bytes=0)
    assert not output.exists()
