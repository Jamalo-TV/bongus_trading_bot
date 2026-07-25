import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from bongus.monitoring import king_watchdog
from bongus.monitoring.log_artifacts import (
    ARCHIVE_RELATIVE_DIR,
    archive_startup_artifacts,
    current_artifacts,
    write_support_bundle,
)


def _write(root: Path, relative: str, content: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_inventory_finds_rotated_logs_recovery_state_and_requested_reference(
    tmp_path,
):
    expected = {
        "scripts/logs/live_trader.log",
        "scripts/logs/live_trader.log.1",
        "scripts/logs/king_watchdog.log",
        "execution_engine/execution_state.jsonl",
        "execution_engine/execution_intents.jsonl",
        "execution_engine/data/private_stream_cursors/futures.jsonl",
        "runtime_heartbeat.json",
        "bongus/monitoring/web_dashboard_logs.html",
    }
    for relative in expected:
        _write(tmp_path, relative, "{}\n")
    _write(tmp_path, "logs/unrelated_test.log", "not a runtime artifact\n")

    discovered = {
        path.relative_to(tmp_path).as_posix()
        for path in current_artifacts(tmp_path)
    }

    assert discovered == expected


def test_startup_archive_moves_session_logs_but_only_copies_durable_state(
    tmp_path,
):
    session_files = {
        "scripts/logs/live_trader.log": "current\n",
        "scripts/logs/live_trader.log.1": "rotated\n",
        "execution_engine/rust_debug_log.txt": "rust debug\n",
        "ssh_log.txt": "ssh debug\n",
        "wsl_ssh_log.txt": "wsl debug\n",
        "runtime_heartbeat.json": '{"alive": true}\n',
    }
    durable_files = {
        "execution_engine/execution_state.jsonl": '{"state": 1}\n',
        "execution_engine/execution_intents.jsonl": '{"intent": 1}\n',
        "execution_engine/data/private_stream_cursors/futures.jsonl": (
            '{"through_ms": 1}\n'
        ),
        ".watchdog_state.json": '{"crashes": []}\n',
    }
    for relative, content in {**session_files, **durable_files}.items():
        _write(tmp_path, relative, content)
    reference = _write(
        tmp_path,
        "bongus/monitoring/web_dashboard_logs.html",
        "<html>logs</html>\n",
    )

    result = archive_startup_artifacts(
        tmp_path,
        retention_count=3,
        now=datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc),
    )

    assert result.archive_dir is not None
    assert not result.errors
    for relative, content in session_files.items():
        assert not (tmp_path / relative).exists()
        assert (result.archive_dir / relative).read_text(encoding="utf-8") == content
    for relative, content in durable_files.items():
        assert (tmp_path / relative).read_text(encoding="utf-8") == content
        assert (result.archive_dir / relative).read_text(encoding="utf-8") == content
    assert reference.exists()
    assert not (result.archive_dir / reference.relative_to(tmp_path)).exists()

    manifest = json.loads(
        (result.archive_dir / "manifest.json").read_text(encoding="utf-8")
    )
    assert sorted(manifest["moved_session_files"]) == sorted(session_files)
    assert sorted(manifest["copied_durable_recovery_files"]) == sorted(
        durable_files
    )


def test_startup_archive_retention_removes_only_old_archive_directories(tmp_path):
    archive_root = tmp_path / ARCHIVE_RELATIVE_DIR
    for name in ("20260101T000000Z", "20260201T000000Z", "20260301T000000Z"):
        _write(archive_root, f"{name}/old.log", name)
    _write(tmp_path, "scripts/logs/live_trader.log", "new session\n")

    result = archive_startup_artifacts(
        tmp_path,
        retention_count=2,
        now=datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc),
    )

    remaining = sorted(path.name for path in archive_root.iterdir())
    assert len(remaining) == 2
    assert result.archive_dir is not None
    assert result.archive_dir.name in remaining
    assert set(result.removed_archives) == {
        "20260101T000000Z",
        "20260201T000000Z",
    }


def test_support_bundle_contains_current_rotated_and_retained_startup_logs(
    tmp_path,
):
    _write(tmp_path, "scripts/logs/live_trader.log", "current\n")
    _write(tmp_path, "scripts/logs/live_trader.log.1", "rotated\n")
    _write(
        tmp_path,
        "execution_engine/execution_intents.jsonl",
        '{"intent": 1}\n',
    )
    _write(
        tmp_path,
        "bongus/monitoring/web_dashboard_logs.html",
        "<html></html>\n",
    )
    _write(
        tmp_path / ARCHIVE_RELATIVE_DIR,
        "20260724T120000Z/scripts/logs/live_trader.log",
        "previous session\n",
    )
    destination = io.BytesIO()

    manifest = write_support_bundle(
        destination,
        tmp_path,
        now=datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc),
    )

    destination.seek(0)
    with zipfile.ZipFile(destination) as bundle:
        names = set(bundle.namelist())
        assert "current/scripts/logs/live_trader.log" in names
        assert "current/scripts/logs/live_trader.log.1" in names
        assert "current/execution_engine/execution_intents.jsonl" in names
        assert (
            "startup_archives/20260724T120000Z/"
            "scripts/logs/live_trader.log"
        ) in names
        bundled_manifest = json.loads(bundle.read("manifest.json"))
    assert bundled_manifest == manifest
    missing_expected = manifest["missing_expected_files"]
    assert isinstance(missing_expected, list)
    assert "execution_engine/execution_state.jsonl" in missing_expected


def test_watchdog_runtime_log_rotation_keeps_a_bounded_backup_set(
    tmp_path,
    monkeypatch,
):
    log_file = tmp_path / "live_trader.log"
    monkeypatch.setattr(king_watchdog, "_LOG_FILE", str(log_file))
    monkeypatch.setattr(king_watchdog, "_LOG_MAX_BYTES", 20)
    monkeypatch.setattr(king_watchdog, "_LOG_BACKUP_COUNT", 2)

    for index in range(8):
        king_watchdog._write(f"line-{index}-long")

    assert log_file.exists()
    assert (tmp_path / "live_trader.log.1").exists()
    assert (tmp_path / "live_trader.log.2").exists()
    assert not (tmp_path / "live_trader.log.3").exists()
