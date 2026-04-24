# Fix Plan - Stale Intent Timeout Flapping, Cooldowns, and Per-Symbol Block Granularity (2026-04-23)

## 0. Scope

This plan addresses the observed "slow flapping" of the trading bot between `SAFE_MODE` and `LIVE_WITH_SYMBOL_BLOCKS`. 

Observed sequence in Telegram:
1. `Mode: SAFE_MODE` (Reason: `stale_pending_intent`, `startup_manual_review (manual_review=DENTUSDT)`)
2. `Mode: LIVE_WITH_SYMBOL_BLOCKS` (Reason: `startup_manual_review (manual_review=DENTUSDT)`)
3. Repeat every ~5-10 minutes.

Constraint: this document defines the implementation plan only. No code changes are included here.

## 1. Goals

1. **Eliminate Flapping:** Prevent symbols that consistently fail to fill from causing repeated 300s timeout cycles.
2. **Improve Portfolio Continuity:** Allow healthy symbols to continue trading even when one symbol is stuck in a stale pending state.
3. **Enhance Diagnostics:** Provide clearer information in alerts about which symbols are causing the stale intent state.

## 2. Observed Root Causes

### 2.1 Missing Cooldown for Stale Intents
When a pending entry times out (after 300s), it is moved to `_stale_pending_enters`. This triggers the `stale_pending_intent` global safe mode flag. The maintenance loop later auto-clears these stale intents if the exchange shows no order or position. However, **no cooldown is applied** to the symbol after auto-clearing. Consequently, the portfolio allocator re-picks the same symbol in its next cycle, leading to another 300s timeout and a recurring trading halt.

### 2.2 Global Halt for Single-Symbol Issues
The `stale_pending_intent` flag is currently treated as a global safe mode cause. While a symbol is stale, it is technically "blocked" until reconciliation, but the entire bot stops all other trading activities. Given that stale intents often result from localized issues (toxicity, OBI, or exchange-specific constraints on a pair), a portfolio-wide halt is overly aggressive.

### 2.3 Silent Execution Stalls
Logs indicate the Rust execution engine may be pausing maker operations for toxic symbols (e.g., `DENTUSDT`). If an instruction is sent during this period, it may sit `Idle` in the Rust `chase_states` without placing orders, eventually triggering the Python-side timeout without an explicit `REJECTED` event.

## 3. Implementation Plan

### Phase A - Stale Intent Cooldowns

1. **Implement Cooldown Activation on Timeout:**
   - In `scripts/live_trader_v2.py`, update `_expire_stale_pending_intents` to activate a symbol cooldown when an entry or exit moves to the stale state.
   - Use exponential backoff for repeated timeouts on the same symbol to prevent long-term churn.

2. **Implement Cooldown Activation on Auto-Clear:**
   - In `_live_self_heal_stale_pending_intents`, ensure that symbols auto-cleared due to "no activity" are placed on cooldown.
   - This "stops the flap at the source" by ensuring the allocator ignores the failing symbol for a meaningful period (e.g., 15-60 minutes).

### Phase B - Move to Per-Symbol Block Granularity

1. **Update `_PER_SYMBOL_SAFE_MODE_FLAGS`:**
   - Add `stale_pending_intent` to the set of per-symbol safe mode flags in `scripts/live_trader_v2.py`.
   - Update `_active_global_safe_mode_flags` and `_active_symbol_block_flags` logic to ensure this transition correctly moves the bot to `LIVE_WITH_SYMBOL_BLOCKS` rather than `SAFE_MODE`.

2. **Ensure Symbol Blocking Logic covers Stale Intents:**
   - Verify that `_blocked_entry_symbols` correctly includes symbols from `_stale_pending_enters` and `_stale_pending_exits` (it currently does).

### Phase C - Enhanced Telegram Alerting

1. **Augment Alerter Display:**
   - In `bongus/monitoring/telegram_alerter.py`, update `_format_safe_mode_reason` to detect `stale_pending_intent`.
   - Read `stale_pending_enter_symbols` and `stale_pending_exit_symbols` from the risk snapshot and append them to the reason string (e.g., `stale_pending_intent (stale=ATAUSDT, ETHUSDT)`).

2. **Reduce Alert Fatigue:**
   - Ensure that the transition from `LIVE` to `LIVE_WITH_SYMBOL_BLOCKS` due to a stale intent is still alerted, but does not repeatedly spam if the set of blocked symbols remains the same.

## 4. File Touch Map (Planned)

- `bongus/core/config.py`: Define `STALE_INTENT_COOLDOWN_BASE_SECONDS` and associated backoff constants.
- `scripts/live_trader_v2.py`: 
    - Move `stale_pending_intent` to `_PER_SYMBOL_SAFE_MODE_FLAGS`.
    - Apply cooldowns in `_expire_stale_pending_intents` and `_live_self_heal_stale_pending_intents`.
- `bongus/monitoring/telegram_alerter.py`: Update `_format_safe_mode_reason` to show stale symbols.
- `tests/test_safe_mode_debounce.py`: Add test cases for stale intent symbol blocking and cooldowns.

## 5. Validation Checklist

1. **Reproduction:** Manually inject a stale pending intent (e.g., by mocking a delayed response) and verify:
    - Bot enters `LIVE_WITH_SYMBOL_BLOCKS` instead of `SAFE_MODE`.
    - Telegram alert correctly identifies the stale symbol.
2. **Cooldown Verification:** Verify that after auto-clearing the stale intent, the symbol is on cooldown and NOT immediately re-picked by the allocator.
3. **Backoff Verification:** Verify that repeated timeouts on the same symbol lead to increasing cooldown durations.
4. **Portfolio Continuity:** Verify that while one symbol is stale/blocked, the bot can still successfully enter and exit other healthy symbols.

## 6. Rollout Order

1. Phase B (Flag movement) first to immediately stop portfolio-wide halts.
2. Phase A (Cooldowns) second to stop the flapping cycle.
3. Phase C (Alerting) third to improve operator visibility.
