# Fix Plan - Venue Latency Debounce and Drawdown UI Consistency (2026-04-23)

## 0. Scope

This plan addresses two systemic observation issues:
1. **Venue Latency Spam:** Frequent "venue latency too high" warnings in the Risk Engine that block entries.
2. **Drawdown Discrepancy:** Divergence between the "Supervisor Status" drawdown (e.g., 1.1%) and the "Anomaly Summary" drawdown (e.g., 4.2%).

Constraint: this document defines the implementation plan only. No code changes are included here.

## 1. Goals

1. **Stabilize Latency Risk:** Prevent transient RTT spikes or single missed heartbeats from causing "block new risk" events.
2. **Unified PnL/Drawdown View:** Ensure that the operator sees consistent drawdown numbers across all monitoring surfaces (Status, Alerts, and Dashboards).
3. **Improved Diagnostics:** Provide more context when latency is high (e.g., actual RTT vs. threshold).

## 2. Observed Root Causes

### 2.1 Latency Sensitivity
The `RiskEngine` in `bongus/engine/risk_engine.py` treats any breach of `max_latency_ms` as an immediate cause to block new entries. The latency is calculated in Python via `_heartbeat_implied_venue_latency_ms`, which incorporates RTT and "overdue" time for missed heartbeats. A single 10s heartbeat miss (due to minor network jitter) can immediately project a "latency" of 10,000ms, triggering the risk block. There is currently no smoothing or debounce for this metric.

### 2.2 Drawdown Calculation Divergence
There are three distinct drawdown calculation paths:
1. **Trader Runtime (`live_trader_v2.py`):** Uses session-start equity vs. current equity (mark-to-market). This is the source for the `RiskEngine` and `SupervisorSnapshot`.
2. **Performance Metrics (`performance_metrics.py`):** Used by the `/status` command and Dashboards. It recalculates drawdown from a historical trade list (typically limited to 5000 trades).
3. **Supervisor Anomaly Detection (`supervisor/core.py`):** Uses the `drawdown_pct` from the risk state (sourced from `live_trader_v2.py`) but may be rendered with different rounding or based on a stale risk snapshot if the DB update loop is lagging.

The discrepancy (1.1% vs 4.2%) strongly suggests that the `/status` report is using a different peak equity (likely only considering the current session's realized PnL) compared to the runtime's "all-time" peak equity or including/excluding open PnL differently.

## 3. Implementation Plan

### Phase A - Latency Debounce and Smoothing

1. **Implement Latency Smoothing:**
   - In `scripts/live_trader_v2.py`, add an EMA (Exponential Moving Average) filter for `_last_heartbeat_rtt_ms`.
   - Update `_heartbeat_implied_venue_latency_ms` to use the smoothed RTT.

2. **Add Risk Gate Debounce:**
   - Modify `bongus/engine/risk_engine.py` to require latency to be high for $N$ consecutive samples (or $X$ seconds) before blocking new risk.
   - Alternatively, add a `VENUE_LATENCY_COOLDOWN_SECONDS` in `bongus/core/config.py` to prevent rapid re-entry after a spike.

### Phase B - Unified Drawdown Reporting

1. **Standardize Peak Equity Logic:**
   - Move the peak equity tracking and drawdown calculation logic from `live_trader_v2.py` into a shared utility or the `StateReader`.
   - Ensure `performance_metrics.py` can access the runtime's tracked `peak_account_equity` from the `risk_state` table instead of re-estimating it from trade history.

2. **Align Supervisor and Trader Snapshots:**
   - Update `bongus/supervisor/core.py` to ensure it prefers the `account_equity_high_watermark` from the risk state when calculating anomalies.
   - Synchronize the use of "Mark-to-Market" vs "Realized+Open" equity across all components.

3. **UI/Narrator Refinement:**
   - Update `RuleBasedNarrator` in `bongus/supervisor/reporting.py` to display the "Soft Drawdown" threshold alongside the current value for context.

## 4. File Touch Map (Planned)

- `bongus/core/config.py`: Add `VENUE_LATENCY_SMOOTHING_FACTOR` and `VENUE_LATENCY_DEBOUNCE_S`.
- `bongus/engine/risk_engine.py`: Implement consecutive-sample gate for latency.
- `scripts/live_trader_v2.py`: Implement RTT smoothing and persist high-watermark consistently.
- `bongus/monitoring/performance_metrics.py`: Update `calculate_metrics` to use persisted peak equity.
- `bongus/supervisor/core.py`: Refine anomaly detection to match runtime calculation.
- `tests/test_risk_execution.py`: Add tests for latency debouncing.

## 5. Validation Checklist

1. **Latency Spike Test:** Simulate a single missed heartbeat and verify that "venue latency too high" is NOT immediately triggered.
2. **Persistent Latency Test:** Simulate a sustained 500ms+ RTT and verify the risk gate triggers after the debounce period.
3. **Drawdown Consistency Check:** Run the bot, generate some PnL, and verify that `/status` and the Telegram anomaly alert report the exact same drawdown percentage.
4. **HWM Persistence:** Restart the bot and verify that the `peak_account_equity` is correctly restored from the DB, preventing drawdown "resets" on restart.

## 6. Rollout Order

1. Phase B (Drawdown alignment) first to resolve operator confusion.
2. Phase A (Latency smoothing) second to reduce false-positive blocks.
