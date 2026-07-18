# Bongus State DB Schema

`bongus/engine/state_store.py` owns schema creation. The SQLite database is the shared contract between the live trader, dashboard, supervisor, Telegram alerts, validation, and reporting.

The current application schema version is **12**. Opening the database through
`StateWriter` applies additive, idempotent migrations; read-only reporting must
not silently migrate a database.

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
| `economic_ledger_events` | Append-only Decimal economic effects keyed by immutable exchange/source identity. |
| `exchange_statement_entries` | Immutable normalized futures-income and margin-interest evidence; ledgered, match-required, or explicitly unmapped. |
| `exchange_statement_cursors` | Monotonic per-account/source history cursor used for overlapped idempotent backfill. |
| `execution_command_sequences` | Durable monotonic sequence allocation per command producer. |
| `execution_command_outbox` | Versioned command envelopes and monotonic ACK state. |
| `lifecycle_events` | Atomic/idempotent position and trade projection evidence. |
| `execution_quality` | Idempotently keyed expected/realized execution samples and causal markout components. |
| `pending_intents` | Intent lifecycle state used by reconciliation and stale-intent recovery. |
| `capital_reservations` | Durable cash, symbol-scoped spot-borrow, futures-margin, fee, gross, repair, and exit claims. Unknown exchange effects continue consuming their claims. |
| `capital_reservation_events` | Append-only reservation admission, dispatch, unknown-delivery, release, and expiry transitions. |

## Durable Recovery Guard Tables

| Table | Purpose |
| --- | --- |
| `cooldown_entries` | Absolute global/symbol entry-cooldown expiry and reason; activations and observed expiries commit immediately and survive restart. |
| `feed_cursors` | Venue/stream/symbol cursor and recovery state. Ranged depth gaps remain `GAPPED` until a fresh snapshot or explicit contiguous-range proof is persisted. |
| `feed_recovery_events` | Append-only gap, rejected-proof, recovery-proof, metadata-change, and scalar-backfill evidence. |

Cooldown and feed recovery writes use StateWriter-owned auxiliary connections
for file-backed databases so a callback cannot commit or roll back an unrelated
cycle batch. In-memory/URI stores share the authoritative connection under one
re-entrant guard lock, avoiding accidentally isolated SQLite databases.

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
| `config_hash_consensus` | boolean | Rust has applied the exact current Python canonical-config SHA-256 (paper is an explicit bypass). |
| `python_config_version_hash` | string | SHA-256 of the effective Python config snapshot. |
| `rust_config_version_hash` | string | Hash declared applied by the latest valid typed `ConfigAck`. |
| `config_sync_status` | string | `pending`, `sending`, `applied`, `rejected`, `stale_ack`, `timeout`, or an explicit failure state. |
| `private_stream_recovery_ready` | boolean | Both spot and futures private streams completed bounded order/trade replay. |
| `private_stream_status` | JSON object | Per-market replay range, counts, status, and error evidence. |
| `rust_execution_ready` | boolean | Rust completed two-venue open-order/account/internal-order reconciliation after private replay. |
| `rust_execution_readiness_status` | string | `READY`, `RECONCILING`, `BLOCKED`, `DISCONNECTED`, or explicit initialization state. |
| `telemetry_gap_detected` | boolean | Rust-to-Python broadcast overflow was observed and has not yet been cleared by fresh private replay plus two-venue reconciliation. |
| `telemetry_gap_skipped_messages` | integer | Number of messages skipped by the lagging receiver in the most recent overflow. |
| `telemetry_gap_event_time_ms` | integer/null | Rust event time for the most recent overflow marker. |
| `telemetry_gap_recovered_at` | timestamp/null | Time Python accepted fresh Rust readiness after both private streams replayed. |
| `exchange_statement_ingestion_ready` | boolean | Authoritative futures-income and margin-interest history fetched and mapped without error. |
| `risk_reasons` | JSON list | Risk-engine reasons such as drawdown, staleness, latency, or exposure. |

Consumers should prefer `safe_mode_codes` for automation and keep `safe_mode_reason` for display/backward compatibility.

## Idempotency and lineage

- `economic_ledger_events.event_key` is unique; a repeated identity with
  different content is a collision, not an update.
- `exchange_statement_entries.statement_key` is unique across venue, account,
  source, type, and exchange transaction ID. Commission and realized-PnL
  statement rows are evidence-only (`MATCH_REQUIRED`) to avoid fill double count.
- `execution_quality.sample_id` is unique when non-empty. The 60-second markout
  identity includes account, market, symbol, exchange trade ID, and horizon.
- Command envelopes include account/environment/strategy/cycle/config lineage,
  deterministic per-leg client IDs, route policy/model, TTL, sequence, command
  hash, and a hard unhedged-notional-time budget.
- A lifecycle is projected complete only after both legs are exchange-verified;
  raw execution/economic evidence remains append-only.
- Inverse entry reservations require a fresh authoritative borrowable-notional
  proof for that symbol. Missing/stale proof is zero capacity; it is never
  inferred from account equity or free quote cash.

Cost observations with unavailable fee conversion or a missing future midpoint
remain visible but are marked incomplete and excluded from calibration.
