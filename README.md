# Bongus Trading Bot

Bongus is a delta-neutral Binance funding arbitrage bot. The currently supervised production path is:

- `scripts.live_trader_v2` is the single canonical live trader module started by the watchdog.
- `bongus/monitoring/king_watchdog.py` is the supervised system entrypoint.
- The Python runtime scans a wide Binance USDT-perp universe, applies the governed legacy selector, and records settlement-aware lower-confidence-bound net-EV and portfolio alternatives in shadow.
- The Rust engine owns exchange connectivity and execution; Python owns scanning, ranking, portfolio construction, governance, and observability.

## What Changed

Watchdog production startup is pinned to `python -m scripts.live_trader_v2`.
The root `live_trader_v2.py` is only a compatibility delegate, and the
package-backed `bongus/runtime/live_trader.py` is not a production entry point.

## Quick Start

1. Create and activate the virtual environment.
   ```bash
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.lock
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
4. Run the supervised stack inside tmux.
   ```bash
   tmux new -s bongus
   python bongus/monitoring/king_watchdog.py
   ```

## Common Commands

- Live trader only:
  ```bash
  python -m scripts.live_trader_v2
  ```
- Dashboard only:
  ```bash
  uvicorn bongus.monitoring.web_dashboard:app --host 127.0.0.1 --port 8080
  ```

  The dashboard denies every HTTP and WebSocket request unless either
  `BONGUS_VIEWER_USERNAME` plus `BONGUS_VIEWER_PASSWORD` (or
  `BONGUS_VIEWER_PASSWORD_SHA256`) or complete `BONGUS_ADMIN_*` credentials
  are configured. Keep the loopback bind unless an authenticated reverse
  proxy and network policy are in place.
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
  cd execution_engine && cargo test --locked
  ```

- Master-plan implementation and promotion-gate verification:
  ```bash
  python scripts/verify_masterplan.py --run-local-checks
  ```
- Seeded million-trace execution fault gate:
  ```bash
  python scripts/run_execution_fault_campaign.py --traces 1000000 --workers 4 --output verification_artifacts/phase1_fault_campaign.json
  ```

`requirements.txt` is the human-edited dependency input. Use
`requirements.lock` for clean-room, CI, and deployment installs. See
[`BUILD.md`](BUILD.md) for lock regeneration and the complete validation
commands.

## Runtime Responsibilities

- Scanner: requires a liquid Binance spot + USDT perpetual pair, then rejects stale, thin, toxic, wide, or newly listed symbols before ranking.
- Ranker: preserves the governed active selector while persisting discrete-settlement forecasts and conservative LCB net-EV decompositions for comparison.
- Allocator: enforces active static caps while a reservation-, capacity-, factor-, settlement-cluster-, CVaR-, and stress-aware optimizer emits shadow allocations.
- Execution: sends durable, versioned, idempotent intents. Ambiguous signed
  order submissions query the deterministic client ID twice before one
  same-ID retry; route labels and hedge budgets are immutable command
  semantics, and unpromoted route policies fail closed.
- Execution feedback: stores normalized economic fills, pending intents, order events, and idempotent post-fill markouts. Cost calibration is measurement-only.
- Control plane: Python sends one canonical effective-config snapshot through
  the durable command journal; testnet/live entries require a matching typed
  Rust `ConfigAck`. Reloads revoke eligibility until the new hash is applied.
- Private execution recovery: spot and futures user streams independently
  replay bot-owned order/trade history from append-only cursors with a 24-hour
  rewind. Rust reconciles both venues and account snapshots before emitting
  execution readiness; either stream gap revokes eligibility immediately.
- Telemetry overflow is explicit rather than lossy: Rust sends the lagging
  client a `TelemetryGap`, closes that transport boundary, revokes execution
  readiness, and requests both private streams to replay their durable cursors
  before two-venue reconciliation can restore readiness.
- Exchange filters refresh off the Rust actor every five minutes. Entries
  reject missing or 15-minute-stale metadata; a tick, lot, notional, or
  trading-status change on an active chase preserves its evidence and revokes
  readiness until reconciliation.
- Signed REST waits do not stop private execution truth. While a bounded REST
  future is pending, the Rust actor continues applying fills and legging
  deadlines; if a fill advances the chase, any stale planned peer submission
  is suppressed so recovery cannot create a duplicate hedge.
- Account statements: paginated futures income and margin-interest history are
  journaled independently of fills. Funding, transfers, and borrow interest
  post Decimal cashflows; commission and realized PnL remain match-required
  evidence so they cannot be counted twice.
- Exact settlement eligibility is represented as filled-position windows plus
  point-in-time rate revisions. Entry at/after settlement earns nothing, late
  sign reversals replace earlier previews, and expected funding is never
  promoted to realized cash without an exchange statement identity.
- Capital reservations distinguish quote cash from symbol-specific spot-borrow
  capacity. An inverse entry requires a fresh authoritative borrowability proof;
  absent, stale, or exhausted proof fails closed. Autonomous inverse selection
  remains disabled.
- Governance: accepts only preregistered experiment results and never promotes capital, leverage, routes, models, or venue execution without their evidence gate.
- Recovery: scopes market-data gaps per symbol/source, treats either private
  account stream as a global execution-truth dependency, keeps unrelated
  orders untouched, and leaves treasury transfers proposal-only.

## Observability

`state.db` is the canonical runtime ledger. It now persists:

- positions, trades, portfolio stats, and risk state
- candidate snapshots and opportunity scores
- market samples and health samples
- append-only economic events with exchange identities and Decimal projections
- immutable exchange-statement rows and monotonic source cursors
- execution outbox/ACK state, lifecycle events, pending intents, and idempotent execution-quality markouts
- feature snapshots, shadow decisions, validation snapshots, and parameter promotions

The FastAPI dashboard exposes these through `/api/*` endpoints so every cycle is auditable.

## Safety

- New risk fails closed when telemetry is stale or risk limits are breached.
- Testnet/live new risk also fails closed when Python and Rust do not agree on
  the exact effective config hash or authoritative statement history is
  unavailable/unmapped. Exits and verified repair paths remain available.
- Testnet/live readiness additionally requires complete spot+futures private
  replay and a successful Rust two-venue order/account reconciliation.
- Rotations remain exit-first.
- The bot does not trade every qualifying symbol.
- ML exit logic stays in shadow mode until explicitly promoted.
- New entries are currently paused in `live_config.json`; per-trade/per-symbol exposure remains $2,500 and gross exposure remains $10,000.
- Settlement-aware ranking, adaptive routing, portfolio optimization, strategy plugins, multi-venue monitoring, and treasury actions cannot self-promote.
- The former Spot Testnet dust sweeper is permanently retired. Its compatibility
  entry point, direct function, legacy enable flag, and watchdog path cannot
  place orders. Treasury remains reconciliation-bound and proposal-only.

## Recovery Playbook

When new entries are blocked, check `risk.safe_mode_reason` and `risk.safe_mode_codes` first.

- Per-symbol guards only (`startup_manual_review`, `hedge_gap`, `startup_exit_candidate`, `naked_leg_unwind_stuck`, `stale_pending_intent`, `exit_failure`): runtime enters `LIVE_WITH_SYMBOL_BLOCKS`; only flagged symbols are blocked, other symbols continue trading.
- Portfolio guard (`risk_limits`): drawdown limits are active and new risk stays blocked portfolio-wide.
- Structured codes include `code`, `scope`, `recoverable`, and `next_action` so supervisor/dashboard logic can distinguish retry, wait, exit, restore, and operator-required states.

Operator actions:

- For startup manual review symbols: flatten on Binance when ready, or acknowledge via supervisor/Telegram (`/acknowledge <SYMBOL>`) if the symbol is eligible.
- For drawdown lock: use one-cycle `reset_equity_high_watermark: true` (manual reset), or opt into auto-heal with:
  - `hwm_auto_decay_after_hours`
  - `hwm_auto_decay_fraction`

Defaults keep prior behavior (`hwm_auto_decay_after_hours = 0.0`, disabled).

### Why Restart Should Stay Quiet

- The trader now writes a 90-second restart settling window into `risk_state`; Telegram and supervisor alerts consolidate restart-time flaps instead of emitting a storm of mode and kill-switch edges.
- Kill-switch drawdown logic now has hysteresis by default: it fires above 10% drawdown and only releases below 8%, so small mark-to-market swings do not toggle the portfolio guard.
- `manual_review` startup orphans still stay visible on the dashboard, but their unrealized MTM is excluded from the drawdown-input equity used by the risk engine. Drawdown tracks the managed portfolio, not unsupported orphan volatility.
- The existing HWM operator workflow is unchanged. For passive recovery instead of a manual reset, the recommended live-config values are `hwm_auto_decay_after_hours = 72.0` and `hwm_auto_decay_fraction = 1.0`.

See `HOW_IT_WORKS.md` for the full runtime flow, `RUNBOOK.md` for operations, `CONFIG.md` for live config keys, and `docs/STATE_DB_SCHEMA.md` for the shared SQLite contract.

See [`MASTERPLAN_IMPLEMENTATION.md`](MASTERPLAN_IMPLEMENTATION.md) for the
Phase 0–6 implementation map, activation boundary, and evidence still required.
