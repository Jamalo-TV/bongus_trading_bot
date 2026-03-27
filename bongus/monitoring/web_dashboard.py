import asyncio
import json
import os
import subprocess
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from bongus.engine.state_store import StateReader
from bongus.ipc.telemetry import TelemetryClient

active_connections: set[WebSocket] = set()
reader = StateReader()

# Log file path for persistent logging
SCRIPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts")
LOG_FILE = os.path.join(SCRIPT_DIR, "logs", "live_trader.log")

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
    return reader.get_stats()

@app.get("/api/trades")
async def api_trades(limit: int = Query(50, ge=1, le=500)):
    return reader.get_trades(limit)

@app.get("/api/risk")
async def api_risk():
    return reader.get_risk()

@app.get("/api/pnl-attribution")
async def api_pnl_attribution():
    return reader.get_pnl_attribution()


@app.post("/api/kill-switch")
async def api_kill_switch():
    from bongus.engine.state_store import StateWriter
    w = StateWriter()
    w.set_risk("kill_switch", "true")
    w.set_risk("allow_new_risk", "false")
    w.close()
    return {"status": "kill_switch_activated"}


# ── Dashboard HTML ──────────────────────────────────────────────────────────

HTML_CONTENT = """
<!DOCTYPE html>
<html class="dark" lang="en">
<head>
<meta charset="utf-8"/>
<meta content="width=device-width, initial-scale=1.0" name="viewport"/>
<title>BONGUS ARB | Delta-Neutral</title>
<script src="https://cdn.tailwindcss.com?plugins=forms,container-queries"></script>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet"/>
<link href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:wght,FILL@100..700,0..1&display=swap" rel="stylesheet"/>
<style>
    body { font-family: 'Inter', sans-serif; background-color: #11131c; color: #e1e1ef; }
    .mono { font-family: 'JetBrains Mono', monospace; }
    .material-symbols-outlined { font-variation-settings: 'FILL' 0, 'wght' 400, 'GRAD' 0, 'opsz' 24; vertical-align: middle; font-size: 18px; }
    ::-webkit-scrollbar { width: 4px; height: 4px; }
    ::-webkit-scrollbar-track { background: #0c0e17; }
    ::-webkit-scrollbar-thumb { background: #3b494c; }
    .glow-green { text-shadow: 0 0 8px rgba(181,255,170,0.4); }
    .glow-red { text-shadow: 0 0 8px rgba(255,193,197,0.4); }
    #kill-switch-modal { display: none; }
    #kill-switch-modal.show { display: flex; }
</style>
<script id="tailwind-config">
    tailwind.config = {
        darkMode: "class",
        theme: { extend: {
            colors: {
                "surface-container": "#1d1f29", "surface-container-low": "#191b24",
                "primary-fixed-dim": "#00e639", "on-surface-variant": "#bac9cc",
                "surface": "#11131c", "surface-container-highest": "#32343e",
                "on-secondary": "#00363d", "surface-container-high": "#282933",
                "background": "#11131c", "tertiary-container": "#ffc1c5",
                "secondary": "#bdf4ff", "on-background": "#e1e1ef",
                "surface-container-lowest": "#0c0e17", "on-error-container": "#ffdad6",
                "on-primary-container": "#006814", "on-primary": "#003907",
                "outline-variant": "#3b494c", "error-container": "#93000a",
                "primary-container": "#00f13d", "on-surface": "#e1e1ef",
                "surface-bright": "#373943", "tertiary": "#ffe7e8",
                "error": "#ffb4ab", "outline": "#849396", "surface-dim": "#11131c",
                "surface-variant": "#32343e", "primary": "#b5ffaa",
                "secondary-fixed-dim": "#00daf3", "tertiary-fixed-dim": "#ffb2b8"
            },
            fontFamily: { "headline": ["Inter"], "body": ["Inter"], "label": ["Inter"] },
            borderRadius: {"DEFAULT": "0px", "lg": "0px", "xl": "0px", "full": "9999px"},
        }}
    }
</script>
</head>
<body class="bg-background text-on-surface overflow-hidden">

<!-- Kill Switch Confirmation Modal -->
<div id="kill-switch-modal" class="fixed inset-0 z-[100] items-center justify-center bg-black/80">
    <div class="bg-surface-container-highest border border-error/40 p-6 max-w-sm w-full mx-4">
        <div class="flex items-center gap-3 mb-4">
            <span class="material-symbols-outlined text-error" style="font-size:24px">warning</span>
            <h2 class="text-sm font-black text-error uppercase tracking-widest">Confirm Kill Switch</h2>
        </div>
        <p class="text-[0.75rem] text-on-surface-variant mb-6">This will immediately block all new positions. Existing positions will remain open. Are you sure?</p>
        <div class="flex gap-3">
            <button id="ks-cancel" class="flex-1 py-2 text-[0.6875rem] font-bold uppercase tracking-widest border border-outline-variant/40 text-outline hover:text-on-surface transition-colors">Cancel</button>
            <button id="ks-confirm" class="flex-1 py-2 text-[0.6875rem] font-bold uppercase tracking-widest bg-error/20 border border-error/40 text-error hover:bg-error/30 transition-colors">Confirm</button>
        </div>
    </div>
</div>

<!-- Header -->
<header class="fixed top-0 left-0 w-full z-50 flex justify-between items-center px-6 h-12 bg-[#11131c] border-b border-outline-variant/20">
    <div class="flex items-center gap-4">
        <span class="text-base font-black tracking-tight text-primary">BONGUS ARB</span>
        <span id="mode-badge" class="px-2 py-0.5 text-[0.625rem] font-black tracking-widest uppercase border bg-surface-container text-outline border-outline-variant/30">--</span>
        <div id="uptime-pill" class="hidden md:flex items-center gap-2 px-2 py-0.5 bg-surface-container-lowest border border-outline-variant/20">
            <span class="material-symbols-outlined text-outline" style="font-size:12px">timer</span>
            <span id="uptime-display" class="text-[0.625rem] mono text-outline">00:00:00</span>
        </div>
    </div>
    <div class="flex items-center gap-3">
        <div class="flex items-center gap-2 px-3 py-1 bg-surface-container-lowest border border-outline-variant/30">
            <span id="ws-dot" class="flex h-2 w-2 rounded-full bg-outline"></span>
            <span id="ws-status-text" class="text-[0.65rem] font-bold text-outline tracking-widest uppercase">CONNECTING</span>
            <span id="latency-display" class="text-[0.625rem] text-outline ml-2 border-l border-outline-variant/30 pl-2 uppercase mono">--ms</span>
        </div>
        <button id="kill-switch-btn" class="flex items-center gap-2 px-3 h-8 text-[0.6875rem] font-bold tracking-tight border border-error/30 text-error/70 hover:bg-error/10 hover:border-error/50 hover:text-error transition-all">
            <span class="material-symbols-outlined" style="font-size:14px">emergency_home</span>
            KILL SWITCH
        </button>
    </div>
</header>

<!-- Main Content -->
<main class="mt-12 mb-8 p-4 overflow-y-auto h-[calc(100vh-80px)]">

    <!-- Hero Metrics Row -->
    <div class="grid grid-cols-2 md:grid-cols-5 gap-px bg-outline-variant/20 border border-outline-variant/20 mb-4">
        <div class="bg-surface-container-lowest p-4">
            <div class="text-[0.625rem] text-outline uppercase font-bold tracking-widest mb-1">TOTAL NET PNL</div>
            <div id="total-pnl" class="text-2xl font-black text-outline mono">$0.00</div>
            <div id="total-pnl-pct" class="text-[0.625rem] text-outline mt-1">-- <span class="text-outline">ROE</span></div>
        </div>
        <div class="bg-surface-container-lowest p-4">
            <div class="text-[0.625rem] text-outline uppercase font-bold tracking-widest mb-1">GROSS EXPOSURE</div>
            <div id="gross-exposure" class="text-2xl font-black text-on-surface mono">$0 <span class="text-outline-variant text-lg">/ $50k</span></div>
            <div class="w-full bg-surface-container h-1 mt-2">
                <div id="exposure-bar" class="bg-secondary h-full transition-all duration-500" style="width:0%"></div>
            </div>
        </div>
        <div class="bg-surface-container-lowest p-4">
            <div class="text-[0.625rem] text-outline uppercase font-bold tracking-widest mb-1">AI SENTIMENT</div>
            <div class="flex items-center gap-3">
                <div id="sentiment-score" class="text-2xl font-black text-outline mono">--</div>
                <div id="sentiment-bars" class="flex-1 h-6 flex items-end gap-px">
                    <div class="h-2 w-1.5 bg-surface-container rounded-sm"></div>
                    <div class="h-3 w-1.5 bg-surface-container rounded-sm"></div>
                    <div class="h-4 w-1.5 bg-surface-container rounded-sm"></div>
                    <div class="h-4 w-1.5 bg-surface-container rounded-sm"></div>
                    <div class="h-3 w-1.5 bg-surface-container rounded-sm"></div>
                    <div class="h-2 w-1.5 bg-surface-container rounded-sm"></div>
                </div>
            </div>
            <div id="sentiment-label" class="text-[0.625rem] text-outline mt-1 uppercase">AWAITING</div>
        </div>
        <div class="bg-surface-container-lowest p-4">
            <div class="text-[0.625rem] text-outline uppercase font-bold tracking-widest mb-1">WIN RATE</div>
            <div id="win-rate" class="text-2xl font-black text-on-surface mono">--</div>
            <div id="win-rate-sub" class="text-[0.625rem] text-outline mt-1 uppercase">NO TRADES YET</div>
        </div>
        <div class="bg-surface-container-lowest p-4">
            <div class="text-[0.625rem] text-outline uppercase font-bold tracking-widest mb-1">LIVE ANN. FUNDING</div>
            <div id="ann-funding-display" class="text-2xl font-black text-outline mono">--%</div>
            <div id="ann-funding-label" class="text-[0.625rem] text-outline mt-1 uppercase">BTCUSDT</div>
        </div>
    </div>

    <!-- PnL Attribution Row -->
    <div class="grid grid-cols-2 md:grid-cols-4 gap-px bg-outline-variant/20 border border-outline-variant/20 mb-4">
        <div class="bg-surface-container-lowest p-3">
            <div class="text-[0.625rem] text-outline uppercase font-bold tracking-widest mb-1">FUNDING COLLECTED</div>
            <div id="attr-funding" class="text-lg font-black text-secondary mono">$0.0000</div>
        </div>
        <div class="bg-surface-container-lowest p-3">
            <div class="text-[0.625rem] text-outline uppercase font-bold tracking-widest mb-1">BASIS PNL</div>
            <div id="attr-basis" class="text-lg font-black text-outline mono">$0.00</div>
        </div>
        <div class="bg-surface-container-lowest p-3">
            <div class="text-[0.625rem] text-outline uppercase font-bold tracking-widest mb-1">EXECUTION COST</div>
            <div id="attr-cost" class="text-lg font-black text-tertiary-container mono">$0.0000</div>
        </div>
        <div class="bg-surface-container-lowest p-3">
            <div class="text-[0.625rem] text-outline uppercase font-bold tracking-widest mb-1 flex items-center gap-2">
                NET PNL
                <span id="trade-count-badge" class="px-1.5 bg-surface-container text-outline border border-outline-variant/20 text-[0.5rem] font-bold">0 TRADES</span>
            </div>
            <div id="attr-net" class="text-lg font-black text-outline mono">$0.00</div>
        </div>
    </div>

    <!-- Main Grid -->
    <div class="grid grid-cols-12 gap-4">
        <!-- Left Column -->
        <div class="col-span-12 xl:col-span-8 space-y-4">

            <!-- Active Arbitrage Pairs -->
            <div class="bg-surface-container-lowest border border-outline-variant/20">
                <div class="px-4 py-2 bg-surface-container-low border-b border-outline-variant/20 flex justify-between items-center">
                    <h2 class="text-[0.75rem] font-bold text-on-surface uppercase tracking-widest">ACTIVE ARBITRAGE PAIRS</h2>
                    <div class="flex items-center gap-3">
                        <span id="position-count" class="text-[0.625rem] text-outline">0 SLOTS</span>
                        <span id="live-indicator" class="text-[0.625rem] text-outline">● IDLE</span>
                    </div>
                </div>
                <div class="overflow-x-auto">
                    <table class="w-full text-left text-[0.6875rem]">
                        <thead>
                            <tr class="border-b border-outline-variant/10 text-outline uppercase font-bold">
                                <th class="p-3">SYMBOL</th>
                                <th class="p-3">DIRECTION</th>
                                <th class="p-3">SPOT (SZ / ENTRY / LIVE)</th>
                                <th class="p-3">PERP (SZ / ENTRY / LIVE)</th>
                                <th class="p-3">ANN. FUNDING</th>
                                <th class="p-3">BASIS</th>
                                <th class="p-3">NET PNL</th>
                            </tr>
                        </thead>
                        <tbody id="arb-tbody" class="mono">
                            <tr><td class="p-3 text-outline" colspan="7">No active positions — waiting for trade signals.</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>

            <!-- Trade History -->
            <div class="bg-surface-container-lowest border border-outline-variant/20">
                <div class="px-4 py-2 bg-surface-container-low border-b border-outline-variant/20 flex justify-between items-center">
                    <h2 class="text-[0.75rem] font-bold text-on-surface uppercase tracking-widest">TRADE HISTORY</h2>
                    <span id="total-trades" class="text-[0.625rem] text-outline mono">0 TRADES</span>
                </div>
                <div class="overflow-x-auto max-h-52 overflow-y-auto">
                    <table class="w-full text-left text-[0.6875rem]">
                        <thead>
                            <tr class="border-b border-outline-variant/10 text-outline uppercase font-bold sticky top-0 bg-surface-container-lowest">
                                <th class="p-3">SYMBOL</th>
                                <th class="p-3">SIDE</th>
                                <th class="p-3">ENTRY</th>
                                <th class="p-3">EXIT</th>
                                <th class="p-3">QTY</th>
                                <th class="p-3">NET PNL</th>
                                <th class="p-3">FUNDING</th>
                            </tr>
                        </thead>
                        <tbody id="trades-tbody" class="mono">
                            <tr><td class="p-3 text-outline" colspan="7">No trade history yet.</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>

        </div>

        <!-- Right Column -->
        <div class="col-span-12 xl:col-span-4 space-y-4">

            <!-- Asset Balances -->
            <div class="bg-surface-container-lowest border border-outline-variant/20">
                <div class="px-4 py-2 bg-surface-container-low border-b border-outline-variant/20">
                    <h2 class="text-[0.75rem] font-bold text-on-surface uppercase tracking-widest">ASSET BALANCES</h2>
                </div>
                <div id="balances-container" class="p-4">
                    <div class="text-[0.75rem] text-outline">Waiting for account update...</div>
                </div>
            </div>

            <!-- Risk Telemetry -->
            <div class="bg-surface-container-lowest border border-outline-variant/20">
                <div class="px-4 py-2 bg-surface-container-low border-b border-outline-variant/20">
                    <h2 class="text-[0.75rem] font-bold text-on-surface uppercase tracking-widest">RISK TELEMETRY</h2>
                </div>
                <div class="p-4 space-y-3">
                    <div class="grid grid-cols-2 gap-3">
                        <div class="bg-surface-container-low p-3 border border-outline-variant/10">
                            <div class="text-[0.625rem] text-outline uppercase font-bold mb-1">SPREAD TOXICITY</div>
                            <div id="spread-toxicity" class="text-lg font-black text-outline mono">-- bps</div>
                            <div id="spread-toxicity-label" class="text-[0.5rem] text-outline mt-0.5 uppercase">AWAITING</div>
                        </div>
                        <div class="bg-surface-container-low p-3 border border-outline-variant/10">
                            <div class="text-[0.625rem] text-outline uppercase font-bold mb-1">VENUE LATENCY</div>
                            <div id="venue-latency" class="text-lg font-black text-outline mono">-- ms</div>
                            <div id="venue-latency-label" class="text-[0.5rem] text-outline mt-0.5 uppercase">AWAITING</div>
                        </div>
                    </div>
                    <div class="bg-surface-container-low p-3 border border-outline-variant/10">
                        <div class="flex justify-between items-center mb-2">
                            <div class="text-[0.625rem] text-outline uppercase font-bold">DRAWDOWN</div>
                            <div id="drawdown-text" class="text-[0.625rem] mono text-on-surface">--% / 10%</div>
                        </div>
                        <div class="w-full bg-surface-container h-1.5 border border-outline-variant/10">
                            <div id="drawdown-bar" class="bg-secondary h-full transition-all duration-500" style="width:0%"></div>
                        </div>
                    </div>
                    <div class="bg-surface-container-low p-3 border border-outline-variant/10">
                        <div class="text-[0.625rem] text-outline uppercase font-bold mb-2">RISK STATUS</div>
                        <div id="risk-reasons" class="text-[0.625rem] mono text-primary">No active risk flags.</div>
                    </div>
                </div>
            </div>

            <!-- Live Funding Rate -->
            <div class="bg-surface-container-lowest border border-outline-variant/20 p-4">
                <div class="text-[0.625rem] text-outline uppercase font-bold tracking-widest mb-2">BEST LIVE FUNDING RATE</div>
                <div id="ann-funding-display2" class="text-4xl font-black text-outline mono">--%</div>
                <div id="ann-funding-label2" class="text-[0.625rem] text-outline mt-1 uppercase">AWAITING DATA</div>
            </div>

        </div>
    </div>
</main>

<!-- Footer Live Feed -->
<footer class="fixed bottom-0 left-0 w-full z-50 h-8 flex items-center justify-between px-4 bg-[#11131c] border-t border-outline-variant/20">
    <div class="flex items-center gap-4 overflow-hidden w-full">
        <span class="text-[0.625rem] font-bold text-primary mono shrink-0">BONGUS_OS ›</span>
        <div id="live-feed" class="flex items-center gap-4 text-[0.625rem] mono text-outline overflow-x-auto w-full pb-1 pt-1">
            <span class="shrink-0"><span class="text-primary-fixed-dim">SYS</span> Initializing...</span>
        </div>
    </div>
    <div class="flex items-center gap-4 border-l border-outline-variant/20 pl-4 shrink-0">
        <div class="flex items-center gap-1.5">
            <span id="stream-dot" class="w-1.5 h-1.5 bg-outline rounded-full"></span>
            <span class="text-[0.625rem] text-outline uppercase">WS</span>
        </div>
        <div class="flex items-center gap-1.5">
            <span id="api-dot" class="w-1.5 h-1.5 bg-outline rounded-full"></span>
            <span class="text-[0.625rem] text-outline uppercase">API</span>
        </div>
    </div>
</footer>

<script>
    // ── State ────────────────────────────────────────────────────────────
    let initialBalances = null;
    let currentBalances = {};
    const startTime = Date.now();

    // ── Uptime ───────────────────────────────────────────────────────────
    setInterval(() => {
        const e = Math.floor((Date.now() - startTime) / 1000);
        const h = String(Math.floor(e / 3600)).padStart(2,'0');
        const m = String(Math.floor((e % 3600) / 60)).padStart(2,'0');
        const s = String(e % 60).padStart(2,'0');
        document.getElementById('uptime-display').innerText = `${h}:${m}:${s}`;
    }, 1000);

    // ── Kill Switch ──────────────────────────────────────────────────────
    const ksModal = document.getElementById('kill-switch-modal');
    document.getElementById('kill-switch-btn').addEventListener('click', () => ksModal.classList.add('show'));
    document.getElementById('ks-cancel').addEventListener('click', () => ksModal.classList.remove('show'));
    document.getElementById('ks-confirm').addEventListener('click', async () => {
        ksModal.classList.remove('show');
        try {
            const r = await fetch('/api/kill-switch', {method:'POST'});
            if (r.ok) {
                addLog('⚠ KILL SWITCH ACTIVATED — new positions blocked');
                const btn = document.getElementById('kill-switch-btn');
                btn.className = btn.className.replace('error/30','error/60').replace('error/70','error');
                btn.textContent = '⚠ KILL SWITCH ACTIVE';
            }
        } catch(e) { addLog('Kill switch request failed: ' + e); }
    });

    // ── WebSocket ────────────────────────────────────────────────────────
    function connectWS() {
        const protocol = window.location.protocol === "https:" ? "wss://" : "ws://";
        const ws = new WebSocket(protocol + window.location.host + "/ws");

        ws.onopen = () => {
            document.getElementById("ws-status-text").innerText = "LIVE";
            document.getElementById("ws-status-text").className = "text-[0.65rem] font-bold text-primary tracking-widest uppercase";
            document.getElementById("ws-dot").className = "flex h-2 w-2 rounded-full bg-primary animate-pulse";
            document.getElementById("stream-dot").className = "w-1.5 h-1.5 bg-primary rounded-full";
            addLog("Rust Engine bridge connected.");
        };
        ws.onclose = () => {
            document.getElementById("ws-status-text").innerText = "OFFLINE";
            document.getElementById("ws-status-text").className = "text-[0.65rem] font-bold text-error tracking-widest uppercase";
            document.getElementById("ws-dot").className = "flex h-2 w-2 rounded-full bg-error";
            document.getElementById("stream-dot").className = "w-1.5 h-1.5 bg-error rounded-full";
            addLog("WebSocket lost — reconnecting in 3s...");
            setTimeout(connectWS, 3000);
        };
        ws.onmessage = (event) => {
            let data;
            try { data = JSON.parse(event.data); } catch(e) { addLog(event.data); return; }

            if (data.event === "AccountUpdate") {
                handleAccountUpdate(data);
            } else if (data.event === "OrderUpdate") {
                const col = data.status === "FILLED" ? "text-primary" : "text-outline";
                addLog(`<span class="${col}">ORDER ${data.status}</span> ${data.symbol} qty=${data.filled_qty}`);
            } else if (data.event === "Connected") {
                addLog(`<span class="text-secondary">CONNECTED</span> ${data.symbol}`);
            } else if (data.event === "Disconnected") {
                addLog(`<span class="text-error">DISCONNECTED</span> ${data.symbol}`);
            } else if (data.event === "MarkPrice") {
                document.getElementById("latency-display").innerText = `${(Date.now() % 1000).toString().padStart(3,'0')}ms`;
            }
        };
    }
    connectWS();

    // ── Account Update ───────────────────────────────────────────────────
    function handleAccountUpdate(data) {
        currentBalances = data.balances;
        if (!initialBalances) initialBalances = JSON.parse(JSON.stringify(data.balances));

        let totalPnl = 0.0, html = '';
        for (const [asset, bal] of Object.entries(currentBalances)) {
            const startBal = initialBalances[asset] || 0.0;
            const diff = bal - startBal;
            if (asset === "USDT") totalPnl += diff;
            const color = diff >= 0 ? "text-primary" : "text-error";
            const sign = diff > 0 ? '+' : '';
            html += `<div class="flex justify-between items-center border-b border-outline-variant/5 pb-2 mb-2">
                <span class="text-[0.75rem] font-bold text-secondary">${asset}</span>
                <div class="text-right mono">
                    <div class="text-[0.75rem] font-bold">${bal.toFixed(4)}</div>
                    <div class="text-[0.5rem] ${color}">${sign}${diff.toFixed(4)}</div>
                </div>
            </div>`;
        }

        const pnlEl = document.getElementById("total-pnl");
        const sign = totalPnl >= 0 ? '+' : '';
        pnlEl.innerText = `${sign}$${totalPnl.toFixed(2)}`;
        pnlEl.className = `text-2xl font-black mono ${totalPnl >= 0 ? 'text-primary glow-green' : 'text-error glow-red'}`;
        document.getElementById("balances-container").innerHTML = html || '<div class="text-[0.75rem] text-outline">No balances received.</div>';
    }

    // ── REST Data ────────────────────────────────────────────────────────
    async function loadDashboardData() {
        try {
            const [posRes, statsRes, riskRes, tradesRes, attrRes] = await Promise.all([
                fetch('/api/positions'),
                fetch('/api/stats'),
                fetch('/api/risk'),
                fetch('/api/trades?limit=20'),
                fetch('/api/pnl-attribution'),
            ]);
            renderPositions(await posRes.json());
            renderStats(await statsRes.json());
            const risk = await riskRes.json();
            renderRisk(risk);
            renderMode(risk);
            renderTrades(await tradesRes.json());
            renderAttribution(await attrRes.json());
            document.getElementById("api-dot").className = "w-1.5 h-1.5 bg-primary rounded-full";
        } catch(e) {
            document.getElementById("api-dot").className = "w-1.5 h-1.5 bg-error rounded-full";
        }
    }

    // ── Renderers ────────────────────────────────────────────────────────
    function renderPositions(positions) {
        const tbody = document.getElementById('arb-tbody');
        const indicator = document.getElementById('live-indicator');
        const countEl = document.getElementById('position-count');
        if (!positions || positions.length === 0) {
            tbody.innerHTML = '<tr><td class="p-3 text-outline" colspan="7">No active positions — waiting for signals.</td></tr>';
            indicator.innerHTML = '● IDLE';
            indicator.className = 'text-[0.625rem] text-outline';
            countEl.textContent = '0 SLOTS';
            return;
        }
        indicator.innerHTML = '<span class="text-primary animate-pulse">●</span> LIVE';
        indicator.className = 'text-[0.625rem] text-primary';
        countEl.textContent = `${positions.length}/4 SLOTS`;

        let html = '';
        for (const p of positions) {
            const pnlColor = p.net_pnl_usd >= 0 ? 'text-primary' : 'text-error';
            const pnlSign = p.net_pnl_usd >= 0 ? '+' : '';
            const fundColor = p.ann_funding > 0 ? 'text-primary' : 'text-error';
            const dir = p.direction === 'long' ? '<span class="text-primary">↑ LONG</span>/<span class="text-error">↓ SHORT</span>' : '<span class="text-error">↓ SHORT</span>/<span class="text-primary">↑ LONG</span>';
            html += `<tr class="border-b border-outline-variant/5 hover:bg-surface-container/30">
                <td class="p-3 font-bold text-secondary">${p.symbol.replace('USDT','')}/<span class="text-outline">USDT</span></td>
                <td class="p-3 text-[0.6rem]">${dir}</td>
                <td class="p-3">${p.qty.toFixed(4)} / ${p.spot_entry.toFixed(2)} / <span class="text-primary">${(p.spot_live||p.spot_entry).toFixed(2)}</span></td>
                <td class="p-3">-${p.qty.toFixed(4)} / ${p.perp_entry.toFixed(2)} / <span class="text-tertiary-container">${(p.perp_live||p.perp_entry).toFixed(2)}</span></td>
                <td class="p-3 ${fundColor} font-bold">${p.ann_funding >= 0 ? '+' : ''}${(p.ann_funding*100).toFixed(2)}%</td>
                <td class="p-3">${p.basis_pct >= 0 ? '+' : ''}${(p.basis_pct*100).toFixed(3)}%</td>
                <td class="p-3 ${pnlColor} font-bold">${pnlSign}$${p.net_pnl_usd.toFixed(2)}</td>
            </tr>`;
        }
        tbody.innerHTML = html;
    }

    function renderStats(stats) {
        const pnl = stats.total_pnl || 0;
        const pnlEl = document.getElementById('total-pnl');
        const sign = pnl >= 0 ? '+' : '';
        pnlEl.innerText = `${sign}$${pnl.toFixed(2)}`;
        pnlEl.className = `text-2xl font-black mono ${pnl >= 0 ? 'text-primary glow-green' : 'text-error glow-red'}`;

        const equity = stats.account_equity || 10000;
        document.getElementById('total-pnl-pct').innerHTML = `${sign}${(equity > 0 ? (pnl/equity*100) : 0).toFixed(2)}% <span class="text-outline">ROE</span>`;

        const exp = stats.gross_exposure || 0;
        const maxExp = stats.max_gross_exposure || 50000;
        document.getElementById('gross-exposure').innerHTML = `$${(exp/1000).toFixed(1)}k <span class="text-outline-variant text-lg">/ $${(maxExp/1000).toFixed(0)}k</span>`;
        document.getElementById('exposure-bar').style.width = `${Math.min(100, (exp/maxExp)*100)}%`;

        const sentiment = stats.sentiment_score;
        if (sentiment !== undefined) {
            const sSign = sentiment >= 0 ? '+' : '';
            document.getElementById('sentiment-score').innerText = `${sSign}${sentiment.toFixed(2)}`;
            document.getElementById('sentiment-score').className = `text-2xl font-black mono ${sentiment > 0 ? 'text-secondary' : sentiment < -0.3 ? 'text-error' : 'text-on-surface'}`;
            document.getElementById('sentiment-label').innerText = sentiment > 0.3 ? 'BULLISH' : sentiment < -0.3 ? 'BEARISH' : 'NEUTRAL';
            const level = Math.min(6, Math.max(0, Math.round((sentiment + 1) * 3)));
            const heights = ['h-2','h-2','h-3','h-4','h-4','h-3'];
            let barsHtml = '';
            for (let i = 0; i < 6; i++) {
                const active = i < level;
                const color = active ? (sentiment < -0.3 ? 'bg-error' : sentiment > 0.3 ? 'bg-secondary' : 'bg-on-surface') : 'bg-surface-container';
                barsHtml += `<div class="${heights[i]} w-1.5 ${color} rounded-sm transition-all duration-300"></div>`;
            }
            document.getElementById('sentiment-bars').innerHTML = barsHtml;
        }

        const wr = stats.win_rate;
        if (wr !== undefined) {
            document.getElementById('win-rate').innerText = `${(wr*100).toFixed(1)}%`;
            document.getElementById('win-rate').className = `text-2xl font-black mono ${wr >= 0.6 ? 'text-primary' : wr >= 0.4 ? 'text-on-surface' : 'text-error'}`;
            document.getElementById('win-rate-sub').innerText = `${Math.round(stats.trade_count||0)} TRADES TOTAL`;
        }

        document.getElementById('total-trades').textContent = `${Math.round(stats.trade_count||0)} TRADES`;

        const funding = stats.ann_funding;
        if (funding !== undefined) {
            const fEl = document.getElementById('ann-funding-display');
            fEl.innerText = `${funding >= 0 ? '+' : ''}${(funding*100).toFixed(2)}%`;
            fEl.className = `text-2xl font-black mono ${funding > 0.06 ? 'text-primary glow-green' : funding > 0 ? 'text-secondary' : 'text-error glow-red'}`;
            const fEl2 = document.getElementById('ann-funding-display2');
            fEl2.innerText = `${funding >= 0 ? '+' : ''}${(funding*100).toFixed(2)}%`;
            fEl2.className = `text-4xl font-black mono ${funding > 0.06 ? 'text-primary glow-green' : funding > 0 ? 'text-secondary' : 'text-error glow-red'}`;
        }
    }

    function renderRisk(risk) {
        const tox = risk.spread_toxicity;
        if (tox !== undefined) {
            document.getElementById('spread-toxicity').innerText = `${tox} bps`;
            document.getElementById('spread-toxicity').className = `text-lg font-black mono ${tox > 30 ? 'text-tertiary-container' : 'text-primary'}`;
            document.getElementById('spread-toxicity-label').innerText = tox > 30 ? 'ELEVATED' : 'NORMAL';
        }
        const lat = risk.venue_latency;
        if (lat !== undefined) {
            document.getElementById('venue-latency').innerText = `${lat} ms`;
            document.getElementById('venue-latency').className = `text-lg font-black mono ${lat > 200 ? 'text-tertiary-container' : 'text-primary'}`;
            document.getElementById('venue-latency-label').innerText = lat > 200 ? 'DEGRADED' : 'STABLE';
            document.getElementById('latency-display').innerText = `${lat}ms`;
        }
        const dd = risk.drawdown_pct;
        if (dd !== undefined) {
            document.getElementById('drawdown-text').innerText = `${(dd*100).toFixed(1)}% / 10%`;
            const bar = document.getElementById('drawdown-bar');
            bar.style.width = `${Math.min(100, dd*1000)}%`;
            bar.className = `h-full transition-all duration-500 ${dd > 0.08 ? 'bg-error' : dd > 0.04 ? 'bg-tertiary-container' : 'bg-secondary'}`;
        }
        const reasons = risk.reasons;
        const rEl = document.getElementById('risk-reasons');
        if (risk.kill_switch === true || risk.kill_switch === 'true') {
            rEl.innerHTML = '<div class="text-error font-bold">⚠ KILL SWITCH ACTIVE</div>';
        } else if (reasons && Array.isArray(reasons) && reasons.length > 0) {
            rEl.innerHTML = reasons.map(r => `<div class="text-tertiary-container">⚠ ${r}</div>`).join('');
        } else {
            rEl.innerHTML = '<div class="text-primary">✓ No active risk flags.</div>';
        }
    }

    function renderTrades(trades) {
        const tbody = document.getElementById('trades-tbody');
        if (!trades || trades.length === 0) {
            tbody.innerHTML = '<tr><td class="p-3 text-outline" colspan="7">No trade history yet.</td></tr>';
            return;
        }
        let html = '';
        for (const t of trades) {
            const pnlColor = t.net_pnl_usd >= 0 ? 'text-primary' : 'text-error';
            const sign = t.net_pnl_usd >= 0 ? '+' : '';
            html += `<tr class="border-b border-outline-variant/5 hover:bg-surface-container/30">
                <td class="p-3 font-bold text-secondary">${t.symbol}</td>
                <td class="p-3 text-outline">${t.side}</td>
                <td class="p-3">$${t.entry_price.toFixed(2)}</td>
                <td class="p-3">$${t.exit_price.toFixed(2)}</td>
                <td class="p-3">${t.qty.toFixed(4)}</td>
                <td class="p-3 ${pnlColor} font-bold">${sign}$${t.net_pnl_usd.toFixed(2)}</td>
                <td class="p-3 text-secondary">+$${(t.funding_collected||0).toFixed(4)}</td>
            </tr>`;
        }
        tbody.innerHTML = html;
    }

    function renderAttribution(attr) {
        if (!attr) return;
        const funding = attr.total_funding || 0;
        const basis = attr.total_basis_pnl || 0;
        const cost = attr.total_execution_cost || 0;
        const net = attr.total_net_pnl || 0;
        const count = attr.trade_count || 0;

        document.getElementById('attr-funding').innerText = `+$${funding.toFixed(4)}`;
        document.getElementById('attr-basis').innerText = `${basis >= 0 ? '+' : ''}$${basis.toFixed(2)}`;
        document.getElementById('attr-basis').className = `text-lg font-black mono ${basis >= 0 ? 'text-primary' : 'text-error'}`;
        document.getElementById('attr-cost').innerText = `-$${Math.abs(cost).toFixed(4)}`;
        document.getElementById('attr-net').innerText = `${net >= 0 ? '+' : ''}$${net.toFixed(2)}`;
        document.getElementById('attr-net').className = `text-lg font-black mono ${net >= 0 ? 'text-primary glow-green' : 'text-error glow-red'}`;
        document.getElementById('trade-count-badge').innerText = `${count} TRADES`;
    }

    function renderMode(risk) {
        const badge = document.getElementById('mode-badge');
        const mode = (risk.trading_mode || '--').toUpperCase();
        badge.innerText = mode;
        if (mode === 'PAPER') {
            badge.className = 'px-2 py-0.5 text-[0.625rem] font-black tracking-widest uppercase border bg-primary/10 text-primary border-primary/30';
        } else if (mode === 'TESTNET') {
            badge.className = 'px-2 py-0.5 text-[0.625rem] font-black tracking-widest uppercase border bg-yellow-500/20 text-yellow-400 border-yellow-500/40';
        } else if (mode === 'LIVE') {
            badge.className = 'px-2 py-0.5 text-[0.625rem] font-black tracking-widest uppercase border bg-error/20 text-error border-error/40';
        } else {
            badge.className = 'px-2 py-0.5 text-[0.625rem] font-black tracking-widest uppercase border bg-surface-container text-outline border-outline-variant/30';
        }
    }

    // ── Log ──────────────────────────────────────────────────────────────
    function addLog(msg) {
        const feed = document.getElementById("live-feed");
        const time = new Date().toLocaleTimeString('en-US', {hour12:false});
        const span = document.createElement("span");
        span.className = "flex items-center gap-1 shrink-0";
        span.innerHTML = `<span class="text-primary-fixed-dim">${time}</span> ${msg}`;
        const sep = document.createElement("span");
        sep.className = "text-outline-variant/40 shrink-0";
        sep.innerText = "│";
        feed.prepend(sep);
        feed.prepend(span);
        while (feed.children.length > 80) feed.removeChild(feed.lastChild);
    }

    // ── Init ─────────────────────────────────────────────────────────────
    loadDashboardData();
    setInterval(loadDashboardData, 5000);
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
        active_connections.discard(websocket)
    except Exception:
        active_connections.discard(websocket)


# ── Log Viewer ─────────────────────────────────────────────────────────────

LOGS_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta content="width=device-width, initial-scale=1.0" name="viewport"/>
<title>BONGUS | Live Logs</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet"/>
<style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { background: #0c0e17; color: #c8c8d8; font-family: 'JetBrains Mono', monospace; font-size: 12px; }
    header { position: fixed; top: 0; left: 0; right: 0; height: 40px; background: #11131c; border-bottom: 1px solid #3b494c33;
             display: flex; align-items: center; justify-content: space-between; padding: 0 16px; z-index: 10; }
    header .title { font-weight: 700; font-size: 13px; color: #b5ffaa; }
    header .status { font-size: 11px; color: #849396; }
    #dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: #849396; margin-right: 6px; }
    #dot.live { background: #b5ffaa; }
    #log-container { position: fixed; top: 40px; bottom: 0; left: 0; right: 0; overflow-y: auto; padding: 12px 16px; }
    .line { white-space: pre-wrap; word-break: break-all; line-height: 1.6; }
    .line.warn { color: #ffb74d; }
    .line.err { color: #ffb4ab; }
    .line.info { color: #c8c8d8; }
    a.back { color: #849396; text-decoration: none; font-size: 11px; }
    a.back:hover { color: #b5ffaa; }
</style>
</head>
<body>
<header>
    <div style="display:flex;align-items:center;gap:12px;">
        <span class="title">BONGUS LOGS</span>
        <a class="back" href="/">&larr; Dashboard</a>
    </div>
    <div class="status"><span id="dot"></span><span id="status-text">CONNECTING</span></div>
</header>
<div id="log-container"></div>
<script>
    const container = document.getElementById('log-container');
    const dot = document.getElementById('dot');
    const statusText = document.getElementById('status-text');
    let autoScroll = true;

    container.addEventListener('scroll', () => {
        autoScroll = container.scrollTop + container.clientHeight >= container.scrollHeight - 40;
    });

    function classify(text) {
        if (/WARNING|WARN/i.test(text)) return 'warn';
        if (/ERROR|CRITICAL|EXCEPTION|Traceback/i.test(text)) return 'err';
        return 'info';
    }

    function addLine(text) {
        const el = document.createElement('div');
        el.className = 'line ' + classify(text);
        el.textContent = text;
        container.appendChild(el);
        while (container.children.length > 5000) container.removeChild(container.firstChild);
        if (autoScroll) container.scrollTop = container.scrollHeight;
    }

    function connect() {
        const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
        const ws = new WebSocket(proto + location.host + '/ws/logs');
        ws.onopen = () => { dot.className = 'live'; statusText.textContent = 'LIVE'; };
        ws.onclose = () => { dot.className = ''; statusText.textContent = 'RECONNECTING'; setTimeout(connect, 2000); };
        ws.onmessage = (e) => { addLine(e.data); };
    }
    connect();
</script>
</body>
</html>
"""

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
    uvicorn.run(app, host="0.0.0.0", port=8080)
