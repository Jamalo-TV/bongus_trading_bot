# Improvement Plan — Deterministic State Reconciliation & Uninterruptible Execution (2026-06-17)

## 0. Scope

This plan addresses the most impactful reliability improvement to the Bongus execution system: replacing reactive safe-mode halts with proactive, runtime state reconciliation that keeps the engine trading through failures that currently cause full stops.

Constraint: this document defines the implementation plan only. No code changes are included here.

## 1. Problem Statement

### 1.1 Current Failure Modes That Halt Trading

The system currently enters safe mode (blocking all new entries) or halts during startup for the following reasons, each of which is recoverable without a full halt:

| Safe Mode Reason / Event | Trigger | Current Behavior | Impact |
|---|---|---|---|
| `hedge_gap` | Spot ↔ perp quantity divergence | Block all trading, require manual review | Misses funding payments on healthy symbols |
| `exit_failure` | EXIT_LONG dispatch fails (REST timeout, rate limit) | Block all trading | Punishes entire portfolio for one symbol's failure |
| `startup_exit_candidate` | Orphaned position detected at startup | Block until exited | Delays normal operation by minutes |
| `startup_manual_review` | Position requires manual audit at startup | Block all trading | Punishes entire portfolio for one symbol's recovery state |
| `startup_reconciliation_failed` | Startup snapshot fetch fails | Block until next attempt | System stuck if Binance returns 5xx |
| `blocked_preflight` | Preflight request timeout (HTTP 408) or server error (5xx) | Halt trading and block startup | Transient network blips permanently stall the bot |
| `audit_unavailable` | ≥3 consecutive audit REST failures | Block all trading | Network blip → total halt |
| `naked_leg_unwind_stuck` | Single-leg exit can't complete | Block all trading | Dust position blocks entire portfolio |
| `heartbeat_bridge` | Rust engine heartbeat stale | Block all trading | Brief WS reconnect freezes Python brain |
| `execution_bridge` | ZMQ send to Rust fails | Block all trading | Transient ZMQ timeout → halt |

### 1.2 The Rust-Side WS Disconnect Problem

In `order_manager.rs` line 1782, a single WS stream disconnection triggers `self.chase_states.clear()`, which aborts **all** in-flight execution cycles across all symbols — including symbols whose WS connections are healthy. The comment at line 1781 already identifies this as wrong:

```rust
// Do not reset state to Disconnected if ONE out of many WS streams drops.
// This causes the entire engine to go blind to all other streams.
// Instead, just clear chase state or rely on reconnection logic.
// self.state = SystemState::Disconnected;
self.chase_states.clear();  // <-- Still too aggressive
```

### 1.3 The Position Drift Blind Spot

The Rust engine has no runtime mechanism to detect divergence between `self.tracked_positions` and exchange ground truth **during** operation. The only reconciliation happens at startup (`execute_reconciliation_sequence`, lines 2822–3013). If a fill is missed during a WS blip, the tracked position drifts permanently until the next full restart.

The Python side has `_audit_tracked_positions_against_exchange` (line 2989) which runs every 300s (or 60s in guarded mode), but it only audits the Python-side state store — it does not feed corrections back to the Rust engine's `tracked_positions`.

### 1.4 What This Plan Delivers

1. **Rust-side runtime position audit** — periodic REST-based verification of `tracked_positions` against exchange truth, without halting.
2. **Per-symbol WS disconnect isolation** — a WS drop on ETHUSDT does not abort an active BTCUSDT chase.
3. **Automatic position adoption** — orphaned positions detected at runtime are adopted into Rust tracking and propagated to Python via telemetry.
4. **Per-symbol safe mode granularity on the Python side** — a hedge gap or startup position issue (exit candidate / manual review) on one symbol blocks only that symbol, not the portfolio.
5. **Closed-loop Python ↔ Rust reconciliation** — Python's audit findings are sent to Rust as corrections, not just logged.
6. **Preflight and Startup Resilience** — retry-with-backoff and endpoint fallback to bypass transient errors (like HTTP 408 / 5xx timeouts) that block preflight.
7. **Spot Balance Reconciliation** — periodic REST audit and correction of `self.spot_balances` to prevent false `INSUFFICIENT_SPOT_BALANCE` rejections and subsequent cooldown triggers.

## 2. Existing Infrastructure Inventory

Before adding anything, inventory exactly what already exists to avoid duplication:

### 2.1 Already Implemented (Do NOT Rebuild)

| Capability | Location | What It Does |
|---|---|---|
| Startup reconciliation | `order_manager.rs:2822–3013` | On WS connect, fetches open orders via REST, cancels orphans, resolves dangling internal orders |
| `RESTORE_POSITION` intent | `order_manager.rs:1314–1329`, `execution.py:32–59` | Python sends ZMQ instruction to re-adopt a position into Rust `tracked_positions` |
| Python periodic audit | `live_trader_v2.py:2989–3046` | Calls `GET /fapi/v2/positionRisk` + `GET /api/v3/account`, compares to local state, removes stale positions |
| Watchdog restart | `king_watchdog.py` | Detects crashed processes, restarts with exponential backoff |
| Safe mode flag system | `live_trader_v2.py:1528–1539` | Per-flag tracking → `_recompute_runtime_mode()` → `SAFE_MODE` or `LIVE_WITH_SYMBOL_BLOCKS` |
| Chase state lifecycle | `order_manager.rs:207–264` | Full state machine: Idle → DualMakerPlaced → LegFilledWaiting → LeggingDefenseTakerPlaced → Completed |
| Per-symbol toxicity gate | `order_manager.rs:1835–1846` | OBI/spread filter per-symbol, not global |

### 2.2 Missing (This Plan Adds)

| Capability | Why It's Needed |
|---|---|
| Rust-side runtime position audit | Catches drift between fills received via WS and actual exchange state |
| Per-symbol chase state cleanup on WS disconnect | Prevents healthy symbols from being collateral damage |
| Rust → Python `PositionDivergence` telemetry event | Closes the reconciliation loop |
| Python → Rust position correction via `RESTORE_POSITION` on divergence | Automated recovery without restart |
| Per-symbol safe mode isolation for `exit_failure`, `hedge_gap` | Scoped blast radius |
| Retry-with-backoff for transient safe mode causes | Auto-clear `execution_bridge`, `audit_unavailable` after brief recovery |
| Rust-side spot balance reconciliation | Fixes drift in `self.spot_balances` which causes false `INSUFFICIENT_SPOT_BALANCE` |
| Preflight REST retry & fallback | Prevents transient network/Binance timeouts (HTTP 408) from failing startup |
| Isolated startup blocks | Restricts `startup_exit_candidate` and `startup_manual_review` blocks to the affected symbol only |

## 3. Implementation Plan

### Phase 1: Quick Wins (Week 1–2)

These are low-risk, high-value changes that can be deployed independently.

#### 1A. Per-Symbol WS Disconnect Isolation (Rust)

**File**: `execution_engine/src/order_manager.rs`

**Current** (line 1782):
```rust
WsEvent::Disconnected { symbol } => {
    self.chase_states.clear();
}
```

**Change to**:
- Remove only the chase state for the disconnected symbol's stream.
- The `symbol` field in `WsEvent::Disconnected` identifies which stream dropped. Remove only `self.chase_states.remove(&symbol.to_uppercase())`.
- If the disconnected stream served multiple symbols (e.g., the shared user data stream), clear only symbols whose last book update came from that stream. Add a `last_ws_source: HashMap<String, String>` to track which WS connection last updated each symbol.
- Do NOT transition to `SystemState::Disconnected` (already disabled by comment).

**Edge cases**:
- If the user data stream drops (which carries `OrderUpdate` for ALL symbols), all active chases are genuinely at risk. In this specific case, clear all chase states BUT emit a `ReconnectionAudit` engine event that triggers the runtime position audit (Phase 2) when the stream reconnects.
- Track which stream type disconnected in the `WsEvent::Disconnected` payload (add an enum field: `BookTicker`, `L2Depth`, `UserData`).

**Testing**:
1. Unit test: Insert chase states for BTCUSDT and ETHUSDT. Fire `Disconnected { symbol: "ETHUSDT" }`. Assert BTCUSDT chase state survives.
2. Unit test: Fire `Disconnected` for user data stream. Assert all chase states cleared AND `ReconnectionAudit` event emitted.

**Risk**: Low. This is strictly less aggressive than the current behavior.

---

#### 1B. Persist Maker/Taker Fill Counters (Rust → Telemetry)

**Files**: `execution_engine/src/order_manager.rs`

**Current**: `maker_fills` and `taker_fills` are `u64` fields (lines 178–179) that reset to 0 on every restart. The `emit_maker_fill_rate()` function broadcasts the current rate via telemetry, but nothing persists it.

**Change**:
- On every fill (maker or taker), include the cumulative counters in the existing `FILLED_CYCLE` telemetry event (emitted via `emit_cycle_order_update`).
- Add `"maker_fills": self.maker_fills, "taker_fills": self.taker_fills` to the JSON payload.
- On the Python side in `RustDataSubscriber._dispatch`, capture these counters and write them to `risk_state` table in state.db.
- On Rust startup reconciliation, emit a `FillCounterRestore` telemetry event so Python can send the persisted counters back via ZMQ (or simply accept that counters reset per-session — this is the simpler option and adequate for baseline measurement).

**Testing**: Verify fill rate appears in telemetry events and can be queried from state.db.

**Risk**: Negligible. Additive-only change to telemetry payloads.

---

#### 1C. Add Depth Quantity to TopOfBook (Rust)

**File**: `execution_engine/src/order_manager.rs`

**Current** (implied from `TopOfBook` usage at lines 1798–1802, 1872–1875):
```rust
struct TopOfBook {
    bid_price: f64,
    ask_price: f64,
}
```

**Change to**:
```rust
struct TopOfBook {
    bid_price: f64,
    ask_price: f64,
    bid_qty: f64,
    ask_qty: f64,
}
```

- Populate `bid_qty` and `ask_qty` from L2Depth events (`bids[0][1]`, `asks[0][1]`).
- For BookTicker events (which don't carry quantity), set qty fields to `f64::NAN` or 0.0 as a "not available" sentinel.
- This enables Phase 3B (depth-weighted legging timeout) without re-reading the plan.

**Testing**: Assert TopOfBook includes quantity after L2Depth event.

**Risk**: Negligible. Struct field addition with backward-compatible defaults.

---

### Phase 2: Rust-Side Runtime Position Audit (Week 3–6)

This is the core deliverable. A periodic, non-blocking REST call from the Rust engine that detects position drift and emits corrections.

#### 2A. Audit Timer in the Rust Event Loop

**File**: `execution_engine/src/order_manager.rs`

**Design**:
- Add a new `EngineEvent::PositionAuditTick` variant.
- In `OrderManager::run()`, spawn a `tokio::spawn` that sends `PositionAuditTick` every 120 seconds (configurable via env `RUST_POSITION_AUDIT_INTERVAL_S`, default 120).
- In the event handler match arm, call a new `async fn runtime_position_audit(&mut self)`.

**Why 120 seconds (not shorter)**:
- Binance REST rate limit: `GET /fapi/v2/positionRisk` costs 5 request weight. At 120s interval = 0.04 weight/second, well within the 2400/minute budget.
- Exchange position changes are reflected in WS within ~100ms under normal conditions. The audit is a **safety net**, not the primary tracking mechanism. 120s catches drift that persists for 2+ minutes.
- Too frequent (e.g., 10s) wastes rate limit budget that may be needed for order submissions.

#### 2B. The Audit Function

**File**: `execution_engine/src/order_manager.rs`

```
async fn runtime_position_audit(&mut self)
```

**Logic**:

1. Skip if `self.trading_mode == "paper"`.
2. Skip if `self.state != SystemState::Trading` (don't audit during reconciliation).
3. Call `self.binance_rest.get_fapi_position_risk().await` (new REST method — see 2C).
4. Parse the response into a `HashMap<String, ExchangePosition>` where `ExchangePosition = { quantity: f64, entry_price: f64, unrealized_pnl: f64 }`.
5. For each symbol in `self.tracked_positions`:
   - If exchange reports the position flat (qty ≈ 0) but Rust tracks it as open → **Stale local position**. Emit `PositionDivergence { symbol, divergence_type: "local_only", local_qty, exchange_qty: 0 }` telemetry event. Remove from `tracked_positions`.
   - If exchange quantity differs from tracked quantity by > 1% relative → **Quantity mismatch**. Emit `PositionDivergence { symbol, divergence_type: "qty_mismatch", local_qty, exchange_qty }`. Update `tracked_positions` to match exchange.
6. For each symbol in exchange response NOT in `self.tracked_positions`:
   - If `positionAmt != 0` and symbol is in the monitored universe → **Orphaned exchange position**. Emit `PositionDivergence { symbol, divergence_type: "exchange_only", local_qty: 0, exchange_qty }`.
   - Do NOT auto-adopt here. Instead, emit the event and let the Python brain decide (it may send `RESTORE_POSITION` back, or it may decide the position is foreign and ignore it).
7. After the audit, emit a `PositionAuditComplete { symbols_checked, divergences_found, timestamp_ms }` heartbeat event for monitoring.

**Critical design decision: who corrects?**

The Rust engine corrects its own `tracked_positions` for clear-cut cases (exchange says flat, local says open → remove local). For ambiguous cases (exchange has a position Rust doesn't know about), the Python brain decides. This preserves the Python-as-strategist, Rust-as-executor separation.

#### 2C. New REST Endpoint Wrapper

**File**: `execution_engine/src/binance_rest.rs`

Add:
```rust
pub async fn get_fapi_position_risk(&self) -> Result<String, String>
```

Calls `GET /fapi/v2/positionRisk` with signature. This endpoint returns all positions (including zero-quantity ones). Filter for `positionAmt != "0"` at the call site.

Also add for Phase 2B spot verification:
```rust
pub async fn get_spot_account(&self) -> Result<String, String>
```

Calls `GET /api/v3/account` with signature.

#### 2E. Spot Balance Verification & Reconciliation
- In `runtime_position_audit(&mut self)`, after querying position risk, query spot account balances using `self.binance_rest.get_spot_account().await`.
- Parse the JSON response, extract free and locked balances for assets matching the monitored symbol universe (e.g. USDT, USDC, FDUSD, etc.), and overwrite the corresponding entries in `self.spot_balances`.
- This ensures any missed WebSocket account updates do not result in a permanently stale `self.spot_balances` state, directly avoiding false `INSUFFICIENT_SPOT_BALANCE` rejections and subsequent entry cooldowns.

#### 2D. New Telemetry Event Type

**File**: `execution_engine/src/order_manager.rs` (emission), `bongus/market_data/rust_data_subscriber.py` (reception)

New event emitted via `self.dash_tx`:

```json
{
  "event": "PositionDivergence",
  "symbol": "ETHUSDT",
  "divergence_type": "local_only | exchange_only | qty_mismatch",
  "local_spot_qty": 1.5,
  "local_perp_qty": 1.5,
  "exchange_perp_qty": 0.0,
  "exchange_entry_price": 0.0,
  "audit_timestamp_ms": 1750142400000
}
```

Python subscriber registration:
```python
# In RustDataSubscriber._dispatch or via .on("PositionDivergence", handler)
```

**Testing**:
1. Integration test: Manually set a tracked position in Rust, mock the REST response to return flat. Assert `PositionDivergence` event emitted with `divergence_type: "local_only"`.
2. Integration test: Mock REST to return a position Rust doesn't track. Assert `divergence_type: "exchange_only"`.
3. Integration test: Mock REST with matching positions. Assert no divergence event.

---

### Phase 3: WS Reconnection Without Full State Reset (Week 6–8)

#### 3A. Targeted Reconciliation on Reconnection

**File**: `execution_engine/src/order_manager.rs`

**Current**: On WS `Connected` event (line 1769):
```rust
WsEvent::Connected { symbol } => {
    if self.state == SystemState::Disconnected {
        self.execute_reconciliation_sequence().await;
    }
}
```

This runs the full startup reconciliation only if the system was in `Disconnected` state. Since Phase 1A no longer transitions to `Disconnected` for individual streams, this never fires on partial reconnections. The system silently proceeds with potentially stale data.

**Change**:
- On `Connected`, if the system is already in `Trading` state, trigger a **targeted** audit for only the reconnected symbol:
  1. REST query the symbol's open orders via `GET /fapi/v1/openOrders?symbol=ETHUSDT`.
  2. If there are bongus-managed orders (`bngs_` prefix) that are not in `internal_orders`, adopt or cancel them.
  3. If there's an active chase state for this symbol that was interrupted by the disconnect, verify the order status via REST.
  4. Trigger `runtime_position_audit` for just this symbol (not the full audit).
- This is a lightweight version of `execute_reconciliation_sequence` scoped to one symbol.

**Edge case**: If the global user data WS reconnects, run the full `runtime_position_audit()` since fill events for ALL symbols may have been missed.

#### 3B. Depth-Weighted Legging Timeout (Optional Enhancement)

**File**: `execution_engine/src/order_manager.rs`

**Depends on**: Phase 1C (TopOfBook with quantity)

**Current** (line 476):
```rust
fn adaptive_legging_timeout_ms(&self, symbol: &str) -> u64 {
    let vol_bps = self.recent_volatility_bps(symbol);
    let raw = 300.0 - vol_bps * 20.0;
    raw.clamp(50.0, 500.0) as u64
}
```

**Change to**:
```rust
fn adaptive_legging_timeout_ms(&self, symbol: &str) -> u64 {
    let vol_bps = self.recent_volatility_bps(symbol);
    let raw = 300.0 - vol_bps * 20.0;
    let base = raw.clamp(50.0, 500.0);

    // Depth factor: thin books → shorter timeout (taker fallback sooner)
    let depth_factor = self.compute_depth_factor(symbol);
    (base * depth_factor).clamp(50.0, 500.0) as u64
}

fn compute_depth_factor(&self, symbol: &str) -> f64 {
    let sym = symbol.to_uppercase();
    // Use the resting leg's book depth
    let perp_depth = self.perp_top_cache.get(&sym)
        .map(|t| t.ask_qty.max(t.bid_qty))
        .unwrap_or(0.0);

    if perp_depth <= 0.0 || perp_depth.is_nan() {
        return 1.0; // No data → no adjustment
    }

    // Compare available depth to our order size
    let our_qty = self.chase_states.get(&sym)
        .map(|c| c.perp_quantity)
        .unwrap_or(0.0);

    if our_qty <= 0.0 {
        return 1.0;
    }

    (perp_depth / our_qty).clamp(0.5, 2.0)
}
```

**Rationale**: When depth >> our order size, the market is thick and passive fills are more likely → allow more time. When depth ≈ our order size, the level could be swept quickly → shorter timeout.

**Testing**: Unit test with varying depth/order ratios. Assert timeout scales appropriately.

**Risk**: Low. Clamp bounds prevent extreme values. Existing legging defense remains as fallback.

---

### Phase 4: Python-Side Closed-Loop Reconciliation (Week 8+)

#### 4A. Handle `PositionDivergence` Events

**File**: `scripts/live_trader_v2.py`

Register a handler for the new `PositionDivergence` telemetry event in the `RustDataSubscriber`:

```python
self._rust_subscriber.on("PositionDivergence", self._handle_position_divergence)
```

Handler logic by divergence type:

| `divergence_type` | Python Action |
|---|---|
| `local_only` | The position was already removed from Rust tracking (Phase 2B step 5). Python should verify its own state store. If the position exists in state.db but exchange confirms flat, call `_clear_local_position_tracking(symbol)`. Log + alert via Telegram. |
| `exchange_only` | Rust found an exchange position it doesn't track. Python checks if it exists in state.db. If yes → send `RESTORE_POSITION` via `ExecutionClient.restore_position_tracking()` to re-sync Rust. If no → alert operator, do NOT auto-adopt (could be manually placed). |
| `qty_mismatch` | Rust already corrected its tracking. Python should update state.db with the corrected quantities. If the mismatch was large (>5%), emit a Telegram alert for operator awareness. |

#### 4B. Per-Symbol Safe Mode Granularity

**File**: `scripts/live_trader_v2.py`

**Current**: `_set_safe_mode_flag("hedge_gap", bool(hedge_gap_symbols))` sets a single boolean that blocks **all** symbols.

**Change**:
- The `_PER_SYMBOL_SAFE_MODE_FLAGS` mechanism already exists (referenced in the docstring at line 1531–1533). Ensure the following flags use it correctly:
  - `hedge_gap` → already per-symbol? **Verify and fix if not.** Should block only symbols with hedge shortfall.
  - `exit_failure` → should block only the symbol whose exit failed, not the entire portfolio.
  - `naked_leg_unwind_stuck` → should block only the stuck symbol.
  - `startup_exit_candidate` → should block only the candidate symbol.
  - `startup_manual_review` → should block only the symbol requiring manual review.

- For each of these, track a `Set[str]` of blocked symbols instead of a single boolean:
  ```python
  self._symbol_safe_mode_blocks: dict[str, set[str]] = {
      "hedge_gap": set(),
      "exit_failure": set(),
      "naked_leg_unwind_stuck": set(),
      "startup_exit_candidate": set(),
      "startup_manual_review": set(),
  }
  ```

- Update `_refresh_startup_recovery_flags()` in `scripts/live_trader_v2.py` to populate these sets with active symbols rather than setting a global boolean safe mode flag.
- In `_should_skip_symbol(symbol)` (or equivalent gating logic), check if the symbol is in any per-symbol block set, rather than checking the global safe mode flag.

**Edge case**: If ALL symbols are individually blocked, the effective behavior is the same as global safe mode. This is correct.

#### 4C. Transient Safe Mode Auto-Clear

**File**: `scripts/live_trader_v2.py`

For inherently transient failures, add auto-retry with backoff:

| Flag | Current Behavior | Proposed Behavior |
|---|---|---|
| `execution_bridge` | Stays set until ZMQ reconnects manually | Retry ZMQ send after 5s, 15s, 30s. Auto-clear after successful send. |
| `audit_unavailable` | Stays set until 3 consecutive successes | Auto-clear after 1 successful audit (the 3-failure threshold to enter is sufficient protection). |
| `heartbeat_bridge` | Stays set until heartbeat received | Add a 30s grace period before setting. Brief WS reconnections shouldn't trigger it. (Note: latency debounce from a previous plan may already address this — verify before implementing.) |

**Testing**: Mock a ZMQ failure → verify safe mode sets → mock recovery → verify safe mode clears within expected backoff window.

#### 4D. Preflight and Startup REST Resilience

**File**: `scripts/live_trader_v2.py`

- **Endpoint Fallback on Transient Errors**: Update `_supports_signed_get_fallback(exc)` to return `True` for server-side or connection/timeout exceptions (such as HTTP 408, 500, 502, 503, 504, or read/connect timeouts) as well as the standard HTTP 400/404 errors. This allows the endpoint fallback (e.g. from `/fapi/v3/account` to `/fapi/v2/account`) to fire on timeouts/5xx, rather than immediately crashing preflight.
- **Retry-with-Backoff for Preflight Requests**: Wrap preflight REST queries (specifically `_ping_exchange()`, `_sync_binance_time()`, the signed account queries, and the startup exchange snapshot fetches) in a retry loop (e.g. 3 attempts with exponential backoff: 1s, 2s, 4s). If an error persists after retries and fallbacks, only then raise the fatal `StartupBlockedError`.
- **Operator Notifications**: Ensure any retried preflight attempts are logged, and alert via Telegram if preflight was delayed by transient errors but successfully recovered.

---

## 4. File Touch Map

### Rust (`execution_engine/src/`)

| File | Phase | Changes |
|---|---|---|
| `order_manager.rs` | 1A | Per-symbol chase state cleanup on WS disconnect |
| `order_manager.rs` | 1B | Add fill counters to FILLED_CYCLE telemetry |
| `order_manager.rs` | 1C | Extend `TopOfBook` struct with `bid_qty`, `ask_qty` |
| `order_manager.rs` | 2A | Add `PositionAuditTick` engine event, timer spawn |
| `order_manager.rs` | 2B | New `runtime_position_audit()` function |
| `order_manager.rs` | 2D | Emit `PositionDivergence` telemetry event |
| `order_manager.rs` | 3A | Targeted reconciliation on WS reconnect |
| `order_manager.rs` | 3B | Depth-weighted legging timeout (optional) |
| `binance_rest.rs` | 2C | Add `get_fapi_position_risk()` and `get_spot_account()` |
| `ipc.rs` | — | No changes required (existing `AlphaInstruction` supports `RESTORE_POSITION`) |

### Python

| File | Phase | Changes |
|---|---|---|
| `bongus/market_data/rust_data_subscriber.py` | 2D | Handle `PositionDivergence` event dispatch |
| `scripts/live_trader_v2.py` | 4A | `_handle_position_divergence()` handler |
| `scripts/live_trader_v2.py` | 4B | Per-symbol safe mode block sets |
| `scripts/live_trader_v2.py` | 4C | Transient safe mode auto-clear |
| `bongus/ipc/execution.py` | — | No changes required (existing `restore_position_tracking` is sufficient) |

### Tests

| File | Phase | Tests |
|---|---|---|
| `execution_engine/src/order_manager.rs` (mod tests) | 1A | WS disconnect isolation |
| `execution_engine/src/order_manager.rs` (mod tests) | 2B | Position audit divergence detection |
| `tests/test_execution_client.py` | — | Existing tests unchanged |
| New: `tests/test_reconciliation_loop.py` | 4A | End-to-end divergence → correction flow |

## 5. Validation Checklist

### Phase 1 (Quick Wins)

- [ ] **WS disconnect isolation**: Disconnect ETHUSDT WS stream while BTCUSDT chase is active. Assert BTCUSDT chase completes normally.
- [ ] **Fill counter telemetry**: Execute 5 trades. Verify `maker_fills` and `taker_fills` appear in FILLED_CYCLE telemetry events.
- [ ] **TopOfBook depth**: Process an L2Depth event. Verify `bid_qty` and `ask_qty` are populated in `spot_top_cache`/`perp_top_cache`.

### Phase 2 (Runtime Audit)

- [ ] **Stale position detection**: Manually close a position on Binance web UI. Verify Rust detects the divergence within 120s and emits `PositionDivergence { divergence_type: "local_only" }`.
- [ ] **Orphan detection**: Manually open a position on Binance web UI. Verify Rust detects it within 120s and emits `PositionDivergence { divergence_type: "exchange_only" }`.
- [ ] **No false positives**: Run the system for 24 hours with no manual intervention. Verify zero `PositionDivergence` events (unless genuine drift occurs).
- [ ] **Rate limit safety**: Verify that the audit REST calls do not cause HTTP 429 or `RATE_LIMIT_EXCEEDED` errors over a 24h period.

### Phase 3 (WS Reconnection)

- [ ] **Partial reconnection**: Add a firewall rule to drop the ETHUSDT L2 stream for 10 seconds, then release. Verify BTCUSDT trading continues uninterrupted and ETHUSDT data resumes without a full restart.
- [ ] **User data stream reconnection**: Drop the user data WS connection. Verify that on reconnect, a full `runtime_position_audit` fires and no fills are lost.

### Phase 4 (Python Closed-Loop)

- [ ] **Divergence → correction flow**: In paper mode, inject a fake `PositionDivergence` event. Verify Python sends `RESTORE_POSITION` (for `exchange_only`) or clears tracking (for `local_only`).
- [ ] **Per-symbol safe mode**: Force an `exit_failure` on ETHUSDT. Verify BTCUSDT entries still proceed. Verify ETHUSDT entries are blocked.
- [ ] **Transient auto-clear**: Force `execution_bridge` safe mode by killing the ZMQ socket. Restart it within 30s. Verify safe mode auto-clears without operator intervention.

## 6. Rollout Order & Risk Management

```
Week 1:   Phase 1A (WS disconnect isolation) — deploy in paper mode for 48h validation
Week 2:   Phase 1B + 1C (fill counters + TopOfBook depth) — deploy alongside paper mode
Week 2:   Promote Phase 1A–1C to live after paper validation
Week 3–4: Phase 2A–2C (Rust audit timer + REST wrappers) — paper mode validation
Week 5:   Phase 2D (PositionDivergence telemetry) — paper mode, verify event emission
Week 6:   Promote Phase 2 to live. Monitor for false positives for 1 week.
Week 7:   Phase 3A (targeted WS reconnection) — paper mode with simulated drops
Week 8:   Promote Phase 3A to live. Phase 3B (depth timeout) only if fill rate data justifies.
Week 9+:  Phase 4A–4C (Python closed-loop) — requires Phases 2–3 stable in production.
```

### Rollback Plan

Each phase is independently deployable and reversible:

- **Phase 1A**: Revert to `self.chase_states.clear()`. Zero risk to other components.
- **Phase 2**: Disable the audit timer by commenting out the `tokio::spawn` that sends `PositionAuditTick`. The engine operates exactly as it does today.
- **Phase 3**: Revert WS reconnect handler to current behavior. Loses per-symbol isolation but returns to known-good state.
- **Phase 4**: Python changes are gated behind the `PositionDivergence` event handler. If no events arrive (Phase 2 reverted), the code is dead but harmless.

### Kill Switch

Add an env var `RUNTIME_AUDIT_ENABLED=true|false` (default `true`) that gates Phase 2. If the audit causes unexpected issues in production, set to `false` to disable without a code change.

## 7. Dependencies and Prerequisites

| Prerequisite | Phase | Status |
|---|---|---|
| Binance REST rate limit headroom | 2 | ✅ Verified: 0.08 weight/sec (positions + spot balance) << 2400/min budget |
| `WsEvent::Disconnected` carries stream type | 1A | ❌ Requires adding stream type to enum |
| `get_fapi_position_risk` endpoint signed correctly | 2C | ❌ New code required; follow existing `get_fapi_account` pattern |
| `get_spot_account` endpoint signed correctly | 2C | ❌ New code required; follow existing `get_account` pattern |
| Paper mode doesn't call REST | 2B | ✅ Explicitly gated in plan |
| `_PER_SYMBOL_SAFE_MODE_FLAGS` mechanism in Python | 4B | ⚠️ Exists in docstring but verify actual implementation |

## 8. Metrics to Track Post-Deployment

| Metric | Source | Target |
|---|---|---|
| Divergence events per day | `PositionDivergence` telemetry | < 2 (zero under normal operation) |
| Time to detect injected divergence | Manual test | < 150 seconds |
| Safe mode total duration per day | `risk_state` table | Reduce by > 50% vs. pre-deployment baseline |
| Symbols blocked by safe mode | `risk_state` table | Per-symbol blocks only, not portfolio-wide |
| Position audit REST failures per day | Rust logs | < 5 (transient network only) |
| Maker fill rate | Persisted counters | Baseline measurement (no target yet) |
| WS reconnection events per day | Rust logs | Informational — correlate with audit triggers |
