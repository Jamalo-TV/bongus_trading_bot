import hashlib
import json
import os
import sqlite3
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from bongus.core.live_approval import LiveApprovalError, sha256_file, sign_live_approval
from bongus.monitoring import king_watchdog


class _FakeProc:
    def __init__(self) -> None:
        self.pid = 1234
        self.returncode = None
        self.terminated = False

    def poll(self) -> int | None:
        return None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout=None) -> None:
        del timeout
        return None

    def kill(self) -> None:
        self.terminated = True


class _FakeExitedProc(_FakeProc):
    def __init__(self, returncode: int = 1) -> None:
        super().__init__()
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode


class _FakePsutilProc:
    class _MemInfo:
        rss = 32 * 1024 * 1024

    def memory_info(self):
        return self._MemInfo()


def test_trader_liveness_grace_skips_restart(monkeypatch):
    proc = _FakeProc()
    tracker = king_watchdog.CrashTracker()
    old_alive = datetime.now(timezone.utc) - timedelta(seconds=600)
    restarted = False

    monkeypatch.setattr(king_watchdog.psutil, "Process", lambda pid: _FakePsutilProc())
    monkeypatch.setattr(king_watchdog, "_read_trader_liveness", lambda: ("PAPER", old_alive, None, None, None))

    def fake_start_process(command, name, cwd=None):
        del command, name, cwd
        nonlocal restarted
        restarted = True
        return object()

    monkeypatch.setattr(king_watchdog, "start_process", fake_start_process)

    result = king_watchdog.check_and_restart(
        proc,
        ["python", "scripts/live_trader.py"],
        "trader",
        ".",
        tracker,
        started_at=king_watchdog.time.time(),
    )

    assert result is proc
    assert restarted is False
    assert proc.terminated is False


def test_trader_liveness_restarts_after_grace(monkeypatch):
    proc = _FakeProc()
    tracker = king_watchdog.CrashTracker()
    old_alive = datetime.now(timezone.utc) - timedelta(seconds=600)
    replacement = object()

    monkeypatch.setattr(king_watchdog.psutil, "Process", lambda pid: _FakePsutilProc())
    monkeypatch.setattr(king_watchdog, "_read_trader_liveness", lambda: ("PAPER", old_alive, None, None, None))
    monkeypatch.setattr(king_watchdog, "start_process", lambda command, name, cwd=None: replacement)

    result = king_watchdog.check_and_restart(
        proc,
        ["python", "scripts/live_trader.py"],
        "trader",
        ".",
        tracker,
        started_at=king_watchdog.time.time() - king_watchdog.TRADER_LIVENESS_STARTUP_GRACE_SECONDS - 5,
    )

    assert result is replacement
    assert proc.terminated is True


def test_trader_restarts_when_decision_loop_stalls_but_liveness_is_fresh(monkeypatch):
    proc = _FakeProc()
    tracker = king_watchdog.CrashTracker()
    replacement = object()
    fresh = datetime.now(timezone.utc) - timedelta(seconds=2)
    loop_ages = {
        "liveness_loop": 1.0,
        "maintenance_loop": 2.0,
        "execution_event_writer": 1.0,
        "trading_loop": king_watchdog.TRADER_REQUIRED_LOOP_MAX_AGE_SECONDS + 1,
    }
    monkeypatch.setattr(king_watchdog.psutil, "Process", lambda pid: _FakePsutilProc())
    monkeypatch.setattr(
        king_watchdog,
        "_read_trader_liveness",
        lambda: ("PAPER", fresh, None, None, loop_ages),
    )
    monkeypatch.setattr(king_watchdog, "start_process", lambda command, name, cwd=None: replacement)

    result = king_watchdog.check_and_restart(
        proc,
        ["python", "-m", "scripts.live_trader_v2"],
        "trader",
        ".",
        tracker,
        started_at=king_watchdog.time.time() - king_watchdog.TRADER_LIVENESS_STARTUP_GRACE_SECONDS - 5,
    )

    assert result is replacement
    assert proc.terminated
    assert tracker.crash_times


def test_false_green_campaign_covers_every_progress_loop_and_recovers_port_collision(
    monkeypatch,
):
    fresh = datetime.now(timezone.utc) - timedelta(seconds=1)
    replacement = object()
    monkeypatch.setattr(king_watchdog.psutil, "Process", lambda pid: _FakePsutilProc())
    monkeypatch.setattr(
        king_watchdog,
        "start_process",
        lambda command, name, cwd=None: replacement,
    )

    for stalled_name in (*king_watchdog.TRADER_REQUIRED_PROGRESS_LOOPS, "missing_map"):
        proc = _FakeProc()
        tracker = king_watchdog.CrashTracker()
        ages = (
            None
            if stalled_name == "missing_map"
            else {
                name: (
                    king_watchdog.TRADER_REQUIRED_LOOP_MAX_AGE_SECONDS + 1
                    if name == stalled_name
                    else 1.0
                )
                for name in king_watchdog.TRADER_REQUIRED_PROGRESS_LOOPS
            }
        )
        monkeypatch.setattr(
            king_watchdog,
            "_read_trader_liveness",
            lambda ages=ages: ("LIVE", fresh, None, None, ages),
        )

        result = king_watchdog.check_and_restart(
            proc,
            ["python", "-m", "scripts.live_trader_v2"],
            "trader",
            ".",
            tracker,
            started_at=(
                king_watchdog.time.time()
                - king_watchdog.TRADER_LIVENESS_STARTUP_GRACE_SECONDS
                - 5
            ),
        )
        assert result is replacement
        assert proc.terminated
        assert tracker.crash_times

    clock = [1_000.0]
    collision = [True]
    dashboard = _FakeExitedProc(returncode=1)
    dashboard_tracker = king_watchdog.CrashTracker()
    monkeypatch.setattr(king_watchdog.time, "time", lambda: clock[0])
    monkeypatch.setattr(
        king_watchdog,
        "_start_block_reason",
        lambda name, ignore_pids=None: (
            "port 8080 is already in use" if name == "dashboard" and collision[0] else None
        ),
    )

    blocked = king_watchdog.check_and_restart(
        dashboard,
        ["python", "-m", "uvicorn"],
        "dashboard",
        ".",
        dashboard_tracker,
    )
    assert blocked is dashboard
    assert dashboard_tracker.permanently_failed

    collision[0] = False
    clock[0] = dashboard_tracker.backoff_until
    recovered = king_watchdog.check_and_restart(
        dashboard,
        ["python", "-m", "uvicorn"],
        "dashboard",
        ".",
        dashboard_tracker,
    )
    assert recovered is replacement
    assert not dashboard_tracker.permanently_failed


def test_crash_budget_survives_watchdog_restart_and_allows_bounded_probe(tmp_path, monkeypatch):
    state_path = str(tmp_path / "watchdog.json")
    clock = [1_000.0]
    monkeypatch.setattr(king_watchdog.time, "time", lambda: clock[0])
    first = king_watchdog.CrashTracker(name="trader", state_path=state_path)
    for offset in (0.0, 1.0, 2.0):
        clock[0] = 1_000.0 + offset
        first.record_crash()
    assert first.permanently_failed

    restored = king_watchdog.CrashTracker(name="trader", state_path=state_path)
    assert restored.permanently_failed
    assert restored.should_restart() is False
    clock[0] = restored.backoff_until
    assert restored.should_restart() is True
    assert restored.permanently_failed is False


def test_restart_is_blocked_when_required_port_is_occupied(monkeypatch):
    proc = _FakeExitedProc(returncode=1)
    tracker = king_watchdog.CrashTracker()
    restarted = False

    def fake_start_process(command, name, cwd=None):
        del command, name, cwd
        nonlocal restarted
        restarted = True
        return object()

    monkeypatch.setattr(
        king_watchdog,
        "_start_block_reason",
        lambda name, ignore_pids=None: "port 8080 is already in use" if name == "dashboard" else None,
    )
    monkeypatch.setattr(king_watchdog, "start_process", fake_start_process)

    result = king_watchdog.check_and_restart(
        proc,
        ["python", "-m", "uvicorn"],
        "dashboard",
        ".",
        tracker,
    )

    assert result is proc
    assert tracker.permanently_failed is True
    assert restarted is False


def test_build_process_defs_skips_rust_required_services_when_unavailable():
    process_defs, skipped = king_watchdog._build_process_defs(
        rust_build_ok=False,
        sentiment_enabled=True,
    )

    names = [name for name, _, _ in process_defs]

    assert "rust" not in names
    assert "trader" not in names
    assert "telegram" in names
    assert "scraper" in names
    assert "dashboard" in names
    assert "supervisor" in names
    assert "testnet_dust_sweeper" not in names
    assert skipped == king_watchdog._RUST_REQUIRED_PROCESS_NAMES


def test_build_process_defs_never_launches_retired_dust_sweeper():
    process_defs, skipped = king_watchdog._build_process_defs(
        rust_build_ok=True,
        sentiment_enabled=False,
        testnet_dust_sweeper_enabled=True,
    )

    process_map = {name: command for name, command, _ in process_defs}

    assert skipped == ()
    assert "testnet_dust_sweeper" not in process_map


def test_testnet_dust_sweeper_env_flag_fails_closed():
    name = king_watchdog.TESTNET_DUST_SWEEPER_ENABLE_ENV

    assert king_watchdog._env_flag_enabled(name, {}) is False
    assert king_watchdog._env_flag_enabled(name, {name: "false"}) is False
    assert king_watchdog._env_flag_enabled(name, {name: "unexpected"}) is False
    assert king_watchdog._env_flag_enabled(name, {name: " true "}) is True


def test_watchdog_runtime_has_no_cargo_or_toolchain_discovery_surface():
    assert king_watchdog.RUST_BUILD_COMMAND == []
    for retired_name in (
        "_CARGO_COMMAND",
        "_RUST_TOOLCHAIN_DIRS",
        "_prepend_path_entries",
        "_resolve_executable",
        "_rust_toolchain_dirs",
    ):
        assert not hasattr(king_watchdog, retired_name)


def test_every_rust_start_rechecks_preflight_before_popen(monkeypatch):
    preflight_calls: list[str] = []
    popen_calls: list[list[str]] = []

    monkeypatch.setattr(
        king_watchdog,
        "run_preflight_checks",
        lambda: preflight_calls.append("checked") or False,
    )
    monkeypatch.setattr(
        king_watchdog.subprocess,
        "Popen",
        lambda command, **kwargs: popen_calls.append(list(command)),
    )

    for _ in range(2):
        with pytest.raises(RuntimeError, match="per-start verification"):
            king_watchdog.start_process(
                king_watchdog.RUST_COMMAND,
                name="rust",
                cwd=king_watchdog.RUST_ENGINE_DIR,
            )

    assert preflight_calls == ["checked", "checked"]
    assert popen_calls == []


def test_live_preflight_rechecks_approval_for_each_rust_launch(tmp_path, monkeypatch):
    binary = tmp_path / "execution_engine.exe"
    payload = bytearray(1024)
    payload[:2] = b"MZ"
    struct.pack_into("<I", payload, 0x3C, 0x80)
    payload[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", payload, 0x84, 0x8664)
    struct.pack_into("<H", payload, 0x86, 1)
    struct.pack_into("<H", payload, 0x94, 0xF0)
    struct.pack_into("<H", payload, 0x96, 0x0022)
    optional_header = 0x98
    struct.pack_into("<H", payload, optional_header, 0x20B)
    struct.pack_into("<I", payload, optional_header + 16, 0x1000)
    struct.pack_into("<I", payload, optional_header + 32, 0x1000)
    struct.pack_into("<I", payload, optional_header + 36, 0x200)
    struct.pack_into("<I", payload, optional_header + 56, 0x2000)
    struct.pack_into("<I", payload, optional_header + 60, 0x200)
    section = optional_header + 0xF0
    payload[section:section + 8] = b".text\0\0\0"
    struct.pack_into("<I", payload, section + 8, 0x200)
    struct.pack_into("<I", payload, section + 12, 0x1000)
    struct.pack_into("<I", payload, section + 16, 0x200)
    struct.pack_into("<I", payload, section + 20, 0x200)
    struct.pack_into("<I", payload, section + 36, 0x60000020)
    binary.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    approval_calls: list[str] = []

    monkeypatch.setattr(king_watchdog, "RUST_COMMAND", [str(binary)])
    monkeypatch.setattr(
        king_watchdog,
        "_release_rust_contract",
        lambda observed: (digest, True),
    )
    monkeypatch.setattr(
        king_watchdog,
        "_verify_live_rust_approval",
        lambda observed: approval_calls.append(str(observed)),
    )
    monkeypatch.setattr(
        king_watchdog,
        "verify_runtime_inventory",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setitem(king_watchdog._ENV, "TRADING_MODE", "live")
    monkeypatch.setitem(king_watchdog._ENV, "BONGUS_RUST_BINARY_SHA256", digest)

    assert king_watchdog.run_preflight_checks()
    assert king_watchdog.run_preflight_checks()
    assert approval_calls == [str(binary), str(binary)]


def test_watchdog_live_approval_is_hmac_authenticated_and_binary_bound(
    tmp_path,
    monkeypatch,
):
    binary = tmp_path / "execution_engine.exe"
    binary.write_bytes(b"approved rust bytes")
    release_manifest = tmp_path / "release-manifest.json"
    release_manifest.write_text('{"production_eligible":true}', encoding="utf-8")
    decision = tmp_path / "gate-d.json"
    decision.write_text('{"decision":"GO"}', encoding="utf-8")
    approval_path = tmp_path / "live-approval.json"
    approval_key = b"operator-held-live-approval-key-32-bytes-minimum"
    config_hash = "a" * 64
    now = datetime.now(timezone.utc)
    payload = {
        "schema_version": 3,
        "approved": True,
        "trading_mode": "live",
        "approved_by": "risk-owner@example.test",
        "approved_at": (now - timedelta(minutes=1)).isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        "config_sha256": config_hash,
        "release_manifest_sha256": sha256_file(release_manifest),
        "rust_binary_sha256": sha256_file(binary),
        "decision_artifact_path": decision.name,
        "decision_artifact_sha256": sha256_file(decision),
        "account_id": "account-123",
        "nonce": "watchdog-restart-check",
    }
    payload["signature_hmac_sha256"] = sign_live_approval(payload, approval_key)
    approval_path.write_text(json.dumps(payload), encoding="utf-8")
    config = SimpleNamespace(
        get=lambda key: str(approval_path) if key == "live_approval_artifact_path" else None,
        canonical_snapshot=lambda: SimpleNamespace(sha256=config_hash),
    )
    monkeypatch.setattr(king_watchdog, "ConfigManager", lambda: config)
    monkeypatch.setattr(king_watchdog, "_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setitem(
        king_watchdog._ENV,
        "BONGUS_LIVE_APPROVAL_HMAC_KEY",
        approval_key.decode("utf-8"),
    )
    monkeypatch.setitem(
        king_watchdog._ENV,
        "BONGUS_EXPECTED_ACCOUNT_UID",
        "account-123",
    )

    king_watchdog._verify_live_rust_approval(binary)
    release_manifest.write_text('{"production_eligible":false}', encoding="utf-8")
    with pytest.raises(LiveApprovalError, match="release manifest hash mismatch"):
        king_watchdog._verify_live_rust_approval(binary)
    release_manifest.write_text('{"production_eligible":true}', encoding="utf-8")
    binary.write_bytes(b"tampered after approval")
    with pytest.raises(LiveApprovalError, match="Rust binary hash mismatch"):
        king_watchdog._verify_live_rust_approval(binary)


def test_database_maintenance_does_not_prune_immediately_on_startup(
    tmp_path,
    monkeypatch,
):
    prune_calls: list[str] = []

    monkeypatch.setattr(king_watchdog.time, "time", lambda: 1_000.0)
    maintenance = king_watchdog.DatabaseMaintenance(
        str(tmp_path / "state.db"),
        str(tmp_path / "audit.db"),
        str(tmp_path / "research.db"),
    )
    monkeypatch.setattr(maintenance, "_prune", lambda: prune_calls.append("pruned"))

    maintenance.run_maintenance_if_needed()

    assert prune_calls == []


def test_watchdog_resolves_and_exports_one_split_data_root(tmp_path):
    project_root = tmp_path / "application"
    data_root = tmp_path / "data-volume"

    resolved = king_watchdog._resolve_runtime_database_paths(
        {
            "BONGUS_DATA_ROOT": str(data_root),
            "BONGUS_AUDIT_DB_PATH": str(data_root / "audit.db"),
            "BONGUS_RESEARCH_DB_PATH": str(data_root / "research.db"),
        },
        project_root=project_root,
        configured_state_path=str(project_root / "state.db"),
        configured_audit_path=str(project_root / "audit.db"),
        configured_research_path=str(project_root / "research.db"),
    )

    assert resolved == (
        data_root.resolve(),
        (data_root / "state.db").resolve(),
        (data_root / "audit.db").resolve(),
        (data_root / "research.db").resolve(),
    )
    assert king_watchdog._ENV["BONGUS_DATA_ROOT"] == str(
        king_watchdog.BONGUS_DATA_ROOT
    )
    assert king_watchdog._ENV["BONGUS_STATE_DB_PATH"] == str(
        king_watchdog.STATE_DATABASE_PATH
    )
    assert king_watchdog._ENV["BONGUS_AUDIT_DB_PATH"] == str(
        king_watchdog.AUDIT_DATABASE_PATH
    )
    assert king_watchdog._ENV["BONGUS_RESEARCH_DB_PATH"] == str(
        king_watchdog.RESEARCH_DATABASE_PATH
    )
    assert king_watchdog._ENV["PYTHONDONTWRITEBYTECODE"] == "1"
    runtime_root = os.path.abspath(str(king_watchdog.BONGUS_DATA_ROOT))
    for path in (
        king_watchdog._LOG_FILE,
        king_watchdog._WATCHDOG_LOCK_PATH,
        king_watchdog._WATCHDOG_STATE_PATH,
        king_watchdog.TRADER_HEARTBEAT_FILE,
        king_watchdog.STORAGE_HEALTH_FILE,
        str(king_watchdog.RUST_RUNTIME_DIR),
        king_watchdog._ENV["BONGUS_SENTIMENT_PATH"],
        king_watchdog._ENV["BONGUS_RUNTIME_DIR"],
        king_watchdog._ENV["BONGUS_STORAGE_RESERVE_PATH"],
    ):
        assert os.path.commonpath([runtime_root, os.path.abspath(path)]) == runtime_root
    assert king_watchdog._ENV["BONGUS_LOG_PATH"] == king_watchdog._LOG_FILE
    assert (
        king_watchdog._ENV["BONGUS_RUNTIME_HEARTBEAT_PATH"]
        == king_watchdog.TRADER_HEARTBEAT_FILE
    )
    for environment_name in (
        "EXECUTION_STATE_JOURNAL_PATH",
        "EXECUTION_INTENT_JOURNAL_PATH",
        "EXECUTION_TELEMETRY_JOURNAL_PATH",
        "EXECUTION_TELEMETRY_CURSOR_PATH",
        "EXECUTION_STORAGE_CONTROL_PATH",
        "PRIVATE_STREAM_CURSOR_DIR",
    ):
        assert os.path.commonpath(
            [runtime_root, os.path.abspath(king_watchdog._ENV[environment_name])]
        ) == runtime_root


def test_watchdog_rejects_split_role_path_outside_manifest_root(tmp_path):
    data_root = tmp_path / "data-volume"
    with pytest.raises(RuntimeError, match="manifest-bound data root"):
        king_watchdog._resolve_runtime_database_paths(
            {
                "BONGUS_DATA_ROOT": str(data_root),
                "BONGUS_RESEARCH_DB_PATH": str(
                    tmp_path / "research-volume" / "research.db"
                ),
            },
            project_root=tmp_path / "application",
            configured_state_path=str(tmp_path / "application" / "state.db"),
            configured_audit_path=str(tmp_path / "application" / "audit.db"),
            configured_research_path=str(
                tmp_path / "application" / "research.db"
            ),
        )


def test_watchdog_rejects_mutable_artifact_path_outside_data_root(tmp_path):
    data_root = (tmp_path / "data").resolve()
    with pytest.raises(RuntimeError, match="must remain under"):
        king_watchdog._resolve_runtime_artifact_path(
            {
                "BONGUS_STORAGE_HEALTH_SNAPSHOT_PATH": str(
                    tmp_path / "release" / "runtime" / "storage_health.json"
                )
            },
            "BONGUS_STORAGE_HEALTH_SNAPSHOT_PATH",
            data_root=data_root,
            default_relative_path=Path("runtime", "storage_health.json"),
        )


def test_database_maintenance_prunes_only_audit_b_and_research_c(tmp_path):
    state_path = tmp_path / "state.db"
    audit_path = tmp_path / "audit.db"
    research_path = tmp_path / "research.db"
    old_time = "2000-01-01 00:00:00"
    fresh_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    with sqlite3.connect(state_path) as connection:
        connection.execute(
            "CREATE TABLE positions (id INTEGER PRIMARY KEY, opened_at TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO positions(opened_at) VALUES (?)", (old_time,))
    with sqlite3.connect(audit_path) as connection:
        connection.execute(
            "CREATE TABLE economic_ledger_events "
            "(id INTEGER PRIMARY KEY, event_time TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO economic_ledger_events(event_time) VALUES (?)",
            (old_time,),
        )
        for table, column, _interval in king_watchdog._AUDIT_PRUNE_RULES:
            connection.execute(
                f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, {column} TEXT NOT NULL)"
            )
            connection.executemany(
                f"INSERT INTO {table}({column}) VALUES (?)",
                [(old_time,), (fresh_time,)],
            )
    with sqlite3.connect(research_path) as connection:
        for table, column, _interval in king_watchdog._RESEARCH_PRUNE_RULES:
            if table in {"market_samples", "market_hourly_aggregates"}:
                continue
            connection.execute(
                f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, {column} TEXT NOT NULL)"
            )
            connection.executemany(
                f"INSERT INTO {table}({column}) VALUES (?)",
                [(old_time,), (fresh_time,)],
            )
        connection.execute(
            """CREATE TABLE market_samples (
                   id INTEGER PRIMARY KEY,
                   sample_minute TEXT NOT NULL,
                   symbol TEXT NOT NULL,
                   ann_funding REAL NOT NULL,
                   basis_pct REAL NOT NULL,
                   mark_price REAL NOT NULL,
                   minute_notional_volume REAL NOT NULL
               )"""
        )
        connection.executemany(
            "INSERT INTO market_samples "
            "(sample_minute, symbol, ann_funding, basis_pct, mark_price, "
            "minute_notional_volume) VALUES (?, 'BTCUSDT', 0.1, 0.001, 100, 1000)",
            [(old_time,), (fresh_time,)],
        )
        connection.execute(
            """CREATE TABLE market_hourly_aggregates (
                   bucket_hour TEXT NOT NULL,
                   symbol TEXT NOT NULL,
                   sample_count INTEGER NOT NULL,
                   ann_funding_avg REAL NOT NULL,
                   ann_funding_min REAL NOT NULL,
                   ann_funding_max REAL NOT NULL,
                   basis_pct_avg REAL NOT NULL,
                   basis_pct_min REAL NOT NULL,
                   basis_pct_max REAL NOT NULL,
                   mark_price_avg REAL NOT NULL,
                   mark_price_min REAL NOT NULL,
                   mark_price_max REAL NOT NULL,
                   notional_volume_sum REAL NOT NULL,
                   source_first_minute TEXT NOT NULL,
                   source_last_minute TEXT NOT NULL,
                   refreshed_at TEXT NOT NULL,
                   PRIMARY KEY (bucket_hour, symbol)
               )"""
        )
        connection.executemany(
            "INSERT INTO market_hourly_aggregates VALUES "
            "(?, 'BTCUSDT', 1, 0.1, 0.1, 0.1, 0.001, 0.001, 0.001, "
            "100, 100, 100, 1000, ?, ?, ?)",
            [
                (old_time, old_time, old_time, old_time),
                (fresh_time, fresh_time, fresh_time, fresh_time),
            ],
        )

    maintenance = king_watchdog.DatabaseMaintenance(
        str(state_path),
        str(audit_path),
        str(research_path),
    )
    maintenance._prune()

    with sqlite3.connect(state_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 1
    with sqlite3.connect(audit_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM economic_ledger_events"
            ).fetchone()[0]
            == 1
        )
        for table, _column, _interval in king_watchdog._AUDIT_PRUNE_RULES:
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
    with sqlite3.connect(research_path) as connection:
        for table, _column, _interval in king_watchdog._RESEARCH_PRUNE_RULES:
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1


def test_database_maintenance_checkpoints_all_roles_without_creating_missing_files(
    tmp_path,
    monkeypatch,
):
    paths = [tmp_path / name for name in ("state.db", "audit.db", "research.db")]
    maintenance = king_watchdog.DatabaseMaintenance(*(str(path) for path in paths))
    checkpoints: list[tuple[str, object]] = []
    monkeypatch.setattr(maintenance, "_prune_role", lambda *args: None)
    monkeypatch.setattr(
        maintenance,
        "_checkpoint",
        lambda role, path: checkpoints.append((role, path)),
    )

    maintenance._prune()

    assert [role for role, _path in checkpoints] == ["state", "audit", "research"]
    assert not any(path.exists() for path in paths)


def _write_storage_snapshot(
    path,
    *,
    state="healthy",
    instantaneous_state=None,
    risk_blocked=False,
    emergency_latched=False,
    recovery_ready=False,
    integrity_ok=True,
    observed_at=None,
):
    payload = {
        "generation": 9,
        "observed_at": (
            observed_at or datetime.now(timezone.utc).isoformat()
        ),
        "state": state,
        "instantaneous_state": instantaneous_state or state,
        "reasons": [],
        "volumes": [],
        "components": [],
        "risk_increase_blocked": risk_blocked,
        "emergency_latched": emergency_latched,
        "healthy_recovery_samples": 3 if recovery_ready else 0,
        "recovery_samples_required": 3,
        "recovery_ready_for_operator": recovery_ready,
        "integrity_ok": integrity_ok,
        "exchange_reconciled": True,
        "active_faults": [],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_storage_orchestration_suppresses_every_optional_process_but_survival_path(
    tmp_path,
    monkeypatch,
):
    snapshot_path = tmp_path / "storage.json"
    _write_storage_snapshot(
        snapshot_path,
        state="degraded",
        risk_blocked=True,
    )
    monkeypatch.setattr(king_watchdog, "STORAGE_HEALTH_FILE", str(snapshot_path))

    storage = king_watchdog._read_storage_orchestration_state()

    assert storage.optional_processes_suppressed
    for name in ("scraper", "dashboard", "supervisor"):
        assert not king_watchdog._storage_process_allowed(name, storage)
    # Rust and trader contain the private stream, reconciliation and storage
    # monitor; telegram is the minimal alert path.
    for name in ("rust", "trader", "telegram"):
        assert king_watchdog._storage_process_allowed(name, storage)


def test_missing_atomic_snapshot_starts_only_survival_processes(tmp_path, monkeypatch):
    missing_path = tmp_path / "missing-storage.json"
    monkeypatch.setattr(king_watchdog, "STORAGE_HEALTH_FILE", str(missing_path))

    storage = king_watchdog._read_storage_orchestration_state()

    assert not storage.available
    assert storage.optional_processes_suppressed
    assert not king_watchdog._storage_process_allowed("dashboard", storage)
    assert not king_watchdog._storage_process_allowed("scraper", storage)
    assert king_watchdog._storage_process_allowed("rust", storage)
    assert king_watchdog._storage_process_allowed("trader", storage)
    assert king_watchdog._storage_process_allowed("telegram", storage)


def test_only_dashboard_is_reenabled_for_fresh_atomic_recovery_proof(
    tmp_path,
    monkeypatch,
):
    snapshot_path = tmp_path / "storage.json"
    _write_storage_snapshot(
        snapshot_path,
        state="healthy",
        instantaneous_state="healthy",
        risk_blocked=True,
        emergency_latched=True,
        recovery_ready=True,
    )
    monkeypatch.setattr(king_watchdog, "STORAGE_HEALTH_FILE", str(snapshot_path))

    storage = king_watchdog._read_storage_orchestration_state()

    assert storage.recovery_dashboard_allowed
    assert king_watchdog._storage_process_allowed("dashboard", storage)
    assert not king_watchdog._storage_process_allowed("scraper", storage)
    assert not king_watchdog._storage_process_allowed("supervisor", storage)


def test_scraper_and_supervisor_wait_for_explicit_risk_latch_clear(
    tmp_path,
    monkeypatch,
):
    snapshot_path = tmp_path / "storage.json"
    _write_storage_snapshot(snapshot_path, state="healthy", risk_blocked=False)
    monkeypatch.setattr(king_watchdog, "STORAGE_HEALTH_FILE", str(snapshot_path))

    storage = king_watchdog._read_storage_orchestration_state()

    assert not storage.optional_processes_suppressed
    assert king_watchdog._storage_process_allowed("scraper", storage)
    assert king_watchdog._storage_process_allowed("supervisor", storage)
    assert king_watchdog._storage_process_allowed("dashboard", storage)


def test_bootstrap_snapshot_suppresses_optional_processes_until_integrity_proven(
    tmp_path,
    monkeypatch,
):
    snapshot_path = tmp_path / "storage.json"
    _write_storage_snapshot(
        snapshot_path,
        state="healthy",
        risk_blocked=False,
        integrity_ok=False,
    )
    monkeypatch.setattr(king_watchdog, "STORAGE_HEALTH_FILE", str(snapshot_path))

    storage = king_watchdog._read_storage_orchestration_state()

    assert not storage.integrity_ok
    assert storage.optional_processes_suppressed
    for name in ("scraper", "dashboard", "supervisor"):
        assert not king_watchdog._storage_process_allowed(name, storage)
    for name in ("rust", "trader", "telegram"):
        assert king_watchdog._storage_process_allowed(name, storage)

    _write_storage_snapshot(
        snapshot_path,
        state="healthy",
        risk_blocked=False,
        integrity_ok=True,
    )
    storage = king_watchdog._read_storage_orchestration_state()

    assert storage.integrity_ok
    assert not storage.optional_processes_suppressed


def test_stale_recovery_proof_does_not_start_dashboard(tmp_path, monkeypatch):
    snapshot_path = tmp_path / "storage.json"
    _write_storage_snapshot(
        snapshot_path,
        state="healthy",
        risk_blocked=True,
        recovery_ready=True,
        observed_at=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
    )
    monkeypatch.setattr(king_watchdog, "STORAGE_HEALTH_FILE", str(snapshot_path))

    storage = king_watchdog._read_storage_orchestration_state()

    assert not storage.snapshot_fresh
    assert not storage.recovery_dashboard_allowed
    assert not king_watchdog._storage_process_allowed("dashboard", storage)


def test_stale_healthy_snapshot_cannot_keep_optional_writers_running(
    tmp_path,
    monkeypatch,
):
    snapshot_path = tmp_path / "storage.json"
    _write_storage_snapshot(
        snapshot_path,
        state="healthy",
        risk_blocked=False,
        observed_at=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
    )
    monkeypatch.setattr(king_watchdog, "STORAGE_HEALTH_FILE", str(snapshot_path))

    storage = king_watchdog._read_storage_orchestration_state()

    assert storage.optional_processes_suppressed
    for name in ("scraper", "dashboard", "supervisor"):
        assert not king_watchdog._storage_process_allowed(name, storage)


def test_storage_pressure_stops_project_owned_backup_jobs(monkeypatch):
    backup = object()
    calls = []
    monkeypatch.setattr(
        king_watchdog,
        "_find_managed_project_processes",
        lambda *names: calls.append(("find", names)) or [backup],
    )
    monkeypatch.setattr(
        king_watchdog,
        "_terminate_processes",
        lambda procs, *, reason: calls.append(("terminate", procs, reason)),
    )

    king_watchdog._stop_project_backup_jobs_for_storage("emergency")

    assert calls[0] == ("find", ("backup_job",))
    assert calls[1][0] == "terminate"
    assert calls[1][1] == [backup]
    assert "emergency" in calls[1][2]


def test_private_stream_cursor_is_bounded_under_rust_runtime_directory():
    cursor_dir = os.path.abspath(king_watchdog._ENV["PRIVATE_STREAM_CURSOR_DIR"])
    runtime_dir = os.path.abspath(str(king_watchdog.RUST_RUNTIME_DIR))

    assert os.path.commonpath([cursor_dir, runtime_dir]) == runtime_dir


def test_trader_blocked_exit_is_logged_once(monkeypatch):
    proc = _FakeExitedProc(returncode=king_watchdog.TRADER_BLOCKED_EXIT_CODE)
    tracker = king_watchdog.CrashTracker()
    messages: list[str] = []

    monkeypatch.setattr(king_watchdog, "_log", messages.append)
    monkeypatch.setattr(king_watchdog, "_read_trader_block_state", lambda: (None, None))
    monkeypatch.setattr(
        king_watchdog,
        "_autonomous_startup_recovery_enabled",
        lambda: False,
    )

    first = king_watchdog.check_and_restart(
        proc,
        ["python", "scripts/live_trader.py"],
        "trader",
        ".",
        tracker,
    )
    second = king_watchdog.check_and_restart(
        proc,
        ["python", "scripts/live_trader.py"],
        "trader",
        ".",
        tracker,
    )

    assert first is proc
    assert second is proc
    assert tracker.permanently_failed is True
    assert sum("BLOCKED mode" in message for message in messages) == 1


def test_autonomous_startup_recovery_requires_validated_non_live_config(tmp_path):
    config_path = tmp_path / "live_config.json"
    config_path.write_text(
        json.dumps({"autonomous_startup_recovery": True}),
        encoding="utf-8",
    )
    manager = king_watchdog.ConfigManager(
        config_path=config_path,
        trading_mode="testnet",
    )

    assert king_watchdog._autonomous_startup_recovery_enabled(
        env={"TRADING_MODE": "testnet"},
        config_manager=manager,
    )
    assert king_watchdog._autonomous_startup_recovery_enabled(
        env={"TRADING_MODE": "paper"},
        config_manager=manager,
    )
    assert not king_watchdog._autonomous_startup_recovery_enabled(
        env={"TRADING_MODE": "live"},
        config_manager=manager,
    )
    assert not king_watchdog._autonomous_startup_recovery_enabled(
        env={"TRADING_MODE": "unexpected"},
        config_manager=manager,
    )


def test_autonomous_startup_recovery_rejects_missing_or_invalid_override(tmp_path):
    missing_path = tmp_path / "missing-key.json"
    missing_path.write_text(json.dumps({"pause_new_entries": True}), encoding="utf-8")
    missing = king_watchdog.ConfigManager(config_path=missing_path, trading_mode="testnet")
    assert not king_watchdog._autonomous_startup_recovery_enabled(
        env={"TRADING_MODE": "testnet"},
        config_manager=missing,
    )

    invalid_path = tmp_path / "invalid.json"
    invalid_path.write_text(
        json.dumps({"autonomous_startup_recovery": "sometimes"}),
        encoding="utf-8",
    )
    invalid = king_watchdog.ConfigManager(config_path=invalid_path, trading_mode="testnet")
    assert invalid.last_error
    assert not king_watchdog._autonomous_startup_recovery_enabled(
        env={"TRADING_MODE": "testnet"},
        config_manager=invalid,
    )


def test_trader_blocked_exit_autonomous_retry_runs_after_persisted_delay(monkeypatch):
    proc = _FakeExitedProc(returncode=king_watchdog.TRADER_BLOCKED_EXIT_CODE)
    tracker = king_watchdog.CrashTracker()
    replacement = object()
    clock = [1_000.0]
    starts: list[tuple[object, str, object]] = []

    monkeypatch.setattr(king_watchdog.time, "time", lambda: clock[0])
    monkeypatch.setattr(king_watchdog, "_read_trader_block_state", lambda: (None, None))
    monkeypatch.setattr(
        king_watchdog,
        "_autonomous_startup_recovery_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        king_watchdog,
        "_start_block_reason",
        lambda name, ignore_pids=None: None,
    )
    monkeypatch.setattr(
        king_watchdog,
        "start_process",
        lambda command, name, cwd=None: starts.append((command, name, cwd)) or replacement,
    )

    first = king_watchdog.check_and_restart(
        proc,
        ["python", "scripts/live_trader.py"],
        "trader",
        ".",
        tracker,
        started_at=900.0,
    )
    assert first is proc
    assert tracker.backoff_until == 1_030.0
    assert len(tracker.crash_times) == 1

    clock[0] = 1_029.0
    waiting = king_watchdog.check_and_restart(
        proc,
        ["python", "scripts/live_trader.py"],
        "trader",
        ".",
        tracker,
        started_at=900.0,
    )
    assert waiting is proc
    assert len(tracker.crash_times) == 1
    assert starts == []

    clock[0] = 1_030.0
    restarted = king_watchdog.check_and_restart(
        proc,
        ["python", "scripts/live_trader.py"],
        "trader",
        ".",
        tracker,
        started_at=900.0,
    )
    assert restarted is replacement
    assert len(starts) == 1


def test_trader_bridge_preflight_blocked_exit_retries_after_rust_ready(monkeypatch):
    proc = _FakeExitedProc(returncode=king_watchdog.TRADER_BLOCKED_EXIT_CODE)
    rust_proc = _FakeProc()
    tracker = king_watchdog.CrashTracker()
    replacement = object()
    messages: list[str] = []

    monkeypatch.setattr(king_watchdog, "_log", messages.append)
    monkeypatch.setattr(
        king_watchdog,
        "_read_trader_block_state",
        lambda: ("execution bridge preflight failed", "blocked_execution_bridge"),
    )
    monkeypatch.setattr(king_watchdog, "_wait_for_rust_ipc", lambda timeout=30: None)
    monkeypatch.setattr(king_watchdog, "_rust_ipc_ready", lambda: True)
    monkeypatch.setattr(
        king_watchdog,
        "_autonomous_startup_recovery_enabled",
        lambda: True,
    )
    monkeypatch.setattr(king_watchdog, "start_process", lambda command, name, cwd=None: replacement)

    result = king_watchdog.check_and_restart(
        proc,
        ["python", "scripts/live_trader.py"],
        "trader",
        ".",
        tracker,
        all_procs={"rust": rust_proc},  # type: ignore
    )

    assert result is replacement
    assert tracker.permanently_failed is False
    assert any("retrying trader startup" in message for message in messages)


def test_live_trader_bridge_blocked_exit_stays_fail_closed(monkeypatch):
    proc = _FakeExitedProc(returncode=king_watchdog.TRADER_BLOCKED_EXIT_CODE)
    rust_proc = _FakeProc()
    tracker = king_watchdog.CrashTracker()
    calls: list[str] = []

    monkeypatch.setitem(king_watchdog._ENV, "TRADING_MODE", "live")
    monkeypatch.setattr(
        king_watchdog,
        "_read_trader_block_state",
        lambda: ("execution bridge preflight failed", "blocked_execution_bridge"),
    )
    monkeypatch.setattr(
        king_watchdog,
        "_wait_for_rust_ipc",
        lambda timeout=30: calls.append("wait"),
    )
    monkeypatch.setattr(king_watchdog, "_rust_ipc_ready", lambda: True)
    monkeypatch.setattr(
        king_watchdog,
        "start_process",
        lambda command, name, cwd=None: calls.append("start"),
    )

    result = king_watchdog.check_and_restart(
        proc,
        ["python", "scripts/live_trader.py"],
        "trader",
        ".",
        tracker,
        all_procs={"rust": rust_proc},  # type: ignore
    )

    assert result is proc
    assert tracker.permanently_failed is True
    assert calls == []


def test_read_trader_liveness_uses_recent_runtime_progress(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE risk_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    stale_alive = datetime.now(timezone.utc) - timedelta(minutes=10)
    fresh_runtime = datetime.now(timezone.utc) - timedelta(seconds=4)
    conn.executemany(
        "INSERT INTO risk_state (key, value, updated_at) VALUES (?, ?, ?)",
        [
            ("runtime_mode", '"LIVE"', fresh_runtime.isoformat()),
            ("heartbeat_status", '"ok"', fresh_runtime.isoformat()),
            ("loop_last_alive_at", stale_alive.isoformat(), stale_alive.isoformat()),
        ],
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(king_watchdog, "TRADER_STATE_DB", str(db_path))
    monkeypatch.setattr(king_watchdog, "TRADER_HEARTBEAT_FILE", str(tmp_path / "runtime_heartbeat.json"))

    runtime_mode, last_alive, safe_mode_reason, mode_changed_at, loop_heartbeat_ages = king_watchdog._read_trader_liveness()

    assert runtime_mode == "LIVE"
    assert last_alive is not None
    assert abs((last_alive - fresh_runtime).total_seconds()) < 1
    assert safe_mode_reason is None
    assert mode_changed_at is None


def test_trader_recent_runtime_progress_skips_restart_after_grace(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE risk_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    stale_alive = datetime.now(timezone.utc) - timedelta(minutes=10)
    fresh_runtime = datetime.now(timezone.utc) - timedelta(seconds=5)
    conn.executemany(
        "INSERT INTO risk_state (key, value, updated_at) VALUES (?, ?, ?)",
        [
            ("runtime_mode", '"LIVE"', fresh_runtime.isoformat()),
            ("preflight_status", '"passed"', fresh_runtime.isoformat()),
            ("loop_last_alive_at", stale_alive.isoformat(), stale_alive.isoformat()),
            (
                "loop_heartbeat_ages",
                json.dumps(
                    {
                        name: 1.0
                        for name in king_watchdog.TRADER_REQUIRED_PROGRESS_LOOPS
                    }
                ),
                fresh_runtime.isoformat(),
            ),
        ],
    )
    conn.commit()
    conn.close()

    proc = _FakeProc()
    tracker = king_watchdog.CrashTracker()
    restarted = False

    monkeypatch.setattr(king_watchdog, "TRADER_STATE_DB", str(db_path))
    monkeypatch.setattr(king_watchdog, "TRADER_HEARTBEAT_FILE", str(tmp_path / "runtime_heartbeat.json"))
    monkeypatch.setattr(king_watchdog.psutil, "Process", lambda pid: _FakePsutilProc())

    def fake_start_process(command, name, cwd=None):
        del command, name, cwd
        nonlocal restarted
        restarted = True
        return object()

    monkeypatch.setattr(king_watchdog, "start_process", fake_start_process)

    result = king_watchdog.check_and_restart(
        proc,
        ["python", "scripts/live_trader.py"],
        "trader",
        ".",
        tracker,
        started_at=king_watchdog.time.time() - king_watchdog.TRADER_LIVENESS_STARTUP_GRACE_SECONDS - 5,
    )

    assert result is proc
    assert restarted is False
    assert proc.terminated is False
