"""Feature engineering for funding-arb research and model training."""

import math

import polars as pl

from config import FUNDING_PERIODS_PER_YEAR, FUNDING_SNAPSHOT_HOURS


def build_feature_frame(df: pl.DataFrame, lookback_minutes: int = 60) -> pl.DataFrame:
    required = {"timestamp", "spot_close", "perp_close", "funding_rate"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {sorted(missing)}")

    feature_df = (
        df.sort("timestamp")
        .with_columns(
            ((pl.col("perp_close") - pl.col("spot_close")) / pl.col("spot_close")).alias(
                "basis_premium_pct"
            ),
            pl.col("spot_close").pct_change().fill_null(0.0).alias("spot_ret_1m"),
            pl.col("perp_close").pct_change().fill_null(0.0).alias("perp_ret_1m"),
            (pl.col("funding_rate") * FUNDING_PERIODS_PER_YEAR).alias("annualized_funding"),
        )
    )

    # ── Funding velocity & acceleration (predictive features) ────────────
    feature_df = feature_df.with_columns(
        pl.col("annualized_funding").diff(n=1).fill_null(0.0).alias("funding_velocity"),
        pl.col("annualized_funding").diff(n=1).diff(n=1).fill_null(0.0).alias("funding_acceleration"),
    )

    # ── Minutes to next funding snapshot ─────────────────────────────────
    feature_df = feature_df.with_columns(
        pl.col("timestamp").dt.hour().alias("_hour"),
        pl.col("timestamp").dt.minute().alias("_minute"),
    )

    snapshot_hours = sorted(FUNDING_SNAPSHOT_HOURS)
    min_to_snap_expr = pl.lit((snapshot_hours[0] + 24) * 60)
    for snap_h in reversed(snapshot_hours):
        snap_total = snap_h * 60
        min_to_snap_expr = (
            pl.when((pl.col("_hour") * 60 + pl.col("_minute")) < snap_total)
            .then(snap_total - (pl.col("_hour") * 60 + pl.col("_minute")))
            .otherwise(min_to_snap_expr)
        )

    feature_df = feature_df.with_columns(
        min_to_snap_expr.alias("minutes_to_next_snapshot"),
    ).drop("_hour", "_minute")

    # ── Order book features ──────────────────────────────────────────────
    obi_col = (
        (pl.col("bid_sz") - pl.col("ask_sz")) / (pl.col("bid_sz") + pl.col("ask_sz") + 1e-9)
        if {"bid_sz", "ask_sz"}.issubset(set(feature_df.columns))
        else pl.lit(0.0)
    )

    queue_col = (
        pl.col("queue_ahead_sz") / (pl.col("queue_ahead_sz") + pl.lit(1000.0))
        if "queue_ahead_sz" in feature_df.columns
        else pl.lit(0.5)
    )

    feature_df = (
        feature_df.with_columns(
        obi_col.alias("order_book_imbalance"),
        queue_col.alias("queue_fill_prob"),

        # Kalman-like smooth estimate of 'true' basis state (proxy via EMA)
        pl.col("basis_premium_pct")
            .ewm_mean(com=lookback_minutes / 2)
            .alias("kalman_basis_state"),

            pl.col("basis_premium_pct")
            .rolling_mean(window_size=lookback_minutes)
            .alias("basis_mean"),
            pl.col("basis_premium_pct")
            .rolling_std(window_size=lookback_minutes)
            .alias("basis_std"),
            pl.col("spot_ret_1m")
            .rolling_std(window_size=lookback_minutes)
            .fill_null(0.0)
            .alias("spot_vol_lookback"),
            (pl.col("spot_close") / pl.col("spot_close").shift(lookback_minutes) - 1.0)
            .fill_null(0.0)
            .alias("spot_trend_lookback"),
            pl.col("annualized_funding")
            .rolling_mean(window_size=lookback_minutes)
            .alias("funding_trend"),
        )
        .with_columns(
            pl.when(pl.col("basis_std") > 0)
            .then((pl.col("basis_premium_pct") - pl.col("basis_mean")) / pl.col("basis_std"))
            .otherwise(0.0)
            .alias("basis_zscore"),
            (pl.col("spot_vol_lookback") * math.sqrt(60 * 24 * 365)).alias(
                "spot_vol_annualized"
            ),
        )
        .drop("basis_mean", "basis_std")
    )

    if {"bid_ask_spread_bps", "depth_usd"}.issubset(set(feature_df.columns)):
        feature_df = feature_df.with_columns(
            (
                (1.0 / (1.0 + pl.col("bid_ask_spread_bps").abs()))
                * pl.col("depth_usd").log1p()
            ).alias("liquidity_score")
        )
    else:
        feature_df = feature_df.with_columns(pl.lit(0.0).alias("liquidity_score"))

    return feature_df


def add_future_edge_target(df: pl.DataFrame, horizon_minutes: int = 60) -> pl.DataFrame:
    if "basis_premium_pct" not in df.columns:
        raise ValueError("Run build_feature_frame first before adding target")

    return df.with_columns(
        (
            pl.col("funding_rate").shift(-horizon_minutes)
            - pl.col("basis_premium_pct").shift(-horizon_minutes).abs() * 0.25
        )
        .fill_null(0.0)
        .alias("future_edge_target")
    )
