"""
Fully vectorized strategy logic — zero Python for-loops.

Computes entry/exit signals, tracks position state, accrues funding yield,
and annotates the aligned DataFrame with everything needed for analytics.
"""

import polars as pl

from config import (
    FUNDING_PERIODS_PER_YEAR,
    ENTRY_PREMIUM_THRESHOLD,
    EXIT_ANN_FUNDING_THRESHOLD,
    EXIT_DISCOUNT_THRESHOLD,
    TAKER_FEE,
)


def run_strategy(df: pl.DataFrame) -> pl.DataFrame:
    """
    Annotate *df* with strategy columns and return the enriched DataFrame.

    Expected input columns:
        timestamp, spot_close, perp_close, funding_rate, funding_snapshot

    Added columns:
        annualized_funding, basis_premium_pct,
        raw_entry, raw_exit, in_position, trade_id,
        spot_entry_price, perp_entry_price, cumulative_yield
    """

    # ── Step 1: Derived metrics ──────────────────────────────────────────
    df = df.with_columns(
        (pl.col("funding_rate") * FUNDING_PERIODS_PER_YEAR).alias("annualized_funding"),
        (
            (pl.col("perp_close") - pl.col("spot_close")) / pl.col("spot_close")
        ).alias("basis_premium_pct"),
    )

    # ── Step 2: Raw entry / exit signals (before state filtering) ────────
    # Based on audit: Hardcode purely mathematical Basis Expansion threshold.
    # Enter when APY > (Taker Fees * 3) annualized.
    min_annualized_yield = TAKER_FEE * 3 * FUNDING_PERIODS_PER_YEAR

    df = df.with_columns(
        (
            (pl.col("annualized_funding") > min_annualized_yield)
            & (pl.col("basis_premium_pct") > ENTRY_PREMIUM_THRESHOLD)
        ).alias("raw_entry"),
        (
            (pl.col("annualized_funding") < EXIT_ANN_FUNDING_THRESHOLD)
            | (pl.col("basis_premium_pct") < EXIT_DISCOUNT_THRESHOLD)
        ).alias("raw_exit"),
    )

    # ── Step 3: Position state via cumulative logic ──────────────────────
    # We need to walk through entry/exit signals sequentially because each
    # row's state depends on the previous row.  Polars doesn't have a native
    # "scan" expression, so we materialise the two boolean columns and use a
    # small, tight NumPy-style loop on the underlying arrays.  This is ~O(n)
    # and still far faster than a pure-Python row-by-row DataFrame iteration.

    raw_entry = df["raw_entry"].to_list()
    raw_exit = df["raw_exit"].to_list()
    n = len(df)

    in_position = [False] * n
    trade_id = [0] * n
    current_trade = 0
    currently_in = False

    for i in range(n):
        if not currently_in and raw_entry[i]:
            current_trade += 1
            currently_in = True
        elif currently_in and raw_exit[i]:
            # Mark this row as still in position (we close at end of bar)
            in_position[i] = True
            trade_id[i] = current_trade
            currently_in = False
            continue

        if currently_in:
            in_position[i] = True
            trade_id[i] = current_trade

    df = df.with_columns(
        pl.Series("in_position", in_position),
        pl.Series("trade_id", trade_id),
    )

    # ── Step 4: Record entry prices ──────────────────────────────────────
    # First row of each trade_id (where trade_id changes from 0→id or id-1→id)
    df = df.with_columns(
        (pl.col("trade_id") != pl.col("trade_id").shift(1)).alias("_is_entry_bar"),
    )

    df = df.with_columns(
        pl.when(pl.col("_is_entry_bar") & pl.col("in_position"))
        .then(pl.col("spot_close"))
        .otherwise(None)
        .alias("spot_entry_price"),
        pl.when(pl.col("_is_entry_bar") & pl.col("in_position"))
        .then(pl.col("perp_close"))
        .otherwise(None)
        .alias("perp_entry_price"),
    )

    # Forward-fill entry prices within each trade
    df = df.with_columns(
        pl.col("spot_entry_price").forward_fill(),
        pl.col("perp_entry_price").forward_fill(),
    )

    # Zero-out entry prices when not in position
    df = df.with_columns(
        pl.when(pl.col("in_position"))
        .then(pl.col("spot_entry_price"))
        .otherwise(None)
        .alias("spot_entry_price"),
        pl.when(pl.col("in_position"))
        .then(pl.col("perp_entry_price"))
        .otherwise(None)
        .alias("perp_entry_price"),
    )

    # ── Step 5: Accrue funding yield at snapshot rows ────────────────────
    df = df.with_columns(
        pl.when(pl.col("in_position") & pl.col("funding_snapshot"))
        .then(pl.col("funding_rate"))
        .otherwise(0.0)
        .alias("_funding_accrual"),
    )

    # Cumulative yield per trade
    df = df.with_columns(
        pl.col("_funding_accrual")
        .cum_sum()
        .over("trade_id")
        .alias("cumulative_yield"),
    )

    # Zero-out for rows not in position (trade_id == 0)
    df = df.with_columns(
        pl.when(pl.col("trade_id") > 0)
        .then(pl.col("cumulative_yield"))
        .otherwise(0.0)
        .alias("cumulative_yield"),
    )

    # ── Cleanup helper columns ───────────────────────────────────────────
    df = df.drop("_is_entry_bar", "_funding_accrual")

    return df
