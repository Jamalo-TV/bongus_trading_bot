# Binance–Hyperliquid read-only research service

This service is an offline-first research boundary. It stores public responses
in a dedicated `research.db`; it does not share Bongus runtime persistence,
configuration, IPC, credentials, signing code, or process supervision.
Its standalone dependency file is `requirements-cross-venue.txt`. Public feed,
schema, replay, probe, and evaluation code remains standard-library-only; the
file pins only the PyArrow artifact backend required by the service.

PyArrow imports remain confined to `artifacts.py`. The deployed service pins
`pyarrow==23.0.1`; `deployment/Install-BongusResearch.sh` installs it from an
offline binary wheelhouse and checks both the exact version and Zstandard codec.
The unit's `ExecStartPre` then runs the collector startup check, replays any
SQLite publication backlog, and fails before public network collection if the
backend is unavailable. The service never writes JSON, CSV, or another format
with a `.parquet` suffix.

## Artifacts

- `raw_snapshots` stores immutable canonical response bytes, response headers,
  HTTP status, content SHA-256, source/capture/receive/availability/persistence
  timestamps, code/configuration hashes, and quality flags.
  Rejected HTTP responses and transport failures are retained as flagged gap
  evidence instead of disappearing from the collection history.
- `opportunity_snapshots` stores exact Decimal strings and calculates return
  using Binance collateral, Hyperliquid collateral, liquidation buffers, and
  the idle transfer buffer. Pair notional is never the return denominator.
- `research_schema_migrations` and SQLite `user_version` independently version
  the store. Triggers reject updates and deletes to evidence tables.
- Parquet evidence is partitioned by dataset, venue, UTC date, UTC hour, and
  explicit venue symbol. Each immutable file has a canonical manifest binding
  row count, byte count, time range, file/row SHA-256, code/configuration hashes,
  Zstd compression, and retention class. Publication uses a same-directory
  temporary file, file fsync, atomic replace, and parent-directory fsync where
  the operating system supports it.
- Gap rows are permanent evidence. The retention verifier classifies raw BBO
  and top-20 books as protected for at least 14 days, compact normalized data
  for at least 180 days, and metadata/funding/decisions/outcomes/manifests as
  permanent. It reports eligibility only and never deletes evidence.
- `research/experiments/binance_hyperliquid_v1.json` freezes the universe,
  complete mandatory stress matrix, deterministic daily/weekly bootstrap,
  causal rules, null hypothesis, confidence bounds, and stop/verdict rules.

## B0 historical feasibility and early abandonment

`screen_binance_hyperliquid_history.py` is an offline-only futility screen. It
accepts one canonical, content-hashed `bongus-cross-venue-b0-history-v1` JSON
artifact containing finalized funding events and independent Binance and
Hyperliquid source-manifest hashes. Each event must use the explicit v1 symbol
mapping, an exact decimal rate, its actual funding interval, a UTC epoch-
nanosecond settlement/availability pair, the venue funding-price kind, a raw
source hash, and quality flags. Hyperliquid events must remain hourly; Binance
events retain their actual supported 1/2/4/8-hour interval. The runner never
downloads history and has no credential or network option.

The B0 contract is frozen in the preregistration: at least 90 common days,
three non-overlapping 30-day windows per asset and 15 across the fixed universe,
at least 99% interval-time coverage, and zero flagged or duplicate settlements.
Insufficient or gapped data always yields `INSUFFICIENT_EVIDENCE` before any
economic conclusion. It is not evidence for abandoning or continuing.

For each complete window the report sums actual discrete Binance and
Hyperliquid settlement rates. It reports the static primary direction
(Binance long, Hyperliquid short) and an intentionally optimistic ex-post
oracle that chooses the better of that direction and its exact reverse. Basis
PnL and slippage are fixed to zero and liquidity is assumed perfect, but the
oracle still subtracts four 5bp taker commissions, a 10bp stablecoin conversion
cost, 5bp repair/failure cost, and 5% annual collateral opportunity cost on
$2,000 total reserved capital. Break-even holding periods use the same explicit
costs.

With sufficient evidence, aggregate oracle net return at or below zero yields
`ABANDON`; so does an oracle that covers costs in 25% or fewer of the 30-day
windows. Otherwise the result is `CONTINUE`, meaning only that collection may
proceed. Every report binds the input file/content, both source manifests,
preregistration, policy and code hashes, uses an immutable canonical report
hash, and always sets `grants_live_authority=false`.

## Offline verification

```powershell
python scripts/screen_binance_hyperliquid_history.py sealed-finalized-history.json --output b0-feasibility-report.json
python scripts/replay_binance_hyperliquid.py tests/fixtures/cross_venue/raw_snapshots.json
python scripts/collect_binance_hyperliquid_shadow.py --fixture tests/fixtures/cross_venue/raw_snapshots.json --database research.db --artifact-root cross_venue_dataset
python scripts/backtest_binance_hyperliquid.py tests/fixtures/cross_venue/evaluation.json --output cross_venue_report.json
python scripts/report_binance_hyperliquid.py cross_venue_report.json
python scripts/verify_cross_venue_dataset.py cross_venue_dataset --as-of-time-ns 1700000000000000000
python scripts/evaluate_binance_hyperliquid.py tests/fixtures/cross_venue/evaluation.json --output cross_venue_evidence.json
```

The collector performs no network operation unless `--allow-network` is
explicitly supplied. Even then, its concrete transport accepts only the frozen
Binance public GET paths and allowlisted Hyperliquid `/info` request bodies.
Continuous mode follows the frozen cadence contract: approximately 1-second
BBO, 1–5-second reference/funding context, normal 30-second top-20 books,
per-settlement funding-history reconciliation, and daily/on-change metadata.
The ±5-minute settlement burst remains dependent on observed settlement
calendars and is reported as incomplete until real cadence evidence proves it.

The hardened `deployment/bongus-research.service.in` unit must run as the
separate `bongus-research` user, with only the research data directory writable.
It removes inherited key variables, hides the live data directory, denies localhost and
socket binding, excludes Unix-domain IPC, and is never part of the live watchdog.

Build the deployment artifact with the dedicated cross-platform builder. It
targets Linux explicitly even when invoked on Windows, downloads the exact
CPython 3.11 PyArrow wheel only on the build host, validates its name, version,
platform tag and dependency metadata, and emits a byte-deterministic ZIP plus a
SHA-256 sidecar:

```bash
python3.11 -I scripts/build_research_release.py \
  --target linux-x86_64 \
  --output dist/bongus-research-linux-x86_64
(cd dist && sha256sum -c bongus-research-linux-x86_64.zip.sha256)
```

For a network-free rebuild, provide a previously reviewed wheel directory with
`--wheelhouse-source`. The builder requires exactly the target wheel and rejects
extra wheels. It never bundles the live configuration, environment files,
engine, IPC, state database, execution packages, fixtures, tests, caches, or
other repository content. The fixed allowlist consists only of the sanitized
research namespace and read-only adapter, nine research CLIs, this document,
the frozen preregistration, the exact requirements file, service unit,
installer, wheel, and manifest controls.

On a clean Linux host, extract as root below the required immutable releases
directory and invoke the bundled installer. Do not copy a working tree to the
host:

```bash
sudo mkdir -p /opt/bongus-research/releases/2026-08-15-v1
sudo unzip dist/bongus-research-linux-x86_64.zip \
  -d /opt/bongus-research/releases/2026-08-15-v1
cd /opt/bongus-research/releases/2026-08-15-v1
sudo bash deployment/Install-BongusResearch.sh
```

Before changing the host, the installer verifies canonical manifest bytes,
the manifest digest, exact inventory and file hashes, root ownership and
immutable permissions, host architecture, exact Python 3.11.15, and the complete
offline wheel. It creates a locked, non-login dedicated user/group only when
missing; an existing identity is accepted only if its UID, primary and sole
group, home, shell, and password-lock state match the contract exactly. It then
installs with network access disabled, proves PyArrow Zstandard support, runs
the boundary check through all nine CLIs and the collector startup check, and
runs `systemd-analyze verify` on the rendered unit. Finally it enables the unit
without starting it. Starting public collection remains an explicit operator
action after host and evidence-path review.

Every accepted collector row is first appended to `research.db`; the service
then groups up to 30 seconds of rows only within exact partition/code/config
boundaries and publishes deterministic Zstd Parquet objects. Startup verifies
existing manifests and replays the entire append-only SQLite journal
idempotently, closing a crash window between capture and artifact publication.
Failed responses additionally produce permanent `collection_gaps` artifacts.

## Germany/France region probe

Run the same fixed public REST and WebSocket probe configuration from one
Germany host and one France host for 48–72 hours. Each host writes its own
fsync-on-append, SHA-256-chained evidence file. Combine the two verified logs
without rewriting their observations, then evaluate the combined evidence:

```powershell
python scripts/probe_cross_venue_region.py --evidence germany.jsonl --allow-network --region germany --probe-host-id de-probe-01 --run-id de-2026-08 --duration-hours 60
python scripts/probe_cross_venue_region.py --evidence france.jsonl --allow-network --region france --probe-host-id fr-probe-01 --run-id fr-2026-08 --duration-hours 60
python scripts/evaluate_cross_venue_regions.py germany.jsonl france.jsonl --output region-selection.json
```

The harness records Binance and Hyperliquid REST RTT, WebSocket event age,
inter-arrival jitter, scheduled-window loss, reconnect counts, and explicit gap
recovery exercises. Aggregation uses deterministic nearest-rank p50/p95/p99.
An eligible region minimizes the maximum, across both venues, of the larger of
REST RTT p99 and WebSocket event-age p99; Germany is the fixed exact-tie
breaker. Missing duration, metrics, gap recovery, identical configuration, or
either region yields `evidence_incomplete`. The probe never uses signing,
accounts, private endpoints, live IPC, or live state.

## Interpretation

The report CLI is descriptive. It deliberately does not manufacture a
statistical verdict or grant live-trading authority. Forward evidence must meet
the preregistered duration, data-quality, confidence-bound, economic, and stress
rules before it can be called a viable research result.

The following gates cannot be satisfied by code or fixtures and remain
`evidence_required`: the 48-hour storage pilot, ≥99% scheduled and fresh decision
anchors, 100% sampled finalized-funding reconciliation, 14-day collector QA,
90 complete forward UTC days, the sealed final 30 days, and a possible extension
to 180 days. A `viable` or `strong` research verdict remains evidence only and
always encodes `grants_live_authority=false`.
