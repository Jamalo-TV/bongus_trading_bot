import itertools
from typing import Any, Dict, List

import polars as pl
from rich.console import Console
from rich.table import Table

import data_loader
import strategy
from analytics import compute_portfolio_stats, compute_trade_summary

console = Console()


def load_historical_data() -> pl.DataFrame | None:
    """Loads historical datasets needed for the optimizer."""
    console.print("[bold cyan]Loading historical datasets... (This may take a moment)[/bold cyan]")
    try:
        df = data_loader.load_data(
            "data/spot_1m.parquet",
            "data/perp_1m.parquet",
            "data/funding_rates.parquet",
        )
        console.print(f"Data loaded successfully. Rows: {len(df):,}")
        return df
    except FileNotFoundError:
        console.print("[bold red]Data files not found. Make sure you have downloaded Binance data![/bold red]")
        return None


def generate_parameter_grid() -> List[Dict[str, float]]:
    """Generates the parameter combinations to search over."""
    param_grid = {
        "ENTRY_ANN_FUNDING_THRESHOLD": [0.10, 0.15, 0.20, 0.25, 0.30, 0.45],  # Annualized yield req. to enter
        "ENTRY_PREMIUM_THRESHOLD": [0.0001, 0.0002, 0.0004, 0.0006],        # % perp must trace above spot
        "EXIT_ANN_FUNDING_THRESHOLD": [0.00, 0.05, 0.08],                     # Exit if yield drops below this
        "EXIT_DISCOUNT_THRESHOLD": [0.00, -0.0001, -0.0005],                  # Exit if perp deeply discounts
    }
    keys, values = zip(*param_grid.items())
    combinations = [dict(zip(keys, v)) for v in itertools.product(*values)]
    return combinations


def evaluate_parameters(df: pl.DataFrame, combinations: List[Dict[str, float]]) -> List[Dict[str, Any]]:
    """Evaluates the strategy logic for each parameter combination and computes stats."""
    console.print(f"[bold yellow]Beginning grid search over {len(combinations)} parameter combinations...[/bold yellow]")
    results = []

    for idx, params in enumerate(combinations):
        # Dynamically inject the parameters into the strategy module
        strategy.ENTRY_ANN_FUNDING_THRESHOLD = params["ENTRY_ANN_FUNDING_THRESHOLD"]
        strategy.ENTRY_PREMIUM_THRESHOLD = params["ENTRY_PREMIUM_THRESHOLD"]
        strategy.EXIT_ANN_FUNDING_THRESHOLD = params["EXIT_ANN_FUNDING_THRESHOLD"]
        strategy.EXIT_DISCOUNT_THRESHOLD = params["EXIT_DISCOUNT_THRESHOLD"]

        # Run the strategy logic
        # Make sure to copy the dataframe to prevent column collisions/accumulation
        sim_df = df.clone()
        annotated_df = strategy.run_strategy(sim_df)

        # Calculate PnL stats
        trades_df = compute_trade_summary(annotated_df)
        stats = compute_portfolio_stats(trades_df)

        # Record valid strategies
        res = {**params, **stats}
        results.append(res)

        if idx > 0 and idx % 25 == 0:
            console.print(f"  ...processed {idx}/{len(combinations)} combinations")

    return results


def filter_and_sort_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Filters results by win rate and trade count, and sorts by Net PnL."""
    # Sort results by absolute Best Net PnL string (Win Rate must optionally be decent > 50%)
    good_results = [r for r in results if r["win_rate"] > 0.40 and r["total_trades"] > 5]

    # If no results survived that filter, just sort by PnL
    if not good_results:
        good_results = results

    good_results.sort(key=lambda x: x["total_net_pnl_usd"], reverse=True)
    return good_results


def display_top_results(good_results: List[Dict[str, Any]]) -> None:
    """Displays the top 5 strategy configurations and prints the best one."""
    if not good_results:
        console.print("[bold red]No valid results to display.[/bold red]")
        return

    table = Table(title="Top 5 Best Strategy Configurations")
    table.add_column("Rank", justify="center", style="cyan", no_wrap=True)
    table.add_column("Entry Yield / Prem", justify="left", style="magenta")
    table.add_column("Exit Yield / Disc", justify="left", style="magenta")
    table.add_column("Net PnL ($)", justify="right", style="green")
    table.add_column("Win Rate", justify="right", style="yellow")
    table.add_column("Trades", justify="right", style="blue")

    for i, r in enumerate(good_results[:5]):
        entry_str = f"> {r['ENTRY_ANN_FUNDING_THRESHOLD']:.0%} / > {r['ENTRY_PREMIUM_THRESHOLD']:.2%}"
        exit_str = f"< {r['EXIT_ANN_FUNDING_THRESHOLD']:.0%} / < {r['EXIT_DISCOUNT_THRESHOLD']:.2%}"
        pnl_str = f"${r['total_net_pnl_usd']:,.2f}"
        win_rate = f"{r['win_rate']:.1%}"
        trades = str(r['total_trades'])

        table.add_row(str(i+1), entry_str, exit_str, pnl_str, win_rate, trades)

    console.print(table)

    best = good_results[0]
    console.print("\n[bold green]✅ BEST PARAMETERS FOUND![/bold green]")
    console.print("Consider updating `config.py` with:")
    console.print(f"ENTRY_ANN_FUNDING_THRESHOLD = {best['ENTRY_ANN_FUNDING_THRESHOLD']}")
    console.print(f"ENTRY_PREMIUM_THRESHOLD = {best['ENTRY_PREMIUM_THRESHOLD']}")
    console.print(f"EXIT_ANN_FUNDING_THRESHOLD = {best['EXIT_ANN_FUNDING_THRESHOLD']}")
    console.print(f"EXIT_DISCOUNT_THRESHOLD = {best['EXIT_DISCOUNT_THRESHOLD']}")


def run_optimizer() -> None:
    """
    Runs a grid search over key hyperparameters and displays the best results.
    """
    df = load_historical_data()
    if df is None:
        return

    combinations = generate_parameter_grid()
    results = evaluate_parameters(df, combinations)
    good_results = filter_and_sort_results(results)
    display_top_results(good_results)


if __name__ == "__main__":
    run_optimizer()
