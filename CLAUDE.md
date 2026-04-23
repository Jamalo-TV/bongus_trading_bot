# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A delta-neutral funding arbitrage bot targeting Binance perpetual futures. The strategy captures funding payments by holding a long spot / short perp position (or vice versa) across up to 4 symbols simultaneously, rotating to higher-yield symbols when the rate gap justifies friction costs.

## Commands

```bash
# Python environment
.venv\Scripts\activate        # activate venv (Windows)
pip install -r requirements.txt

# Tests
pytest tests/                          # run all tests
pytest tests/test_strategy.py          # single file
pytest tests/test_risk_engine.py::test_kill_switch  # single test

# Type checking
pyright

# Rust execution engine
cd execution_engine && cargo run --release   # build + run
cd execution_engine && cargo build --release # build only
cd execution_engine && cargo test            # Rust tests

# Run the full system (supervisor starts all processes)
python bongus/monitoring/king_watchdog.py

# Run Python trader directly (without watchdog)
python scripts/live_trader_v2.py

# Dashboard only
uvicorn bongus.monitoring.web_dashboard:app --host 0.0.0.0 --port 8080
```

## Architecture

The system is split into a Python brain and a Rust execution engine that communicate over two IPC channels:

- **Python → Rust**: ZMQ PUSH on `tcp://127.0.0.1:5555`, msgpack-encoded order intents
- **Rust → Python**: TCP broadcast on port `9000`, newline-delimited JSON events (`L2Depth`, `OrderUpdate`, `MarkPrice`)

### Process Supervision

`bongus/monitoring/king_watchdog.py` is the entry point for the full system. It launches and monitors four child processes: Rust engine, Python live trader, sentiment scraper, and FastAPI dashboard. It restarts any process that crashes or exceeds 1 GB RAM.

### Python Package (`bongus/`)

| Package | Purpose |
|---|---|
| `core/config.py` | All tunable constants (thresholds, fees, limits) — single source of truth |
| `core/config_manager.py` | Hot-reloads `live_config.json` every 30 s without restart; falls back to `config.py` |
| `engine/risk_engine.py` | `RiskEngine` evaluates `RiskState` → `RiskDecision`; handles soft drawdown (scale ×0.5), kill switch |
| `engine/state_store.py` | SQLite-backed `StateWriter`/`StateReader`; shared state plus append-only execution-event ledger |
| `engine/cost_model.py` | Blended entry/exit cost with maker-fill probability and depth-scaled slippage |
| `engine/execution_alpha.py` | Order routing simulation: fill probability, expected cost, `RustIPCBridge` |
| `ipc/execution.py` | `ExecutionClient` — ZMQ PUSH socket to Rust, 500 ms send timeout |
| `ipc/telemetry.py` | `TelemetryClient` — async TCP reader, yields JSON events |
| `market_data/funding_ranker.py` | Fetches all funding rates in one REST call; updated sub-minute by WS `MarkPrice` events |
| `market_data/depth_tracker.py` | Tracks L2 depth per symbol for liquidity filtering |
| `market_data/rust_data_subscriber.py` | Async TCP client for Rust port 9000; dispatches callbacks to depth/order/price handlers |
| `portfolio/portfolio_allocator.py` | Slot management, liquidity filter (5× notional), rotation decisions |
| `portfolio/correlation_breaker.py` | Portfolio-level circuit breaker: HALTED (≥50% negative), EMERGENCY (100%) |
| `monitoring/web_dashboard.py` | FastAPI app; REST + WebSocket endpoints; reads persisted state and proxies Rust telemetry |
| `strategies/strategy.py` | Core signal generation on Polars DataFrames |
| `strategies/multi_symbol_runner.py` | Runs `strategy.py` per symbol, combines results with globally unique `trade_id` |

- **State Persistence**: Order updates and execution events are queued in an `asyncio.Queue` and persisted to SQLite by a background worker to avoid blocking the hot callback path.
- **DB Maintenance**: Periodic archival (to `archive.db`) and retention policies for market/health samples keep the main state footprint lean. WAL checkpoints and VACUUM run during daily maintenance.

### Main Live Trader (`scripts/live_trader_v2.py`)

`LiveTraderV2` wires everything together:
1. `RustDataSubscriber` feeds depth, order fills, and mark prices into in-memory caches
2. `FundingRanker` polls Binance REST every 60 s; WS mark-price events update it sub-minute
3. `PortfolioAllocator.decide()` returns enter/exit/hold/rotation decisions each cycle
4. Exits are dispatched first via `ExecutionClient`; rotation entries wait for FILLED confirmation (or 10 s timeout) before entering the new symbol
5. `StateWriter` persists positions, stats, and trades to SQLite

### Rust Engine (`execution_engine/src/`)

| Module | Purpose |
|---|---|
| `main.rs` | Tokio runtime; wires `mpsc` channels for WS events, Alpha IPC, and dashboard broadcast |
| `binance_ws.rs` | WS connection manager for L2 depth and mark price streams |
| `binance_rest.rs` | REST API (order placement, exchange info) |
| `order_manager.rs` | Order state machine; handles FILLED/CANCELLED/EXPIRED transitions |
| `ipc.rs` | ZMQ PULL listener on port 5555; TCP broadcast server on port 9000 |
| `user_data_ws.rs` | User data stream for order updates |
| `collateral_engine.rs` | Collateral and margin tracking |

## Configuration

**Static parameters** live in `bongus/core/config.py` — edit here for permanent changes.

**Runtime overrides** go in `live_config.json` (project root). `ConfigManager` picks them up within 30 s. Only keys defined in `config.py` are accepted.

**Environment variables** (`.env` file or shell):
- `BINANCE_API_KEY`, `BINANCE_API_SECRET`
- `TRADING_MODE`: `paper` (default, no real orders), `testnet`, or `live`

## Key Design Constraints

- **Polars, not pandas** — all strategy DataFrames use Polars
- **Multi-symbol** — 8 monitored symbols, max 4 concurrent slots, $2,500/slot, 2× leverage = $5k notional each
- **Rotation payback gate** — a swap only triggers if friction pays back within 8 hours (`ROTATION_MAX_PAYBACK_DAYS = 0.333`)
- **Funding annualization** — raw funding rate × 1095 (3 settlements/day × 365) throughout the codebase
- **Exit-first invariant** — in rotation, the exit order must confirm FILLED before the entry is dispatched; never flip a position in a single atomic call

## Recent Foundations

- Order updates now carry richer execution metadata, including fill-price, commission, and execution-type fields when available.
- SQLite persists append-only execution events for attribution, debugging, and future replay work.
- The dashboard reads state without wiping the database on startup.
