# Bongus State DB Schema

`bongus/engine/state_store.py` owns schema creation. The SQLite database is the shared contract between the live trader, dashboard, supervisor, Telegram alerts, validation, and reporting.

Schema changes should be additive and idempotent. Existing live databases must reopen safely.

## Core Tables

| Table | Purpose |
| --- | --- |
| `positions` | Open managed positions and startup recovery state. |
| `trade_history` | Closed trade records with funding, execution cost, basis PnL, mode, and session metadata. |
| `portfolio_stats` | Key-value portfolio metrics for dashboard and supervisor reads. |
| `risk_state` | Key-value runtime/risk contract. |

## Decision And Execution Tables

| Table | Purpose |
| --- | --- |
| `candidate_snapshots` | Per-cycle accepted/rejected scanner candidates and rejection reasons. |
| `opportunity_scores` | Ranked opportunity scores and component metrics. |
| `market_samples` | Market/funding samples retained for diagnostics and replay. |
| `execution_events` | Rust/Python execution events, fills, rejects, fees, and reason strings. |
| `execution_quality` | Expected vs realized execution quality samples for cost-model calibration. |
| `pending_intents` | Intent lifecycle state used by reconciliation and stale-intent recovery. |

## Governance And ML Tables

| Table | Purpose |
| --- | --- |
| `feature_snapshots` | Feature rows captured for open trades and future labels. |
| `model_shadow_decisions` | Advisory hold/exit model decisions. |
| `validation_snapshots` | Walk-forward and live-validation gate snapshots. |
| `parameter_promotions` | Promotion/rollback audit records for config overrides. |
| `health_samples` | Runtime health metrics used for monitoring and future SLOs. |

## Risk State Contract

Important keys in `risk_state`:

| Key | Shape | Notes |
| --- | --- | --- |
| `runtime_mode` | string | `LIVE`, `LIVE_WITH_SYMBOL_BLOCKS`, `SAFE_MODE`, or `BLOCKED`. |
| `safe_mode_reason` | string | Legacy comma-separated safe-mode flags. |
| `safe_mode_codes` | JSON list | Structured descriptors with `code`, `scope`, `recoverable`, `next_action`, and `description`. |
| `blocked_reason` | string | Startup/preflight block reason when runtime is `BLOCKED`. |
| `entry_block_reason` | string | Why new entries are blocked right now. Exits may still be allowed. |
| `pause_new_entries` | boolean | Operator/config pause for new entries. |
| `allow_new_risk` | boolean | Final runtime decision for new risk. |
| `runtime_ready` | boolean | Runtime mode and preflight are entry-capable. |
| `execution_bridge_healthy` | boolean | Rust IPC and heartbeat bridge are usable. |
| `telemetry_connected` | boolean | Rust telemetry stream is healthy enough for runtime decisions. |
| `risk_reasons` | JSON list | Risk-engine reasons such as drawdown, staleness, latency, or exposure. |

Consumers should prefer `safe_mode_codes` for automation and keep `safe_mode_reason` for display/backward compatibility.
