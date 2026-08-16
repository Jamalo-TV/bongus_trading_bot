# Safety-program baseline: 2026-08-15

Classification: `OPERATIONS`

This record captures the start of the execution-safety program. It is not a
live-trading approval and it does not satisfy any elapsed-time evidence gate.

## Source and configuration identity

| Item | Value |
|---|---|
| Git commit | `7ee71fadbfdfbb946aff8bfe15bbe95bdf86f7ef` |
| Git tree | `28f3934ae96fbfdc5ef52f7b4450534b9fd34312` |
| Effective engineering mode | `testnet` |
| `live_config.json` SHA-256 after lockdown | `172bed96cc72997480841e35a880c3e23324b18d2f8edd50abe7ee88308bd6bc` |
| New entries | administratively paused |
| Live approval artifact | absent |
| Baseline source archive SHA-256 | `4ecc93d242f41415c7eaa29506fa70897e54d17d9d56e1b792e817ad95f7e4a0` |

No Binance API key or secret was available in the process or local environment.
Consequently, a fresh signed account, balance, withdrawal-permission, position,
and open-order snapshot could not be collected. Historical signed evidence is
retained under `verification_artifacts/evidence`, but it is stale and is not
treated as current truth.

## Effective runtime state

- No watchdog, Python trader, Rust execution engine, dashboard, or IPC listener
  was running when the baseline was captured. A stopped runtime cannot submit an
  entry.
- The configuration loader resolves `pause_new_entries=true` for the next start.
- The current persisted projection contains zero positions and zero pending
  intents.
- The durable command outbox contains 65 terminal commands and one expired
  `CONFIG_SYNC` marked `SEND_FAILED`; the nonterminal row is not an order.
- The most recent persisted reconciliation snapshot (2026-07-26) records zero
  bot-owned open orders, zero unrelated open orders, zero exchange positions, and
  no mismatch. Its age prevents it from serving as a current signed snapshot.
- Reduce-only exit and reconciliation paths remain entry-gate independent in the
  Python and Rust safety contracts. Those paths were exercised by the baseline
  test suite; no exchange order was sent.

## Verified database snapshot

The repository backup implementation took a transactionally coherent online
snapshot and then independently checked its SQLite integrity, table counts, file
size, and content hash.

| Item | Value |
|---|---|
| Snapshot size | `5,133,869,056` bytes |
| Snapshot SHA-256 | `7dc57b14e3c2214e69339c924c8206aa6b1cad812dfd31f69bb0a498fc47f97b` |
| Integrity result | `ok` |
| Verified tables | `35` |
| Manifest SHA-256 | `bbd1a36184940318292a74093644ed407f642c40a6723d6b94db67ab60adf3c5` |

The hash-addressed files are stored in the ignored operational evidence tree at
`verification_artifacts/evidence/baseline_20260815/backups/`.

The exact tracked source at the baseline commit was also archived as
`verification_artifacts/evidence/baseline_20260815/bongus-source-7ee71fad.tar`
(6,031,360 bytes). Its SHA-256 is the source-archive hash above; this records
the release/source identity without treating the later working tree as baseline
evidence.

## Baseline validation

The mandated commands produced these results before feature implementation:

| Command | Result |
|---|---|
| `pytest tests/ -q` | 1,005 passed, 21 environment-declared skips, 11 subtests passed |
| `pyright` | 0 errors, 0 warnings |
| `cargo fmt --manifest-path execution_engine/Cargo.toml -- --check` | passed |
| `cargo test --manifest-path execution_engine/Cargo.toml --locked` | 115 passed |
| `cargo clippy --manifest-path execution_engine/Cargo.toml --all-targets` | passed |

The first canonical verifier rerun exposed one nondeterministic Windows
`PermissionError` while atomically quarantining a cleanup directory. This is a
baseline finding, not a waived failure. The implementation now performs a full
preflight and bounded identity-checked retry for transient Windows sharing
violations; its injected regression and the affected cleanup paths passed 20
consecutive focused repetitions. A clean canonical verifier rerun remains a
required post-change check.

## Baseline blockers

- Fresh signed exchange/account truth is unavailable until a testnet-only API key
  with withdrawals disabled is supplied through the deployment secret mechanism.
- The 5.13 GB state database exceeds the backup tool's 1.5 GB default operational
  budget.
- The host has about 34.8 GB free on its system drive (roughly 3.5%), far below the
  program's provisional seven-day `>30%` free-disk acceptance gate.
- Thirty consecutive reconciled UTC closes and the 90--180-day cross-venue
  forward experiment have not elapsed.
- No local test result may be promoted into those empirical gates.
