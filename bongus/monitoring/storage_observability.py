"""Read-only storage diagnostics for the operator dashboard.

The storage guard publishes one small JSON document with an atomic replace.
This module consumes that document without creating directories or databases.
SQLite file and page metrics come from filesystem metadata and the database
header.  The optional table-activity probe uses an immutable, query-only
connection so it cannot create a database, journal, WAL, or checkpoint.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final


DEFAULT_STORAGE_SNAPSHOT_RELATIVE_PATH: Final = Path("runtime/storage_health.json")
DEFAULT_STATE_DATABASE_RELATIVE_PATH: Final = Path("state.db")
DEFAULT_AUDIT_DATABASE_RELATIVE_PATH: Final = Path("audit.db")
DEFAULT_RESEARCH_DATABASE_RELATIVE_PATH: Final = Path("research.db")
STORAGE_SNAPSHOT_MAX_BYTES: Final = 1_000_000
TABLE_RATE_WINDOW_SECONDS: Final = 3_600
TABLE_RATE_QUERY_BUDGET_SECONDS: Final = 0.5

_STORAGE_STATES: Final = frozenset(
    {"healthy", "warning", "degraded", "emergency", "critical"}
)
_DEGRADED_STATES: Final = frozenset({"degraded", "emergency", "critical"})

# These columns are time indexes in the current state-store schema. Keeping the
# allowlist fixed avoids accepting table or column names from the snapshot or a
# request. A missing table is reported normally during rolling migrations.
_TABLE_RATE_PROBES: Final = (
    ("candidate_snapshots", "snapshot_time"),
    ("opportunity_scores", "score_time"),
    ("model_shadow_decisions", "decision_time"),
    ("health_samples", "sample_time"),
    ("market_samples", "sample_minute"),
    ("market_hourly_aggregates", "bucket_hour"),
)
_STATE_TABLE_RATE_PROBES: Final = ()
_AUDIT_TABLE_RATE_PROBES: Final = (
    ("feed_recovery_events", "event_time"),
    ("health_samples", "sample_time"),
)
_RESEARCH_TABLE_RATE_PROBES: Final = (
    ("candidate_snapshots", "snapshot_time"),
    ("opportunity_scores", "score_time"),
    ("model_shadow_decisions", "decision_time"),
    ("feature_snapshots", "snapshot_time"),
    ("execution_quality", "sample_time"),
    ("market_samples", "sample_minute"),
    ("market_hourly_aggregates", "bucket_hour"),
)


def _resolved_override(
    project_root: Path,
    environment_name: str,
    default_relative_path: Path,
) -> Path:
    configured = os.getenv(environment_name, "").strip()
    path = Path(configured) if configured else project_root / default_relative_path
    if not path.is_absolute():
        path = project_root / path
    # ``absolute`` is deliberately used instead of ``resolve``: diagnostics
    # must not require the target to exist and must not follow links here.
    return path.absolute()


def _database_root(project_root: Path) -> Path:
    configured = os.getenv("BONGUS_DATA_ROOT", "").strip()
    if not configured:
        return project_root.absolute()
    candidate = Path(configured)
    if not candidate.is_absolute():
        candidate = project_root / candidate
    return candidate.absolute()


def storage_snapshot_path(project_root: Path) -> Path:
    return _resolved_override(
        project_root,
        "BONGUS_STORAGE_HEALTH_SNAPSHOT_PATH",
        DEFAULT_STORAGE_SNAPSHOT_RELATIVE_PATH,
    )


def state_database_path(project_root: Path) -> Path:
    default_root = _database_root(project_root)
    return _resolved_override(
        default_root,
        "BONGUS_STATE_DB_PATH",
        DEFAULT_STATE_DATABASE_RELATIVE_PATH,
    )


def audit_database_path(project_root: Path) -> Path:
    default_root = _database_root(project_root)
    return _resolved_override(
        default_root,
        "BONGUS_AUDIT_DB_PATH",
        DEFAULT_AUDIT_DATABASE_RELATIVE_PATH,
    )


def research_database_path(project_root: Path) -> Path:
    default_root = _database_root(project_root)
    return _resolved_override(
        default_root,
        "BONGUS_RESEARCH_DB_PATH",
        DEFAULT_RESEARCH_DATABASE_RELATIVE_PATH,
    )


def _unavailable_snapshot(path: Path, status_name: str, error: str) -> dict[str, object]:
    return {
        "available": False,
        "status": status_name,
        "path": str(path),
        "size_bytes": None,
        "modified_at": None,
        "snapshot": None,
        "error": error,
    }


def read_storage_snapshot(
    path: Path,
    *,
    max_bytes: int = STORAGE_SNAPSHOT_MAX_BYTES,
) -> dict[str, object]:
    """Read one atomically published snapshot with a strict memory bound."""

    path = Path(path).absolute()
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return _unavailable_snapshot(path, "unavailable", "storage health snapshot is not available")
    except OSError as exc:
        return _unavailable_snapshot(path, "unavailable", f"could not inspect storage health snapshot: {exc}")

    if path.is_symlink() or not stat.S_ISREG(path_stat.st_mode):
        return _unavailable_snapshot(path, "malformed", "storage health snapshot is not a regular file")
    if path_stat.st_size > max(0, max_bytes):
        result = _unavailable_snapshot(
            path,
            "oversized",
            f"storage health snapshot exceeds {max(0, max_bytes)} bytes",
        )
        result["size_bytes"] = int(path_stat.st_size)
        return result

    try:
        with path.open("rb") as handle:
            encoded = handle.read(max(0, max_bytes) + 1)
    except OSError as exc:
        return _unavailable_snapshot(path, "unavailable", f"could not read storage health snapshot: {exc}")
    if len(encoded) > max(0, max_bytes):
        result = _unavailable_snapshot(
            path,
            "oversized",
            f"storage health snapshot exceeds {max(0, max_bytes)} bytes",
        )
        result["size_bytes"] = len(encoded)
        return result

    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        result = _unavailable_snapshot(path, "malformed", f"storage health snapshot is malformed: {exc}")
        result["size_bytes"] = len(encoded)
        return result
    if not isinstance(payload, dict):
        result = _unavailable_snapshot(
            path,
            "malformed",
            "storage health snapshot is malformed: expected a JSON object",
        )
        result["size_bytes"] = len(encoded)
        return result

    state = payload.get("state")
    required_fields = (
        "generation",
        "observed_at",
        "volumes",
        "components",
        "risk_increase_blocked",
    )
    missing = [field for field in required_fields if field not in payload]
    invalid_shape = (
        isinstance(payload.get("generation"), bool)
        or not isinstance(payload.get("generation"), int)
        or not isinstance(payload.get("observed_at"), str)
        or not isinstance(payload.get("volumes"), list)
        or not isinstance(payload.get("components"), list)
        or not isinstance(payload.get("risk_increase_blocked"), bool)
    )
    if not isinstance(state, str) or state.lower() not in _STORAGE_STATES or missing or invalid_shape:
        if not isinstance(state, str) or state.lower() not in _STORAGE_STATES:
            detail = "invalid storage state"
        elif missing:
            detail = "missing required fields: " + ", ".join(missing)
        else:
            detail = "one or more required fields have invalid types"
        result = _unavailable_snapshot(path, "malformed", f"storage health snapshot is malformed: {detail}")
        result["size_bytes"] = len(encoded)
        return result

    return {
        "available": True,
        "status": "available",
        "path": str(path),
        "size_bytes": len(encoded),
        "modified_at": datetime.fromtimestamp(path_stat.st_mtime, tz=timezone.utc).isoformat(),
        "snapshot": payload,
        "error": None,
    }


def storage_is_degraded(snapshot_result: dict[str, object]) -> bool:
    """Fail conservatively when storage health cannot be proven available."""

    if snapshot_result.get("available") is not True:
        return True
    payload = snapshot_result.get("snapshot")
    if not isinstance(payload, dict):
        return True
    return str(payload.get("state") or "").lower() in _DEGRADED_STATES


def _file_size(path: Path) -> tuple[bool, int | None, str | None]:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return False, 0, None
    except OSError as exc:
        return False, None, str(exc)
    if path.is_symlink() or not stat.S_ISREG(path_stat.st_mode):
        return False, None, "not a regular file"
    return True, int(path_stat.st_size), None


def _sqlite_header_metrics(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as handle:
            header = handle.read(100)
    except OSError as exc:
        return {"available": False, "source": "sqlite_header", "error": str(exc)}

    if len(header) < 100 or header[:16] != b"SQLite format 3\x00":
        return {
            "available": False,
            "source": "sqlite_header",
            "error": "database does not contain a complete SQLite header",
        }

    raw_page_size = int.from_bytes(header[16:18], "big")
    page_size = 65_536 if raw_page_size == 1 else raw_page_size
    if page_size < 512 or page_size > 65_536 or page_size & (page_size - 1):
        return {
            "available": False,
            "source": "sqlite_header",
            "error": f"invalid SQLite page size {page_size}",
        }

    page_count = int.from_bytes(header[28:32], "big")
    freelist_pages = int.from_bytes(header[36:40], "big")
    try:
        physical_size = path.stat().st_size
    except OSError as exc:
        return {"available": False, "source": "sqlite_header", "error": str(exc)}
    physical_pages = physical_size // page_size
    # Page-count zero is legal only for very old SQLite database headers. The
    # physical page count remains useful in that case.
    effective_page_count = page_count or physical_pages
    used_pages = max(0, effective_page_count - min(freelist_pages, effective_page_count))
    return {
        "available": True,
        "source": "sqlite_header",
        "page_size_bytes": page_size,
        "page_count": effective_page_count,
        "header_page_count": page_count,
        "physical_page_count": physical_pages,
        "freelist_pages": freelist_pages,
        "freelist_bytes": freelist_pages * page_size,
        "used_pages": used_pages,
        "used_page_bytes": used_pages * page_size,
        "file_change_counter": int.from_bytes(header[24:28], "big"),
        "schema_cookie": int.from_bytes(header[40:44], "big"),
        "error": None,
    }


def _table_insert_rates(
    path: Path,
    *,
    observed_at: datetime,
    window_seconds: int = TABLE_RATE_WINDOW_SECONDS,
    probes: tuple[tuple[str, str], ...] = _TABLE_RATE_PROBES,
) -> dict[str, object]:
    window_seconds = max(1, int(window_seconds))
    window_start = observed_at - timedelta(seconds=window_seconds)
    deadline = time.monotonic() + TABLE_RATE_QUERY_BUDGET_SECONDS
    connection: sqlite3.Connection | None = None
    try:
        # ``immutable=1`` is intentional. It excludes the WAL from this
        # diagnostic view, but guarantees that observing the live database
        # cannot create sidecars, take write locks, or checkpoint it.
        uri = path.resolve(strict=True).as_uri() + "?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True, timeout=0.0)
        connection.execute("PRAGMA query_only=ON")
        connection.set_progress_handler(lambda: 1 if time.monotonic() >= deadline else 0, 1_000)
        existing_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            )
        }
        tables: dict[str, dict[str, object]] = {}
        for table, timestamp_column in probes:
            if table not in existing_tables:
                tables[table] = {
                    "available": False,
                    "timestamp_column": timestamp_column,
                    "reason": "table is not present",
                }
                continue
            if time.monotonic() >= deadline:
                tables[table] = {
                    "available": False,
                    "timestamp_column": timestamp_column,
                    "reason": "shared table-rate query budget exhausted",
                }
                continue
            try:
                # Table and column identifiers come exclusively from the fixed
                # module allowlist above; request/snapshot data is never used.
                row = connection.execute(
                    f'SELECT COUNT(*) FROM "{table}" '
                    f'WHERE "{timestamp_column}" >= ? AND "{timestamp_column}" <= ?',
                    (window_start.isoformat(), observed_at.isoformat()),
                ).fetchone()
                recent_rows = int(row[0] or 0) if row is not None else 0
                tables[table] = {
                    "available": True,
                    "timestamp_column": timestamp_column,
                    "rows_in_window": recent_rows,
                    "estimated_inserts_per_hour": recent_rows * 3_600.0 / window_seconds,
                    "reason": None,
                }
            except sqlite3.Error as exc:
                tables[table] = {
                    "available": False,
                    "timestamp_column": timestamp_column,
                    "reason": f"{type(exc).__name__}: {exc}",
                }
        return {
            "available": any(bool(item.get("available")) for item in tables.values()),
            "source": "sqlite_immutable_main_database",
            "includes_uncheckpointed_wal": False,
            "window_seconds": window_seconds,
            "window_started_at": window_start.isoformat(),
            "tables": tables,
            "error": None,
        }
    except (OSError, sqlite3.Error) as exc:
        return {
            "available": False,
            "source": "sqlite_immutable_main_database",
            "includes_uncheckpointed_wal": False,
            "window_seconds": window_seconds,
            "window_started_at": window_start.isoformat(),
            "tables": {},
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass


def collect_database_metrics(
    path: Path,
    *,
    observed_at: datetime | None = None,
    table_rate_probes: tuple[tuple[str, str], ...] = _TABLE_RATE_PROBES,
) -> dict[str, object]:
    """Collect no-write database, WAL, SHM, page, and activity metrics."""

    path = Path(path).absolute()
    now = (observed_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    database_exists, database_bytes, database_error = _file_size(path)
    wal_path = Path(f"{path}-wal")
    shm_path = Path(f"{path}-shm")
    wal_exists, wal_bytes, wal_error = _file_size(wal_path)
    shm_exists, shm_bytes, shm_error = _file_size(shm_path)
    files = {
        "database": {
            "path": str(path),
            "exists": database_exists,
            "size_bytes": database_bytes,
            "error": database_error,
        },
        "wal": {
            "path": str(wal_path),
            "exists": wal_exists,
            "size_bytes": wal_bytes,
            "error": wal_error,
        },
        "shm": {
            "path": str(shm_path),
            "exists": shm_exists,
            "size_bytes": shm_bytes,
            "error": shm_error,
        },
    }
    known_sizes = (database_bytes, wal_bytes, shm_bytes)
    total_bytes = sum(value for value in known_sizes if isinstance(value, int))

    if not database_exists:
        return {
            "available": False,
            "status": "unavailable",
            "read_mode": "filesystem_header_and_sqlite_immutable",
            "files": files,
            "total_size_bytes": total_bytes,
            "pages": {"available": False, "source": "sqlite_header", "error": database_error or "database is not available"},
            "table_insert_rates": {
                "available": False,
                "source": "sqlite_immutable_main_database",
                "includes_uncheckpointed_wal": False,
                "window_seconds": TABLE_RATE_WINDOW_SECONDS,
                "tables": {},
                "error": database_error or "database is not available",
            },
            "error": database_error or "state database is not available",
        }

    pages = _sqlite_header_metrics(path)
    rates = _table_insert_rates(
        path,
        observed_at=now,
        probes=table_rate_probes,
    )
    available = bool(pages.get("available"))
    return {
        "available": available,
        "status": "available" if available else "malformed",
        "read_mode": "filesystem_header_and_sqlite_immutable",
        "files": files,
        "total_size_bytes": total_bytes,
        "pages": pages,
        "table_insert_rates": rates,
        "error": None if available else pages.get("error"),
    }


def collect_storage_observability(
    project_root: Path,
    *,
    snapshot_path: Path | None = None,
    database_path: Path | None = None,
    audit_path: Path | None = None,
    research_path: Path | None = None,
    observed_at: datetime | None = None,
) -> dict[str, object]:
    """Build the complete bounded dashboard payload without filesystem writes."""

    project_root = Path(project_root).absolute()
    now = (observed_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    snapshot = read_storage_snapshot(snapshot_path or storage_snapshot_path(project_root))
    resolved_state_path = database_path or state_database_path(project_root)
    state_override = os.getenv("BONGUS_STATE_DB_PATH", "").strip()
    data_root_override = os.getenv("BONGUS_DATA_ROOT", "").strip()
    derived_role_root = Path(resolved_state_path).absolute().parent
    resolved_audit_path = audit_path or (
        derived_role_root / DEFAULT_AUDIT_DATABASE_RELATIVE_PATH
        if state_override and not data_root_override and not os.getenv(
            "BONGUS_AUDIT_DB_PATH", ""
        ).strip()
        else audit_database_path(project_root)
    )
    resolved_research_path = research_path or (
        derived_role_root / DEFAULT_RESEARCH_DATABASE_RELATIVE_PATH
        if state_override and not data_root_override and not os.getenv(
            "BONGUS_RESEARCH_DB_PATH", ""
        ).strip()
        else research_database_path(project_root)
    )
    database = collect_database_metrics(
        resolved_state_path,
        observed_at=now,
        table_rate_probes=_STATE_TABLE_RATE_PROBES,
    )
    audit_database = collect_database_metrics(
        resolved_audit_path,
        observed_at=now,
        table_rate_probes=_AUDIT_TABLE_RATE_PROBES,
    )
    research_database = collect_database_metrics(
        resolved_research_path,
        observed_at=now,
        table_rate_probes=_RESEARCH_TABLE_RATE_PROBES,
    )
    snapshot_payload = snapshot.get("snapshot")
    return {
        "schema_version": 1,
        "observed_at": now.isoformat(),
        "available": snapshot.get("available") is True,
        "status": snapshot.get("status"),
        "error": snapshot.get("error"),
        "snapshot_path": snapshot.get("path"),
        "snapshot_size_bytes": snapshot.get("size_bytes"),
        "snapshot_modified_at": snapshot.get("modified_at"),
        "snapshot": snapshot_payload,
        "database": database,
        "databases": {
            "state": database,
            "audit": audit_database,
            "research": research_database,
        },
        "risk_increase_blocked": (
            bool(snapshot_payload.get("risk_increase_blocked", False))
            if isinstance(snapshot_payload, dict)
            else True
        ),
    }


__all__ = [
    "DEFAULT_AUDIT_DATABASE_RELATIVE_PATH",
    "DEFAULT_RESEARCH_DATABASE_RELATIVE_PATH",
    "DEFAULT_STATE_DATABASE_RELATIVE_PATH",
    "DEFAULT_STORAGE_SNAPSHOT_RELATIVE_PATH",
    "STORAGE_SNAPSHOT_MAX_BYTES",
    "TABLE_RATE_WINDOW_SECONDS",
    "collect_database_metrics",
    "collect_storage_observability",
    "audit_database_path",
    "read_storage_snapshot",
    "state_database_path",
    "research_database_path",
    "storage_is_degraded",
    "storage_snapshot_path",
]
