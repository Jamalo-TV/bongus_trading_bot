"""
Data loading and time-series alignment.

Reads spot OHLCV, perp OHLCV, and funding-rate Parquet files, merges them
onto a single 1-minute timeline via asof join, and forward-fills the funding
rate so every row knows the "current" prevailing rate.
"""

from pathlib import Path

import polars as pl

from bongus.core.config import FUNDING_SNAPSHOT_HOURS


def load_data(
    spot_path: str | Path,
    perp_path: str | Path,
    funding_path: str | Path,
) -> pl.DataFrame:
    """
    Load and align spot, perp, and funding data onto a 1-minute timeline.

    Returns a DataFrame with columns:
        timestamp, spot_close, perp_close, funding_rate, funding_snapshot
    """
    spot = pl.read_parquet(spot_path).select(
        pl.col("timestamp").cast(pl.Datetime("us", "UTC")),
        pl.col("close").alias("spot_close"),
    )

    perp = pl.read_parquet(perp_path).select(
        pl.col("timestamp").cast(pl.Datetime("us", "UTC")),
        pl.col("close").alias("perp_close"),
    )

    funding = pl.read_parquet(funding_path).select(
        pl.col("timestamp").cast(pl.Datetime("us", "UTC")),
        pl.col("funding_rate"),
    )

    # ── Step 1: Exact-join spot and perp on timestamp ─────────────────────
    df = spot.join(perp, on="timestamp", how="inner")

    # ── Step 2: Asof-join funding rates ──────────────────────────────────
    # Sort both sides by timestamp (required for join_asof)
    df = df.sort("timestamp")
    funding = funding.sort("timestamp")

    df = df.join_asof(
        funding,
        on="timestamp",
        strategy="backward",  # carry the most recent funding rate forward
    )

    # ── Step 3: Forward-fill any remaining nulls ─────────────────────────
    df = df.with_columns(
        pl.col("funding_rate").forward_fill(),
    )

    # ── Step 4: Mark funding snapshot rows ───────────────────────────────
    df = df.with_columns(
        (
            pl.col("timestamp").dt.hour().is_in(FUNDING_SNAPSHOT_HOURS)
            & (pl.col("timestamp").dt.minute() == 0)
        ).alias("funding_snapshot"),
    )

    return df
