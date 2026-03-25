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


def start_process(command, name: str, cwd=None):
    _log(f"Starting {name}: {' '.join(command)}")
    proc = subprocess.Popen(
        command, cwd=cwd, env=_ENV,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    threading.Thread(target=_pipe_reader, args=(proc.stdout, name), daemon=True).start()
    threading.Thread(target=_pipe_reader, args=(proc.stderr, name), daemon=True).start()
    return proc


def check_and_restart(proc, command, name: str, cwd=None):
    if proc.poll() is not None:
        _log(f"[WATCHDOG] {name} crashed or stopped! Restarting...")
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


def main():
    _log("Starting King Watchdog Supervisor...")

    rust_proc = start_process(RUST_COMMAND, name="rust", cwd=RUST_ENGINE_DIR)
    time.sleep(2)

    python_proc = start_process(PYTHON_COMMAND, name="trader")
    scraper_proc = start_process(SCRAPER_COMMAND, name="scraper")
    dashboard_proc = start_process(DASHBOARD_COMMAND, name="dashboard")
    telegram_proc = start_process(TELEGRAM_COMMAND, name="telegram")

    try:
        while True:
            time.sleep(10)
            rust_proc = check_and_restart(rust_proc, RUST_COMMAND, name="rust", cwd=RUST_ENGINE_DIR)
            python_proc = check_and_restart(python_proc, PYTHON_COMMAND, name="trader")
            scraper_proc = check_and_restart(scraper_proc, SCRAPER_COMMAND, name="scraper")
            dashboard_proc = check_and_restart(dashboard_proc, DASHBOARD_COMMAND, name="dashboard")
            telegram_proc = check_and_restart(telegram_proc, TELEGRAM_COMMAND, name="telegram")

    except KeyboardInterrupt:
        _log("Watchdog shutting down. Terminating child processes...")
        rust_proc.terminate()
        python_proc.terminate()
        scraper_proc.terminate()
        dashboard_proc.terminate()
        telegram_proc.terminate()


if __name__ == "__main__":
    main()
