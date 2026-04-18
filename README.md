# Bongus Trading Bot

Bongus is a delta-neutral Binance funding arbitrage bot built around one canonical live runtime:

- `scripts/live_trader.py` is the only manual trader entrypoint.
- `bongus/monitoring/king_watchdog.py` is the supervised system entrypoint.
- The Python runtime scans a wide Binance USDT-perp universe, ranks opportunities by net edge after costs, sizes tightly, and sends intents to the Rust execution engine.
- The Rust engine owns exchange connectivity and execution; Python owns scanning, ranking, portfolio construction, governance, and observability.

## What Changed

The old runtime split between a legacy single-symbol trader and a newer multi-symbol path is gone. The repo now uses a package-backed canonical trader service under `bongus/runtime/live_trader.py`, with hot-reloaded runtime config in `live_config.json` and shared state persisted through `bongus/engine/state_store.py`.

## Quick Start

1. Create and activate the virtual environment.
   ```bash
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```
2. Build the Rust engine.
   ```bash
   cd execution_engine
   cargo build --release
   cd ..
   ```
3. Set environment variables.
   - `BINANCE_API_KEY`
   - `BINANCE_API_SECRET`
   - `TRADING_MODE=paper|testnet|live`
4. Run the supervised stack.
   ```bash
   python bongus/monitoring/king_watchdog.py
   ```

## Common Commands

- Canonical trader only:
  ```bash
  python scripts/live_trader.py
  ```
- Dashboard only:
  ```bash
  uvicorn bongus.monitoring.web_dashboard:app --host 0.0.0.0 --port 8080
  ```
- Backtest / analytics:
  ```bash
  python scripts/backtest.py --enhanced-report
  ```
- Walk-forward validation and governance:
  ```bash
  python -c "from scripts.walk_forward import run_walk_forward_validation"
  ```
- Python tests:
  ```bash
  pytest tests
  ```
- Type checking:
  ```bash
  pyright
  ```
- Rust tests:
  ```bash
  cd execution_engine && cargo test
  ```

## Runtime Responsibilities

- Scanner: requires a liquid Binance spot + USDT perpetual pair, then rejects stale, thin, toxic, wide, or newly listed symbols before ranking.
- Ranker: scores candidates with winsorized percentile components and shared fee/slippage assumptions from `bongus/engine/cost_model.py`.
- Allocator: selects only the best top-N names, applies per-symbol and cluster caps, and sizes from depth, volatility, and gross-budget limits.
- Execution feedback: stores execution-quality samples, pending intents, and order events for auditability.
- Governance: writes validation snapshots and parameter promotions, and can auto-promote approved overrides into `live_config.json`.
- Shadow exits: records minute-level feature snapshots and shadow-mode hold-vs-exit recommendations without controlling live exits.

## Observability

`state.db` is the canonical runtime ledger. It now persists:

- positions, trades, portfolio stats, and risk state
- candidate snapshots and opportunity scores
- market samples and health samples
- execution events, pending intents, and execution-quality samples
- feature snapshots, shadow decisions, validation snapshots, and parameter promotions

The FastAPI dashboard exposes these through `/api/*` endpoints so every cycle is auditable.

## Safety

- New risk fails closed when telemetry is stale or risk limits are breached.
- Rotations remain exit-first.
- The bot does not trade every qualifying symbol.
- ML exit logic stays in shadow mode until explicitly promoted.

## Recovery Playbook

When new entries are blocked, check `risk.safe_mode_reason` first.

- Per-symbol guards only (`startup_manual_review`, `hedge_gap`, `startup_exit_candidate`, `naked_leg_unwind_stuck`): runtime enters `LIVE_WITH_SYMBOL_BLOCKS`; only flagged symbols are blocked, other symbols continue trading.
- Portfolio guard (`risk_limits`): drawdown limits are active and new risk stays blocked portfolio-wide.

Operator actions:

- For startup manual review symbols: flatten on Binance when ready, or acknowledge via supervisor/Telegram (`/acknowledge <SYMBOL>`) if the symbol is eligible.
- For drawdown lock: use one-cycle `reset_equity_high_watermark: true` (manual reset), or opt into auto-heal with:
  - `hwm_auto_decay_after_hours`
  - `hwm_auto_decay_fraction`

Defaults keep prior behavior (`hwm_auto_decay_after_hours = 0.0`, disabled).

See `HOW_IT_WORKS.md` for the full runtime flow.
