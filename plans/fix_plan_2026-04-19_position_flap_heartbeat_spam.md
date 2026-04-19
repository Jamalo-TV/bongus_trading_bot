# Fix Plan — Over-Slot Orphans, Runtime-Mode Flap, Heartbeat Spam, Non-Positive-Funding Anomaly, DUAL_SUBMISSION_FAILED Retry Loop (2026-04-19)

> **Intended reader:** the local AI that will edit code and push to the
> server.
> **Scope:** code-only. Do **not** modify `state.db`, `live_config.json`,
> or anything else on the running server. The operator does the deploy
> after the PR lands.
> **Hard constraint from user:** the bot must always be able to boot
> with whatever open positions are on the exchange (including orphans).
> No change here may introduce a startup-blocking condition or break
> the existing continue-on-restart invariant.

---

## 0. Executive summary

The user reported, with Telegram evidence, five independent symptoms:

| # | Symptom | What the user sees |
|---|---------|--------------------|
| A | **Dashboard shows `6 position(s) | 7 trades tracked`** — `MAX_CONCURRENT_POSITIONS = 4` | 6 open positions held simultaneously, 2 more than the hard cap |
| B | **Runtime mode flapping every ~60 s between `SAFE_MODE` and `LIVE_WITH_SYMBOL_BLOCKS`** with `safe_mode_reason = stale_pending_intent, startup_manual_review (manual_review=ACTUSDT, DEGOUSDT, DYMUSDT)` | Telegram spam: `🧭 RUNTIME MODE CHANGED` and `⚠️ SAFE MODE ACTIVE` on repeat |
| C | **Heartbeat status flapping `ok` → `missed` → `ok`** | `💓 HEARTBEAT STATUS` alerts firing on every tick transition with no debounce |
| D | **Supervisor anomaly `Open position exists while annualized funding is non-positive`** even though the live slots were all opened on positive funding | `🚨 supervisor anomaly summary` alert triggered by one orphan symbol's current funding |
| E | **`DUAL_SUBMISSION_FAILED` retry loop** on a small set of symbols (DENTUSDT, ZECUSDT, ZENUSDT, 1000CATUSDT, BANKUSDT, TRBUSDT), each producing a 300 s pending-ENTER that becomes stale, fails again immediately, and keeps the `stale_pending_intent` flag oscillating | Pending enters count climbs to 1–3 and drops on every heartbeat log line; the failure log entries drive symptom B |

All five share one underlying reality: **the system is currently
carrying orphaned `manual_review` positions (ACTUSDT, DEGOUSDT,
DYMUSDT, plus pre-existing live slots FIOUSDT, STRKUSDT, TRUUSDT as of
the most recent log batch) and a handful of symbols that repeatedly
fail `DUAL_SUBMISSION_FAILED` on entry.** Today's code does not
distinguish `manual_review` orphans from active, managed positions in
three critical places:

1. Slot-budget math (symptom A) — orphans are filtered *out* of the
   allocator input, so the allocator happily opens 4 new positions on
   top of them. That fix is simple but **must** be made correctly, or
   the bot will start refusing to run because an ongoing orphan eats
   its slot budget.
2. Supervisor funding aggregation (symptom D) — orphans' current
   funding is blended into the `min()` used for the anomaly detector.
3. Exit-flow interaction with ENTER rejections (symptom E) — rejected
   entries do not activate a cooldown, so the allocator keeps picking
   the same broken symbol, which in turn drives the runtime-mode
   flap.

Symptoms B and C are alert-debounce regressions that only *manifest*
because of symptom E and because Rust timing jitter (clock drift +
busy loops handling failed orders) makes the heartbeat tick
non-monotonic. Both are fixed with small, local changes in
`telegram_alerter.py`.

None of these fixes changes what the bot *does*. They change what the
bot *alerts about* and how it counts orphans when budgeting new slots.

---

## 1. Evidence

### 1.1 Symptom A — over-slot orphans

**Dashboard** (reported by user):

```
6 position(s) | 7 trades tracked
```

The "7 trades tracked" = 6 open + 1 closed, per
[bongus/monitoring/performance_metrics.py:112](../bongus/monitoring/performance_metrics.py#L112).
That's not a counting bug — there really are 6 open positions.

**Log** [scripts/logs/live_trader.log:139](../scripts/logs/live_trader.log#L139):

```
Spot hedge gap after live recovery for ACTUSDT, DEGOUSDT, DYMUSDT,
FIOUSDT, STRKUSDT, TRUUSDT
```

ACTUSDT, DEGOUSDT, DYMUSDT are the three manual-review orphans called
out in the Telegram alert (`manual_review=ACTUSDT, DEGOUSDT, DYMUSDT`).
FIOUSDT, STRKUSDT, TRUUSDT are three "normal" slots that the user
saw `⚡ ORDER FILLED` / `📈 POSITION OPENED` alerts for. 3 + 3 = 6.

**Why the allocator lets this happen** —
[scripts/live_trader_v2.py:5840-5863](../scripts/live_trader_v2.py#L5840-L5863)
(method `_get_open_positions`):

```python
for r in rows:
    if str(r.get("recovery_state") or "").strip().lower() == "manual_review":
        continue
    ...
```

Manual-review orphans are **filtered out of the list passed to the
allocator**. The allocator then does, in
[bongus/portfolio/portfolio_allocator.py:158](../bongus/portfolio/portfolio_allocator.py#L158):

```python
free_slots = max(0, MAX_CONCURRENT_POSITIONS - len(open_positions))
```

With `open_positions = []` (all three orphans filtered out) it computes
`free_slots = 4 - 0 = 4` even though the exchange already carries 3
orphans. It then fills 3 new slots → 3 orphans + 3 live = **6**.

The orphan symbols *are* already in the `blocked_symbols` set
propagated to the allocator (via `_PER_SYMBOL_SAFE_MODE_FLAGS`
containing `"startup_manual_review"`), so the allocator will not
*reopen the same symbol*. It just doesn't know to **reserve a slot**
for the orphan's existence.

### 1.2 Symptom B — runtime mode flap

Telegram dump (abbreviated):

```
⚠️ SAFE MODE ACTIVE
Reason: stale_pending_intent, startup_manual_review (manual_review=ACTUSDT, DEGOUSDT, DYMUSDT)
...
🧭 RUNTIME MODE CHANGED
New: LIVE_WITH_SYMBOL_BLOCKS
...
⚠️ SAFE MODE ACTIVE
Reason: stale_pending_intent, startup_manual_review (...)
...
🧭 RUNTIME MODE CHANGED
New: LIVE_WITH_SYMBOL_BLOCKS
```

repeated several times within minutes. Key observation: the reason
includes `stale_pending_intent` during SAFE_MODE, and drops it during
LIVE_WITH_SYMBOL_BLOCKS. That tells us the flag that flips is
`stale_pending_intent`, not `startup_manual_review` (which is stable).

**Log** (abridged from
[scripts/logs/live_trader.log](../scripts/logs/live_trader.log)):

```
L1519: Pending ENTER for DENTUSDT timed out after 300s; symbol remains blocked until a terminal update arrives
L1626: Pending ENTER for 1000CATUSDT timed out after 300s
... dozens of:
Entry failure for DENTUSDT ended with REJECTED (DUAL_SUBMISSION_FAILED) but exchange shows no surviving position to recover
```

Cycle: allocator dispatches enter → Rust rejects with
`DUAL_SUBMISSION_FAILED` → `_handle_failed_order_update` pops the
pending intent → self-heal clears `stale_pending_intent` → runtime
mode drops to `LIVE_WITH_SYMBOL_BLOCKS` → allocator re-picks the same
symbol on the next tick → new pending intent → 300 s timeout fires →
`stale_pending_intent` re-raises → runtime mode flips to `SAFE_MODE`.

The runtime-mode debounce
([bongus/monitoring/telegram_alerter.py:51](../bongus/monitoring/telegram_alerter.py#L51),
`_RUNTIME_MODE_DEBOUNCE_S = 60.0`) is defeated because *each leg* of
the flap lasts longer than 60 s (the 300 s pending-timeout + self-heal
cycle).

### 1.3 Symptom C — heartbeat flap with no debounce

Telegram dump: `💓 HEARTBEAT STATUS State: missed` →
`State: ok` → `State: missed` in close succession.

**Code**
[bongus/monitoring/telegram_alerter.py:495-500](../bongus/monitoring/telegram_alerter.py#L495-L500)
(approximate line; per the summary, the heartbeat handler was near
the end of the alerter alongside runtime-mode and safe-mode-reason
debounces):

```python
if heartbeat_status != prev_heartbeat_status and heartbeat_status:
    await send_telegram(session, "💓 *HEARTBEAT STATUS*\nState: `{heartbeat_status}`")
```

— edge-triggered, no candidate debounce.

**Source of the flap**
[scripts/live_trader_v2.py:2959-2986](../scripts/live_trader_v2.py#L2959-L2986)
(heartbeat loop) + lines 1322-1326 (state write):

```python
heartbeat_status = "ok" if self._heartbeat_misses == 0 and self._last_heartbeat_ack_monotonic > 0.0 \
                    else ("missed" if self._heartbeat_misses > 0 else "unknown")
```

A single missed ack (Rust busy handling a -1021 recvWindow retry or
user-data-stream reconnect) takes `_heartbeat_misses` 0 → 1 for
exactly one tick before the next ack resets it to 0. That one-tick
excursion is enough to flip the persisted status from `ok` to `missed`
and immediately back, firing two Telegram alerts.

Per [bongus/core/config.py:245](../bongus/core/config.py#L245):

```python
HEARTBEAT_MISS_THRESHOLD = 3
```

The risk engine's kill-switch reaction only trips when misses reach
the threshold of 3, but the **alerter fires on any non-zero miss
count**. That is the regression.

### 1.4 Symptom D — anomaly "non-positive funding"

Telegram dump:

```
🚨 Supervisor anomaly summary (last 1 h)
- Open position exists while annualized funding is non-positive.
```

**Code**
[bongus/supervisor/core.py:32-41](../bongus/supervisor/core.py#L32-L41)
(approximate, per the summary):

```python
open_position_funding = [ann_funding_for(p) for p in positions]  # ALL positions, including orphans
current_ann_funding = min(open_position_funding) if open_position_funding else ...
```

and [bongus/supervisor/core.py:115-121](../bongus/supervisor/core.py#L115-L121):

```python
if (
    snapshot.open_positions > 0
    and snapshot.funding_staleness_status != "stale"
    and snapshot.ann_funding <= 0
):
    anomalies.append("Open position exists while annualized funding is non-positive.")
```

`positions = reader.get_positions()` returns *all* rows, including
`manual_review`. The orphans are long, naked perp structures on
symbols whose funding has since flipped negative. One orphan with
negative funding drags `min()` below zero → anomaly fires → user
gets alerted about something the bot cannot act on.

### 1.5 Symptom E — DUAL_SUBMISSION_FAILED retry loop

Rust emits `DUAL_SUBMISSION_FAILED`
([execution_engine/src/order_manager.rs:1915,2378,2412,2423](../execution_engine/src/order_manager.rs#L1915))
when one leg of a simultaneous spot+futures submission fails. In the
log this consistently lands on the same ~6 symbols (DENTUSDT,
ZECUSDT, ZENUSDT, 1000CATUSDT, BANKUSDT, TRBUSDT). Most likely root
cause is exchange-side: listing-only on one side, lot-size mismatch,
perpetual delisted, or Binance-1021 clock skew. We are not trying
to fix *why* those symbols fail — we are trying to stop the bot from
picking them again on the very next tick.

**Code**
[scripts/live_trader_v2.py:4626-4708](../scripts/live_trader_v2.py#L4626-L4708)
(`_handle_failed_order_update`):

- On REJECTED ENTER, pops `_pending_enters` and `_stale_pending_enters`.
- Calls `_refresh_stale_pending_flag()`.
- **Does NOT call `self._cooldowns.activate_symbol(...)`.**

Compare
[scripts/live_trader_v2.py:5156-5177](../scripts/live_trader_v2.py#L5156-L5177)
(`_on_order_rejected`), which *does* handle EXIT rejections with
retry/backoff. Entry rejections have no equivalent cooldown.

The `CooldownManager` exists
([bongus/engine/cooldown_manager.py](../bongus/engine/cooldown_manager.py))
with `activate_symbol(symbol, duration_s, reason)` and
`is_symbol_active(symbol)` — but is currently only consumed by the
correlation breaker. Wiring it into the entry-reject path is a small,
localized change.

---

## 2. Root causes

| # | Root cause | File:line |
|---|-----------|-----------|
| A | `_get_open_positions` filters `manual_review` rows out of allocator input, so `free_slots = MAX_CONCURRENT_POSITIONS - len(managed_only)` understates occupancy. Orphans sit in addition to the 4-slot budget. | [scripts/live_trader_v2.py:5840-5863](../scripts/live_trader_v2.py#L5840-L5863) + [bongus/portfolio/portfolio_allocator.py:158](../bongus/portfolio/portfolio_allocator.py#L158) |
| B | Runtime-mode debounce (60 s) is shorter than the DUAL_SUBMISSION_FAILED retry cycle (≈300 s + self-heal), so the flap survives debounce. Secondary cause: entry rejects do not cool down, which drives the flap at all. | [bongus/monitoring/telegram_alerter.py:51](../bongus/monitoring/telegram_alerter.py#L51), [scripts/live_trader_v2.py:4626-4708](../scripts/live_trader_v2.py#L4626-L4708) |
| C | Heartbeat alert is edge-triggered with no candidate debounce. A single missed ack (which does not even reach `HEARTBEAT_MISS_THRESHOLD=3` for kill-switch) is enough to fire. | [bongus/monitoring/telegram_alerter.py:495-500](../bongus/monitoring/telegram_alerter.py#L495-L500), [scripts/live_trader_v2.py:1322-1326](../scripts/live_trader_v2.py#L1322-L1326), [scripts/live_trader_v2.py:2959-2986](../scripts/live_trader_v2.py#L2959-L2986) |
| D | Supervisor `ann_funding` aggregation uses `min()` across **all** positions, including `manual_review` orphans whose funding has since flipped sign. | [bongus/supervisor/core.py:32-41](../bongus/supervisor/core.py#L32-L41) |
| E | Entry rejection handler does not activate a symbol cooldown, so the allocator re-picks the failing symbol on the very next tick. | [scripts/live_trader_v2.py:4626-4708](../scripts/live_trader_v2.py#L4626-L4708) + [bongus/engine/cooldown_manager.py](../bongus/engine/cooldown_manager.py) |

---

## 3. Acceptance criteria

A PR is acceptable iff **all** of the following hold:

1. With 3 pre-existing `manual_review` orphans + `MAX_CONCURRENT_POSITIONS = 4`,
   the allocator dispatches **at most 1** new entry (4 − 3 = 1), not 4.
   Total open positions never exceeds `MAX_CONCURRENT_POSITIONS`
   *counting orphans as occupied slots*.
2. With the DUAL_SUBMISSION_FAILED loop active on a symbol, the symbol
   is placed on a cooldown of at least 600 s on its first rejection,
   and the cooldown doubles (bounded at a configured cap) on each
   subsequent rejection within a 1 h window. The allocator does not
   pick the symbol while it is cooling down.
3. Runtime-mode Telegram alerts require the candidate mode to be
   stable for a tunable `_RUNTIME_MODE_DEBOUNCE_S` (default raised to
   ≥ 180 s) **and** the transition to reflect a real state change
   (i.e. a new reason, not just a flag toggling back after <300 s).
4. Heartbeat status Telegram alerts require the candidate status to
   be stable for at least `HEARTBEAT_MISS_THRESHOLD × HEARTBEAT_INTERVAL_SECONDS`
   (default 30 s), applied consistently with the `HEARTBEAT_MISS_THRESHOLD`
   used by the risk engine. A single-tick miss does **not** produce a
   Telegram alert.
5. The supervisor anomaly detector does not flag
   "Open position exists while annualized funding is non-positive"
   purely because of a `manual_review` orphan. The anomaly still
   fires when a *managed* position is on a negative-funding symbol.
6. On startup, the bot continues to boot with whatever positions
   exist on the exchange. **No** change here may raise, exit, or
   halt the main loop. The existing continue-on-restart guarantee
   remains intact.
7. Every change is unit-tested. No regression in existing pytest or
   `pyright` passes. `cargo build --release` (the Rust side) is
   untouched by this PR.
8. `live_config.json` is not modified. Any new tunables are added to
   `bongus/core/config.py` with safe defaults and *optionally*
   hot-reloadable via the existing `ConfigManager` allowlist, but
   not required to be overridden for the fix to work.

---

## 4. Fix plan

### 4.1 Fix A — slot budget must account for `manual_review` orphans

**Goal:** `free_slots = MAX_CONCURRENT_POSITIONS − (managed + manual_review)`,
so orphans *occupy* a slot even though they cannot be rotated or
exited safely.

**Preferred implementation** (minimal blast radius):

Change [scripts/live_trader_v2.py:5840-5863](../scripts/live_trader_v2.py#L5840-L5863)
(method `_get_open_positions`) **not** to filter out `manual_review`
rows. Instead, return **all** open positions, and attach the
`recovery_state` to each returned record so downstream code can still
distinguish managed from orphaned.

Then, at the single allocator call site in
[scripts/live_trader_v2.py](../scripts/live_trader_v2.py) (the line
that does `allocator.decide(open_positions=...)`), pass the full list.
The allocator's `free_slots` math
([bongus/portfolio/portfolio_allocator.py:158](../bongus/portfolio/portfolio_allocator.py#L158))
will then correctly reserve slots for orphans.

**Important sub-points:**

1. The allocator already uses `blocked_symbols` to stop re-entering a
   symbol that is in `startup_manual_review`. That behavior stays.
   The only change is that the *slot count* now includes the orphan.
2. Because orphans should not appear in rotation candidate lists,
   audit the allocator's rotation logic for any `open_positions`
   iteration that would treat an orphan as a candidate to *swap out*.
   If the current allocator iterates `open_positions` and considers
   each for replacement, add a `recovery_state != "manual_review"`
   guard so orphans are counted-but-not-rotated.
3. Update `_get_open_positions`'s docstring to explicitly state
   "returns all OPEN rows including manual_review; downstream
   consumers must filter by recovery_state if needed."
4. Audit every caller of `_get_open_positions` in
   [scripts/live_trader_v2.py](../scripts/live_trader_v2.py). If any
   caller previously depended on the old "managed-only" semantics
   (e.g. PnL aggregation for telemetry), give it an explicit filter
   at the call site. Do **not** silently change the value it sees.
   Expect at least the following call sites to need audit: the risk
   engine `RiskState` build, the dashboard snapshot, the heartbeat
   log line, any `len(open_positions)` metric.

**Tests to add** ([tests/test_portfolio_allocator.py](../tests/test_portfolio_allocator.py)
or a new `tests/test_slot_budget.py`):

- `test_manual_review_orphan_consumes_slot`: 3 manual-review orphans
  + `MAX_CONCURRENT_POSITIONS=4` → allocator's `free_slots == 1`.
- `test_mixed_slots_cap`: 2 managed + 2 orphans → `free_slots == 0`.
- `test_orphan_not_rotated_out`: orphan with lower funding than a
  swap candidate is **not** proposed for rotation.

### 4.2 Fix B — entry-rejection cooldown (stops the flap at the source)

**Goal:** when an ENTER order is rejected (any reason, but especially
DUAL_SUBMISSION_FAILED), the symbol is placed on a cooldown so the
allocator cannot re-pick it for N seconds. Cooldown duration grows on
repeat rejections within a window.

**Implementation:**

1. Add to [bongus/core/config.py](../bongus/core/config.py):

   ```python
   # Entry-rejection cooldown
   ENTRY_REJECT_COOLDOWN_BASE_SECONDS = 600        # first rejection: 10 min
   ENTRY_REJECT_COOLDOWN_MAX_SECONDS = 14400       # cap: 4 h
   ENTRY_REJECT_COOLDOWN_BACKOFF_WINDOW_SECONDS = 3600  # 1 h window to count recent rejects
   ENTRY_REJECT_COOLDOWN_BACKOFF_FACTOR = 2.0      # double on each repeat in window
   ```

   Leave `cooldown_enabled` gate in
   [bongus/engine/cooldown_manager.py](../bongus/engine/cooldown_manager.py)
   default-on.

2. In [scripts/live_trader_v2.py:4626-4708](../scripts/live_trader_v2.py#L4626-L4708)
   (`_handle_failed_order_update`), in the REJECTED-ENTER branch,
   after the existing `_pending_enters.pop(symbol, None)` and
   `_stale_pending_enters.pop(symbol, None)`:

   ```python
   # Compute backoff from recent rejection history
   now = time.time()
   recent = [
       t for t in self._recent_entry_rejects.get(symbol, [])
       if now - t < ENTRY_REJECT_COOLDOWN_BACKOFF_WINDOW_SECONDS
   ]
   n = len(recent)
   duration = min(
       ENTRY_REJECT_COOLDOWN_BASE_SECONDS * (ENTRY_REJECT_COOLDOWN_BACKOFF_FACTOR ** n),
       ENTRY_REJECT_COOLDOWN_MAX_SECONDS,
   )
   self._cooldowns.activate_symbol(symbol, duration, f"entry_rejected:{reason_code}")
   recent.append(now)
   self._recent_entry_rejects[symbol] = recent
   ```

   Add `self._recent_entry_rejects: dict[str, list[float]] = {}` to
   `__init__`.

3. Propagate `self._cooldowns` into the allocator call site so that
   the allocator's `blocked_symbols` param includes every symbol
   currently in cooldown. Likely this means adding, at the allocator
   call site:

   ```python
   blocked_symbols = self._compute_blocked_symbols()  # existing set
   blocked_symbols |= {
       s for s in candidate_symbols if self._cooldowns.is_symbol_active(s)
   }
   ```

   If `_compute_blocked_symbols` already exists, extend it. If not,
   add the union at the allocator call site.

4. For observability, on every cooldown activation write a log line
   `logger.warning("Entry cooldown armed for %s: %.0fs (recent=%d, reason=%s)", ...)`.
   Also surface the cooldown list in the heartbeat log string.

**Tests to add**:

- `test_entry_reject_activates_cooldown`: rejecting DENTUSDT once →
  symbol shows up in `is_symbol_active` for 600 s.
- `test_entry_reject_backoff`: 3 rejections within 1 h → cooldown
  grows to `600 * 2 * 2 = 2400` s on the 3rd.
- `test_cooldown_expiry_allows_re_entry`: after cooldown expiry,
  `blocked_symbols` no longer contains the symbol.
- `test_cooldown_capped`: 20 rejections in window → cooldown ≤
  `ENTRY_REJECT_COOLDOWN_MAX_SECONDS`.

### 4.3 Fix C — runtime-mode debounce tuned for 300 s intent lifecycle

**Goal:** the runtime-mode Telegram alert does not fire for a flap
driven by a `stale_pending_intent` flag that is toggling on and off
faster than the DUAL_SUBMISSION_FAILED + pending-timeout cycle.

**Implementation:**

1. In [bongus/monitoring/telegram_alerter.py:51](../bongus/monitoring/telegram_alerter.py#L51),
   increase `_RUNTIME_MODE_DEBOUNCE_S` from `60.0` to `180.0`. Same
   for `_SAFE_MODE_REASON_DEBOUNCE_S` if present. Rationale: the
   pending-intent lifecycle is 300 s; a 60 s debounce is simply too
   short. 180 s is the best compromise: still short enough for a
   genuine SAFE_MODE entry to alert within 3 minutes, long enough to
   absorb any single pending-intent flap that Fix B has not yet
   suppressed.

2. Additionally, add a "substantive-change" guard so that a candidate
   runtime-mode transition that reverts to a previously-alerted mode
   within `_RUNTIME_MODE_DEBOUNCE_S` is treated as no-change. The
   logic in
   [bongus/monitoring/telegram_alerter.py:432-459](../bongus/monitoring/telegram_alerter.py#L432-L459)
   (candidate debounce) already tracks the previous alerted mode;
   extend it with a `last_alerted_at` timestamp and a "minimum-dwell
   since last opposite-direction alert" of `_RUNTIME_MODE_DEBOUNCE_S`.

3. Do **not** raise the debounce any higher than 180 s. The
   supervisor's kill-switch alert runs on a 600 s throttle and is
   the fallback for real problems — leave headroom between the two.

**Tests to add** (`tests/test_telegram_alerter_debounce.py`):

- `test_runtime_mode_flap_absorbed_under_180s`: alternate
  `SAFE_MODE` ↔ `LIVE_WITH_SYMBOL_BLOCKS` every 120 s for 10 min →
  **at most one** alert.
- `test_runtime_mode_stable_change_fires`: `LIVE` → `SAFE_MODE`
  stable for 200 s → exactly one alert.
- `test_runtime_mode_reverted_within_debounce_no_alert`: mode goes
  `LIVE` → `SAFE_MODE` → `LIVE` within 170 s → no alert for the
  revert.

### 4.4 Fix D — heartbeat alert with consecutive-miss debounce

**Goal:** single-tick heartbeat noise does not fire a Telegram alert.
The alert fires iff heartbeat has been `missed` for
`HEARTBEAT_MISS_THRESHOLD` consecutive polls.

**Implementation (two options — pick 4.4.a, it is smaller):**

**4.4.a — debounce in the alerter only (preferred)**

In [bongus/monitoring/telegram_alerter.py:495-500](../bongus/monitoring/telegram_alerter.py#L495-L500):

```python
# Track consecutive observations of the same heartbeat_status.
candidate = heartbeat_status
if candidate != self._hb_candidate:
    self._hb_candidate = candidate
    self._hb_candidate_count = 1
else:
    self._hb_candidate_count += 1

# Only promote to the stable state after N consecutive observations.
stable_threshold = max(1, HEARTBEAT_MISS_THRESHOLD)
if self._hb_candidate_count < stable_threshold:
    return  # not stable yet

if candidate != prev_heartbeat_status and candidate:
    await send_telegram(session, f"💓 *HEARTBEAT STATUS*\nState: `{candidate}`")
    prev_heartbeat_status = candidate
```

Import `HEARTBEAT_MISS_THRESHOLD` from
[bongus/core/config.py:245](../bongus/core/config.py#L245). Initialize
`self._hb_candidate` and `self._hb_candidate_count` in the alerter
state struct.

**4.4.b — write `heartbeat_status` to `state.db` only once confirmed**
(more invasive; skip unless 4.4.a is rejected in review)

In [scripts/live_trader_v2.py:1322-1326](../scripts/live_trader_v2.py#L1322-L1326),
require `_heartbeat_misses >= HEARTBEAT_MISS_THRESHOLD` before writing
`"missed"`, otherwise keep writing `"ok"` until the threshold trips.
This mirrors how the kill-switch uses the threshold. The downside is
that consumers other than Telegram (e.g. dashboard) also lose the
short-lived miss signal.

**Tests to add** (`tests/test_telegram_alerter_debounce.py`):

- `test_heartbeat_single_tick_miss_no_alert`: status = `ok`, `missed`,
  `ok` in consecutive polls → no alert.
- `test_heartbeat_threshold_miss_alerts_once`: status = `missed`
  for `HEARTBEAT_MISS_THRESHOLD` consecutive polls → exactly one
  alert; the `ok` return also requires the same threshold.

### 4.5 Fix E — supervisor anomaly filters manual_review

**Goal:** the "non-positive funding" anomaly does not fire purely
because of a `manual_review` orphan's current funding.

**Implementation:**

1. In [bongus/supervisor/core.py:32-41](../bongus/supervisor/core.py#L32-L41),
   change the aggregation to exclude orphans:

   ```python
   managed = [p for p in positions if (p.recovery_state or "").lower() != "manual_review"]
   open_position_funding = [ann_funding_for(p) for p in managed]
   current_ann_funding = min(open_position_funding) if open_position_funding else None
   ```

2. At [bongus/supervisor/core.py:115-121](../bongus/supervisor/core.py#L115-L121),
   the condition becomes automatically correct because
   `snapshot.ann_funding is None` when all open positions are
   orphans. Keep the guard as `snapshot.ann_funding is not None and
   snapshot.ann_funding <= 0`.

3. Add an additional, weaker anomaly that only fires when a *managed*
   position's funding flips negative — this preserves operator
   visibility for real trouble without false positives. Use the same
   3600 s throttle already in place.

4. Audit the `snapshot.open_positions` count used in the same
   condition. If it already counts orphans, leave it — the condition
   is now "managed funding went negative OR no managed positions
   but we still have opens" (the latter is not interesting, so the
   `ann_funding is None` guard handles it).

**Tests to add** (`tests/test_supervisor_anomalies.py`):

- `test_non_positive_funding_only_from_orphan_no_anomaly`: 1
  orphan at −5 % ann, no managed positions → no anomaly.
- `test_non_positive_funding_from_managed_fires_anomaly`: 1
  managed position at −3 % ann → anomaly fires.
- `test_mixed_orphan_negative_managed_positive_no_anomaly`: 1
  orphan at −10 %, 1 managed at +30 % → no anomaly.

---

## 5. Things NOT to do

1. **Do not close the orphans programmatically.** The whole point of
   `manual_review` is that the bot cannot unwind them safely. Any
   change that auto-closes them re-introduces the naked-leg loop that
   commits `2c1cfd0`, `66799fc`, `6453b6a`, `b8d93fc`, `121beac`
   already fixed.
2. **Do not change the 300 s pending-intent timeout** in
   [bongus/core/config.py:246](../bongus/core/config.py#L246). Fix B
   stops the loop by cooling down the symbol, not by shortening the
   timeout. A shorter timeout would only make the flap faster.
3. **Do not silently exclude orphans from `_get_open_positions`'s
   return value when any other caller depends on it.** If you want
   an "orphans-only" or "managed-only" view, add an explicit
   parameter (`include_manual_review: bool = True`) and have each
   caller opt in. Default must be "all positions".
4. **Do not raise `_RUNTIME_MODE_DEBOUNCE_S` above 180 s.** Higher
   values delay real SAFE_MODE alerts past the point where the
   operator can intervene in time for an actual kill-switch event.
5. **Do not bypass `HEARTBEAT_MISS_THRESHOLD` for the kill-switch
   reaction.** Fix C changes the *alert* path only. The kill-switch
   continues to use the existing threshold.
6. **Do not disable the Telegram alerter as a workaround.** The user
   relies on these alerts for real incidents.
7. **Do not write to `live_config.json`.** It is operator-owned. Any
   new tunables go in `config.py` with safe defaults.
8. **Do not modify `state.db`.** The orphans' rows are real state;
   the bot must continue to respect them on next boot.
9. **Do not change the Rust execution engine.** All fixes are in
   Python. Rust changes require `cargo test` and a rebuild/redeploy
   cycle that is out of scope.
10. **Do not add a startup gate that refuses to run when orphans
    occupy all slots.** The user's invariant — "when I restart it,
    it should continue" — is absolute. If 4 orphans eat the full
    slot budget, the bot runs and does nothing new. That is the
    correct behavior.
11. **Do not amend `MAX_CONCURRENT_POSITIONS`.** 4 is deliberate.
    The symptom is "orphans are outside the cap", not "cap is too
    low".

---

## 6. Implementation checklist

- [ ] **Fix A**
  - [ ] Refactor
    [scripts/live_trader_v2.py:5840-5863](../scripts/live_trader_v2.py#L5840-L5863)
    `_get_open_positions` to return all OPEN positions including
    `manual_review`, attaching `recovery_state`.
  - [ ] Audit every caller of `_get_open_positions`; add explicit
    filter at the call site where a caller needs managed-only.
  - [ ] Verify allocator `free_slots` math in
    [bongus/portfolio/portfolio_allocator.py:158](../bongus/portfolio/portfolio_allocator.py#L158)
    now counts orphans.
  - [ ] Guard allocator rotation logic so orphans are not considered
    for swap-out.
  - [ ] Tests: `test_manual_review_orphan_consumes_slot`,
    `test_mixed_slots_cap`, `test_orphan_not_rotated_out`.

- [ ] **Fix B**
  - [ ] Add `ENTRY_REJECT_COOLDOWN_*` constants to
    [bongus/core/config.py](../bongus/core/config.py).
  - [ ] Initialize `self._recent_entry_rejects` in `LiveTraderV2.__init__`.
  - [ ] In
    [scripts/live_trader_v2.py:4626-4708](../scripts/live_trader_v2.py#L4626-L4708)
    `_handle_failed_order_update` ENTER branch, activate
    `self._cooldowns.activate_symbol(...)` with backoff.
  - [ ] Extend `blocked_symbols` computation at the allocator call
    site to include cooldown-active symbols.
  - [ ] Tests: `test_entry_reject_activates_cooldown`,
    `test_entry_reject_backoff`,
    `test_cooldown_expiry_allows_re_entry`,
    `test_cooldown_capped`.

- [ ] **Fix C**
  - [ ] Raise `_RUNTIME_MODE_DEBOUNCE_S` to `180.0` in
    [bongus/monitoring/telegram_alerter.py:51](../bongus/monitoring/telegram_alerter.py#L51).
  - [ ] Add minimum-dwell guard for reverted candidates in the
    candidate-debounce block at
    [bongus/monitoring/telegram_alerter.py:432-459](../bongus/monitoring/telegram_alerter.py#L432-L459).
  - [ ] Tests: `test_runtime_mode_flap_absorbed_under_180s`,
    `test_runtime_mode_stable_change_fires`,
    `test_runtime_mode_reverted_within_debounce_no_alert`.

- [ ] **Fix D**
  - [ ] Add consecutive-observation debounce to the heartbeat alert
    block near
    [bongus/monitoring/telegram_alerter.py:495-500](../bongus/monitoring/telegram_alerter.py#L495-L500).
    Threshold = `HEARTBEAT_MISS_THRESHOLD` from
    [bongus/core/config.py:245](../bongus/core/config.py#L245).
  - [ ] Initialize `_hb_candidate` / `_hb_candidate_count` in the
    alerter state.
  - [ ] Tests: `test_heartbeat_single_tick_miss_no_alert`,
    `test_heartbeat_threshold_miss_alerts_once`.

- [ ] **Fix E**
  - [ ] In
    [bongus/supervisor/core.py:32-41](../bongus/supervisor/core.py#L32-L41),
    exclude `recovery_state == "manual_review"` from
    `open_position_funding`.
  - [ ] Verify the anomaly-fire condition at
    [bongus/supervisor/core.py:115-121](../bongus/supervisor/core.py#L115-L121)
    handles `ann_funding is None` correctly.
  - [ ] Tests:
    `test_non_positive_funding_only_from_orphan_no_anomaly`,
    `test_non_positive_funding_from_managed_fires_anomaly`,
    `test_mixed_orphan_negative_managed_positive_no_anomaly`.

- [ ] **Cross-cutting**
  - [ ] Run `pytest tests/` — all green.
  - [ ] Run `pyright` — no new errors.
  - [ ] **Do not** run the Rust build unless a Python change
    surfaces a type-schema mismatch in IPC payloads (none expected).
  - [ ] Manual smoke on a dev branch: start the bot against a paper
    harness with injected orphans; confirm allocator respects slot
    budget and heartbeat/runtime-mode alerts do not fire on a
    synthetic flap.
  - [ ] Update [CLAUDE.md](../CLAUDE.md) "Recent Foundations" with
    one line summarizing the change (without listing every internal
    detail).

---

## 7. Out-of-scope (explicitly)

- **Root-causing DUAL_SUBMISSION_FAILED on specific symbols.** Whether
  DENTUSDT is delisted, ZECUSDT has a lot-size mismatch, or the
  recvWindow error is Binance-side vs clock-side — not this PR.
  Fix B is the *correct* response regardless: if a symbol cannot be
  entered cleanly, we cool it down and move on.
- **Orphan cleanup tooling.** ACTUSDT, DEGOUSDT, DYMUSDT will remain
  orphans until the operator intervenes manually. Any future orphan-
  unwind tool is a separate PR and requires its own design doc.
- **Dashboard layout** — showing orphans distinctly from managed
  positions in the UI is a nice-to-have, not blocking.
- **Rebalancing HWM across orphan MTM.** Covered by the prior plan
  [docs/fix_plan_2026-04-19_restart_telegram_spam.md](./fix_plan_2026-04-19_restart_telegram_spam.md);
  do not conflate.

---

## 8. Summary for the reviewer

Five small, independent changes. Each has an obvious failure mode
(crash the bot, block startup, disable a safety alert) that Section 5
spells out in advance. Fix A is the only one that changes behavior;
B/C/D/E are alert-hygiene and anomaly-accuracy changes. Together they
eliminate the 6-position over-slot condition, stop the SAFE_MODE /
LIVE_WITH_SYMBOL_BLOCKS flap at its source, suppress the heartbeat
alert spam, and remove the false-positive non-positive-funding
anomaly.

Nothing here changes the Rust engine, live_config.json, or state.db.
Nothing here breaks the "bot must boot with whatever positions the
exchange has" invariant.
