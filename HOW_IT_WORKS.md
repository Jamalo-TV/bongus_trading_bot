## How Bongus Works

### Strategy Summary

Bongus is a delta-neutral funding arbitrage bot for Binance. Instead of taking a directional bet, it tries to earn the net carry from funding payments while staying hedged across spot and perpetual futures.

At a high level, the system:

- watches a basket of Binance symbols for funding opportunities,
- filters out symbols that are too illiquid or too expensive to rotate into,
- opens hedged spot/perp positions across multiple portfolio slots,
- rotates into better candidates only when the improvement should repay friction fast enough,
- records positions, trades, and execution events so live behavior can be audited and replayed.

The current design is meant to be run in `paper`, `testnet`, or `live` mode. In `paper` mode, the bot uses live Binance market data with synthetic fills, which makes it the safest end-to-end validation path.

### System Split

The bot is intentionally split into two runtimes:

- Python brain: ranking, allocation, risk, state persistence, analytics, and dashboard serving.
- Rust engine: market data ingestion, low-latency order handling, and telemetry broadcast.

They communicate over two channels:

- Python -> Rust: ZMQ PUSH on `tcp://127.0.0.1:5555`
- Rust -> Python and dashboard clients: TCP broadcast on `127.0.0.1:9000`

This separation keeps research and orchestration flexible in Python while pushing the hot-path execution loop into Rust.

### Live Data And Execution Flow

#### 1. Market data enters through Rust

The Rust engine subscribes to Binance WebSocket feeds for:

- perpetual mark prices,
- perpetual order book updates,
- spot order book updates,
- user-data streams when the bot is in `testnet` or `live` mode.

In `paper` mode, it still consumes live market data from Binance mainnet but skips authenticated order streams and simulates fills locally.

#### 2. Python maintains the decision state

`scripts/live_trader_v2.py` wires together the live system:

- `RustDataSubscriber` receives depth, order updates, and mark prices.
- `FundingRanker` refreshes funding rates and updates them sub-minute from mark-price events.
- `DepthTracker` keeps a current view of spot and perp liquidity.
- `PortfolioAllocator` decides whether each symbol should be entered, held, exited, or rotated.
- `RiskEngine` can scale down or stop the system when drawdown or portfolio stress becomes unhealthy.

#### 3. Exit-first rotation is enforced

The bot never flips directly from one opportunity into another in a single atomic step. It follows an exit-first invariant:

1. send the exit,
2. wait for a confirmed fill or timeout,
3. only then dispatch the replacement entry.

That matters because funding strategies usually lose their edge quickly if they accidentally become directional during a rotation.

#### 4. Rust manages order state

The Rust engine accepts order intents from Python, tracks the order lifecycle, and broadcasts normalized `OrderUpdate` events back to the rest of the stack.

Recent upgrades made those events much richer. They can now carry:

- cumulative filled quantity,
- average and last fill price,
- commission and commission asset,
- realized PnL,
- maker/taker hinting,
- execution type metadata,
- spot and perp leg fill prices for cycle-complete paper fills.

That richer payload gives Python better truth for trade attribution and future replay work.

### Persistence And Observability

SQLite is the shared state layer for the live trader and the dashboard.

The state store now persists:

- positions,
- trades,
- stats and snapshots,
- append-only execution events with raw payload backups.

That execution-event ledger is an important step forward. It means the system no longer has to infer everything from coarse status updates; it can keep a durable record of what the execution layer reported at the time.

The FastAPI dashboard reads from persisted state and Rust telemetry. It no longer clears the database on startup, which makes it much safer for debugging, reconciliation work, and post-run analysis.

### Main Components

#### Python

- `bongus/core/config.py`: static defaults and trading constants
- `bongus/core/config_manager.py`: hot-reloads `live_config.json`
- `bongus/market_data/funding_ranker.py`: ranks symbols by funding edge
- `bongus/market_data/depth_tracker.py`: tracks spot/perp liquidity
- `bongus/portfolio/portfolio_allocator.py`: slot management and rotation decisions
- `bongus/portfolio/correlation_breaker.py`: portfolio-wide circuit breaker
- `bongus/engine/risk_engine.py`: drawdown and kill-switch logic
- `bongus/engine/state_store.py`: SQLite persistence and execution-event ledger
- `bongus/monitoring/web_dashboard.py`: REST and WebSocket monitoring surface
- `scripts/live_trader_v2.py`: main live orchestration loop

#### Rust

- `execution_engine/src/main.rs`: process wiring, channels, WebSocket startup, IPC broadcast
- `execution_engine/src/binance_ws.rs`: market-data WebSocket manager
- `execution_engine/src/binance_rest.rs`: exchange REST client
- `execution_engine/src/order_manager.rs`: order state machine and paper execution logic
- `execution_engine/src/user_data_ws.rs`: authenticated order updates in non-paper modes
- `execution_engine/src/ipc.rs`: ZMQ intake and telemetry broadcast

### Operating Modes

#### `paper`

- Uses live Binance market data.
- Does not place real orders.
- Generates synthetic fills inside the Rust engine.
- Best mode for end-to-end validation and regression testing.

#### `testnet`

- Uses Binance testnet execution.
- Good for API-path validation.
- Usually less reliable than `paper` for repeatable behavior.

#### `live`

- Uses real exchange execution.
- Requires accurate reconciliation, cost accounting, and strong operational discipline.

### Safety Model

Bongus already contains several practical protections:

- liquidity gating before entries,
- portfolio correlation breaker behavior,
- watchdog-based process supervision,
- exit-first rotation semantics,
- paper mode for live-market dry runs,
- append-only execution logs for auditability.

The next major safety upgrade is full startup reconciliation against exchange truth, so restarts can rebuild exact state instead of relying on local assumptions.

### What Improved Recently

The most recent upgrade tranche focused on truth and attribution rather than new alpha:

- execution events are now stored durably,
- order updates include richer fill metadata,
- live trade records can use real fill fields when present,
- phantom pending entries from failed or zero-sized dispatches were blocked,
- the dashboard stopped wiping state on startup.

That is a meaningful improvement because it raises trust in the bot's live accounting and makes later optimizations much safer.

### Practical Mental Model

The easiest way to think about the system is:

- Rust watches the market and handles order mechanics.
- Python decides what the portfolio should look like.
- SQLite remembers what happened.
- The dashboard helps you inspect whether reality matched the plan.

If those four layers stay aligned, the strategy can improve safely. If they drift apart, the first job is not finding new alpha; it is restoring truth.
