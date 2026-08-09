import atexit
import datetime
import hashlib
import json
import math
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil
from dotenv import load_dotenv

if __package__ in {None, ""}:
    _BOOTSTRAP_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if _BOOTSTRAP_ROOT not in sys.path:
        sys.path.insert(0, _BOOTSTRAP_ROOT)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DOTENV_PATH = os.path.join(_PROJECT_ROOT, ".env")
load_dotenv(_DOTENV_PATH)

from bongus.core.config import (
    AUDIT_DB_PATH,
    DEFAULT_MONITORED_SYMBOLS,
    RESEARCH_DB_PATH,
    STATE_DB_PATH,
)
from bongus.core.config_manager import ConfigManager
from bongus.core.live_approval import LiveApprovalError, verify_live_approval
from bongus.monitoring.log_artifacts import (
    archive_startup_artifacts,
    startup_archive_max_bytes_from_env,
    startup_archive_retention_from_env,
    startup_archive_retention_days_from_env,
)
from bongus.monitoring.storage_observability import read_storage_snapshot
from scripts.release_manifest import (
    ReleaseManifestError,
    inspect_executable,
    verify_runtime_inventory,
)

_ENV = {
    **os.environ,
    "PYTHONPATH": _PROJECT_ROOT,
    "PYTHONUNBUFFERED": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
}
if not str(_ENV.get("MONITORED_SYMBOLS", "")).strip():
    _ENV["MONITORED_SYMBOLS"] = ",".join(DEFAULT_MONITORED_SYMBOLS)


def _resolve_runtime_database_paths(
    environment: dict[str, str],
    *,
    project_root: Path,
    configured_state_path: str,
    configured_audit_path: str,
    configured_research_path: str,
) -> tuple[Path, Path, Path, Path]:
    """Resolve the shared data root and three distinct role databases."""

    raw_data_root = str(environment.get("BONGUS_DATA_ROOT", "") or "").strip()
    data_root = Path(raw_data_root) if raw_data_root else Path(configured_state_path).parent
    if raw_data_root and not data_root.is_absolute():
        raise RuntimeError("BONGUS_DATA_ROOT must be an absolute path")
    if not data_root.is_absolute():
        data_root = project_root / data_root
    data_root = data_root.resolve(strict=False)

    def role_path(environment_name: str, configured_path: str, filename: str) -> Path:
        expected = (data_root / filename).resolve(strict=False)
        raw_override = str(environment.get(environment_name, "") or "").strip()
        if raw_override:
            candidate = Path(raw_override)
            if not candidate.is_absolute():
                raise RuntimeError(f"{environment_name} must be an absolute path")
            candidate = candidate.resolve(strict=False)
            if candidate != expected:
                raise RuntimeError(
                    f"{environment_name} must be exactly {expected}; split storage "
                    "is one manifest-bound data root"
                )
            return candidate
        elif raw_data_root or Path(configured_path).resolve(strict=False).parent != data_root:
            return expected
        else:
            candidate = Path(configured_path)
        if not candidate.is_absolute():
            candidate = data_root / candidate
        return candidate.resolve(strict=False)

    state_path = role_path("BONGUS_STATE_DB_PATH", configured_state_path, "state.db")
    audit_path = role_path("BONGUS_AUDIT_DB_PATH", configured_audit_path, "audit.db")
    research_path = role_path(
        "BONGUS_RESEARCH_DB_PATH", configured_research_path, "research.db"
    )
    if len({state_path, audit_path, research_path}) != 3:
        raise RuntimeError("state, audit, and research database paths must be distinct")
    return data_root, state_path, audit_path, research_path


def _resolve_runtime_artifact_path(
    environment: dict[str, str],
    environment_name: str,
    *,
    data_root: Path,
    default_relative_path: Path,
) -> Path:
    """Resolve one mutable artifact without permitting release/data-root escape."""

    raw = str(environment.get(environment_name, "") or "").strip()
    candidate = Path(raw) if raw else data_root / default_relative_path
    if raw and not candidate.is_absolute():
        raise RuntimeError(f"{environment_name} must be an absolute path")
    candidate = candidate.resolve(strict=False)
    try:
        candidate.relative_to(data_root.resolve(strict=False))
    except ValueError as exc:
        raise RuntimeError(f"{environment_name} must remain under {data_root}") from exc
    return candidate


(
    BONGUS_DATA_ROOT,
    STATE_DATABASE_PATH,
    AUDIT_DATABASE_PATH,
    RESEARCH_DATABASE_PATH,
) = _resolve_runtime_database_paths(
    _ENV,
    project_root=Path(_PROJECT_ROOT),
    configured_state_path=STATE_DB_PATH,
    configured_audit_path=AUDIT_DB_PATH,
    configured_research_path=RESEARCH_DB_PATH,
)
# Every supervised child receives the same explicit storage contract, even
# when the operator supplied only BONGUS_DATA_ROOT.
_ENV["BONGUS_DATA_ROOT"] = str(BONGUS_DATA_ROOT)
_ENV["BONGUS_STATE_DB_PATH"] = str(STATE_DATABASE_PATH)
_ENV["BONGUS_AUDIT_DB_PATH"] = str(AUDIT_DATABASE_PATH)
_ENV["BONGUS_RESEARCH_DB_PATH"] = str(RESEARCH_DATABASE_PATH)

_RUNTIME_ROOT = Path(BONGUS_DATA_ROOT).resolve(strict=False)
_ENV["BONGUS_RUNTIME_DIR"] = str(_RUNTIME_ROOT / "runtime")
_ENV["BONGUS_STORAGE_RESERVE_PATH"] = str(
    _RUNTIME_ROOT / "runtime" / "emergency-storage.reserve"
)

# ── Unified log file (same path the dashboard reads) ───────────────────────
_LOG_DIR = str(_RUNTIME_ROOT / "scripts" / "logs")
os.makedirs(_LOG_DIR, exist_ok=True)
_LOG_FILE = os.path.join(_LOG_DIR, "live_trader.log")
_LOG_MAX_BYTES = max(256 * 1024, int(str(_ENV.get("BONGUS_LOG_MAX_BYTES", "2097152")) or "2097152"))
_LOG_BACKUP_COUNT = max(1, int(str(_ENV.get("BONGUS_LOG_BACKUP_COUNT", "5")) or "5"))
_WATCHDOG_LOCK_PATH = str(_RUNTIME_ROOT / ".watchdog.lock")
_WATCHDOG_STATE_PATH = str(_RUNTIME_ROOT / ".watchdog_state.json")
_ENV["BONGUS_LOG_PATH"] = _LOG_FILE
_ENV["BONGUS_RUNTIME_HEARTBEAT_PATH"] = str(
    _RUNTIME_ROOT / "runtime_heartbeat.json"
)
_ENV["BONGUS_SENTIMENT_PATH"] = str(_RUNTIME_ROOT / "current_sentiment.json")
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


# Resolved from the process manifest below.  Keeping the working directory
# beside the packaged executable makes the same watchdog code valid in both
# the source tree and the minimal release, which intentionally contains no
# Cargo crate or ``execution_engine/`` source directory.
RUST_ENGINE_DIR = _PROJECT_ROOT
# Retained as an empty compatibility constant for diagnostics/tests.  Runtime
# startup must never invoke a compiler or package manager.
RUST_BUILD_COMMAND: list[str] = []
RUST_COMMAND: list[str] = []


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
    rust = processes.get("rust")
    if (
        not isinstance(rust, dict)
        or rust.get("kind") != "binary"
        or not str(rust.get("target") or "").strip()
    ):
        raise RuntimeError("process manifest Rust engine must be a prebuilt binary")
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
_rust_binary_relative = Path(_PROCESS_TARGETS["rust"])
if _rust_binary_relative.is_absolute() or ".." in _rust_binary_relative.parts:
    raise RuntimeError("Rust binary target must be a contained project-relative path")
_rust_binary_path = Path(_PROJECT_ROOT, _rust_binary_relative)
if os.name == "nt" and _rust_binary_path.suffix.lower() != ".exe":
    _rust_binary_path = _rust_binary_path.with_suffix(".exe")
RUST_COMMAND = [str(_rust_binary_path.resolve())]
RUST_ENGINE_DIR = str(_rust_binary_path.resolve().parent)
PYTHON_COMMAND = [sys.executable, "-m", CANONICAL_TRADER_MODULE]
SCRAPER_COMMAND = [sys.executable, _PROCESS_TARGETS["scraper"]]
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
TELEGRAM_COMMAND = [sys.executable, _PROCESS_TARGETS["telegram"]]
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
TRADER_STATE_DB = str(STATE_DATABASE_PATH)
TRADER_AUDIT_DB = str(AUDIT_DATABASE_PATH)
TRADER_RESEARCH_DB = str(RESEARCH_DATABASE_PATH)
TRADER_HEARTBEAT_FILE = _ENV["BONGUS_RUNTIME_HEARTBEAT_PATH"]
TRADER_LIVENESS_STALE_SECONDS = 180
TRADER_LIVENESS_STARTUP_GRACE_SECONDS = 180
TRADER_REQUIRED_LOOP_MAX_AGE_SECONDS = 30.0
# Storage monitoring and hourly retention are intentionally heavier than the
# other service loops.  They run serialized SQLite/filesystem work (whose
# read-only fallback has a 30 second busy timeout) off the asyncio thread.
# Give those two dedicated loops the aggregate liveness allowance while
# retaining the tighter deadline for maintenance/order/event/decision progress.
TRADER_STORAGE_MONITOR_MAX_AGE_SECONDS = float(TRADER_LIVENESS_STALE_SECONDS)
TRADER_RETENTION_LOOP_MAX_AGE_SECONDS = float(TRADER_LIVENESS_STALE_SECONDS)
STORAGE_HEALTH_FILE = str(
    _resolve_runtime_artifact_path(
        _ENV,
        "BONGUS_STORAGE_HEALTH_SNAPSHOT_PATH",
        data_root=_RUNTIME_ROOT,
        default_relative_path=Path("runtime", "storage_health.json"),
    )
)
_ENV["BONGUS_STORAGE_HEALTH_SNAPSHOT_PATH"] = STORAGE_HEALTH_FILE


def _normalize_storage_health_max_age(raw_value: object) -> float:
    """Bound an explicit override, defaulting to the storage-loop deadline."""

    raw_text = str(raw_value or "").strip()
    try:
        value = (
            float(raw_text)
            if raw_text
            else TRADER_STORAGE_MONITOR_MAX_AGE_SECONDS
        )
    except (TypeError, ValueError):
        value = TRADER_STORAGE_MONITOR_MAX_AGE_SECONDS
    if value != value or value in {float("inf"), float("-inf")}:
        value = TRADER_STORAGE_MONITOR_MAX_AGE_SECONDS
    return min(300.0, max(30.0, value))


STORAGE_HEALTH_MAX_AGE_SECONDS = _normalize_storage_health_max_age(
    _ENV.get("BONGUS_STORAGE_HEALTH_MAX_AGE_SECONDS")
)
# Every supervised child, including the dashboard, must apply the same
# normalized freshness window as the watchdog that starts and stops it.
_ENV["BONGUS_STORAGE_HEALTH_MAX_AGE_SECONDS"] = (
    f"{STORAGE_HEALTH_MAX_AGE_SECONDS:g}"
)
RUST_RUNTIME_DIR = (_RUNTIME_ROOT / "runtime" / "rust").resolve(strict=False)
_ENV["BONGUS_RUST_RUNTIME_DIR"] = str(RUST_RUNTIME_DIR)
_ENV["EXECUTION_STATE_JOURNAL_PATH"] = str(
    RUST_RUNTIME_DIR / "execution_state.jsonl"
)
_ENV["EXECUTION_INTENT_JOURNAL_PATH"] = str(
    RUST_RUNTIME_DIR / "execution_intents.jsonl"
)
_ENV["EXECUTION_TELEMETRY_JOURNAL_PATH"] = str(
    RUST_RUNTIME_DIR / "execution_telemetry.jsonl"
)
_ENV["EXECUTION_TELEMETRY_CURSOR_PATH"] = str(
    RUST_RUNTIME_DIR / "execution_telemetry.cursor"
)
_ENV["EXECUTION_STORAGE_CONTROL_PATH"] = str(
    RUST_RUNTIME_DIR / "storage_control.json"
)
_ENV["PRIVATE_STREAM_CURSOR_DIR"] = str(
    RUST_RUNTIME_DIR / "private_stream_cursors"
)
_ENV.setdefault("EXECUTION_STATE_ENTRY_MAX_BYTES", "30000000")
_ENV.setdefault("EXECUTION_INTENT_JOURNAL_MAX_BYTES", "80000000")
_ENV.setdefault("EXECUTION_TELEMETRY_JOURNAL_MAX_BYTES", "30000000")
_ENV.setdefault("EXECUTION_TELEMETRY_PRIMARY_CONSUMER_ID", "python-live-trader")
TRADER_REQUIRED_PROGRESS_LOOPS: tuple[str, ...] = (
    "liveness_loop",
    "maintenance_loop",
    "retention_loop",
    "execution_event_writer",
    "storage_monitor",
    "trading_loop",
)
PROCESS_STOP_TIMEOUT_SECONDS = 5.0
TRADER_GRACEFUL_STOP_TIMEOUT_SECONDS = 60.0
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
    # Backups are intentionally not supervised or restarted by the watchdog,
    # but a manually scheduled project-owned backup must still be stopped when
    # the storage guard enters its fail-safe states.
    "backup_job": (
        "backup_db.py",
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
# The trader cannot establish its execution/reconciliation barrier without
# Rust. The Telegram alerter is deliberately independent so a missing engine
# never removes the minimal operator-alert path.
_RUST_REQUIRED_PROCESS_NAMES: tuple[str, ...] = ("trader",)
_STORAGE_OPTIONAL_PROCESS_NAMES: frozenset[str] = frozenset(
    {"scraper", "dashboard", "supervisor"}
)

_MAX_WAL_SIZE_MB = 256
_AUDIT_PRUNE_RULES: tuple[tuple[str, str, str], ...] = (
    ("feed_recovery_events", "event_time", "-2 days"),
    ("health_samples", "sample_time", "-2 days"),
)
_RESEARCH_PRUNE_RULES: tuple[tuple[str, str, str], ...] = (
    ("candidate_snapshots", "snapshot_time", "-2 days"),
    ("opportunity_scores", "score_time", "-2 days"),
    ("model_shadow_decisions", "decision_time", "-2 days"),
    ("feature_snapshots", "snapshot_time", "-3 days"),
    ("execution_quality", "sample_time", "-3 days"),
    ("market_samples", "sample_minute", "-7 days"),
    ("market_hourly_aggregates", "bucket_hour", "-90 days"),
)


class DatabaseMaintenance:
    """Maintain each split SQLite role without crossing retention boundaries."""

    def __init__(
        self,
        state_path: str,
        audit_path: str | None = None,
        research_path: str | None = None,
    ) -> None:
        self.database_paths: dict[str, Path] = {
            "state": Path(state_path).resolve(strict=False),
            "audit": Path(audit_path or TRADER_AUDIT_DB).resolve(strict=False),
            "research": Path(research_path or TRADER_RESEARCH_DB).resolve(strict=False),
        }
        if len(set(self.database_paths.values())) != 3:
            raise ValueError("database maintenance role paths must be distinct")
        # Do not prune immediately on watchdog startup. The trader and
        # observers open their role stores during bootstrap; a same-moment
        # DELETE sweep can lock them out.
        self.last_full_prune_at: float = time.time()
        self.prune_interval_seconds = 86400  # 24 hours

    @staticmethod
    def _open_existing(path: Path) -> sqlite3.Connection:
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError(f"role database is unavailable: {path}")
        return sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True, timeout=30)

    def run_maintenance_if_needed(self) -> None:
        self._check_wal_sizes()
        self._check_periodic_prune()

    def _check_wal_sizes(self) -> None:
        for role, database_path in self.database_paths.items():
            wal_path = Path(f"{database_path}-wal")
            if not wal_path.is_file():
                continue
            try:
                size_mb = wal_path.stat().st_size / (1024 * 1024)
                if size_mb > _MAX_WAL_SIZE_MB:
                    _log(
                        f"[WATCHDOG] {role} WAL size ({size_mb:.1f} MB) exceeds "
                        f"limit ({_MAX_WAL_SIZE_MB} MB). Triggering PASSIVE checkpoint..."
                    )
                    self._checkpoint(role, database_path)
            except OSError as exc:
                _log(f"[WATCHDOG] Error checking {role} WAL size: {exc}")

    def _check_periodic_prune(self) -> None:
        now = time.time()
        if (now - self.last_full_prune_at) > self.prune_interval_seconds:
            _log("[WATCHDOG] Running periodic role-separated database pruning...")
            self._prune()
            self.last_full_prune_at = now

    def _checkpoint(self, role: str, database_path: Path) -> None:
        try:
            with closing(self._open_existing(database_path)) as conn:
                # PASSIVE never blocks active readers/writers and does not
                # require a temporary second database image.
                result = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                if result and int(result[0]) != 0:
                    raise sqlite3.OperationalError(
                        f"checkpoint remained busy (log={result[1]}, checkpointed={result[2]})"
                    )
            _log(f"[WATCHDOG] {role} database checkpoint successful.")
        except (OSError, sqlite3.Error) as exc:
            _log(f"[WATCHDOG] Error during {role} database checkpoint: {exc}")

    def _prune_role(
        self,
        role: str,
        database_path: Path,
        queries: tuple[tuple[str, str, str], ...],
    ) -> None:
        try:
            with closing(self._open_existing(database_path)) as conn:
                conn.execute("PRAGMA busy_timeout=30000")

                if role == "research":
                    conn.execute(
                        """INSERT INTO market_hourly_aggregates (
                               bucket_hour, symbol, sample_count,
                               ann_funding_avg, ann_funding_min, ann_funding_max,
                               basis_pct_avg, basis_pct_min, basis_pct_max,
                               mark_price_avg, mark_price_min, mark_price_max,
                               notional_volume_sum, source_first_minute,
                               source_last_minute, refreshed_at
                           )
                           SELECT strftime('%Y-%m-%dT%H:00:00+00:00', sample_minute),
                                  symbol, COUNT(*),
                                  AVG(ann_funding), MIN(ann_funding), MAX(ann_funding),
                                  AVG(basis_pct), MIN(basis_pct), MAX(basis_pct),
                                  AVG(mark_price), MIN(mark_price), MAX(mark_price),
                                  SUM(minute_notional_volume),
                                  MIN(sample_minute), MAX(sample_minute),
                                  strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
                           FROM market_samples
                           WHERE datetime(sample_minute) < datetime('now', '-7 days')
                             AND datetime(sample_minute) >= datetime('now', '-90 days')
                           GROUP BY strftime('%Y-%m-%dT%H', sample_minute), symbol
                           ON CONFLICT(bucket_hour, symbol) DO UPDATE SET
                               sample_count=excluded.sample_count,
                               ann_funding_avg=excluded.ann_funding_avg,
                               ann_funding_min=excluded.ann_funding_min,
                               ann_funding_max=excluded.ann_funding_max,
                               basis_pct_avg=excluded.basis_pct_avg,
                               basis_pct_min=excluded.basis_pct_min,
                               basis_pct_max=excluded.basis_pct_max,
                               mark_price_avg=excluded.mark_price_avg,
                               mark_price_min=excluded.mark_price_min,
                               mark_price_max=excluded.mark_price_max,
                               notional_volume_sum=excluded.notional_volume_sum,
                               source_first_minute=excluded.source_first_minute,
                               source_last_minute=excluded.source_last_minute,
                               refreshed_at=excluded.refreshed_at"""
                    )
                    conn.commit()

                for table, col, interval in queries:
                    table_total = 0
                    for _ in range(256):
                        cursor = conn.execute(
                            f"DELETE FROM {table} WHERE rowid IN ("
                            f"SELECT rowid FROM {table} "
                            f"WHERE datetime({col}) < datetime('now', ?) LIMIT 5000)",
                            (interval,),
                        )
                        deleted = max(0, int(cursor.rowcount))
                        conn.commit()
                        table_total += deleted
                        if deleted < 5000:
                            break
                    if table_total:
                        _log(
                            f"[WATCHDOG] Pruned {table_total} Tier-"
                            f"{'B' if role == 'audit' else 'C'} rows from {role}.{table}."
                        )

                auto_vacuum = int(conn.execute("PRAGMA auto_vacuum").fetchone()[0])
                if auto_vacuum == 2:
                    conn.execute("PRAGMA incremental_vacuum(1000)")
                    conn.commit()
            _log(f"[WATCHDOG] Periodic {role} database pruning complete.")
        except (OSError, sqlite3.Error) as exc:
            _log(f"[WATCHDOG] Error during periodic {role} pruning: {exc}")

    def _prune(self) -> None:
        # State contains Tier-A recovery truth only: checkpoint it, but never
        # issue a table query or delete against it. Audit pruning is restricted
        # to the two classified Tier-B tables; research owns all Tier-C rules.
        self._prune_role("audit", self.database_paths["audit"], _AUDIT_PRUNE_RULES)
        self._prune_role(
            "research",
            self.database_paths["research"],
            _RESEARCH_PRUNE_RULES,
        )
        for role, database_path in self.database_paths.items():
            self._checkpoint(role, database_path)


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


def _autonomous_startup_recovery_enabled(
    *,
    env=None,
    config_manager: ConfigManager | None = None,
) -> bool:
    """Return a validated, mode-bounded startup-recovery decision.

    The override must be explicitly present in a successfully validated runtime
    config.  LIVE and unknown modes stay fail-closed regardless of file content.
    """

    source = _ENV if env is None else env
    trading_mode = str(source.get("TRADING_MODE", "paper") or "paper").strip().lower()
    if trading_mode not in {"paper", "testnet"}:
        return False

    manager = config_manager or ConfigManager(trading_mode=trading_mode)
    if manager.last_error:
        return False
    if "autonomous_startup_recovery" in manager.missing_required_live_keys():
        return False
    return manager.get_bool("autonomous_startup_recovery")


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
        f"BONGUS_DATA_ROOT={_safe_env('BONGUS_DATA_ROOT', str(BONGUS_DATA_ROOT))} "
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
        self._blocked_exit_identity: tuple[int, float | None] | None = None
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

    def defer_restart(self, delay_seconds: float) -> None:
        """Persist a one-shot restart delay without opening the circuit."""

        self.backoff_until = max(
            self.backoff_until,
            time.time() + max(0.0, float(delay_seconds)),
        )
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


def _trader_loop_deadline(name: str) -> float:
    if name == "storage_monitor":
        return TRADER_STORAGE_MONITOR_MAX_AGE_SECONDS
    if name == "retention_loop":
        return TRADER_RETENTION_LOOP_MAX_AGE_SECONDS
    return TRADER_REQUIRED_LOOP_MAX_AGE_SECONDS


def _stalled_trader_loops(loop_heartbeat_ages: dict[str, float] | None) -> list[str]:
    """Return required service loops whose own progress is stale.

    `on_order_update` is intentionally excluded because a quiet account may
    receive no order events.  The other loops are continuous and must progress
    independently even when the aggregate liveness writer remains healthy.
    """

    if not loop_heartbeat_ages:
        return list(TRADER_REQUIRED_PROGRESS_LOOPS)
    stalled: list[str] = []
    for name in TRADER_REQUIRED_PROGRESS_LOOPS:
        try:
            age = float(loop_heartbeat_ages[name])
        except (KeyError, TypeError, ValueError):
            stalled.append(name)
            continue
        if not math.isfinite(age) or age < 0.0 or age > _trader_loop_deadline(name):
            stalled.append(name)
    return sorted(stalled)


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


def _effective_reported_loop_ages(
    loop_heartbeat_ages: dict[str, float] | None,
    *,
    reported_at: datetime.datetime | None,
    now: datetime.datetime | None = None,
) -> dict[str, float] | None:
    """Advance serialized age-at-report values by report staleness exactly once.

    The trader publishes each loop value as ``monotonic_now - last_progress``.
    Those values stop increasing if the atomic JSON file or SQLite snapshot
    freezes, so trusting them verbatim creates a false green.  Convert them to
    effective ages at read time using the timestamp belonging to the same
    report.  Callers that already hold current/effective ages should continue
    to use :func:`_stalled_trader_loops` directly.
    """

    if loop_heartbeat_ages is None:
        return None
    if reported_at is None:
        # A relative-age report without its sampling timestamp cannot prove
        # progress.  Fail closed through the missing-map path.
        return None
    observed_now = now or datetime.datetime.now(datetime.timezone.utc)
    if observed_now.tzinfo is None:
        observed_now = observed_now.replace(tzinfo=datetime.timezone.utc)
    else:
        observed_now = observed_now.astimezone(datetime.timezone.utc)
    report_staleness = max(0.0, (observed_now - reported_at).total_seconds())
    effective: dict[str, float] = {}
    for name, raw_age in loop_heartbeat_ages.items():
        try:
            reported_age = float(raw_age)
        except (TypeError, ValueError):
            effective[str(name)] = math.inf
            continue
        if not math.isfinite(reported_age) or reported_age < 0.0:
            effective[str(name)] = math.inf
            continue
        effective[str(name)] = reported_age + report_staleness
    return effective


def _read_trader_liveness() -> tuple[str | None, datetime.datetime | None, str | None, datetime.datetime | None, dict[str, float] | None]:
    observed_now = datetime.datetime.now(datetime.timezone.utc)
    file_runtime_mode = None
    file_last_alive = None
    file_safe_mode_reason = None
    file_mode_changed_at = None
    file_loop_heartbeat_ages = None
    file_loop_ages_reported_at = None
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
                    file_loop_ages_reported_at = _parse_iso_timestamp(
                        str(payload.get("updated_at") or "")
                    ) or file_last_alive
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass

    effective_file_loop_ages = _effective_reported_loop_ages(
        file_loop_heartbeat_ages,
        reported_at=file_loop_ages_reported_at,
        now=observed_now,
    )

    if not os.path.exists(TRADER_STATE_DB):
        return (
            file_runtime_mode,
            file_last_alive,
            file_safe_mode_reason,
            file_mode_changed_at,
            effective_file_loop_ages,
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
            effective_file_loop_ages,
        )

    runtime_mode = None
    safe_mode_reason = None
    mode_changed_at = None
    loop_heartbeat_ages = None
    loop_ages_reported_at = None
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
        if key == "loop_heartbeat_ages":
            loop_ages_reported_at = updated_at
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
    effective_db_loop_ages = _effective_reported_loop_ages(
        loop_heartbeat_ages,
        reported_at=loop_ages_reported_at,
        now=observed_now,
    )
    return (
        file_runtime_mode or runtime_mode,
        merged_last_alive,
        file_safe_mode_reason if file_safe_mode_reason is not None else safe_mode_reason,
        merged_mode_changed_at,
        (
            effective_file_loop_ages
            if file_loop_heartbeat_ages is not None
            else effective_db_loop_ages
        ),
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


def _read_storage_pressure() -> tuple[bool, str]:
    """Compatibility view over the richer storage-orchestration state."""

    storage = _read_storage_orchestration_state()
    return storage.optional_processes_suppressed, storage.state


@dataclass(frozen=True, slots=True)
class StorageOrchestrationState:
    """Small, read-only supervisor view of the atomic storage snapshot."""

    available: bool
    state: str
    instantaneous_state: str
    risk_increase_blocked: bool
    emergency_latched: bool
    recovery_ready_for_operator: bool
    recovery_dashboard_allowed: bool
    integrity_ok: bool
    snapshot_fresh: bool
    detail: str = ""

    @property
    def optional_processes_suppressed(self) -> bool:
        return (
            not self.snapshot_fresh
            or not self.integrity_ok
            or self.risk_increase_blocked
            or self.state
            in {
                "degraded",
                "emergency",
                "critical",
                "invalid_snapshot",
            }
        )


def _read_storage_orchestration_state(
    *,
    now: datetime.datetime | None = None,
) -> StorageOrchestrationState:
    """Read and validate the atomic proof used for process suppression.

    Missing, malformed, stale, or otherwise unreadable proof never authorizes
    an optional writer.  Essential Rust/trader/alert services still start so
    the trader can publish a healthy snapshot.  The recovery dashboard is a
    narrow exception: it may be launched while risk remains blocked only after
    a fresh snapshot proves every guard recovery prerequisite.
    """

    result = read_storage_snapshot(Path(STORAGE_HEALTH_FILE))
    if result.get("available") is not True:
        status_name = str(result.get("status") or "unavailable")
        if status_name == "unavailable":
            return StorageOrchestrationState(
                available=False,
                state="unavailable",
                instantaneous_state="unavailable",
                risk_increase_blocked=True,
                emergency_latched=False,
                recovery_ready_for_operator=False,
                recovery_dashboard_allowed=False,
                integrity_ok=False,
                snapshot_fresh=False,
                detail=str(result.get("error") or "storage snapshot unavailable"),
            )
        return StorageOrchestrationState(
            available=False,
            state="invalid_snapshot",
            instantaneous_state="invalid_snapshot",
            risk_increase_blocked=True,
            emergency_latched=True,
            recovery_ready_for_operator=False,
            recovery_dashboard_allowed=False,
            integrity_ok=False,
            snapshot_fresh=False,
            detail=str(result.get("error") or "storage snapshot invalid"),
        )

    payload = result.get("snapshot")
    if not isinstance(payload, dict):
        return StorageOrchestrationState(
            available=False,
            state="invalid_snapshot",
            instantaneous_state="invalid_snapshot",
            risk_increase_blocked=True,
            emergency_latched=True,
            recovery_ready_for_operator=False,
            recovery_dashboard_allowed=False,
            integrity_ok=False,
            snapshot_fresh=False,
            detail="storage snapshot payload is missing",
        )

    state = str(payload.get("state") or "invalid_snapshot").strip().lower()
    instantaneous_state = str(
        payload.get("instantaneous_state") or state
    ).strip().lower()
    risk_blocked = bool(payload.get("risk_increase_blocked"))
    emergency_latched = bool(payload.get("emergency_latched"))
    recovery_ready = bool(payload.get("recovery_ready_for_operator"))
    integrity_ok = payload.get("integrity_ok") is True
    observed_at = _parse_iso_timestamp(str(payload.get("observed_at") or ""))
    current = now or datetime.datetime.now(datetime.timezone.utc)
    snapshot_fresh = False
    if observed_at is not None:
        age_seconds = (current - observed_at).total_seconds()
        snapshot_fresh = -5.0 <= age_seconds <= STORAGE_HEALTH_MAX_AGE_SECONDS

    healthy_samples = payload.get("healthy_recovery_samples")
    samples_required = payload.get("recovery_samples_required")
    samples_proven = (
        isinstance(healthy_samples, int)
        and not isinstance(healthy_samples, bool)
        and isinstance(samples_required, int)
        and not isinstance(samples_required, bool)
        and samples_required > 0
        and healthy_samples >= samples_required
    )
    active_faults = payload.get("active_faults")
    no_active_faults = isinstance(active_faults, list) and not active_faults
    recovery_dashboard_allowed = all(
        (
            risk_blocked,
            recovery_ready,
            snapshot_fresh,
            state == "healthy",
            instantaneous_state == "healthy",
            samples_proven,
            integrity_ok,
            payload.get("exchange_reconciled") is True,
            no_active_faults,
        )
    )
    return StorageOrchestrationState(
        available=True,
        state=state,
        instantaneous_state=instantaneous_state,
        risk_increase_blocked=risk_blocked,
        emergency_latched=emergency_latched,
        recovery_ready_for_operator=recovery_ready,
        recovery_dashboard_allowed=recovery_dashboard_allowed,
        integrity_ok=integrity_ok,
        snapshot_fresh=snapshot_fresh,
    )


def _storage_process_allowed(name: str, storage: StorageOrchestrationState) -> bool:
    """Keep survival services alive and suppress every optional writer."""

    if name not in _STORAGE_OPTIONAL_PROCESS_NAMES:
        return True
    if not storage.optional_processes_suppressed:
        return True
    return name == "dashboard" and storage.recovery_dashboard_allowed


def _stop_supervised_process_for_storage(
    proc: subprocess.Popen | _StoppedProcess,
    *,
    name: str,
    storage_state: str,
) -> _StoppedProcess:
    if proc.poll() is None:
        _log(
            "[WATCHDOG] Storage pressure "
            f"({storage_state}); stopping optional {name} process."
        )
        proc.terminate()
        try:
            proc.wait(timeout=PROCESS_STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    return _StoppedProcess(returncode=0)


def _stop_project_backup_jobs_for_storage(storage_state: str) -> None:
    backup_jobs = _find_managed_project_processes("backup_job")
    if backup_jobs:
        _terminate_processes(
            backup_jobs,
            reason=(
                "Storage pressure "
                f"({storage_state}); stopping optional project backup jobs"
            ),
        )


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


def _child_starts_new_session(platform_name: str | None = None) -> bool:
    """Isolate POSIX children from the watchdog terminal's signal group."""

    return (platform_name or os.name) == "posix"


def start_process(command, name: str, cwd=None):
    run_cwd = cwd or _PROJECT_ROOT
    if name == "rust":
        if list(command) != RUST_COMMAND:
            raise RuntimeError("refusing an unmanifested Rust launch command")
        if not run_preflight_checks():
            raise RuntimeError("packaged Rust executable failed per-start verification")
        RUST_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    _log(f"Starting {name}: {' '.join(command)} (cwd={run_cwd})")
    proc = subprocess.Popen(
        command, cwd=run_cwd, env=_ENV,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=_child_starts_new_session(),
    )
    threading.Thread(target=_pipe_reader, args=(proc.stdout, name), daemon=True).start()
    threading.Thread(target=_pipe_reader, args=(proc.stderr, name), daemon=True).start()
    return proc


def _terminate_and_reap_process(
    proc: subprocess.Popen | _StoppedProcess,
    *,
    name: str,
    timeout: float,
    reason: str,
) -> None:
    """Terminate one child, then force-kill and reap it after its grace period."""

    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except (OSError, ProcessLookupError):
        if proc.poll() is not None:
            return
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _log(
            f"[WATCHDOG] {name} did not terminate gracefully within "
            f"{timeout:.0f}s after {reason}; "
            "sending SIGKILL."
        )
        with suppress(OSError, ProcessLookupError):
            proc.kill()
        proc.wait()


def _stop_trader_for_restart(proc, *, reason: str) -> None:
    """Give the trader time to persist and drain before forced termination."""

    _terminate_and_reap_process(
        proc,
        name="trader",
        timeout=TRADER_GRACEFUL_STOP_TIMEOUT_SECONDS,
        reason=reason,
    )


def _shutdown_supervised_processes(
    procs: dict[str, subprocess.Popen | _StoppedProcess],
) -> None:
    """Stop children in dependency order, reaping each child exactly once."""

    optional_names = [name for name in procs if name not in {"trader", "rust"}]
    ordered_names = (["trader"] if "trader" in procs else []) + optional_names
    if "rust" in procs:
        ordered_names.append("rust")

    seen_processes: set[int] = set()
    for name in ordered_names:
        proc = procs[name]
        process_identity = id(proc)
        if process_identity in seen_processes:
            continue
        seen_processes.add(process_identity)
        timeout = (
            TRADER_GRACEFUL_STOP_TIMEOUT_SECONDS
            if name == "trader"
            else PROCESS_STOP_TIMEOUT_SECONDS
        )
        try:
            _terminate_and_reap_process(
                proc,
                name=name,
                timeout=timeout,
                reason="watchdog shutdown",
            )
        except Exception as exc:
            # One broken child handle must not strand the remaining children,
            # especially the Rust process that intentionally stays up until
            # the trader has completed its durable shutdown.
            _log(f"[WATCHDOG] Error while stopping {name}: {exc}")


class _WatchdogShutdownRequested(BaseException):
    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


@dataclass(slots=True)
class _ShutdownSignalState:
    cleanup_started: bool = False

    def handle(self, signum: int, _frame: object) -> None:
        if self.cleanup_started:
            # Once ordered cleanup begins, subsequent terminal/service signals
            # cannot interrupt the trader's durable 60-second grace period.
            return
        raise _WatchdogShutdownRequested(signum)


def _install_shutdown_signal_handlers(
    state: _ShutdownSignalState,
) -> dict[int, Any]:
    previous_handlers: dict[int, Any] = {}
    requested_signals = [signal.SIGINT, signal.SIGTERM]
    hangup_signal = getattr(signal, "SIGHUP", None)
    if hangup_signal is not None:
        requested_signals.append(hangup_signal)
    for requested_signal in dict.fromkeys(requested_signals):
        signal_number = int(requested_signal)
        previous_handlers[signal_number] = signal.getsignal(signal_number)
        signal.signal(signal_number, state.handle)
    return previous_handlers


def _restore_shutdown_signal_handlers(previous_handlers: dict[int, Any]) -> None:
    for signal_number, previous_handler in previous_handlers.items():
        signal.signal(signal_number, previous_handler)


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
            autonomous_recovery = _autonomous_startup_recovery_enabled()
            rust_proc = all_procs.get("rust") if all_procs else None
            if (
                autonomous_recovery
                and bridge_preflight_blocked
                and rust_proc is not None
                and rust_proc.poll() is None
            ):
                _log(
                    "[WATCHDOG] Trader exited in BLOCKED mode because the Rust execution bridge "
                    "was still coming up. Waiting for Rust IPC and retrying trader startup."
                )
                _wait_for_rust_ipc(timeout=30)
                if _rust_ipc_ready():
                    return start_process(command, name=name, cwd=cwd)
            
            if autonomous_recovery:
                exit_identity = (int(getattr(proc, "pid", -1)), started_at)
                already_recorded = getattr(tracker, "_blocked_exit_identity", None) == exit_identity
                if not already_recorded:
                    tracker._blocked_exit_identity = exit_identity
                    tracker.record_crash()
                    if not tracker.permanently_failed:
                        tracker.defer_restart(30.0)
                    delay = max(0.0, tracker.backoff_until - time.time())
                    _log(
                        "[WATCHDOG] Trader exited in BLOCKED mode (exit=78). "
                        "Validated autonomous recovery is enabled for paper/testnet; "
                        f"retrying in {delay:.0f}s."
                    )
                    return proc

                if not tracker.should_restart():
                    return proc

                block_reason = _start_block_reason(name, ignore_pids={proc.pid})
                if block_reason is not None:
                    tracker.trip_circuit()
                    _log(f"[WATCHDOG] FATAL: cannot restart {name}: {block_reason}")
                    return proc

                tracker._blocked_exit_identity = None
                _log(
                    "[WATCHDOG] BLOCKED trader retry delay elapsed; restarting under "
                    "validated paper/testnet autonomous recovery."
                )
                return start_process(command, name=name, cwd=cwd)
                
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
            if name == "trader":
                _stop_trader_for_restart(proc, reason="memory limit restart")
            else:
                proc.terminate()
                try:
                    proc.wait(timeout=PROCESS_STOP_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    _log(
                        f"[WATCHDOG] {name} did not terminate in "
                        f"{PROCESS_STOP_TIMEOUT_SECONDS:.0f}s, sending SIGKILL..."
                    )
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

                _stop_trader_for_restart(proc, reason="aggregate liveness restart")
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
            _stop_trader_for_restart(proc, reason="service loop restart")
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

                _stop_trader_for_restart(proc, reason="stale pending intent restart")
                return start_process(command, name=name, cwd=cwd)

    return proc


def _release_rust_contract(binary: Path) -> tuple[str | None, bool]:
    """Return the manifest-bound digest and production eligibility.

    Source checkouts do not contain a release manifest and may use the explicit
    environment hash.  A packaged release always binds the exact Rust path and
    bytes, including development-only packages.
    """

    manifest_path = Path(_PROJECT_ROOT, "release-manifest.json")
    if not manifest_path.exists():
        return None, False
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 2:
        raise ValueError("unsupported packaged release manifest schema")
    production_eligible = payload.get("production_eligible")
    rust_record = payload.get("rust_binary")
    if not isinstance(production_eligible, bool) or not isinstance(rust_record, dict):
        raise ValueError("malformed packaged Rust release contract")
    raw_relative = rust_record.get("path")
    expected_digest = rust_record.get("sha256")
    if not isinstance(raw_relative, str) or not isinstance(expected_digest, str):
        raise ValueError("packaged Rust path/hash is missing")
    relative = Path(raw_relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("packaged Rust path is not contained")
    if Path(_PROJECT_ROOT, relative).resolve() != binary.resolve():
        raise ValueError("packaged Rust path disagrees with process manifest")
    if len(expected_digest) != 64 or any(
        character not in "0123456789abcdef" for character in expected_digest
    ):
        raise ValueError("packaged Rust SHA-256 is malformed")
    return expected_digest, production_eligible


def _validate_native_executable(binary: Path) -> None:
    executable_format, _machine = inspect_executable(binary)
    expected_format = "pe" if os.name == "nt" else "elf"
    if executable_format != expected_format:
        raise ValueError(
            "packaged Rust engine is for the wrong operating system: "
            f"expected={expected_format}, observed={executable_format}"
        )


def _validate_pe_header(binary: Path) -> None:
    """Backward-compatible test/API shim; runtime uses the native validator."""

    executable_format, _machine = inspect_executable(binary)
    if executable_format != "pe":
        raise ValueError("packaged Rust engine is not a PE executable")


def _verify_live_rust_approval(binary: Path) -> None:
    """Revalidate the operator-held approval before every live Rust launch."""

    config = ConfigManager()
    artifact_path = str(config.get("live_approval_artifact_path") or "").strip()
    if not artifact_path:
        raise LiveApprovalError("live approval artifact path is required")
    approval = verify_live_approval(
        artifact_path,
        key=str(_ENV.get("BONGUS_LIVE_APPROVAL_HMAC_KEY", "") or "").encode("utf-8"),
        expected_config_sha256=config.canonical_snapshot().sha256,
        release_manifest_path=Path(_PROJECT_ROOT, "release-manifest.json"),
        rust_binary_path=binary,
        expected_account_id=(
            str(_ENV.get("BONGUS_EXPECTED_ACCOUNT_UID", "") or "").strip() or None
        ),
    )
    _log(
        "Live Rust approval verified "
        f"(approved_by={approval.approved_by}, expires_at={approval.expires_at})."
    )


def run_preflight_checks() -> bool:
    """Verify the packaged Rust executable without building on the device."""

    binary = Path(RUST_COMMAND[0])
    _log(f"Running preflight check for packaged Rust engine at {binary}...")
    try:
        if not binary.is_file():
            _log(
                "[WATCHDOG] FATAL: packaged Rust engine is missing. "
                "Build with `cargo build --locked --release` on a build volume "
                f"and deploy only {binary.name}."
            )
            return False
        if binary.stat().st_size <= 0:
            _log("[WATCHDOG] FATAL: packaged Rust engine is empty.")
            return False
        _validate_native_executable(binary)
        trading_mode = str(_ENV.get("TRADING_MODE", "paper") or "paper").strip().lower()
        release_manifest_path = Path(_PROJECT_ROOT, "release-manifest.json")
        if release_manifest_path.exists():
            verify_runtime_inventory(
                Path(_PROJECT_ROOT),
                require_production=trading_mode == "live",
                expected_linux_signing_key_sha256=(
                    str(_ENV.get("BONGUS_RELEASE_SIGNING_KEY_SHA256", "") or "")
                    if trading_mode == "live"
                    else ""
                ),
            )
        manifest_sha256, production_eligible = _release_rust_contract(binary)
        configured_sha256 = str(
            _ENV.get("BONGUS_RUST_BINARY_SHA256", "") or ""
        ).strip().lower()
        if configured_sha256 and manifest_sha256 and configured_sha256 != manifest_sha256:
            _log("[WATCHDOG] FATAL: configured and release-manifest Rust hashes disagree.")
            return False
        expected_sha256 = configured_sha256 or manifest_sha256 or ""
        if expected_sha256:
            if len(expected_sha256) != 64 or any(
                character not in "0123456789abcdef"
                for character in expected_sha256
            ):
                _log("[WATCHDOG] FATAL: BONGUS_RUST_BINARY_SHA256 is malformed.")
                return False
            digest = hashlib.sha256()
            with binary.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
            if digest.hexdigest() != expected_sha256:
                _log("[WATCHDOG] FATAL: packaged Rust binary hash mismatch.")
                return False
        if trading_mode == "live":
            if not production_eligible:
                _log(
                    "[WATCHDOG] FATAL: live mode requires a signed, production-eligible release manifest."
                )
                return False
            _verify_live_rust_approval(binary)
        _log(
            "Packaged Rust engine preflight passed "
            f"(bytes={binary.stat().st_size}, hash_pinned={bool(expected_sha256)}, "
            f"production_eligible={production_eligible})."
        )
        return True
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        LiveApprovalError,
        ReleaseManifestError,
    ) as e:
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
    startup_storage = _read_storage_orchestration_state()
    archive_result = None
    if startup_storage.optional_processes_suppressed:
        _log(
            "[WATCHDOG] Storage pressure "
            f"({startup_storage.state}); skipping optional startup artifact archival."
        )
        _stop_project_backup_jobs_for_storage(startup_storage.state)
    else:
        archive_result = archive_startup_artifacts(
            _RUNTIME_ROOT,
            retention_count=startup_archive_retention_from_env(),
            retention_days=startup_archive_retention_days_from_env(),
            max_total_bytes=startup_archive_max_bytes_from_env(),
            # Durable journals already carry their own checksums/checkpoints.  A
            # startup archive references them instead of multiplying them on every
            # restart.
            copy_durable=False,
        )
    _log("Starting King Watchdog Supervisor...")
    if archive_result is not None and archive_result.archive_dir is not None:
        archive_relative = archive_result.archive_dir.relative_to(
            _RUNTIME_ROOT
        )
        _log(
            "[WATCHDOG] Archived previous session diagnostics to "
            f"{archive_relative} "
            f"(moved={len(archive_result.moved)}, "
            f"snapshotted={len(archive_result.copied)})."
        )
    if archive_result is not None:
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
    db_maint = DatabaseMaintenance(
        TRADER_STATE_DB,
        TRADER_AUDIT_DB,
        TRADER_RESEARCH_DB,
    )
    storage_suppressed_names: set[str] = set()
    shutdown_signal_state = _ShutdownSignalState()
    previous_signal_handlers = _install_shutdown_signal_handlers(
        shutdown_signal_state
    )

    try:
        for name, cmd, cwd in process_defs:
            if not _storage_process_allowed(name, startup_storage):
                _log(
                    "[WATCHDOG] Storage pressure "
                    f"({startup_storage.state}); not starting optional {name} process."
                )
                procs[name] = _StoppedProcess(returncode=0)
                start_times[name] = time.time()
                storage_suppressed_names.add(name)
                continue
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

        while True:
            storage = _read_storage_orchestration_state()
            storage_blocked = storage.optional_processes_suppressed
            storage_state = storage.state
            if not storage_blocked:
                db_maint.run_maintenance_if_needed()
            else:
                _stop_project_backup_jobs_for_storage(storage_state)
            time.sleep(10)
            for name, cmd, cwd in process_defs:
                proc = procs[name]
                tracker = trackers[name]

                if not _storage_process_allowed(name, storage):
                    if name not in storage_suppressed_names or proc.poll() is None:
                        procs[name] = _stop_supervised_process_for_storage(
                            proc,
                            name=name,
                            storage_state=storage_state,
                        )
                    storage_suppressed_names.add(name)
                    continue
                if (
                    name in storage_suppressed_names
                    and proc.poll() is not None
                ):
                    block_reason = _start_block_reason(name)
                    if block_reason is None:
                        if storage_blocked and name == "dashboard":
                            _log(
                                "[WATCHDOG] Fresh atomic recovery proof is ready; "
                                "starting the authenticated recovery dashboard."
                            )
                        else:
                            _log(
                                "[WATCHDOG] Storage risk latch was explicitly cleared; "
                                f"restarting optional {name} process."
                            )
                        proc = start_process(cmd, name=name, cwd=cwd)
                        procs[name] = proc
                        start_times[name] = time.time()
                        storage_suppressed_names.discard(name)
                    else:
                        _log(
                            f"[WATCHDOG] Optional {name} remains stopped: {block_reason}"
                        )
                    continue

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

    except (KeyboardInterrupt, _WatchdogShutdownRequested):
        pass
    finally:
        shutdown_signal_state.cleanup_started = True
        _log("Watchdog shutting down. Terminating child processes...")
        _shutdown_supervised_processes(procs)
        _restore_shutdown_signal_handlers(previous_signal_handlers)


if __name__ == "__main__":
    main()
