import asyncio
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from bongus.ipc import ExecutionClient

logger = logging.getLogger(__name__)

active_connections = set()

async def consume_tcp_stream():
    """Background task: Reads from Rust IPC and broadcasts to all WebSocket clients."""
    async for event in ExecutionClient.listen_events():
        msg = json.dumps(event)
        disconnected_clients = set()
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

HTML_CONTENT = """
<!DOCTYPE html>
<html>
<head>
    <title>Bongus Multi-Asset Arbitrage Orchestrator</title>
    <style>
        body { background: #111827; color: #fff; font-family: sans-serif; padding: 20px; }
        table { border-collapse: collapse; width: 100%; margin-top: 10px; }
        th, td { padding: 10px; text-align: left; border-bottom: 1px solid #374151; }
        th { color: #9ca3af; font-weight: normal; }
        .status { margin-top: 30px; color: #9ca3af; font-size: 0.9em; }
    </style>
</head>
<body>
    <div style="background: #1f2937; padding: 20px; border-radius: 8px; margin-bottom: 20px;">
        <h1 style="margin: 0; color: #e0e0e0;">Total PnL: <span id="total-pnl" style="color: #4ade80;">$0.00</span></h1>
    </div>

    <details style="margin-bottom: 20px; cursor: pointer;" open>
        <summary style="font-size: 1.2em; color: #60a5fa; outline: none;">Detailed Asset PnL</summary>
        <table>
            <thead><tr><th>Asset</th><th>Current Balance</th><th>Calculated PnL</th></tr></thead>
            <tbody id="pnl-details"></tbody>
        </table>
    </details>
    
    <div class="status" id="ws-status">Connecting to engine...</div>

    <script>
        let initialBalances = null;
        let currentBalances = {};
        
        const protocol = window.location.protocol === "https:" ? "wss://" : "ws://";
        const ws = new WebSocket(protocol + window.location.host + "/ws");

        ws.onopen = () => { document.getElementById("ws-status").innerText = "● Live execution stream active"; };
        ws.onclose = () => { document.getElementById("ws-status").innerText = "○ Disconnected from stream"; };

        ws.onmessage = (event) => {
            const data = JSON.parse(event.data);
            
            if (data.event === "AccountUpdate") {
                currentBalances = data.balances;
                
                if (!initialBalances) {
                    initialBalances = JSON.parse(JSON.stringify(data.balances));
                }

                let totalPnl = 0.0;
                let pnlHtml = "";

                for (const [asset, bal] of Object.entries(currentBalances)) {
                    let startBal = initialBalances[asset] || 0.0;
                    let diff = bal - startBal;
                    
                    if (asset === "USDT") { totalPnl += diff; } 

                    let color = diff >= 0 ? "#4ade80" : "#f87171";
                    let sign = diff > 0 ? '+' : '';
                    pnlHtml += `<tr>
                        <td>${asset}</td>
                        <td>${bal.toFixed(4)}</td>
                        <td style="color: ${color}">${sign}${diff.toFixed(4)}</td>
                    </tr>`;
                }

                let totalColor = totalPnl >= 0 ? "#4ade80" : "#f87171";
                let totalSign = totalPnl > 0 ? '+' : '';
                document.getElementById("total-pnl").innerText = `${totalSign}$${totalPnl.toFixed(2)}`;
                document.getElementById("total-pnl").style.color = totalColor;
                document.getElementById("pnl-details").innerHTML = pnlHtml;
            }
        };
    </script>
</body>
</html>
"""

@app.get("/")
async def get_dashboard():
    return HTMLResponse(HTML_CONTENT)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_connections.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        active_connections.remove(websocket)
