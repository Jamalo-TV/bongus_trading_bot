# Ultraplan implementation and activation boundaries

This release implements the immediate safety, measurement and operational work.
It does not replace the forward observations required by `ALPHA_FREEZE.md`.
Default configuration keeps new entries paused, the decision engine in shadow,
live approval mandatory, inverse entry disabled and paid AI reporting disabled.
The isolated paper acceptance runner enables paper decisions in its own data
directory; it cannot modify this release seed or inherit exchange credentials.

## Implemented foundations

* A completed Rust hedge now retains a durable terminal-publication outbox until
  the telemetry relay has durably persisted its terminal event. Recovery replays
  pending publications with stable identity instead of losing the Python outcome
  between the execution checkpoint and telemetry persistence.
* REST acknowledgments rebase onto current execution state after asynchronous
  waits. Cancel responses reconcile executed quantities; an ambiguous submission
  followed by negative order lookups cannot authorize a second placement.
* Kill episodes survive process restarts. Release requires an explicit operator
  recovery request and fresh flat-account evidence; missing prices, manual
  inventory or dust cannot silently become proof of flatness.
* Config changes run on the trader's owned event loop. Telegram commands require
  an explicit authorized chat list. Safety schema changes are shared by Python
  and Rust, preserving the configuration-consensus barrier.
* Funding refreshes compare exchange event times before changing rates,
  settlement calendars or freshness. Old responses cannot overwrite newer data.
* Read-only carry reporting separates realized cash, inventory/MTM, settlement
  income and evidence-backed operating costs. Unknown costs remain unknown;
  observed fills do not incur a second synthetic spread/slippage deduction.
  Historical carry/rotation comparisons remain research-only.
* The paper acceptance CLI runs the actual supervised stack and binds evidence
  to source/binary hashes. It rejects process restarts, stalled loops, missing
  public-market freshness and interrupted observation, then checks shutdown and
  all three role databases. Packaged Linux and Windows development releases
  include this checker.

## Ordered next gates

1. **Measure:** reconcile every fill, fee, funding settlement, transfer, inventory
   movement and operating invoice against complete account statements. Unknown
   price PnL or missing invoices means incomplete all-cost PnL, never zero cost.
2. **Falsify carry:** preregister comparable 7- and 30-day holding/rotation
   experiments with the same capital, symbols, timestamps and executable prices.
   Separate selection history from out-of-sample observations. Preserve losing
   windows, delisted instruments, funding reversals and failed execution attempts.
3. **Execution:** collect route-specific attempt/fill, cancellation, markout and
   unhedged-notional-time measurements. Promote no route from simulated fills.
   Require at least 100 representative attempts per proposed route; do not trade
   merely to manufacture that sample. Cost bias must fit both the edge budget and
   a preregistered error tolerance (proposed: min(2 bps, 25% of net edge)).
4. **Forward evidence:** at least 90 days of frozen forward observation, including
   a sealed final 30-day evaluation, with positive lower confidence bound on
   after-cost mean returns using dependence-aware day/week blocks. Sharpe/Sortino
   are supporting diagnostics and cannot replace reconciliation or tail tests.
5. **Operations/faults:** complete testnet execution, restart, timeout, partial
   fill, stale feed, disk-pressure and reconciliation campaigns. Require 30 days
   of unattended readiness >=99.5%, no critical reconciliation failures and
   bounded resource use on the intended server. A 30-minute paper pass is an
   initial acceptance check, not this longer gate.
6. **Micro-canary:** only an independently approved, signed release/config/account
   artifact permits real orders. Choose notional from current exchange filters
   and reserved collateral, with an explicit absolute loss budget. This release
   issues no approval artifact and does not use exchange credentials in its soak.
7. **Capital:** paper -> testnet -> minimum feasible live hedge -> 100/250/500/
   1,000 USD -> larger stages only when capital, fees and margin actually permit.
   Skip infeasible small stages. Every increase needs reconciled real settlements,
   representative fills and demonstrated costs at the previous size. Pause or
   step back on reconciliation, cost, drawdown or operational gate failure.
8. **Scale or stop:** price server/data/transfer costs into the capital decision.
   Stop or pivot if executable carry after all costs lacks a positive lower
   confidence bound. No profitability claim follows from technical tests alone.

See `deployment/README.md` for Linux systemd deployment and the credential-free
acceptance command. Multi-day research, real exchange fills and capital increases
remain empirical gates; they cannot honestly be completed by a code change.
