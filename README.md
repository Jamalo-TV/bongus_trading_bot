# Bongus Trading Bot

Delta-neutral funding arbitrage on Binance perpetual futures. The bot earns the net carry from funding payments while staying fully hedged across spot and perp legs. It monitors up to 30 symbols, holds up to 4 concurrent slots, and rotates into better opportunities when the improvement pays back trading friction fast enough.

---

## Strategy in Brief

Binance perpetual futures pay or charge a funding rate every 8 hours. When the rate is positive, longs pay shorts. The bot exploits this by:

1. **Buying spot** and **shorting the perp** on the same symbol — the position is delta-neutral (no directional exposure).
2. Collecting funding every 8 hours from the perp short.
3. **Rotating** to a better-paying symbol when the rate gap justifies the round-trip friction, using a payback-period gate (`ROTATION_MAX_PAYBACK_DAYS = 0.333`, i.e. 8 hours).

Position sizing uses half-Kelly criterion, recalculated hourly from recent trade history. Leverage scales with funding rate magnitude (configurable tiers in `config.py`).

---

## Architecture

```
┌─────────────────────────────────┐     ZMQ PUSH :5555    ┌────────────────────────────┐
│  Python Brain (live_trader_v2)  │ ─────────────────────► │  Rust Execution Engine     │
│                                 │                         │  (execution_engine/)       │
│  • FundingRanker                │ ◄───────────────────── │                            │
│  • DepthTracker                 │    TCP broadcast :9000  │  • Binance WS (depth/mark) │
│  • PortfolioAllocator (Kelly)   │                         │  • Order state machine     │
│  • RiskEngine                   │                         │  • User-data stream        │
│  • StateWriter (SQLite)         │                         │  • Paper fill simulation   │
└────────────┬────────────────────┘                         └────────────────────────────┘
             │ SQLite (state.db)
             ▼
┌─────────────────────────────────┐
│  FastAPI Dashboard (:8080)      │
│  REST + WebSocket               │
└─────────────────────────────────┘
```

**IPC channels:**
- Python → Rust: ZMQ PUSH `tcp://127.0.0.1:5555`, msgpack-encoded order intents
- Rust → Python/dashboard: TCP broadcast `127.0.0.1:9000`, newline-delimited JSON (`L2Depth`, `OrderUpdate`, `MarkPrice`)

**Process supervisor:** `king_watchdog.py` launches and monitors all four processes (Rust engine, Python trader, sentiment scraper, dashboard). It restarts any process that crashes or exceeds 1 GB RAM.

---

## Prerequisites

| Dependency | Version | Notes |
|---|---|---|
| Python | 3.10+ | Polars requires 3.9+; 3.10 recommended |
| Rust + Cargo | stable (1.75+) | `rustup install stable` |
| libzmq | any | Required by `pyzmq`; usually auto-installed |
| Binance account | — | API keys with spot + futures permissions |

---

## Server Setup (Linux VPS)

Tested on Ubuntu 22.04. Run as a non-root user.

### 1. System packages

```bash
sudo apt update && sudo apt install -y \
  python3.10 python3.10-venv python3-pip \
  build-essential pkg-config libzmq3-dev \
  git curl
```

### 2. Rust toolchain

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source "$HOME/.cargo/env"
rustup update stable
```

### 3. Clone and set up Python environment

```bash
git clone https://github.com/Jamalo-TV/bongus_trading_bot.git
cd bongus_trading_bot

python3.10 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 4. Build the Rust engine

```bash
cd execution_engine
cargo build --release
cd ..
```

The binary lands at `execution_engine/target/release/execution_engine`. The watchdog finds it automatically.

### 5. Create your `.env` file

```bash
cp .env.example .env   # if present, otherwise create manually
nano .env
```

Minimum required contents:

```env
TRADING_MODE=paper

# Futures keys (required for testnet/live; optional for paper)
BINANCE_API_KEY=your_futures_api_key
BINANCE_API_SECRET=your_futures_api_secret

# Spot keys — defaults to futures keys if not set
BINANCE_SPOT_API_KEY=your_spot_api_key
BINANCE_SPOT_API_SECRET=your_spot_api_secret

# Capital parameters
ACCOUNT_EQUITY_USD=10000
MAX_GROSS_EXPOSURE_USD=50000
```

> **Never commit `.env` to source control.** It is already in `.gitignore`.

### 6. Run with systemd (recommended for production)

Create `/etc/systemd/system/bongus.service`:

```ini
[Unit]
Description=Bongus Trading Bot
After=network.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER/bongus_trading_bot
EnvironmentFile=/home/YOUR_USER/bongus_trading_bot/.env
ExecStart=/home/YOUR_USER/bongus_trading_bot/.venv/bin/python bongus/monitoring/king_watchdog.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable bongus
sudo systemctl start bongus
sudo journalctl -u bongus -f   # follow logs
```

### 7. Open the dashboard port (optional)

```bash
sudo ufw allow 8080/tcp   # or use a reverse proxy (nginx/caddy)
```

Then visit `http://YOUR_SERVER_IP:8080`.

---

## Local Development Setup

```bash
# Windows
.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Linux/macOS
source .venv/bin/activate
pip install -r requirements.txt

# Build Rust engine
cd execution_engine && cargo build --release && cd ..

# Run tests
pytest tests/ -q           # Python suite (169 tests)
cd execution_engine && cargo test   # Rust suite
```

---

## Configuration

### Static parameters — `bongus/core/config.py`

Edit directly for permanent changes. Key constants:

| Constant | Default | Description |
|---|---|---|
| `CAPITAL_PER_SLOT_USD` | 2500 | Base capital per slot before Kelly and leverage |
| `MAX_CONCURRENT_POSITIONS` | 4 | Max open slots |
| `TARGET_LEVERAGE` | 2.0 | Default leverage |
| `MAX_LEVERAGE` | 3.0 | Hard cap |
| `LEVERAGE_TIERS` | see file | Rate → leverage mapping |
| `ROTATION_MIN_GAP_ANN` | 0.20 | Min annualized rate gap to trigger rotation |
| `ROTATION_MAX_PAYBACK_DAYS` | 0.333 | Max payback period (8 hours) |
| `LIQUIDITY_FILTER_MULTIPLIER` | 5.0 | Entry depth must be ≥ 5× notional |
| `MIN_FUNDING_RATE_ANN` | 0.05 | Min rate to consider entering |

### Runtime overrides — `live_config.json`

Drop a JSON file in the project root. `ConfigManager` picks it up within 30 seconds, no restart needed. Only keys that exist in `config.py` are accepted.

Example:

```json
{
  "ROTATION_MIN_GAP_ANN": 0.30,
  "MAX_CONCURRENT_POSITIONS": 2
}
```

### Environment variables — `.env`

| Variable | Default | Description |
|---|---|---|
| `TRADING_MODE` | `paper` | `paper`, `testnet`, or `live` |
| `BINANCE_API_KEY` | — | Futures API key |
| `BINANCE_API_SECRET` | — | Futures API secret |
| `BINANCE_SPOT_API_KEY` | falls back to futures key | Spot API key |
| `BINANCE_SPOT_API_SECRET` | falls back to futures secret | Spot API secret |
| `ACCOUNT_EQUITY_USD` | 10000 | Starting equity for risk calculations |
| `MAX_GROSS_EXPOSURE_USD` | 50000 | Hard gross exposure cap |
| `MONITORED_SYMBOLS` | all Binance perps (top 30) | Comma-separated override, e.g. `BTCUSDT,ETHUSDT` |

---

## Running the System

### Full supervised stack (recommended)

```bash
PYTHONPATH=/path/to/bongus_trading_bot python bongus/monitoring/king_watchdog.py
```

The watchdog starts: Rust engine → Python trader → sentiment scraper → FastAPI dashboard.

### Individual components

```bash
# Python trader only (no watchdog)
python scripts/live_trader_v2.py

# Dashboard only (reads persisted state.db)
uvicorn bongus.monitoring.web_dashboard:app --host 0.0.0.0 --port 8080
```

---

## Operating Modes

| Mode | Market data | Orders | Use case |
|---|---|---|---|
| `paper` | Live Binance mainnet | Synthetic fills in Rust | End-to-end dry run — always start here |
| `testnet` | Binance testnet | Real testnet orders | API path validation |
| `live` | Live Binance mainnet | Real orders | Production |

> Start in `paper` mode. Validate behavior, PnL attribution, and rotation logic before promoting to `testnet` or `live`.

---

## Dashboard

FastAPI app at `http://localhost:8080`. Endpoints:

| Endpoint | Description |
|---|---|
| `GET /api/positions` | Open positions with notional, funding, PnL |
| `GET /api/stats` | Aggregated stats (total PnL, win rate, etc.) |
| `GET /api/risk` | Current risk state, mode, drawdown |
| `GET /api/trades?limit=N` | Recent trade history |
| `GET /api/pnl-attribution` | PnL breakdown by symbol |
| `WS /ws` | Live telemetry stream from Rust engine |

The dashboard reads from `state.db` and does not wipe it on startup.

---

## Risk and Safety

**Built-in protections:**
- **Liquidity gate** — entry depth must be ≥ 5× notional; no entering thin books
- **Exit-first invariant** — rotation never flips a position atomically; exit must confirm `FILLED` before the entry is dispatched
- **Soft drawdown** — `RiskEngine` halves position scale when drawdown exceeds threshold
- **Kill switch** — hard stop at configurable max drawdown; exits all positions
- **Correlation breaker** — halts entries when ≥50% of open positions are negative; emergency exit at 100%
- **Kelly sizing guard** — when recent trade history shows no positive edge (Kelly ≤ 0), all new entries are suppressed
- **Watchdog** — restarts crashed or memory-bloated processes; BLOCKED mode requires manual operator intervention

**Operator warnings:**
- This is real trading software. Even a hedged strategy is exposed to basis shocks, liquidation cascades, borrow cost spikes, and execution quality degradation.
- Keep API keys out of source control. Use `.env` or environment injection.
- Treat every `testnet` and `live` promotion as a risk event. Validate paper mode first.
- If the trader enters `BLOCKED` mode at startup, it will not restart automatically. Investigate `state.db` and the watchdog logs before clearing the block.

---

## Key Files

| File | Purpose |
|---|---|
| `scripts/live_trader_v2.py` | Main live trading loop |
| `bongus/core/config.py` | All tunable constants |
| `bongus/core/config_manager.py` | Hot-reload of `live_config.json` |
| `bongus/market_data/funding_ranker.py` | Symbol ranking by funding edge |
| `bongus/market_data/depth_tracker.py` | Spot/perp liquidity tracking |
| `bongus/portfolio/portfolio_allocator.py` | Kelly sizing, slot management, rotation |
| `bongus/portfolio/correlation_breaker.py` | Portfolio-level circuit breaker |
| `bongus/engine/risk_engine.py` | Drawdown and kill-switch logic |
| `bongus/engine/state_store.py` | SQLite persistence + execution-event ledger |
| `bongus/monitoring/king_watchdog.py` | Process supervisor — system entry point |
| `bongus/monitoring/web_dashboard.py` | FastAPI dashboard |
| `execution_engine/src/order_manager.rs` | Order state machine |
| `execution_engine/src/binance_ws.rs` | WebSocket market data |
| `execution_engine/src/ipc.rs` | ZMQ intake + TCP broadcast |
