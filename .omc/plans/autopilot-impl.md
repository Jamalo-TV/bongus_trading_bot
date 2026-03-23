# Autopilot Implementation Plan — Bongus Masterplan Phase 1 & 2

## Wave 1 — Trivial Config Changes (independent, parallel)
- [ ] T1: Fix BNB fee discount in config.py (items 1 + 6)
- [ ] T2: Tighten snipe window in config.py

## Wave 2 — Dynamic Symbols (depends on Wave 1)
- [ ] T3: FundingRanker dynamic mode (remove symbol filter, accept all Binance perps)
- [ ] T4: DepthTracker top-15 WS subscription rotation

## Wave 3 — PortfolioAllocator upgrades (independent of Wave 2)
- [ ] T5: Dynamic leverage scaling (get_leverage_for_rate helper)
- [ ] T6: Auto-compounding (StateReader.get_account_equity + PortfolioAllocator capital arg)

## Wave 4 — New modules (independent)
- [ ] T7: FundingPredictor module (decay prediction)
- [ ] T8: BybitFundingMonitor module (read-only)

## Wave 5 — Wire new modules into live_trader_v2.py
- [ ] T9: Integrate FundingPredictor + BybitFundingMonitor into live_trader_v2.py
- [ ] T10: Dynamic symbol subscriptions in live_trader_v2.py

## Wave 6 — Inverse Funding (Python side, largest)
- [ ] T11: state_store.py direction column
- [ ] T12: strategy.py inverse signal
- [ ] T13: live_trader_v2.py ENTER_SHORT/EXIT_SHORT dispatch

## Wave 7 — Inverse Funding (Rust side)
- [ ] T14: Rust ipc.rs parse ENTER_SHORT/EXIT_SHORT
- [ ] T15: Rust order_manager.rs short execution path

## Wave 8 — Tests
- [ ] T16: Update/add tests for all changed modules
- [ ] T17: Run full test suite
