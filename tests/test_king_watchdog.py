import sqlite3
from datetime import datetime, timedelta, timezone

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
    monkeypatch.setattr(king_watchdog, "_read_trader_liveness", lambda: ("PAPER", old_alive, None, None))

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
    monkeypatch.setattr(king_watchdog, "_read_trader_liveness", lambda: ("PAPER", old_alive, None, None))
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
    assert "telegram" not in names
    assert "scraper" in names
    assert "dashboard" in names
    assert "supervisor" in names
    assert skipped == king_watchdog._RUST_REQUIRED_PROCESS_NAMES


def test_trader_blocked_exit_is_logged_once(monkeypatch):
    proc = _FakeExitedProc(returncode=king_watchdog.TRADER_BLOCKED_EXIT_CODE)
    tracker = king_watchdog.CrashTracker()
    messages: list[str] = []

    monkeypatch.setattr(king_watchdog, "_log", messages.append)

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

    runtime_mode, last_alive, safe_mode_reason, mode_changed_at = king_watchdog._read_trader_liveness()

    assert runtime_mode == "LIVE"
    assert last_alive is not None
    assert abs((last_alive - fresh_runtime).total_seconds()) < 1


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
        ],
    )
    conn.commit()
    conn.close()

    proc = _FakeProc()
    tracker = king_watchdog.CrashTracker()
    restarted = False

    monkeypatch.setattr(king_watchdog, "TRADER_STATE_DB", str(db_path))
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
