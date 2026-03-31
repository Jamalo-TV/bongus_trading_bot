"""Backtest data-quality checks shared by research scripts."""

from dataclasses import dataclass

import polars as pl


@dataclass(frozen=True)
class DataQualityReport:
    ok: bool
    issues: list[str]
    max_gap_minutes: float
    funding_null_ratio: float


def _float_or_zero(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def validate_market_data(
    df: pl.DataFrame, *, max_allowed_gap_minutes: float
) -> DataQualityReport:
    issues: list[str] = []
    if df.is_empty():
        issues.append("market data is empty")
        return DataQualityReport(
            ok=False,
            issues=issues,
            max_gap_minutes=0.0,
            funding_null_ratio=1.0,
        )

    max_gap_minutes_seen = 0.0
    if "timestamp" in df.columns:
        gap_series = df.select(
            (
                pl.col("timestamp")
                .diff()
                .dt.total_seconds()
                .truediv(60.0)
                .fill_null(0.0)
                .alias("gap_minutes")
            )
        )["gap_minutes"]
        max_gap_minutes_seen = _float_or_zero(gap_series.max())
        if max_gap_minutes_seen > max_allowed_gap_minutes:
            issues.append(
                f"max timestamp gap {max_gap_minutes_seen:.2f}m exceeds {max_allowed_gap_minutes:.2f}m"
            )
    else:
        issues.append("missing timestamp column")

    funding_null_ratio = 0.0
    if "funding_rate" in df.columns:
        funding_null_ratio = _float_or_zero(df["funding_rate"].is_null().mean())
        if funding_null_ratio > 0.0:
            issues.append(f"funding_rate contains {funding_null_ratio:.1%} null rows")
    else:
        issues.append("missing funding_rate column")

    return DataQualityReport(
        ok=not issues,
        issues=issues,
        max_gap_minutes=max_gap_minutes_seen,
        funding_null_ratio=funding_null_ratio,
    )


def add_funding_freshness_flags(
    df: pl.DataFrame, *, max_funding_staleness_minutes: float
) -> pl.DataFrame:
    if "timestamp" not in df.columns:
        return df

    if "funding_snapshot" not in df.columns:
        return df.with_columns(
            pl.lit(0.0).alias("funding_staleness_minutes"),
            pl.lit(False).alias("funding_is_stale"),
        )

    return (
        df.with_columns(
            pl.when(pl.col("funding_snapshot"))
            .then(pl.col("timestamp"))
            .otherwise(None)
            .forward_fill()
            .alias("_last_funding_snapshot_ts")
        )
        .with_columns(
            (
                (pl.col("timestamp") - pl.col("_last_funding_snapshot_ts"))
                .dt.total_seconds()
                .truediv(60.0)
                .fill_null(0.0)
            ).alias("funding_staleness_minutes")
        )
        .with_columns(
            (
                pl.col("funding_staleness_minutes") > max_funding_staleness_minutes
            ).alias("funding_is_stale")
        )
        .drop("_last_funding_snapshot_ts")
    )
