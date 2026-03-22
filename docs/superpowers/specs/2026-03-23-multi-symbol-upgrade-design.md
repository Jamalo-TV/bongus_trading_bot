# Multi-Symbol Funding Arbitrage Upgrade — Design Spec

**Date:** 2026-03-23
**Status:** Approved for implementation
**Bankroll:** $10K | **Target positions:** 3–4 concurrent | **Leverage:** 2x

---

## 1. Executive Summary

Upgrade the bot from a single-symbol (BTC-only) REST-polling loop into a multi-symbol streaming portfolio manager. The Rust execution engine already handles multi-symbol WebSocket feeds; this upgrade rebuilds the Python brain layer to match.

**Four phases:**
1. Market Data & Infrastructure — multi-stream connections, funding ranker, per-symbol depth tracking
2. Risk Management & Correlation — dynamic slippage, cross-asset circuit breaker, concentration limits
3. Capital Allocation & Rebalancing — fractional sizing, liquidity filter, rotation logic
4. Rust Execution Engine — concurrent chase states, spot WS streams, fill confirmation

---

## 2. Architecture & Data Flow

```
Binance WebSocket (spot + perp, per symbol)
        │
        ▼
Rust execution_engine
 ├─ WsConnectionManager × N × 2   (spot + perp per symbol)
 ├─ OrderManager                   (single actor, sequential event loop)
 └─ port 9000 TCP broadcast        newline-delimited JSON events
        │
        ▼
bongus/market_data/
 ├─ rust_data_subscriber.py        asyncio StreamReader.readline() → event dispatch
 ├─ depth_tracker.py               4 caches per symbol (spot/perp × bid/ask)
 └─ funding_ranker.py              single REST call → filter → ranked list
        │
        ▼
bongus/portfolio/
 ├─ portfolio_allocator.py         sizing, liquidity filter, rotation decisions
 └─ correlation_breaker.py         graduated circuit breaker
        │
        ▼
bongus/ipc/execution.py            ZMQ PUSH → AlphaInstruction per symbol → Rust
        │
        ▼
Rust OrderManager
 └─ chase_states: HashMap<String, ChaseState>
```

**Key invariants:**
- Python never opens a Binance WebSocket directly — all market data flows Rust → port 9000
- Funding rates are REST-polled every 60s (single request, full market array)
- Python is the sole decision-maker; Rust only executes what Python instructs
- Exits always dispatch before entries — margin integrity guaranteed
- No ENTER fires until exit `FILLED` confirmation received from port 9000

---

## 3. Phase 1 — Market Data & Infrastructure

### 3.1 Monitored Symbols

```python
# bongus/core/config.py addition
MONITORED_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT",
    "PEPEUSDT", "BNBUSDT", "ARBUSDT", "SUIUSDT",
]
```

### 3.2 `bongus/market_data/rust_data_subscriber.py`

- Asyncio TCP client connecting to Rust port 9000
- Uses `asyncio.StreamReader.readline()` exclusively — never `.read()`
- Handles TCP packet fragmentation automatically (readline buffers until `\n`)
- Reconnects with exponential backoff if Rust restarts
- Dispatches parsed events to registered callbacks: `on_depth(event)`, `on_order_update(event)`

### 3.3 `bongus/market_data/depth_tracker.py`

Maintains **four** caches per symbol — spot and perp are tracked independently because altcoin liquidity diverges significantly between venues:

| Cache | Used by | Direction |
|---|---|---|
| `spot_bid_depth[symbol]` | future use | — |
| `spot_ask_depth[symbol]` | `cost_per_leg_spot` | buying spot (entering long) |
| `perp_bid_depth[symbol]` | `cost_per_leg_perp` | shorting perp (entering short) |
| `perp_ask_depth[symbol]` | future use (exit perp) | — |

On each `L2Depth` event: sums `price × qty` for top 5 levels on each side independently. Binance returns `qty` in **base asset units** (e.g., BTC), so `price × qty` correctly yields USDT notional.

For the **liquidity filter**, the allocator checks the bottleneck leg:
```python
min_depth = min(depth_tracker.spot_ask_depth[symbol],
                depth_tracker.perp_bid_depth[symbol])
```

For **exit** friction calculations, the allocator uses:
```python
min_exit_depth = min(depth_tracker.spot_bid_depth[symbol],
                     depth_tracker.perp_ask_depth[symbol])
```

### 3.4 `bongus/market_data/funding_ranker.py`

- Single `GET /fapi/v1/premiumIndex` (no symbol param) → full market array
- Filters in Python for `MONITORED_SYMBOLS`
- Converts 8h rate → annualized: `rate * FUNDING_PERIODS_PER_YEAR` (uses existing config constant = 1095)
- Returns sorted `list[tuple[str, float]]` highest-first
- Polls every 60s (funding only changes at 8h snapshots)

### 3.5 Cost Model Fix

Remove `depth_usd=500_000.0` hardcoded defaults from `cost_model.py`. All call sites receive live depth from `DepthTracker`:

```python
cost_per_leg_spot(depth_usd=depth_tracker.spot_ask_depth[symbol])  # entry
cost_per_leg_perp(depth_usd=depth_tracker.perp_bid_depth[symbol])  # entry
```

Add `blended_exit_cost()` to `cost_model.py` — mirrors `blended_entry_cost()` but used for exit legs and for computing expected future exit cost of a rotation target:

```python
def blended_exit_cost(notional: float, depth_usd: float = 500_000.0) -> float:
    """Dollar cost to close a position, blended maker/taker."""
    return notional * blended_action_cost_pct(size_usd=notional, depth_usd=depth_usd)
```

This function is referenced in the rotation payback formula (Section 5.2). Without it, that formula raises `NameError` at runtime.

---

## 4. Phase 2 — Risk Management & Correlation

### 4.1 Dynamic Slippage

Solved by Phase 1 — `DepthTracker` provides real per-symbol, per-side depth. The cost model already accepts `depth_usd`; the hardcoded default is the only thing being removed.

### 4.2 Concentration Limits

`live_trader.py` currently hardcodes `symbol_concentration=1.0`. The allocator computes it correctly:

```python
concentration[symbol] = position_notional[symbol] / total_portfolio_notional
```

`RiskEngine.evaluate()` already enforces `MAX_SYMBOL_CONCENTRATION = 0.60` — it just needs real data.

### 4.3 `bongus/portfolio/correlation_breaker.py`

**State machine — mutually exclusive and collectively exhaustive:**

| State | Condition | Action |
|---|---|---|
| `CLEAR` | `negative_ratio < 0.50` | Normal trading, allow entries |
| `HALTED` | `0.50 ≤ negative_ratio < 1.00` | Block new entries; hold existing; let allocator rotate |
| `EMERGENCY` | `negative_ratio == 1.00` | Exit all positions immediately (market urgency) |

Where `negative_ratio = count(ann_funding < EXIT_ANN_FUNDING_THRESHOLD) / len(open_positions)`.

**Edge case:** `len(open_positions) == 0` → immediately return `CLEAR, allow_new_entries=True` (avoids division by zero).

```python
@dataclass
class BreakerDecision:
    state: Literal["CLEAR", "HALTED", "EMERGENCY"]
    allow_new_entries: bool
    positions_to_exit: list[str]   # empty unless EMERGENCY
    reason: str
```

**Config additions:**
```python
BREAKER_HALT_RATIO = 0.50       # ≥ 50% negative → HALTED
BREAKER_EMERGENCY_RATIO = 1.00  # 100% negative → EMERGENCY
```

**Priority:** `CorrelationBreaker` evaluates before `RiskEngine`. EMERGENCY bypasses RiskEngine entirely.

---

## 5. Phase 3 — Capital Allocation & Rebalancing

### 5.1 `bongus/portfolio/portfolio_allocator.py`

**Sizing constants (config.py additions):**
```python
MAX_CONCURRENT_POSITIONS = 4
CAPITAL_PER_SLOT_USD = 2_500          # ACCOUNT_EQUITY_USD / MAX_CONCURRENT_POSITIONS
TARGET_LEVERAGE = 2.0                  # notional = $5K per position
LIQUIDITY_FILTER_MULTIPLIER = 5.0     # min depth must be ≥ 5× notional
```

**Liquidity filter (hard stop, checked before any other logic):**
```python
target_notional = CAPITAL_PER_SLOT_USD * TARGET_LEVERAGE
min_depth = min(depth_tracker.spot_ask_depth[symbol],
                depth_tracker.perp_bid_depth[symbol])
if min_depth < LIQUIDITY_FILTER_MULTIPLIER * target_notional:
    continue  # skip regardless of funding rate
```

### 5.2 Rotation Logic (Approach D — hybrid)

Both conditions must hold simultaneously:

**Condition 1 — Minimum rate gap:**
```python
rate_gap = new_rate - current_rate
gap_clears_minimum = rate_gap > ROTATION_MIN_GAP_ANN  # 5% annualized
```

**Evaluation pipeline order (must be followed strictly):**
1. Funding ranker produces sorted candidate list
2. Liquidity filter eliminates candidates that fail `min_depth < 5× notional` — ensures `new_notional` is always valid before payback math runs
3. Rotation payback evaluated only on survivors

**Condition 2 — Full round-trip fee payback:**
```python
total_friction_usd = (
    blended_exit_cost(current_notional, depth=current_exit_depth)   # exit old
    + blended_entry_cost(new_notional, depth=new_entry_depth)        # enter new
    + blended_exit_cost(new_notional, depth=new_exit_depth)          # expected future exit of new
)
incremental_daily_income = (rate_gap / 365) * new_notional  # rate_gap is annualized
# Guard: should always be > 0 (rate_gap > ROTATION_MIN_GAP_ANN and new_notional > 0
# guaranteed by liquidity filter), but defend against misconfiguration:
if incremental_daily_income <= 0:
    continue
payback_days = total_friction_usd / incremental_daily_income
fee_pays_back = payback_days <= ROTATION_MAX_PAYBACK_DAYS           # ≤ 0.333 (8 hours)
```

The third term (`blended_exit_cost` of the new symbol) prevents rotating into illiquid meme coins with catastrophic expected exit costs — the "roach motel" trap.

**Rotation constants:**
```python
ROTATION_MIN_GAP_ANN = 0.05        # 5% annualized minimum rate gap
ROTATION_MAX_PAYBACK_DAYS = 0.333  # must pay back within 1 funding period (8h)
```

### 5.3 `AllocationDecision`

```python
@dataclass
class AllocationDecision:
    enter: list[tuple[str, float]]  # [(symbol, notional_usd)]
    exit:  list[tuple[str, str]]    # [(symbol, reason)]
    hold:  list[str]                # symbols to keep untouched
```

### 5.4 Execution Order & Exit Confirmation

The orchestrator maintains `pending_exits: set[str]`. Sequence for a rotation:

1. Dispatch EXIT via ZMQ → add symbol to `pending_exits`
2. `RustDataSubscriber.on_order_update(symbol, status="FILLED")` fires
3. Symbol removed from `pending_exits` → capital slot released
4. ENTER dispatched for rotation target

**Fallback:** If no `FILLED` confirmation within `ROTATION_CONFIRM_TIMEOUT_S = 10`, log warning, leave ENTER queued for next cycle. Never fires blind.

---

## 6. Phase 4 — Rust Execution Engine Upgrades

### 6.1 `MarketType` Enum

```rust
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]   // serializes as "spot" / "perp" in JSON
pub enum MarketType {
    Spot,
    Perp,
}
```

Used in `WsEvent::L2Depth` — zero heap allocation per event vs. `String`. Critical for performance across 10–20 concurrent WebSocket streams at 100ms depth update intervals.

The `#[serde(rename_all = "lowercase")]` attribute ensures the port 9000 JSON wire format emits `"market": "spot"` or `"market": "perp"`. `RustDataSubscriber` parses this field and routes to the correct `DepthTracker` cache: `"spot"` → `spot_bid_depth` / `spot_ask_depth`; `"perp"` → `perp_bid_depth` / `perp_ask_depth`.

### 6.2 Updated `WsEvent::L2Depth`

```rust
L2Depth {
    symbol: String,
    market: MarketType,
    bids: Vec<(f64, f64)>,
    asks: Vec<(f64, f64)>,
}
```

### 6.3 `chase_states: HashMap<String, ChaseState>`

```rust
// Before
chase: Option<ChaseState>,

// After
chase_states: HashMap<String, ChaseState>,  // key: symbol
```

**Concurrency model:** `OrderManager` already runs as a single actor task — one `engine_rx: Receiver<EngineEvent>` channel, all mutations sequential inside `run()`. Standard `std::collections::HashMap` is correct. No `Arc<RwLock<>>` or `DashMap` required — the channel is the synchronization primitive.

All methods previously reading/writing `self.chase` route by symbol via `self.chase_states.entry(symbol)`.

**Chase collision on EMERGENCY exit:** When an `EXIT` `AlphaInstruction` arrives for a symbol that already has an active `ChaseState`, the `OrderManager` must cancel the existing chase immediately and start a new exit chase. This is the only correct behavior for EMERGENCY scenarios — queuing behind an in-progress entry chase would delay the exit. Implementation: on receiving an EXIT instruction, call `self.chase_states.remove(&symbol)` (cancelling any REST cancel calls for in-flight orders) then insert the new exit `ChaseState`.

### 6.4 Spot WebSocket Connections

`main.rs` spawns two `WsConnectionManager` instances per symbol:

| Instance | URL | Subscriptions | `market` tag |
|---|---|---|---|
| Perp manager | `wss://fstream.binance.com/ws` | markPrice, bookTicker, depth5@100ms | `MarketType::Perp` |
| Spot manager | `wss://stream.binance.com:9443/ws` | depth5@100ms only | `MarketType::Spot` |

**Symbol list alignment:** The existing Rust `top_assets` hardcodes 20 symbols and is missing `PEPEUSDT` and `SUIUSDT`. Replace `top_assets` with `MONITORED_SYMBOLS` (8 symbols from `config.py` / environment), producing 16 total WS connections (8 spot + 8 perp). This is well within Binance limits and avoids unnecessary connections for symbols Python never trades. The Rust binary should read this list from an environment variable or config file rather than hardcoding, so both Python and Rust stay in sync without recompilation.

Connections are paced at 50ms intervals per symbol (existing behavior) to avoid rate limits.

### 6.5 Explicit `FILLED` Broadcast

When a chase completes (`filled_qty >= expected_qty`), `OrderManager` emits an explicit `OrderUpdate` with `status: "FILLED"` via `dash_tx` (port 9000). This is the linchpin signal Python awaits before releasing a capital slot and dispatching the rotation ENTER.

### 6.6 Rust File Change Summary

| File | Change |
|---|---|
| `order_manager.rs` | `chase` → `chase_states: HashMap<String, ChaseState>`; add `MarketType` enum; explicit `FILLED` emit; EXIT instruction cancels existing chase for same symbol |
| `binance_ws.rs` | Add `market: MarketType` param to constructor and `L2Depth` events |
| `main.rs` | Replace hardcoded `top_assets` (20 symbols) with `MONITORED_SYMBOLS` from env/config; spawn spot + perp `WsConnectionManager` per symbol with correct `MarketType` |

---

## 7. New File Manifest

```
bongus/
  market_data/
    __init__.py
    rust_data_subscriber.py
    depth_tracker.py
    funding_ranker.py
  portfolio/
    __init__.py
    portfolio_allocator.py
    correlation_breaker.py

scripts/
  live_trader_v2.py              (new orchestrator; live_trader.py untouched as fallback)

execution_engine/src/
  order_manager.rs               (modified)
  binance_ws.rs                  (modified)
  main.rs                        (modified)

bongus/core/config.py            (additions only — no removals)
bongus/engine/cost_model.py      (remove hardcoded depth defaults; add blended_exit_cost())
```

---

## 8. Config Additions Summary

```python
# Symbols
MONITORED_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT",
                     "PEPEUSDT", "BNBUSDT", "ARBUSDT", "SUIUSDT"]

# Allocation
MAX_CONCURRENT_POSITIONS = 4
CAPITAL_PER_SLOT_USD = 2_500
TARGET_LEVERAGE = 2.0
LIQUIDITY_FILTER_MULTIPLIER = 5.0

# Rotation
ROTATION_MIN_GAP_ANN = 0.05
ROTATION_MAX_PAYBACK_DAYS = 0.333
ROTATION_CONFIRM_TIMEOUT_S = 10

# Circuit breaker
BREAKER_HALT_RATIO = 0.50
BREAKER_EMERGENCY_RATIO = 1.00
```

---

## 9. What Is Not Changed

- `live_trader.py` — preserved as single-symbol fallback
- `strategy.py` — backtest signal logic untouched
- `multi_symbol_runner.py` — backtest runner untouched
- `risk_engine.py` — no interface changes, only receives better data
- `state_store.py` — no schema changes needed
- `ipc/execution.py` — `ExecutionClient` interface unchanged (accepts generic `Dict[str, Any]`), but **semantic contract has changed**: the orchestrator now dispatches instructions for multiple symbols; the `symbol` field in the payload routes each instruction to the correct `ChaseState` in the Rust `OrderManager`. Callers must not assume single-symbol behavior.
