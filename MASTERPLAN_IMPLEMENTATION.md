# Autonomy and profit master plan implementation status

The repository now contains a broad local implementation spine for Phases
0–6 of [`autonomy_profit_masterplan.md`](autonomy_profit_masterplan.md). The
machine-readable verifier separates local implementation from empirical
promotion evidence. All 15 Section K failure scenarios now have complete,
passing local behavior contracts, including real Python/Rust process-boundary
campaigns. Multi-day credentialed exchange, profitability, soak, and canary
evidence does not exist yet. This is not a claim that the strategy is
profitable or eligible for more capital.

`live_config.json` keeps new entries paused, caps one symbol/trade at $2,500,
and caps gross exposure at $10,000. Dynamic leverage is disabled. No empirical
gate changes those limits automatically.

## Phase status

| Phase | Implemented locally | Current activation | Evidence still required |
|---|---|---|---|
| 0 — measurement/correctness | Append-only Decimal economic ledger; immutable futures-income/margin-interest journal and cursors; idempotent exchange identities; causal next-event replay; discrete settlement accounting; decision/order/outcome lineage; locked Python/Rust builds and CI | Ledger, statement ingestion, and instrumentation active; prior replay explicitly invalidated | Continuous paper/testnet exchange reconciliation, statement matching, and complete lineage sample |
| 1 — safety/state | Durable command outbox and ACK journal; typed canonical config sync; Rust-restart fail-closed consensus; compiled $2,500/$10,000 ceilings; TTL/sequence/hash/deterministic order IDs; cumulative per-leg oracle and Rust state; full command/ACK/exchange-event transport Cartesian campaign; before/after crash matrix for all eight lifecycle boundaries; append-only private-stream cursors; bot ownership/account reconciliation; off-actor exchange-metadata refresh with active-cycle revocation; private-fill/legging progress during signed REST waits with stale peer suppression; verified backup/corrupt-primary-and-WAL quarantine/restore; hash-checked lifecycle projection rebuild gated by exchange truth; seeded fault campaign; accepted-timeout, stream-reconnect, config-restart, and metadata-mutation process campaigns | Safety checks active; spot+futures private replay and Rust two-venue reconciliation gate readiness; dashboard/API deny by default; destructive sweeper permanently retired | Fresh credentialed exchange/account evidence |
| 2 — execution | Route optimizer and simulator; size-aware fresh capacity; Decimal exchange filters; durable 60-second markouts; hierarchical measurement-only cost calibration; perpetual-only emergency shadow routing under spot-book withdrawal; exact residual repair | Route recommendations shadow-only; actual route labelled `legacy_dual_maker`; every unpromoted route fails closed in Python and Rust; rejected exits preserve durable exposure for reconciliation | ≥100 representative cycles, calibrated route-cost holdout, injected-failure SLOs, statistically no-worse route result |
| 3 — strategy/ranking | Per-symbol settlement forecasts; exact filled-window eligibility and exchange-credit reconciliation; uncertainty-aware LCB net EV; incremental/partial rotation; direction-aware hold/exit | New ranking, rotation, and hold/exit decisions persist in shadow; expected funding cannot become realized without statement identity; legacy execution ownership remains | Purged OOS, settlements, shadow/paper decision-value lift after corrected costs |
| 4 — recovery | Durable scoped safe modes/cooldowns/incidents; interrupted-recovery restart handling; service progress/restart budgets; sequence-gap isolation; private order/trade replay with cumulative-tail reconstruction and fail-closed truncation; telemetry-overflow markers that force both cursor replays; API/time/filter classifiers; reservation-aware treasury proposals; reconciled daily reports and transition-only alerts | Symbol-local market recovery and global private-stream/telemetry-overflow recovery are active; unknown guards fail to operator review; treasury remains proposal-only | 30-day unattended paper/testnet soak, scheduled fault injection, ≥99.5% readiness, zero critical reconciliation errors |
| 5 — portfolio | Central cash/margin/gross/repair reservations with distinct symbol-scoped spot-borrow budgets; conservative covariance/factor/settlement/liquidity constraints; CVaR/stress limits; confidence/capacity sizing | Portfolio optimizer persists shadow allocations under ceilings no higher than current static limits; absent/stale borrow proof is zero; inverse selection and leverage remain inert | Shadow/paper then canary risk-adjusted lift without execution, reconciliation, or tail deterioration |
| 6 — research | Causal feature store; ridge baseline and drift checks; purged walk-forward; immutable preregistered experiments; deterministic cohorts/sequential/multiple-testing controls; read-only venue adapters; isolated strategy plugins | Offline, read-only, or shadow-only with separate strategy IDs/risk budgets | Separately approved canary budgets and fault-contained OOS evidence for each extension |

## Verification

Run the local implementation suite and generate an honest machine-readable gate
report:

```powershell
python scripts/verify_masterplan.py --run-local-checks
```

The 2026-07-18 validation run passed 669 Python tests plus 5 parametrized
subtests (2 tests skipped by their declared environment conditions), all 58 Rust tests, repository-wide Pyright,
Python bytecode compilation, and Rust formatting. Its machine-readable report
is `verification_artifacts/current.json`: local validation and protected-capital
safety are `PASS`, implementation coverage is `NOT_VERIFIED`, and every
promotion gate is `BLOCKED_EVIDENCE`. All 15 failure-matrix checks are `PASS`;
the remaining implementation verdict reflects phase-level empirical evidence,
not a missing local failure scenario.

Run the Phase 1 million-trace campaign and have the verifier consume its
evidence:

```powershell
python scripts/run_execution_fault_campaign.py `
  --traces 1000000 `
  --workers 4 `
  --output verification_artifacts/phase1_fault_campaign.json
python scripts/verify_masterplan.py
```

Generated evidence lives in ignored `verification_artifacts/`. The verifier
reports missing elapsed-time, exchange, settlement, calibration, or canary data
as `BLOCKED_EVIDENCE`; it never converts unit-test success into trading evidence.
Its local implementation verdict remains `NOT_VERIFIED` while phase contracts
that require empirical runtime proof are partial, even when every local failure
scenario passes.

The local campaign run on 2026-07-18 completed 1,000,000 traces with seed
`20260718`, including 333,336 duplicate deliveries, 111,112 dropped deliveries,
1,181,465 stale deliveries, 1,000,000 crash/restores, and 58,824 cancel/fill
ambiguities. It reported zero duplicate exchange effects and zero invariant
failures. A signed GET-only testnet readback and a real isolated restore drill
are now hash-addressed by `verification_artifacts/masterplan_external_evidence.json`.
Five of the six Phase 1 gate metrics pass; promotion remains blocked because
the exchange has one futures position without local ownership lineage. The
same readback also records unassigned spot inventory, unavailable demo-margin
endpoints, and missing dedicated-account UID configuration rather than mutating
them away. It normalized and content-hashed 485 historical funding statements
across 37 symbols, but they are not treated as canary proof because account
identity/ownership, closed-cycle lineage, daily reconciliation, and elapsed
soak requirements remain unsatisfied. Fresh resolved exchange/account,
representative-cycle, settlement, calibration, soak, and canary gates remain
blocked.

Phase 4 now has a resumable, append-only soak journal and all four required
hash-addressed evidence views (unattended soak, fault injection, incident log,
and readiness report). The first real observation at 2026-07-18T19:01:57Z is
honestly red: 0.0 consecutive days, 0% decision-service readiness, 11 distinct
blocking reconciliation/invariant incidents, 15 unresolved alert reasons, and
no injected-gap or routine-recovery denominator. Missing denominators remain
`null`, never implicit success. Every append revalidates strict sequence,
monotonic UTC time, previous-record links, filenames, and record hashes; a
single observation cannot create historical soak time.

Phase 0 now also has a hash-addressed `clean_ci`, `causal_replay`, and
`runtime_reconciliation` evidence bundle. Two independent replays of
1,052,560 point-in-time market rows produce identical replay/trade
fingerprints and the complete local verifier is green. The real retained
runtime sample is not green: all 7 exchange trade updates lack complete
decision/order/fill lineage, none of those fills or the 485 authenticated
funding statements maps into the currently empty local economic ledger, and
there is no attested daily exchange-precision reconciliation. The manifest
therefore reports Phase 0 as attested but blocked rather than interpreting
empty journals as perfect coverage.

An authenticated daily-reconciliation baseline is also recorded. Account
artifacts now expose exact combined spot/futures/margin wallet balances,
perpetual positions, valuation prices, precision tolerances, endpoint
completeness, and dedicated-account identity status without exposing
credentials. The append-only daily chain compares each later exchange delta
with economic-ledger balance and position deltas over the exact intervening
window. The first record has zero intervals and cannot count as a successful
day; the current baseline remains identity-unverified and the demo margin
endpoints remain unknown.

## Promotion boundary

The following remain blocked until their predeclared gates are satisfied and
reviewed independently:

- switching production ranking from legacy to LCB net EV;
- activating any route other than `legacy_dual_maker`;
- allowing treasury transfers;
- increasing size, gross exposure, capital, or leverage;
- placing orders on a second venue;
- promoting an ML model or strategy plugin from shadow.

Safety exits and reconciliation can reduce risk even while new entries are
paused. No model, optimizer, experiment, report, or recovery recipe can raise a
capital ceiling by itself.
