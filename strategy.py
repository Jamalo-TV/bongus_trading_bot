"""
Fully vectorized strategy logic — zero Python for-loops in signal generation.

Computes entry/exit signals with quality filters, tracks position state,
accrues funding yield, and annotates the aligned DataFrame for analytics.
"""

import polars as pl

from config import (
    FUNDING_PERIODS_PER_YEAR,
    ENTRY_ANN_FUNDING_THRESHOLD,
    ENTRY_PREMIUM_THRESHOLD,
    EXIT_ANN_FUNDING_THRESHOLD,
    EXIT_DISCOUNT_THRESHOLD,
    FUNDING_SNAPSHOT_HOURS,
)


def run_strategy(df: pl.DataFrame, features: pl.DataFrame | None = None) -> pl.DataFrame:
    """
    Annotate *df* with strategy columns and return the enriched DataFrame.

    Expected input columns:
        timestamp, spot_close, perp_close, funding_rate, funding_snapshot

    Optional *features* DataFrame (from feature_engineering.build_feature_frame)
    provides basis_zscore and funding_velocity for signal quality filtering.

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

    # ── Step 1b: Funding velocity (rate of change over 12 bars) ──────────
    df = df.with_columns(
        pl.col("annualized_funding").diff(n=12).fill_null(0.0).alias("funding_velocity"),
    )

    # ── Step 1c: Minutes to next funding snapshot ────────────────────────
    df = df.with_columns(
        pl.col("timestamp").dt.hour().alias("_hour"),
        pl.col("timestamp").dt.minute().alias("_minute"),
    )

    # Compute minutes to next snapshot (vectorized)
    snapshot_hours = sorted(FUNDING_SNAPSHOT_HOURS)
    # Build a conditional chain for minutes_to_next_snapshot
    min_to_snap_expr = pl.lit((snapshot_hours[0] + 24) * 60)  # default: wrap to next day
    for snap_h in reversed(snapshot_hours):
        snap_total = snap_h * 60
        min_to_snap_expr = (
            pl.when((pl.col("_hour") * 60 + pl.col("_minute")) < snap_total)
            .then(snap_total - (pl.col("_hour") * 60 + pl.col("_minute")))
            .otherwise(min_to_snap_expr)
        )

    df = df.with_columns(
        min_to_snap_expr.alias("minutes_to_next_snapshot"),
    )

    # ── Step 1d: Join basis_zscore from features if available ────────────
    has_zscore = False
    if features is not None and "basis_zscore" in features.columns:
        # Join on timestamp to bring in zscore
        zscore_df = features.select("timestamp", "basis_zscore")
        df = df.join(zscore_df, on="timestamp", how="left")
        df = df.with_columns(pl.col("basis_zscore").fill_null(0.0))
        has_zscore = True

    # ── Step 2: Raw entry / exit signals with quality filters ─────────────
    entry_expr = (
        (pl.col("annualized_funding") > ENTRY_ANN_FUNDING_THRESHOLD)
        & (pl.col("basis_premium_pct") > ENTRY_PREMIUM_THRESHOLD)
        # Filter 1: Funding not decelerating (velocity >= 0)
        & (pl.col("funding_velocity") >= 0.0)
        # Filter 2: Not too close to funding snapshot (> 30 min)
        & (pl.col("minutes_to_next_snapshot") > 30)
    )

    # Filter 3: Basis z-score cap (don't enter at extreme premium)
    if has_zscore:
        entry_expr = entry_expr & (pl.col("basis_zscore") < 2.0)

    df = df.with_columns(
        entry_expr.alias("raw_entry"),
        (
            (pl.col("annualized_funding") < EXIT_ANN_FUNDING_THRESHOLD)
            | (pl.col("basis_premium_pct") < EXIT_DISCOUNT_THRESHOLD)
        ).alias("raw_exit"),
    )

    # ── Step 3: Position state via cumulative logic ──────────────────────
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

    df = df.with_columns(
        pl.col("spot_entry_price").forward_fill(),
        pl.col("perp_entry_price").forward_fill(),
    )

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

    df = df.with_columns(
        pl.col("_funding_accrual")
        .cum_sum()
        .over("trade_id")
        .alias("cumulative_yield"),
    )

    df = df.with_columns(
        pl.when(pl.col("trade_id") > 0)
        .then(pl.col("cumulative_yield"))
        .otherwise(0.0)
        .alias("cumulative_yield"),
    )

    # ── Cleanup helper columns ───────────────────────────────────────────
    df = df.drop("_is_entry_bar", "_funding_accrual", "_hour", "_minute")

    return df
