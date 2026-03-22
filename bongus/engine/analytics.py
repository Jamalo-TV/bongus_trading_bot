"""
Per-trade and portfolio-level analytics.

Takes the annotated DataFrame from strategy.run_strategy() and produces:
  1. A per-trade summary table (gross yield, basis PnL, fees, net PnL, duration)
  2. Aggregate portfolio statistics (win rate, avg return, capital efficiency)
"""

import cost_model
import polars as pl

from config import NOTIONAL_PER_TRADE


def compute_trade_summary(df: pl.DataFrame) -> pl.DataFrame:
    """
    Group the strategy DataFrame by trade_id and compute per-trade metrics.

    Returns a DataFrame with one row per trade.
    """
    # Keep only rows that are part of a trade
    trades = df.filter(pl.col("trade_id") > 0)

    if trades.is_empty():
        return pl.DataFrame({
            "trade_id": pl.Series([], dtype=pl.Int64),
            "entry_time": pl.Series([], dtype=pl.Datetime("us", "UTC")),
            "exit_time": pl.Series([], dtype=pl.Datetime("us", "UTC")),
            "duration_hours": pl.Series([], dtype=pl.Float64),
            "spot_entry_price": pl.Series([], dtype=pl.Float64),
            "perp_entry_price": pl.Series([], dtype=pl.Float64),
            "spot_exit_price": pl.Series([], dtype=pl.Float64),
            "perp_exit_price": pl.Series([], dtype=pl.Float64),
            "gross_yield_pct": pl.Series([], dtype=pl.Float64),
            "basis_pnl_pct": pl.Series([], dtype=pl.Float64),
            "fees_pct": pl.Series([], dtype=pl.Float64),
            "net_pnl_pct": pl.Series([], dtype=pl.Float64),
            "net_pnl_usd": pl.Series([], dtype=pl.Float64),
            "annualized_return_pct": pl.Series([], dtype=pl.Float64),
        })

    summary = trades.group_by("trade_id").agg(
        # Timing
        pl.col("timestamp").first().alias("entry_time"),
        pl.col("timestamp").last().alias("exit_time"),

        # Entry / exit prices
        pl.col("spot_entry_price").first().alias("spot_entry_price"),
        pl.col("perp_entry_price").first().alias("perp_entry_price"),
        pl.col("spot_close").last().alias("spot_exit_price"),
        pl.col("perp_close").last().alias("perp_exit_price"),

        # Gross yield = total funding collected (as fraction)
        pl.col("cumulative_yield").last().alias("gross_yield_pct"),
    ).sort("trade_id")

    # ── Derived columns ──────────────────────────────────────────────────
    rt_cost = cost_model.round_trip_cost_pct()

    summary = summary.with_columns(
        # Duration in hours
        (
            (pl.col("exit_time") - pl.col("entry_time"))
            .dt.total_minutes()
            / 60.0
        ).alias("duration_hours"),

        # Basis PnL: change in the spread between entry and exit.
        # At entry you are long spot / short perp.
        # Spot PnL = (spot_exit - spot_entry) / spot_entry
        # Perp PnL = (perp_entry - perp_exit) / perp_entry  (short)
        (
            (pl.col("spot_exit_price") - pl.col("spot_entry_price"))
            / pl.col("spot_entry_price")
            + (pl.col("perp_entry_price") - pl.col("perp_exit_price"))
            / pl.col("perp_entry_price")
        ).alias("basis_pnl_pct"),

        # Fees as a fixed pct of notional
        pl.lit(rt_cost).alias("fees_pct"),
    )

    summary = summary.with_columns(
        (
            pl.col("gross_yield_pct") + pl.col("basis_pnl_pct") - pl.col("fees_pct")
        ).alias("net_pnl_pct"),
    )

    summary = summary.with_columns(
        (pl.col("net_pnl_pct") * NOTIONAL_PER_TRADE).alias("net_pnl_usd"),

        # Annualized return: scale the net PnL by how long capital was locked
        pl.when(pl.col("duration_hours") > 0)
        .then(
            pl.col("net_pnl_pct") / pl.col("duration_hours") * 8760.0  # hours/year
        )
        .otherwise(0.0)
        .alias("annualized_return_pct"),
    )

    return summary


def compute_portfolio_stats(trades: pl.DataFrame) -> dict:
    """
    Compute aggregate portfolio statistics from the per-trade summary.
    Returns a dict of key metrics.
    """
    if trades.is_empty() or trades.height == 0:
        return {
            "total_trades": 0,
            "winners": 0,
            "losers": 0,
            "win_rate": 0.0,
            "total_net_pnl_usd": 0.0,
            "avg_net_pnl_pct": 0.0,
            "median_net_pnl_pct": 0.0,
            "avg_duration_hours": 0.0,
            "avg_annualized_return_pct": 0.0,
            "best_trade_pct": 0.0,
            "worst_trade_pct": 0.0,
            "total_gross_yield_pct": 0.0,
            "total_fees_pct": 0.0,
        }

    total = trades.height
    winners = trades.filter(pl.col("net_pnl_pct") > 0).height
    losers = total - winners

    return {
        "total_trades": total,
        "winners": winners,
        "losers": losers,
        "win_rate": winners / total if total > 0 else 0.0,
        "total_net_pnl_usd": trades["net_pnl_usd"].sum(),
        "avg_net_pnl_pct": trades["net_pnl_pct"].mean(),
        "median_net_pnl_pct": trades["net_pnl_pct"].median(),
        "avg_duration_hours": trades["duration_hours"].mean(),
        "avg_annualized_return_pct": trades["annualized_return_pct"].mean(),
        "best_trade_pct": trades["net_pnl_pct"].max(),
        "worst_trade_pct": trades["net_pnl_pct"].min(),
        "total_gross_yield_pct": trades["gross_yield_pct"].sum(),
        "total_fees_pct": trades["fees_pct"].sum(),
    }
