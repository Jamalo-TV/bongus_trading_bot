# Bongus Trading Bot

Delta-neutral funding arbitrage on Binance with a Python decision engine and a Rust execution engine.

The bot ranks symbols by expected funding edge, filters them by liquidity and portfolio constraints, and enters hedged spot/perp positions. It is designed to run in `paper`, `testnet`, or `live` mode, with `paper` using live Binance market data but no real orders.

## What It Does

- Monitors a basket of Binance symbols for positive or negative funding opportunities.
- Sizes positions across multiple slots instead of trading a single pair.
- Rotates out of weaker opportunities when the expected improvement pays back trading friction.
- Uses a Rust execution engine for market data, order handling, and low-latency IPC.
- Persists positions, trades, risk snapshots, and append-only execution events to SQLite.

## Architecture

- Python brain: signal generation, portfolio allocation, risk checks, funding ranking, dashboard state.
- Rust engine: WebSocket market data, order state machine, user-data stream handling, telemetry broadcast.
- Python -> Rust: ZMQ PUSH on `tcp://127.0.0.1:5555`
- Rust -> Python/dashboard: TCP broadcast on `127.0.0.1:9000`
- Shared persistence: SQLite `state.db`

## Modes

- `paper`: live Binance market data, synthetic fills, no real exchange orders.
- `testnet`: Binance futures testnet execution.
- `live`: real execution with real keys.

## Quick Start

```powershell
# Windows
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Python tests
pytest tests -q

# Rust tests
cd execution_engine
cargo test
cd ..
```

## Run The System

```powershell
# Full supervised stack
python bongus/monitoring/king_watchdog.py

# Trader only
python scripts/live_trader_v2.py

# Dashboard only
uvicorn bongus.monitoring.web_dashboard:app --host 127.0.0.1 --port 8080
```

## Current Foundations

- Multi-symbol funding ranking with configurable slot allocation.
- Exit-first rotation invariant.
- Liquidity-aware entry and exit gating.
- Portfolio circuit breaker and watchdog supervision.
- Execution-event ledger with fill metadata for attribution and replay.
- Dashboard that reads persisted state instead of wiping it on startup.

## Testing Notes

- The Python suite expects the packages in `requirements.txt`, including `feedparser`.
- `paper` mode is the safest way to test the full application against live Binance market data.
- If you are changing execution or state handling, run both the Python and Rust suites.

## Important Files

- `scripts/live_trader_v2.py`
- `bongus/engine/state_store.py`
- `bongus/monitoring/web_dashboard.py`
- `bongus/portfolio/portfolio_allocator.py`
- `bongus/market_data/funding_ranker.py`
- `execution_engine/src/order_manager.rs`
- `execution_engine/src/user_data_ws.rs`

## Safety

- This is still trading software. Even in a hedged strategy, execution quality, basis shocks, borrow costs, and bad reconciliation can hurt badly.
- Treat `paper` mode as mandatory before any `testnet` or `live` promotion.
- Keep API keys out of source control and use `.env` or environment variables.

See [HOW_IT_WORKS.md](HOW_IT_WORKS.md) for a deeper system walkthrough.
