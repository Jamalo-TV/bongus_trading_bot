import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

if __package__ in {None, ""}:
    _BOOTSTRAP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if _BOOTSTRAP_ROOT not in sys.path:
        sys.path.insert(0, _BOOTSTRAP_ROOT)

from bongus.engine.state_store import StateReader
from bongus.ipc.telemetry import TelemetryClient
from bongus.monitoring.performance_metrics import calculate_metrics

active_connections: set[WebSocket] = set()

reader = StateReader()
TEMPLATE_DIR = Path(os.path.dirname(os.path.abspath(__file__)))

# Log file path for persistent logging
SCRIPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts")
LOG_FILE = os.path.join(SCRIPT_DIR, "logs", "live_trader.log")


def _read_template(filename: str) -> str:
    return (TEMPLATE_DIR / filename).read_text(encoding="utf-8")

async def consume_tcp_stream():
    """Background task: Reads from Rust IPC and broadcasts to all WebSocket clients."""
    client = TelemetryClient(host='127.0.0.1', port=9000)
    print("FastAPI attempting to connect to Rust Engine IPC via TelemetryClient.")

    async for event in client.stream_events():
        if event is None:
            continue
        msg = json.dumps(event)
        disconnected_clients: set[WebSocket] = set()
        for connection in active_connections:
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

app = FastAPI(title="Bongus Web Dashboard", lifespan=lifespan)


# ── REST API Endpoints ──────────────────────────────────────────────────────

@app.get("/api/positions")
async def api_positions():
    return reader.get_positions()

@app.get("/api/stats")
async def api_stats():
    stats = reader.get_stats()
    stats.update(calculate_metrics(reader))
    # Position count is operator-facing state, so derive it from the live positions
    # table instead of waiting for the trader's heartbeat cache to refresh.
    stats["open_positions"] = float(len(reader.get_positions()))
    return stats

@app.get("/api/trades")
async def api_trades(limit: int = Query(50, ge=1, le=500)):
    return reader.get_trades(limit)

@app.get("/api/risk")
async def api_risk():
    return reader.get_risk()

@app.get("/api/pnl-attribution")
async def api_pnl_attribution():
    return reader.get_pnl_attribution()

@app.get("/api/execution-events")
async def api_execution_events(limit: int = Query(100, ge=1, le=500)):
    return reader.get_execution_events(limit)


@app.get("/api/metrics")
async def api_metrics():
    return calculate_metrics(reader)


@app.get("/api/validation")
async def api_validation(limit: int = Query(24, ge=1, le=500)):
    return {
        "current": calculate_metrics(reader),
        "latest_snapshot": reader.get_latest_validation_snapshot(),
        "history": reader.get_validation_snapshots(limit=limit),
    }


@app.get("/api/candidates")
async def api_candidates(limit: int = Query(200, ge=1, le=1000)):
    return reader.get_candidate_snapshots(limit=limit)


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

@app.get("/")
async def get_dashboard():
    return HTMLResponse(HTML_CONTENT)

@app.get("/explain")
async def get_explain():
    return HTMLResponse(EXPLAIN_HTML)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
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
    host = os.getenv("DASHBOARD_HOST", "0.0.0.0").strip() or "0.0.0.0"
    port = int(os.getenv("DASHBOARD_PORT", "8080").strip() or "8080")
    uvicorn.run(app, host=host, port=port)
