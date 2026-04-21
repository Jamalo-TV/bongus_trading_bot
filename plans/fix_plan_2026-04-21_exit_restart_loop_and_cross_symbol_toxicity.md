# Fix Plan — Stuck DYMUSDT exit, cross-symbol toxicity gate, and the 20-minute restart loop (2026-04-21)

> Intended reader: the local AI that will edit the code and push to the
> server. **Do NOT touch the live server, `state.db`, or `live_config.json`.**
> Source edits only. Startup must continue to boot with whatever
> positions exist on the exchange; no change here may raise, exit, or
> halt the main loop.

---

## 0. Status of the prior plan (2026-04-20 restart-restore alerts)

None of the five fixes in
[plans/fix_plan_2026-04-20_restart_restore_alerts_and_pnl_sign.md](fix_plan_2026-04-20_restart_restore_alerts_and_pnl_sign.md)
have been applied. I confirmed on commit `7fee51d` by reading:

| # | Symptom | Where | Still present? |
|---|---------|-------|---|
| 1 | `POSITION OPENED` fires for restored orphans on restart | [bongus/monitoring/telegram_alerter.py:313-328](../bongus/monitoring/telegram_alerter.py#L313-L328) — no `recovery_state` gate | yes |
| 2 | `Funding Collected: \`+$-4.0384\`` | [bongus/monitoring/telegram_alerter.py:342](../bongus/monitoring/telegram_alerter.py#L342) still hard-codes `+$` | yes |
| 3 | Supervisor anomaly fires on `exit_candidate` rows | [bongus/supervisor/core.py:32-36](../bongus/supervisor/core.py#L32-L36) — filter only excludes `manual_review` | yes |
| 4 | Alert uses live `ann_funding` instead of `entry_ann_funding` | [bongus/monitoring/telegram_alerter.py:326](../bongus/monitoring/telegram_alerter.py#L326) | yes |
| 5 | Direction line always shows "Long Spot / Short Perp" for naked orphans | [bongus/monitoring/telegram_alerter.py:316-320](../bongus/monitoring/telegram_alerter.py#L316-L320) | yes |

All five are still valid and should be landed. This new plan
supersedes nothing in the old one — both should be applied. The
items in §4 below are **strictly higher priority** because they are
the root cause of the current restart-every-25-minutes loop that
generates the Telegram noise the previous plan describes.

---

## 1. Executive summary

[scripts/logs/live_trader.log](../scripts/logs/live_trader.log) on
2026-04-21 shows a live production loop that has been running for
hours:

```
12:26:46 [WATCHDOG] trader stuck in SAFE_MODE/stale_pending_intent for 1207s. Restarting trader process to trigger startup reconciliation.
12:26:50 CRITICAL Startup recovery: DYMUSDT has confirmed open position but stale EXIT intent. Clearing intent — trading loop will re-dispatch exit.
12:27:04 INFO     Startup recovery: exiting DYMUSDT (funding decayed to -63.40% annualized and should be exited)
12:27:04 INFO     EXIT dispatched for DYMUSDT qty=224424.30000 (skip_spot=True, skip_perp=False)
12:27:04 INFO     [rust] Dynamic chase state initialized from AlphaInstruction for DYMUSDT.
...  (no order placement, no fill, no cancel, no reject — just silence) ...
12:52:12 [WATCHDOG] trader stuck in SAFE_MODE/stale_pending_intent for 1207s. Restarting ...
12:52:16 CRITICAL Startup recovery: DYMUSDT has confirmed open position but stale EXIT intent ...
```

This pattern repeats every ~25 minutes (four times in the current log
window, eight+ counting rotated logs). Each iteration re-initializes
the DYMUSDT chase state in Rust; Rust never places a Binance order;
Python times out after 300 s; the watchdog restarts Python 1207 s
later; repeat.

Root cause: the Rust execution engine's `try_place_dual_maker`
function — the only place that turns a `ChaseState::Idle` into a live
order — is gated by a **global** `!self.is_toxic` flag. Because
`AGLDUSDT`, `FIOUSDT`, `TRUUSDT`, and `DENTUSDT` flap spread-toxic
hundreds of times per run, the global flag is effectively always
true, and **no symbol** can place any order — including a single-leg
MARKET unwind on DYMUSDT. The flag is cross-symbol but all the
reasoning behind it is per-symbol maker depth, so it is simply wrong.

Secondary causes that prolong or mask the loop:

- **Single-leg MARKET unwinds are gated by the same maker-only
  toxicity check.** Even a per-symbol flag would be wrong for taker
  orders.
- **`try_place_dual_maker` is only called from incidental WS ticks.**
  No background timer re-attempts placement, so any lull in
  market-data traffic or any toxicity flicker indefinitely postpones
  the exit.
- **ENTER rejections with `reason=chase_active` leak pending intents
  for 300 s.** `_on_order_rejected` at
  [scripts/live_trader_v2.py:5197](../scripts/live_trader_v2.py#L5197)
  only handles EXIT rejections; ENTER rejections just log a warning
  and let the `_pending_enters` row sit until the stale-intent timer
  fires. TRUUSDT, DENTUSDT, ACTUSDT all show this pattern.

The alerter bugs from the 2026-04-20 plan make the loop even more
alarming to the operator (spurious POSITION OPENED, `+$-X.XXXX`
funding, duplicate supervisor anomalies) but they are cosmetic; the
restart loop is real and bleeds funding on DYMUSDT every hour the
position stays open at −60 % annualized.

---

## 2. Evidence

### 2.1 Restart-every-25-minutes loop

[scripts/logs/live_trader.log](../scripts/logs/live_trader.log):

| Line | Timestamp | Event |
|------|-----------|-------|
| 1466 | 12:26:46 | `[WATCHDOG] trader stuck in SAFE_MODE/stale_pending_intent for 1207s. Restarting ...` |
| 2948 | 12:52:12 | same, 1207 s |
| 4554 | 13:17:37 | same, 1208 s |
| 6635 | 13:43:03 | same, 1209 s |

Driving logic at
[bongus/monitoring/king_watchdog.py:755-773](../bongus/monitoring/king_watchdog.py#L755-L773):
if `runtime_mode == "SAFE_MODE"` and the reason contains
`stale_pending_intent` for more than
`SAFE_MODE_STALE_INTENT_RESTART_SECONDS`, terminate the trader and
restart it. That is firing correctly; the problem is that the reason
never clears because the DYMUSDT EXIT never completes.

### 2.2 DYMUSDT EXIT never places an order

Every restart cycle follows the identical path:

```
Handling Alpha Instruction: AlphaInstruction { symbol: Some("DYMUSDT"), intent: "EXIT_LONG", quantity: 224424.3, ..., skip_spot_leg: true, skip_perp_leg: false, ... }
EXIT received for DYMUSDT while chase active (phase: Idle) — preempting chase
Dynamic chase state initialized from AlphaInstruction for DYMUSDT.
```

Then nothing. `grep DYMUSDT` across the entire live log produces 48
matches, and every one of them is one of:

- `Skipping ... safe mode: ... startup_exit_candidate` (Python main loop declining new entries)
- `Handling Alpha Instruction` for RESTORE_POSITION or EXIT_LONG
- `Restored tracked position`
- `Synced recovered position`
- `Startup recovery: ...`
- `Spot hedge gap detected`
- `EXIT dispatched`
- `EXIT received for DYMUSDT while chase active (phase: Idle) — preempting chase`
- `Dynamic chase state initialized from AlphaInstruction`
- `Pending EXIT for DYMUSDT is older than 300s`

No `Placing single-leg MARKET unwind for DYMUSDT`, no fill event, no
cancellation, no rejection, no order id. The chase is initialized
and then sits at `ChasePhase::Idle` forever.

### 2.3 Global toxicity gate blocks `try_place_dual_maker`

The only code path that flips `ChasePhase::Idle` into a live order
for a single-leg EXIT is
[execution_engine/src/order_manager.rs:2004-2129](../execution_engine/src/order_manager.rs#L2004-L2129).
It is called from two places:

- [order_manager.rs:1517](../execution_engine/src/order_manager.rs#L1517)
  — inside the BookTicker (`WsEvent::BookTicker`) handler, gated by
  `if !self.is_toxic { ... }`
- [order_manager.rs:1612](../execution_engine/src/order_manager.rs#L1612)
  — inside the L2Depth handler, same gate

And `is_toxic` is set one line above each call site at
[order_manager.rs:1514](../execution_engine/src/order_manager.rs#L1514):

```rust
self.is_toxic = !self.toxic_symbols.is_empty();
```

So if *any* symbol is in `toxic_symbols`, `is_toxic = true` and
`try_place_dual_maker` is skipped — for **every** symbol, not just
the toxic one.

Observed toxicity flap in the current log:

| Symbol | `Spread toxicity detected` | `Toxicity resolved` | Net residual |
|--------|-----:|-----:|-----:|
| AGLDUSDT | 548 | 510 | 38 |
| DENTUSDT | 465 | 464 | 1 |
| TRUUSDT | 219 | 14 | **205** |
| FIOUSDT | 209 | 0 | **209** |
| SUIUSDT | 83 | 21 | 62 |
| ETHUSDT | 66 | 66 | 0 |
| BANKUSDT | 66 | 43 | 23 |

Four of the seven symbols are monotonically accruing toxicity (more
detects than resolves), and the total detected-vs-resolved count
across the whole engine is 1658 vs 1120. At any instant
`toxic_symbols` is virtually guaranteed to be non-empty, so
`is_toxic = true` globally, so `try_place_dual_maker` never runs.

### 2.4 The unwind path is a MARKET order, not a maker

[order_manager.rs:2105-2129](../execution_engine/src/order_manager.rs#L2105-L2129):

```rust
info!(
    "Placing single-leg MARKET unwind for {} on {:?}",
    chase_snapshot.symbol, active_leg
);
...
let submission = match active_leg {
    Leg::Spot => self.binance_rest.place_spot_market_order(...).await,
    Leg::Futures => self.binance_rest.place_futures_market_order(...).await,
};
```

The EXIT gets preempted at
[order_manager.rs:1331-1336](../execution_engine/src/order_manager.rs#L1331-L1336)
and falls through to a fresh `ChaseState` that a subsequent WS tick
pumps into `try_place_dual_maker`. For a single-leg position
(hedge_gap / naked perp), `active_leg()` returns `Some(Leg::Futures)`
and the code places a taker market order. There is no economic
reason to gate a market order on maker-spread toxicity. The gate is
there to protect maker fills from adversely wide spreads, which is
irrelevant for takers.

### 2.5 ENTER rejections leak pending intents

[scripts/logs/live_trader.log](../scripts/logs/live_trader.log):

```
12:27:18 ENTER dispatched for TRUUSDT qty=526074
12:27:18 [rust] Currently executing a Chase for TRUUSDT, skipping new alpha instruction.
12:27:18 [rust] OrderRejected ... reason=chase_active intent_id=enter_long_truusdt_81e930c1d917
12:27:19 OrderRejected from Rust: symbol=TRUUSDT intent=ENTER_LONG reason=chase_active intent_id=...
... (5 minutes of nothing happening) ...
12:32:20 ERROR  Pending ENTER for TRUUSDT timed out after 300s; symbol remains blocked until a terminal update arrives
12:34:55 WARNING Auto-cleared stale ENTER for TRUUSDT because exchange shows no open order or position
```

[scripts/live_trader_v2.py:5191-5212](../scripts/live_trader_v2.py#L5191-L5212):

```python
def _on_order_rejected(self, symbol: str, intent: str, intent_id: str | None, reason: str) -> None:
    """Rust rejected an instruction — for exits, clear pending state and schedule an immediate retry."""
    logger.warning("OrderRejected from Rust: symbol=%s intent=%s reason=%s intent_id=%s", symbol, intent, reason, intent_id)
    is_exit = intent in ("EXIT_LONG", "EXIT_SHORT")
    if not is_exit:
        return
    ...
```

Non-exit rejections (ENTER_LONG / ENTER_SHORT) are logged and
ignored. The in-memory `_pending_enters[symbol]` and the matching
`pending_intents` row stay populated, which:

1. Blocks the main loop from re-entering the symbol until the 300 s
   timer fires (at line 4762-4766).
2. Contributes to SAFE_MODE via `stale_pending_intent` once the 300 s
   elapses — feeding the same watchdog timer that restarts the
   trader.
3. Emits `Pending ENTER for TRUUSDT timed out after 300s` noise at
   CRITICAL log level.

None of this is necessary: Rust already told Python the instruction
was rejected with a terminal reason. Python can discard the pending
row on the spot.

### 2.6 No independent retry timer for idle chases

Even after the toxicity gate is fixed (§4.1), the chase only runs
when a WS tick arrives. In practice WS traffic is abundant and this
is fine, but there is no safety-net timer that re-invokes
`try_place_dual_maker` when a chase has been `Idle` for more than,
say, 2 s. A transient WS hiccup during the 100 ms window between
"fresh chase initialized" and "first WS tick" will silently defer the
exit by however long the next tick takes. For a single-leg naked
unwind of a stuck position, that delay is the difference between
"closed within seconds" and "SAFE_MODE/stale_pending_intent".

### 2.7 What is *not* broken

- Python reconciliation and startup recovery: working as intended,
  successfully classifying DYMUSDT as `exit_candidate`, dispatching
  the EXIT, and persisting the pending intent.
- Rust RESTORE_POSITION handling: working; the recovered perp
  quantity (224424.3) is preserved across restarts.
- Watchdog restart cadence: firing correctly; 1207 s matches
  `SAFE_MODE_STALE_INTENT_RESTART_SECONDS`.
- Trade accounting and SQLite persistence: unaffected.

---

## 3. Root causes

| # | Root cause | File:line |
|---|------------|-----------|
| A | `is_toxic` is a global boolean, but toxicity is per-symbol. `try_place_dual_maker` therefore idles every symbol whenever any symbol's spread is wide. | [order_manager.rs:1514](../execution_engine/src/order_manager.rs#L1514), [order_manager.rs:1516-1517](../execution_engine/src/order_manager.rs#L1516-L1517), [order_manager.rs:1611-1612](../execution_engine/src/order_manager.rs#L1611-L1612) |
| B | Single-leg MARKET unwinds inherit the maker-only toxicity gate even though they are taker orders. | [order_manager.rs:2004-2010](../execution_engine/src/order_manager.rs#L2004-L2010), [order_manager.rs:2105](../execution_engine/src/order_manager.rs#L2105) |
| C | `try_place_dual_maker` only runs on WS ticks; there is no background retry for an Idle chase. | [order_manager.rs:1447-1615](../execution_engine/src/order_manager.rs#L1447-L1615) |
| D | `_on_order_rejected` ignores non-exit rejections; pending ENTER intents leak for 300 s. | [scripts/live_trader_v2.py:5191-5212](../scripts/live_trader_v2.py#L5191-L5212) |
| E | Prior plan (2026-04-20) alerter/supervisor fixes never landed. | see §0 table |

---

## 4. Fix plan

Land in this order. §4.1 is the single change that breaks the loop;
§4.2 and §4.3 are safety nets; §4.4 is the dependent Python change;
§4.5 is the already-scoped alerter plan from 2026-04-20.

### 4.1 Make the toxicity gate per-symbol

**File:** [execution_engine/src/order_manager.rs](../execution_engine/src/order_manager.rs)

At both `try_place_dual_maker` call sites
([1516-1518](../execution_engine/src/order_manager.rs#L1516-L1518)
and
[1611-1613](../execution_engine/src/order_manager.rs#L1611-L1613)),
replace the global `!self.is_toxic` guard with a per-symbol check
using the symbol whose WS tick just arrived:

```rust
if !self.toxic_symbols.contains_key(&sym_upper) {
    self.try_place_dual_maker(sym_upper).await;
}
```

Inside `try_place_dual_maker` itself
([2004-2033](../execution_engine/src/order_manager.rs#L2004-L2033)),
add a defensive early-return for the same check so any future caller
cannot bypass it for a *toxic-on-that-symbol* case — but only for
maker operations. Single-leg market unwinds must skip the check
(§4.2).

Keep `self.is_toxic` as a derived convenience flag if any dashboards
or metrics read it; do not delete it, but stop using it as a gate.

Regression risks:
- Maker dual-maker placements for a currently-healthy symbol will now
  proceed even while a different symbol is toxic. That is the *point*
  and matches the original design intent of the toxicity filter.
- The toxicity gate has always been strictly conservative (deny more
  than needed); loosening it cannot introduce a new code path —
  everything downstream of `try_place_dual_maker` still honours its
  existing checks.

### 4.2 Bypass the maker-toxicity gate for single-leg MARKET unwinds

Inside `try_place_dual_maker`
([2004-2129](../execution_engine/src/order_manager.rs#L2004-L2129)),
the single-leg branch (`if let Some(active_leg) = chase_snapshot.active_leg()`
at [2034](../execution_engine/src/order_manager.rs#L2034)) already
routes to `place_spot_market_order` /
`place_futures_market_order`. Before any per-symbol toxicity check
added in §4.1, skip the gate when the chase is a single-leg exit:

Pseudo-diff at the top of `try_place_dual_maker` (after the
`phase != Idle` guard):

```rust
let is_single_leg_market_unwind = chase_snapshot.is_single_leg() && chase_snapshot.is_exit;
if !is_single_leg_market_unwind && self.toxic_symbols.contains_key(&sym_upper) {
    return;
}
```

Rationale: market orders don't wait for tight spreads. In the
DYMUSDT case the position is bleeding −63 %-annualized funding;
deferring the unwind because some *unrelated* symbol has a wide
spread is actively harmful.

Separately: the log line at
[order_manager.rs:2105](../execution_engine/src/order_manager.rs#L2105)
says "Placing single-leg MARKET unwind" but the function is named
`try_place_dual_maker` and the phase flip at
[2088](../execution_engine/src/order_manager.rs#L2088) is
`ChasePhase::DualMakerPlaced`. Consider splitting this branch into
its own helper (`try_place_single_leg_market_unwind`) in a follow-up
commit for readability, or at minimum use `ChasePhase::TakerPlaced`
or similar for the market-unwind branch. Not a blocker for the fix
but worth fixing while the code is open.

### 4.3 Add a background "nudge" timer for Idle chases

**File:** [execution_engine/src/order_manager.rs](../execution_engine/src/order_manager.rs)

Add a periodic tick (e.g. every 1 s) inside the existing `tokio`
select/loop in `run` that iterates `self.chase_states` and, for any
chase whose `phase == ChasePhase::Idle` and whose `start_time` is
older than 2 s, invokes `try_place_dual_maker(sym_upper)`. This
closes the "WS was silent when the chase initialized" gap.

If a 1 s tick conflicts with existing runtime structure, a simpler
alternative is to call `try_place_dual_maker(sym_upper)` once
*immediately* after `handle_alpha_instruction` initializes a new
chase — with the current cached top-of-book — instead of waiting for
the next WS event. That single line eliminates the dependency on
incidental WS traffic for the common case without introducing a new
timer.

Pseudo-diff at
[order_manager.rs:1441-1444](../execution_engine/src/order_manager.rs#L1441-L1444)
(right after "Dynamic chase state initialized"):

```rust
info!(
    "Dynamic chase state initialized from AlphaInstruction for {}.",
    sym_upper
);
// Kick the chase immediately with the current cached top-of-book
// instead of waiting for the next WS tick.
self.try_place_dual_maker(sym_upper.clone()).await;
```

(`sym_upper` must not have been moved by the preceding insert; use
`.clone()` if needed to preserve ownership.)

### 4.4 Clear pending ENTER state on Rust rejection

**File:** [scripts/live_trader_v2.py:5191-5212](../scripts/live_trader_v2.py#L5191-L5212)

Extend `_on_order_rejected` to handle non-exit rejections. For
`reason == "chase_active"` on an ENTER, the correct action is to
clear the pending_enter row immediately and treat it as "try again
next cycle". For other terminal rejection reasons the row should
also be cleared so the symbol isn't wedged.

Pseudo-diff:

```python
def _on_order_rejected(self, symbol: str, intent: str, intent_id: str | None, reason: str) -> None:
    logger.warning(
        "OrderRejected from Rust: symbol=%s intent=%s reason=%s intent_id=%s",
        symbol, intent, reason, intent_id,
    )
    is_exit = intent in ("EXIT_LONG", "EXIT_SHORT")
    is_enter = intent in ("ENTER_LONG", "ENTER_SHORT")

    if is_enter:
        tracked = self._pending_enters.get(symbol)
        if tracked and (not intent_id or tracked.get("intent_id") == intent_id):
            self._pending_enters.pop(symbol, None)
            if intent_id:
                self.state_writer.update_pending_intent(
                    intent_id,
                    status="REJECTED",
                    last_error=reason,
                )
            # Do NOT synthesise a retry here — the main decision loop
            # will re-evaluate the symbol on its next tick. This avoids
            # tight-loop thrash when Rust's chase is genuinely busy.
        return

    if not is_exit:
        return
    # existing exit-handling body unchanged
    ...
```

Deliberately *not* scheduling a retry: if Rust has an `chase_active`
for this symbol it is probably because an earlier attempt is still
mid-flight. Let the main loop decide when to try again instead of
piling up re-dispatches.

Follow-up (optional, not required for §3 acceptance): `chase_active`
rejections for ENTER that persist across two consecutive main-loop
ticks imply a stuck chase in Rust that the Rust side can't clear
itself. A durable fix would add a `CLEAR_CHASE` Alpha instruction so
Python can forcibly reset Rust's chase_states for a symbol on
reconciliation. Out of scope for this plan.

### 4.5 Land the 2026-04-20 alerter/supervisor plan

See
[plans/fix_plan_2026-04-20_restart_restore_alerts_and_pnl_sign.md](fix_plan_2026-04-20_restart_restore_alerts_and_pnl_sign.md).
All five items are still valid. The specific edits:

- [bongus/monitoring/telegram_alerter.py:313-328](../bongus/monitoring/telegram_alerter.py#L313-L328) — suppress `POSITION OPENED` when `recovery_state` is non-empty
- [bongus/monitoring/telegram_alerter.py:326](../bongus/monitoring/telegram_alerter.py#L326) — prefer `entry_ann_funding` over `ann_funding`
- [bongus/monitoring/telegram_alerter.py:316-320](../bongus/monitoring/telegram_alerter.py#L316-L320) — render "Naked perp (recovered)" for `hedge_ratio <= 0` or `recovery_state` non-empty
- [bongus/monitoring/telegram_alerter.py:342](../bongus/monitoring/telegram_alerter.py#L342) — signed dollar render for `Funding Collected`
- [bongus/supervisor/core.py:32-36](../bongus/supervisor/core.py#L32-L36) — widen the filter to `{manual_review, exit_candidate}`

Ship as separate commits per the 2026-04-20 checklist. Nothing here
changes the logic of the earlier plan.

### 4.6 Do not touch

- `state.db` — operational state on the live server.
- `live_config.json` — no config knob here.
- Reconciler / SAFE_MODE / `recovery_state` classification logic in
  [scripts/live_trader_v2.py:2123-2234](../scripts/live_trader_v2.py#L2123-L2234).
- `king_watchdog.py` safe-mode restart timer. Once §4.1-§4.4 land,
  the trader will no longer enter the `stale_pending_intent` loop,
  so the timer is simply unused. Do not shorten or remove it — it is
  still the correct safety net if a real stall recurs.

---

## 5. Acceptance criteria

A PR is acceptable iff **all** hold:

1. With `AGLDUSDT` / `FIOUSDT` / `TRUUSDT` flapping spread-toxic, a
   dispatched single-leg EXIT for a different symbol (e.g. DYMUSDT)
   produces a `Placing single-leg MARKET unwind for DYMUSDT on
   Futures` log line within 2 s of receipt of the EXIT Alpha
   instruction — not only after every toxic symbol clears.
2. A fresh regression test in
   [execution_engine/src/order_manager.rs](../execution_engine/src/order_manager.rs)
   (alongside the existing tests near line 2677+) covers:
   - per-symbol toxicity no longer blocks a healthy symbol's
     dual-maker placement
   - single-leg EXIT market unwind proceeds regardless of any
     symbol's toxicity
3. `Pending EXIT for DYMUSDT is older than 300s` does not fire in a
   scenario where the engine would have placed the market unwind in
   §5.1. Regression test added under
   [tests/test_live_trader_order_lifecycle.py](../tests/test_live_trader_order_lifecycle.py)
   or an appropriate neighbour (create one if none fits).
4. Rust-side ENTER rejections with `reason=chase_active` clear the
   corresponding `_pending_enters` entry immediately. Pending-intent
   row is marked `REJECTED`. Regression test:
   `tests/test_live_trader_order_lifecycle.py` — call
   `_on_order_rejected(symbol, "ENTER_LONG", intent_id, "chase_active")`
   with a pre-seeded `_pending_enters[symbol]` and assert it's gone
   and the pending_intent row is updated.
5. Watchdog no longer restarts the trader purely because a symbol
   is toxic: in a simulated 10-minute run with constant toxicity
   flap on three symbols and a fresh EXIT on a fourth,
   `SAFE_MODE/stale_pending_intent` never fires. (If this is not
   unit-testable against the real binary, document it in the PR
   description with a reproduction script under
   [scripts/](../scripts/).)
6. All items from
   [plans/fix_plan_2026-04-20_restart_restore_alerts_and_pnl_sign.md](fix_plan_2026-04-20_restart_restore_alerts_and_pnl_sign.md)
   §3 are satisfied (either landed in this PR or tracked as
   follow-ups with open issues).
7. `cargo test`, `pytest tests/`, and `pyright` stay green.
8. Startup continues to boot with whatever positions exist on the
   exchange; existing startup-recovery + safe-mode behaviour is
   preserved unchanged.

---

## 6. Implementation checklist for the local AI

- [ ] **Commit 1 (Rust)** — §4.1. Per-symbol toxicity gate at both
      call sites; defensive in-function check. Add Rust test:
      `toxic_unrelated_symbol_does_not_block_placement`.
- [ ] **Commit 2 (Rust)** — §4.2. Bypass toxicity gate for
      `is_single_leg() && is_exit` market unwinds. Add Rust test:
      `single_leg_market_unwind_ignores_toxicity`.
- [ ] **Commit 3 (Rust)** — §4.3. Kick `try_place_dual_maker`
      immediately after `Dynamic chase state initialized`. Add Rust
      test that simulates a chase init with pre-populated
      top-of-book and asserts a `Placing single-leg MARKET unwind`
      log/event (or an equivalent observable side effect) without a
      subsequent WS tick.
- [ ] **Commit 4 (Python)** — §4.4. Extend `_on_order_rejected` to
      clear pending_enter state. Add
      `tests/test_live_trader_order_lifecycle.py::test_enter_rejection_clears_pending_enter`.
- [ ] **Commit 5-9 (Python)** — §4.5, i.e. the five checklist items
      from the 2026-04-20 plan. One commit each per that plan's
      §6.

Each commit keeps `cargo test`, `pytest tests/`, and `pyright`
green. Run the preflight build (`cd execution_engine && cargo build
--release`) at least once before the final PR because the
watchdog's preflight depends on it.

---

## 7. What the operator does after the fix lands

1. Pull, rebuild the Rust engine, restart via
   [bongus/monitoring/king_watchdog.py](../bongus/monitoring/king_watchdog.py).
   No DB surgery. No config change.
2. On next startup, Python reconciliation will again classify DYMUSDT
   as `exit_candidate` and dispatch the EXIT. This time Rust will
   place a market unwind within ~1 s, the unwind fills, and the
   position closes.
3. After the close:
   - `POSITION CLOSED` alert correctly formats `Funding Collected` with
     the right sign (prior plan §4.2).
   - Supervisor no longer emits the duplicate "Open position with
     non-positive funding" anomaly for rows already in
     `exit_candidate` (prior plan §4.3).
   - The `stale_pending_intent` flag clears; the 25-minute watchdog
     restart loop ends.
4. ATAUSDT and QUICKUSDT (the other two hedge-gap symbols in the
   current run) will follow the same path when their funding flips.
   Verify this by watching the `recovery_state` field in
   `positions` and a corresponding EXIT dispatch with a normal
   fill.

No separate action is required for other plans under
[plans/](../plans/); they are orthogonal.
