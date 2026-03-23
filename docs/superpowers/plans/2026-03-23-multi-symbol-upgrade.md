# Multi-Symbol Funding Arbitrage Upgrade — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade the bot from a single-symbol (BTC-only) REST-polling loop to a multi-symbol streaming portfolio manager with dynamic capital allocation and portfolio-level risk management.

**Architecture:** Two new Python packages (`bongus/market_data/`, `bongus/portfolio/`) consume real-time depth from Rust's port 9000 TCP broadcast and make multi-symbol allocation decisions. The Rust execution engine gains concurrent per-symbol chase states, explicit FILLED confirmations, and dual spot+perp WebSocket connections.

**Tech Stack:** Python 3.10, asyncio, requests (existing), zmq/msgpack (existing), pytest; Rust/Tokio, serde_json, zeromq (existing)

**Spec:** `docs/superpowers/specs/2026-03-23-multi-symbol-upgrade-design.md`

---

## File Map

### New Python Files
| File | Responsibility |
|---|---|
| `bongus/market_data/__init__.py` | Package marker |
| `bongus/market_data/depth_tracker.py` | 4-cache per-symbol depth (spot/perp × bid/ask) |
| `bongus/market_data/rust_data_subscriber.py` | asyncio TCP listener on Rust port 9000 |
| `bongus/market_data/funding_ranker.py` | Single REST call → ranked funding list |
| `bongus/portfolio/__init__.py` | Package marker |
| `bongus/portfolio/correlation_breaker.py` | Graduated circuit breaker (CLEAR/HALTED/EMERGENCY) |
| `bongus/portfolio/portfolio_allocator.py` | Fractional sizing, liquidity filter, rotation logic |
| `scripts/live_trader_v2.py` | New multi-symbol orchestrator (live_trader.py untouched) |
| `tests/test_depth_tracker.py` | DepthTracker unit tests |
| `tests/test_funding_ranker.py` | FundingRanker unit tests |
| `tests/test_correlation_breaker.py` | CorrelationBreaker unit tests |
| `tests/test_portfolio_allocator.py` | PortfolioAllocator unit tests |
| `tests/test_rust_data_subscriber.py` | RustDataSubscriber dispatch unit tests |

### Modified Python Files
| File | Change |
|---|---|
| `bongus/core/config.py` | Add MONITORED_SYMBOLS, allocation/rotation/breaker constants |
| `bongus/engine/cost_model.py` | Add `blended_exit_cost()`; remove `depth_usd=500_000.0` defaults |

### Modified Rust Files
| File | Change |
|---|---|
| `execution_engine/src/order_manager.rs` | Add `MarketType` enum; `chase` → `chase_states: HashMap`; explicit FILLED emit; EXIT cancels existing chase |
| `execution_engine/src/binance_ws.rs` | Add `market: MarketType` to constructor and `L2Depth` events |
| `execution_engine/src/main.rs` | Read symbols from `MONITORED_SYMBOLS` env var; spawn spot + perp WS per symbol |

---

## Task 1: Config Additions

**Files:**
- Modify: `bongus/core/config.py`

- [ ] **Step 1: Add constants to config.py**

Open `bongus/core/config.py` and append at the end:

```python
# ── Multi-Symbol ─────────────────────────────────────────────────────────────
MONITORED_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT",
    "PEPEUSDT", "BNBUSDT", "ARBUSDT", "SUIUSDT",
]

# ── Capital Allocation ────────────────────────────────────────────────────────
MAX_CONCURRENT_POSITIONS = 4
CAPITAL_PER_SLOT_USD = 2_500          # ACCOUNT_EQUITY_USD / MAX_CONCURRENT_POSITIONS
TARGET_LEVERAGE = 2.0                  # notional = CAPITAL_PER_SLOT_USD * TARGET_LEVERAGE = $5K
LIQUIDITY_FILTER_MULTIPLIER = 5.0     # skip if min(spot_ask, perp_bid) < 5× notional

# ── Rotation ──────────────────────────────────────────────────────────────────
ROTATION_MIN_GAP_ANN = 0.05           # 5% annualized minimum rate gap to trigger rotation
ROTATION_MAX_PAYBACK_DAYS = 0.333     # fees must pay back within 1 funding period (8h)
ROTATION_CONFIRM_TIMEOUT_S = 10       # seconds to wait for FILLED confirmation before giving up

# ── Circuit Breaker ───────────────────────────────────────────────────────────
BREAKER_HALT_RATIO = 0.50             # ≥ 50% of positions negative → HALTED
BREAKER_EMERGENCY_RATIO = 1.00        # 100% of positions negative → EMERGENCY
```

- [ ] **Step 2: Verify no syntax errors**

```bash
python -c "from bongus.core.config import MONITORED_SYMBOLS, MAX_CONCURRENT_POSITIONS, ROTATION_MIN_GAP_ANN, BREAKER_HALT_RATIO; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add bongus/core/config.py
git commit -m "feat: add multi-symbol config constants (symbols, allocation, rotation, circuit breaker)"
```

---

## Task 2: Cost Model — Add `blended_exit_cost`

**Files:**
- Modify: `bongus/engine/cost_model.py`
- Test: `tests/test_cost_model.py`

The `depth_usd=500_000.0` defaults on existing functions are kept for backtest compatibility.
`live_trader_v2.py` always passes real depth explicitly. Only add `blended_exit_cost` here.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cost_model.py`:

```python
def test_blended_exit_cost_returns_positive_dollar_amount():
    """blended_exit_cost must return a positive USD cost."""
    from cost_model import blended_exit_cost
    cost = blended_exit_cost(5_000.0, depth_usd=1_000_000.0)
    assert cost > 0.0, f"Expected positive cost, got {cost}"
    assert cost < 5_000.0 * 0.05, f"Cost {cost} seems too high for $5k notional"


def test_blended_exit_cost_is_less_than_round_trip():
    """blended_exit_cost (one action) must be less than round_trip_cost (two actions)."""
    from cost_model import blended_exit_cost, round_trip_cost
    notional = 5_000.0
    depth = 1_000_000.0
    exit_cost = blended_exit_cost(notional, depth_usd=depth)
    rt_cost = round_trip_cost(notional, depth_usd=depth)
    assert exit_cost < rt_cost, f"One-way exit {exit_cost} should be less than round-trip {rt_cost}"
```

- [ ] **Step 2: Run to verify failure**

```bash
cd C:\Users\gabri\Bongus\bongus_trading_bot
python -m pytest tests/test_cost_model.py::test_blended_exit_cost_returns_positive_dollar_amount tests/test_cost_model.py::test_blended_exit_cost_is_less_than_round_trip -v
```
Expected: Both FAIL with `ImportError: cannot import name 'blended_exit_cost'`

- [ ] **Step 3: Add `blended_exit_cost` to cost_model.py**

Open `bongus/engine/cost_model.py`. Find `blended_entry_cost` and add `blended_exit_cost` immediately after it:

```python
def blended_exit_cost(notional: float, depth_usd: float = 500_000.0) -> float:
    """Dollar cost to close one position (spot sell + perp cover), blended maker/taker.

    Mirrors blended_entry_cost. depth_usd=500_000.0 default is kept for backtest
    compatibility — live_trader_v2.py always passes real depth from DepthTracker.
    """
    return notional * blended_action_cost_pct(size_usd=notional, depth_usd=depth_usd)
```

- [ ] **Step 4: Run all cost model tests**

```bash
python -m pytest tests/test_cost_model.py -v
```
Expected: All tests PASS (including two new ones)

- [ ] **Step 5: Commit**

```bash
git add bongus/engine/cost_model.py tests/test_cost_model.py
git commit -m "feat: add blended_exit_cost() to cost_model for rotation payback calculation"
```

---

## Task 3: Rust — `MarketType` Enum + `L2Depth` Update

**Files:**
- Modify: `execution_engine/src/order_manager.rs` (add enum, update WsEvent)
- Modify: `execution_engine/src/binance_ws.rs` (add market field)

This task is compile-verified, not TDD. Read `order_manager.rs` in full before modifying.

- [ ] **Step 1: Add `MarketType` enum to order_manager.rs**

Find the top of `order_manager.rs` where `WsEvent` is defined (around line 22). Add the enum before the `WsEvent` definition:

```rust
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum MarketType {
    Spot,
    Perp,
}
```

- [ ] **Step 2: Add `market: MarketType` to `WsEvent::L2Depth`**

Find the `L2Depth` variant (around line 30–34) and add the `market` field:

```rust
L2Depth {
    symbol: String,
    market: MarketType,   // NEW: "spot" or "perp" in JSON
    bids: Vec<(f64, f64)>,
    asks: Vec<(f64, f64)>,
},
```

- [ ] **Step 3: Update `binance_ws.rs` — add `market` to struct and constructor**

First, ensure `MarketType` is imported at the top of `binance_ws.rs`. Update the existing import line to include it:
```rust
use crate::order_manager::{WsEvent, MarketType};
```
(If this import already exists without `MarketType`, add it. If `MarketType` is already there from a prior edit, skip.)

In `WsConnectionManager` struct, add:
```rust
market: MarketType,
```

In `WsConnectionManager::new`, add the `market` parameter:
```rust
pub fn new(url: &str, symbol: &str, event_sender: Sender<WsEvent>, market: MarketType) -> Self {
    Self {
        // ... existing fields ...
        market,
    }
}
```

In `handle_connection`, where `L2Depth` is sent (around line 168), add `market: self.market`:
```rust
let _ = self.event_sender.send(WsEvent::L2Depth {
    symbol: self.symbol.to_uppercase(),
    market: self.market,   // NEW
    bids: raw_bids,
    asks: raw_asks,
}).await;
```

- [ ] **Step 4: Fix all `WsConnectionManager::new` call sites in main.rs**

Add `MarketType::Perp` to all existing calls (will be replaced fully in Task 4):
```rust
let mut ws_manager = WsConnectionManager::new(&url, &sym, tx_clone, MarketType::Perp);
```

- [ ] **Step 5: Build to verify compilation**

```bash
cd execution_engine
cargo build 2>&1
```
Expected: Compiles with no errors (warnings about unused fields are OK)

- [ ] **Step 6: Commit**

```bash
cd ..
git add execution_engine/src/order_manager.rs execution_engine/src/binance_ws.rs execution_engine/src/main.rs
git commit -m "feat(rust): add MarketType enum with serde lowercase; add market field to L2Depth WsEvent"
```

---

## Task 4: Rust — Spot WebSocket Connections + Symbol List from Env

**Files:**
- Modify: `execution_engine/src/main.rs`

- [ ] **Step 1: Replace hardcoded `top_assets` with env-var symbol list**

Before editing, read `main.rs` and confirm:
- The variable `use_testnet` is defined before the WS spawning block (it controls testnet vs mainnet URL).
- The variable `binance_ws_url` holds the perp WS URL (`wss://fstream.binance.com/ws` in production). Note its exact name.
- The variable `ws_tx` is the `Sender<WsEvent>` channel used by existing spawns.

Then find the `top_assets` vec in `main.rs` (around line 95) and replace the entire block from `let top_assets = vec![...]` through the end of the WS spawning loop with:

```rust
// Read monitored symbols from env — must match Python's MONITORED_SYMBOLS
let symbols_env = std::env::var("MONITORED_SYMBOLS")
    .unwrap_or_else(|_| "BTCUSDT,ETHUSDT,SOLUSDT,DOGEUSDT,PEPEUSDT,BNBUSDT,ARBUSDT,SUIUSDT".to_string());
let monitored_symbols: Vec<String> = symbols_env
    .split(',')
    .map(|s| s.trim().to_string())
    .filter(|s| !s.is_empty())
    .collect();

tracing::info!("Monitoring {} symbols: {:?}", monitored_symbols.len(), monitored_symbols);

let spot_ws_url = if use_testnet {
    "wss://testnet.binance.vision/ws".to_string()
} else {
    "wss://stream.binance.com:9443/ws".to_string()
};

// Spawn perp + spot WsConnectionManager for each symbol
for symbol in &monitored_symbols {
    // Perp: markPrice + bookTicker + depth5@100ms
    let sym = symbol.clone();
    let tx_clone = ws_tx.clone();
    let perp_url = binance_ws_url.to_string();
    tokio::spawn(async move {
        let mut ws_manager = WsConnectionManager::new(&perp_url, &sym, tx_clone, MarketType::Perp);
        ws_manager.run().await;
    });
    tokio::time::sleep(Duration::from_millis(50)).await;

    // Spot: depth5@100ms only
    let sym = symbol.clone();
    let tx_clone = ws_tx.clone();
    let s_url = spot_ws_url.clone();
    tokio::spawn(async move {
        let mut ws_manager = WsConnectionManager::new(&s_url, &sym, tx_clone, MarketType::Spot);
        ws_manager.run().await;
    });
    tokio::time::sleep(Duration::from_millis(50)).await;
}
```

- [ ] **Step 2: Update spot WS subscription in `binance_ws.rs`**

The spot `WsConnectionManager` should only subscribe to `depth5@100ms` (no markPrice or bookTicker). In `handle_connection`, branch on `self.market`:

```rust
let streams = if self.market == MarketType::Spot {
    vec![format!("{}@depth5@100ms", self.symbol)]
} else {
    vec![
        format!("{}@markPrice", self.symbol),
        format!("{}@bookTicker", self.symbol),
        format!("{}@depth5@100ms", self.symbol),
    ]
};
```

- [ ] **Step 3: Verify `MarketType` import in binance_ws.rs**

Task 3 added this import — verify it is present at the top of `binance_ws.rs`:
```rust
use crate::order_manager::{WsEvent, MarketType};
```
If missing (e.g., if Task 3 was done in a separate worktree), add it now.

- [ ] **Step 4: Build to verify compilation**

```bash
cd execution_engine
cargo build 2>&1
```
Expected: Compiles cleanly

- [ ] **Step 5: Commit**

```bash
cd ..
git add execution_engine/src/main.rs execution_engine/src/binance_ws.rs
git commit -m "feat(rust): spawn spot+perp WS per symbol; read symbol list from MONITORED_SYMBOLS env var"
```

---

## Task 5: Rust — Multi-Symbol Chase States

**Files:**
- Modify: `execution_engine/src/order_manager.rs`

Read the **full** `order_manager.rs` before this task — the chase state machine is complex and all self.chase references must be updated.

- [ ] **Step 1: Change `chase` field to `chase_states` HashMap**

In the `OrderManager` struct, change:
```rust
// Before:
chase: Option<ChaseState>,

// After:
chase_states: HashMap<String, ChaseState>,  // key: symbol
```

- [ ] **Step 2: Update `OrderManager::new` to initialize `chase_states`**

```rust
chase_states: HashMap::new(),
```

- [ ] **Step 3: Update all `self.chase` reads to route by symbol**

Find every occurrence of `self.chase` in the file. For each:

| Old pattern | New pattern |
|---|---|
| `self.chase = Some(state)` | `self.chase_states.insert(symbol.clone(), state)` |
| `self.chase = None` | `self.chase_states.remove(&symbol)` |
| `if let Some(ref chase) = self.chase` | `if let Some(chase) = self.chase_states.get(&symbol)` |
| `if let Some(ref mut chase) = self.chase` | `if let Some(chase) = self.chase_states.get_mut(&symbol)` |
| `self.chase.is_some()` | `self.chase_states.contains_key(&symbol)` |
| `self.chase.is_none()` | `!self.chase_states.contains_key(&symbol)` |

The `symbol` variable in each context should come from the `AlphaInstruction.symbol` or the active event's symbol.

- [ ] **Step 4: Handle EXIT collision — cancel existing chase**

First, read the `ChaseState` struct definition in `order_manager.rs`. Confirm the exact field name used for the in-flight order ID (it may be `client_order_id`, `order_id`, `active_order_id`, or similar). Also confirm whether the `cancel_order` helper exists on `self.binance_rest` and its signature.

Then, in the block that handles an incoming `EXIT` `AlphaInstruction`, add at the top before inserting the new exit chase (substitute the correct field name if different from `client_order_id`):

```rust
if let Some(existing) = self.chase_states.remove(&instruction.symbol) {
    warn!("EXIT received for {} while chase active (phase: {:?}) — cancelling existing chase",
        instruction.symbol, existing.phase);
    // If existing chase has an in-flight order, cancel it via REST
    // NOTE: verify the field name 'client_order_id' matches your ChaseState struct
    if let Some(order_id) = existing.client_order_id {
        let _ = self.binance_rest.cancel_order(&instruction.symbol, &order_id).await;
    }
}
// Now insert the exit chase state
```

- [ ] **Step 5: Build**

```bash
cd execution_engine
cargo build 2>&1
```
Expected: Compiles. Fix any remaining `self.chase` references the compiler flags.

- [ ] **Step 6: Commit**

```bash
cd ..
git add execution_engine/src/order_manager.rs
git commit -m "feat(rust): replace single chase Option with chase_states HashMap for concurrent multi-symbol execution"
```

---

## Task 6: Rust — Explicit `FILLED` Broadcast

**Files:**
- Modify: `execution_engine/src/order_manager.rs`

- [ ] **Step 1: Find where chase completion is detected**

Search `order_manager.rs` for where `filled_qty` is compared to the expected quantity (this is where the chase loop determines the order is complete). It will look something like:

```rust
if update.filled_qty >= chase.expected_qty {
    // chase is done
}
```

- [ ] **Step 2: Add explicit FILLED broadcast at chase completion**

Before writing, confirm `self.dash_tx` field type in the `OrderManager` struct — it must be a `tokio::sync::broadcast::Sender<String>` (or similar string-based sender) for the `serde_json::to_string` approach below to compile. If it wraps a different type, serialize to that type instead.

In that completion block, after the existing logic, add:

```rust
// Broadcast FILLED to Python via port 9000 (Python awaits this to release capital slots)
let filled_event = serde_json::json!({
    "event": "OrderUpdate",
    "symbol": symbol,
    "status": "FILLED",
    "filled_qty": chase.expected_qty,
    "client_order_id": chase.client_order_id,
});
if let Ok(msg) = serde_json::to_string(&filled_event) {
    let _ = self.dash_tx.send(msg);
}
```

- [ ] **Step 3: Build**

```bash
cd execution_engine
cargo build 2>&1
```
Expected: Compiles cleanly

- [ ] **Step 4: Smoke-test with test_send.py**

In a terminal, start the Rust engine (`cargo run` in execution_engine/), then in another terminal:

```bash
cd execution_engine
python test_send.py
```

Check Rust logs for `FILLED` broadcast messages.

- [ ] **Step 5: Commit**

```bash
cd ..
git add execution_engine/src/order_manager.rs
git commit -m "feat(rust): emit explicit FILLED OrderUpdate on port 9000 when chase completes"
```

---

## Task 7: Python Package Scaffolding

**Files:**
- Create: `bongus/market_data/__init__.py`
- Create: `bongus/portfolio/__init__.py`

- [ ] **Step 1: Create empty package markers**

```bash
python -c "open('bongus/market_data/__init__.py', 'a').close(); open('bongus/portfolio/__init__.py', 'a').close()"
```

- [ ] **Step 2: Verify imports work**

```bash
python -c "import bongus.market_data; import bongus.portfolio; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add bongus/market_data/__init__.py bongus/portfolio/__init__.py
git commit -m "feat: scaffold market_data and portfolio Python packages"
```

---

## Task 8: `DepthTracker`

**Files:**
- Create: `bongus/market_data/depth_tracker.py`
- Create: `tests/test_depth_tracker.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_depth_tracker.py`:

```python
"""Tests for DepthTracker — 4-cache per-symbol depth tracking."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'bongus', 'market_data')))

from depth_tracker import DepthTracker


def _make_levels(base_price: float, qty: float, n: int = 5) -> list:
    """Helper: create N levels at base_price each with qty."""
    return [(base_price, qty)] * n


def test_initial_state_all_zero():
    """Fresh tracker has zero depth for any symbol."""
    t = DepthTracker()
    assert t.spot_ask_depth("BTCUSDT") == 0.0
    assert t.perp_bid_depth("BTCUSDT") == 0.0
    assert t.get_entry_depth("BTCUSDT") == 0.0
    assert t.get_exit_depth("ETHUSDT") == 0.0


def test_spot_depth_updates_on_spot_event():
    """Spot L2Depth event updates spot caches only."""
    t = DepthTracker()
    bids = _make_levels(50_000.0, 2.0)  # 5 levels × 50_000 × 2 = 500_000 USD bid
    asks = _make_levels(50_100.0, 1.0)  # 5 levels × 50_100 × 1 = 250_500 USD ask
    t.on_l2depth("BTCUSDT", "spot", bids, asks)

    assert abs(t.spot_bid_depth("BTCUSDT") - 500_000.0) < 1.0
    assert abs(t.spot_ask_depth("BTCUSDT") - 250_500.0) < 1.0
    # Perp caches remain zero
    assert t.perp_bid_depth("BTCUSDT") == 0.0
    assert t.perp_ask_depth("BTCUSDT") == 0.0


def test_perp_depth_updates_on_perp_event():
    """Perp L2Depth event updates perp caches only."""
    t = DepthTracker()
    bids = _make_levels(50_000.0, 3.0)  # 750_000 USD bid
    asks = _make_levels(50_050.0, 2.0)  # 500_500 USD ask
    t.on_l2depth("BTCUSDT", "perp", bids, asks)

    assert abs(t.perp_bid_depth("BTCUSDT") - 750_000.0) < 1.0
    assert abs(t.perp_ask_depth("BTCUSDT") - 500_500.0) < 1.0
    # Spot caches remain zero
    assert t.spot_bid_depth("BTCUSDT") == 0.0


def test_get_entry_depth_is_min_of_spot_ask_and_perp_bid():
    """Entry depth = min(spot_ask, perp_bid) — the bottleneck for entering long spot + short perp.

    spot: bids=_make_levels(3000, 10) [ignored for entry], asks=_make_levels(3005, 2) → spot_ask = 5*3005*2 = 30_050
    perp: bids=_make_levels(3000, 5) → perp_bid = 5*3000*5 = 75_000, asks ignored for entry
    entry = min(30_050, 75_000) = 30_050
    """
    t = DepthTracker()
    t.on_l2depth("ETHUSDT", "spot", _make_levels(3000.0, 10.0), _make_levels(3005.0, 2.0))
    t.on_l2depth("ETHUSDT", "perp", _make_levels(3000.0, 5.0), _make_levels(3005.0, 8.0))

    assert abs(t.spot_ask_depth("ETHUSDT") - 30_050.0) < 1.0
    assert abs(t.perp_bid_depth("ETHUSDT") - 75_000.0) < 1.0
    assert abs(t.get_entry_depth("ETHUSDT") - 30_050.0) < 1.0  # min of the two


def test_get_exit_depth_is_min_of_spot_bid_and_perp_ask():
    """Exit depth = min(spot_bid, perp_ask) — the bottleneck for exiting long spot + short perp.

    spot: bids=_make_levels(150, 100) → spot_bid = 5*150*100 = 75_000, asks ignored for exit
    perp: bids ignored for exit, asks=_make_levels(150.5, 30) → perp_ask = 5*150.5*30 = 22_575
    exit = min(75_000, 22_575) = 22_575
    """
    t = DepthTracker()
    t.on_l2depth("SOLUSDT", "spot", _make_levels(150.0, 100.0), _make_levels(150.5, 50.0))
    t.on_l2depth("SOLUSDT", "perp", _make_levels(150.0, 200.0), _make_levels(150.5, 30.0))

    assert abs(t.spot_bid_depth("SOLUSDT") - 75_000.0) < 1.0
    assert abs(t.perp_ask_depth("SOLUSDT") - 22_575.0) < 1.0
    assert abs(t.get_exit_depth("SOLUSDT") - 22_575.0) < 1.0  # min of the two


def test_multiple_symbols_are_independent():
    """Updates to one symbol do not affect another."""
    t = DepthTracker()
    t.on_l2depth("BTCUSDT", "perp", _make_levels(50_000.0, 1.0), _make_levels(50_100.0, 1.0))
    assert t.perp_bid_depth("ETHUSDT") == 0.0


def test_depth_uses_top_5_levels_only():
    """Only the first 5 levels are summed; extra levels are ignored."""
    t = DepthTracker()
    # 10 levels — only first 5 should count
    bids = [(100.0, 1.0)] * 10
    t.on_l2depth("DOGEUSDT", "spot", bids, [])
    expected = 100.0 * 1.0 * 5  # 500.0
    assert abs(t.spot_bid_depth("DOGEUSDT") - 500.0) < 1e-9
```

- [ ] **Step 2: Run to verify failure**

```bash
python -m pytest tests/test_depth_tracker.py -v
```
Expected: ImportError (module not yet created)

- [ ] **Step 3: Implement `depth_tracker.py`**

Create `bongus/market_data/depth_tracker.py`:

```python
"""Per-symbol order book depth tracker with separate spot and perp caches.

Maintains four USD-denominated depth caches per symbol:
  spot_bid_usd  — bid-side depth of spot book (used for exit: selling spot)
  spot_ask_usd  — ask-side depth of spot book (used for entry: buying spot)
  perp_bid_usd  — bid-side depth of perp book (used for entry: shorting perp)
  perp_ask_usd  — ask-side depth of perp book (used for exit: covering perp short)

Binance returns qty in base asset units (e.g., BTC). Multiplying price × qty
yields USD notional. Only the top 5 levels are summed per side.
"""

from dataclasses import dataclass, field


_TOP_N = 5


@dataclass
class _SymbolDepth:
    spot_bid_usd: float = 0.0
    spot_ask_usd: float = 0.0
    perp_bid_usd: float = 0.0
    perp_ask_usd: float = 0.0


class DepthTracker:
    def __init__(self) -> None:
        self._depths: dict[str, _SymbolDepth] = {}

    def on_l2depth(
        self,
        symbol: str,
        market: str,
        bids: list[tuple[float, float]],
        asks: list[tuple[float, float]],
    ) -> None:
        """Update depth caches for a symbol from a L2Depth event.

        market: "spot" or "perp" (matches MarketType serde output from Rust)
        bids/asks: list of (price, qty) tuples — qty in base asset units
        """
        if symbol not in self._depths:
            self._depths[symbol] = _SymbolDepth()

        bid_usd = sum(p * q for p, q in bids[:_TOP_N])
        ask_usd = sum(p * q for p, q in asks[:_TOP_N])

        depth = self._depths[symbol]
        if market == "spot":
            depth.spot_bid_usd = bid_usd
            depth.spot_ask_usd = ask_usd
        elif market == "perp":
            depth.perp_bid_usd = bid_usd
            depth.perp_ask_usd = ask_usd

    # ── Entry/Exit convenience methods ────────────────────────────────────────

    def get_entry_depth(self, symbol: str) -> float:
        """Bottleneck USD depth for entering a delta-neutral position.

        Entry = buy spot (hits asks) + short perp (hits bids).
        Returns min(spot_ask, perp_bid).
        """
        d = self._depths.get(symbol, _SymbolDepth())
        return min(d.spot_ask_usd, d.perp_bid_usd)

    def get_exit_depth(self, symbol: str) -> float:
        """Bottleneck USD depth for exiting a delta-neutral position.

        Exit = sell spot (hits bids) + cover perp short (hits asks).
        Returns min(spot_bid, perp_ask).
        """
        d = self._depths.get(symbol, _SymbolDepth())
        return min(d.spot_bid_usd, d.perp_ask_usd)

    # ── Individual cache accessors ────────────────────────────────────────────

    def spot_ask_depth(self, symbol: str) -> float:
        return self._depths.get(symbol, _SymbolDepth()).spot_ask_usd

    def spot_bid_depth(self, symbol: str) -> float:
        return self._depths.get(symbol, _SymbolDepth()).spot_bid_usd

    def perp_bid_depth(self, symbol: str) -> float:
        return self._depths.get(symbol, _SymbolDepth()).perp_bid_usd

    def perp_ask_depth(self, symbol: str) -> float:
        return self._depths.get(symbol, _SymbolDepth()).perp_ask_usd
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/test_depth_tracker.py -v
```
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add bongus/market_data/depth_tracker.py tests/test_depth_tracker.py
git commit -m "feat: add DepthTracker with 4-cache per-symbol spot/perp bid/ask depth tracking"
```

---

## Task 9: `FundingRanker`

**Files:**
- Create: `bongus/market_data/funding_ranker.py`
- Create: `tests/test_funding_ranker.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_funding_ranker.py`:

```python
"""Tests for FundingRanker — single REST call, filtered, sorted funding rates."""
import os
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'bongus', 'market_data')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'bongus', 'core')))

from funding_ranker import FundingRanker


_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

# Annualized funding = lastFundingRate * 3 * 365 = lastFundingRate * 1095
_MOCK_RESPONSE = [
    {"symbol": "BTCUSDT",  "lastFundingRate": "0.0001"},   # 0.0001 * 1095 = 0.1095
    {"symbol": "ETHUSDT",  "lastFundingRate": "0.0003"},   # 0.0003 * 1095 = 0.3285
    {"symbol": "SOLUSDT",  "lastFundingRate": "0.0002"},   # 0.0002 * 1095 = 0.219
    {"symbol": "XRPUSDT",  "lastFundingRate": "0.0005"},   # filtered out
]


def _mock_requests_get(response_data):
    mock_resp = MagicMock()
    mock_resp.json.return_value = response_data
    mock_resp.raise_for_status = MagicMock()
    return mock_resp


def test_initial_rates_are_zero():
    """Before refresh, all rates are 0.0."""
    ranker = FundingRanker(_SYMBOLS)
    assert ranker.get_rate("BTCUSDT") == 0.0
    assert ranker.get_rate("ETHUSDT") == 0.0


def test_unknown_symbol_returns_zero():
    """Symbols not in the ranker return 0.0, not KeyError."""
    ranker = FundingRanker(_SYMBOLS)
    assert ranker.get_rate("PEPEUSDT") == 0.0


def test_refresh_updates_rates():
    """After refresh, rates match annualized values from API."""
    ranker = FundingRanker(_SYMBOLS)
    with patch("requests.get", return_value=_mock_requests_get(_MOCK_RESPONSE)):
        import asyncio
        asyncio.run(ranker.refresh())

    assert abs(ranker.get_rate("BTCUSDT") - 0.1095) < 1e-9
    assert abs(ranker.get_rate("ETHUSDT") - 0.3285) < 1e-9
    assert abs(ranker.get_rate("SOLUSDT") - 0.2190) < 1e-9


def test_refresh_filters_unmonitored_symbols():
    """Symbols not in MONITORED_SYMBOLS are ignored even if in API response."""
    ranker = FundingRanker(_SYMBOLS)
    with patch("requests.get", return_value=_mock_requests_get(_MOCK_RESPONSE)):
        import asyncio
        asyncio.run(ranker.refresh())

    assert ranker.get_rate("XRPUSDT") == 0.0  # not in _SYMBOLS, not tracked


def test_get_ranked_returns_sorted_highest_first():
    """get_ranked returns symbols sorted by annualized rate, highest first."""
    ranker = FundingRanker(_SYMBOLS)
    with patch("requests.get", return_value=_mock_requests_get(_MOCK_RESPONSE)):
        import asyncio
        asyncio.run(ranker.refresh())

    ranked = ranker.get_ranked()
    rates = [r for _, r in ranked]
    assert rates == sorted(rates, reverse=True)
    assert ranked[0][0] == "ETHUSDT"   # highest: 0.3285
    assert ranked[-1][0] == "BTCUSDT"  # lowest: 0.1095


def test_get_ranked_returns_all_monitored_symbols():
    """get_ranked includes every symbol in MONITORED_SYMBOLS, even after refresh."""
    ranker = FundingRanker(_SYMBOLS)
    with patch("requests.get", return_value=_mock_requests_get(_MOCK_RESPONSE)):
        import asyncio
        asyncio.run(ranker.refresh())
    ranked = ranker.get_ranked()
    assert set(s for s, _ in ranked) == set(_SYMBOLS)


def test_refresh_makes_single_http_request():
    """Only one HTTP GET is made regardless of how many symbols are monitored."""
    ranker = FundingRanker(_SYMBOLS)
    with patch("requests.get", return_value=_mock_requests_get(_MOCK_RESPONSE)) as mock_get:
        import asyncio
        asyncio.run(ranker.refresh())
    assert mock_get.call_count == 1
```

- [ ] **Step 2: Run to verify failure**

```bash
python -m pytest tests/test_funding_ranker.py -v
```
Expected: ImportError

- [ ] **Step 3: Implement `funding_ranker.py`**

Create `bongus/market_data/funding_ranker.py`:

```python
"""Funding rate ranker — single REST call, filtered to monitored symbols, sorted highest-first.

Uses asyncio.to_thread to run the blocking requests.get call off the event loop.
Does NOT open parallel requests — Binance returns all symbols in one response.
"""

import asyncio
import logging

import requests

logger = logging.getLogger(__name__)

_ENDPOINT = "https://fapi.binance.com/fapi/v1/premiumIndex"
_FUNDING_PERIODS_PER_YEAR = 1095  # 3 per day × 365


class FundingRanker:
    def __init__(self, symbols: list[str]) -> None:
        self._symbols: set[str] = set(symbols)
        self._rates: dict[str, float] = {s: 0.0 for s in symbols}

    async def refresh(self) -> None:
        """Fetch all funding rates in a single request and update the cache.

        Binance /fapi/v1/premiumIndex with no symbol param returns every market.
        We filter in Python for our monitored symbols.
        """
        try:
            resp = await asyncio.to_thread(
                requests.get, _ENDPOINT, timeout=10
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("FundingRanker: HTTP request failed: %s", exc)
            return

        for item in data:
            symbol = item.get("symbol", "")
            if symbol not in self._symbols:
                continue
            raw_rate = float(item.get("lastFundingRate", 0.0))
            self._rates[symbol] = raw_rate * _FUNDING_PERIODS_PER_YEAR

    def get_rate(self, symbol: str) -> float:
        """Return annualized funding rate for symbol, or 0.0 if not tracked."""
        return self._rates.get(symbol, 0.0)

    def get_ranked(self) -> list[tuple[str, float]]:
        """Return all monitored symbols sorted by annualized rate, highest first."""
        return sorted(self._rates.items(), key=lambda x: x[1], reverse=True)

    async def run_forever(self, interval_s: int = 60) -> None:
        """Refresh funding rates on a fixed interval. Runs indefinitely."""
        while True:
            await self.refresh()
            await asyncio.sleep(interval_s)
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/test_funding_ranker.py -v
```
Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add bongus/market_data/funding_ranker.py tests/test_funding_ranker.py
git commit -m "feat: add FundingRanker — single REST call, filtered, sorted annualized funding rates"
```

---

## Task 10: `RustDataSubscriber`

**Files:**
- Create: `bongus/market_data/rust_data_subscriber.py`
- Create: `tests/test_rust_data_subscriber.py`

The TCP `run()` loop requires a live server to test. Instead, test the `_dispatch` method directly — it is pure Python with no I/O.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_rust_data_subscriber.py`:

```python
"""Tests for RustDataSubscriber._dispatch method."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'bongus', 'market_data')))

from rust_data_subscriber import RustDataSubscriber


def test_dispatch_l2depth_calls_on_depth():
    received = {}

    def on_depth(symbol, market, bids, asks):
        received.update({"symbol": symbol, "market": market, "bids": bids, "asks": asks})

    sub = RustDataSubscriber(on_depth=on_depth)
    sub._dispatch({
        "event": "L2Depth",
        "symbol": "BTCUSDT",
        "market": "perp",
        "bids": [[50000.0, 1.0]],
        "asks": [[50100.0, 0.5]],
    })

    assert received["symbol"] == "BTCUSDT"
    assert received["market"] == "perp"
    assert received["bids"] == [[50000.0, 1.0]]


def test_dispatch_order_update_calls_on_order_update():
    received = {}

    def on_order_update(symbol, status, filled_qty, client_order_id):
        received.update({"symbol": symbol, "status": status, "filled_qty": filled_qty})

    sub = RustDataSubscriber(on_order_update=on_order_update)
    sub._dispatch({
        "event": "OrderUpdate",
        "symbol": "ETHUSDT",
        "status": "FILLED",
        "filled_qty": 1.5,
        "client_order_id": "abc123",
    })

    assert received["symbol"] == "ETHUSDT"
    assert received["status"] == "FILLED"
    assert received["filled_qty"] == 1.5


def test_dispatch_unknown_event_does_not_crash():
    sub = RustDataSubscriber()
    sub._dispatch({"event": "UnknownEvent", "data": "whatever"})  # must not raise


def test_dispatch_no_callbacks_does_not_crash():
    sub = RustDataSubscriber()  # no callbacks registered
    sub._dispatch({"event": "L2Depth", "symbol": "X", "market": "spot", "bids": [], "asks": []})
```

- [ ] **Step 2: Run to verify failure**

```bash
python -m pytest tests/test_rust_data_subscriber.py -v
```
Expected: ImportError (module not yet created)

- [ ] **Step 3: Implement `rust_data_subscriber.py`**

Create `bongus/market_data/rust_data_subscriber.py`:

```python
"""Asyncio TCP client that subscribes to the Rust engine's port 9000 broadcast.

The Rust engine emits newline-delimited JSON. We use StreamReader.readline()
to handle TCP packet fragmentation automatically — never .read().

Expected event shapes:
  {"event": "L2Depth", "symbol": "BTCUSDT", "market": "spot"|"perp",
   "bids": [[price, qty], ...], "asks": [[price, qty], ...]}

  {"event": "OrderUpdate", "symbol": "BTCUSDT", "status": "FILLED",
   "filled_qty": 0.1, "client_order_id": "abc123"}
"""

import asyncio
import json
import logging
from typing import Callable, Any

logger = logging.getLogger(__name__)


class RustDataSubscriber:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9000,
        on_depth: Callable[..., None] | None = None,
        on_order_update: Callable[..., None] | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._on_depth = on_depth
        self._on_order_update = on_order_update
        self._reconnect_delay = 1.0

    async def run(self) -> None:
        """Connect to Rust engine and process events indefinitely with reconnect."""
        while True:
            try:
                reader, _ = await asyncio.open_connection(self._host, self._port)
                self._reconnect_delay = 1.0
                logger.info("Connected to Rust engine at %s:%d", self._host, self._port)
                await self._read_loop(reader)
            except (ConnectionRefusedError, OSError) as exc:
                logger.warning(
                    "Cannot connect to Rust engine (%s). Retrying in %.1fs",
                    exc, self._reconnect_delay,
                )
            except Exception as exc:
                logger.error("Unexpected error in RustDataSubscriber: %s", exc)

            await asyncio.sleep(self._reconnect_delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, 30.0)

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        """Read newline-delimited JSON lines and dispatch to callbacks."""
        while True:
            line = await reader.readline()
            if not line:
                logger.warning("Rust engine closed connection — reconnecting")
                return

            try:
                event = json.loads(line.decode())
            except json.JSONDecodeError as exc:
                logger.warning("Failed to parse event from Rust: %s | raw: %r", exc, line[:200])
                continue

            self._dispatch(event)

    def _dispatch(self, event: dict[str, Any]) -> None:
        event_type = event.get("event")

        if event_type == "L2Depth" and self._on_depth is not None:
            self._on_depth(
                symbol=event.get("symbol", ""),
                market=event.get("market", ""),
                bids=event.get("bids", []),
                asks=event.get("asks", []),
            )
        elif event_type == "OrderUpdate" and self._on_order_update is not None:
            self._on_order_update(
                symbol=event.get("symbol", ""),
                status=event.get("status", ""),
                filled_qty=event.get("filled_qty", 0.0),
                client_order_id=event.get("client_order_id", ""),
            )
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/test_rust_data_subscriber.py -v
```
Expected: All 4 PASS

- [ ] **Step 5: Commit**

```bash
git add bongus/market_data/rust_data_subscriber.py tests/test_rust_data_subscriber.py
git commit -m "feat: add RustDataSubscriber — asyncio TCP listener for Rust port 9000 broadcast"
```

---

## Task 11: `CorrelationBreaker`

**Files:**
- Create: `bongus/portfolio/correlation_breaker.py`
- Create: `tests/test_correlation_breaker.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_correlation_breaker.py`:

```python
"""Tests for CorrelationBreaker state machine.

States are mutually exclusive and collectively exhaustive:
  CLEAR:     negative_ratio < 0.50  (includes 0%)
  HALTED:    0.50 <= negative_ratio < 1.00
  EMERGENCY: negative_ratio == 1.00
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'bongus', 'portfolio')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'bongus', 'core')))

from correlation_breaker import CorrelationBreaker

_THRESHOLD = 0.01  # EXIT_ANN_FUNDING_THRESHOLD from config


def _above(n: int) -> dict[str, float]:
    """n positions all above threshold."""
    return {f"SYM{i}": _THRESHOLD + 0.05 for i in range(n)}


def _below(n: int, start: int = 0) -> dict[str, float]:
    """n positions all below threshold."""
    return {f"SYM{i}": _THRESHOLD - 0.001 for i in range(start, start + n)}


def test_empty_portfolio_is_clear():
    """No positions → CLEAR with allow_new_entries=True."""
    breaker = CorrelationBreaker()
    decision = breaker.evaluate({})
    assert decision.state == "CLEAR"
    assert decision.allow_new_entries is True
    assert decision.positions_to_exit == []


def test_all_above_threshold_is_clear():
    """0% negative → CLEAR."""
    breaker = CorrelationBreaker()
    decision = breaker.evaluate(_above(4))
    assert decision.state == "CLEAR"
    assert decision.allow_new_entries is True


def test_40_percent_negative_is_still_clear():
    """2/5 = 40% negative → CLEAR (below 50% halt threshold)."""
    breaker = CorrelationBreaker()
    positions = {**_above(3), **_below(2, start=3)}
    decision = breaker.evaluate(positions)
    assert decision.state == "CLEAR"
    assert decision.allow_new_entries is True


def test_50_percent_negative_is_halted():
    """2/4 = 50% negative → HALTED."""
    breaker = CorrelationBreaker()
    positions = {**_above(2), **_below(2, start=2)}
    decision = breaker.evaluate(positions)
    assert decision.state == "HALTED"
    assert decision.allow_new_entries is False
    assert decision.positions_to_exit == []  # HALTED does not force exits


def test_75_percent_negative_is_halted():
    """3/4 = 75% negative → HALTED (not EMERGENCY)."""
    breaker = CorrelationBreaker()
    positions = {**_above(1), **_below(3, start=1)}
    decision = breaker.evaluate(positions)
    assert decision.state == "HALTED"


def test_100_percent_negative_is_emergency():
    """4/4 = 100% negative → EMERGENCY."""
    breaker = CorrelationBreaker()
    positions = _below(4)
    decision = breaker.evaluate(positions)
    assert decision.state == "EMERGENCY"
    assert decision.allow_new_entries is False
    assert set(decision.positions_to_exit) == set(positions.keys())


def test_emergency_positions_to_exit_is_all_symbols():
    """EMERGENCY: positions_to_exit contains every open symbol."""
    breaker = CorrelationBreaker()
    positions = _below(3)
    decision = breaker.evaluate(positions)
    assert sorted(decision.positions_to_exit) == sorted(positions.keys())


def test_single_position_negative_is_emergency():
    """1/1 = 100% → EMERGENCY, not CLEAR."""
    breaker = CorrelationBreaker()
    decision = breaker.evaluate({"BTCUSDT": _THRESHOLD - 0.001})
    assert decision.state == "EMERGENCY"


def test_single_position_positive_is_clear():
    """1/1 all positive → CLEAR."""
    breaker = CorrelationBreaker()
    decision = breaker.evaluate({"BTCUSDT": _THRESHOLD + 0.05})
    assert decision.state == "CLEAR"
```

- [ ] **Step 2: Run to verify failure**

```bash
python -m pytest tests/test_correlation_breaker.py -v
```
Expected: ImportError

- [ ] **Step 3: Implement `correlation_breaker.py`**

Create `bongus/portfolio/correlation_breaker.py`:

```python
"""Cross-asset correlation circuit breaker.

Monitors open positions' funding rates and returns a graduated decision:
  CLEAR:     < 50% of positions below EXIT_ANN_FUNDING_THRESHOLD
  HALTED:    ≥ 50% but < 100% below threshold — block new entries
  EMERGENCY: 100% below threshold — exit all positions immediately

States are mutually exclusive and collectively exhaustive.
Empty portfolio always returns CLEAR.
"""

from dataclasses import dataclass, field
from typing import Literal

from bongus.core.config import (
    EXIT_ANN_FUNDING_THRESHOLD,
    BREAKER_HALT_RATIO,
    BREAKER_EMERGENCY_RATIO,
)


@dataclass
class BreakerDecision:
    state: Literal["CLEAR", "HALTED", "EMERGENCY"]
    allow_new_entries: bool
    positions_to_exit: list[str]
    reason: str


class CorrelationBreaker:
    def evaluate(self, open_positions: dict[str, float]) -> BreakerDecision:
        """Evaluate portfolio state.

        Args:
            open_positions: {symbol: current_ann_funding_rate}

        Returns:
            BreakerDecision with state, entry permission, and any forced exits.
        """
        if not open_positions:
            return BreakerDecision(
                state="CLEAR",
                allow_new_entries=True,
                positions_to_exit=[],
                reason="no open positions",
            )

        negative = [
            s for s, rate in open_positions.items()
            if rate < EXIT_ANN_FUNDING_THRESHOLD
        ]
        ratio = len(negative) / len(open_positions)

        if ratio < BREAKER_HALT_RATIO:
            return BreakerDecision(
                state="CLEAR",
                allow_new_entries=True,
                positions_to_exit=[],
                reason=f"{len(negative)}/{len(open_positions)} positions below threshold",
            )

        if ratio < BREAKER_EMERGENCY_RATIO:
            return BreakerDecision(
                state="HALTED",
                allow_new_entries=False,
                positions_to_exit=[],
                reason=f"{len(negative)}/{len(open_positions)} positions below threshold — halted",
            )

        return BreakerDecision(
            state="EMERGENCY",
            allow_new_entries=False,
            positions_to_exit=list(open_positions.keys()),
            reason="all positions below funding threshold — emergency exit",
        )
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/test_correlation_breaker.py -v
```
Expected: All 9 tests PASS

- [ ] **Step 5: Commit**

```bash
git add bongus/portfolio/correlation_breaker.py tests/test_correlation_breaker.py
git commit -m "feat: add CorrelationBreaker with graduated CLEAR/HALTED/EMERGENCY circuit breaker"
```

---

## Task 12: `PortfolioAllocator`

**Files:**
- Create: `bongus/portfolio/portfolio_allocator.py`
- Create: `tests/test_portfolio_allocator.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_portfolio_allocator.py`:

```python
"""Tests for PortfolioAllocator — sizing, liquidity filter, rotation logic."""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'bongus', 'portfolio')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'bongus', 'core')))

from portfolio_allocator import PortfolioAllocator, OpenPosition


_TARGET_NOTIONAL = 5_000.0   # CAPITAL_PER_SLOT_USD * TARGET_LEVERAGE
_MIN_DEPTH = 5 * _TARGET_NOTIONAL  # 25_000.0


def _mock_depth(entry: float, exit_: float) -> MagicMock:
    d = MagicMock()
    d.get_entry_depth.return_value = entry
    d.get_exit_depth.return_value = exit_
    d.spot_ask_depth.return_value = entry
    d.perp_bid_depth.return_value = entry
    d.spot_bid_depth.return_value = exit_
    d.perp_ask_depth.return_value = exit_
    return d


def _mock_ranker(rates: dict[str, float]) -> MagicMock:
    r = MagicMock()
    r.get_rate.side_effect = lambda s: rates.get(s, 0.0)
    r.get_ranked.return_value = sorted(rates.items(), key=lambda x: x[1], reverse=True)
    return r


def test_liquidity_filter_blocks_thin_book():
    """Symbol with insufficient depth is skipped regardless of funding rate."""
    depth = _mock_depth(entry=_MIN_DEPTH - 1.0, exit_=_MIN_DEPTH)  # just below threshold
    ranker = _mock_ranker({"PEPEUSDT": 2.0})  # huge funding rate
    alloc = PortfolioAllocator(depth, ranker)

    decision = alloc.decide([])
    assert not any(s == "PEPEUSDT" for s, _ in decision.enter)


def test_liquidity_filter_passes_thick_book():
    """Symbol with sufficient depth is included."""
    depth = _mock_depth(entry=_MIN_DEPTH + 1.0, exit_=_MIN_DEPTH + 1.0)
    ranker = _mock_ranker({"BTCUSDT": 0.5})
    alloc = PortfolioAllocator(depth, ranker)

    decision = alloc.decide([])
    assert any(s == "BTCUSDT" for s, _ in decision.enter)


def test_fills_empty_slots_with_top_ranked():
    """With 0 open positions, top N liquid symbols are entered."""
    depth = _mock_depth(entry=_MIN_DEPTH * 10, exit_=_MIN_DEPTH * 10)
    ranker = _mock_ranker({"ETHUSDT": 1.0, "SOLUSDT": 0.8, "BTCUSDT": 0.5, "DOGEUSDT": 0.3, "PEPEUSDT": 0.2})
    alloc = PortfolioAllocator(depth, ranker)

    decision = alloc.decide([])
    assert len(decision.enter) == 4  # MAX_CONCURRENT_POSITIONS
    symbols_entered = [s for s, _ in decision.enter]
    assert "ETHUSDT" in symbols_entered  # highest rate


def test_full_portfolio_no_new_entries_without_rotation():
    """With MAX_CONCURRENT_POSITIONS held and no rotation candidate, no entries."""
    depth = _mock_depth(entry=_MIN_DEPTH * 10, exit_=_MIN_DEPTH * 10)
    held_rate = 0.5
    ranker = _mock_ranker({"BTCUSDT": held_rate, "ETHUSDT": held_rate + 0.01, "SOLUSDT": held_rate, "DOGEUSDT": held_rate})
    positions = [
        OpenPosition("BTCUSDT", _TARGET_NOTIONAL, held_rate),
        OpenPosition("ETHUSDT", _TARGET_NOTIONAL, held_rate + 0.01),
        OpenPosition("SOLUSDT", _TARGET_NOTIONAL, held_rate),
        OpenPosition("DOGEUSDT", _TARGET_NOTIONAL, held_rate),
    ]
    alloc = PortfolioAllocator(depth, ranker)

    decision = alloc.decide(positions)
    # No slots free, rate gaps too small → no entries
    assert len(decision.enter) == 0


def test_no_rotation_when_gap_below_minimum():
    """Rotation is blocked if rate gap < ROTATION_MIN_GAP_ANN (5%)."""
    depth = _mock_depth(entry=_MIN_DEPTH * 10, exit_=_MIN_DEPTH * 10)
    current_rate = 0.30
    new_rate = current_rate + 0.03  # only 3% gap — below 5% minimum
    ranker = _mock_ranker({"BTCUSDT": current_rate, "NEWCOIN": new_rate})
    position = OpenPosition("BTCUSDT", _TARGET_NOTIONAL, current_rate)
    alloc = PortfolioAllocator(depth, ranker)

    decision = alloc.decide([position])
    exit_symbols = [s for s, _ in decision.exit]
    assert "BTCUSDT" not in exit_symbols


def test_rotation_triggers_when_gap_and_payback_met():
    """Rotation fires when rate gap > 5% AND fees pay back within 8 hours."""
    # Use a very deep book so friction costs are tiny
    depth = _mock_depth(entry=10_000_000.0, exit_=10_000_000.0)
    current_rate = 0.10
    new_rate = 0.50  # 40% gap — well above 5% minimum; tiny friction → fast payback
    ranker = _mock_ranker({"BTCUSDT": current_rate, "HIGHCOIN": new_rate})
    position = OpenPosition("BTCUSDT", _TARGET_NOTIONAL, current_rate)
    alloc = PortfolioAllocator(depth, ranker)

    decision = alloc.decide([position])
    exit_symbols = [s for s, _ in decision.exit]
    assert "BTCUSDT" in exit_symbols


def test_already_held_symbols_not_re_entered():
    """A symbol already in open positions is not added to enter list."""
    depth = _mock_depth(entry=_MIN_DEPTH * 10, exit_=_MIN_DEPTH * 10)
    ranker = _mock_ranker({"BTCUSDT": 0.5, "ETHUSDT": 0.4})
    positions = [OpenPosition("BTCUSDT", _TARGET_NOTIONAL, 0.5)]
    alloc = PortfolioAllocator(depth, ranker)

    decision = alloc.decide(positions)
    enter_symbols = [s for s, _ in decision.enter]
    assert "BTCUSDT" not in enter_symbols


def test_exit_notional_is_target_notional():
    """All enter decisions use the configured target notional."""
    depth = _mock_depth(entry=_MIN_DEPTH * 10, exit_=_MIN_DEPTH * 10)
    ranker = _mock_ranker({"ETHUSDT": 0.5})
    alloc = PortfolioAllocator(depth, ranker)

    decision = alloc.decide([])
    for _, notional in decision.enter:
        assert abs(notional - _TARGET_NOTIONAL) < 0.01


def test_rotation_decision_includes_rotation_targets():
    """AllocationDecision.rotation_targets maps exited symbol to its structured entry target."""
    depth = _mock_depth(entry=10_000_000.0, exit_=10_000_000.0)
    ranker = _mock_ranker({"BTCUSDT": 0.10, "HIGHCOIN": 0.50})
    position = OpenPosition("BTCUSDT", _TARGET_NOTIONAL, 0.10)
    alloc = PortfolioAllocator(depth, ranker)

    decision = alloc.decide([position])
    assert "BTCUSDT" in decision.rotation_targets, "rotation_targets must contain the exited symbol"
    assert decision.rotation_targets["BTCUSDT"] == "HIGHCOIN", "rotation target should be the higher-rate symbol"
```

- [ ] **Step 2: Run to verify failure**

```bash
python -m pytest tests/test_portfolio_allocator.py -v
```
Expected: ImportError

- [ ] **Step 3: Implement `portfolio_allocator.py`**

Create `bongus/portfolio/portfolio_allocator.py`:

```python
"""Capital allocation engine — fractional sizing, liquidity filter, rotation logic.

Evaluation pipeline (order matters):
  1. FundingRanker produces sorted candidate list
  2. Liquidity filter eliminates candidates with insufficient depth
  3. Fill empty slots with top-ranked filtered candidates
  4. Evaluate rotation for each existing position (on filtered candidates only)

Rotation requires BOTH:
  - rate_gap > ROTATION_MIN_GAP_ANN (5% annualized)
  - total friction pays back within ROTATION_MAX_PAYBACK_DAYS (8 hours)

Total friction = exit(current) + entry(new) + expected_exit(new)
The third term prevents rotating into illiquid coins that are cheap to enter
but expensive to exit ("roach motel" trap).
"""

from dataclasses import dataclass, field

from bongus.core.config import (
    MAX_CONCURRENT_POSITIONS,
    CAPITAL_PER_SLOT_USD,
    TARGET_LEVERAGE,
    LIQUIDITY_FILTER_MULTIPLIER,
    ROTATION_MIN_GAP_ANN,
    ROTATION_MAX_PAYBACK_DAYS,
)
from bongus.engine.cost_model import blended_entry_cost, blended_exit_cost


@dataclass
class OpenPosition:
    symbol: str
    notional_usd: float
    ann_funding: float


@dataclass
class AllocationDecision:
    enter: list[tuple[str, float]]          # [(symbol, notional_usd)]
    exit: list[tuple[str, str]]             # [(symbol, reason)]
    hold: list[str]                         # symbols to keep untouched
    rotation_targets: dict[str, str] = field(default_factory=dict)  # {exited → entry_target}


class PortfolioAllocator:
    def __init__(self, depth_tracker, funding_ranker) -> None:
        self._depth = depth_tracker
        self._funding = funding_ranker

    def decide(self, open_positions: list[OpenPosition]) -> AllocationDecision:
        """Produce an allocation decision for this cycle.

        Args:
            open_positions: list of currently held positions

        Returns:
            AllocationDecision with enter/exit/hold lists.
            Always execute exits before entries.
        """
        target_notional = CAPITAL_PER_SLOT_USD * TARGET_LEVERAGE
        open_symbols = {p.symbol for p in open_positions}

        # Step 1 & 2: Get ranked candidates, apply liquidity filter
        candidates: list[tuple[str, float]] = []
        for symbol, rate in self._funding.get_ranked():
            if self._depth.get_entry_depth(symbol) >= LIQUIDITY_FILTER_MULTIPLIER * target_notional:
                candidates.append((symbol, rate))

        # Step 3: Fill empty slots
        enter: list[tuple[str, float]] = []
        available_slots = MAX_CONCURRENT_POSITIONS - len(open_positions)
        for symbol, _rate in candidates:
            if available_slots <= 0:
                break
            if symbol not in open_symbols:
                enter.append((symbol, target_notional))
                open_symbols.add(symbol)
                available_slots -= 1

        # Step 4: Evaluate rotation for each existing position
        exits: list[tuple[str, str]] = []
        rotation_targets: dict[str, str] = {}  # {exited_symbol: entry_target}
        for position in open_positions:
            target = self._find_rotation_target(position, candidates, target_notional)
            if target:
                exits.append((position.symbol, f"rotation to {target}"))
                rotation_targets[position.symbol] = target

        exit_symbols = {s for s, _ in exits}
        hold = [p.symbol for p in open_positions if p.symbol not in exit_symbols]

        return AllocationDecision(enter=enter, exit=exits, hold=hold, rotation_targets=rotation_targets)

    def _find_rotation_target(
        self,
        position: OpenPosition,
        candidates: list[tuple[str, float]],
        target_notional: float,
    ) -> str | None:
        """Return the first candidate that passes both rotation conditions, or None."""
        current_exit_depth = self._depth.get_exit_depth(position.symbol)

        for new_symbol, new_rate in candidates:
            if new_symbol == position.symbol:
                continue

            rate_gap = new_rate - position.ann_funding
            if rate_gap <= ROTATION_MIN_GAP_ANN:
                continue  # gap too small

            new_entry_depth = self._depth.get_entry_depth(new_symbol)
            new_exit_depth = self._depth.get_exit_depth(new_symbol)

            total_friction_usd = (
                blended_exit_cost(position.notional_usd, depth_usd=current_exit_depth)
                + blended_entry_cost(target_notional, depth_usd=new_entry_depth)
                + blended_exit_cost(target_notional, depth_usd=new_exit_depth)  # expected future exit
            )

            incremental_daily_income = (rate_gap / 365) * target_notional
            if incremental_daily_income <= 0:
                continue  # guard against misconfiguration

            payback_days = total_friction_usd / incremental_daily_income
            if payback_days <= ROTATION_MAX_PAYBACK_DAYS:
                return new_symbol

        return None
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/test_portfolio_allocator.py -v
```
Expected: All 8 tests PASS

- [ ] **Step 5: Run full test suite to check for regressions**

```bash
python -m pytest tests/ -v
```
Expected: All existing tests still pass

- [ ] **Step 6: Commit**

```bash
git add bongus/portfolio/portfolio_allocator.py tests/test_portfolio_allocator.py
git commit -m "feat: add PortfolioAllocator with fractional sizing, liquidity filter, and rotation logic"
```

---

## Task 13: `live_trader_v2.py` Orchestrator

**Files:**
- Create: `scripts/live_trader_v2.py`

This is the thin orchestrator wiring all components together. Manual integration testing only.

- [ ] **Step 1: Implement `live_trader_v2.py`**

Create `scripts/live_trader_v2.py`:

```python
"""Multi-symbol live trader orchestrator.

Wires together:
  - RustDataSubscriber (depth + fill confirmations from Rust port 9000)
  - FundingRanker (single REST call every 60s)
  - CorrelationBreaker (portfolio-level circuit breaker)
  - PortfolioAllocator (sizing, liquidity filter, rotation)
  - ExecutionClient (ZMQ PUSH to Rust)
  - StateWriter/StateReader (SQLite shared state)

Execution invariant: exits are dispatched first; ENTER for a rotation target
only fires after FILLED confirmation from Rust (or timeout fallback).

The original live_trader.py is preserved as a single-symbol fallback.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

from dotenv import load_dotenv

from bongus.core.config import (
    MONITORED_SYMBOLS,
    CAPITAL_PER_SLOT_USD,
    TARGET_LEVERAGE,
    ROTATION_CONFIRM_TIMEOUT_S,
    EXIT_ANN_FUNDING_THRESHOLD,
)
from bongus.engine.state_store import StateWriter, StateReader
from bongus.ipc.execution import ExecutionClient
from bongus.market_data.depth_tracker import DepthTracker
from bongus.market_data.funding_ranker import FundingRanker
from bongus.market_data.rust_data_subscriber import RustDataSubscriber
from bongus.portfolio.correlation_breaker import CorrelationBreaker
from bongus.portfolio.portfolio_allocator import OpenPosition, PortfolioAllocator

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("live_trader_v2")


class LiveTraderV2:
    def __init__(self) -> None:
        self.depth_tracker = DepthTracker()
        self.funding_ranker = FundingRanker(MONITORED_SYMBOLS)
        self.breaker = CorrelationBreaker()
        self.allocator = PortfolioAllocator(self.depth_tracker, self.funding_ranker)
        self.execution = ExecutionClient(endpoint="tcp://127.0.0.1:5555")
        self.state_writer = StateWriter()
        self.state_reader = StateReader()

        # Pending exit tracking: symbol → asyncio.Event (set when FILLED received from Rust).
        # Note: spec described this as set[str]; dict[str, Event] enables per-symbol await
        # without a global polling loop — deliberate improvement over the spec.
        self._exit_events: dict[str, asyncio.Event] = {}

        # Mark price cache: populated from top-of-book perp bids in depth events.
        # Used by _dispatch_enter to compute base-asset qty from notional.
        self._mark_prices: dict[str, float] = {}

        self.subscriber = RustDataSubscriber(
            on_depth=self._on_depth_update,
            on_order_update=self._on_order_update,
        )

    def _on_depth_update(self, symbol: str, market: str, bids: list, asks: list) -> None:
        """Update depth cache; capture top perp bid as mark price proxy."""
        self.depth_tracker.on_l2depth(symbol, market, bids, asks)
        if market == "perp" and bids:
            # bids is list of [price, qty] — top bid is bids[0]
            self._mark_prices[symbol] = float(bids[0][0])

    def _on_order_update(self, symbol: str, status: str, **_kwargs) -> None:
        if status == "FILLED" and symbol in self._exit_events:
            logger.info("Exit FILLED confirmed for %s — releasing capital slot", symbol)
            self._exit_events[symbol].set()

    def _get_open_positions(self) -> list[OpenPosition]:
        rows = self.state_reader.get_positions()
        positions = []
        for r in rows:
            spot_price = r.get("spot_live", 0.0)
            # If spot_live is populated (price > $1), use actual qty × price.
            # Otherwise fall back to configured slot size (e.g., cold start with stale cache).
            if spot_price > 1.0:
                notional_usd = r["qty"] * spot_price
            else:
                notional_usd = CAPITAL_PER_SLOT_USD * TARGET_LEVERAGE
            positions.append(OpenPosition(
                symbol=r["symbol"],
                notional_usd=notional_usd,
                ann_funding=self.funding_ranker.get_rate(r["symbol"]),
            ))
        return positions

    def _dispatch_exit(self, symbol: str, urgency: float = 0.8) -> asyncio.Event:
        """Send EXIT instruction and return an Event that fires when FILLED."""
        event = asyncio.Event()
        self._exit_events[symbol] = event
        self.execution.send_order_intent({
            "symbol": symbol,
            "intent": "EXIT_LONG",
            "quantity": 0.0,      # Rust reads from tracked position
            "urgency": urgency,
            "max_slippage_bps": 20.0 if urgency >= 1.0 else 5.0,
            "exposure_scale": 1.0,
        })
        logger.info("EXIT dispatched for %s (urgency=%.1f)", symbol, urgency)
        return event

    def _dispatch_enter(self, symbol: str, notional_usd: float) -> None:
        """Send ENTER instruction. Skips if no mark price has been received yet."""
        mark_price = self._mark_prices.get(symbol, 0.0)
        if mark_price <= 0.0:
            logger.warning(
                "No mark price for %s yet — skipping ENTER (will retry next cycle)", symbol
            )
            return
        qty = round(notional_usd / mark_price, 5)
        self.execution.send_order_intent({
            "symbol": symbol,
            "intent": "ENTER_LONG",
            "quantity": qty,
            "urgency": 0.8,
            "max_slippage_bps": 5.0,
            "exposure_scale": 1.0,
        })
        logger.info("ENTER dispatched for %s qty=%.5f (notional=$%.0f, price=$%.2f)",
                    symbol, qty, notional_usd, mark_price)

    async def _await_exit_confirmation(self, symbol: str) -> bool:
        """Wait for FILLED event. Returns True if confirmed, False on timeout."""
        event = self._exit_events.get(symbol)
        if event is None:
            return False
        try:
            await asyncio.wait_for(event.wait(), timeout=ROTATION_CONFIRM_TIMEOUT_S)
            return True
        except asyncio.TimeoutError:
            logger.warning("Exit confirmation timeout for %s — entry will be deferred", symbol)
            return False
        finally:
            self._exit_events.pop(symbol, None)

    async def _trading_loop(self) -> None:
        while True:
            try:
                open_positions = self._get_open_positions()
                funding_rates = {p.symbol: p.ann_funding for p in open_positions}

                # ── 1. Circuit breaker ───────────────────────────────────────
                breaker_decision = self.breaker.evaluate(funding_rates)

                if breaker_decision.state == "EMERGENCY":
                    logger.warning("CIRCUIT BREAKER: EMERGENCY — exiting all positions")
                    for symbol in breaker_decision.positions_to_exit:
                        if symbol not in self._exit_events:
                            self._dispatch_exit(symbol, urgency=1.0)
                    await asyncio.sleep(1)
                    continue

                if not breaker_decision.allow_new_entries:
                    logger.info("CIRCUIT BREAKER: HALTED — blocking new entries")
                    await asyncio.sleep(1)
                    continue

                # ── 2. Allocation decision ───────────────────────────────────
                decision = self.allocator.decide(open_positions)

                # ── 3. Dispatch exits ────────────────────────────────────────
                target_notional = CAPITAL_PER_SLOT_USD * TARGET_LEVERAGE
                for symbol, reason in decision.exit:
                    if symbol not in self._exit_events:
                        logger.info("Rotation: exiting %s (%s)", symbol, reason)
                        self._dispatch_exit(symbol, urgency=0.8)

                # ── 4. Await exit confirmations, dispatch rotation entries ────
                # Use AllocationDecision.rotation_targets (structured field, not string parsing)
                # Exit-before-enter invariant: ENTER only fires after FILLED confirmed.
                for exited_symbol, rotation_target in decision.rotation_targets.items():
                    confirmed = await self._await_exit_confirmation(exited_symbol)
                    if confirmed:
                        self._dispatch_enter(rotation_target, target_notional)
                    else:
                        logger.warning(
                            "Skipping rotation entry for %s — exit of %s unconfirmed",
                            rotation_target, exited_symbol,
                        )

                # ── 5. Dispatch entries for empty slots ─────────────────────
                for symbol, notional in decision.enter:
                    if symbol not in self._exit_events:
                        self._dispatch_enter(symbol, notional)

            except Exception as exc:
                logger.error("Error in trading loop: %s", exc, exc_info=True)

            await asyncio.sleep(1)

    async def run(self) -> None:
        logger.info("Starting LiveTraderV2 — monitoring %d symbols", len(MONITORED_SYMBOLS))
        await asyncio.gather(
            self.subscriber.run(),
            self.funding_ranker.run_forever(interval_s=60),
            self._trading_loop(),
        )


async def main() -> None:
    trader = LiveTraderV2()
    await trader.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("LiveTraderV2 stopped.")
```

- [ ] **Step 2: Verify syntax**

```bash
python -c "import ast; ast.parse(open('scripts/live_trader_v2.py').read()); print('syntax OK')"
```
Expected: `syntax OK`

- [ ] **Step 3: Integration smoke test (requires Rust engine running)**

Start the Rust engine in one terminal:
```bash
cd execution_engine
MONITORED_SYMBOLS=BTCUSDT cargo run
```

In another terminal, run the v2 trader in dry-run observation mode:
```bash
cd C:\Users\gabri\Bongus\bongus_trading_bot
USE_TESTNET=true python scripts/live_trader_v2.py
```

Observe logs for:
- `"Connected to Rust engine at 127.0.0.1:9000"`
- `"Monitoring 8 symbols"`
- Depth update events flowing through

Ctrl+C to stop. No trades should be dispatched if funding rates are below `ENTRY_ANN_FUNDING_THRESHOLD`.

- [ ] **Step 4: Run full test suite one final time**

```bash
python -m pytest tests/ -v
```
Expected: All tests pass

- [ ] **Step 5: Commit**

```bash
git add scripts/live_trader_v2.py
git commit -m "feat: add live_trader_v2.py multi-symbol orchestrator with circuit breaker, allocator, and exit confirmation"
```

---

## Summary Checklist

| Task | Component | Status |
|---|---|---|
| 1 | Config constants | - [ ] |
| 2 | `blended_exit_cost` + cost model | - [ ] |
| 3 | Rust: `MarketType` enum + `L2Depth` | - [ ] |
| 4 | Rust: Spot WS + symbol env var | - [ ] |
| 5 | Rust: `chase_states` HashMap | - [ ] |
| 6 | Rust: FILLED broadcast | - [ ] |
| 7 | Python package scaffolding | - [ ] |
| 8 | `DepthTracker` | - [ ] |
| 9 | `FundingRanker` | - [ ] |
| 10 | `RustDataSubscriber` | - [ ] |
| 11 | `CorrelationBreaker` | - [ ] |
| 12 | `PortfolioAllocator` | - [ ] |
| 13 | `live_trader_v2.py` | - [ ] |
