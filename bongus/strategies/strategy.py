"""
Fully vectorized strategy logic — zero Python for-loops in signal generation.

Computes entry/exit signals with quality filters, tracks position state,
accrues funding yield, and annotates the aligned DataFrame for analytics.
"""

import polars as pl

from bongus.core.config import (
    BASIS_DEVIATION_STOP,
    ENTRY_ANN_FUNDING_THRESHOLD,
    ENTRY_PREMIUM_THRESHOLD,
    EXIT_ANN_FUNDING_THRESHOLD,
    EXIT_DISCOUNT_THRESHOLD,
    FUNDING_CAPTURE_DELAY_MIN,
    FUNDING_PERIODS_PER_YEAR,
    FUNDING_SNAPSHOT_HOURS,
    HOLD_THROUGH_FUNDING,
    MARGIN_BORROW_RATE_ANNUAL,
    SNAPSHOT_SNIPE_ENABLED,
    SNIPE_ANN_FUNDING_THRESHOLD,
    SNIPE_ENTRY_WINDOW_MIN,
    SNIPE_ENTRY_WINDOW_MAX,
)


def _compute_derived_metrics(
    df: pl.DataFrame, features: pl.DataFrame | None,
) -> tuple[pl.DataFrame, bool, bool, bool]:
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

    # ── Step 1d: Join basis_zscore, funding_momentum, OBI from features ──
    has_zscore = False
    has_momentum = False
    has_obi = False
    if features is not None:
        join_cols = ["timestamp"]
        if "basis_zscore" in features.columns:
            join_cols.append("basis_zscore")
            has_zscore = True
        if "funding_momentum" in features.columns:
            join_cols.append("funding_momentum")
            has_momentum = True
        if "order_book_imbalance" in features.columns:
            join_cols.append("order_book_imbalance")
            has_obi = True

        if len(join_cols) > 1:
            feat_df = features.select(join_cols)
            df = df.join(feat_df, on="timestamp", how="left")
            if has_zscore:
                df = df.with_columns(pl.col("basis_zscore").fill_null(0.0))
            if has_momentum:
                df = df.with_columns(pl.col("funding_momentum").fill_null(0.0))
            if has_obi:
                df = df.with_columns(pl.col("order_book_imbalance").fill_null(0.0))

    return df, has_zscore, has_momentum, has_obi


def _compute_raw_signals(
    df: pl.DataFrame, has_zscore: bool, has_momentum: bool = False, has_obi: bool = False,
) -> pl.DataFrame:
    entry_expr = (
        (pl.col("annualized_funding") > ENTRY_ANN_FUNDING_THRESHOLD)
        & (pl.col("basis_premium_pct") > ENTRY_PREMIUM_THRESHOLD)
        # Filter 1: Funding not decelerating (velocity >= 0)
        & (pl.col("funding_velocity") >= 0.0)
        # Filter 2: Not too close to funding snapshot (> 15 min to avoid snapshot volatility)
        & (pl.col("minutes_to_next_snapshot") > 15)
    )

    # Filter 3: Basis z-score cap (don't enter at extreme premium)
    if has_zscore:
        entry_expr = entry_expr & (pl.col("basis_zscore") < 2.0)

    # Filter 4: Funding momentum (don't enter when funding is reverting below its EMA)
    if has_momentum:
        entry_expr = entry_expr & (pl.col("funding_momentum") > 0.0)

    # Filter 5: Order book imbalance (prefer bid-heavy book when going long spot)
    if has_obi:
        entry_expr = entry_expr & (pl.col("order_book_imbalance") > 0.1)

    # Snapshot snipe stays disabled in the release candidate until costs are
    # recalibrated from realized fills.
    snipe_entry_expr = (
        (pl.col("annualized_funding") > SNIPE_ANN_FUNDING_THRESHOLD)
        & (pl.col("basis_premium_pct") > ENTRY_PREMIUM_THRESHOLD)
        & (pl.col("minutes_to_next_snapshot") >= SNIPE_ENTRY_WINDOW_MIN)
        & (pl.col("minutes_to_next_snapshot") <= SNIPE_ENTRY_WINDOW_MAX)
        & pl.lit(SNAPSHOT_SNIPE_ENABLED)
    )

    # Snipe exit: close shortly after snapshot if funding is declining
    snipe_exit_expr = (
        (pl.col("minutes_to_next_snapshot") > (8 * 60 - 10))  # just past a snapshot
        & (pl.col("funding_velocity") < 0.0)
    )

    # Hold-through-funding: Flag rows in the window just AFTER a funding snapshot.
    # Detect by checking that the previous bar was near-zero minutes to snapshot
    # and the current bar has wrapped to nearly a full 8h cycle.
    just_after_snapshot = (
        pl.col("minutes_to_next_snapshot") > (8 * 60 - FUNDING_CAPTURE_DELAY_MIN)
    ) & (
        pl.col("minutes_to_next_snapshot").shift(1) <= FUNDING_CAPTURE_DELAY_MIN
    )

    inverse_signal_expr = pl.lit(False)

    # Long-only release candidate: exit when positive funding decays or basis inverts.
    exit_cond = (
        (pl.col("annualized_funding") < EXIT_ANN_FUNDING_THRESHOLD)
        | (pl.col("basis_premium_pct") < EXIT_DISCOUNT_THRESHOLD)
    )

    # Hold-through-funding: suppress normal exits in the window right after a
    # funding snapshot so the position captures the payment before re-evaluating.
    if HOLD_THROUGH_FUNDING:
        exit_cond = exit_cond & ~just_after_snapshot

    # Add snipe_exit: close shortly after snapshot if funding is declining
    exit_cond = exit_cond | (
        (pl.col("minutes_to_next_snapshot") > (8 * 60 - 5))  # Just past snapshot
        & (pl.col("funding_velocity") < 0.0)
    )

    df = df.with_columns(
        (entry_expr | snipe_entry_expr).alias("raw_entry"),
        exit_cond.alias("raw_exit"),
        inverse_signal_expr.alias("inverse_signal"),
    )
    return df


def _apply_basis_deviation_stop(df: pl.DataFrame) -> pl.DataFrame:
    """Force exit when basis deviates too far from entry basis (tail-risk protection)."""
    df = df.with_columns(
        (
            (pl.col("perp_entry_price") - pl.col("spot_entry_price"))
            / pl.col("spot_entry_price")
        ).alias("_entry_basis"),
    )

    df = df.with_columns(
        pl.when(
            pl.col("in_position")
            & (
                (pl.col("basis_premium_pct") - pl.col("_entry_basis")).abs()
                > BASIS_DEVIATION_STOP
            )
        )
        .then(True)
        .otherwise(pl.col("raw_exit"))
        .alias("raw_exit"),
    )

    df = df.drop("_entry_basis")
    return df


def _compute_position_state(df: pl.DataFrame) -> pl.DataFrame:
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
    return df


def _record_entry_prices(df: pl.DataFrame) -> pl.DataFrame:
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
    return df


def _accrue_funding_yield(df: pl.DataFrame) -> pl.DataFrame:
    # Per-snapshot borrowing cost: annual rate / number of 8h periods per year
    borrow_cost_per_snapshot = MARGIN_BORROW_RATE_ANNUAL / FUNDING_PERIODS_PER_YEAR

    df = df.with_columns(
        pl.when(pl.col("in_position") & pl.col("funding_snapshot"))
        .then(pl.col("funding_rate") - borrow_cost_per_snapshot)
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
    return df


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
    df, has_zscore, has_momentum, has_obi = _compute_derived_metrics(df, features)
    df = _compute_raw_signals(df, has_zscore, has_momentum, has_obi)
    df = _compute_position_state(df)
    df = _record_entry_prices(df)

    # ── Basis deviation stop-loss (tail-risk protection) ───────────────
    # Must run after entry prices are known; re-run state machine with updated exits
    df = _apply_basis_deviation_stop(df)
    df = df.drop("in_position", "trade_id", "spot_entry_price", "perp_entry_price")
    df = _compute_position_state(df)
    df = _record_entry_prices(df)

    df = _accrue_funding_yield(df)

    # ── Cleanup helper columns ───────────────────────────────────────────
    df = df.drop("_is_entry_bar", "_funding_accrual", "_hour", "_minute")

    return df
