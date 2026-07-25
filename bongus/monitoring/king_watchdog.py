import atexit
import datetime
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path

import psutil
from dotenv import load_dotenv

if __package__ in {None, ""}:
    _BOOTSTRAP_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if _BOOTSTRAP_ROOT not in sys.path:
        sys.path.insert(0, _BOOTSTRAP_ROOT)

from bongus.core.config import DEFAULT_MONITORED_SYMBOLS, AUTONOMOUS_STARTUP_RECOVERY
from bongus.core.config_manager import ConfigManager
from bongus.monitoring.log_artifacts import (
    archive_startup_artifacts,
    startup_archive_retention_from_env,
)

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


def _path_entries(value: str | None) -> list[str]:
    return [entry for entry in str(value or "").split(os.pathsep) if entry]


def _prepend_path_entries(env: dict[str, str], entries: list[str]) -> None:
    current_entries = _path_entries(env.get("PATH"))
    current_norm = {os.path.normcase(entry) for entry in current_entries}
    to_add: list[str] = []
    for entry in entries:
        if not entry:
            continue
        norm_entry = os.path.normcase(entry)
        if norm_entry in current_norm:
            continue
        current_norm.add(norm_entry)
        to_add.append(entry)
    env["PATH"] = os.pathsep.join([*to_add, *current_entries])


def _rust_toolchain_dirs() -> list[str]:
    candidates: list[str] = []
    cargo_home = str(os.environ.get("CARGO_HOME") or _ENV.get("CARGO_HOME") or "").strip()
    if cargo_home:
        candidates.append(os.path.join(cargo_home, "bin"))
    home_dir = os.path.expanduser("~")
    if home_dir:
        candidates.append(os.path.join(home_dir, ".cargo", "bin"))
    if os.name != "nt":
        candidates.append("/root/.cargo/bin")

    unique_dirs: list[str] = []
    seen: set[str] = set()
    for directory in candidates:
        if not directory or not os.path.isdir(directory):
            continue
        norm_directory = os.path.normcase(os.path.abspath(directory))
        if norm_directory in seen:
            continue
        seen.add(norm_directory)
        unique_dirs.append(directory)
    return unique_dirs


def _resolve_executable(executable: str, env: dict[str, str], extra_dirs: list[str]) -> str:
    resolved = shutil.which(executable, path=str(env.get("PATH") or "")) or shutil.which(executable)
    if resolved:
        return resolved

    suffixes = (".exe", ".cmd", ".bat") if os.name == "nt" else ("",)
    for directory in extra_dirs:
        for suffix in suffixes:
            candidate = os.path.join(directory, executable + suffix)
            if os.path.isfile(candidate):
                return candidate
    return executable


_RUST_TOOLCHAIN_DIRS = _rust_toolchain_dirs()
_prepend_path_entries(_ENV, _RUST_TOOLCHAIN_DIRS)
# Resolve Cargo after adding platform-specific toolchain locations.  The
# previous hard-coded Linux path made every Windows deployment enter degraded
# mode even when rustup had installed Cargo normally.
_CARGO_COMMAND = _resolve_executable("cargo", _ENV, _RUST_TOOLCHAIN_DIRS)

# ── Unified log file (same path the dashboard reads) ───────────────────────
_LOG_DIR = os.path.join(_PROJECT_ROOT, "scripts", "logs")
os.makedirs(_LOG_DIR, exist_ok=True)
_LOG_FILE = os.path.join(_LOG_DIR, "live_trader.log")
_LOG_MAX_BYTES = max(256 * 1024, int(str(_ENV.get("BONGUS_LOG_MAX_BYTES", "2097152")) or "2097152"))
_LOG_BACKUP_COUNT = max(1, int(str(_ENV.get("BONGUS_LOG_BACKUP_COUNT", "5")) or "5"))
_WATCHDOG_LOCK_PATH = os.path.join(_PROJECT_ROOT, ".watchdog.lock")
_WATCHDOG_STATE_PATH = os.path.join(_PROJECT_ROOT, ".watchdog_state.json")
_WATCHDOG_LOCK_FD: int | None = None
_log_lock = threading.Lock()


def _ts() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]


def _rotate_log_if_needed(incoming_line: str) -> None:
    try:
        current_size = os.path.getsize(_LOG_FILE)
    except OSError:
        current_size = 0

    incoming_size = len((incoming_line + "\n").encode("utf-8", errors="replace"))
    if current_size <= 0 or current_size + incoming_size <= _LOG_MAX_BYTES:
        return

    oldest_backup = f"{_LOG_FILE}.{_LOG_BACKUP_COUNT}"
    with suppress(OSError):
        if os.path.exists(oldest_backup):
            os.remove(oldest_backup)

    for index in range(_LOG_BACKUP_COUNT - 1, 0, -1):
        source = f"{_LOG_FILE}.{index}"
        dest = f"{_LOG_FILE}.{index + 1}"
        with suppress(OSError):
            if os.path.exists(source):
                os.replace(source, dest)

    with suppress(OSError):
        if os.path.exists(_LOG_FILE):
            os.replace(_LOG_FILE, f"{_LOG_FILE}.1")


def _write(line: str) -> None:
    with _log_lock:
        _rotate_log_if_needed(line)
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
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
RUST_BUILD_COMMAND = [_CARGO_COMMAND, "build", "--release"]
RUST_COMMAND = [_CARGO_COMMAND, "run", "--release"]


def _load_process_manifest() -> dict[str, object]:
    """Load the versioned process-ownership manifest used by supervision.

    Keeping executable ownership in a machine-readable manifest prevents a
    compatibility wrapper or dormant package from silently becoming a second
    production trader.  Invalid manifests stop startup at import time instead
    of falling back to an ambiguous hard-coded executable.
    """

    manifest_path = os.path.join(
        _PROJECT_ROOT,
        "bongus",
        "runtime",
        "process_manifest.json",
    )
    try:
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot load process manifest {manifest_path}: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise RuntimeError("unsupported or malformed process manifest")
    processes = manifest.get("processes")
    canonical_name = manifest.get("canonical_trader")
    if not isinstance(processes, dict) or not isinstance(canonical_name, str):
        raise RuntimeError("process manifest is missing processes/canonical_trader")
    canonical = processes.get(canonical_name)
    if (
        not isinstance(canonical, dict)
        or canonical.get("kind") != "python_module"
        or not str(canonical.get("target") or "").strip()
    ):
        raise RuntimeError("process manifest canonical trader is not a Python module")
    return manifest


PROCESS_MANIFEST = _load_process_manifest()
_PROCESS_SPECS = PROCESS_MANIFEST.get("processes")
if not isinstance(_PROCESS_SPECS, dict):  # Already validated; keeps static typing exact.
    raise RuntimeError("process manifest processes must be an object")
_PROCESS_TARGETS = {
    str(name): str(spec.get("target") or "")
    for name, spec in _PROCESS_SPECS.items()
    if isinstance(spec, dict)
}
CANONICAL_TRADER_MODULE = _PROCESS_TARGETS["trader"]
PYTHON_COMMAND = [sys.executable, "-m", CANONICAL_TRADER_MODULE]
SCRAPER_COMMAND = [sys.executable, _PROCESS_TARGETS["sentiment"]]
_DASHBOARD_HOST = str(_ENV.get("DASHBOARD_HOST", "127.0.0.1")).strip() or "127.0.0.1"
_DASHBOARD_PORT = str(_ENV.get("DASHBOARD_PORT", "8080")).strip() or "8080"
try:
    _DASHBOARD_PORT_INT = int(_DASHBOARD_PORT)
except ValueError:
    _DASHBOARD_PORT_INT = 8080
DASHBOARD_COMMAND = [
    sys.executable, "-m", "uvicorn",
    _PROCESS_TARGETS["dashboard"],
    "--host", _DASHBOARD_HOST, "--port", _DASHBOARD_PORT,
]
SUPERVISOR_COMMAND = [sys.executable, "-m", _PROCESS_TARGETS["supervisor"]]
TELEGRAM_COMMAND = [sys.executable, "bongus/monitoring/telegram_alerter.py"]
TESTNET_DUST_SWEEPER_COMMAND = [sys.executable, _PROCESS_TARGETS["testnet_dust_sweeper"]]
TESTNET_DUST_SWEEPER_ENABLE_ENV = "BONGUS_ENABLE_TESTNET_DUST_SWEEPER"

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
TRADER_HEARTBEAT_FILE = os.path.join(_PROJECT_ROOT, "runtime_heartbeat.json")
TRADER_LIVENESS_STALE_SECONDS = 180
TRADER_LIVENESS_STARTUP_GRACE_SECONDS = 180
TRADER_REQUIRED_LOOP_MAX_AGE_SECONDS = 30.0
TRADER_REQUIRED_PROGRESS_LOOPS: tuple[str, ...] = (
    "liveness_loop",
    "maintenance_loop",
    "execution_event_writer",
    "trading_loop",
)
PROCESS_STOP_TIMEOUT_SECONDS = 5.0
SAFE_MODE_STALE_INTENT_RESTART_SECONDS = 1200
_TRADER_LIVENESS_RISK_KEYS: tuple[str, ...] = (
    "runtime_mode",
    "loop_last_alive_at",
    "preflight_status",
    "heartbeat_status",
    "safe_mode_reason",
    "last_runtime_mode_change",
    "loop_heartbeat_ages",
)

_PROCESS_PORTS: dict[str, tuple[int, ...]] = {
    "rust": (5555, 9000),
    "dashboard": (_DASHBOARD_PORT_INT,),
}

_PYTHON_PROCESS_MATCHERS: dict[str, tuple[str, ...]] = {
    "watchdog": (
        "bongus.monitoring.king_watchdog",
        "bongus/monitoring/king_watchdog.py",
        "bongus\\monitoring\\king_watchdog.py",
    ),
    "trader": (
        CANONICAL_TRADER_MODULE,
        "scripts/live_trader_v2.py",
        "scripts\\live_trader_v2.py",
        "live_trader_v2.py",  # Deprecated root compatibility wrapper.
    ),
    "dashboard": (
        "bongus.monitoring.web_dashboard:app",
        "bongus/monitoring/web_dashboard.py",
        "bongus\\monitoring\\web_dashboard.py",
    ),
    "supervisor": (
        "bongus.monitoring.supervisor_service",
        "bongus/monitoring/supervisor_service.py",
        "bongus\\monitoring\\supervisor_service.py",
    ),
    "telegram": (
        "bongus/monitoring/telegram_alerter.py",
        "bongus\\monitoring\\telegram_alerter.py",
    ),
    "scraper": (
        "bongus/strategies/sentiment_scraper.py",
        "bongus\\strategies\\sentiment_scraper.py",
    ),
    "rebalancer": (
        "bongus/portfolio/auto_rebalance.py",
        "bongus\\portfolio\\auto_rebalance.py",
    ),
    "testnet_dust_sweeper": (
        "bongus/portfolio/auto_rebalance.py",
        "bongus\\portfolio\\auto_rebalance.py",
    ),
}

_WATCHDOG_PROCESS_NAMES: tuple[str, ...] = ("watchdog",)
_CHILD_PROCESS_NAMES: tuple[str, ...] = (
    "trader",
    "dashboard",
    "supervisor",
    "telegram",
    "scraper",
    "rust",
    "rebalancer",  # Legacy process name retained for stale-process cleanup.
    "testnet_dust_sweeper",
)
_RUST_REQUIRED_PROCESS_NAMES: tuple[str, ...] = ("trader", "telegram")

_MAX_WAL_SIZE_MB = 500


class DatabaseMaintenance:
    """Handles periodic database pruning and WAL file size management."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.wal_path = f"{db_path}-wal"
        # Do not launch a full prune immediately on watchdog startup. The
        # trader, telegram alerter, and supervisor all open the state DB during
        # bootstrap, and a same-moment VACUUM/DELETE sweep can lock them out.
        self.last_full_prune_at: float = time.time()
        self.prune_interval_seconds = 86400  # 24 hours

    def run_maintenance_if_needed(self):
        self._check_wal_size()
        self._check_periodic_prune()

    def _check_wal_size(self):
        if not os.path.exists(self.wal_path):
            return
        try:
            size_mb = os.path.getsize(self.wal_path) / (1024 * 1024)
            if size_mb > _MAX_WAL_SIZE_MB:
                _log(f"[WATCHDOG] Database WAL size ({size_mb:.1f} MB) exceeds limit ({_MAX_WAL_SIZE_MB} MB). Triggering checkpoint...")
                self._checkpoint()
        except Exception as e:
            _log(f"[WATCHDOG] Error checking WAL size: {e}")

    def _check_periodic_prune(self):
        now = time.time()
        if (now - self.last_full_prune_at) > self.prune_interval_seconds:
            _log("[WATCHDOG] Running periodic database pruning...")
            self._prune()
            self.last_full_prune_at = now

    def _checkpoint(self):
        try:
            with sqlite3.connect(self.db_path, timeout=30) as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            _log("[WATCHDOG] Database checkpoint successful.")
        except Exception as e:
            _log(f"[WATCHDOG] Error during database checkpoint: {e}")

    def _prune(self):
        try:
            # We use the logic from fast_prune.py
            with sqlite3.connect(self.db_path, timeout=30) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                
                # Pruning queries
                queries = [
                    ("candidate_snapshots", "snapshot_time", "-1 day"),
                    ("feature_snapshots", "snapshot_time", "-1 day"),
                    ("market_samples", "sample_minute", "-2 days"),
                    ("health_samples", "sample_time", "-2 days"),
                ]
                
                for table, col, interval in queries:
                    cursor = conn.execute(f"DELETE FROM {table} WHERE datetime({col}) < datetime('now', ?)", (interval,))
                    if cursor.rowcount > 0:
                        _log(f"[WATCHDOG] Pruned {cursor.rowcount} rows from {table}.")
                
                conn.commit()
                
                # Vacuuming is expensive and locks the DB, but necessary once a day.
                # Since watchdog is the one doing it, it's safer than a cron job.
                _log("[WATCHDOG] Vacuuming database to reclaim space...")
                conn.execute("VACUUM")
                
                # Final checkpoint to shrink WAL after vacuum
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                
            _log("[WATCHDOG] Periodic database pruning complete.")
        except Exception as e:
            _log(f"[WATCHDOG] Error during periodic pruning: {e}")


class _StoppedProcess:
    def __init__(self, returncode: int = 1) -> None:
        self.pid = -1
        self.returncode = returncode

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        return None

    def wait(self, timeout=None) -> int:
        del timeout
        return self.returncode

    def kill(self) -> None:
        return None


def _safe_env(name: str, default: str) -> str:
    raw = _ENV.get(name)
    if raw is None:
        return default
    text = str(raw).strip()
    return text or default


def _env_flag_enabled(name: str, env=None) -> bool:
    """Return True only for an explicit, conventional truthy environment value."""
    source = _ENV if env is None else env
    return str(source.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _log_runtime_config() -> None:
    sentiment_enabled = bool(ConfigManager().get("sentiment_enabled"))
    dotenv_status = "present" if os.path.exists(_DOTENV_PATH) else "missing"
    _log(
        "Runtime config: "
        f".env={dotenv_status} "
        f"TRADING_MODE={_safe_env('TRADING_MODE', 'paper')} "
        f"ACCOUNT_EQUITY_USD={_safe_env('ACCOUNT_EQUITY_USD', '10000')} "
        f"MAX_GROSS_EXPOSURE_USD={_safe_env('MAX_GROSS_EXPOSURE_USD', '10000')} "
        f"MONITORED_SYMBOLS={_safe_env('MONITORED_SYMBOLS', '<default>')} "
        f"DASHBOARD_BIND={_safe_env('DASHBOARD_HOST', '127.0.0.1')}:{_safe_env('DASHBOARD_PORT', '8080')} "
        f"SENTIMENT_ENABLED={sentiment_enabled} "
        f"TESTNET_DUST_SWEEPER_ENABLED="
        f"{_env_flag_enabled(TESTNET_DUST_SWEEPER_ENABLE_ENV)}"
    )


def _locked_watchdog_pid() -> int | None:
    if not os.path.exists(_WATCHDOG_LOCK_PATH):
        return None
    try:
        with open(_WATCHDOG_LOCK_PATH, encoding="utf-8") as handle:
            raw = handle.read().strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _pid_is_watchdog(pid: int) -> bool:
    with suppress(psutil.Error, OSError):
        proc = psutil.Process(pid)
        return _is_python_project_process(proc, "watchdog")
    return False


def _release_watchdog_lock() -> None:
    global _WATCHDOG_LOCK_FD
    fd = _WATCHDOG_LOCK_FD
    _WATCHDOG_LOCK_FD = None
    if fd is not None:
        with suppress(OSError):
            os.close(fd)
    with suppress(OSError):
        os.remove(_WATCHDOG_LOCK_PATH)


def _acquire_watchdog_lock() -> tuple[bool, int | None]:
    global _WATCHDOG_LOCK_FD
    existing_pid = _locked_watchdog_pid()
    if existing_pid is not None and _pid_is_watchdog(existing_pid):
        return False, existing_pid
    if existing_pid is not None:
        with suppress(OSError):
            os.remove(_WATCHDOG_LOCK_PATH)

    while True:
        try:
            fd = os.open(_WATCHDOG_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.write(fd, str(os.getpid()).encode("ascii"))
            _WATCHDOG_LOCK_FD = fd
            return True, None
        except FileExistsError:
            existing_pid = _locked_watchdog_pid()
            if existing_pid is not None and _pid_is_watchdog(existing_pid):
                return False, existing_pid
            with suppress(OSError):
                os.remove(_WATCHDOG_LOCK_PATH)


def _norm_path(value: str | None) -> str:
    if not value:
        return ""
    return os.path.normcase(os.path.abspath(value))


def _is_path_within(path: str | None, root: str) -> bool:
    norm_path = _norm_path(path)
    norm_root = _norm_path(root)
    if not norm_path or not norm_root:
        return False
    try:
        return os.path.commonpath([norm_path, norm_root]) == norm_root
    except ValueError:
        return False


def _proc_info_value(proc: psutil.Process, key: str):
    info = getattr(proc, "info", None)
    if isinstance(info, dict):
        return info.get(key)
    return None


def _proc_cmdline(proc: psutil.Process) -> list[str]:
    cmdline = _proc_info_value(proc, "cmdline")
    if isinstance(cmdline, (list, tuple)):
        return [str(part) for part in cmdline if part]
    with suppress(psutil.Error, OSError):
        return [str(part) for part in proc.cmdline() if part]
    return []


def _proc_name(proc: psutil.Process) -> str:
    name = _proc_info_value(proc, "name")
    if name:
        return str(name)
    with suppress(psutil.Error, OSError):
        return str(proc.name())
    return "unknown"


def _proc_cwd(proc: psutil.Process) -> str:
    cwd = _proc_info_value(proc, "cwd")
    if cwd:
        return str(cwd)
    with suppress(psutil.Error, OSError):
        return str(proc.cwd())
    return ""


def _describe_process(proc: psutil.Process) -> str:
    name = _proc_name(proc)
    cmdline = " ".join(_proc_cmdline(proc)).strip()
    if cmdline:
        return f"{name}[pid={proc.pid}] {cmdline}"
    return f"{name}[pid={proc.pid}]"


def _is_python_project_process(proc: psutil.Process, name: str) -> bool:
    if name not in _PYTHON_PROCESS_MATCHERS:
        return False
    proc_name = _proc_name(proc).lower()
    if not proc_name.startswith("python") and proc_name != "py.exe":
        return False
    cwd = _proc_cwd(proc)
    if not _is_path_within(cwd, _PROJECT_ROOT):
        return False
    cmdline_text = " ".join(_proc_cmdline(proc)).lower()
    return any(token in cmdline_text for token in _PYTHON_PROCESS_MATCHERS[name])


def _is_rust_project_process(proc: psutil.Process) -> bool:
    proc_name = _proc_name(proc).lower()
    cwd = _proc_cwd(proc)
    if proc_name in ("execution_engine.exe", "execution_engine"):
        return _is_path_within(cwd, RUST_ENGINE_DIR)
    if proc_name in ("cargo.exe", "cargo"):
        if not _is_path_within(cwd, RUST_ENGINE_DIR):
            return False
        cmdline_text = " ".join(_proc_cmdline(proc)).lower()
        return "run" in cmdline_text and "--release" in cmdline_text
    return False


def _is_managed_project_process(proc: psutil.Process, name: str) -> bool:
    if name == "rust":
        return _is_rust_project_process(proc)
    return _is_python_project_process(proc, name)


def _find_managed_project_processes(*names: str) -> list[psutil.Process]:
    current_pid = os.getpid()
    managed: list[psutil.Process] = []
    target_names = names or (_WATCHDOG_PROCESS_NAMES + _CHILD_PROCESS_NAMES)
    for proc in psutil.process_iter(attrs=["pid", "name", "cmdline", "cwd"]):
        if proc.pid == current_pid:
            continue
        if any(
            _is_managed_project_process(proc, name)
            for name in target_names
        ):
            managed.append(proc)
    return managed


def _terminate_processes(procs: list[psutil.Process], *, reason: str) -> None:
    if not procs:
        return
    summary = ", ".join(_describe_process(proc) for proc in procs[:5])
    if len(procs) > 5:
        summary += f", ... (+{len(procs) - 5} more)"
    _log(f"[WATCHDOG] {reason}: {summary}")
    for proc in procs:
        with suppress(psutil.Error, OSError):
            proc.terminate()
    _, alive = psutil.wait_procs(procs, timeout=PROCESS_STOP_TIMEOUT_SECONDS)
    if alive:
        summary = ", ".join(_describe_process(proc) for proc in alive[:5])
        if len(alive) > 5:
            summary += f", ... (+{len(alive) - 5} more)"
        _log(f"[WATCHDOG] Escalating to kill for stubborn processes: {summary}")
        for proc in alive:
            with suppress(psutil.Error, OSError):
                proc.kill()
        psutil.wait_procs(alive, timeout=PROCESS_STOP_TIMEOUT_SECONDS)


def _cleanup_stale_project_processes() -> None:
    stale = _find_managed_project_processes(*_CHILD_PROCESS_NAMES)
    if not stale:
        return
    _terminate_processes(
        stale,
        reason="Found stale Bongus-owned processes from a previous run; terminating before startup",
    )


def _other_watchdogs_running() -> list[psutil.Process]:
    return _find_managed_project_processes(*_WATCHDOG_PROCESS_NAMES)


atexit.register(_release_watchdog_lock)


def _find_listening_process(port: int) -> psutil.Process | None:
    for conn in psutil.net_connections(kind="tcp"):
        local_addr = getattr(conn, "laddr", ())
        if not local_addr:
            continue
        if getattr(local_addr, "port", None) != port:
            continue
        if conn.status != psutil.CONN_LISTEN:
            continue
        if conn.pid is None:
            return None
        with suppress(psutil.Error, OSError):
            return psutil.Process(conn.pid)
        return None
    return None


def _start_block_reason(name: str, ignore_pids: set[int] | None = None) -> str | None:
    ignore = ignore_pids or set()
    conflicts: list[str] = []
    for port in _PROCESS_PORTS.get(name, ()):
        owner = _find_listening_process(port)
        if owner is None or owner.pid in ignore:
            continue
        conflicts.append(f"port {port} is already in use by {_describe_process(owner)}")
    if conflicts:
        return "; ".join(conflicts)
    return None


class CrashTracker:
    """Per-process durable crash budget with a bounded circuit-breaker probe.

    A host/watchdog restart must not erase a crash storm.  Conversely, a quick
    exit must not leave a service silently dead forever: after the maximum
    cooldown one supervised probe is allowed and the budget remains durable.
    """

    def __init__(self, name: str = "", state_path: str | None = None):
        self.name = str(name)
        self.state_path = state_path
        self.crash_times: list[float] = []
        self.backoff_until: float = 0.0
        self.current_backoff: float = 0.0
        self.permanently_failed: bool = False
        self._load()

    def _load(self) -> None:
        if not self.state_path or not self.name:
            return
        try:
            with open(self.state_path, encoding="utf-8") as handle:
                root = json.load(handle)
            payload = dict(root.get("processes", {})).get(self.name, {})
            if not isinstance(payload, dict):
                return
            self.crash_times = [float(item) for item in payload.get("crash_times", [])]
            self.backoff_until = float(payload.get("backoff_until", 0.0))
            self.current_backoff = float(payload.get("current_backoff", 0.0))
            self.permanently_failed = bool(payload.get("circuit_open", False))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return

    def _persist(self) -> None:
        if not self.state_path or not self.name:
            return
        root: dict[str, object] = {"schema_version": 1, "processes": {}}
        try:
            with open(self.state_path, encoding="utf-8") as handle:
                existing = json.load(handle)
            if isinstance(existing, dict) and int(existing.get("schema_version", 0)) == 1:
                root = existing
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        processes = root.setdefault("processes", {})
        if not isinstance(processes, dict):
            processes = {}
            root["processes"] = processes
        processes[self.name] = {
            "crash_times": self.crash_times,
            "backoff_until": self.backoff_until,
            "current_backoff": self.current_backoff,
            "circuit_open": self.permanently_failed,
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        temp_path = f"{self.state_path}.tmp.{os.getpid()}"
        try:
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump(root, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.state_path)
        finally:
            with suppress(OSError):
                if os.path.exists(temp_path):
                    os.remove(temp_path)

    def trip_circuit(self, cooldown_seconds: float = MAX_BACKOFF_SECONDS) -> None:
        self.permanently_failed = True
        self.current_backoff = max(self.current_backoff, float(cooldown_seconds))
        self.backoff_until = max(self.backoff_until, time.time() + float(cooldown_seconds))
        self._persist()

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
            self.current_backoff = MAX_BACKOFF_SECONDS
            self.backoff_until = now + MAX_BACKOFF_SECONDS
            self._persist()
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
        self._persist()

    def should_restart(self) -> bool:
        now = time.time()
        if self.permanently_failed:
            if now < self.backoff_until:
                return False
            # Allow one bounded probe after the durable circuit cooldown.  A
            # further crash immediately consumes the same persisted budget.
            self.permanently_failed = False
            self.backoff_until = 0.0
            self._persist()
            return True
        return now >= self.backoff_until

    def reset(self) -> None:
        self.crash_times.clear()
        self.current_backoff = 0.0
        self.backoff_until = 0.0
        self.permanently_failed = False
        self._persist()


def _stalled_trader_loops(loop_heartbeat_ages: dict[str, float] | None) -> list[str]:
    """Return required service loops whose own progress is stale.

    `on_order_update` is intentionally excluded because a quiet account may
    receive no order events.  The other loops are continuous and must progress
    independently even when the aggregate liveness writer remains healthy.
    """

    if not loop_heartbeat_ages:
        return list(TRADER_REQUIRED_PROGRESS_LOOPS)
    return sorted(
        name
        for name in TRADER_REQUIRED_PROGRESS_LOOPS
        if name not in loop_heartbeat_ages
        or float(loop_heartbeat_ages[name]) > TRADER_REQUIRED_LOOP_MAX_AGE_SECONDS
    )


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


def _read_trader_liveness() -> tuple[str | None, datetime.datetime | None, str | None, datetime.datetime | None, dict[str, float] | None]:
    file_runtime_mode = None
    file_last_alive = None
    file_safe_mode_reason = None
    file_mode_changed_at = None
    file_loop_heartbeat_ages = None
    if os.path.exists(TRADER_HEARTBEAT_FILE):
        try:
            with open(TRADER_HEARTBEAT_FILE, encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict):
                runtime_mode_value = payload.get("runtime_mode")
                if runtime_mode_value is not None:
                    file_runtime_mode = str(runtime_mode_value)
                safe_mode_value = payload.get("safe_mode_reason")
                if safe_mode_value is not None:
                    file_safe_mode_reason = str(safe_mode_value)
                file_last_alive = _parse_iso_timestamp(str(payload.get("loop_last_alive_at") or ""))
                file_mode_changed_at = _parse_iso_timestamp(str(payload.get("last_runtime_mode_change") or ""))
                loop_ages = payload.get("loop_heartbeat_ages")
                if isinstance(loop_ages, dict):
                    file_loop_heartbeat_ages = {
                        str(key): float(value)
                        for key, value in loop_ages.items()
                    }
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass

    if not os.path.exists(TRADER_STATE_DB):
        return (
            file_runtime_mode,
            file_last_alive,
            file_safe_mode_reason,
            file_mode_changed_at,
            file_loop_heartbeat_ages,
        )
    placeholders = ", ".join("?" for _ in _TRADER_LIVENESS_RISK_KEYS)
    try:
        with sqlite3.connect(TRADER_STATE_DB, timeout=2) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT key, value, updated_at FROM risk_state WHERE key IN ({placeholders})",
                _TRADER_LIVENESS_RISK_KEYS,
            ).fetchall()
    except sqlite3.Error:
        return (
            file_runtime_mode,
            file_last_alive,
            file_safe_mode_reason,
            file_mode_changed_at,
            file_loop_heartbeat_ages,
        )

    runtime_mode = None
    safe_mode_reason = None
    mode_changed_at = None
    loop_heartbeat_ages = None
    progress_times: list[datetime.datetime] = []
    for row in rows:
        key = str(row["key"])
        raw_value = row["value"]
        try:
            parsed_value = json.loads(raw_value)
        except Exception:
            parsed_value = raw_value
        if key == "runtime_mode":
            runtime_mode = str(parsed_value)
        elif key == "safe_mode_reason":
            safe_mode_reason = str(parsed_value)
        elif key == "last_runtime_mode_change":
            mode_changed_at = _parse_iso_timestamp(str(parsed_value))
        elif key == "loop_heartbeat_ages":
            if isinstance(parsed_value, dict):
                loop_heartbeat_ages = {str(k): float(v) for k, v in parsed_value.items()}
        updated_at = _parse_iso_timestamp(str(row["updated_at"]))
        if updated_at is not None:
            progress_times.append(updated_at)
        
        if key == "loop_last_alive_at":
            loop_last_alive_at = _parse_iso_timestamp(str(parsed_value))
            if loop_last_alive_at is not None:
                progress_times.append(loop_last_alive_at)
    db_last_alive = max(progress_times, default=None)
    candidates = [dt for dt in (db_last_alive, file_last_alive) if dt is not None]
    merged_last_alive = max(candidates, default=None)
    mode_change_candidates = [dt for dt in (mode_changed_at, file_mode_changed_at) if dt is not None]
    merged_mode_changed_at = max(mode_change_candidates, default=None)
    return (
        file_runtime_mode or runtime_mode,
        merged_last_alive,
        file_safe_mode_reason if file_safe_mode_reason is not None else safe_mode_reason,
        merged_mode_changed_at,
        file_loop_heartbeat_ages if file_loop_heartbeat_ages is not None else loop_heartbeat_ages,
    )


def _read_trader_block_state() -> tuple[str | None, str | None]:
    if not os.path.exists(TRADER_STATE_DB):
        return None, None
    try:
        with sqlite3.connect(TRADER_STATE_DB, timeout=2) as conn:
            rows = conn.execute(
                "SELECT key, value FROM risk_state WHERE key IN (?, ?)",
                ("blocked_reason", "preflight_status"),
            ).fetchall()
    except sqlite3.Error:
        return None, None

    blocked_reason = None
    preflight_status = None
    for key, raw_value in rows:
        try:
            parsed_value = json.loads(raw_value)
        except Exception:
            parsed_value = raw_value
        if key == "blocked_reason":
            blocked_reason = str(parsed_value or "").strip() or None
        elif key == "preflight_status":
            preflight_status = str(parsed_value or "").strip() or None
    return blocked_reason, preflight_status


def _rust_ipc_ready(host: str = "127.0.0.1", port: int = 9000, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


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


def _build_process_defs(
    *,
    rust_build_ok: bool,
    sentiment_enabled: bool,
    testnet_dust_sweeper_enabled: bool = False,
):
    process_defs = [
        ("trader", PYTHON_COMMAND, _PROJECT_ROOT),
        ("dashboard", DASHBOARD_COMMAND, _PROJECT_ROOT),
        ("supervisor", SUPERVISOR_COMMAND, _PROJECT_ROOT),
        ("telegram", TELEGRAM_COMMAND, _PROJECT_ROOT),
    ]
    # The legacy flag is retained only so stale deployments can be diagnosed.
    # It never authorizes the retired account-wide liquidation utility.
    _ = testnet_dust_sweeper_enabled
    if sentiment_enabled:
        process_defs.insert(1, ("scraper", SCRAPER_COMMAND, _PROJECT_ROOT))
    if rust_build_ok:
        process_defs.insert(0, ("rust", RUST_COMMAND, RUST_ENGINE_DIR))
        return process_defs, ()

    process_defs = [
        process_def
        for process_def in process_defs
        if process_def[0] not in _RUST_REQUIRED_PROCESS_NAMES
    ]
    return process_defs, _RUST_REQUIRED_PROCESS_NAMES


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


def check_and_restart(
    proc,
    command,
    name: str,
    cwd,
    tracker: CrashTracker,
    started_at: float | None = None,
    all_procs: dict[str, subprocess.Popen | _StoppedProcess] | None = None,
):
    if proc.poll() is not None:
        exit_code = proc.returncode

        if tracker.permanently_failed and not tracker.should_restart():
            return proc

        if name == "trader" and exit_code == TRADER_BLOCKED_EXIT_CODE:
            blocked_reason, preflight_status = _read_trader_block_state()
            bridge_preflight_blocked = (
                preflight_status == "blocked_execution_bridge"
                or blocked_reason == "execution bridge preflight failed"
            )
            rust_proc = all_procs.get("rust") if all_procs else None
            if bridge_preflight_blocked and rust_proc is not None and rust_proc.poll() is None:
                _log(
                    "[WATCHDOG] Trader exited in BLOCKED mode because the Rust execution bridge "
                    "was still coming up. Waiting for Rust IPC and retrying trader startup."
                )
                _wait_for_rust_ipc(timeout=30)
                if _rust_ipc_ready():
                    return start_process(command, name=name, cwd=cwd)
            
            if AUTONOMOUS_STARTUP_RECOVERY:
                _log("[WATCHDOG] Trader exited in BLOCKED mode (exit=78). Autonomous recovery enabled, retrying in 30s.")
                tracker.record_crash()
                tracker.backoff_until = time.time() + 30.0
                return proc
                
            tracker.trip_circuit()
            _log(
                "[WATCHDOG] Trader exited in BLOCKED mode (exit=78). "
                "Leaving it stopped until an operator intervenes."
            )
            return proc

        tracker.record_crash()

        if tracker.permanently_failed and not tracker.should_restart():
            _log(
                f"[WATCHDOG] FATAL: {name} crashed {QUICK_EXIT_MAX_CRASHES} times within {QUICK_EXIT_WINDOW_SECONDS}s. "
                "Marking as permanently failed and stopping retries."
            )
            return proc

        if not tracker.should_restart():
            delay = tracker.backoff_until - time.time()
            _log(
                f"[WATCHDOG] {name} crashed (exit={exit_code}), "
                f"backoff active — next restart in {delay:.0f}s "
                f"({len(tracker.crash_times)} crashes in window)"
            )
            return proc

        block_reason = _start_block_reason(name, ignore_pids={proc.pid})
        if block_reason is not None:
            tracker.trip_circuit()
            _log(f"[WATCHDOG] FATAL: cannot restart {name}: {block_reason}")
            return proc

        _log(
            f"[WATCHDOG] {name} crashed (exit={exit_code}), restarting. "
            f"({len(tracker.crash_times)} crashes in window)"
        )
        return start_process(command, name=name, cwd=cwd)

    # ── Memory Monitoring ────────────────────────────────────────────────
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

    # ── Liveness Checks ──────────────────────────────────────────────────
    if name == "trader" and proc.poll() is None:
        if started_at is not None and (time.time() - started_at) < TRADER_LIVENESS_STARTUP_GRACE_SECONDS:
            return proc
        runtime_mode, last_alive, safe_mode_reason, mode_changed_at, loop_heartbeat_ages = _read_trader_liveness()

        # 1. Heartbeat check
        if runtime_mode != "BLOCKED" and last_alive is not None:
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            age = (now_utc - last_alive).total_seconds()
            if age > TRADER_LIVENESS_STALE_SECONDS:
                _log(
                    f"[WATCHDOG] trader loop liveness stale ({age:.1f}s). Restarting trader while preserving Rust IPC."
                )

                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                return start_process(command, name=name, cwd=cwd)

        # 2. Per-service progress check.  A healthy aggregate heartbeat is not
        # readiness if the decision, writer or reconciler/maintenance task is
        # wedged independently.
        stalled_loops = _stalled_trader_loops(loop_heartbeat_ages)
        if runtime_mode != "BLOCKED" and stalled_loops:
            _log(
                "[WATCHDOG] trader service loop progress stale: "
                + ", ".join(stalled_loops)
                + ". Restarting trader while preserving Rust IPC."
            )
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            tracker.record_crash()
            return start_process(command, name=name, cwd=cwd)

        # 3. Stale intent check
        if (
            runtime_mode in {"SAFE_MODE", "LIVE_WITH_SYMBOL_BLOCKS"}
            and safe_mode_reason is not None
            and "stale_pending_intent" in safe_mode_reason
            and mode_changed_at is not None
        ):
            elapsed = (datetime.datetime.now(datetime.timezone.utc) - mode_changed_at).total_seconds()
            if elapsed > SAFE_MODE_STALE_INTENT_RESTART_SECONDS:
                _log(
                    f"[WATCHDOG] trader stuck in {runtime_mode}/stale_pending_intent for {elapsed:.0f}s. "
                    "Restarting trader + rust to clear stuck in-memory chases."
                )

                if all_procs and "rust" in all_procs:
                    rproc = all_procs["rust"]
                    if rproc.poll() is None:
                        _log("[WATCHDOG] Killing rust engine to clear its in-memory chase states.")
                        rproc.terminate()
                        with suppress(subprocess.TimeoutExpired):
                            rproc.wait(timeout=2)
                        rproc.kill()

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
    _log(f"Running preflight build check for Rust engine via {_CARGO_COMMAND}...")
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
        search_roots = ", ".join(_RUST_TOOLCHAIN_DIRS) or "<none>"
        _log(
            "[WATCHDOG] FATAL: `cargo` command not found. "
            f"Checked PATH plus Rust toolchain dirs: {search_roots}"
        )
        return False
    except Exception as e:
        _log(f"[WATCHDOG] FATAL: Unexpected error during preflight check: {e}")
        return False


def main():
    acquired_lock, owner_pid = _acquire_watchdog_lock()
    if not acquired_lock:
        owner_summary = f"pid={owner_pid}" if owner_pid is not None else "unknown owner"
        if owner_pid is not None:
            with suppress(psutil.Error, OSError):
                owner_summary = _describe_process(psutil.Process(owner_pid))
        print(
            "[WATCHDOG] FATAL: another King Watchdog instance is already "
            f"running: {owner_summary}",
            flush=True,
        )
        return
    _cleanup_stale_project_processes()
    archive_result = archive_startup_artifacts(
        Path(_PROJECT_ROOT),
        retention_count=startup_archive_retention_from_env(),
    )
    _log("Starting King Watchdog Supervisor...")
    if archive_result.archive_dir is not None:
        archive_relative = archive_result.archive_dir.relative_to(
            Path(_PROJECT_ROOT)
        )
        _log(
            "[WATCHDOG] Archived previous session diagnostics to "
            f"{archive_relative} "
            f"(moved={len(archive_result.moved)}, "
            f"snapshotted={len(archive_result.copied)})."
        )
    for archive_error in archive_result.errors:
        _log(f"[WATCHDOG] Log archive warning: {archive_error}")
    _log_runtime_config()
    sentiment_enabled = bool(ConfigManager().get("sentiment_enabled"))
    testnet_dust_sweeper_enabled = _env_flag_enabled(TESTNET_DUST_SWEEPER_ENABLE_ENV)

    # Preflight check for Rust engine
    rust_build_ok = run_preflight_checks()

    if not sentiment_enabled:
        _log("Sentiment scraper disabled by config.")
    if testnet_dust_sweeper_enabled:
        _log(
            "[WATCHDOG] Ignoring legacy "
            f"{TESTNET_DUST_SWEEPER_ENABLE_ENV}: the account-wide dust sweeper is retired."
        )
    else:
        _log("Spot Testnet dust sweeper is retired and cannot be supervised.")
    process_defs, skipped_process_names = _build_process_defs(
        rust_build_ok=rust_build_ok,
        sentiment_enabled=sentiment_enabled,
        testnet_dust_sweeper_enabled=testnet_dust_sweeper_enabled,
    )
    if not rust_build_ok:
        _log("[WATCHDOG] Running in degraded mode without Rust engine.")
        if skipped_process_names:
            _log(
                "[WATCHDOG] Skipping "
                + ", ".join(skipped_process_names)
                + " because they require the Rust execution bridge."
            )

    trackers: dict[str, CrashTracker] = {
        name: CrashTracker(name=name, state_path=_WATCHDOG_STATE_PATH)
        for name, _, _ in process_defs
    }
    start_times: dict[str, float] = {}
    procs: dict[str, subprocess.Popen | _StoppedProcess] = {}
    db_maint = DatabaseMaintenance(TRADER_STATE_DB)

    for name, cmd, cwd in process_defs:
        block_reason = _start_block_reason(name)
        if block_reason is not None:
            trackers[name].trip_circuit()
            _log(f"[WATCHDOG] FATAL: cannot restart {name}: {block_reason}")
            procs[name] = _StoppedProcess()
            start_times[name] = time.time()
            continue
        procs[name] = start_process(cmd, name=name, cwd=cwd)
        start_times[name] = time.time()
        if name == "rust":
            _wait_for_rust_ipc(timeout=30)

    try:
        while True:
            db_maint.run_maintenance_if_needed()
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
                    all_procs=procs,
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
