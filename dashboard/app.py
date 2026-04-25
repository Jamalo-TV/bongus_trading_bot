from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from core.state import EMERGENCY_HALT_EVENT, flatten_all_positions, METRICS
import asyncio
import time

app = FastAPI(title="Bongus Overhaul Dashboard")

@app.get("/", response_class=HTMLResponse)
async def index():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Bongus Architecture Overhaul</title>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: #0f172a; color: #f8fafc; padding: 2rem; }
            .container { max-width: 800px; margin: 0 auto; }
            .card { background: #1e293b; border-radius: 12px; padding: 1.5rem; margin-bottom: 1.5rem; border: 1px solid #334155; }
            .status-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 1rem; }
            .metric { font-size: 0.875rem; color: #94a3b8; }
            .value { font-size: 1.25rem; font-weight: bold; color: #38bdf8; }
            .panic-btn { background: #ef4444; color: white; border: none; padding: 1rem 2rem; border-radius: 8px; font-weight: bold; cursor: pointer; width: 100%; font-size: 1.25rem; transition: background 0.2s; }
            .panic-btn:hover { background: #dc2626; }
            .panic-btn:disabled { background: #450a0a; cursor: not-allowed; }
            .halted { color: #ef4444; border-color: #ef4444; }
            h1 { margin-top: 0; color: #f1f5f9; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Bongus Control Center</h1>
            
            <div id="status-card" class="card">
                <div class="status-grid">
                    <div class="metric">System Status: <span id="sys-status" class="value">ACTIVE</span></div>
                    <div class="metric">Cython Math Latency: <span id="math-latency" class="value">0.00ms</span></div>
                    <div class="metric">Risk Checks Passed: <span id="risk-count" class="value">0</span></div>
                    <div class="metric">ML Model Status: <span id="ml-status" class="value">Disconnected</span></div>
                </div>
            </div>

            <div class="card">
                <h3>Emergency Kill Switch</h3>
                <button id="panic-btn" class="panic-btn" onclick="triggerPanic()">TRAPDOOR / FLATTEN ALL</button>
            </div>
        </div>

        <script>
            async function updateStatus() {
                try {
                    const res = await fetch('/api/v1/status');
                    const data = await res.json();
                    
                    document.getElementById('sys-status').innerText = data.halted ? 'HALTED' : 'ACTIVE';
                    document.getElementById('sys-status').style.color = data.halted ? '#ef4444' : '#10b981';
                    document.getElementById('math-latency').innerText = data.metrics.last_ewma_latency_ms.toFixed(4) + 'ms';
                    document.getElementById('risk-count').innerText = data.metrics.total_orders_validated;
                    document.getElementById('ml-status').innerText = data.metrics.model_loaded ? 'LOADED' : 'PENDING';
                    
                    if (data.halted) {
                        document.getElementById('panic-btn').disabled = true;
                        document.getElementById('panic-btn').innerText = 'SYSTEM HALTED';
                        document.getElementById('status-card').classList.add('halted');
                    }
                } catch (e) { console.error(e); }
            }

            async function triggerPanic() {
                if (!confirm("Confirm EMERGENCY FLATTEN? This will close ALL positions.")) return;
                const res = await fetch('/api/v1/panic', { method: 'POST' });
                if (res.ok) updateStatus();
            }

            setInterval(updateStatus, 1000);
            updateStatus();
        </script>
    </body>
    </html>
    """

@app.get("/api/v1/status")
async def status():
    return {
        "halted": EMERGENCY_HALT_EVENT.is_set(),
        "metrics": METRICS
    }

@app.post("/api/v1/panic")
async def panic():
    EMERGENCY_HALT_EVENT.set()
    asyncio.create_task(flatten_all_positions())
    return {"status": "HALTED"}
