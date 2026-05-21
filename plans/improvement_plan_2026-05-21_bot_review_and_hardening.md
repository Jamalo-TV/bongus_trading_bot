# Bongus Trading Bot Review And Improvement Plan

Date: 2026-05-21

## Executive View

Bongus has strong engineering instincts and better-than-average safety thinking for a retail/systematic funding bot. The project clearly reflects real production scars: it has startup reconciliation, symbol-scoped blocks, restart supervision, telemetry awareness, and a deliberate Python/Rust split.

The biggest issue is not that the bot is naive. The issue is that too many responsibilities are coupled through one runtime state plane and one operational failure domain. When something goes wrong, strategy logic, recovery logic, database state, process orchestration, and alerting can all interact in surprising ways.

Short version:

- The trading ideas are respectable.
- The safety intent is strong.
- The operational architecture is too entangled.
- The next step is not a rewrite. It is separation of responsibilities and reduction of shared-failure surfaces.

## 1. Architecture Quality

Overall assessment: good foundation, overly coupled execution.

### What is good

- The Python/Rust split is directionally correct.
  - Python owns scanning, ranking, portfolio logic, governance, and observability.
  - Rust owns exchange connectivity and execution.
- The system has explicit recovery concepts.
  - `startup_manual_review`
  - `startup_exit_candidate`
  - `naked_leg_unwind_stuck`
  - `LIVE_WITH_SYMBOL_BLOCKS`
- The bot preserves auditability.
  - `state.db` is treated as a canonical ledger.
  - Candidate ranking, execution events, pending intents, health samples, and governance data are persisted.
- The project already has multiple control layers.
  - trader
  - risk engine
  - supervisor
  - watchdog
  - telegram alerter

### What is weak

- The runtime shape is too monolithic at the system level.
  - `scripts/live_trader_v2.py` is effectively a large coordination brain handling trading, recovery, state transitions, exchange reconciliation, risk interpretation, and runtime-mode management.
- SQLite is doing too much for too many actors.
  - trader
  - dashboard
  - supervisor
  - telegram alerter
  - watchdog maintenance
  all contend on the same file.
- The watchdog is not just a process supervisor.
  - It also performs database maintenance and stack bootstrap sequencing.
  - That makes it part orchestration layer, part janitor, part process manager.
- Operational concerns bleed into strategy runtime behavior.
  - Restart settling, stale intent handling, manual review, kill-switch semantics, telemetry freshness, and alert suppression are all intertwined.

### Architecture verdict

This is a serious prototype moving toward a platform, but it is still operating like a dense single service. I would not split it into many microservices, but I would absolutely split it into cleaner process and module boundaries.

Recommended split:

- Keep Rust execution as its own service.
- Keep one Python trader service for market selection and portfolio decisions.
- Keep supervisor/telegram/dashboard as a separate read-mostly ops service.
- Move DB maintenance out of the watchdog critical startup path.

That is the right level of separation here. Not microservices. Not one giant process. A few clean service boundaries.

## 2. Trading / Risk Logic Quality

Overall assessment: solid and realistic, with stronger operational safeguards than pure alpha sophistication.

### What is good

- The bot does not blindly trade every positive funding name.
  - It filters for hedgeability, liquidity, spread, depth, listing age, and toxicity.
- Allocation is more thoughtful than simple equal-weighting.
  - top-N selection
  - cluster caps
  - gross exposure budgeting
  - per-symbol caps
  - volatility and depth-aware sizing
- Risk logic is practical.
  - drawdown soft scaling
  - hard drawdown kill switch
  - latency gating
  - stale data gating
  - exit-first rotation behavior
- Recovery logic is unusually mature for this class of bot.
  - It tries to distinguish symbols that need review from symbols that can continue normally.

### What is weaker

- Risk semantics are spread across multiple layers.
  - `RiskEngine`
  - trader runtime mode flags
  - startup recovery flags
  - supervisor regime logic
  - watchdog restart policy
- Some “risk” actions are really operational exception handling.
  - That is fine in practice, but it makes reasoning harder.
- There is a lot of policy encoded in runtime branching rather than explicit state machines.
  - Example: recovery exit candidate, stale intent, latency hold, orphan/manual-review handling.
- The system appears stronger at not doing dumb things than at proving optimal things.
  - That is not a criticism, just an accurate maturity marker.
  - It behaves more like a hardened execution/governance framework than a highly isolated alpha engine.

### Trading / risk verdict

The bot’s core trading/risk logic is decent and production-aware. The weakest part is not alpha selection. The weakest part is policy sprawl: too many safety decisions are encoded in separate places, which raises the chance of contradictory or emergent behavior.

## 3. Biggest Technical Risks

### Risk 1: Shared SQLite as a central coordination bus

This is the largest current operational risk.

Why it matters:

- Multiple writers and maintenance jobs contend on one file.
- Startup races can crash unrelated services.
- “database is locked” becomes an infra problem that looks like a trading problem.

Observed evidence:

- watchdog prune/VACUUM interfered with trader and telegram startup
- supervisor and trader both showed `sqlite3.OperationalError: database is locked`

### Risk 2: Monolithic runtime state machine hidden in imperative branching

`live_trader_v2.py` has accumulated many intertwined responsibilities.

Why it matters:

- difficult to reason about recovery edge cases
- safe-mode behavior can emerge from branch interaction rather than intended policy
- changes are likely to produce regressions in adjacent flows

Observed evidence:

- startup recovery exit failures escalated into global `exit_failure`
- per-symbol failure semantics were not consistently preserved

### Risk 3: Too many system responsibilities in the watchdog

The watchdog should primarily supervise process health and startup order.

Why it matters:

- when it also performs DB maintenance and service orchestration, its failure modes widen
- restart behavior becomes harder to reason about
- maintenance can interfere with normal boot

### Risk 4: Policy duplication across layers

The same idea exists in multiple places:

- runtime mode
- safe mode flags
- risk engine decisions
- supervisor regime
- telegram alert suppression

Why it matters:

- conflicting interpretations
- alert spam or suppression gaps
- hard-to-debug “why did it do that?” moments

### Risk 5: Operational sidecars are not truly sidecars

Dashboard, telegram, and supervisor are tightly coupled to the same state substrate and startup timing.

Why it matters:

- observability failures can contribute to runtime instability
- support tooling can compete with the thing it is meant to observe

## 4. What I’d Refactor First

### First priority: separate operational state access from trading writes

Goal:

- Make the trader the dominant writer.
- Make the rest of the stack mostly read-only.

Actions:

- Preserve `StateWriter` ownership primarily in the trader and tightly scoped maintenance jobs.
- Convert sidecars to `StateReader` wherever possible.
- Remove startup migrations from sidecars entirely.
- Move maintenance into a dedicated low-frequency maintenance command or separate service window.

Why first:

- This reduces the most dangerous class of failure immediately.

### Second priority: extract explicit recovery state machine

Goal:

- Replace scattered recovery branching with a dedicated recovery controller.

Actions:

- Introduce a `RecoveryCoordinator` module.
- Represent lifecycle states explicitly:
  - `tracked`
  - `exit_candidate`
  - `manual_review`
  - `pending_exit`
  - `stuck_unwind`
  - `reconciled`
- Move startup reconciliation and retry/backoff policy into that component.

Why second:

- Recovery logic is where the runtime is most subtle and easiest to regress.

### Third priority: narrow the watchdog

Goal:

- Make watchdog responsible for process lifecycle, not business-adjacent maintenance.

Actions:

- Remove daily pruning/VACUUM from startup path.
- Keep only:
  - start order
  - liveness checks
  - restart policy
  - controlled shutdown
- Move DB pruning into:
  - manual admin command, or
  - maintenance worker, or
  - maintenance window logic after trader confirms idle state

### Fourth priority: unify risk decision surfaces

Goal:

- Make one component the source of truth for “can we add risk, must we reduce risk, is this symbol blocked”.

Actions:

- Define a single runtime policy object each cycle:
  - `portfolio_mode`
  - `allow_new_risk`
  - `forced_exit_symbols`
  - `blocked_symbols`
  - `operator_review_symbols`
  - `alert_reason_codes`
- Feed that object to:
  - trader
  - supervisor
  - telegram
  instead of each layer reconstructing policy independently.

### Fifth priority: shrink and partition `live_trader_v2.py`

Goal:

- Reduce file size and isolate complexity by concern.

Suggested decomposition:

- `startup_reconciliation.py`
- `recovery_coordinator.py`
- `execution_intents.py`
- `runtime_policy.py`
- `position_accounting.py`
- `allocator_cycle.py`

This is not about style. It is about reducing blast radius.

## Suggested Structural Direction

Yes, I would split it up, but only to the level that improves fault isolation.

### Recommended target structure

#### Service 1: Rust execution service

Responsibilities:

- exchange WS/REST
- order placement
- fill/update telemetry
- execution-only concerns

#### Service 2: Python trader service

Responsibilities:

- funding scan
- ranking
- allocation
- risk-policy application
- startup reconciliation
- intent creation
- canonical writes to state

#### Service 3: Ops service

Responsibilities:

- supervisor loop
- telegram commands
- dashboard API
- reporting
- read-mostly analytics

#### Service 4: Optional maintenance worker

Responsibilities:

- pruning
- checkpointing
- vacuuming
- offline repair/migrations

This is enough separation. More would likely add complexity without enough payoff.

## Improvement Roadmap

### Phase 1: Stabilize the runtime surface

Time horizon: immediate

Goals:

- eliminate startup lock races
- keep recovery failures symbol-scoped
- reduce alert storms

Tasks:

- keep watchdog DB pruning off the startup path
- audit all sidecars for accidental write paths
- ensure per-symbol recovery failure never escalates to portfolio-wide safe mode unless explicitly intended
- add regression tests around:
  - restart during recovery
  - startup with orphan/manual-review positions
  - DB lock tolerance
  - alert suppression/debounce

Success criteria:

- clean restart under active positions
- no `database is locked` during ordinary startup
- no repeated safe-mode spam from one stuck recovery symbol

### Phase 2: Extract the recovery subsystem

Time horizon: short

Goals:

- make recovery understandable
- reduce branching in trader runtime

Tasks:

- create `RecoveryCoordinator`
- move startup classification and exit retry policy into it
- formalize recovery state transitions
- log transitions as structured events

Success criteria:

- recovery behavior can be explained from one module
- test fixtures can simulate every recovery path without booting full runtime

### Phase 3: Make policy single-sourced

Time horizon: short to medium

Goals:

- unify risk and mode semantics

Tasks:

- introduce a cycle-level `RuntimePolicySnapshot`
- have trader write it once
- have supervisor/telegram read it rather than infer it independently
- normalize reason codes

Success criteria:

- the answer to “why are entries blocked?” comes from one canonical object
- fewer contradictory mode changes and alert messages

### Phase 4: Rework persistence roles

Time horizon: medium

Goals:

- reduce write contention
- improve runtime resilience

Tasks:

- formally classify tables:
  - hot write path
  - warm write path
  - read-mostly analytics
- consider splitting hot operational state from heavy historical analytics
- consider moving analytics tables away from the runtime-critical DB

Success criteria:

- trader remains stable even under dashboard/supervisor/reporting load
- maintenance tasks do not threaten trading uptime

### Phase 5: Modularize the trader runtime

Time horizon: medium

Goals:

- lower change risk
- improve maintainability

Tasks:

- carve out startup reconciliation
- carve out recovery
- carve out execution-intent lifecycle
- carve out market-scan/rank/allocate pipeline
- keep `LiveTraderV2` or canonical runtime as an orchestration shell

Success criteria:

- main runtime file becomes coordinator, not logic dump
- code review and testing become much easier

### Phase 6: Improve observability for operators

Time horizon: medium

Goals:

- make operator actions easier and safer

Tasks:

- structured reason codes instead of free-form strings everywhere
- dashboard view for:
  - blocked symbols
  - recovery state transitions
  - pending intents
  - last policy decision
- show whether a block is:
  - portfolio-wide
  - symbol-scoped
  - operational
  - exchange rejection driven

Success criteria:

- operator can diagnose most issues without reading logs

## Final Recommendation

Do not rewrite the bot.

Do not split it into many services.

Do split it into cleaner responsibility boundaries:

- one execution service
- one trader service
- one ops service
- optionally one maintenance worker

The highest-value changes are not new strategy features. They are:

1. reducing shared-state contention
2. isolating recovery logic
3. making policy single-sourced
4. shrinking the blast radius of changes

If those are done well, the bot will become calmer, easier to trust, and easier to evolve without breaking its own safety machinery.
