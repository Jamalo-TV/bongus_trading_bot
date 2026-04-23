# Fix Plan - DB Efficiency, Exit Reject Loop, Liveness Restarts, and Startup Manual Review (2026-04-23)

## 0. Scope

This plan captures the analysis findings and the implementation order for four issues:

1. Database growth toward multi-GB over multi-day runs.
2. Repeated "Exit ... failed with status REJECTED" loops for DENTUSDT.
3. Trader process restarts that appear random.
4. "ENTRY LOCKED ... symbol guard: manual review DENTUSDT" while autonomous mode is expected.

Constraint: this document defines the implementation plan only. No code changes are included here.

## 1. Goals

1. Keep runtime DB footprint stable over long runs.
2. Prevent deterministic reject loops from spamming retries and writes.
3. Avoid false liveness restarts caused by event-loop starvation.
4. Make runtime behavior and UI messaging consistent with intended autonomous policy.

## 2. Observed Root Causes

### 2.1 DB Growth Vectors

- `execution_events` is append-heavy and currently largest by bytes.
- `candidate_snapshots` grows every allocator cycle and has no retention path.
- `pending_intents` can accumulate terminal rows during repeated failures.
- `archive_old_data(...)` currently archives/deletes only `trade_history` and `execution_events`.
- Retention inputs for market and health tables are currently ignored.

### 2.2 DENTUSDT REJECTED Loop

- Exit attempts repeatedly hit hard exchange failures in the log (insufficient spot balance / futures constraints).
- Reject handling plus rapid redispatch behavior causes repeated EXIT attempts.
- Each reject cycle generates multiple synchronous DB writes and log churn.

### 2.3 Restart Cause

- Captured restart path was watchdog liveness stale, not crash backoff and not memory-limit kill.
- The watchdog restarted trader after stale loop heartbeat age crossed threshold.
- High-frequency callback work plus per-event sync writes is a plausible contributor to liveness stalls.

### 2.4 ENTRY LOCKED + Manual Review

- Current risk state combines:
  - global entry pause (`pause_new_entries=true`), and
  - per-symbol manual-review guard on DENTUSDT.
- UI text favors symbol-guard phrasing in LIVE_WITH_SYMBOL_BLOCKS mode, which can mask the global pause reason.
- DENTUSDT is classified as unsupported inverse/long-perp startup recovery and intentionally placed in manual review.

## 3. Implementation Plan

## Phase A - DB Footprint Control

1. Extend retention/archival coverage in `bongus/engine/state_store.py`:
   - Honor `market_retention_days` for `market_samples`.
   - Honor `health_retention_days` for `health_samples`.
   - Add retention policy for `candidate_snapshots`.
   - Add retention policy for terminal `pending_intents` rows.
2. Add lifecycle cleanup for terminal intents:
   - Resolve or archive intents after terminal outcomes instead of keeping forever.
3. Reduce write amplification:
   - Downsample/coalesce low-value execution updates (for example repeated NEW/CANCELED noise) before persisting.
4. Add compaction policy:
   - After retention, run WAL checkpoint and periodic VACUUM strategy.
5. Add DB growth telemetry:
   - Persist per-table row counts and approximate byte trends to risk/stats snapshot.
   - Add alert threshold for daily growth spikes.

Success criteria:
- Daily DB size growth is bounded and predictable.
- No unbounded growth in `candidate_snapshots` or `pending_intents`.

## Phase B - Exit Reject Loop Hardening

1. Classify rejects into transient vs hard failures.
2. For hard failures:
   - Disable immediate short-interval retry for the symbol.
   - Enter cooldown/backoff with explicit reason.
3. Ensure pending intent transitions are terminal for hard failures.
4. Ensure later FILLED updates reconcile safely if out-of-order events occur.
5. Add bounded retry caps and escalation to manual intervention state after cap.

Success criteria:
- Deterministic reject scenarios do not create 1 Hz redispatch loops.
- Pending intent state converges and no repeated risk-block spam for same unchanged condition.

## Phase C - Liveness Restart Stabilization

1. Reduce event-loop blocking in hot callback path:
   - Move order-update persistence to queue + worker/batch flush.
2. Add per-task liveness stamps:
   - `trading_loop`, `maintenance_loop`, and subscriber processing timestamps.
3. Improve watchdog diagnostics on restart:
   - Include last task heartbeat ages and queue backlog in reason snapshot.
4. Review watchdog stale threshold vs actual loop interval after batching improvements.

Success criteria:
- No false liveness restarts during telemetry bursts.
- If restart happens, cause is attributable from persisted diagnostics.

## Phase D - Autonomous Policy and UI Consistency

1. Confirm intended policy for unsupported inverse recovered positions:
   - Option 1: stay manual review (current behavior).
   - Option 2: autonomous unwind policy for this class.
2. If keeping manual review:
   - Keep symbol-specific block, but surface global pause reason first in UI when both are active.
3. If enabling autonomous handling:
   - Implement safe unwind path for inverse recovery class with strict guards.
4. Update dashboard blocker composition text to show both global and symbol-level blockers clearly.

Success criteria:
- Runtime behavior matches operator expectation for autonomy.
- ENTRY LOCKED message explains both global and symbol guards without ambiguity.

## 4. File Touch Map (Planned)

- `bongus/engine/state_store.py` (retention, archival, lifecycle, compaction hooks)
- `scripts/live_trader_v2.py` (reject handling, intent lifecycle, callback write path)
- `bongus/market_data/rust_data_subscriber.py` (if callback dispatch changes needed)
- `bongus/monitoring/king_watchdog.py` (diagnostics and stale decision instrumentation)
- `bongus/monitoring/web_dashboard.html` and/or `bongus/monitoring/web_dashboard.py` (blocker messaging)
- Tests under `tests/` for retention, reject behavior, liveness telemetry, and UI blocker logic

## 5. Validation Checklist

1. Long soak test (24h+) with bounded DB growth trend.
2. Forced reject scenario reproducer confirms no rapid retry loop.
3. High-event-rate simulation confirms no liveness stale restart.
4. Startup recovery scenario for unsupported inverse symbol confirms chosen policy behavior.
5. Dashboard reflects global pause and symbol guard simultaneously.

## 6. Rollout Order

1. Phase A (DB controls) first to stop storage risk.
2. Phase B (reject loop hardening) second to stop churn source.
3. Phase C (liveness stabilization) third to reduce restart instability.
4. Phase D (policy/UI consistency) fourth to align behavior and operator expectations.

## 7. Unknowns / Explicit Non-Claims

1. Exact single blocking call that produced the observed 101s liveness stall is not proven from logs alone.
2. Whether unsupported inverse recovery should be fully autonomous is a policy decision, not a purely technical default.
