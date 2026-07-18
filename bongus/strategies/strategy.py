"""Causal strategy kernel for delta-neutral funding arbitrage research.

Signal and feature calculations are vectorized.  The small position lifecycle
loop is intentional: it makes event ordering explicit and auditable.
"""

from dataclasses import dataclass

import polars as pl

from bongus.core.config import (
    BASIS_DEVIATION_STOP,
    ENTRY_ANN_FUNDING_THRESHOLD,
    ENTRY_PREMIUM_THRESHOLD,
    EXIT_ANN_FUNDING_THRESHOLD,
    EXIT_DISCOUNT_THRESHOLD,
    FUNDING_CAPTURE_DELAY_MIN,
    FUNDING_PERIODS_PER_YEAR,
    FUNDING_SNAPSHOT_HOURS,
    HOLD_THROUGH_FUNDING,
    SNAPSHOT_SNIPE_ENABLED,
    SNIPE_ANN_FUNDING_THRESHOLD,
    SNIPE_ENTRY_WINDOW_MAX,
    SNIPE_ENTRY_WINDOW_MIN,
)
from bongus.strategies.opportunity_adapters import (
    apply_replay_settlement_cashflows,
)


@dataclass(frozen=True, slots=True)
class StrategyParameters:
    """Versionable parameters consumed by the strategy kernel."""

    entry_ann_funding_threshold: float = ENTRY_ANN_FUNDING_THRESHOLD
    entry_premium_threshold: float = ENTRY_PREMIUM_THRESHOLD
    exit_ann_funding_threshold: float = EXIT_ANN_FUNDING_THRESHOLD
    exit_discount_threshold: float = EXIT_DISCOUNT_THRESHOLD
    basis_deviation_stop: float = BASIS_DEVIATION_STOP


def _compute_derived_metrics(
    df: pl.DataFrame,
    features: pl.DataFrame | None,
) -> tuple[pl.DataFrame, bool, bool, bool]:
    df = df.with_columns(
        (pl.col("funding_rate") * FUNDING_PERIODS_PER_YEAR).alias(
            "annualized_funding"
        ),
        ((pl.col("perp_close") - pl.col("spot_close")) / pl.col("spot_close")).alias(
            "basis_premium_pct"
        ),
    ).with_columns(
        pl.col("annualized_funding")
        .diff(n=12)
        .fill_null(0.0)
        .alias("funding_velocity"),
    )

    # Polars extracts hour/minute as Int8.  Cast before multiplying: Int8
    # arithmetic wraps after 127 (02:08 used to become -128 minutes).
    df = df.with_columns(
        pl.col("timestamp").dt.hour().cast(pl.Int32).alias("_hour"),
        pl.col("timestamp").dt.minute().cast(pl.Int32).alias("_minute"),
    ).with_columns(
        (pl.col("_hour") * 60 + pl.col("_minute")).alias("_minute_of_day"),
    )

    snapshot_hours = sorted(FUNDING_SNAPSHOT_HOURS)
    minutes_to_snapshot = (
        pl.lit((snapshot_hours[0] + 24) * 60) - pl.col("_minute_of_day")
    )
    for snapshot_hour in reversed(snapshot_hours):
        snapshot_minute = snapshot_hour * 60
        minutes_to_snapshot = (
            pl.when(pl.col("_minute_of_day") < snapshot_minute)
            .then(snapshot_minute - pl.col("_minute_of_day"))
            .otherwise(minutes_to_snapshot)
        )
    df = df.with_columns(
        minutes_to_snapshot.alias("minutes_to_next_snapshot"),
    )

    has_zscore = False
    has_momentum = False
    has_obi = False
    if features is not None:
        join_cols = ["timestamp"]
        if "basis_zscore" in features.columns:
            join_cols.append("basis_zscore")
            has_zscore = True
        if "funding_momentum" in features.columns:
            join_cols.append("funding_momentum")
            has_momentum = True
        if "order_book_imbalance" in features.columns:
            join_cols.append("order_book_imbalance")
            has_obi = True

        if len(join_cols) > 1:
            df = df.join(features.select(join_cols), on="timestamp", how="left")
            for column, present in (
                ("basis_zscore", has_zscore),
                ("funding_momentum", has_momentum),
                ("order_book_imbalance", has_obi),
            ):
                if present:
                    df = df.with_columns(pl.col(column).fill_null(0.0))

    return df, has_zscore, has_momentum, has_obi


def _compute_raw_signals(
    df: pl.DataFrame,
    has_zscore: bool,
    has_momentum: bool = False,
    has_obi: bool = False,
    parameters: StrategyParameters | None = None,
) -> pl.DataFrame:
    parameters = parameters or StrategyParameters()
    entry_expr = (
        (pl.col("annualized_funding") > parameters.entry_ann_funding_threshold)
        & (pl.col("basis_premium_pct") > parameters.entry_premium_threshold)
        & (pl.col("funding_velocity") >= 0.0)
        & (pl.col("minutes_to_next_snapshot") > 15)
    )
    if has_zscore:
        entry_expr = entry_expr & (pl.col("basis_zscore") < 2.0)
    if has_momentum:
        entry_expr = entry_expr & (pl.col("funding_momentum") > 0.0)
    if has_obi:
        entry_expr = entry_expr & (pl.col("order_book_imbalance") > 0.1)

    snipe_entry_expr = (
        (pl.col("annualized_funding") > SNIPE_ANN_FUNDING_THRESHOLD)
        & (pl.col("basis_premium_pct") > parameters.entry_premium_threshold)
        & (pl.col("minutes_to_next_snapshot") >= SNIPE_ENTRY_WINDOW_MIN)
        & (pl.col("minutes_to_next_snapshot") <= SNIPE_ENTRY_WINDOW_MAX)
        & pl.lit(SNAPSHOT_SNIPE_ENABLED)
    )

    just_after_snapshot = (
        pl.col("minutes_to_next_snapshot") > (8 * 60 - FUNDING_CAPTURE_DELAY_MIN)
    ) & (
        pl.col("minutes_to_next_snapshot").shift(1) <= FUNDING_CAPTURE_DELAY_MIN
    )

    exit_condition = (
        (pl.col("annualized_funding") < parameters.exit_ann_funding_threshold)
        | (pl.col("basis_premium_pct") < parameters.exit_discount_threshold)
    )
    if HOLD_THROUGH_FUNDING:
        exit_condition = exit_condition & ~just_after_snapshot
    exit_condition = exit_condition | (
        (pl.col("minutes_to_next_snapshot") > (8 * 60 - 5))
        & (pl.col("funding_velocity") < 0.0)
    )

    return df.with_columns(
        (entry_expr | snipe_entry_expr).alias("raw_entry"),
        exit_condition.alias("raw_exit"),
        pl.lit(False).alias("inverse_signal"),
    )


def _compute_position_state(
    df: pl.DataFrame,
    parameters: StrategyParameters,
    *,
    force_close_at_end: bool = False,
) -> pl.DataFrame:
    """Apply signals using a conservative, causal event-time convention.

    A signal observed on row ``i`` becomes a pending order which may fill only
    at row ``i + 1``. Funding at a row is eligible only when the position was
    already open before that row, so an entry filled on a settlement row never
    receives that settlement. Settlement is ordered before an exit fill at the
    same timestamp.

    For long spot / short perp, only basis widening is adverse. ``in_position``
    remains true on the exit-fill row so its quote is included in the trade;
    ``exit_filled`` is the authoritative post-fill terminal marker.
    """
    raw_entry = [bool(value) for value in df["raw_entry"].to_list()]
    raw_exit = [bool(value) for value in df["raw_exit"].to_list()]
    spot_prices = [float(value) for value in df["spot_close"].to_list()]
    perp_prices = [float(value) for value in df["perp_close"].to_list()]
    basis_values = [float(value) for value in df["basis_premium_pct"].to_list()]
    row_count = len(df)

    in_position = [False] * row_count
    trade_id = [0] * row_count
    spot_entry_price: list[float | None] = [None] * row_count
    perp_entry_price: list[float | None] = [None] * row_count
    entry_filled = [False] * row_count
    exit_filled = [False] * row_count
    forced_exit = [False] * row_count
    funding_eligible = [False] * row_count
    basis_stop_triggered = [False] * row_count
    effective_exit = list(raw_exit)

    current_trade = 0
    active_trade = 0
    currently_in = False
    pending_entry = False
    pending_exit = False
    entry_armed = True
    active_spot_entry: float | None = None
    active_perp_entry: float | None = None
    active_entry_basis: float | None = None

    for index in range(row_count):
        # Funding settles before orders executable at this row.  This is
        # conservative for new entries and deterministic for exit fills.
        funding_eligible[index] = currently_in
        exited_this_row = False

        if pending_exit and currently_in:
            in_position[index] = True
            trade_id[index] = active_trade
            spot_entry_price[index] = active_spot_entry
            perp_entry_price[index] = active_perp_entry
            exit_filled[index] = True
            exited_this_row = True
            currently_in = False
            pending_exit = False
        elif pending_entry and not currently_in:
            current_trade += 1
            active_trade = current_trade
            currently_in = True
            pending_entry = False
            active_spot_entry = spot_prices[index]
            active_perp_entry = perp_prices[index]
            active_entry_basis = (
                (active_perp_entry - active_spot_entry) / active_spot_entry
            )
            entry_filled[index] = True

        if currently_in:
            in_position[index] = True
            trade_id[index] = active_trade
            spot_entry_price[index] = active_spot_entry
            perp_entry_price[index] = active_perp_entry

            adverse_basis_move = (
                active_entry_basis is not None
                and basis_values[index] - active_entry_basis
                > parameters.basis_deviation_stop
            )
            if adverse_basis_move:
                basis_stop_triggered[index] = True
                effective_exit[index] = True
            if effective_exit[index]:
                pending_exit = True
        elif exited_this_row:
            # Never flip on an exit-fill row. Persistent entry conditions stay
            # disarmed until a false observation rearms the entry edge.
            if not raw_entry[index]:
                entry_armed = True
        else:
            if not raw_entry[index]:
                entry_armed = True
            elif entry_armed and not effective_exit[index]:
                pending_entry = True
                entry_armed = False

        if exited_this_row:
            active_trade = 0
            active_spot_entry = None
            active_perp_entry = None
            active_entry_basis = None

    # Walk-forward windows have a predeclared liquidation boundary.  Realize an
    # already-open trade at its final quote; do not round-trip an entry first
    # filled on that same final row.
    if (
        force_close_at_end
        and row_count > 0
        and currently_in
        and in_position[-1]
        and not entry_filled[-1]
    ):
        exit_filled[-1] = True
        forced_exit[-1] = True

    return df.with_columns(
        pl.Series("in_position", in_position),
        pl.Series("trade_id", trade_id),
        pl.Series("spot_entry_price", spot_entry_price, dtype=pl.Float64),
        pl.Series("perp_entry_price", perp_entry_price, dtype=pl.Float64),
        pl.Series("entry_filled", entry_filled),
        pl.Series("exit_filled", exit_filled),
        pl.Series("forced_exit", forced_exit),
        pl.Series("funding_eligible", funding_eligible),
        pl.Series("basis_stop_triggered", basis_stop_triggered),
        pl.Series("raw_exit", effective_exit),
    )


def _accrue_funding_yield(df: pl.DataFrame) -> pl.DataFrame:
    # Funding is a discrete perp-leg cash flow.  The canonical replay adapter
    # deliberately does not prorate it by elapsed time or synthesize spot
    # borrow for the long-spot/short-perp route.
    df = apply_replay_settlement_cashflows(df).with_columns(
        pl.col("_funding_accrual")
        .cum_sum()
        .over("trade_id")
        .alias("cumulative_yield"),
    )
    return df.with_columns(
        pl.when(pl.col("trade_id") > 0)
        .then(pl.col("cumulative_yield"))
        .otherwise(0.0)
        .alias("cumulative_yield"),
    )


def run_strategy(
    df: pl.DataFrame,
    features: pl.DataFrame | None = None,
    parameters: StrategyParameters | None = None,
    *,
    force_close_at_end: bool = False,
) -> pl.DataFrame:
    """Annotate market data with causal signals, fills, state and funding.

    Expected columns are ``timestamp``, ``spot_close``, ``perp_close``,
    ``funding_rate`` and ``funding_snapshot``.  Funding rates remain per
    settlement and are annualized with ``FUNDING_PERIODS_PER_YEAR``.
    """
    required = {
        "timestamp",
        "spot_close",
        "perp_close",
        "funding_rate",
        "funding_snapshot",
    }
    missing = required.difference(df.columns)
    if missing:
        raise ValueError(f"strategy data missing columns: {sorted(missing)}")
    if df["timestamp"].null_count() or not df["timestamp"].is_sorted():
        raise ValueError("strategy timestamps must be non-null and sorted")
    if df["timestamp"].n_unique() != df.height:
        raise ValueError("strategy timestamps must be unique")
    if features is not None and (
        "timestamp" not in features.columns
        or features["timestamp"].null_count()
        or features["timestamp"].n_unique() != features.height
    ):
        raise ValueError("feature timestamps must be present, non-null and unique")

    parameters = parameters or StrategyParameters()
    df, has_zscore, has_momentum, has_obi = _compute_derived_metrics(df, features)
    df = _compute_raw_signals(
        df,
        has_zscore,
        has_momentum,
        has_obi,
        parameters,
    )
    df = _compute_position_state(
        df,
        parameters,
        force_close_at_end=force_close_at_end,
    )
    df = _accrue_funding_yield(df)
    return df.drop(
        "_funding_accrual",
        "_hour",
        "_minute",
        "_minute_of_day",
    )
