# Bongus Operator Runbook

This is the compact production checklist for Bongus. The current supervised runtime is `bongus/monitoring/king_watchdog.py`, which starts `scripts/live_trader_v2.py`, the Rust execution engine, and the dashboard.

## Start

Always run Bongus inside tmux.

```bash
tmux new -s bongus
cd /mnt/data/bongus_trading_bot
python3 bongus/monitoring/king_watchdog.py
```

If the `bongus` session already exists:

```bash
tmux attach -t bongus
```

## Stop Or Restart

Use the tmux session. Prefer a clean keyboard interrupt to stop the watchdog and child processes, then restart from the same session.

```bash
tmux attach -t bongus
```

After code changes that affect runtime behavior, restart only from inside tmux. Do not restart for documentation-only changes.

## First Status Checks

Check these in order:

```bash
tmux ls
tail -n 200 scripts/logs/live_trader.log
python3 check_db.py
```

Dashboard state comes from SQLite. The important risk keys are `runtime_mode`, `safe_mode_reason`, `safe_mode_codes`, `entry_block_reason`, `pause_new_entries`, `runtime_ready`, `execution_bridge_healthy`, `telemetry_connected`, `telemetry_gap_detected`, `config_hash_consensus`, `rust_config_version_hash`, `private_stream_recovery_ready`, `rust_execution_ready`, and `exchange_statement_ingestion_ready`.

The aggregate heartbeat is not sufficient on its own. After startup grace the
watchdog requires fresh independent progress for liveness publication,
maintenance/reconciliation, execution-event writing, and trading decisions.
A missing progress map is treated as stalled, not healthy. Port collisions open
a durable retry circuit; after the cooldown, only one bounded probe is allowed.

## Dashboard Access

The dashboard binds to `127.0.0.1` by default and denies all HTTP and
WebSocket access until credentials are complete. Configure either a read-only
viewer or the admin account before starting the stack:

```bash
export BONGUS_VIEWER_USERNAME=bongus-viewer
export BONGUS_VIEWER_PASSWORD_SHA256=<sha256-of-a-long-random-password>
export BONGUS_ADMIN_USERNAME=bongus-admin
export BONGUS_ADMIN_PASSWORD_SHA256=<different-sha256>
```

Admin credentials can also read viewer routes. Do not expose port `8080`
directly; use an authenticated reverse proxy if remote access is required.

## Logs And Support Bundles

The watchdog writes the combined Python, Rust, and supervisor stream to
`scripts/logs/live_trader.log`. It rotates the active file at 2 MiB and keeps
five backups by default. On a full watchdog start, files from the prior session
are moved into a timestamped directory under `scripts/logs/archive/`; durable
execution journals are copied there but left live for safe restart recovery.
The newest 10 startup archives are retained.

Open `/logs` and select **Download all logs** to download the current files,
rotated files, retained startup archives, execution journals, private-stream
cursors, and an inventory manifest as a ZIP file.

These environment variables adjust bounded retention when needed:

```bash
export BONGUS_LOG_MAX_BYTES=2097152
export BONGUS_LOG_BACKUP_COUNT=5
export BONGUS_STARTUP_ARCHIVE_COUNT=10
```

Do not manually clear `execution_engine/execution_state.jsonl` or
`execution_engine/execution_intents.jsonl`. They are recovery journals, not
ordinary logs.

## Safe Modes

Use `safe_mode_codes` when available. Each code has:

- `scope`: `global` blocks the portfolio; `symbol` blocks only affected symbols.
- `recoverable`: `true` means retry/reconcile paths may clear it.
- `next_action`: the intended runbook action.

Safe-mode codes and exact `symbol_block_reasons:<SYMBOL>` records are restored
from SQLite before decisions can run. Unknown future codes are retained as
global, non-recoverable operator-review guards. Never clear the database row to
recover: complete the descriptor's `next_action` and let the owning recovery
path explicitly clear it.

Supervisor incidents reopen in their prior durable state. A process crash while
a recipe owns `RECOVERING` records `ATTEMPT_INTERRUPTED`; the already-consumed
attempt returns to `WAITING`, or to `EXHAUSTED` when no retry remains. Only an
`ACK_REQUIRED` incident with recorded invariant proof accepts an identified
operator acknowledgement.

Common actions:

- `retry_exchange_audit`: wait one audit interval or restart if telemetry is wedged.
- `restore_rust_ipc`: verify Rust is running and ports `5555` and `9000` are healthy.
- `replay_private_order_and_trade_history`: keep entries paused while both
  private streams reconnect; inspect `private_stream_status` for a truncated
  page, stale/corrupt cursor, signed REST error, or a gap beyond retention.
- `telemetry_gap_detected=true`: leave observation/reconciliation running and
  entries paused. Rust disconnects the lagging client and requests spot and
  futures private replay automatically. It clears only after both streams are
  structurally `READY` and Rust emits a fresh reconciled `ExecutionReadiness`.
- `reconcile_spot_and_futures_execution_state`: inspect
  `rust_execution_readiness_reason`; Rust retries on the position-audit
  interval while both private streams remain backfilled.
- `active_cycle_exchange_metadata_changed`: Rust detected a filter/status
  change while an order cycle existed. The chase is retained; do not unpause
  entries until the next account/open-order reconciliation emits `READY`.
- Slow signed REST does not suspend fill handling. If an incident shows
  `SUBMITTING`, leave reconciliation running and inspect private order updates;
  `NOT_SUBMITTED` means a stale planned leg was deliberately suppressed after
  authoritative progress and must not be manually replayed.
- During flash volatility, compressed available margin below
  `minimum_liquidation_buffer_usd` trips the kill switch. A withdrawn spot book
  does not hide the shadow perpetual reduce-only route when the perpetual book
  remains executable. If that close is rejected, the position must remain in
  SQLite as `manual_review`; `min_notional`, precision, or reduce-only rejection
  is never proof of flatness.
- `reconcile_intent_terminal_state`: compare pending intent state against Binance orders/fills.
- `reconcile_or_exit_symbol`: inspect spot/perp quantities and allow a controlled symbol exit if needed.
- `operator_acknowledge_or_flatten`: manually confirm the orphan/mismatch, then acknowledge or flatten.
- `wait_or_derisk`: keep new entries blocked until drawdown/risk limits release, or flatten deliberately.

## Pause And Recovery

`pause_new_entries` blocks new entries. It should not be treated as a blanket ban on recovery exits, flatten requests, or reconciliation. Before unpausing, confirm:

- `runtime_ready` is true.
- `execution_bridge_healthy` is true.
- `telemetry_connected` is true.
- `config_hash_consensus` is true and the Python/Rust hashes match.
- `private_stream_recovery_ready` is true with both `spot` and `perp` present.
- `rust_execution_ready` is true after both open-order and account snapshots reconcile.
- `exchange_statement_ingestion_ready` is true with no unmapped statement types.
- `entry_block_reason` is empty.
- No global `safe_mode_codes` require operator review.

Autonomous startup recovery never clears `pause_new_entries`; only an explicit
operator/supervisor action can remove that pause.

## Database Corruption Recovery

Stop every writer and verify the selected manifest first. Normal replacement
requires both `--replace` and `--confirm-quiesced`. Use
`--quarantine-corrupt-target` only when SQLite proves the primary or WAL is
structurally corrupt; it preserves the damaged files under
`corrupt_quarantine/` and cannot override a readable database held by an active
writer.

After restoring, rebuild logical `positions` and `trade_history` through
`StateWriter.rebuild_lifecycle_projections(...)` with a fresh authoritative
exchange-position snapshot. Replay verifies every lifecycle content hash and
commits only when the journal's final symbol/side/direction/quantity/hedge ratio
exactly matches exchange truth. A mismatch means exchange history must be
ingested first; do not force the projection.

Before any route/model/portfolio/capital promotion, run:

```bash
python scripts/verify_masterplan.py --run-local-checks
```

`BLOCKED_EVIDENCE` is an expected safety outcome when representative cycles,
settlements, calibration, soak time, or canary evidence is missing. Do not
override it by weakening thresholds.

### Read-only testnet promotion evidence

Keep `TRADING_MODE=testnet` and `pause_new_entries=true`. The account collector
has no POST, DELETE, order, cancel, or transfer API; it performs signed GETs and
compares exchange truth with the existing SQLite projection.

```powershell
$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
python scripts/collect_testnet_account_evidence.py `
  --output "verification_artifacts/evidence/account_reconciliation_$stamp.json"
```

Configure `BONGUS_EXPECTED_ACCOUNT_UID` only after independently verifying the
dedicated testnet account identity. An exchange-only position, external order,
unassigned inventory, unavailable liability endpoint, or missing UID must keep
the reconciliation blocked. Never auto-adopt, cancel, flatten, or relabel those
facts merely to make a gate pass.

Persist a real restore-drill record and assemble the Phase 1 evidence manifest:

```powershell
python backup_db.py drill <verified-manifest.json> `
  --directory verification_artifacts/evidence/restore_drills `
  --evidence-output verification_artifacts/evidence/backup_restore.json
python scripts/build_masterplan_external_evidence.py `
  --account-reconciliation <account-artifact.json> `
  --backup-restore verification_artifacts/evidence/backup_restore.json
python scripts/verify_masterplan.py --run-local-checks
```

The assembler hashes every referenced artifact and records failed metrics as
observed. `attested=true` means the readbacks are authentic; it does not mean
the promotion criteria passed.

Generate the Phase 0 measurement bundle from a completed full verifier run,
the authenticated account readback, the read-only state database, and two
independent causal replays over the checked-in market data:

```powershell
python scripts/collect_phase0_evidence.py `
  --clean-verifier verification_artifacts/current.json `
  --account-reconciliation <fresh-account-artifact.json>
```

An empty decision, fill, ledger, statement, or daily-reconciliation sample is
never 100%. Legacy exchange fills remain in the lineage/mapping denominator.
When daily reconciliation is operational, pass its immutable artifact with
`--daily-reconciliation`; otherwise the daily unexplained value remains
`null` and precision remains false. Add the resulting bundle to the assembler
with `--phase0-evidence <phase0-bundle.json>`.

After every fresh account readback, append its balance/position snapshot to the
daily reconciliation chain:

```powershell
python scripts/collect_daily_reconciliation.py `
  --account-reconciliation <fresh-account-artifact.json>
```

The first observation is baseline-only and never counts as a reconciled day.
Later observations compare authenticated combined wallet and perpetual-position
deltas against economic-ledger events in the exact intervening window. Missing
account identity, incomplete endpoints, unvalued differences, or differences
above the exchange precision keep that interval blocked. Feed the newest daily
bundle into the next Phase 0 collection with `--daily-reconciliation`.

Start or resume the Phase 4 soak journal with a fresh account readback. Each
invocation appends exactly one observation. The collector verifies the entire
sequence/previous-hash/content-hash chain before appending, rejects live mode
or raised capital ceilings, and derives elapsed days from observation times.
Run it on a scheduler no less often than the configured 15-minute maximum gap:

```powershell
python scripts/collect_soak_evidence.py `
  --account-reconciliation <fresh-account-artifact.json>
```

The command prints the immutable bundle path. Feed that exact path back into
the assembler:

```powershell
python scripts/build_masterplan_external_evidence.py `
  --account-reconciliation <fresh-account-artifact.json> `
  --backup-restore <backup-restore-artifact.json> `
  --soak-evidence <soak-bundle-artifact.json>
```

Do not delete a failing journal observation, backdate a sample, increase the
gap tolerance after an outage, or interpret a missing fault/recovery
denominator as 100%. A new journal is a new soak campaign and starts at zero
elapsed days.

## Flatten

Use the dashboard, supervisor, or Telegram command path that writes the shared state/config contract. After requesting flatten, watch pending exits and execution events until every intended symbol reaches a terminal state.

Do not clear pending intents manually unless Binance reconciliation confirms the order/fill terminal state.

## HWM Recovery

For a manual drawdown HWM reset, use the one-cycle config key:

```json
{
  "reset_equity_high_watermark": true
}
```

For passive recovery, use:

```json
{
  "hwm_auto_decay_after_hours": 72.0,
  "hwm_auto_decay_fraction": 1.0
}
```

Keep these values conservative in live mode and record the reason in operator notes.

## Config

Runtime config is hot-loaded from `live_config.json`. The generated reference is `CONFIG.md`.

Regenerate it after config-key changes:

```bash
python3 scripts/generate_config_reference.py
```

High-impact keys to check when the bot is idle:

- `pause_new_entries`
- `autonomous_startup_recovery`
- `allow_autonomous_inverse_liquidation`
- `max_gross_exposure_usd`
- `entry_ann_funding_threshold`
- `min_expected_edge_bps`
- `rotation_max_payback_days`

## Dangerous Legacy Scripts

Root-level scripts named `fix_*`, `force_exit*`, `manual_exit*`, `clear_intents.py`, `update_db.py`, and one-off `check_*` files should be treated as operator tools, not production runtime. Read the file and verify the target database/exchange mode before running any of them.
