"""Research and live feature engineering helpers."""

from __future__ import annotations

import polars as pl

from bongus.core.config import FUNDING_PERIODS_PER_YEAR


def build_feature_frame(df: pl.DataFrame) -> pl.DataFrame:
    basis = ((pl.col("perp_close") - pl.col("spot_close")) / pl.col("spot_close")).alias("basis_pct")
    annualized_funding = (pl.col("funding_rate") * FUNDING_PERIODS_PER_YEAR).alias("annualized_funding")
    returns = pl.col("spot_close").pct_change().fill_null(0.0)

    feature_df = df.with_columns(
        basis,
        annualized_funding,
        returns.alias("_spot_returns"),
    ).with_columns(
        pl.col("basis_pct")
        .rolling_mean(window_size=60, min_samples=5)
        .alias("_basis_mean"),
        pl.col("basis_pct")
        .rolling_std(window_size=60, min_samples=5)
        .fill_null(0.0)
        .alias("_basis_std"),
        pl.col("annualized_funding")
        .ewm_mean(span=30, adjust=False)
        .alias("_funding_ema"),
        pl.col("_spot_returns")
        .rolling_std(window_size=30, min_samples=5)
        .fill_null(0.0)
        .alias("realized_volatility"),
    ).with_columns(
        pl.when(pl.col("_basis_std") > 0)
        .then((pl.col("basis_pct") - pl.col("_basis_mean")) / pl.col("_basis_std"))
        .otherwise(0.0)
        .alias("basis_zscore"),
        (pl.col("annualized_funding") - pl.col("_funding_ema")).alias("funding_momentum"),
        (1.0 / (1.0 + pl.col("_basis_std") * 10_000.0)).alias("basis_stability"),
        (1.0 / (1.0 + pl.col("realized_volatility") * 200.0)).alias("regime_health"),
    )
    return feature_df.drop("_spot_returns", "_basis_mean", "_basis_std", "_funding_ema")


def add_future_edge_target(df: pl.DataFrame, horizon_minutes: int = 60) -> pl.DataFrame:
    if "annualized_funding" not in df.columns:
        df = build_feature_frame(df)

    future_basis = pl.col("basis_pct").shift(-horizon_minutes)
    future_funding = pl.col("annualized_funding").shift(-horizon_minutes)
    current_basis = pl.col("basis_pct")
    current_funding = pl.col("annualized_funding")

    return df.with_columns(
        (
            (future_funding.fill_null(current_funding) - current_funding)
            - (future_basis.fill_null(current_basis) - current_basis).abs() * 100.0
        ).alias("future_edge_target")
    )
