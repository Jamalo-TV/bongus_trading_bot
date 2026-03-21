"""Data quality, freshness, and latency checks for market datasets."""

from dataclasses import dataclass

import polars as pl


@dataclass
class DataQualityReport:
    rows: int
    null_violations: dict[str, int]
    duplicate_timestamps: int
    max_gap_minutes: float
    issues: list[str]

    @property
    def ok(self) -> bool:
        return len(self.issues) == 0


def validate_market_data(
    df: pl.DataFrame,
    required_columns: tuple[str, ...] = (
        "timestamp",
        "spot_close",
        "perp_close",
        "funding_rate",
        "funding_snapshot",
    ),
    max_allowed_gap_minutes: int = 1,
) -> DataQualityReport:
    missing = [c for c in required_columns if c not in df.columns]
    if missing:
        return DataQualityReport(
            rows=df.height,
            null_violations={},
            duplicate_timestamps=0,
            max_gap_minutes=0.0,
            issues=[f"missing columns: {', '.join(missing)}"],
        )

    nulls = {col: int(df[col].null_count()) for col in required_columns}

    duplicate_timestamps = (
        df.group_by("timestamp")
        .count()
        .filter(pl.col("count") > 1)
        .height
    )

    max_gap_minutes = 0.0
    if df.height > 1:
        gaps = (
            df.sort("timestamp")
            .select((pl.col("timestamp").diff().dt.total_minutes()).alias("gap"))
            .drop_nulls()
        )
        if gaps.height > 0:
            max_gap_minutes = float(gaps["gap"].max())

    issues: list[str] = []
    for col, count in nulls.items():
        if count > 0:
            issues.append(f"nulls in {col}: {count}")
    if duplicate_timestamps > 0:
        issues.append(f"duplicate timestamps: {duplicate_timestamps}")
    if max_gap_minutes > max_allowed_gap_minutes:
        issues.append(
            f"max timestamp gap {max_gap_minutes:.2f}m exceeds {max_allowed_gap_minutes}m"
        )

    return DataQualityReport(
        rows=df.height,
        null_violations=nulls,
        duplicate_timestamps=duplicate_timestamps,
        max_gap_minutes=max_gap_minutes,
        issues=issues,
    )


def add_funding_freshness_flags(
    df: pl.DataFrame,
    max_funding_staleness_minutes: int = 8 * 60,
) -> pl.DataFrame:
    if "funding_snapshot" not in df.columns:
        raise ValueError("funding_snapshot column is required for freshness checks")

    enriched = (
        df.sort("timestamp")
        .with_columns(
            pl.when(pl.col("funding_snapshot"))
            .then(pl.col("timestamp"))
            .otherwise(None)
            .forward_fill()
            .alias("last_funding_timestamp")
        )
        .with_columns(
            (pl.col("timestamp") - pl.col("last_funding_timestamp"))
            .dt.total_minutes()
            .fill_null(max_funding_staleness_minutes + 1)
            .alias("funding_staleness_minutes")
        )
        .with_columns(
            (pl.col("funding_staleness_minutes") > max_funding_staleness_minutes)
            .alias("stale_funding_data")
        )
    )

    return enriched
