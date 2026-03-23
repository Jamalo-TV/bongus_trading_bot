# Bongus Bot Upgrade Spec — Masterplan Phase 1 & 2

## Scope
Implement masterplan items 1–8 in priority order. Items 9–10 (cross-exchange, Kelly) are out of scope.

## Item 1 — BNB Fee Discount Correction
**File:** `bongus/core/config.py`
- `MAKER_FEE_SPOT`: `0.00075` → `0.0005625` (0.05625%, actual BNB-discounted rate)
- `TAKER_FEE_SPOT`: `0.00075` → `0.000675` (0.0675%, BNB-discounted)
- Recalculate `TAKER_FEE` and `MAKER_FEE` blended averages accordingly
- Python-only. No Rust changes.

## Item 2 — Dynamic Top-30 Symbol Universe
**Files:** `bongus/core/config.py`, `bongus/market_data/funding_ranker.py`, `scripts/live_trader_v2.py`
- Add `DYNAMIC_SYMBOL_MODE = True` and `MAX_MONITORED_SYMBOLS = 30` to config.py
- `FundingRanker`: when dynamic mode, accept all symbols from Binance premiumIndex instead of filtering. Populate `self._symbols` dynamically after first refresh.
- `DepthTracker` WS subscriptions: cap at top 15 by current funding rate, rotate every 60s.
- `live_trader_v2.py`: update DepthTracker subscription set each funding refresh cycle.
- Python-only.

## Item 3 — Inverse Funding Mode
**Python:** `config.py`, `strategies/strategy.py`, `ipc/execution.py`, `scripts/live_trader_v2.py`, `engine/state_store.py`
**Rust:** `execution_engine/src/order_manager.rs`, `execution_engine/src/ipc.rs`

### Python:
- `config.py`: Add `INVERSE_FUNDING_ENABLED = True`
- `strategy.py`: emit `direction="short"` when `ann_funding < -ENTRY_ANN_FUNDING_THRESHOLD`
- `live_trader_v2.py`: send `intent="ENTER_SHORT"` / `intent="EXIT_SHORT"`; track direction per position
- `state_store.py`: add `direction` column to positions table (default `"long"`)

### Rust:
- `ipc.rs`: parse `ENTER_SHORT` / `EXIT_SHORT` intents
- `order_manager.rs`: short path — SELL margin spot + BUY perp for entry; BUY spot + SELL perp for exit

## Item 4 — Dynamic Leverage Scaling
**File:** `bongus/portfolio/portfolio_allocator.py`
- Add `get_leverage_for_rate(ann_funding: float) -> float`:
  - `< 25%` → 2x, `25–50%` → 3x, `50–100%` → 4x, `> 100%` → 5x
- `target_notional = CAPITAL_PER_SLOT_USD * get_leverage_for_rate(rate)`
- Respect `MAX_LEVERAGE` and `MAX_NOTIONAL_PER_TRADE` hard caps
- Python-only.

## Item 5 — Auto-Compounding
**Files:** `bongus/portfolio/portfolio_allocator.py`, `bongus/engine/state_store.py`, `scripts/live_trader_v2.py`
- `StateReader`: add `get_account_equity() -> float` reading from risk_snapshot table
- `live_trader_v2.py`: every 24h compute `capital_per_slot = equity / MAX_CONCURRENT_POSITIONS`, pass to `PortfolioAllocator`
- `PortfolioAllocator.__init__`: accept optional `capital_per_slot_usd` arg (default `CAPITAL_PER_SLOT_USD`)
- Python-only.

## Item 6 — Snapshot Snipe Window Tightening
**File:** `bongus/core/config.py`
- `SNIPE_ENTRY_WINDOW_MIN`: `60` → `15`
- `SNIPE_ENTRY_WINDOW_MAX`: `120` → `30`
- Python-only.

## Item 7 — Funding Rate Decay Prediction
**New file:** `bongus/market_data/funding_predictor.py`
- `FundingPredictor`: rolling deque of 12 rate samples per symbol
- `predict_rate_at_snapshot(symbol, minutes_to_snapshot) -> float` using linear extrapolation
- `live_trader_v2.py`: feed samples from WS MarkPrice events; use predicted rate for entry decisions
- Python-only.

## Item 8 — Bybit Read-Only Monitoring
**New file:** `bongus/market_data/bybit_monitor.py`
- `BybitFundingMonitor`: REST `https://api.bybit.com/v5/market/tickers?category=linear` every 60s
- `get_rate(symbol) -> float | None`
- `live_trader_v2.py`: log warning if Binance rate > 30% but Bybit rate < 5%
- Python-only.

## Implementation Order
1. Item 1 (fee fix) + Item 6 (snipe window) — trivial config changes
2. Item 2 (dynamic symbols) — FundingRanker + DepthTracker
3. Item 4 (dynamic leverage) + Item 5 (auto-compounding) — PortfolioAllocator
4. Item 7 (decay prediction) — new module
5. Item 8 (Bybit monitor) — new module
6. Item 3 (inverse funding) — largest, Python + Rust, last

## Out of Scope
- POST_ONLY GTX orders in Rust
- Kelly criterion sizing
- Cross-exchange execution
- VIP tier progression
