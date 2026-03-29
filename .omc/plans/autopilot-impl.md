# Autopilot Implementation Plan — Bongus Masterplan Phase 1 & 2

## Wave 1 — Trivial Config Changes (independent, parallel)
- [x] T1: Fix BNB fee discount in config.py (items 1 + 6) — DONE (TAKER_FEE_SPOT=0.0005625)
- [x] T2: Tighten snipe window in config.py — DONE (SNIPE_ENTRY_WINDOW_MIN=15)

## Wave 2 — Dynamic Symbols (depends on Wave 1)
- [x] T3: FundingRanker dynamic mode (remove symbol filter, accept all Binance perps) — DONE (DYNAMIC_SYMBOL_MODE flag, FundingRanker supports None symbols)
- [x] T4: DepthTracker top-15 WS subscription rotation — DONE (MAX_DEPTH_SUBSCRIPTIONS=15)

## Wave 3 — PortfolioAllocator upgrades (independent of Wave 2)
- [x] T5: Dynamic leverage scaling (get_leverage_for_rate helper) — DONE (LEVERAGE_TIERS + get_leverage_for_rate)
- [x] T6: Auto-compounding (StateReader.get_account_equity + PortfolioAllocator capital arg) — DONE (AutoCompounder + _maybe_recompound)

## Wave 4 — New modules (independent)
- [x] T7: FundingPredictor module (decay prediction) — DONE (funding_predictor.py with TWAP projection + confidence)
- [x] T8: BybitFundingMonitor module (read-only) — DONE (bybit_monitor.py)

## Wave 5 — Wire new modules into live_trader_v2.py
- [x] T9: Integrate FundingPredictor + BybitFundingMonitor into live_trader_v2.py — DONE
- [x] T10: Dynamic symbol subscriptions in live_trader_v2.py — DONE (DYNAMIC_SYMBOL_MODE flag wired)

## Wave 6 — Inverse Funding (Python side, largest)
- [x] T11: state_store.py direction column — DONE
- [x] T12: strategy.py inverse signal — DONE (INVERSE_FUNDING_ENABLED)
- [x] T13: live_trader_v2.py ENTER_SHORT/EXIT_SHORT dispatch — DONE

## Wave 7 — Inverse Funding (Rust side)
- [x] T14: Rust ipc.rs parse ENTER_SHORT/EXIT_SHORT — DONE (AlphaInstruction.intent string)
- [x] T15: Rust order_manager.rs short execution path — DONE (is_buy = ENTER_LONG || EXIT_SHORT)

## Wave 8 — Tests
- [x] T16: Update/add tests for all changed modules — DONE (116/116 passing)
- [x] T17: Run full test suite — DONE (116 passed in 2.08s)

## Phase 2 Profitability Optimisations (2026-03-29)
- [x] OPT1: Fix rotation payback formula — removed sunk-cost future exit_new from friction numerator
- [x] OPT2: Tune ROTATION_MIN_GAP_ANN 0.05→0.03, ROTATION_MAX_PAYBACK_DAYS 0.5→2.0
- [x] OPT3: Wire FundingPredictor into entry gate (_predictor_allows_entry)
- [x] OPT4: Wire sentiment score into dynamic threshold scaling (_effective_entry_threshold)
- [x] OPT5: Graduated CorrelationBreaker — added WARNED (33%) and PARTIAL_EXIT (75%) states
