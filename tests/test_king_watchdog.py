from datetime import datetime, timedelta, timezone

from bongus.monitoring import king_watchdog


class _FakeProc:
    def __init__(self) -> None:
        self.pid = 1234
        self.returncode = None
        self.terminated = False

    def poll(self):
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

    def poll(self):
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
    monkeypatch.setattr(king_watchdog, "_read_trader_liveness", lambda: ("PAPER", old_alive))

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
    monkeypatch.setattr(king_watchdog, "_read_trader_liveness", lambda: ("PAPER", old_alive))
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
