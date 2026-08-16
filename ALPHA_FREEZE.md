# Bongus safety-program alpha freeze

Status: **ACTIVE**  
Effective date: 2026-08-15  
Scope: the complete safety, PnL, research, evidence, and operations program

This manifest is a release-control boundary. New entries remain administratively
paused for the duration of the program. Passing tests, research results, or an
evidence gate cannot enable live trading or raise a capital limit. A live Binance
micro-canary requires a separate, explicit approval artifact. Hyperliquid trading
requires a new and separately approved execution project.

## Frozen work

The following changes are forbidden during this program:

- ML models, feature engineering, or model promotion;
- OBI or other microstructure-signal tuning;
- sentiment improvements;
- maker-fill prediction changes;
- new normal execution routes;
- symbol-universe expansion;
- funding-threshold optimization;
- rotation-policy optimization;
- leverage, capital, slot, or notional increases;
- dashboard redesign or cosmetic work;
- performance tuning without a measured safety bottleneck.

Permitted exceptions are limited to emergency execution, correctness repairs,
exchange-protocol migrations, risk controls, durability, reconciliation, and the
measurement or observability needed to prove safety or economics. Any exception
must explain why it is necessary and must not change strategy selection behavior.

## Change classification

Every pull request must declare exactly one classification:

- `SAFETY`: execution correctness, risk controls, durability, or recovery;
- `PNL`: ledger, reconciliation, NAV, fees, funding, or TCA;
- `RESEARCH`: the isolated, read-only Binance-Hyperliquid experiment only;
- `OPERATIONS`: deployment, backups, monitoring, or runbooks.

Unclassified or multiply classified changes must not merge. Safety lifecycle
changes and research-policy or alpha changes must never share one pull request.

## Global safety invariants

1. Exposure-changing data is durable before acknowledgement.
2. Actual fills, never requested quantities, define inventory.
3. Every exposure has durable cycle, intent, order, and fill lineage.
4. Missing account information is `UNKNOWN`, never zero.
5. Unknown state blocks entries while preserving reconciliation and exits.
6. Lifecycle history is append-only; completion uses tombstones, not deletion.
7. Quantities, prices, fees, and money use exact decimals.
8. Retries use deterministic client identifiers and read back ambiguous results.
9. Internal account transfers are not consolidated PnL.
10. Live permission is an explicit signed artifact and is never inferred.
11. An entry cannot use the emergency route.
12. A rotation exit must be confirmed filled before its replacement entry is sent.

## Frozen architectural decisions

| Decision | Program default |
|---|---|
| Binance account model | Standard Spot plus separate USD-M Futures |
| Execution source of truth | Rust durable execution journal |
| Python role | Idempotent projection and reporting layer |
| Ultimate reconciliation truth | Signed exchange balances, positions, orders, fills, and statements |
| Automatic emergency authority | Cancel and derisk naked exposure or dangerous margin states |
| Balanced position under uncertainty | Freeze entries and reconcile; flatten only when a risk threshold requires it |
| Normal active route | Existing dual-maker route only |
| Emergency route | Dedicated reduce-only and inventory-limited route |
| Entry maker TTL | Provisional 15 seconds, subject to testnet validation |
| Research execution assumption | All taker; maker is sensitivity only |
| Research leverage | 2x per leg primary; 3x sensitivity only |
| Research target size | USD 1,250 per leg |
| Research universe | BTC, ETH, SOL, XRP, DOGE |
| Primary cross-venue direction | Long Binance perp, short Hyperliquid perp |
| Research capital | Two independently prefunded collateral pools |

## Administrative lockdown

- `live_config.json` must keep `pause_new_entries` set to `true`.
- Only `paper` and `testnet` modes are allowed during engineering and evidence
  collection.
- Withdrawals must remain disabled on every trading API key.
- Capital, leverage, universe size, and slot count must not increase.
- Exits, signed reconciliation, and emergency derisking remain available while
  entries are paused.

The freeze ends only through a separately reviewed change that cites the complete
definition-of-done evidence. It does not end automatically with elapsed time.
