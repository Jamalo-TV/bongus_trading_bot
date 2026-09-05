# Offline carry economics and evidence reports

`bongus/research/carry_economics.py` and `scripts/report_carry_economics.py`
measure supplied evidence and explicit sensitivities. They do not import an
exchange client, load credentials, change runtime configuration, authorize a
route, or increase capital. `live_activation_authorized` and
`profitability_established` are always false. Technical tests of this module do
not establish a trading edge.

## Run and preserve evidence

```powershell
.venv/Scripts/python.exe scripts/report_carry_economics.py --input inputs.json
.venv/Scripts/python.exe scripts/report_carry_economics.py --input inputs.json --input-sha256 REVIEWED_SHA256 --output new-report.json
.venv/Scripts/python.exe scripts/report_carry_economics.py --input close-inputs.json --ledger-db data/trading_state.db
```

The input is an explicit UTF-8 JSON object. Monetary and rate fields should be
decimal strings; rates are fractions (`"0.0001"` = 1 basis point). Timestamps
must include a timezone. The actual-mode database must already exist; the CLI
opens it read-only and uses a read transaction, including committed WAL data.
It never migrates the database. Output defaults to stdout. `--output` creates
a new file exclusively and refuses to overwrite any existing artifact.

Input, ledger, comparison and report SHA-256 hashes make a calculation
content-addressed. They do **not** prove exchange completeness, authenticity,
an honest policy freeze date, or independent review. An evidence reviewer must
pin the exact hash externally. Files are not write-protected against other
programs; a subsequent modification invalidates the recorded hash.

## Three distinct views

1. **Actual accounting** uses the existing economic ledger and NAV identity.
   Realized cash is consolidated spot/perpetual realized price PnL + signed
   funding - commissions - borrow interest. Deposits, withdrawals and internal
   transfers are not income. Internal transfers must net to zero for a
   finalized consolidated NAV. MTM adds unrealized PnL change and stablecoin FX
   separately. Costs already embedded in executed prices, such as spread,
   slippage, basis and legging outcomes, are not deducted again. Markout is a
   diagnostic rather than another expense.
2. **Historical/shadow comparisons** replay supplied fixed-quantity hedge
   cycles using settlement times, signed rates and settlement mark prices.
   They are not actual ledger earnings and do not estimate live fill behavior.
   Positive spot quantity means long spot/short perpetual; negative quantity
   is the inverse direction and requires its borrowing costs explicitly.
3. **Unit economics** shows assumed annual net trading edge on reserved equity
   and explicit OPEX. It does not infer that edge from a snapshot, backtest or
   an annualized display rate. Drawdown, loss-month and ruin probabilities stay
   unknown because these inputs do not contain a return distribution.

## Operating-cost contract

Every complete view must cover `server`, `data`, `transfer` and
`other_operations`, including an explicit, evidenced zero when applicable.
Each item has a unique `source_id`, `category`, `amount_usd`, and `basis`:
`actual`, `accrued`, `assumption` or `unknown`. Missing amounts/categories stay
unknown. An example structure, **not a cost estimate**, is:

```json
[
  {"source_id":"invoice-1", "category":"server", "amount_usd":null, "basis":"unknown"},
  {"source_id":"data-check-1", "category":"data", "amount_usd":"0", "basis":"actual"},
  {"source_id":"transfer-check-1", "category":"transfer", "amount_usd":"0", "basis":"actual"},
  {"source_id":"operations-check-1", "category":"other_operations", "amount_usd":null, "basis":"unknown"}
]
```

`actual` is an input attestation, not independently verified by this tool.
Allocate invoices over the exact reporting period and account scope; retain
the invoices/allocation method. Do not count the entire annual invoice in
each monthly report. Set `included_in_exchange_pnl: true` only when the same
cost's `source_id` is a commission/borrow ledger event key already deducted
in that close; the report then avoids a second deduction. Charges outside
the exchange account are incremental OPEX. Funding, fee, principal-transfer
and execution-cost inputs must not overlap.

## Actual-mode JSON

Supply `mode: "actual"`, `account_id`, `trading_mode`, `start_time`, `end_time`,
`ledger_reconciled` (boolean), `average_reserved_capital_usd`,
`operating_costs`, and `nav_inputs`. The database is selected by `--ledger-db`.
The report scope is **[start, end)**, so adjacent reporting periods do not
count their boundary event twice. The stated trading mode remains visible;
paper/testnet accounting must never be represented as live profit.

`nav_inputs` accepts the existing `calculate_daily_nav_close` fields:

- `opening_nav_usd`, `closing_nav_usd`, `tolerance_usd`;
- `realized_price_pnl_usd` for the consolidated hedge, including spot cost
  basis, `unrealized_pnl_change_usd`, `stablecoin_fx_movement_usd`;
- `external_deposits_usd`, `external_withdrawals_usd`, `actual_funding_usd`,
  `commission_cost_usd`, `borrow_interest_cost_usd`, `internal_transfers_usd`.

Cashflow categories come from the scoped ledger when present. An explicit
override must agree with a present ledger subtotal. A missing category is not
automatically zero; a reconciled source may supply an explicit zero. Exchange
`REALIZED_PNL` commonly covers only the perpetual leg. The adapter therefore
does **not** relabel that subtotal as consolidated spot/perpetual PnL: provide
the consolidated spot cost-basis calculation, or keep this field unknown.

Only a finalized NAV, asserted complete reconciliation, complete valued event
lineage and actual OPEX produce `MEASURED` / a non-null
`verified_realized_net_profit_usd`. Here "verified" means the supplied
accounting identities pass, not that this tool contacted an exchange or
proved future profitability. Missing inputs yield `MEASUREMENT_INCOMPLETE`;
complete assumed/accrued OPEX yields `COST_ASSUMPTION_VIEW`. The net-cost and
MTM views remain explicitly separate. Reserved capital is time-weighted
equity committed to spot inventory, perpetual collateral and the associated
safety buffer, not gross spot-plus-perpetual notional.

## Paired 7-/30-day research

`mode: "comparison"` accepts `comparisons`, a list of objects with `candidate`
and `baseline`, plus `expected_digest`. Each side must use exactly the same
start/end and reserved-capital budget. Only 7- or 30-day periods are accepted.
An empty/mismatched expected digest returns `INSUFFICIENT_EVIDENCE`; inspect
the calculation, then pin the comparison digest in an independent reviewed
manifest before reproducing it. Both horizons and complete causal cost views
are required for `READY_FOR_RESEARCH_REVIEW`. This is an integrity/coverage
gate only, with no statistical or live-activation implication.

A single-cycle side contains these `CarryWindow` fields:

```text
label, start, end, policy_frozen_at, data_cutoff,
reserved_capital_usd, spot_quantity,
spot_entry_usd, spot_exit_usd, perp_entry_usd, perp_exit_usd,
prices_are_fills, commissions_usd, borrow_cost_usd,
execution_shortfall_usd, operating_costs, settlements,
funding_history_complete, evidence_kind
```

`evidence_kind` is `assumption`, `historical`, `shadow` or `live`; a label alone
does not authenticate it. Each settlement contains `source_id`,
`settlement_time`, `available_at`, `rate` and `mark_price_usd`. Eligibility is
**(cycle start, cycle end]**, an explicit modeling convention: production
eligibility must be established from exchange position/settlement records.
The settlement must be available by the data cutoff. Incomplete history
blocks net results. No 8-hour interval or fixed 1095 multiplier is used here.
Existing runtime display annualization remains unchanged.

With actual fill prices, `execution_shortfall_usd` must be zero because price
PnL already includes it. With reference mids, it is the total non-overlapping
execution shortfall for all entry and exit legs. Commission and borrowing
remain separate. Maker non-fills, emergency hedges, partial fills and failed
orders require their actual costs; an assumed maker probability is not a fill.

A rotating baseline uses `CarryPortfolioWindow`: period `label`, `start`,
`end`, `policy_frozen_at`, `data_cutoff`, `reserved_capital_usd`,
`operating_costs`, `evidence_kind`, `cycle_history_complete`, and `cycles` (a
list of the single-cycle objects above). Period OPEX is charged once; every
cycle's OPEX must explicitly be zero. Cycles must stay in the period, have
unique labels/funding payment IDs, and their concurrent capital reservations
must fit the shared budget. The common period reserve includes idle reserved
cash; it is not a sum of repeatedly reused capital. A failed/no-fill attempt
can have zero quantity and its evidenced fees/shortfall. Include all attempts,
losing cycles, forced closes and non-trading periods. The module calculates
their outcomes; it does not discover an optimal policy or infer missing
baseline trades. Use raw execution/reconciliation evidence for the baseline.

## Capital sensitivities

The following **synthetic sensitivity**, not a recommendation or measured
return, can be supplied directly as the input JSON:

```json
{
  "mode": "unit_economics",
  "capitals_usd": ["100", "500", "1000", "5000", "10000"],
  "reserved_fraction": "0.8",
  "annual_net_edge_on_reserved_before_opex": "0.03",
  "monthly_opex_usd": "5",
  "assumption_label": "Synthetic 3% net trading edge sensitivity; not a forecast"
}
```

For capital C, reserve fraction u, assumed annual net trading edge e and
monthly OPEX O, flat-capital annual PnL is `C*u*e - 12*O`; break-even capital
is `12*O/(u*e)` only for positive e. The synthetic inputs above imply -$57.60
per year on $100 and a $2,500 fixed-cost break-even; they do not establish e.
Run pessimistic/base/optimistic **assumptions** separately and retain labels.
Trading edge e must already include commissions, spread/shortfall, adverse
selection, borrowing, failures, rotation and all other variable trading costs
without duplication. Notional funding APR is not this reserved-equity edge.

The report also shows monthly reinvestment
`C_next = C*(1 + u*e/12) - O`, stops at cash-budget exhaustion, and never earns
returns on negative capital. Flat-capital scenarios still charge the stated
12-month OPEX, even when that requires external cost funding. The exchange
minimum notional, lot/price filters, reserve/margin requirements and feasible
hedge quantity remain `NOT_EVALUATED`; a positive arithmetic result does not
make a $100 hedge technically possible.

## Evidence still required before any production decision

Collect exchange fills/fees/funding, spot cost basis, collateral reservations,
invoice allocations, complete funding schedules, executable prices and
unhedged notional-time. Freeze policies before out-of-sample observation.
Compare slow 7-/30-day carry with the existing rotation baseline on matched
periods, including failed orders and delisted instruments. Independently
reconcile full account NAV. Bootstrap uncertainty with dependence-aware
blocks; do not count overlapping periods as independent samples. Real
deployment still requires the separate fault, operational, reconciliation,
drawdown, micro-canary and capital gates. Neither a positive backtest nor a
successful invocation of this report changes those requirements.
