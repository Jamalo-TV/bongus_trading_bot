# Bongus Operator Runbook

This is the compact production checklist for Bongus. The reviewed Linux
systemd unit is the sole authoritative production entry point. It runs
`bongus/monitoring/king_watchdog.py`, which owns the Python trader, Rust
execution engine, and dashboard as one service cgroup.

## Start

Install the reviewed release under `/opt` and keep mutable state under
`/var/lib/bongus`. Before starting any paper or testnet soak, require Chrony to
report `Leap status: Normal`, a positive stratum, and an absolute `System time`
offset no greater than 250 ms; no greater than 100 ms is preferred. Do not
start the soak while the clock is unsynchronized or outside that bound:

```bash
timedatectl status
chronyc -n tracking
```

After that gate passes, start only the installed unit:

```bash
sudo systemctl start bongus.service
sudo systemctl is-active bongus.service
sudo systemctl status bongus.service --no-pager
```

Do not launch the watchdog, trader, Rust engine, or dashboard directly in
production, whether from tmux, screen, SSH, cron, or another process manager.
Doing so bypasses the cgroup limits and can create two traders against one
account. Direct execution is development-only. Do not deploy active-active
traders or automatic failover.

## Stop Or Restart

Use systemd so the complete control group receives the same stop lifecycle:

```bash
sudo systemctl stop bongus.service
sudo systemctl is-active bongus.service
```

For an approved restart, keep entries paused, verify no unresolved order result
or active recovery cycle exists, then use `sudo systemctl restart
bongus.service`. Do not restart for documentation-only changes and never point
the running unit at a mutable checkout.

## First Status Checks

Check these in order:

```bash
timedatectl status
chronyc -n tracking
sudo systemctl is-enabled bongus.service
sudo systemctl is-active bongus.service
systemctl show bongus.service -p MemoryCurrent -p MemoryPeak -p MemoryHigh -p MemoryMax
sudo systemctl status bongus-ops-health.service --no-pager
systemctl list-timers bongus-ops-health.timer --no-pager
systemctl list-timers bongus-backup.timer --no-pager
sudo systemctl status bongus-backup.service --no-pager
sudo journalctl -u bongus.service --since '30 minutes ago' --no-pager
```

Stop the soak and restore clock synchronization if Chrony no longer reports
`Leap status: Normal`, a positive stratum, and an absolute `System time` offset
at or below 250 ms. Treat offsets above 100 ms as a warning requiring
investigation even though the hard block is 250 ms.

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

## Clock, Backups, And Independent Monitoring

Use chrony with the host clock set to UTC. The independent health probe warns
above 100 ms absolute offset and becomes critical above 250 ms or whenever
chrony is unsynchronized. A critical clock result requires
`pause_new_entries=true`; the read-only probe deliberately cannot mutate config
or send a notification.

```bash
timedatectl status
chronyc -n tracking
sudo systemctl start bongus-ops-health.timer
sudo systemctl start bongus-backup.timer
systemctl list-timers bongus-ops-health.timer --no-pager
systemctl list-timers bongus-backup.timer --no-pager
sudo journalctl -u bongus-ops-health.service --since '30 minutes ago' --no-pager
```

The timer runs once per minute. It checks the independent runtime heartbeat and
becomes critical only after its age exceeds 125 seconds, representing two
missed one-minute windows plus scheduler tolerance. Configure a separate host
monitor to page on the failed unit; the repository does not embed credentials
or notification destinations.

The installed backup timer runs every 10 minutes, reserving a five-minute
copy/upload target inside the 15-minute RPO. That target is not proven until a
real 5.13 GB live-WAL pipeline passes the Linux timing gate. It publishes one atomic,
hash-bound generation containing `state.db`, `audit.db`, `research.db`, the
runtime configuration, and migration activation evidence. Each database and
the complete set are capped at 8 GB. The dedicated backup job independently
requires 20 GB of post-operation free headroom and caps the old-plus-staging
backup tree at 20.5 GB; it does not rely on the trader process to stop it. This
dedicated backup-identity command exercises the same complete-set path as the timer:

```bash
cd /opt/bongus/releases/REVIEWED_VERSION
sudo -u bongus-backup env BONGUS_DATA_ROOT=/var/lib/bongus \
  .venv/bin/python -m scripts.create_verified_backup_set create \
  --data-root /var/lib/bongus \
  --backup-directory /var/lib/bongus/backups \
  --rust-execution-binary /opt/bongus/releases/REVIEWED_VERSION/bin/execution_engine \
  --rust-recovery-control-socket /var/lib/bongus/runtime/rust/recovery-control.sock \
  --rust-recovery-generations-directory /var/lib/bongus/runtime/rust/recovery_generations \
  --source-budget-bytes 8000000000 --set-budget-bytes 8000000000 \
  --required-headroom-bytes 20000000000 \
  --backup-tree-budget-bytes 20500000000 --retention-count 1
```

The complete set also contains the exact immutable Rust recovery generation
(execution state, intents, telemetry plus ACK cursor, and both private-stream
cursors); mutable live runtime files are never copied. The one-minute health
timer becomes critical when either the oldest database
capture in the newest complete set or the encrypted offsite receipt exceeds
900 seconds. Its minute check validates exact manifests and sizes without
rehashing the multi-gigabyte database payloads; creation and upload perform the
deep checksum and SQLite integrity passes. Each local set triggers the packaged
no-cache Restic adapter. The root-owned offsite environment must pin the
reviewed 64-hex Restic repository config ID; every upload reads it back and
refuses to advance the receipt on a mismatch. The health gate rejects any
receipt that does not bind the coherent Rust generation and its exact member
hashes.

Perform a restore drill monthly: download one tagged snapshot into a new empty
root-owned directory, run `restic check`, deep-verify its set manifest, then run
`python -m scripts.create_verified_backup_set restore-empty ... --destination ...
--rust-execution-binary .../bin/execution_engine` and open the restored trio
through the split-store startup validator. Quarterly, repeat from
a blank Linux host using only the externally verified release, operator-held
trust pin, remote Restic credentials, and downloaded snapshot. Record snapshot
ID, manifest hash, elapsed download/verify/restore time, and final health result;
never restore over the live data root. Research uploads are daily and must not
consume the trading host's operational disk budget.

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
python scripts/verify_master_execution_plan.py
```

`BLOCKED_EVIDENCE` is an expected safety outcome when representative cycles,
settlements, calibration, soak time, or canary evidence is missing. Do not
override it by weakening thresholds.

### Read-only testnet promotion evidence

Keep `TRADING_MODE=testnet` and `pause_new_entries=true`. The account collector
has no POST, DELETE, order, cancel, or transfer API; it performs signed GETs and
compares exchange truth with the existing SQLite projection.

```bash
cd /opt/bongus/releases/REVIEWED_VERSION
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
account_artifact="/var/lib/bongus/verification_artifacts/evidence/account_reconciliation_${stamp}.json"
sudo systemd-run --wait --pipe --collect \
  --unit="bongus-account-evidence-${stamp}" \
  --property=Type=oneshot \
  --property=User=bongus \
  --property=Group=bongus \
  --property=WorkingDirectory=/opt/bongus/releases/REVIEWED_VERSION \
  --property=EnvironmentFile=/etc/bongus/trader.env \
  --property=Environment=BONGUS_DATA_ROOT=/var/lib/bongus \
  /opt/bongus/releases/REVIEWED_VERSION/.venv/bin/python \
  -m scripts.collect_testnet_account_evidence \
  --db /var/lib/bongus/state.db \
  --config /var/lib/bongus/live_config.json \
  --output "$account_artifact"
```

Configure `BONGUS_EXPECTED_ACCOUNT_UID` only after independently verifying the
dedicated testnet account identity. An exchange-only position, external order,
unassigned inventory, unavailable liability endpoint, or missing UID must keep
the reconciliation blocked. Never auto-adopt, cancel, flatten, or relabel those
facts merely to make a gate pass.

Persist a real restore-drill record. It is one source for a separately produced
schema-v1 `operations` evidence artifact; a restore drill alone cannot claim
that paging, offsite encryption, RPO, systemd, clock, disk, and soak gates all
passed:

```powershell
python backup_db.py drill <verified-manifest.json> `
  --directory verification_artifacts/evidence/restore_drills `
  --evidence-output verification_artifacts/evidence/backup_restore.json
python scripts/build_masterplan_external_evidence.py `
  --operations <complete-operations-evidence-v1.json> `
  --signed-testnet <complete-signed-testnet-campaign-v1.json>
python scripts/verify_master_execution_plan.py
```

The assembler accepts only same-directory, schema-v1, correctly labelled JSON
artifacts and records content-addressed relative references. The verifier opens
and hashes those files again. Local results, raw booleans, absolute paths, and
hash-shaped strings cannot substitute for the complete artifacts.

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

```bash
cd /opt/bongus/releases/REVIEWED_VERSION
sudo -u bongus env BONGUS_DATA_ROOT=/var/lib/bongus TRADING_MODE=testnet \
  .venv/bin/python -m scripts.collect_daily_reconciliation \
  --audit-db /var/lib/bongus/audit.db \
  --config /var/lib/bongus/live_config.json \
  --account-reconciliation /var/lib/bongus/verification_artifacts/evidence/account_reconciliation_YYYYMMDDTHHMMSSZ.json \
  --journal-dir /var/lib/bongus/verification_artifacts/daily_reconciliation_journal \
  --output-dir /var/lib/bongus/verification_artifacts/evidence
```

The daily collector also rejects either signed account timestamp when it is in
the future or more than 300 seconds old; a stale baseline cannot start a daily
chain.

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
On the installed Linux host, run it from the reviewed release with every
mutable path bound explicitly to the manifest-owned runtime directory:

```bash
cd /opt/bongus/releases/REVIEWED_VERSION
sudo -u bongus env BONGUS_DATA_ROOT=/var/lib/bongus TRADING_MODE=testnet \
  .venv/bin/python -m scripts.collect_soak_evidence \
  --db /var/lib/bongus/state.db \
  --audit-db /var/lib/bongus/audit.db \
  --config /var/lib/bongus/live_config.json \
  --account-reconciliation /var/lib/bongus/verification_artifacts/evidence/account_reconciliation_YYYYMMDDTHHMMSSZ.json \
  --journal-dir /var/lib/bongus/verification_artifacts/soak_journal \
  --output-dir /var/lib/bongus/verification_artifacts/evidence
```

Run that observation on an external scheduler no less often than the configured
15-minute maximum gap. Before every observation, an authorized operator or
credential-aware scheduler must create a new machine-attested account artifact
at the referenced runtime path; reusing an old readback does not constitute
fresh reconciliation evidence. The collector rejects either the artifact
generation time or signed snapshot observation time when it is in the future
or more than 300 seconds old. No repository timer reads trading credentials or
performs this signed account collection automatically.

The command prints the immutable bundle path. It remains a source for the
complete schema-v1 `safety_window` artifact; it cannot alone prove thirty daily
NAV closes or every required fault campaign. Once that complete artifact has
been independently assembled, bind it into the canonical manifest:

```powershell
python scripts/build_masterplan_external_evidence.py `
  --safety-window <complete-safety-window-evidence-v1.json>
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

Automatic/passive HWM decay is forbidden in production. Both decay settings
must remain disabled:

```json
{
  "hwm_auto_decay_after_hours": 0.0,
  "hwm_auto_decay_fraction": 0.0
}
```

A manual reset requires an identified operator, exchange/account
reconciliation, a recorded reason, and confirmation that it does not conceal a
real drawdown. Never use HWM decay to make an entry gate pass.

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

## Starting A Soak After A Split-Store Schema Change

A startup error that reports an older migration-manifest schema is fail-closed:
the runtime never upgrades an activated split store implicitly. Stop every
writer and preserve the complete old generation as one immutable,
checksum-recorded archive: all three databases and sidecars, the migration
manifest, runtime config, watchdog state, and the complete Rust recovery tree.
Do not edit or partially reuse that generation.

Before abandoning its local projections, collect a fresh signed, GET-only
exchange reconciliation and require it to prove the account is flat: no
futures positions, no open orders, no margin liabilities, and no unmatched
non-cash spot inventory. If it is not flat, stop and use a reviewed,
data-preserving split-store upgrade/recovery procedure.

For a flat paper/testnet soak, initialize a new empty data root with the current
reviewed release and its safe config seed. Keep `TRADING_MODE=paper` and
`pause_new_entries=true` for the first full-watchdog smoke. Never copy the old
`state.db`, `audit.db`, `research.db`, or `migration-manifest.json` into the new
root; doing so deliberately reproduces the stale-schema refusal.
