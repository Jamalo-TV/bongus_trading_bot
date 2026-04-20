# Fix Plan — Per-Symbol Cap Overshoot After Auto-Recompound

**Date:** 2026-04-20
**Severity:** HIGH (entries silently blocked; bot frozen despite `entry_policy=OPEN`)
**Status:** Diagnosed, not yet implemented
**Affected version:** current `main` (verified against commit `d02438a`)

## Symptom

Repeating warning every trading cycle (observed over ~10 h window, 581 × ATAUSDT + 300 × SUIUSDT):

```
WARNING live_trader_v2: ENTER blocked for ATAUSDT — projected symbol notional $5389 would
exceed per-symbol cap $5000 (open=$0, new=$5389)
```

ATAUSDT and SUIUSDT are the only candidates that pass every other filter (funding threshold,
basis, depth, regime). They are then sized at ~$5,389 notional and rejected by the per-symbol
cap check. No other symbols pass earlier filters, so the bot does not enter **anything**.

Dashboard reports `CAN TRADE`, `allow_new_risk=true`, `pause_new_entries=false`, kill switch
clear, scale 1.0, yet `open_position_count=0` and `trade_count=8 over 7 days`.

## Root Cause (confirmed via code trace)

**The sizing floor grew with account equity; the per-symbol cap did not.**

### Path 1 — Daily auto-compound reassigns `_capital_per_slot`

[scripts/live_trader_v2.py:6138-6149](../scripts/live_trader_v2.py#L6138-L6149):

```python
async def _maybe_recompound(self) -> None:
    ...
    new_capital = equity / MAX_CONCURRENT_POSITIONS
    self.allocator = PortfolioAllocator(
        self.depth_tracker, self.funding_ranker, capital_per_slot_usd=new_capital
    )
```

Current state at observation time:
- `equity = $10,794.60`
- `MAX_CONCURRENT_POSITIONS = 4`
- → `_capital_per_slot = $2,698.65`

### Path 2 — Target notional scales up with `_capital_per_slot`

[scripts/live_trader_v2.py:6790-6793](../scripts/live_trader_v2.py#L6790-L6793) (and duplicate
at [6394-6395](../scripts/live_trader_v2.py#L6394-L6395)):

```python
base_target_notional = min(
    self.allocator._capital_per_slot * TARGET_LEVERAGE * max(0.1, self._effective_notional_scale()),
    MAX_NOTIONAL_PER_TRADE,
)
# = min(2698.65 * 2.0 * 1.0, 7500) = $5,397.30
```

`_var_sized_notional` + `round(..., 2)` shaves it to ~$5,389 (matches log).

### Path 3 — Per-symbol cap is static

[scripts/live_trader_v2.py:6047-6060](../scripts/live_trader_v2.py#L6047-L6060):

```python
per_symbol_cap = float(self._config.get("per_symbol_notional_cap_usd"))  # = 5000
existing_symbol_gross = self._current_gross_by_symbol.get(symbol, 0.0)
if existing_symbol_gross + notional_usd > per_symbol_cap:   # 0 + 5389 > 5000 → blocks
    logger.warning("ENTER blocked for %s — projected symbol notional ...")
    return
```

Cap is sourced from [bongus/core/config.py:36](../bongus/core/config.py#L36):

```python
PER_SYMBOL_NOTIONAL_CAP_USD = 5_000.0
```

This constant was calibrated to the pre-compound `$2,500 × 2× leverage = $5,000` slot size
and has no scaling hook. `ConfigManager` registers the key
([bongus/core/config_manager.py:183](../bongus/core/config_manager.py#L183)), but nothing
updates it at recompound time.

### Deterministic trigger condition

```
equity × TARGET_LEVERAGE / MAX_CONCURRENT_POSITIONS  >  PER_SYMBOL_NOTIONAL_CAP_USD
```

At starting equity $10,959 the threshold was already crossed
(`10959 × 2 / 4 = 5,479.50`). The bug has been latent for the entire validation run; it
explains the 8-trades-in-7-days sample and the -16.28 Sharpe.

## Fix Strategies

### Strategy A (RECOMMENDED) — Clamp target notional at the cap before sizing

**Rationale:** preserves the stated intent of `PER_SYMBOL_NOTIONAL_CAP_USD` ("prevents
accumulating multiple slots in one symbol") and makes the cap an actual ceiling instead of a
trap. Excess equity from compounding stays idle until the operator raises the cap or the
slot count — the safe default.

**Edits (three sites):**

1. [scripts/live_trader_v2.py:6790-6793](../scripts/live_trader_v2.py#L6790-L6793) — primary
   entry sizing:
   ```python
   base_target_notional = min(
       self.allocator._capital_per_slot * TARGET_LEVERAGE * max(0.1, self._effective_notional_scale()),
       MAX_NOTIONAL_PER_TRADE,
       float(self._config.get("per_symbol_notional_cap_usd")),   # NEW clamp
   )
   ```

2. [scripts/live_trader_v2.py:6394-6395](../scripts/live_trader_v2.py#L6394-L6395) — the
   parallel sizing block used for the VaR target payload (`var_target_notional_usd`). Apply
   the same `min(..., per_symbol_notional_cap_usd)` term.

3. [scripts/live_trader_v2.py:6891-6894](../scripts/live_trader_v2.py#L6891-L6894) — rotation
   fallback notional:
   ```python
   rotation_notional = decision.rotation_notionals.get(
       exited_symbol,
       min(CAPITAL_PER_SLOT_USD * TARGET_LEVERAGE,
           float(self._config.get("per_symbol_notional_cap_usd"))),
   )
   ```
   `decision.rotation_notionals` itself will already be clamped because `notional_overrides`
   is derived from the clamped `base_target_notional` via `_var_sized_notional`.

### Strategy B (alternative, NOT recommended as first fix) — Make the cap scale

Only do this if the operator explicitly wants position size to grow with equity. Touches
more of the stack:

1. Store a dynamic cap on the allocator at recompound time
   (`self._per_symbol_cap = new_capital * TARGET_LEVERAGE * 1.0`).
2. Rewrite the cap check at line 6048 to read from the allocator instead of `self._config`.
3. Expose a `per_symbol_notional_cap_scale` multiplier in `ConfigManager` as the live knob.
4. Update the dashboard if it surfaces the cap anywhere.

The current constraint stack (gross exposure, correlation breaker,
`MAX_SYMBOL_CONCENTRATION=0.30`) was validated with a $5,000-per-symbol assumption. Raising
the per-symbol ceiling shifts risk the operator may not have budgeted for.

## Stopgap (server-side, no code change — apply immediately)

Edit `live_config.json` to add a single key:

```json
{
  "entry_ann_funding_threshold": 0.03,
  "entry_premium_threshold": 0.002,
  "pause_new_entries": false,
  "reset_equity_high_watermark": false,
  "per_symbol_notional_cap_usd": 6000
}
```

`ConfigManager` hot-reloads within 30 s
([bongus/core/config_manager.py:183](../bongus/core/config_manager.py#L183)). Unblocks
entries immediately. $6,000 gives ~10% headroom over the current target $5,397 and stays
below `MAX_NOTIONAL_PER_TRADE=7500`. Stopgap only — Strategy A must still land.

## Tests

1. **New regression** in `tests/test_live_trader.py` (create if absent):
   - Stub `LiveTraderV2` with `_capital_per_slot=2_800`, `TARGET_LEVERAGE=2.0`,
     `per_symbol_notional_cap_usd=5000`.
   - Assert `base_target_notional` after the clamp is `5000.0`, not `5600.0`.
   - Assert `_dispatch_enter(symbol, 5000, ...)` does NOT log `ENTER blocked`.

2. **Update** [tests/test_portfolio_allocator.py:45](../tests/test_portfolio_allocator.py#L45):
   add a case with `capital_per_slot_usd=2800` and per-symbol cap $5,000; assert the
   allocator's enter tuple carries `≤5000`. The existing test uses 2500 and never exercises
   the bug path.

3. **Defensive log** at top of `_maybe_recompound`: if
   `new_capital * TARGET_LEVERAGE > PER_SYMBOL_NOTIONAL_CAP_USD`, emit a WARNING
   (`"Recompounded capital exceeds per-symbol cap — position size will be clamped"`).
   Prevents silent degradation of future operators' mental model.

## Verification Checklist

1. `pytest tests/test_portfolio_allocator.py tests/test_live_trader.py -v` — all green, new
   regression test present.
2. `pyright scripts/live_trader_v2.py bongus/portfolio/portfolio_allocator.py` — no new type
   errors.
3. Hand-trace arithmetic at fix site: `_capital_per_slot=2698.65`, `leverage=2.0`,
   `scale=1.0`, `MAX_NOTIONAL_PER_TRADE=7500`, `cap=5000` → `base_target_notional == 5000.0`.
4. Grep for other callers:
   ```
   rg '_capital_per_slot\s*\*\s*TARGET_LEVERAGE' scripts bongus
   ```
   Currently two hits (6394, 6791); both must be patched.
5. Confirm `_var_sized_notional`
   ([scripts/live_trader_v2.py:869-879](../scripts/live_trader_v2.py#L869-L879)) requires no
   change — it already `min`s against `base_notional`, so if `base_notional` is clamped, VaR
   output is implicitly clamped.

## What NOT to Change

- Do NOT raise `PER_SYMBOL_NOTIONAL_CAP_USD` in `config.py` beyond $5,000 without also
  updating `MAX_SYMBOL_CONCENTRATION` and the portfolio gross-exposure ceiling. The risk
  stack is calibrated to this number.
- Do NOT reorder the two exposure guards at lines 6024-6060 — gross must precede per-symbol
  so pending exposure is accounted for.
- Do NOT remove `_maybe_recompound`. The auto-compound is desired behavior; the bug is its
  *interaction* with the static cap.
- Do NOT disable `_var_sized_notional` — it is orthogonal to this bug.

## Expected Outcome

- `ENTER blocked ... per-symbol cap` warnings disappear from `live_trader.log`.
- Next cycle that surfaces ATAUSDT / SUIUSDT with passing filters dispatches an entry.
- Notional per slot stabilizes at $5,000 (Strategy A) instead of drifting upward with equity.
- `/api/stats` trade count begins incrementing; validation board accumulates a meaningful
  Sharpe sample.

## References

- Block site: [scripts/live_trader_v2.py:6047-6060](../scripts/live_trader_v2.py#L6047-L6060)
- Sizing sites: [scripts/live_trader_v2.py:6790-6793](../scripts/live_trader_v2.py#L6790-L6793),
  [scripts/live_trader_v2.py:6394-6395](../scripts/live_trader_v2.py#L6394-L6395)
- Recompound: [scripts/live_trader_v2.py:6138-6149](../scripts/live_trader_v2.py#L6138-L6149)
- Static cap: [bongus/core/config.py:36](../bongus/core/config.py#L36)
- Config manager registration: [bongus/core/config_manager.py:183](../bongus/core/config_manager.py#L183)
- Allocator override path: [bongus/portfolio/portfolio_allocator.py:135-199](../bongus/portfolio/portfolio_allocator.py#L135-L199)
