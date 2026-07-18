import asyncio
import base64
import hashlib
import json
import os
import secrets
import sys
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.requests import HTTPConnection

if __package__ in {None, ""}:
    _BOOTSTRAP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if _BOOTSTRAP_ROOT not in sys.path:
        sys.path.insert(0, _BOOTSTRAP_ROOT)

from bongus.core.config_manager import ConfigManager
from bongus.engine.state_store import StateReader
from bongus.ipc.telemetry import TelemetryClient
from bongus.monitoring.performance_metrics import calculate_metrics
from bongus.supervisor.daily_report import build_reconciled_daily_report

active_connections: set[WebSocket] = set()

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DOTENV_PATH = os.path.join(PROJECT_ROOT, ".env")
CONFIG_PATH = os.path.join(PROJECT_ROOT, "live_config.json")
load_dotenv(DOTENV_PATH)

reader = StateReader()
config_manager = ConfigManager(config_path=CONFIG_PATH)
admin_security = HTTPBasic()
viewer_security = HTTPBasic(auto_error=False)
TEMPLATE_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
_CANDIDATE_BPS_SENTINEL = 10_000.0

# Log file path for persistent logging
LOG_FILE = os.path.join(PROJECT_ROOT, "scripts", "logs", "live_trader.log")
_RUNTIME_OFFLINE_MIN_SECONDS = 15.0
_RUNTIME_OFFLINE_GRACE_MULTIPLIER = 3.0


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
    yield
    task.cancel()
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
    stats.update(calculate_metrics(reader, config=config_manager))
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
    return calculate_metrics(reader, config=config_manager)


@app.get("/api/validation")
async def api_validation(limit: int = Query(24, ge=1, le=500)):
    return {
        "current": calculate_metrics(reader, config=config_manager),
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


# ── Log Viewer ─────────────────────────────────────────────────────────────

LOGS_HTML = _read_template("web_dashboard_logs.html")

@app.get("/logs")
async def get_logs():
    return HTMLResponse(LOGS_HTML)


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
    try:
        while True:
            await asyncio.sleep(1)
            if not os.path.exists(LOG_FILE):
                continue
            try:
                current_size = os.path.getsize(LOG_FILE)
                if current_size > last_file_size:
                    with open(LOG_FILE, "r", encoding="utf-8") as f:
                        f.seek(last_file_size)
                        new_lines = f.readlines()
                    for line in new_lines:
                        line = line.strip()
                        if line:
                            await websocket.send_text(line)
                    last_file_size = current_size
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
