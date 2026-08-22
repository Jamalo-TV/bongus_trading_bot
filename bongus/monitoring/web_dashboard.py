import asyncio
import base64
import hashlib
import json
import os
import secrets
import sys
import tempfile
import threading
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import BinaryIO, cast

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.background import BackgroundTask
from starlette.requests import HTTPConnection

if __package__ in {None, ""}:
    _BOOTSTRAP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if _BOOTSTRAP_ROOT not in sys.path:
        sys.path.insert(0, _BOOTSTRAP_ROOT)

from bongus.core.config import (
    AUDIT_DB_PATH,
    LIVE_CONFIG_PATH,
    RESEARCH_DB_PATH,
    STATE_DB_PATH,
)
from bongus.core.config_manager import ConfigManager
from bongus.engine.split_state_store import SplitStateReader
from bongus.engine.state_store import StateReader
from bongus.ipc.telemetry import TelemetryClient
from bongus.monitoring.log_artifacts import (
    DEFAULT_SUPPORT_BUNDLE_MAX_BYTES,
    write_support_bundle,
)
from bongus.monitoring.performance_metrics import calculate_metrics
from bongus.monitoring.storage_observability import (
    collect_storage_observability,
    read_storage_snapshot,
    storage_is_degraded,
    storage_snapshot_path,
)
from bongus.supervisor.daily_report import build_reconciled_daily_report

active_connections: set[WebSocket] = set()

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DOTENV_PATH = os.path.join(PROJECT_ROOT, ".env")
CONFIG_PATH = LIVE_CONFIG_PATH
load_dotenv(DOTENV_PATH)



class _LazyDashboardStateReader:
    """Keep storage-only diagnostics available before the state DB exists."""

    def __init__(self) -> None:
        self._reader: StateReader | None = None
        self._lock = threading.Lock()

    def _get(self) -> StateReader:
        if self._reader is not None:
            return self._reader
        with self._lock:
            if self._reader is None:
                self._reader = SplitStateReader(
                    state_path=STATE_DB_PATH,
                    audit_path=AUDIT_DB_PATH,
                    research_path=RESEARCH_DB_PATH,
                )
            return self._reader

    def __getattr__(self, name: str):
        return getattr(self._get(), name)

    # Explicit forwarding keeps these high-risk operator/readiness seams
    # patchable in isolated tests without forcing the production database to
    # open merely while unittest resolves an attribute.
    def get_exchange_statement_entries(self, **filters):
        return self._get().get_exchange_statement_entries(**filters)

    def get_positions_for_current_mode(self):
        return self._get().get_positions_for_current_mode()

    def get_risk(self):
        return self._get().get_risk()

    def close(self) -> None:
        with self._lock:
            current = self._reader
            self._reader = None
        if current is not None:
            current.close()


reader = _LazyDashboardStateReader()
config_manager = ConfigManager(config_path=CONFIG_PATH)
admin_security = HTTPBasic()
viewer_security = HTTPBasic(auto_error=False)
TEMPLATE_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
_CANDIDATE_BPS_SENTINEL = 10_000.0


def _resolve_log_file_path() -> str:
    configured_path = str(os.getenv("BONGUS_LOG_PATH") or "").strip()
    if configured_path:
        return configured_path
    return os.path.join(PROJECT_ROOT, "scripts", "logs", "live_trader.log")


def _resolve_support_bundle_root() -> Path:
    configured_root = str(os.getenv("BONGUS_DATA_ROOT") or "").strip()
    return Path(configured_root or PROJECT_ROOT)


# Log file path for persistent logging
LOG_FILE = _resolve_log_file_path()
_RUNTIME_OFFLINE_MIN_SECONDS = 15.0
_RUNTIME_OFFLINE_GRACE_MULTIPLIER = 3.0
_STORAGE_WS_INTERVAL_SECONDS = 5.0
_DEGRADED_SUPPORT_BUNDLE_MAX_BYTES = 8_000_000
try:
    _storage_recovery_proof_max_age = float(
        os.getenv("BONGUS_STORAGE_HEALTH_MAX_AGE_SECONDS", "60") or "60"
    )
except ValueError:
    _storage_recovery_proof_max_age = 60.0
if (
    _storage_recovery_proof_max_age != _storage_recovery_proof_max_age
    or _storage_recovery_proof_max_age in {float("inf"), float("-inf")}
):
    _storage_recovery_proof_max_age = 60.0
_STORAGE_RECOVERY_PROOF_MAX_AGE_SECONDS = min(
    300.0,
    max(30.0, _storage_recovery_proof_max_age),
)


class _SupportBundleSizeLimitError(OSError):
    pass


class _BoundedSupportBundleWriter:
    """Seekable ZIP sink that refuses to allocate beyond the hard cap."""

    def __init__(self, raw: BinaryIO, max_bytes: int) -> None:
        self.raw = raw
        self.max_bytes = max(0, int(max_bytes))

    def write(self, data: bytes) -> int:
        end_position = self.raw.tell() + len(data)
        if end_position > self.max_bytes:
            raise _SupportBundleSizeLimitError(
                f"support bundle exceeds {self.max_bytes} bytes"
            )
        return self.raw.write(data)

    def tell(self) -> int:
        return self.raw.tell()

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        return self.raw.seek(offset, whence)

    def flush(self) -> None:
        self.raw.flush()


def _read_template(filename: str) -> str:
    return (TEMPLATE_DIR / filename).read_text(encoding="utf-8")


def _finite_float(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and parsed not in {float("inf"), float("-inf")} else None


def _normalize_candidate_bps(value, *, depth_usd):
    parsed = _finite_float(value)
    if parsed is None:
        return None
    depth = _finite_float(depth_usd) or 0.0
    if depth <= 0.0 and parsed >= _CANDIDATE_BPS_SENTINEL:
        return None
    return parsed


def _normalize_candidate_snapshot(snapshot: dict) -> dict:
    normalized = dict(snapshot)
    metrics = dict(normalized.get("metrics") or {})
    depth_usd = metrics.get("depth_usd")
    metrics["spread_bps"] = _normalize_candidate_bps(metrics.get("spread_bps"), depth_usd=depth_usd)
    if metrics.get("toxicity_available") is False:
        metrics["toxicity_bps"] = None
    else:
        metrics["toxicity_bps"] = _normalize_candidate_bps(metrics.get("toxicity_bps"), depth_usd=depth_usd)
    normalized["metrics"] = metrics
    return normalized


def _exact_json(value):
    """Preserve exact ledger decimals as strings in API responses."""

    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _exact_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_exact_json(item) for item in value]
    return value


def _parse_iso_timestamp(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _runtime_offline_threshold_seconds() -> float:
    configured = _finite_float(config_manager.get("max_runtime_staleness_seconds")) or 0.0
    return max(_RUNTIME_OFFLINE_MIN_SECONDS, configured * _RUNTIME_OFFLINE_GRACE_MULTIPLIER)


def _decorate_risk_snapshot(risk: dict) -> dict:
    snapshot = dict(risk)
    liveness_candidates = [
        _parse_iso_timestamp(snapshot.get("loop_last_alive_at")),
        _parse_iso_timestamp(snapshot.get("risk_last_evaluated_at")),
    ]
    last_alive = max((value for value in liveness_candidates if value is not None), default=None)
    if last_alive is None:
        runtime_freshness_seconds = 9_999.0
        runtime_offline = True
        snapshot["runtime_last_alive_at"] = ""
    else:
        runtime_freshness_seconds = max(
            0.0,
            (datetime.now(timezone.utc) - last_alive).total_seconds(),
        )
        runtime_offline = runtime_freshness_seconds > _runtime_offline_threshold_seconds()
        snapshot["runtime_last_alive_at"] = last_alive.isoformat()

    snapshot["runtime_freshness_seconds"] = runtime_freshness_seconds
    snapshot["runtime_offline"] = runtime_offline
    if runtime_offline:
        snapshot["telemetry_connected"] = False
        snapshot["execution_bridge_healthy"] = False
        snapshot["runtime_ready"] = False
        snapshot["allow_new_risk"] = False
        if not str(snapshot.get("entry_block_reason") or "").strip():
            if last_alive is not None:
                snapshot["entry_block_reason"] = (
                    f"runtime offline: last trader heartbeat {runtime_freshness_seconds:.0f}s ago"
                )
            else:
                snapshot["entry_block_reason"] = "runtime offline: no trader heartbeat recorded"
    return snapshot


def _storage_payload() -> dict[str, object]:
    """Read the operator snapshot without opening the dashboard StateReader."""

    try:
        return collect_storage_observability(Path(PROJECT_ROOT))
    except Exception as exc:
        # An observability failure must not turn the dashboard itself into a
        # restart loop. It also fails conservatively for entry admission.
        return {
            "schema_version": 1,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "available": False,
            "status": "unavailable",
            "error": f"storage observability failed: {type(exc).__name__}: {exc}",
            "snapshot_path": str(storage_snapshot_path(Path(PROJECT_ROOT))),
            "snapshot_size_bytes": None,
            "snapshot_modified_at": None,
            "snapshot": None,
            "database": {
                "available": False,
                "status": "unavailable",
                "error": "database metrics are unavailable",
            },
            "risk_increase_blocked": True,
        }


def _current_storage_snapshot() -> dict[str, object]:
    """Read only the bounded atomic health file for low-space decisions."""

    return read_storage_snapshot(storage_snapshot_path(Path(PROJECT_ROOT)))


def _validated_storage_recovery_proof(
    result: dict[str, object],
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """Return a fresh, complete operator-recovery proof or fail closed.

    Degraded storage, not just emergency storage, latches entry admission.  A
    recovery request is therefore valid whenever risk is still explicitly
    blocked and the guard proves its full healthy-sample, integrity, and
    exchange-reconciliation barrier.  This function never clears that latch;
    it only validates the authenticated request that the trader will consume.
    """

    snapshot = result.get("snapshot") if isinstance(result, dict) else None
    if result.get("available") is not True or not isinstance(snapshot, dict):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage recovery proof is unavailable.",
        )

    observed_text = str(snapshot.get("observed_at") or "").strip()
    try:
        observed_at = datetime.fromisoformat(observed_text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage recovery proof has an invalid timestamp.",
        ) from exc
    if observed_at.tzinfo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage recovery proof timestamp is not timezone-aware.",
        )
    current = now or datetime.now(timezone.utc)
    proof_age_seconds = (current - observed_at.astimezone(timezone.utc)).total_seconds()
    if not (-5.0 <= proof_age_seconds <= _STORAGE_RECOVERY_PROOF_MAX_AGE_SECONDS):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage recovery proof is stale.",
        )

    generation = snapshot.get("generation")
    healthy_samples = snapshot.get("healthy_recovery_samples")
    samples_required = snapshot.get("recovery_samples_required")
    active_faults = snapshot.get("active_faults")
    samples_proven = (
        isinstance(healthy_samples, int)
        and not isinstance(healthy_samples, bool)
        and isinstance(samples_required, int)
        and not isinstance(samples_required, bool)
        and samples_required > 0
        and healthy_samples >= samples_required
    )
    proof_complete = all(
        (
            isinstance(generation, int)
            and not isinstance(generation, bool)
            and generation > 0,
            snapshot.get("risk_increase_blocked") is True,
            snapshot.get("recovery_ready_for_operator") is True,
            str(snapshot.get("state") or "").lower() == "healthy",
            str(snapshot.get("instantaneous_state") or "").lower() == "healthy",
            samples_proven,
            snapshot.get("integrity_ok") is True,
            snapshot.get("exchange_reconciled") is True,
            isinstance(active_faults, list) and not active_faults,
        )
    )
    if not proof_complete:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Storage recovery prerequisites are not yet satisfied.",
        )
    return cast(dict[str, object], snapshot)


def _storage_ws_interval_seconds() -> float:
    try:
        configured = float(os.getenv("BONGUS_STORAGE_WS_INTERVAL_SECONDS", ""))
    except ValueError:
        configured = _STORAGE_WS_INTERVAL_SECONDS
    if configured != configured:
        configured = _STORAGE_WS_INTERVAL_SECONDS
    return min(60.0, max(0.25, configured or _STORAGE_WS_INTERVAL_SECONDS))


def _admin_username() -> str:
    return os.getenv("BONGUS_ADMIN_USERNAME", "").strip() or os.getenv("USERNAME", "").strip()


def _admin_auth_configured() -> bool:
    return bool(_admin_username()) and bool(
        os.getenv("BONGUS_ADMIN_PASSWORD", "").strip()
        or os.getenv("BONGUS_ADMIN_PASSWORD_SHA256", "").strip()
    )


def _admin_password_matches(password: str) -> bool:
    expected_hash = os.getenv("BONGUS_ADMIN_PASSWORD_SHA256", "").strip().lower()
    if expected_hash:
        digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return secrets.compare_digest(digest, expected_hash)
    expected_password = os.getenv("BONGUS_ADMIN_PASSWORD", "").strip()
    return bool(expected_password) and secrets.compare_digest(password, expected_password)


def _viewer_username() -> str:
    return os.getenv("BONGUS_VIEWER_USERNAME", "").strip() or _admin_username()


def _viewer_auth_configured() -> bool:
    viewer_username = os.getenv("BONGUS_VIEWER_USERNAME", "").strip()
    viewer_password_configured = bool(
        os.getenv("BONGUS_VIEWER_PASSWORD", "").strip()
        or os.getenv("BONGUS_VIEWER_PASSWORD_SHA256", "").strip()
    )
    return bool(viewer_username and viewer_password_configured) or _admin_auth_configured()


def _viewer_password_matches(password: str) -> bool:
    expected_hash = os.getenv("BONGUS_VIEWER_PASSWORD_SHA256", "").strip().lower()
    if expected_hash:
        digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return secrets.compare_digest(digest, expected_hash)
    expected_password = os.getenv("BONGUS_VIEWER_PASSWORD", "").strip()
    if expected_password:
        return secrets.compare_digest(password, expected_password)
    return False


def _viewer_credentials_match(username: str, password: str) -> bool:
    explicit_viewer_username = os.getenv("BONGUS_VIEWER_USERNAME", "").strip()
    viewer_match = bool(explicit_viewer_username) and (
        secrets.compare_digest(username, explicit_viewer_username)
        and _viewer_password_matches(password)
    )
    # An administrator must always be able to read the state they administer,
    # even when a lower-privilege viewer credential is configured separately.
    admin_match = _admin_auth_configured() and (
        secrets.compare_digest(username, _admin_username())
        and _admin_password_matches(password)
    )
    return viewer_match or admin_match


def require_viewer(
    credentials: HTTPBasicCredentials | None = Depends(viewer_security),
) -> str:
    if not _viewer_auth_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Dashboard viewer credentials are not configured on the server.",
        )
    if credentials is None or not _viewer_credentials_match(
        credentials.username,
        credentials.password,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid dashboard credentials.",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def _websocket_viewer_authorized(websocket: WebSocket) -> bool:
    """Authenticate WebSocket handshakes with the same HTTP Basic realm.

    Browsers reuse the credentials supplied for the protected dashboard page
    on same-origin WebSocket handshakes.  Missing or malformed headers fail
    closed before the socket is accepted, so raw telemetry and logs are never
    exposed merely because the bind address was changed.
    """

    authorization = str(websocket.headers.get("authorization") or "")
    scheme, _, encoded = authorization.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return False
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
        username, separator, password = decoded.partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return bool(separator) and _viewer_credentials_match(username, password)


def require_viewer_connection(connection: HTTPConnection) -> str:
    """Apply viewer auth globally without sending WebSockets through HTTPBasic.

    FastAPI applies app-level dependencies to both HTTP and WebSocket routes,
    while ``HTTPBasic`` accepts only an HTTP Request. WebSocket endpoints do
    their equivalent header validation immediately before accepting the socket.
    """

    if connection.scope.get("type") == "websocket":
        return ""
    if not _viewer_auth_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Dashboard viewer credentials are not configured on the server.",
        )
    authorization = str(connection.headers.get("authorization") or "")
    scheme, _, encoded = authorization.partition(" ")
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
        username, separator, password = decoded.partition(":")
    except (ValueError, UnicodeDecodeError):
        username, separator, password = "", "", ""
    if scheme.lower() != "basic" or not separator or not _viewer_credentials_match(username, password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid dashboard credentials.",
            headers={"WWW-Authenticate": "Basic"},
        )
    return username


def require_admin(credentials: HTTPBasicCredentials = Depends(admin_security)) -> str:
    if not _admin_auth_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin credentials are not configured on the server.",
        )
    username = _admin_username()
    if not (
        secrets.compare_digest(credentials.username, username)
        and _admin_password_matches(credentials.password)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin credentials.",
            headers={"WWW-Authenticate": "Basic"},
        )
    return username

async def consume_tcp_stream():
    """Background task: Reads from Rust IPC and broadcasts to all WebSocket clients."""
    client = TelemetryClient(host='127.0.0.1', port=9000)
    print("FastAPI attempting to connect to Rust Engine IPC via TelemetryClient.")

    async for event in client.stream_events():
        if event is None:
            continue
        try:
            msg = json.dumps(event)
        except Exception as e:
            print(f"Failed to json serialize telemetry event: {e}", flush=True)
            continue

        disconnected_clients: set[WebSocket] = set()
        for connection in list(active_connections):
            try:
                await connection.send_text(msg)
            except Exception:
                disconnected_clients.add(connection)

        active_connections.difference_update(disconnected_clients)

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(consume_tcp_stream())
    try:
        yield
    finally:
        task.cancel()
        try:
            with suppress(asyncio.CancelledError):
                await task
        finally:
            reader.close()

app = FastAPI(
    title="Bongus Web Dashboard",
    lifespan=lifespan,
    dependencies=[Depends(require_viewer_connection)],
)


# ── REST API Endpoints ──────────────────────────────────────────────────────

@app.get("/api/positions")
async def api_positions():
    return reader.get_positions_for_current_mode()

@app.get("/api/stats")
async def api_stats():
    stats = reader.get_stats()
    stats.update(calculate_metrics(reader._get(), config=config_manager))
    stats.update(reader.get_open_pnl_summary())
    # Position count is operator-facing state, so derive it from the live positions
    # table instead of waiting for the trader's heartbeat cache to refresh.
    stats["open_positions"] = float(len(reader.get_positions_for_current_mode()))
    return stats

@app.get("/api/trades")
async def api_trades(limit: int = Query(50, ge=1, le=500)):
    return reader.get_trades(limit, session_scoped=False)

@app.get("/api/risk")
async def api_risk():
    return _decorate_risk_snapshot(reader.get_risk())

@app.get("/api/pnl-attribution")
async def api_pnl_attribution():
    return reader.get_pnl_attribution(session_scoped=False)

@app.get("/api/execution-events")
async def api_execution_events(limit: int = Query(100, ge=1, le=500)):
    return reader.get_execution_events(limit, session_scoped=False)


@app.get("/api/economic-ledger")
async def api_economic_ledger(
    limit: int = Query(100, ge=1, le=1000),
    symbol: str | None = None,
    cycle_id: str | None = None,
):
    return reader.get_economic_ledger_events(
        limit=limit,
        symbol=symbol,
        cycle_id=cycle_id,
    )


@app.get("/api/exchange-statements")
async def api_exchange_statements(
    limit: int = Query(100, ge=1, le=1000),
    statement_type: str | None = None,
    reconciliation_status: str | None = None,
):
    return reader.get_exchange_statement_entries(
        statement_type=statement_type,
        reconciliation_status=reconciliation_status,
        limit=limit,
        descending=True,
    )


@app.get("/api/economic-projection")
async def api_economic_projection(
    symbol: str | None = None,
    cycle_id: str | None = None,
):
    risk = reader.get_risk()
    projection = reader.project_economic_ledger(
        trading_mode=str(risk.get("trading_mode") or "") or None,
        symbol=symbol,
        cycle_id=cycle_id,
    )
    return _exact_json(projection)


@app.get("/api/daily-report")
async def api_daily_report():
    now = datetime.now(timezone.utc)
    risk = reader.get_risk()
    stats = reader.get_stats()
    open_incidents = 0
    critical_incidents = 0
    incident_table = reader.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='supervisor_incidents'"
    ).fetchone()
    if incident_table is not None:
        row = reader.conn.execute(
            """SELECT COUNT(*),
                      SUM(CASE WHEN severity='CRITICAL' THEN 1 ELSE 0 END)
               FROM supervisor_incidents WHERE state != 'RESOLVED'"""
        ).fetchone()
        open_incidents = int(row[0] or 0)
        critical_incidents = int(row[1] or 0)
    report = build_reconciled_daily_report(
        reader.conn,
        start_time=now - timedelta(days=1),
        end_time=now,
        reconciliation_matched=bool(risk.get("economic_ledger_reconciled", False)),
        reserved_capital_usd=stats.get("reserved_capital_usd", 0.0),
        account_equity_usd=stats.get("account_equity", 0.0),
        open_incidents=open_incidents,
        critical_incidents=critical_incidents,
        trading_mode=str(risk.get("trading_mode") or "") or None,
    )
    return _exact_json(report)


@app.get("/api/metrics")
async def api_metrics():
    return calculate_metrics(reader._get(), config=config_manager)


@app.get("/api/validation")
async def api_validation(limit: int = Query(24, ge=1, le=500)):
    return {
        "current": calculate_metrics(reader._get(), config=config_manager),
        "latest_snapshot": reader.get_latest_validation_snapshot(),
        "history": reader.get_validation_snapshots(limit=limit),
    }


@app.get("/api/candidates")
async def api_candidates(limit: int = Query(200, ge=1, le=1000)):
    return [_normalize_candidate_snapshot(row) for row in reader.get_candidate_snapshots(limit=limit)]


@app.get("/api/opportunity-scores")
async def api_opportunity_scores(limit: int = Query(50, ge=1, le=500)):
    return reader.get_opportunity_scores(limit=limit)


@app.get("/api/execution-quality")
async def api_execution_quality(limit: int = Query(100, ge=1, le=500)):
    return reader.get_execution_quality(limit=limit)


@app.get("/api/shadow-decisions")
async def api_shadow_decisions(limit: int = Query(100, ge=1, le=500)):
    return reader.get_shadow_decisions(limit=limit)


@app.get("/api/promotions")
async def api_promotions(limit: int = Query(50, ge=1, le=200)):
    return reader.get_parameter_promotions(limit=limit)


@app.get("/api/validation-snapshots")
async def api_validation_snapshots(limit: int = Query(50, ge=1, le=200)):
    return reader.get_validation_snapshots(limit=limit)


@app.get("/api/health")
async def api_health(limit: int = Query(100, ge=1, le=500)):
    return reader.get_health_samples(limit=limit)


@app.get("/api/storage")
async def api_storage():
    """Return the atomic storage state plus strictly read-only file metrics."""

    return await asyncio.to_thread(_storage_payload)


# ── Dashboard HTML ──────────────────────────────────────────────────────────

HTML_CONTENT = _read_template("web_dashboard.html")
EXPLAIN_HTML = _read_template("web_dashboard_explain.html")
ADMIN_HTML = _read_template("web_dashboard_admin.html")

@app.get("/")
async def get_dashboard():
    return HTMLResponse(HTML_CONTENT)


@app.get("/admin")
async def get_admin(_admin_user: str = Depends(require_admin)):
    return HTMLResponse(ADMIN_HTML)


@app.get("/explain")
async def get_explain():
    return HTMLResponse(EXPLAIN_HTML)


@app.post("/api/admin/flatten-all")
async def api_admin_flatten_all(admin_user: str = Depends(require_admin)):
    open_positions = reader.get_positions_for_current_mode()
    request_id = uuid.uuid4().hex[:12]
    now_iso = datetime.now(timezone.utc).isoformat()
    config_manager.apply_updates(
        {
            "pause_new_entries": True,
            "operator_flatten_all_request_id": request_id,
            "operator_flatten_all_requested_at": now_iso,
            "operator_flatten_all_requested_by": admin_user,
        }
    )
    return {
        "request_id": request_id,
        "status": "requested",
        "paused_new_entries": True,
        "requested_at": now_iso,
        "open_position_count": len(open_positions),
    }


@app.post("/api/admin/storage/acknowledge-recovery")
async def api_admin_acknowledge_storage_recovery(
    admin_user: str = Depends(require_admin),
):
    """Request recovery only after the atomic guard proves every prerequisite.

    This endpoint never clears either process's latch directly.  It writes a
    hash-covered operator request which the trader consumes, sends through the
    Rust CONFIG_SYNC/ACK barrier, and only then clears the local durable latch.
    """

    result = await asyncio.to_thread(_current_storage_snapshot)
    snapshot = _validated_storage_recovery_proof(result)

    request_id = f"storage-recovery-{uuid.uuid4().hex[:12]}"
    requested_at = datetime.now(timezone.utc).isoformat()
    config_manager.apply_updates(
        {
            "storage_recovery_request_id": request_id,
            "storage_recovery_requested_at": requested_at,
            "storage_recovery_requested_by": admin_user,
        }
    )
    return {
        "request_id": request_id,
        "status": "requested",
        "requested_at": requested_at,
        "requested_by": admin_user,
        "snapshot_generation": snapshot.get("generation"),
    }

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    if not _websocket_viewer_authorized(websocket):
        await websocket.close(code=4401, reason="dashboard authentication required")
        return
    await websocket.accept()
    active_connections.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        active_connections.discard(websocket)
    except Exception:
        active_connections.discard(websocket)


@app.websocket("/ws/storage")
async def websocket_storage(websocket: WebSocket):
    """Publish bounded storage snapshots to authenticated dashboard viewers."""

    if not _websocket_viewer_authorized(websocket):
        await websocket.close(code=4401, reason="dashboard authentication required")
        return
    await websocket.accept()
    try:
        while True:
            payload = await asyncio.to_thread(_storage_payload)
            await websocket.send_json(payload)
            await asyncio.sleep(_storage_ws_interval_seconds())
    except WebSocketDisconnect:
        pass
    except Exception:
        # The socket may disappear between polling and sending. The next
        # authenticated connection receives a fresh atomic snapshot.
        pass


# ── Log Viewer ─────────────────────────────────────────────────────────────

LOGS_HTML = _read_template("web_dashboard_logs.html")

@app.get("/logs")
async def get_logs():
    return HTMLResponse(LOGS_HTML)


@app.get("/api/logs/download")
async def download_logs():
    """Download current and retained startup diagnostics as a ZIP archive."""

    try:
        storage_snapshot = await asyncio.to_thread(_current_storage_snapshot)
        degraded = storage_is_degraded(storage_snapshot)
    except Exception:
        # Unknown storage health is not permission to build the larger bundle.
        degraded = True
    content_cap = (
        _DEGRADED_SUPPORT_BUNDLE_MAX_BYTES
        if degraded
        else DEFAULT_SUPPORT_BUNDLE_MAX_BYTES
    )
    bundle = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
    bounded_bundle = _BoundedSupportBundleWriter(
        cast(BinaryIO, bundle),
        DEFAULT_SUPPORT_BUNDLE_MAX_BYTES,
    )
    try:
        write_support_bundle(
            cast(BinaryIO, bounded_bundle),
            _resolve_support_bundle_root(),
            max_uncompressed_bytes=content_cap,
            degraded=degraded,
        )
        bundle.seek(0, os.SEEK_END)
        bundle_bytes = bundle.tell()
        if bundle_bytes > DEFAULT_SUPPORT_BUNDLE_MAX_BYTES:
            raise HTTPException(
                status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
                detail="Support bundle exceeded the 64 MB download cap.",
            )
        bundle.seek(0)
    except _SupportBundleSizeLimitError as exc:
        bundle.close()
        raise HTTPException(
            status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
            detail="Support bundle exceeded the 64 MB download cap.",
        ) from exc
    except Exception:
        bundle.close()
        raise
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return StreamingResponse(
        iter(lambda: bundle.read(64 * 1024), b""),
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="bongus-logs-{timestamp}.zip"'
            ),
            "Cache-Control": "no-store",
            "Content-Length": str(bundle_bytes),
            "X-Bongus-Support-Bundle-Mode": "degraded" if degraded else "normal",
        },
        background=BackgroundTask(bundle.close),
    )


@app.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket):
    """Stream persistent log file to the browser."""
    if not _websocket_viewer_authorized(websocket):
        await websocket.close(code=4401, reason="dashboard authentication required")
        return
    await websocket.accept()

    # Send initial history from persistent log file
    initial_lines_sent = False
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
                # Send up to 2000 lines of history (roughly 5MB of logs)
                for line in lines[-2000:]:
                    line = line.strip()
                    if line:
                        await websocket.send_text(line)
                initial_lines_sent = True
        except Exception as e:
            await websocket.send_text(f"[log viewer] Could not read log file: {e}")
    
    if not initial_lines_sent:
        await websocket.send_text("[log viewer] No persistent log found — waiting for new entries...")

    # Stream new lines by polling the log file
    last_file_size = os.path.getsize(LOG_FILE) if os.path.exists(LOG_FILE) else 0
    last_file_identity = None
    if os.path.exists(LOG_FILE):
        initial_stat = os.stat(LOG_FILE)
        last_file_identity = (initial_stat.st_dev, initial_stat.st_ino)
    try:
        while True:
            # A send-only polling loop cannot observe a browser that closes
            # while the log file is idle. That leaves the ASGI handler alive
            # indefinitely and prevents Uvicorn/systemd from completing a
            # graceful stop. Poll the receive side with the same one-second
            # cadence so disconnect frames terminate the handler promptly.
            try:
                message = await asyncio.wait_for(websocket.receive(), timeout=1.0)
            except asyncio.TimeoutError:
                message = None
            if message is not None and message.get("type") == "websocket.disconnect":
                return
            if not os.path.exists(LOG_FILE):
                continue
            try:
                current_stat = os.stat(LOG_FILE)
                current_size = current_stat.st_size
                current_identity = (current_stat.st_dev, current_stat.st_ino)
                if (
                    current_identity != last_file_identity
                    or current_size < last_file_size
                ):
                    # Size-based rotation replaces/truncates the active file.
                    # Resume at byte zero instead of waiting for the new file
                    # to grow past the old file's size.
                    last_file_size = 0
                if current_size > last_file_size:
                    with open(LOG_FILE, "r", encoding="utf-8") as f:
                        f.seek(last_file_size)
                        new_lines = f.readlines()
                    for line in new_lines:
                        line = line.strip()
                        if line:
                            await websocket.send_text(line)
                    last_file_size = current_size
                last_file_identity = current_identity
            except Exception:
                pass
    except WebSocketDisconnect:
        pass
    except Exception:
        pass


if __name__ == "__main__":
    import uvicorn
    host = os.getenv("DASHBOARD_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.getenv("DASHBOARD_PORT", "8080").strip() or "8080")
    uvicorn.run(app, host=host, port=port)
