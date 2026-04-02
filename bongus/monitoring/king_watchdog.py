import datetime
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time

import psutil
from dotenv import load_dotenv

from bongus.core.config import DEFAULT_MONITORED_SYMBOLS
from bongus.core.config_manager import ConfigManager

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DOTENV_PATH = os.path.join(_PROJECT_ROOT, ".env")
load_dotenv(_DOTENV_PATH)

_ENV = {
    **os.environ,
    "PYTHONPATH": _PROJECT_ROOT,
    "PYTHONUNBUFFERED": "1",
}
if not str(_ENV.get("MONITORED_SYMBOLS", "")).strip():
    _ENV["MONITORED_SYMBOLS"] = ",".join(DEFAULT_MONITORED_SYMBOLS)

# ── Unified log file (same path the dashboard reads) ───────────────────────
_LOG_DIR = os.path.join(_PROJECT_ROOT, "scripts", "logs")
os.makedirs(_LOG_DIR, exist_ok=True)
_LOG_FILE = os.path.join(_LOG_DIR, "live_trader.log")
_log_lock = threading.Lock()


def _ts() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]


def _write(line: str) -> None:
    with _log_lock, open(_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()


def _log(msg: str) -> None:
    """Tee watchdog's own messages to stdout and the log file."""
    print(msg, flush=True)
    _write(f"{_ts()} INFO [watchdog] {msg}")


def _pipe_reader(stream, label: str) -> None:
    """Drain a subprocess pipe; tee each line to stdout + log file."""
    try:
        for raw in iter(stream.readline, b""):
            text = raw.decode("utf-8", errors="replace").rstrip()
            if text:
                print(f"[{label}] {text}", flush=True)
                _write(f"{_ts()} INFO [{label}] {text}")
    except Exception:
        pass
    finally:
        stream.close()


RUST_ENGINE_DIR = os.path.join(_PROJECT_ROOT, "execution_engine")
RUST_BUILD_COMMAND = ["cargo", "build", "--release"]
RUST_COMMAND = ["cargo", "run", "--release"]
PYTHON_COMMAND = [sys.executable, "scripts/live_trader.py"]
SCRAPER_COMMAND = [sys.executable, "bongus/strategies/sentiment_scraper.py"]
_DASHBOARD_HOST = str(_ENV.get("DASHBOARD_HOST", "0.0.0.0")).strip() or "0.0.0.0"
_DASHBOARD_PORT = str(_ENV.get("DASHBOARD_PORT", "8080")).strip() or "8080"
DASHBOARD_COMMAND = [
    sys.executable, "-m", "uvicorn",
    "bongus.monitoring.web_dashboard:app",
    "--host", _DASHBOARD_HOST, "--port", _DASHBOARD_PORT,
]
SUPERVISOR_COMMAND = [sys.executable, "-m", "bongus.monitoring.supervisor_service"]
TELEGRAM_COMMAND = [sys.executable, "bongus/monitoring/telegram_alerter.py"]

MEMORY_LIMIT_MB = 1024

CRASH_WINDOW_SECONDS = 120
MAX_CRASHES_IN_WINDOW = 5
INITIAL_BACKOFF_SECONDS = 10
MAX_BACKOFF_SECONDS = 600
BACKOFF_MULTIPLIER = 2
STABLE_THRESHOLD_SECONDS = 60
QUICK_EXIT_WINDOW_SECONDS = 15
QUICK_EXIT_MAX_CRASHES = 3
TRADER_BLOCKED_EXIT_CODE = 78
TRADER_STATE_DB = os.path.join(_PROJECT_ROOT, "state.db")
TRADER_LIVENESS_STALE_SECONDS = 90
TRADER_LIVENESS_STARTUP_GRACE_SECONDS = 120


def _safe_env(name: str, default: str) -> str:
    raw = _ENV.get(name)
    if raw is None:
        return default
    text = str(raw).strip()
    return text or default


def _log_runtime_config() -> None:
    sentiment_enabled = bool(ConfigManager().get("sentiment_enabled"))
    dotenv_status = "present" if os.path.exists(_DOTENV_PATH) else "missing"
    _log(
        "Runtime config: "
        f".env={dotenv_status} "
        f"TRADING_MODE={_safe_env('TRADING_MODE', 'paper')} "
        f"ACCOUNT_EQUITY_USD={_safe_env('ACCOUNT_EQUITY_USD', '10000')} "
        f"MAX_GROSS_EXPOSURE_USD={_safe_env('MAX_GROSS_EXPOSURE_USD', '50000')} "
        f"MONITORED_SYMBOLS={_safe_env('MONITORED_SYMBOLS', '<default>')} "
        f"DASHBOARD_BIND={_safe_env('DASHBOARD_HOST', '0.0.0.0')}:{_safe_env('DASHBOARD_PORT', '8080')} "
        f"SENTIMENT_ENABLED={sentiment_enabled}"
    )


class CrashTracker:
    """Per-process crash history with exponential backoff and quick-exit detection."""

    def __init__(self):
        self.crash_times: list[float] = []
        self.backoff_until: float = 0.0
        self.current_backoff: float = 0.0
        self.permanently_failed: bool = False

    def record_crash(self) -> None:
        if self.permanently_failed:
            return

        now = time.time()
        self.crash_times.append(now)

        # Check for quick-exit (e.g. 3 crashes within 15 seconds)
        quick_exit_cutoff = now - QUICK_EXIT_WINDOW_SECONDS
        recent_crashes = [t for t in self.crash_times if t >= quick_exit_cutoff]
        if len(recent_crashes) >= QUICK_EXIT_MAX_CRASHES:
            self.permanently_failed = True
            return

        cutoff = now - CRASH_WINDOW_SECONDS
        self.crash_times = [t for t in self.crash_times if t >= cutoff]

        if len(self.crash_times) >= MAX_CRASHES_IN_WINDOW:
            if self.current_backoff == 0:
                self.current_backoff = INITIAL_BACKOFF_SECONDS
            else:
                self.current_backoff = min(
                    self.current_backoff * BACKOFF_MULTIPLIER,
                    MAX_BACKOFF_SECONDS,
                )
            self.backoff_until = now + self.current_backoff

    def should_restart(self) -> bool:
        if self.permanently_failed:
            return False
        return time.time() >= self.backoff_until

    def reset(self) -> None:
        self.crash_times.clear()
        self.current_backoff = 0.0
        self.backoff_until = 0.0
        self.permanently_failed = False


def _parse_iso_timestamp(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def _read_trader_liveness() -> tuple[str | None, datetime.datetime | None]:
    if not os.path.exists(TRADER_STATE_DB):
        return None, None
    try:
        with sqlite3.connect(TRADER_STATE_DB, timeout=2) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT key, value FROM risk_state WHERE key IN ('runtime_mode', 'loop_last_alive_at')"
            ).fetchall()
    except sqlite3.Error:
        return None, None

    runtime_mode = None
    loop_last_alive_at = None
    for row in rows:
        key = str(row["key"])
        raw_value = row["value"]
        try:
            parsed_value = json.loads(raw_value)
        except Exception:
            parsed_value = raw_value
        if key == "runtime_mode":
            runtime_mode = str(parsed_value)
        elif key == "loop_last_alive_at":
            loop_last_alive_at = _parse_iso_timestamp(str(parsed_value))
    return runtime_mode, loop_last_alive_at


def _wait_for_rust_ipc(host: str = "127.0.0.1", port: int = 9000, timeout: float = 30.0) -> None:
    """Block until the Rust engine's TCP broadcast port is accepting connections."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                _log(f"[WATCHDOG] Rust IPC ready on {host}:{port}.")
                return
        except OSError:
            time.sleep(0.5)
    _log(f"[WATCHDOG] Rust IPC did not become ready within {timeout}s — proceeding anyway.")


def start_process(command, name: str, cwd=None):
    run_cwd = cwd or _PROJECT_ROOT
    _log(f"Starting {name}: {' '.join(command)} (cwd={run_cwd})")
    proc = subprocess.Popen(
        command, cwd=run_cwd, env=_ENV,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    threading.Thread(target=_pipe_reader, args=(proc.stdout, name), daemon=True).start()
    threading.Thread(target=_pipe_reader, args=(proc.stderr, name), daemon=True).start()
    return proc


def check_and_restart(proc, command, name: str, cwd, tracker: CrashTracker, started_at: float | None = None):
    if proc.poll() is not None:
        exit_code = proc.returncode

        if name == "trader" and exit_code == TRADER_BLOCKED_EXIT_CODE:
            tracker.permanently_failed = True
            _log(
                "[WATCHDOG] Trader exited in BLOCKED mode (exit=78). "
                "Leaving it stopped until an operator intervenes."
            )
            return proc

        # Avoid logging over and over if it's already permanently failed
        if tracker.permanently_failed:
            return proc

        tracker.record_crash()

        if tracker.permanently_failed:
            _log(f"[WATCHDOG] FATAL: {name} crashed {QUICK_EXIT_MAX_CRASHES} times within {QUICK_EXIT_WINDOW_SECONDS}s. Marking as permanently failed and stopping retries.")
            return proc

        if not tracker.should_restart():
            delay = tracker.backoff_until - time.time()
            _log(
                f"[WATCHDOG] {name} crashed (exit={exit_code}), "
                f"backoff active — next restart in {delay:.0f}s "
                f"({len(tracker.crash_times)} crashes in window)"
            )
            return proc

        _log(
            f"[WATCHDOG] {name} crashed (exit={exit_code}), restarting. "
            f"({len(tracker.crash_times)} crashes in window)"
        )
        return start_process(command, name=name, cwd=cwd)

    try:
        p = psutil.Process(proc.pid)
        mem_mb = p.memory_info().rss / (1024 * 1024)
        if mem_mb > MEMORY_LIMIT_MB:
            _log(f"[WATCHDOG] {name} memory spike ({mem_mb:.2f} MB)! Killing and restarting...")
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _log(f"[WATCHDOG] {name} did not terminate in 5s, sending SIGKILL...")
                proc.kill()
                proc.wait()
            return start_process(command, name=name, cwd=cwd)
    except psutil.NoSuchProcess:
        pass
    except Exception as e:
        _log(f"[WATCHDOG] Error monitoring {name}: {e}")

    if name == "trader" and proc.poll() is None:
        if started_at is not None and (time.time() - started_at) < TRADER_LIVENESS_STARTUP_GRACE_SECONDS:
            return proc
        runtime_mode, last_alive = _read_trader_liveness()
        if runtime_mode != "BLOCKED" and last_alive is not None:
            age = (
                datetime.datetime.now(datetime.timezone.utc) - last_alive
            ).total_seconds()
            if age > TRADER_LIVENESS_STALE_SECONDS:
                _log(
                    f"[WATCHDOG] trader loop liveness stale ({age:.0f}s). "
                    "Restarting trader process."
                )
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                return start_process(command, name=name, cwd=cwd)

    return proc


def run_preflight_checks() -> bool:
    """Run preflight checks before starting the main loop."""
    _log("Running preflight build check for Rust engine...")
    try:
        proc = subprocess.run(
            RUST_BUILD_COMMAND,
            cwd=RUST_ENGINE_DIR,
            env=_ENV,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="replace")
            _log(f"[WATCHDOG] FATAL: Preflight build check failed for Rust engine. Exit code: {proc.returncode}")
            _log(f"[WATCHDOG] stderr: {stderr}")
            if "openssl" in stderr.lower():
                _log("[WATCHDOG] HINT: It looks like openssl-sys failed to build. Try installing `libssl-dev` and `pkg-config` (e.g., `sudo apt-get install libssl-dev pkg-config`).")
            return False
        _log("Preflight build check passed.")
        return True
    except FileNotFoundError:
        _log("[WATCHDOG] FATAL: `cargo` command not found. Please install Rust and Cargo.")
        return False
    except Exception as e:
        _log(f"[WATCHDOG] FATAL: Unexpected error during preflight check: {e}")
        return False


def main():
    _log("Starting King Watchdog Supervisor...")
    _log_runtime_config()
    sentiment_enabled = bool(ConfigManager().get("sentiment_enabled"))

    # Preflight check for Rust engine
    rust_build_ok = run_preflight_checks()

    process_defs = [
        ("trader",    PYTHON_COMMAND,    _PROJECT_ROOT),
        ("dashboard", DASHBOARD_COMMAND, _PROJECT_ROOT),
        ("supervisor", SUPERVISOR_COMMAND, _PROJECT_ROOT),
        ("telegram",  TELEGRAM_COMMAND,  _PROJECT_ROOT),
    ]
    if sentiment_enabled:
        process_defs.insert(1, ("scraper", SCRAPER_COMMAND, _PROJECT_ROOT))
    else:
        _log("Sentiment scraper disabled by config.")
    if rust_build_ok:
        process_defs.insert(0, ("rust", RUST_COMMAND, RUST_ENGINE_DIR))
    else:
        _log("[WATCHDOG] Running in degraded mode without Rust engine.")

    trackers: dict[str, CrashTracker] = {name: CrashTracker() for name, _, _ in process_defs}
    start_times: dict[str, float] = {}
    procs: dict[str, subprocess.Popen] = {}

    for name, cmd, cwd in process_defs:
        procs[name] = start_process(cmd, name=name, cwd=cwd)
        start_times[name] = time.time()
        if name == "rust":
            _wait_for_rust_ipc(timeout=30)

    try:
        while True:
            time.sleep(10)
            for name, cmd, cwd in process_defs:
                proc = procs[name]
                tracker = trackers[name]

                # Reset crash history if process has been stable
                if proc.poll() is None and (time.time() - start_times.get(name, 0)) > STABLE_THRESHOLD_SECONDS:
                    if tracker.crash_times and not tracker.permanently_failed:
                        _log(f"[WATCHDOG] {name} stable for {STABLE_THRESHOLD_SECONDS}s, resetting crash history.")
                        tracker.reset()

                new_proc = check_and_restart(
                    proc,
                    cmd,
                    name,
                    cwd,
                    tracker,
                    started_at=start_times.get(name),
                )
                if new_proc is not proc:
                    start_times[name] = time.time()
                procs[name] = new_proc

    except KeyboardInterrupt:
        _log("Watchdog shutting down. Terminating child processes...")
        for name, proc in procs.items():
            if proc.poll() is None:
                proc.terminate()


if __name__ == "__main__":
    main()
