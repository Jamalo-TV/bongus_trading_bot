"""Basic historical data-quality checks used by research tooling."""

from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl


@dataclass(slots=True)
class DataQualityReport:
    ok: bool
    issues: list[str] = field(default_factory=list)


def validate_market_data(df: pl.DataFrame, max_allowed_gap_minutes: int = 1) -> DataQualityReport:
    issues: list[str] = []
    if df.is_empty():
        return DataQualityReport(ok=False, issues=["empty dataset"])

    max_gap_minutes = (
        df.select(pl.col("timestamp").diff().dt.total_minutes().max().alias("max_gap_minutes"))
        .get_column("max_gap_minutes")
        .item()
    )
    if isinstance(max_gap_minutes, (int, float)) and max_gap_minutes > max_allowed_gap_minutes:
        issues.append(f"gap exceeds {max_allowed_gap_minutes} minute(s)")

    for column in ("spot_close", "perp_close", "funding_rate"):
        if column in df.columns and df[column].null_count() > 0:
            issues.append(f"{column} contains nulls")

    if "spot_close" in df.columns and (df["spot_close"] <= 0).any():
        issues.append("spot_close contains non-positive values")
    if "perp_close" in df.columns and (df["perp_close"] <= 0).any():
        issues.append("perp_close contains non-positive values")

    return DataQualityReport(ok=not issues, issues=issues)


def add_funding_freshness_flags(df: pl.DataFrame, max_funding_staleness_minutes: int) -> pl.DataFrame:
    return df.with_columns(
        (
            pl.col("timestamp").cum_count()
            - pl.when(pl.col("funding_snapshot")).then(pl.col("timestamp").cum_count()).otherwise(None).forward_fill().fill_null(0)
        ).alias("_bars_since_snapshot")
    ).with_columns(
        (pl.col("_bars_since_snapshot") <= max_funding_staleness_minutes).alias("funding_is_fresh")
    ).drop("_bars_since_snapshot")
