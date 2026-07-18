"""Per-trade and portfolio-level analytics."""

import polars as pl

from bongus.core.config import NOTIONAL_PER_TRADE
from bongus.engine import cost_model


def compute_trade_summary(
    df: pl.DataFrame,
    *,
    gross_notional_usd: float = NOTIONAL_PER_TRADE,
) -> pl.DataFrame:
    """
    Group the strategy DataFrame by trade_id and compute per-trade metrics.

    Returns a DataFrame with one row per trade.
    """
    if gross_notional_usd <= 0:
        raise ValueError("gross_notional_usd must be positive")

    # Keep only rows that are part of a trade.  Causal strategy output has an
    # explicit terminal marker, so unrealized/open trades are excluded from
    # realized performance rather than pretending the final data row is an exit.
    trades = df.filter(pl.col("trade_id") > 0)

    if "exit_filled" in trades.columns and not trades.is_empty():
        completed_ids = (
            trades.filter(pl.col("exit_filled"))
            .select("trade_id")
            .unique()
        )
        trades = trades.join(completed_ids, on="trade_id", how="semi")

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
            "funding_yield_perp_pct": pl.Series([], dtype=pl.Float64),
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

        # Funding is paid on the perp leg. Preserve that native one-leg return
        # before normalizing every contribution to combined pair gross.
        pl.col("cumulative_yield").last().alias("funding_yield_perp_pct"),
    ).sort("trade_id")

    # ── Derived columns ──────────────────────────────────────────────────
    # NOTIONAL_PER_TRADE is combined spot+perp gross.  Each matched leg carries
    # half of it.  The cost model returns the *sum* of spot and perp leg rates,
    # so divide that sum by two to express fees on combined pair gross.
    per_leg_notional_usd = gross_notional_usd / 2.0
    summed_leg_rt_cost = cost_model.blended_round_trip_cost_pct(
        size_usd=per_leg_notional_usd
    )
    pair_gross_rt_cost = summed_leg_rt_cost / 2.0

    summary = summary.with_columns(
        # Duration in hours
        (
            (pl.col("exit_time") - pl.col("entry_time"))
            .dt.total_minutes()
            / 60.0
        ).alias("duration_hours"),

        # Basis PnL on a gross-capital basis: total dollar spread PnL divided by
        # the combined spot+perp entry notional. This avoids double-counting the
        # same matched quantity as two separate returns.
        (
            (
                (pl.col("spot_exit_price") - pl.col("spot_entry_price"))
                + (pl.col("perp_entry_price") - pl.col("perp_exit_price"))
            )
            / (pl.col("spot_entry_price") + pl.col("perp_entry_price"))
        ).alias("basis_pnl_pct"),

        # Funding is earned only on the perp half of combined gross.
        (pl.col("funding_yield_perp_pct") / 2.0).alias("gross_yield_pct"),

        pl.lit(pair_gross_rt_cost).alias("fees_pct"),
    )

    summary = summary.with_columns(
        (
            pl.col("gross_yield_pct") + pl.col("basis_pnl_pct") - pl.col("fees_pct")
        ).alias("net_pnl_pct"),
    )

    summary = summary.with_columns(
        (pl.col("net_pnl_pct") * gross_notional_usd).alias("net_pnl_usd"),

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
