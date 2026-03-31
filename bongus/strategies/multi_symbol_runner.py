"""Run the funding-arb strategy across multiple symbols independently.

Each symbol gets its own signal generation pass. Results are combined into
a single DataFrame with a 'symbol' column for downstream risk/analytics.
"""

from pathlib import Path

import polars as pl
from strategy import run_strategy


def run_multi_symbol(
    symbol_data: dict[str, pl.DataFrame],
    features_by_symbol: dict[str, pl.DataFrame] | None = None,
) -> pl.DataFrame:
    """Run strategy per symbol and combine results.

    Parameters
    ----------
    symbol_data : dict mapping symbol name → aligned DataFrame
        Each value must have columns: timestamp, spot_close, perp_close,
        funding_rate, funding_snapshot.
    features_by_symbol : optional dict mapping symbol name → features DataFrame

    Returns
    -------
    Combined DataFrame with an added 'symbol' column and unique trade_id
    values across all symbols.
    """
    results: list[pl.DataFrame] = []
    trade_id_offset = 0

    for symbol, df in sorted(symbol_data.items()):
        features = (features_by_symbol or {}).get(symbol)
        result = run_strategy(df, features)

        max_trade = result["trade_id"].max()
        if max_trade is None:
            max_trade = 0

        # Offset trade_ids so they're globally unique
        result = result.with_columns(
            pl.lit(symbol).alias("symbol"),
            (pl.col("trade_id") + trade_id_offset).alias("trade_id"),
        )

        trade_id_offset += max_trade
        results.append(result)

    if not results:
        raise ValueError("No symbol data provided")

    return pl.concat(results).sort("timestamp", "symbol")


def load_multi_symbol(
    data_dir: str | Path,
    symbols: list[str],
) -> dict[str, pl.DataFrame]:
    """Load spot/perp/funding data for multiple symbols from a directory.

    Expects files named: {SYMBOL}_spot_1m.parquet, {SYMBOL}_perp_1m.parquet,
    {SYMBOL}_funding.parquet.
    """
    from bongus.market_data.data_loader import load_data

    data_dir = Path(data_dir)
    symbol_data: dict[str, pl.DataFrame] = {}

    for sym in symbols:
        spot_path = data_dir / f"{sym}_spot_1m.parquet"
        perp_path = data_dir / f"{sym}_perp_1m.parquet"
        funding_path = data_dir / f"{sym}_funding.parquet"

        if spot_path.exists() and perp_path.exists() and funding_path.exists():
            symbol_data[sym] = load_data(str(spot_path), str(perp_path), str(funding_path))

    return symbol_data
