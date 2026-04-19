# Fix Plan — Hedge Gap Recovery, Emergency Flatten, and Restart Autonomy (2026-04-19)

> **Intended reader:** the local AI that will edit code and push the fix.
> **Hard constraint from the user:** do not change runtime state by hand. Do **not** edit `state.db`, `live_config.json`, or anything on the running server as part of the PR.
> **User goal:** when the bot restarts, it must pick up open exchange positions automatically, evaluate them, and keep/close/reduce them without throwing an operator-only startup error. Emergency Flatten must actually flatten.

---

## 0. Executive Summary

There are **two separate bugs** and they compound each other:

1. **Restart autonomy is broken by classification, not by sync.**
   The bot **does** recover exchange positions and sync them back into Rust correctly, but it classifies hedge-gap longs as `manual_review`, which removes them from normal risk/funding management. After restart, the positions are visible but effectively sidelined.

2. **Emergency Flatten fails on large naked-perp exits.**
   For hedge-gap longs, Python correctly chooses a perp-only exit, but Rust submits the **entire futures quantity as one market order**. Binance rejects some of those exits with `-4005 Quantity greater than max quantity`. The code then retries the same impossible exit forever.

Current live evidence on **2026-04-19**:

- Startup at `2026-04-19 20:33:07 UTC` recovered **7** exchange positions and restored all 7 into Rust.
- The same startup marked all 7 as spot hedge gaps.
- Emergency Flatten later closed most of them, but **DYMUSDT** and **STRKUSDT** remained stuck.
- Current `state.db` snapshot shows:
  - `runtime_mode = LIVE_WITH_SYMBOL_BLOCKS`
  - `safe_mode_reason = naked_leg_unwind_stuck, startup_manual_review`
  - `open_position_count = 2`
  - `managed_open_position_count = 0`
  - `manual_review_position_count = 2`
  - `operator_flatten_all_status = in_progress`
  - `operator_flatten_all_remaining_symbols = ["DYMUSDT", "STRKUSDT"]`

That is the exact failure mode the user described: after restart the bot "has the positions" but does not truly manage them, and Emergency Flatten never finishes.

---

## 1. Evidence

### 1.1 Restart recovery already restores positions into Rust

From `scripts/logs/live_trader.log` on `2026-04-19 20:33:07 UTC`:

- `Live startup reconciliation complete: 7 exchange positions, 0 stale local rows removed, 2 mismatches, 7 review items`
- `Startup recovery: spot hedge gap for ACTUSDT, BANKUSDT, DEGOUSDT, DYMUSDT, FIOUSDT, STRKUSDT, TRUUSDT`
- `Synced recovered position ... to execution engine` for all 7 symbols
- `Startup recovery synced 7 open position(s) back into the Rust execution engine`

So the bug is **not** "positions are missing after restart". The sync path works.

### 1.2 Current stuck state

Current `state.db` inspection shows two remaining open positions:

- `DYMUSDT`
  - `qty = 274424.3`
  - `hedge_ratio = 0.0`
  - `ann_funding = 0.1095`
  - `recovery_state = manual_review`
- `STRKUSDT`
  - `qty = 71925.5`
  - `hedge_ratio = 0.0`
  - `ann_funding = 0.1095`
  - `recovery_state = manual_review`

Relevant `risk_state` keys:

- `runtime_mode = LIVE_WITH_SYMBOL_BLOCKS`
- `safe_mode_reason = naked_leg_unwind_stuck, startup_manual_review`
- `managed_open_position_count = 0`
- `manual_review_position_count = 2`
- `operator_flatten_all_status = in_progress`
- `operator_flatten_all_remaining_symbols = ["DYMUSDT", "STRKUSDT"]`
- `startup_reconciliation_manual_review = ["DYMUSDT", "STRKUSDT"]`
- `startup_reconciliation_recovery_actions = {"DYMUSDT": ..., "STRKUSDT": ...}`

### 1.3 Emergency Flatten failure signature

From `scripts/logs/live_trader.log` starting at `2026-04-19 20:46:15 UTC` and later `20:57-20:59 UTC`:

- `Operator flatten-all request ... dispatched exits for ACTUSDT, BANKUSDT, DEGOUSDT, DYMUSDT, FIOUSDT, STRKUSDT, TRUUSDT`
- repeated Rust errors:
  - `Failed single-leg unwind: Some("futures market order returned HTTP 400 (exchange error -4005 (Quantity greater than max quantity.))")`
- repeated Python warnings:
  - `Startup recovery exit for DYMUSDT failed with status REJECTED (SINGLE_LEG_SUBMISSION_FAILED)`
  - `Startup recovery exit for STRKUSDT failed with status REJECTED (SINGLE_LEG_SUBMISSION_FAILED)`
- repeated re-dispatches of the same flatten request every ~1 second

This is not a one-off exchange glitch. It is a deterministic retry loop.

---

## 2. Root Causes

### Root Cause A — Hedge-gap longs are incorrectly turned into `manual_review`

In `scripts/live_trader_v2.py:2118-2151`, `_classify_startup_recovered_position(...)` returns:

- `manual_review` for unsupported inverse structure
- **also `manual_review` for any long with `hedge_ratio < 1 - tolerance`**

That second branch is the design bug.

Why it breaks autonomy:

- In startup reconciliation (`scripts/live_trader_v2.py:3116-3224`), every recovered hedge-gap long is persisted with `recovery_state="manual_review"`.
- In the trading loop (`scripts/live_trader_v2.py:6592-6668`), `managed_positions` explicitly excludes `manual_review`.
- In risk evaluation (`scripts/live_trader_v2.py:4841-4845` and `5021-5050`), `manual_review` positions are excluded from managed-risk exposure and derisking inputs.
- `_dispatch_startup_recovery_exits(...)` only auto-exits `manual_review` positions when `startup_recovery_auto_exit_manual_review` is enabled (`scripts/live_trader_v2.py:2249-2312`), and the default is `False`.

Net effect:

- the bot sees the recovered positions,
- restores them into Rust,
- then stops evaluating them as part of the normal strategy/risk path.

That is why restart currently feels "non-autonomous".

### Root Cause B — `_on_startup()` says hedge gap is warning-only, but state still stays `manual_review`

`scripts/live_trader_v2.py:3453-3474` is internally contradictory:

- it clears `_startup_manual_review_symbols`
- immediately calls `_refresh_startup_recovery_flags(current_positions)`
- logs that hedge gaps are "warning only" and the bot will continue trading
- clears only the `hedge_gap` safe-mode flag

But `_refresh_startup_recovery_flags(...)` repopulates `startup_manual_review` from the persisted rows, because the rows still contain `recovery_state="manual_review"`.

So the comment says "warning only", but the actual persisted state still says "operator review". The runtime therefore keeps treating those positions as non-managed.

### Root Cause C — Rust exit normalization knows step size, but not max market quantity

Relevant Rust paths:

- `execution_engine/src/order_manager.rs:665-689`
- `execution_engine/src/order_manager.rs:1378-1415`
- `execution_engine/src/order_manager.rs:2098-2145`
- `execution_engine/src/binance_rest.rs:29-34`
- `execution_engine/src/binance_rest.rs:298-334`

What happens today:

1. Python restores a hedge-gap long with `spot_qty=0`, `perp_qty=full futures size`.
2. Python dispatches an exit with `skip_spot_leg=True` (`scripts/live_trader_v2.py:1043-1068`), which is correct for a missing spot hedge.
3. Rust resolves exit quantity from tracked position size and normalizes **only by step size**.
4. Rust submits one single-leg futures market order for the whole tracked quantity.
5. Binance rejects large orders with `-4005 Quantity greater than max quantity`.

The Rust exchange metadata struct does not carry `maxQty`, and the parser only records `tickSize` and `stepSize`. There is no slicing logic for oversized market exits.

### Root Cause D — Flatten retry policy keeps resubmitting the same impossible exit forever

There are two retry loops layered on top of each other:

1. `scripts/live_trader_v2.py:5351-5415`
   `_maybe_process_operator_flatten_all_request(...)`
   redispatches every still-open symbol on each trading-loop pass while request status stays active.

2. `scripts/live_trader_v2.py:5191-5217`
   `_on_order_rejected(...)`
   immediately retries any rejected exit after 0.5 s.

And the failure path at `scripts/live_trader_v2.py:4631-4728`:

- clears the pending exit
- records startup-recovery failure
- leaves the symbol open
- does **not** move the flatten request to `failed` / `partial_failed`

Also note:

- `_dispatch_startup_recovery_exits(...)` respects backoff and stuck-symbol state.
- `_maybe_process_operator_flatten_all_request(...)` does **not**.

So once DYMUSDT / STRKUSDT hit repeated hard rejects, the operator flatten request never converges. It just churns forever.

### Root Cause E — `hedge_gap` flag semantics do not match actual symbol blocking

`hedge_gap` is in `_PER_SYMBOL_SAFE_MODE_FLAGS`, but `_blocked_entry_symbols()` does not include hedge-gap symbols.

That means `hedge_gap` affects runtime-mode labeling, but not actual per-symbol entry blocking logic. This is secondary to the main bug, but it should be cleaned up while touching recovery classification so the UI and behavior agree.

---

## 3. What The Fix Must Achieve

After the PR lands and the bot restarts with the current exchange state:

1. The bot must still recover open positions from Binance and restore them into Rust.
2. A recovered hedge-gap long must **not** become `manual_review` just because spot is missing.
3. Those positions must count as **managed** positions and remain eligible for:
   - funding-decay exits,
   - kill-switch exits,
   - derisk exits,
   - admin flatten exits.
4. Emergency Flatten must either:
   - fully flatten them by chunking the futures leg into valid order sizes, or
   - fail cleanly once with a terminal request status and a useful note.
5. No repeated 1 Hz redispatch loop.
6. No startup error that forces operator acknowledgement for a plain hedge-gap long.

---

## 4. Implementation Plan

### 4.1 Split "unsupported manual review" from "hedge gap but still manageable"

Primary file: `scripts/live_trader_v2.py`

Change the recovery model so that:

- `manual_review` is reserved for positions the runtime truly cannot reconstruct or unwind safely.
  - Keep unsupported inverse / long-perp structures in this bucket.
  - Keep ambiguous exchange-state cases in this bucket if any exist.

- A recovered long with partial or zero spot hedge should no longer be `manual_review`.
  - It should be classified as normal `tracked` or `exit_candidate`.
  - The hedge shortfall should be surfaced separately through `hedge_gap_symbols` and reconciliation metadata.

Recommended implementation shape:

- Update `_classify_startup_recovered_position(...)` so the hedge-gap branch no longer returns `manual_review`.
- Preserve the existing funding-based decision:
  - if funding stale: `tracked`
  - if funding decayed: `exit_candidate`
  - else: `tracked`
- Keep the hedge-gap reason in reconciliation snapshot / risk snapshot so the UI still explains why the symbol is degraded.

Important consequence:

- Once hedge-gap longs stop being `manual_review`, the normal trading/risk loop will manage them automatically.
- Their exits will still be perp-only because `_exit_leg_skip_flags(...)` already does the right thing for `hedge_ratio == 0`.

### 4.2 Remove the false "warning only" contradiction

Still in `scripts/live_trader_v2.py`:

- Clean up `_on_startup()` so the code matches the intended behavior.
- Do not clear `hedge_gap` and claim "continue trading" while rows still repopulate `startup_manual_review`.

After the reclassification above, startup should end in one of these states:

- `LIVE`
- `LIVE_WITH_SYMBOL_BLOCKS` only for true unsupported/manual-review symbols

It should **not** use startup manual review as the hedge-gap carrier anymore.

### 4.3 Make large futures exits chunk-aware in Rust

Primary files:

- `execution_engine/src/binance_rest.rs`
- `execution_engine/src/order_manager.rs`

Required work:

1. Extend `ExchangeSymbolInfo` to include futures/spot max quantity fields.
   - Prefer `MARKET_LOT_SIZE.maxQty` for market orders.
   - Fall back to `LOT_SIZE.maxQty` if `MARKET_LOT_SIZE` is absent.

2. Extend the exchange-info parser to extract:
   - `stepSize`
   - `maxQty`
   - optionally `minQty` for completeness

3. Add a quantity planner in Rust for exit orders.
   - Given tracked remaining qty, venue, and market-order max qty:
     - slice the exit into valid chunks,
     - round each chunk to step size,
     - submit sequentially,
     - update tracked leg after each fill,
     - only emit final success/failure once the whole leg is done.

4. Apply this especially to the single-leg unwind path (`skip_spot_leg=true` / futures-only).

Do **not** try to solve this in Python by splitting intents there first. The Rust engine already owns tracked leg state and is the right layer to chunk market exits safely.

### 4.4 Stop endless flatten redispatches

Primary file: `scripts/live_trader_v2.py`

Fix `_maybe_process_operator_flatten_all_request(...)` so it is not a "fire every open symbol every second" loop.

Required behavior:

- Dispatch once per eligible symbol.
- Skip symbols that already have:
  - pending exit intent,
  - active exit event,
  - backoff window still running,
  - stuck/hard-failed state.
- Track per-request progress:
  - dispatched
  - pending
  - filled
  - failed

When a symbol hard-fails:

- do not keep redispatching it every cycle,
- retain it in `remaining_symbols`,
- update the flatten request note/status clearly.

Recommended terminal states:

- `completed`
- `partial_failed`
- `failed`

`in_progress` must not survive forever on a deterministic hard reject.

### 4.5 Narrow exit auto-retry behavior

Primary file: `scripts/live_trader_v2.py`

`_on_order_rejected(...)` currently retries every rejected exit after 0.5 seconds.

That is too broad.

Change it so that immediate retry happens only for transient transport-style failures, not for hard semantic rejects such as:

- `SINGLE_LEG_SUBMISSION_FAILED`
- `DUAL_SUBMISSION_FAILED`
- invalid skip flags
- size-limit rejects like `Quantity greater than max quantity`

Once Rust slicing is added, those size-limit rejects should mostly disappear anyway. But the Python layer still needs sane retry filtering so one new hard reject cannot recreate the loop.

### 4.6 Keep unsupported inverse positions as real manual review

Do **not** remove the current unsupported inverse/manual-review flow.

This test should still stay conceptually true:

- `tests/test_live_trader_startup.py:4310-4384`
  `test_live_startup_tracks_unsupported_inverse_style_positions_for_manual_review`

That path is materially different from a plain missing-spot hedge-gap long.

### 4.7 Optional cleanup: make hedge-gap symbol blocking explicit

Secondary cleanup, but worth doing while in the area:

- either remove `hedge_gap` from runtime-mode symbol-block semantics and treat it as visibility-only,
- or maintain an explicit internal hedge-gap symbol set and include it in `_blocked_entry_symbols()` / `_describe_symbol_block()`.

Pick one model and make the runtime mode, UI messaging, and actual symbol blocking agree.

For this PR, the important thing is: **hedge gap must not force operator-only startup review for otherwise manageable recovered longs.**

---

## 5. Tests To Add Or Update

### Python: `tests/test_live_trader_startup.py`

Add / update tests for the new intended behavior:

1. `test_live_startup_hedge_gap_long_is_tracked_not_manual_review`
   - startup recovers long perp with missing spot
   - expect `recovery_state != "manual_review"`
   - expect symbol appears in `hedge_gap_symbols`

2. `test_restart_hedge_gap_positions_count_as_managed_open_positions`
   - with current DYM/STRK-like rows
   - expect `managed_open_position_count == len(open_positions)`
   - expect `manual_review_position_count == 0`

3. `test_kill_switch_can_exit_recovered_hedge_gap_long`
   - recovered long with `hedge_ratio=0`
   - kill-switch / derisk path should dispatch exit through normal managed flow

4. `test_operator_flatten_all_does_not_redispatch_hard_failed_symbol_every_cycle`
   - active flatten request
   - one symbol hard-fails
   - subsequent cycles do not spam `_dispatch_exit` again immediately

5. `test_operator_flatten_all_marks_partial_failed_when_symbol_cannot_be_closed`
   - verify request leaves `in_progress`

6. `test_hard_rejected_exit_is_not_immediately_retried`
   - cover `_on_order_rejected(...)`

Update or replace existing tests that encode the old unwanted behavior for zero-hedge longs:

- `tests/test_live_trader_startup.py:3813-4025`
  These currently assume zero/partial hedge long recovery becomes `manual_review` and depends on `startup_recovery_auto_exit_manual_review`.
  Those assumptions should be narrowed so they only apply to truly unsupported structures, not to plain hedge-gap longs.

Keep the unsupported inverse/manual-review tests unchanged in intent:

- `tests/test_live_trader_startup.py:4310-4384`

### Rust: `execution_engine/src/order_manager.rs` and friends

Add Rust tests for:

1. parsing `maxQty` / market lot size from exchange info
2. large single-leg futures unwind is chunked into valid market-order slices
3. restored `spot_qty=0`, large `perp_qty>maxQty` position can be fully flattened
4. hard submission failure leaves no stale chase state and emits one terminal failure, not an infinite retry loop

---

## 6. Things The Local AI Should Not Do

Do **not** take any of these shortcuts:

- Do not "fix" this by simply setting `startup_recovery_auto_exit_manual_review = true`.
  - That would just auto-liquidate hedge-gap longs on every restart.
  - The user explicitly wants the bot to evaluate and continue managing recovered positions, not panic-close them by default.

- Do not just clear `startup_manual_review` flags on startup without changing classification.
  - The rows would still be wrong, and the next refresh would recreate the same behavior.

- Do not only suppress the Python retry loop without adding Rust max-qty slicing.
  - Flatten would stop spamming, but it still would not work on large positions.

- Do not patch `state.db` or `live_config.json` in the PR.
  - The fix needs to be code-level and restart-safe.

---

## 7. Acceptance Criteria

The PR is successful when all of the following are true on a fresh restart:

1. The bot recovers open exchange positions without crashing or demanding operator review for plain hedge-gap longs.
2. A recovered hedge-gap long is visible **and managed**:
   - not `manual_review`
   - counted in `managed_open_position_count`
   - eligible for risk/funding exits
3. Emergency Flatten closes DYMUSDT / STRKUSDT-style naked perps without `-4005 Quantity greater than max quantity`.
4. If an exit cannot be completed automatically, the flatten request transitions to a terminal failure state with a useful note instead of looping forever.
5. The log no longer shows repeated 1 Hz redispatches of the same flatten request.

---

## 8. Suggested Implementation Order

Apply the fix in this order to keep the diff understandable:

1. Reclassify hedge-gap longs away from `manual_review` in Python.
2. Update Python risk/trading-loop assumptions and tests.
3. Extend Rust exchange metadata with max market quantity.
4. Implement chunked single-leg exit execution in Rust.
5. Tighten Python flatten request lifecycle and retry policy.
6. Run focused Python + Rust regression tests.

That order gives you a clean separation:

- first fix restart autonomy,
- then fix actual flatten execution,
- then fix retry/request lifecycle polish.
