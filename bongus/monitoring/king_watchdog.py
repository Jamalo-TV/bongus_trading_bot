import datetime
import os
import subprocess
import sys
import threading
import time

import psutil
from dotenv import load_dotenv

load_dotenv()

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ENV = {
    **os.environ,
    "PYTHONPATH": _PROJECT_ROOT,
    "PYTHONUNBUFFERED": "1",
}

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


RUST_ENGINE_DIR = "execution_engine"
RUST_BUILD_COMMAND = ["cargo", "build", "--release"]
RUST_COMMAND = ["cargo", "run", "--release"]
PYTHON_COMMAND = [sys.executable, "scripts/live_trader_v2.py"]
SCRAPER_COMMAND = [sys.executable, "bongus/strategies/sentiment_scraper.py"]
DASHBOARD_COMMAND = [
    sys.executable, "-m", "uvicorn",
    "bongus.monitoring.web_dashboard:app",
    "--host", "0.0.0.0", "--port", "8080",
]
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


def start_process(command, name: str, cwd=None):
    _log(f"Starting {name}: {' '.join(command)}")
    proc = subprocess.Popen(
        command, cwd=cwd, env=_ENV,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    threading.Thread(target=_pipe_reader, args=(proc.stdout, name), daemon=True).start()
    threading.Thread(target=_pipe_reader, args=(proc.stderr, name), daemon=True).start()
    return proc


def check_and_restart(proc, command, name: str, cwd, tracker: CrashTracker):
    if proc.poll() is not None:
        exit_code = proc.returncode

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

    # Preflight check for Rust engine
    rust_build_ok = run_preflight_checks()

    process_defs = [
        ("trader",    PYTHON_COMMAND,    None),
        ("scraper",   SCRAPER_COMMAND,   None),
        ("dashboard", DASHBOARD_COMMAND, None),
        ("telegram",  TELEGRAM_COMMAND,  None),
    ]
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
            time.sleep(2)

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

                new_proc = check_and_restart(proc, cmd, name, cwd, tracker)
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
