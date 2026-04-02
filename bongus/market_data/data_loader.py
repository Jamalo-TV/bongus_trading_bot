"""Parquet alignment helpers for spot, perp, and funding datasets."""

from __future__ import annotations

import polars as pl

from bongus.core.config import FUNDING_SNAPSHOT_HOURS


def load_data(spot_path: str, perp_path: str, funding_path: str) -> pl.DataFrame:
    spot = pl.read_parquet(spot_path).select(
        pl.col("timestamp"),
        pl.col("close").alias("spot_close"),
    )
    perp = pl.read_parquet(perp_path).select(
        pl.col("timestamp"),
        pl.col("close").alias("perp_close"),
    )
    funding = pl.read_parquet(funding_path).select("timestamp", "funding_rate")

    df = (
        spot.join(perp, on="timestamp", how="inner")
        .sort("timestamp")
        .join(funding.sort("timestamp"), on="timestamp", how="left")
        .with_columns(pl.col("funding_rate").forward_fill().fill_null(0.0))
        .with_columns(
            (
                pl.col("timestamp").dt.hour().is_in(FUNDING_SNAPSHOT_HOURS)
                & (pl.col("timestamp").dt.minute() == 0)
            ).alias("funding_snapshot")
        )
    )
    return df.select("timestamp", "spot_close", "perp_close", "funding_rate", "funding_snapshot")
