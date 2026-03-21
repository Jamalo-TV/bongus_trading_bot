import asyncio
import json
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import HTMLResponse
from contextlib import asynccontextmanager

from state_store import StateReader

active_connections: set[WebSocket] = set()
reader = StateReader()

async def consume_tcp_stream():
    """Background task: Reads from Rust IPC and broadcasts to all WebSocket clients."""
    while True:
        try:
            tcp_reader, _ = await asyncio.open_connection('127.0.0.1', 9000)
            print("FastAPI connected to Rust Engine IPC.")
            while True:
                line = await tcp_reader.readline()
                if not line:
                    break

                msg = line.decode('utf-8').strip()

                disconnected_clients: set[WebSocket] = set()
                for connection in active_connections:
                    try:
                        await connection.send_text(msg)
                    except Exception:
                        disconnected_clients.add(connection)

                active_connections.difference_update(disconnected_clients)

        except Exception as e:
            print(f"FastAPI IPC connection error: {e}")
            await asyncio.sleep(2)

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


# ── Dashboard HTML ──────────────────────────────────────────────────────────

HTML_CONTENT = """
<!DOCTYPE html>
<html class="dark" lang="en">
<head>
<meta charset="utf-8"/>
<meta content="width=device-width, initial-scale=1.0" name="viewport"/>
<title>KINETIC MONOLITH | DELTA-NEUTRAL ARBITRAGE</title>
<script src="https://cdn.tailwindcss.com?plugins=forms,container-queries"></script>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet"/>
<link href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:wght,FILL@100..700,0..1&display=swap" rel="stylesheet"/>
<style>
    body {
        font-family: 'Inter', sans-serif;
        background-color: #11131c;
        color: #e1e1ef;
    }
    .mono {
        font-family: 'JetBrains Mono', monospace;
    }
    .material-symbols-outlined {
        font-variation-settings: 'FILL' 0, 'wght' 400, 'GRAD' 0, 'opsz' 24;
        vertical-align: middle;
        font-size: 18px;
    }
    ::-webkit-scrollbar {
        width: 4px;
        height: 4px;
    }
    ::-webkit-scrollbar-track {
        background: #0c0e17;
    }
    ::-webkit-scrollbar-thumb {
        background: #3b494c;
    }
    .glow-green {
        text-shadow: 0 0 8px rgba(181, 255, 170, 0.4);
    }
    .glow-red {
        text-shadow: 0 0 8px rgba(255, 193, 197, 0.4);
    }
</style>
<script id="tailwind-config">
    tailwind.config = {
        darkMode: "class",
        theme: {
        extend: {
            colors: {
            "on-error": "#690005",
            "surface-container": "#1d1f29",
            "surface-container-low": "#191b24",
            "primary-fixed-dim": "#00e639",
            "on-tertiary-container": "#b40039",
            "on-surface-variant": "#bac9cc",
            "surface": "#11131c",
            "surface-container-highest": "#32343e",
            "inverse-primary": "#006e16",
            "on-secondary": "#00363d",
            "on-primary-fixed-variant": "#00530e",
            "surface-container-high": "#282933",
            "background": "#11131c",
            "tertiary-fixed": "#ffdadb",
            "tertiary-container": "#ffc1c5",
            "secondary": "#bdf4ff",
            "on-background": "#e1e1ef",
            "surface-container-lowest": "#0c0e17",
            "on-secondary-fixed-variant": "#004f58",
            "on-error-container": "#ffdad6",
            "primary-fixed": "#72ff70",
            "on-primary-container": "#006814",
            "inverse-on-surface": "#2e303a",
            "on-primary": "#003907",
            "outline-variant": "#3b494c",
            "on-tertiary": "#67001d",
            "error-container": "#93000a",
            "on-secondary-container": "#00616d",
            "primary-container": "#00f13d",
            "surface-tint": "#00e639",
            "on-tertiary-fixed": "#40000f",
            "on-primary-fixed": "#002203",
            "on-tertiary-fixed-variant": "#91002d",
            "on-surface": "#e1e1ef",
            "surface-bright": "#373943",
            "tertiary": "#ffe7e8",
            "inverse-surface": "#e1e1ef",
            "secondary-fixed": "#9cf0ff",
            "error": "#ffb4ab",
            "outline": "#849396",
            "surface-dim": "#11131c",
            "surface-variant": "#32343e",
            "secondary-container": "#00e3fd",
            "primary": "#b5ffaa",
            "on-secondary-fixed": "#001f24",
            "secondary-fixed-dim": "#00daf3",
            "tertiary-fixed-dim": "#ffb2b8"
            },
            fontFamily: {
            "headline": ["Inter"],
            "body": ["Inter"],
            "label": ["Inter"]
            },
            borderRadius: {"DEFAULT": "0px", "lg": "0px", "xl": "0px", "full": "9999px"},
        },
        },
    }
</script>
</head>
<body class="bg-background text-on-surface overflow-hidden">
<!-- TopNavBar -->
<header class="fixed top-0 left-0 w-full z-50 flex justify-between items-center px-4 h-12 bg-[#11131c] border-b border-outline-variant/20">
<div class="flex items-center gap-6">
<span class="text-lg font-black tracking-tighter text-primary">KINETIC MONOLITH</span>
<nav class="hidden md:flex items-center gap-4">
<a class="font-['Inter'] uppercase tracking-[0.05rem] text-[0.75rem] text-primary border-b-2 border-primary pb-1" href="#">TERMINAL</a>
<a class="font-['Inter'] uppercase tracking-[0.05rem] text-[0.75rem] text-outline hover:text-secondary transition-colors duration-100" href="#">MARKETS</a>
<a class="font-['Inter'] uppercase tracking-[0.05rem] text-[0.75rem] text-outline hover:text-secondary transition-colors duration-100" href="#">EXCHANGES</a>
<a class="font-['Inter'] uppercase tracking-[0.05rem] text-[0.75rem] text-outline hover:text-secondary transition-colors duration-100" href="#">LOGS</a>
<a class="font-['Inter'] uppercase tracking-[0.05rem] text-[0.75rem] text-outline hover:text-secondary transition-colors duration-100" href="#">RISK</a>
</nav>
</div>
<div class="flex items-center gap-4">
<div class="flex items-center gap-2 px-3 py-1 bg-surface-container-lowest border border-outline-variant/30">
<span class="flex h-2 w-2 rounded-full bg-primary animate-pulse"></span>
<span id="ws-status-text" class="text-[0.65rem] font-bold text-primary tracking-widest uppercase">CONNECTING...</span>
<div class="flex gap-1 ml-2">
<span class="w-1 h-1 bg-primary rounded-full"></span>
<span class="w-1 h-1 bg-primary rounded-full"></span>
<span class="w-1 h-1 bg-primary rounded-full"></span>
</div>
<span id="latency-display" class="text-[0.625rem] text-outline ml-2 border-l border-outline-variant/30 pl-2 uppercase">-- LATENCY</span>
</div>
<button class="bg-on-tertiary-container text-white px-3 h-8 text-[0.6875rem] font-bold tracking-tighter flex items-center gap-2 hover:bg-tertiary-container hover:text-on-tertiary transition-all">
<span class="material-symbols-outlined text-[14px]">cancel</span>
            GLOBAL KILL SWITCH
        </button>
<div class="flex items-center gap-3 text-outline border-l border-outline-variant/30 pl-4">
<span class="material-symbols-outlined hover:text-primary cursor-pointer">account_tree</span>
<span class="material-symbols-outlined hover:text-primary cursor-pointer">settings</span>
<span class="material-symbols-outlined hover:text-primary cursor-pointer">notifications</span>
</div>
</div>
</header>
<!-- SideNavBar -->
<aside class="fixed left-0 top-12 bottom-8 w-64 flex flex-col z-40 bg-surface-container-lowest border-r border-outline-variant/20">
<div class="p-4 border-b border-outline-variant/10">
<div class="flex items-center gap-3">
<div class="w-8 h-8 bg-surface-container-high flex items-center justify-center border border-outline-variant/30">
<span class="material-symbols-outlined text-secondary" style="font-variation-settings: 'FILL' 1;">dns</span>
</div>
<div>
<div class="text-[0.75rem] font-bold text-secondary uppercase">NODE_ARBITRAGE_01</div>
<div id="uptime-display" class="text-[0.625rem] text-primary mono">UPTIME: --</div>
</div>
</div>
</div>
<nav class="flex-1 py-4 overflow-y-auto">
<div class="space-y-1">
<a class="flex items-center gap-3 px-4 py-2 text-[0.6875rem] font-medium text-primary bg-surface-container-low border-l-2 border-primary" href="#">
<span class="material-symbols-outlined" style="font-variation-settings: 'FILL' 1;">account_balance_wallet</span>
                PORTFOLIO
            </a>
<a class="flex items-center gap-3 px-4 py-2 text-[0.6875rem] font-medium text-outline hover:bg-surface-container-low hover:text-secondary transition-all" href="#">
<span class="material-symbols-outlined">reorder</span>
                ORDERBOOK
            </a>
<a class="flex items-center gap-3 px-4 py-2 text-[0.6875rem] font-medium text-outline hover:bg-surface-container-low hover:text-secondary transition-all" href="#">
<span class="material-symbols-outlined">history</span>
                HISTORY
            </a>
<a class="flex items-center gap-3 px-4 py-2 text-[0.6875rem] font-medium text-outline hover:bg-surface-container-low hover:text-secondary transition-all" href="#">
<span class="material-symbols-outlined">psychology</span>
                ALGO_LAB
            </a>
<a class="flex items-center gap-3 px-4 py-2 text-[0.6875rem] font-medium text-outline hover:bg-surface-container-low hover:text-secondary transition-all" href="#">
<span class="material-symbols-outlined">assessment</span>
                REPORTS
            </a>
<a class="flex items-center gap-3 px-4 py-2 text-[0.6875rem] font-medium text-outline hover:bg-surface-container-low hover:text-secondary transition-all" href="#">
<span class="material-symbols-outlined">terminal</span>
                SYSTEM
            </a>
</div>
<div class="mt-8 px-4">
<button class="w-full bg-primary text-on-primary py-2 text-[0.6875rem] font-black uppercase tracking-widest hover:brightness-110 active:scale-95 transition-all">
                DEPLOY_BOT
            </button>
</div>
</nav>
<div class="p-4 border-t border-outline-variant/10 space-y-2">
<a class="flex items-center gap-3 text-[0.625rem] text-outline hover:text-secondary" href="#">
<span class="material-symbols-outlined text-[16px]">help_outline</span>
            SUPPORT
        </a>
<a class="flex items-center gap-3 text-[0.625rem] text-outline hover:text-secondary" href="#">
<span class="material-symbols-outlined text-[16px]">description</span>
            DOCS
        </a>
</div>
</aside>
<!-- Main Content Canvas -->
<main class="ml-64 mt-12 mb-8 p-4 overflow-y-auto h-[calc(100vh-80px)]">
<!-- Hero Metrics Row -->
<div class="grid grid-cols-1 md:grid-cols-4 lg:grid-cols-5 gap-px bg-outline-variant/20 border border-outline-variant/20 mb-4">
<div class="bg-surface-container-lowest p-4">
<div class="text-[0.625rem] text-outline uppercase font-bold tracking-widest mb-1">TOTAL NET PNL (USD)</div>
<div id="total-pnl" class="text-2xl font-black text-outline mono">$0.00</div>
<div id="total-pnl-pct" class="text-[0.625rem] text-outline mt-1">-- <span class="text-outline">ROE</span></div>
</div>
<div class="bg-surface-container-lowest p-4">
<div class="text-[0.625rem] text-outline uppercase font-bold tracking-widest mb-1">GROSS EXPOSURE</div>
<div id="gross-exposure" class="text-2xl font-black text-on-surface mono">$0 <span class="text-outline-variant text-lg">/ $50k</span></div>
<div class="w-full bg-surface-container h-1 mt-2">
<div id="exposure-bar" class="bg-secondary h-full" style="width: 0%"></div>
</div>
</div>
<div class="bg-surface-container-lowest p-4">
<div class="text-[0.625rem] text-outline uppercase font-bold tracking-widest mb-1">AI SENTIMENT SCORE</div>
<div class="flex items-center gap-3">
<div id="sentiment-score" class="text-2xl font-black text-outline mono">--</div>
<div id="sentiment-bars" class="flex-1 h-6 flex items-center gap-px">
<div class="h-4 w-1 bg-surface-container"></div>
<div class="h-4 w-1 bg-surface-container"></div>
<div class="h-4 w-1 bg-surface-container"></div>
<div class="h-4 w-1 bg-surface-container"></div>
<div class="h-4 w-1 bg-surface-container"></div>
<div class="h-4 w-1 bg-surface-container"></div>
</div>
</div>
<div id="sentiment-label" class="text-[0.625rem] text-outline mt-1 uppercase">AWAITING DATA</div>
</div>
<div class="bg-surface-container-lowest p-4">
<div class="text-[0.625rem] text-outline uppercase font-bold tracking-widest mb-1">WIN RATE</div>
<div id="win-rate" class="text-2xl font-black text-on-surface mono">--</div>
<div id="win-rate-sub" class="text-[0.625rem] text-outline mt-1 uppercase">NO TRADES YET</div>
</div>
<div class="bg-surface-container-lowest p-4 hidden lg:block">
<div class="text-[0.625rem] text-outline uppercase font-bold tracking-widest mb-1">TOTAL TRADES</div>
<div id="total-trades" class="text-2xl font-black text-on-surface mono">0</div>
<div id="total-volume" class="text-[0.625rem] text-outline mt-1 uppercase">VOLUME: $0</div>
</div>
</div>
<div class="grid grid-cols-12 gap-4">
<!-- Left Column: Active Arbitrage Pairs -->
<div class="col-span-12 xl:col-span-8 space-y-4">
<div class="bg-surface-container-lowest border border-outline-variant/20">
<div class="px-4 py-2 bg-surface-container-low border-b border-outline-variant/20 flex justify-between items-center">
<h2 class="text-[0.75rem] font-bold text-on-surface uppercase tracking-widest">ACTIVE ARBITRAGE PAIRS</h2>
<div class="flex gap-2">
<span class="text-[0.625rem] text-outline">AUTO-HEDGE: ON</span>
<span id="live-indicator" class="text-[0.625rem] text-outline">-- IDLE</span>
</div>
</div>
<div class="overflow-x-auto">
<table class="w-full text-left text-[0.6875rem]">
<thead>
<tr class="border-b border-outline-variant/10 text-outline uppercase font-bold">
<th class="p-3">SYMBOL</th>
<th class="p-3">SPOT LEG (SZ/ENT/LIVE)</th>
<th class="p-3">PERP LEG (SZ/ENT/LIVE)</th>
<th class="p-3">ANN. FUNDING</th>
<th class="p-3">BASIS</th>
<th class="p-3">NET PNL</th>
<th class="p-3">STATUS</th>
</tr>
</thead>
<tbody id="arb-tbody" class="mono">
<tr class="border-b border-outline-variant/5">
<td class="p-3 text-outline" colspan="7">No active positions. Waiting for trade signals...</td>
</tr>
</tbody>
</table>
</div>
</div>
<!-- Trade History -->
<div class="bg-surface-container-lowest border border-outline-variant/20">
<div class="px-4 py-2 bg-surface-container-low border-b border-outline-variant/20 flex justify-between items-center">
<h2 class="text-[0.75rem] font-bold text-on-surface uppercase tracking-widest">RECENT TRADES</h2>
</div>
<div class="overflow-x-auto max-h-48 overflow-y-auto">
<table class="w-full text-left text-[0.6875rem]">
<thead>
<tr class="border-b border-outline-variant/10 text-outline uppercase font-bold sticky top-0 bg-surface-container-lowest">
<th class="p-3">SYMBOL</th>
<th class="p-3">SIDE</th>
<th class="p-3">ENTRY</th>
<th class="p-3">EXIT</th>
<th class="p-3">QTY</th>
<th class="p-3">PNL</th>
<th class="p-3">FUNDING</th>
</tr>
</thead>
<tbody id="trades-tbody" class="mono">
<tr class="border-b border-outline-variant/5">
<td class="p-3 text-outline" colspan="7">No trade history yet.</td>
</tr>
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
<div id="balances-container" class="p-4 space-y-4">
    <div class="text-[0.75rem] text-outline">Waiting for AccountUpdate via IPC...</div>
</div>
</div>
<!-- Risk Telemetry -->
<div class="bg-surface-container-lowest border border-outline-variant/20">
<div class="px-4 py-2 bg-surface-container-low border-b border-outline-variant/20">
<h2 class="text-[0.75rem] font-bold text-on-surface uppercase tracking-widest">RISK TELEMETRY</h2>
</div>
<div class="p-4 grid grid-cols-2 gap-4">
<div class="bg-surface-container-low p-3 border border-outline-variant/10">
<div class="text-[0.625rem] text-outline uppercase font-bold mb-1">SPREAD TOXICITY</div>
<div id="spread-toxicity" class="text-lg font-black text-outline mono">-- bps</div>
<div id="spread-toxicity-label" class="text-[0.625rem] text-outline mt-1 uppercase">AWAITING</div>
</div>
<div class="bg-surface-container-low p-3 border border-outline-variant/10">
<div class="text-[0.625rem] text-outline uppercase font-bold mb-1">VENUE LATENCY</div>
<div id="venue-latency" class="text-lg font-black text-outline mono">-- ms</div>
<div id="venue-latency-label" class="text-[0.625rem] text-outline mt-1 uppercase">AWAITING</div>
</div>
<div class="col-span-2 bg-surface-container-low p-3 border border-outline-variant/10">
<div class="flex justify-between items-end mb-1">
<div class="text-[0.625rem] text-outline uppercase font-bold">MAX DRAWDOWN TRACKER</div>
<div id="drawdown-text" class="text-[0.625rem] mono text-on-surface">--% / 10%</div>
</div>
<div class="w-full bg-surface-container h-2 border border-outline-variant/20">
<div id="drawdown-bar" class="bg-secondary h-full" style="width: 0%"></div>
</div>
</div>
<div class="col-span-2 bg-surface-container-low p-3 border border-outline-variant/10">
<div class="text-[0.625rem] text-outline uppercase font-bold mb-1">RISK STATUS</div>
<div id="risk-reasons" class="text-[0.625rem] mono text-primary">No active risk flags.</div>
</div>
</div>
</div>
<!-- Ann. Funding Rate -->
<div class="bg-surface-container-lowest border border-outline-variant/20 p-4">
<div class="text-[0.625rem] text-outline uppercase font-bold tracking-widest mb-2">LIVE ANNUALIZED FUNDING</div>
<div id="ann-funding-display" class="text-3xl font-black text-outline mono">--%</div>
<div id="ann-funding-label" class="text-[0.625rem] text-outline mt-1 uppercase">BTCUSDT</div>
</div>
</div>
</div>
</main>
<!-- Footer Terminal -->
<footer class="fixed bottom-0 left-0 w-full z-50 h-8 flex items-center justify-between px-4 bg-[#11131c] border-t border-outline-variant/20">
<div class="flex items-center gap-6 overflow-hidden w-full">
<span class="text-[0.625rem] font-bold text-primary mono shrink-0">[v3.0.0] LIVE_FEED:</span>
<div id="live-feed" class="flex items-center gap-4 text-[0.625rem] mono text-outline overflow-x-auto w-full custom-scrollbar pb-1 pt-1">
    <span class="flex items-center gap-1 shrink-0"><span class="text-primary-fixed-dim">SYSTEM</span> INITIALIZING ENGINE WEBSOCKETS...</span>
</div>
</div>
<div class="flex items-center gap-4 border-l border-outline-variant/20 pl-4 shrink-0">
<div class="flex items-center gap-2">
<span id="stream-dot" class="w-1.5 h-1.5 bg-outline rounded-full"></span>
<span class="text-[0.625rem] text-outline uppercase">STREAMS</span>
</div>
<div class="flex items-center gap-2">
<span id="api-dot" class="w-1.5 h-1.5 bg-outline rounded-full"></span>
<span class="text-[0.625rem] text-outline uppercase">API</span>
</div>
<span class="text-[0.625rem] text-secondary font-bold mono">KINETIC_MONOLITH_OS</span>
</div>
</footer>

<script>
    // ── State ──────────────────────────────────────────────────────────
    let initialBalances = null;
    let currentBalances = {};
    const startTime = Date.now();

    // ── Uptime ticker ──────────────────────────────────────────────────
    setInterval(() => {
        const elapsed = Math.floor((Date.now() - startTime) / 1000);
        const h = String(Math.floor(elapsed / 3600)).padStart(2, '0');
        const m = String(Math.floor((elapsed % 3600) / 60)).padStart(2, '0');
        const s = String(elapsed % 60).padStart(2, '0');
        document.getElementById('uptime-display').innerText = `UPTIME: ${h}:${m}:${s}`;
    }, 1000);

    // ── WebSocket with auto-reconnect ──────────────────────────────────
    function connectWS() {
        const protocol = window.location.protocol === "https:" ? "wss://" : "ws://";
        const ws = new WebSocket(protocol + window.location.host + "/ws");

        ws.onopen = () => {
            const el = document.getElementById("ws-status-text");
            el.innerText = "TRADING";
            el.className = "text-[0.65rem] font-bold text-primary tracking-widest uppercase";
            document.getElementById("stream-dot").className = "w-1.5 h-1.5 bg-primary rounded-full";
            document.getElementById("api-dot").className = "w-1.5 h-1.5 bg-primary rounded-full";
            addLog("WebSocket connected to Rust Engine bridge.");
        };

        ws.onclose = () => {
            const el = document.getElementById("ws-status-text");
            el.innerText = "DISCONNECTED";
            el.className = "text-[0.65rem] font-bold text-error tracking-widest uppercase";
            document.getElementById("stream-dot").className = "w-1.5 h-1.5 bg-error rounded-full";
            addLog("WebSocket connection lost. Reconnecting...");
            setTimeout(connectWS, 3000);
        };

        ws.onmessage = (event) => {
            let data;
            try {
                data = JSON.parse(event.data);
            } catch (e) {
                addLog(event.data);
                return;
            }

            if (data.event === "AccountUpdate") {
                handleAccountUpdate(data);
            } else if (data.event === "OrderUpdate") {
                addLog(`ORDER ${data.status}: ${data.symbol} | Filled: ${data.filled_qty}`);
            } else if (data.event === "Connected") {
                addLog(`WS CONNECTED: ${data.symbol}`);
            } else if (data.event === "Disconnected") {
                addLog(`WS DISCONNECTED: ${data.symbol}`);
            } else if (data.event === "PositionPnL") {
                addLog(`PNL ${data.symbol}: $${data.unrealized_pnl.toFixed(2)} @ ${data.mark_price.toFixed(2)}`);
            }
        };
    }
    connectWS();

    // ── Account Update Handler ─────────────────────────────────────────
    function handleAccountUpdate(data) {
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

            let color = diff >= 0 ? "text-primary" : "text-error";
            let sign = diff > 0 ? '+' : '';

            pnlHtml += `
            <div class="flex justify-between items-center border-b border-outline-variant/5 pb-2">
                <div>
                    <div class="text-[0.75rem] font-bold text-secondary">${asset}</div>
                </div>
                <div class="text-right mono">
                    <div class="text-[0.75rem] font-bold">${bal.toFixed(4)}</div>
                    <div class="text-[0.625rem] ${color}">PNL: ${sign}${diff.toFixed(4)}</div>
                </div>
            </div>`;
        }

        let totalColorClass = totalPnl >= 0 ? "text-primary glow-green" : "text-error glow-red";
        let totalSign = totalPnl >= 0 ? '+' : '';

        const pnlEl = document.getElementById("total-pnl");
        pnlEl.innerText = `${totalSign}$${totalPnl.toFixed(2)}`;
        pnlEl.className = `text-2xl font-black mono ${totalColorClass}`;

        document.getElementById("balances-container").innerHTML = pnlHtml;
    }

    // ── REST Data Fetch ────────────────────────────────────────────────
    async function loadDashboardData() {
        try {
            const [posRes, statsRes, riskRes, tradesRes] = await Promise.all([
                fetch('/api/positions'),
                fetch('/api/stats'),
                fetch('/api/risk'),
                fetch('/api/trades?limit=20'),
            ]);
            const positions = await posRes.json();
            const stats = await statsRes.json();
            const risk = await riskRes.json();
            const trades = await tradesRes.json();

            renderPositions(positions);
            renderStats(stats);
            renderRisk(risk);
            renderTrades(trades);
            document.getElementById("api-dot").className = "w-1.5 h-1.5 bg-primary rounded-full";
        } catch (e) {
            // API not reachable yet, silently wait
        }
    }

    function renderPositions(positions) {
        const tbody = document.getElementById('arb-tbody');
        const indicator = document.getElementById('live-indicator');
        if (!positions || positions.length === 0) {
            tbody.innerHTML = '<tr class="border-b border-outline-variant/5"><td class="p-3 text-outline" colspan="7">No active positions. Waiting for trade signals...</td></tr>';
            indicator.innerHTML = '-- IDLE';
            indicator.className = 'text-[0.625rem] text-outline';
            return;
        }
        indicator.innerHTML = '<span class="text-primary">&#9679;</span> LIVE';
        indicator.className = 'text-[0.625rem] text-primary';

        let html = '';
        for (const p of positions) {
            const pnlColor = p.net_pnl_usd >= 0 ? 'text-primary' : 'text-error';
            const pnlSign = p.net_pnl_usd >= 0 ? '+' : '';
            const fundColor = p.ann_funding > 0 ? 'text-primary' : 'text-error';
            const statusClass = p.status === 'OPEN' ? 'bg-primary/10 text-primary border-primary/20' : 'bg-secondary/10 text-secondary border-secondary/20';

            html += `
            <tr class="border-b border-outline-variant/5 hover:bg-surface-container/30">
                <td class="p-3 font-bold text-secondary">${p.symbol.replace('USDT','')}/USDT</td>
                <td class="p-3">${p.qty.toFixed(4)} / ${p.spot_entry.toFixed(2)} / <span class="text-primary">${(p.spot_live || p.spot_entry).toFixed(2)}</span></td>
                <td class="p-3">-${p.qty.toFixed(4)} / ${p.perp_entry.toFixed(2)} / <span class="text-tertiary-container">${(p.perp_live || p.perp_entry).toFixed(2)}</span></td>
                <td class="p-3 ${fundColor}">${p.ann_funding >= 0 ? '+' : ''}${(p.ann_funding * 100).toFixed(1)}%</td>
                <td class="p-3">${p.basis_pct >= 0 ? '+' : ''}${(p.basis_pct * 100).toFixed(2)}%</td>
                <td class="p-3 ${pnlColor}">${pnlSign}$${p.net_pnl_usd.toFixed(2)}</td>
                <td class="p-3"><span class="px-2 py-0.5 ${statusClass} border text-[0.625rem]">${p.status}</span></td>
            </tr>`;
        }
        tbody.innerHTML = html;
    }

    function renderStats(stats) {
        // Total PnL
        const pnl = stats.total_pnl || 0;
        const pnlEl = document.getElementById('total-pnl');
        const sign = pnl >= 0 ? '+' : '';
        pnlEl.innerText = `${sign}$${pnl.toFixed(2)}`;
        pnlEl.className = `text-2xl font-black mono ${pnl >= 0 ? 'text-primary glow-green' : 'text-error glow-red'}`;

        const equity = stats.account_equity || 10000;
        const roePct = equity > 0 ? ((pnl / equity) * 100).toFixed(2) : '0.00';
        document.getElementById('total-pnl-pct').innerHTML = `${sign}${roePct}% <span class="text-outline">ROE</span>`;

        // Gross Exposure
        const exposure = stats.gross_exposure || 0;
        const maxExposure = stats.max_gross_exposure || 50000;
        const expK = (exposure / 1000).toFixed(1);
        const maxK = (maxExposure / 1000).toFixed(0);
        document.getElementById('gross-exposure').innerHTML = `$${expK}k <span class="text-outline-variant text-lg">/ $${maxK}k</span>`;
        const expPct = Math.min(100, (exposure / maxExposure) * 100);
        document.getElementById('exposure-bar').style.width = `${expPct}%`;

        // Sentiment
        const sentiment = stats.sentiment_score;
        if (sentiment !== undefined) {
            const sSign = sentiment >= 0 ? '+' : '';
            document.getElementById('sentiment-score').innerText = `${sSign}${sentiment.toFixed(2)}`;
            document.getElementById('sentiment-score').className = `text-2xl font-black mono ${sentiment > 0 ? 'text-secondary' : sentiment < -0.3 ? 'text-error' : 'text-on-surface'}`;
            document.getElementById('sentiment-label').innerText = sentiment > 0.3 ? 'BULLISH SKEW' : sentiment < -0.3 ? 'BEARISH SKEW' : 'NEUTRAL';
            // Bars
            const level = Math.min(6, Math.max(0, Math.round((sentiment + 1) * 3)));
            let barsHtml = '';
            for (let i = 0; i < 6; i++) {
                const opacity = i < level ? (i < 2 ? '30' : i < 4 ? '60' : '') : '';
                const color = i < level ? (opacity ? `bg-secondary/${opacity}` : 'bg-secondary') : 'bg-surface-container';
                barsHtml += `<div class="h-4 w-1 ${color}"></div>`;
            }
            document.getElementById('sentiment-bars').innerHTML = barsHtml;
        }

        // Win Rate
        const winRate = stats.win_rate;
        if (winRate !== undefined) {
            document.getElementById('win-rate').innerText = `${(winRate * 100).toFixed(1)}%`;
            document.getElementById('win-rate-sub').innerText = `${Math.round(stats.trade_count || 0)} TRADES`;
        }

        // Total Trades
        const trades = stats.trade_count || 0;
        document.getElementById('total-trades').innerText = trades.toLocaleString();

        // Ann. Funding
        const funding = stats.ann_funding;
        if (funding !== undefined) {
            const fEl = document.getElementById('ann-funding-display');
            fEl.innerText = `${funding >= 0 ? '+' : ''}${(funding * 100).toFixed(2)}%`;
            fEl.className = `text-3xl font-black mono ${funding > 0.06 ? 'text-primary glow-green' : funding > 0 ? 'text-secondary' : 'text-error glow-red'}`;
        }
    }

    function renderRisk(risk) {
        // Spread toxicity
        const toxicity = risk.spread_toxicity;
        if (toxicity !== undefined) {
            const tEl = document.getElementById('spread-toxicity');
            tEl.innerText = `${toxicity} bps`;
            tEl.className = `text-lg font-black mono ${toxicity > 30 ? 'text-tertiary-container' : 'text-primary'}`;
            document.getElementById('spread-toxicity-label').innerText = toxicity > 30 ? 'ELEVATED' : 'NORMAL';
        }

        // Venue latency
        const latency = risk.venue_latency;
        if (latency !== undefined) {
            const lEl = document.getElementById('venue-latency');
            lEl.innerText = `${latency} ms`;
            lEl.className = `text-lg font-black mono ${latency > 200 ? 'text-tertiary-container' : 'text-primary'}`;
            document.getElementById('venue-latency-label').innerText = latency > 200 ? 'DEGRADED' : 'STABLE';
            document.getElementById('latency-display').innerText = `${latency}ms LATENCY`;
        }

        // Drawdown
        const dd = risk.drawdown_pct;
        if (dd !== undefined) {
            const ddPct = (dd * 100).toFixed(1);
            document.getElementById('drawdown-text').innerText = `${ddPct}% / 10%`;
            const barPct = Math.min(100, dd * 1000); // 10% max = 100% bar
            const bar = document.getElementById('drawdown-bar');
            bar.style.width = `${barPct}%`;
            bar.className = dd > 0.08 ? 'bg-error h-full' : dd > 0.04 ? 'bg-tertiary-container h-full' : 'bg-secondary h-full';
        }

        // Risk reasons
        const reasons = risk.reasons;
        const reasonsEl = document.getElementById('risk-reasons');
        if (reasons && Array.isArray(reasons) && reasons.length > 0) {
            reasonsEl.innerHTML = reasons.map(r => `<div class="text-tertiary-container">&#9888; ${r}</div>`).join('');
        } else if (risk.kill_switch) {
            reasonsEl.innerHTML = '<div class="text-error font-bold">KILL SWITCH ACTIVE</div>';
        } else {
            reasonsEl.innerHTML = '<div class="text-primary">No active risk flags.</div>';
        }
    }

    function renderTrades(trades) {
        const tbody = document.getElementById('trades-tbody');
        if (!trades || trades.length === 0) {
            tbody.innerHTML = '<tr class="border-b border-outline-variant/5"><td class="p-3 text-outline" colspan="7">No trade history yet.</td></tr>';
            return;
        }
        let html = '';
        for (const t of trades) {
            const pnlColor = t.net_pnl_usd >= 0 ? 'text-primary' : 'text-error';
            const pnlSign = t.net_pnl_usd >= 0 ? '+' : '';
            html += `
            <tr class="border-b border-outline-variant/5 hover:bg-surface-container/30">
                <td class="p-3 font-bold text-secondary">${t.symbol}</td>
                <td class="p-3">${t.side}</td>
                <td class="p-3">$${t.entry_price.toFixed(2)}</td>
                <td class="p-3">$${t.exit_price.toFixed(2)}</td>
                <td class="p-3">${t.qty.toFixed(4)}</td>
                <td class="p-3 ${pnlColor}">${pnlSign}$${t.net_pnl_usd.toFixed(2)}</td>
                <td class="p-3 text-secondary">${pnlSign}$${(t.funding_collected || 0).toFixed(4)}</td>
            </tr>`;
        }
        tbody.innerHTML = html;
    }

    // ── Log Helper ─────────────────────────────────────────────────────
    function addLog(msg) {
        const feed = document.getElementById("live-feed");
        const time = new Date().toLocaleTimeString('en-US', {hour12: false});

        const logSpan = document.createElement("span");
        logSpan.className = "flex items-center gap-1 shrink-0";
        logSpan.innerHTML = `<span class="text-primary-fixed-dim">${time}</span> ${msg}`;

        const sep = document.createElement("span");
        sep.className = "text-outline-variant shrink-0";
        sep.innerText = "|";

        feed.prepend(sep);
        feed.prepend(logSpan);

        while (feed.children.length > 80) {
            feed.removeChild(feed.lastChild);
        }
    }

    // ── Init + Polling ─────────────────────────────────────────────────
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
