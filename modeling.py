"""Regime-aware and uncertainty-aware edge modeling."""

from dataclasses import dataclass

import polars as pl


@dataclass
class RegimeStats:
    name: str
    count: int
    mean_edge: float
    std_edge: float


class RegimeAwareEdgeModel:
    def __init__(self) -> None:
        self.vol_threshold: float = 0.0
        self.trend_threshold: float = 0.0
        self.regime_stats: dict[str, RegimeStats] = {}
        self.fitted = False

    def fit(self, train_df: pl.DataFrame, target_col: str = "future_edge_target") -> None:
        required = {"spot_vol_annualized", "spot_trend_lookback", target_col}
        missing = required - set(train_df.columns)
        if missing:
            raise ValueError(f"Missing required columns for fit: {sorted(missing)}")

        self.vol_threshold = float(train_df["spot_vol_annualized"].drop_nulls().drop_nans().median() or 0.0)
        self.trend_threshold = float(train_df["spot_trend_lookback"].abs().drop_nulls().drop_nans().median() or 0.0)

        labeled = self._assign_regime(train_df)
        grouped = (
            labeled.group_by("regime")
            .agg(
                pl.len().alias("count"),
                pl.col(target_col).mean().fill_nan(0.0).fill_null(0.0).alias("mean_edge"),
                pl.col(target_col).std().fill_nan(0.0).fill_null(0.0).alias("std_edge"),
            )
            .sort("regime")
        )

        stats: dict[str, RegimeStats] = {}
        for row in grouped.iter_rows(named=True):
            name = str(row["regime"])
            stats[name] = RegimeStats(
                name=name,
                count=int(row["count"]),
                mean_edge=float(row["mean_edge"]),
                std_edge=float(row["std_edge"]),
            )

        self.regime_stats = stats
        self.fitted = True

    def predict(self, df: pl.DataFrame) -> pl.DataFrame:
        if not self.fitted:
            raise RuntimeError("Model must be fit before predict")

        labeled = self._assign_regime(df)
        means = {k: v.mean_edge for k, v in self.regime_stats.items()}
        stds = {k: max(v.std_edge, 1e-8) for k, v in self.regime_stats.items()}

        predicted = labeled.with_columns(
            pl.col("regime").replace_strict(means, default=0.0, return_dtype=pl.Float64).alias("expected_edge"),
            pl.col("regime").replace_strict(stds, default=1.0, return_dtype=pl.Float64).alias("edge_uncertainty"),
        ).with_columns(
            (pl.col("expected_edge") / pl.col("edge_uncertainty")).alias("signal_to_noise")
        )

        return predicted

    def _assign_regime(self, df: pl.DataFrame) -> pl.DataFrame:
        if {"spot_vol_annualized", "spot_trend_lookback"} - set(df.columns):
            raise ValueError("Input must contain spot_vol_annualized and spot_trend_lookback")

        return df.with_columns(
            pl.when(
                (pl.col("spot_vol_annualized") >= self.vol_threshold)
                & (pl.col("spot_trend_lookback").abs() >= self.trend_threshold)
            )
            .then(pl.lit("high_vol_trending"))
            .when(
                (pl.col("spot_vol_annualized") >= self.vol_threshold)
                & (pl.col("spot_trend_lookback").abs() < self.trend_threshold)
            )
            .then(pl.lit("high_vol_mean_reverting"))
            .when(
                (pl.col("spot_vol_annualized") < self.vol_threshold)
                & (pl.col("spot_trend_lookback").abs() >= self.trend_threshold)
            )
            .then(pl.lit("low_vol_trending"))
            .otherwise(pl.lit("low_vol_mean_reverting"))
            .alias("regime")
        )
