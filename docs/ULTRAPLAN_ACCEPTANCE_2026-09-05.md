# Ultraplan: initial Linux operational acceptance

**PASS — paper only.** The supervised runtime completed 1,807.585 seconds
(30 minutes 7.585 seconds) of continuously healthy operation on 2026-09-05.
This is an initial technical acceptance, not a profitability or live-trading
approval. The longer gates in `ULTRAPLAN_IMPLEMENTATION.md` remain mandatory.

## Immutable candidate and environment

- Runtime source commit: `a9ae82f9fc423efe8adc3ac0427db39c67079add`.
- Ubuntu 24.04 x86_64 in an isolated WSL2 installation; CPython 3.11.15,
  native Rust 1.94.1 release executable. This was not a remote VPS test.
- Native Rust SHA-256:
  `9913c439a371ad52047c92a090362ee239943626626ebe27080aa0389f339b38`.
- Offline-installable development ZIP SHA-256:
  `feecf38872b851c397066c297c0516bbee47e4da05389532a9c3227497d16f1b`.
- The actual Linux installer completed offline dependency installation and
  systemd unit rendering/verification. Installed Python runtime: 388,017,056
  bytes, below its 600 MB budget.
- The acceptance runner used an unprivileged `bongus` systemd service with
  `NoNewPrivileges=yes`, `ProtectSystem=strict`, `ProtectHome=yes`, private
  temporary/device namespaces, `UMask=0027`, a 3.5 GB hard memory limit,
  no swap and 512-task limit. Its output parent was explicitly writable.
- Python and Rust dotenv loading were disabled. The child environment excluded
  exchange, Telegram and AI credentials. Public market data was used.
- The isolated paper configuration used simulated equity of USD 5,000,
  USD 500 per-symbol notional and USD 1,000 gross exposure limit. Initial
  monitored symbols were BTCUSDT and ETHUSDT. This was not a USD 100 live test.

## Observed result

| Check | Result |
|---|---|
| First fully ready sample | 2026-09-05 17:19:44.852703 UTC |
| Final healthy sample | 2026-09-05 17:49:52.434701 UTC |
| Continuous healthy duration | 1,807.585 seconds |
| Accepted observations | 362, normally five seconds apart |
| Supervised process replacements | 0 during the accepted window |
| Fresh funding symbols | At least 526 in every accepted sample |
| Maximum sampled pending critical receipts | 5; no receipt-age gate failure |
| Pending receipts/publications after shutdown | 0 / 0 |
| Graceful shutdown | Passed; no forced-child-stop evidence |
| SQLite integrity | `ok` for state.db, audit.db and research.db |
| Runtime source/binary changes during soak | 0 |
| cgroup memory peak | 1,676,902,400 bytes, including cache and observer |
| Sum of child RSS, sampled peak | 737,353,728 bytes; shared pages may be counted twice |
| Paper trades / economic ledger events | 0 / 0 |
| Persisted positions at end | 0 |
| Persisted opportunity-funnel events | 60 |

The highest observed trading-loop heartbeat age was 1.2 seconds. Retention
reached 59.9 seconds, within its deadline. The runtime was stopped cleanly at
17:49:52.868153 UTC. No trades were forced to manufacture an execution sample.

## Technical verification

- Full Linux Python suite: **1,402 passed, 6 skipped, 13 subtests passed** on
  `6241b52`. The only subsequent runtime change before the soak was the
  platform guard in `storage_guard.py`; its 63 Linux tests passed, with one
  Windows-specific skip, and all 64 passed on Windows.
- Final Python type checks: zero errors on Linux and Windows.
- Native Rust: **194 Linux tests**, **193 Windows tests**, formatting and
  all-target/all-feature Clippy with warnings treated as errors passed.
- Separate research release: sanitized build, fresh offline PyArrow 23.0.1
  installation, Zstd, nine CLI help checks and isolated collector startup
  passed. These are technical checks, not historical or live return evidence.
- Linux suite skips comprise two optional uncompiled Cython checks and four
  checks requiring Windows PowerShell or Windows sharing semantics.

The review also verified compatibility with the preceding split-store
activation identity. Publication bookkeeping uses a reserved `schema_meta`
namespace; deployed table routing, historical rows and migration manifests
are preserved. Fault tests cover corrupt metadata and atomic rollback.

## Evidence and limits

Local raw evidence is retained under
`data/ultraplan-acceptance-2026-09-05/`; it is deliberately not committed as
trading history. `summary.json`, the raw JSONL, report, console, test results
and effective systemd properties are included there.

- Paper report SHA-256:
  `a02fae6ae140a16144c5fb9d27166033963c8b9d6956f766b48d952066b3030c`.
- Raw sample JSONL SHA-256:
  `789e2e042fbd23e2caa24e5206bf9af83a58e1ae0f430b441ba826686f0085d0`.

Earlier interrupted or failed trial windows were excluded entirely. Tests
were stopped before this accepted runtime started, so synthetic test intents
could not reach its IPC sockets.

This run provides no actual fills, funding settlements, account statement
reconciliation, loss distribution or profit estimate. Testnet faults, real
execution costs, encrypted offsite backup/paging, multi-day resource behavior
on the intended VPS and the 30-/90-day operational/economic gates remain to
be demonstrated. Dynamic subscription growth and deferred events during slow
REST calls deserve attention in the longer resource soak; this short run
does not establish their worst-case bounds. Live trading remains gated.

See `deployment/README.md` for installation and the paper acceptance command.
