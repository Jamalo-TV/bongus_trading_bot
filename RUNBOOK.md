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
tail -n 200 scripts/logs/king_watchdog.log
python3 check_db.py
```

Dashboard state comes from SQLite. The important risk keys are `runtime_mode`, `safe_mode_reason`, `safe_mode_codes`, `entry_block_reason`, `pause_new_entries`, `runtime_ready`, `execution_bridge_healthy`, and `telemetry_connected`.

## Safe Modes

Use `safe_mode_codes` when available. Each code has:

- `scope`: `global` blocks the portfolio; `symbol` blocks only affected symbols.
- `recoverable`: `true` means retry/reconcile paths may clear it.
- `next_action`: the intended runbook action.

Common actions:

- `retry_exchange_audit`: wait one audit interval or restart if telemetry is wedged.
- `restore_rust_ipc`: verify Rust is running and ports `5555` and `9000` are healthy.
- `reconcile_intent_terminal_state`: compare pending intent state against Binance orders/fills.
- `reconcile_or_exit_symbol`: inspect spot/perp quantities and allow a controlled symbol exit if needed.
- `operator_acknowledge_or_flatten`: manually confirm the orphan/mismatch, then acknowledge or flatten.
- `wait_or_derisk`: keep new entries blocked until drawdown/risk limits release, or flatten deliberately.

## Pause And Recovery

`pause_new_entries` blocks new entries. It should not be treated as a blanket ban on recovery exits, flatten requests, or reconciliation. Before unpausing, confirm:

- `runtime_ready` is true.
- `execution_bridge_healthy` is true.
- `telemetry_connected` is true.
- `entry_block_reason` is empty.
- No global `safe_mode_codes` require operator review.

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
