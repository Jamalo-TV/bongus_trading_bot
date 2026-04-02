## Bongus Trading Bot: How It Works

### Canonical Runtime

The live system now has one production Python path:

1. `bongus/monitoring/king_watchdog.py` starts the Rust engine, canonical trader, sentiment scraper, and dashboard.
2. `scripts/live_trader.py` is a thin launcher for `bongus/runtime/live_trader.py`.
3. `CanonicalMultiSymbolTrader` hot-loads runtime config, verifies state/IPC readiness, consumes Rust telemetry, scans the Binance universe, ranks opportunities, allocates the portfolio, and writes every decision into `state.db`.

### Python / Rust Split

- Python:
  - scanner, ranking, top-N portfolio construction
  - cost/risk/governance logic
  - shadow exit collection and scoring
  - state persistence and dashboard APIs
- Rust:
  - Binance WebSocket and REST connectivity
  - order placement and chase logic
  - legging defense and maker/taker execution handling
  - broadcast of order-book and order-update telemetry back to Python

### Live Cycle

Each trader cycle does the following:

1. Refreshes the tradable USDT-perp universe and keeps only names with a valid Binance spot hedge path.
2. Pulls funding and mark/index snapshots from Binance REST.
3. Merges those with Rust-fed book/depth telemetry.
4. Applies hard safety filters:
   - missing spot pair
   - low depth
   - wide spread
   - new listing
   - stale data
   - delist risk
   - structural toxicity
5. Persists every accept/reject decision into `candidate_snapshots`.
6. Ranks accepted names with winsorized-percentile components:
   - predicted net funding edge after shared costs
   - depth
   - spread
   - short-horizon realized volatility
   - basis stability
   - regime health
7. Persists the ranked results into `opportunity_scores`.
8. Builds a top-N portfolio with:
   - per-symbol caps
   - static cluster caps
   - gross exposure budget
   - depth / volatility / budget sizing clamps
9. Sends exit intents before any replacement entry.
10. Writes market samples, health samples, pending intents, and risk state updates.

### Shared State Contract

`bongus/engine/state_store.py` is the runtime contract between trader, dashboard, and governance.

Core tables:

- `positions`
- `trade_history`
- `portfolio_stats`
- `risk_state`

Scanner / ranking / execution / governance tables:

- `candidate_snapshots`
- `opportunity_scores`
- `market_samples`
- `execution_events`
- `execution_quality`
- `pending_intents`
- `feature_snapshots`
- `model_shadow_decisions`
- `validation_snapshots`
- `parameter_promotions`
- `health_samples`

Schema creation is additive and idempotent so existing live databases can be reopened safely.

### Cost And Risk

- `bongus/engine/cost_model.py` is the shared source of truth for fees, slippage, spread crossing, edge estimates, and payback calculations.
- `bongus/engine/risk_engine.py` still owns hard limits, derisking, and kill-switch decisions.
- New risk is blocked when runtime telemetry is stale or safety thresholds are breached.

### Governance

`scripts/walk_forward.py` no longer stops at a research summary.

It now:

- evaluates walk-forward windows
- computes acceptance, utilization, and drawdown gates
- writes `validation_snapshots`
- writes `parameter_promotions`
- auto-promotes approved overrides into `live_config.json`

### Shadow Exits

The runtime records feature snapshots for open trades and writes shadow-mode hold-vs-exit recommendations into `model_shadow_decisions`.

Important:

- the shadow model is advisory only
- live exits remain deterministic
- ratcheting remains disabled until shadow uplift is proven

### Dashboard

`bongus/monitoring/web_dashboard.py` serves the legacy UI plus API endpoints for:

- positions, stats, trades, risk, and pnl attribution
- latest candidate snapshots
- opportunity scores
- execution-quality samples
- shadow decisions
- validation snapshots and promotions
- health samples

That makes every cycle explainable: which names were scanned, why they were rejected, what ranked highest, and what the shadow exit model would have preferred.
