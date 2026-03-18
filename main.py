"""
CLI entrypoint for the Delta-Neutral Funding Arbitrage Backtester.

Usage:
    python main.py                              # uses data/ defaults
    python main.py --spot data/spot_1m.parquet   # custom paths
"""

import argparse
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Add project root to sys.path to avoid ImportError when run from outside the dir
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import polars as pl

from data_loader import load_data
from data_quality import validate_market_data, add_funding_freshness_flags
from walk_forward import run_walk_forward_validation, AcceptanceGates
from execution_alpha import OrderIntent, VenueQuote, route_order
from risk_engine import RiskEngine, RiskLimits, RiskState
from strategy import run_strategy
from analytics import compute_trade_summary, compute_portfolio_stats
from cost_model import round_trip_cost_pct
from config import (
    ENTRY_ANN_FUNDING_THRESHOLD,
    EXIT_ANN_FUNDING_THRESHOLD,
    ENTRY_PREMIUM_THRESHOLD,
    NOTIONAL_PER_TRADE,
    MAX_ALLOWED_GAP_MINUTES,
    MAX_FUNDING_STALENESS_MINUTES,
    MAX_GROSS_EXPOSURE_USD,
    MAX_SYMBOL_CONCENTRATION,
    MAX_DRAWDOWN_PCT,
    MAX_VENUE_LATENCY_MS,
    WF_MIN_AVG_OOS_EDGE,
    WF_MIN_WINDOWS_PASSING,
    WF_MIN_TRADES_PER_WINDOW,
    WF_MIN_SIGNAL_TO_NOISE,
)

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


def _ensure_data() -> tuple[str, str, str]:
    """Return paths, generating sample data if necessary."""
    spot = os.path.join(DATA_DIR, "spot_1m.parquet")
    perp = os.path.join(DATA_DIR, "perp_1m.parquet")
    funding = os.path.join(DATA_DIR, "funding_rates.parquet")

    if not all(os.path.exists(p) for p in (spot, perp, funding)):
        print("Data files not found - generating synthetic data ...")
        from generate_sample_data import main as gen_main
        gen_main()

    return spot, perp, funding


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Delta-Neutral Funding Arbitrage Backtester"
    )
    parser.add_argument("--spot", default=None, help="Path to spot 1m parquet")
    parser.add_argument("--perp", default=None, help="Path to perp 1m parquet")
    parser.add_argument("--funding", default=None, help="Path to funding rates parquet")
    parser.add_argument(
        "--enhanced-report",
        action="store_true",
        help="Run data-quality, risk, execution, and walk-forward diagnostics",
    )
    args = parser.parse_args()

    # ── Load data ────────────────────────────────────────────────────────
    if args.spot and args.perp and args.funding:
        spot_path, perp_path, funding_path = args.spot, args.perp, args.funding
    else:
        spot_path, perp_path, funding_path = _ensure_data()

    print("=" * 72)
    print("  DELTA-NEUTRAL FUNDING ARBITRAGE BACKTESTER")
    print("=" * 72)

    print("\n> Loading & aligning data ...")
    df = load_data(spot_path, perp_path, funding_path)
    print(f"  Aligned timeline: {df.height:,} rows "
          f"({df['timestamp'].min()} -> {df['timestamp'].max()})")

    if args.enhanced_report:
        dq_report = validate_market_data(df, max_allowed_gap_minutes=MAX_ALLOWED_GAP_MINUTES)
        print("\n> Data quality checks ...")
        print(f"  Data quality status: {'PASS' if dq_report.ok else 'FAIL'}")
        if dq_report.issues:
            for issue in dq_report.issues:
                print(f"  - {issue}")
        df = add_funding_freshness_flags(
            df,
            max_funding_staleness_minutes=MAX_FUNDING_STALENESS_MINUTES,
        )

    # ── Run strategy ─────────────────────────────────────────────────────
    print("\n> Running strategy ...")
    print(f"  Entry: ann. funding > {ENTRY_ANN_FUNDING_THRESHOLD:.0%}, "
          f"premium > {ENTRY_PREMIUM_THRESHOLD:.2%}")
    print(f"  Exit:  ann. funding < {EXIT_ANN_FUNDING_THRESHOLD:.0%} "
          f"or perp at discount")
    print(f"  Round-trip cost: {round_trip_cost_pct():.2%}")

    df = run_strategy(df)

    num_trades = df.filter(pl.col("trade_id") > 0).select(
        pl.col("trade_id").n_unique()
    ).item()
    print(f"  Trades identified: {num_trades}")

    # ── Trade summary ────────────────────────────────────────────────────
    print("\n> Computing trade summaries ...")
    trade_summary = compute_trade_summary(df)

    if trade_summary.is_empty():
        print("\n  WARNING: No trades were generated. Consider loosening entry thresholds.")
        sys.exit(0)

    # Pretty-print the trade table
    print("\n" + "-" * 72)
    print("  PER-TRADE SUMMARY")
    print("-" * 72)

    display_cols = trade_summary.select(
        "trade_id",
        pl.col("entry_time").dt.strftime("%Y-%m-%d %H:%M"),
        pl.col("exit_time").dt.strftime("%Y-%m-%d %H:%M"),
        pl.col("duration_hours").round(1),
        (pl.col("gross_yield_pct") * 100).round(4).alias("yield_%"),
        (pl.col("basis_pnl_pct") * 100).round(4).alias("basis_%"),
        (pl.col("fees_pct") * 100).round(4).alias("fees_%"),
        (pl.col("net_pnl_pct") * 100).round(4).alias("net_%"),
        pl.col("net_pnl_usd").round(2).alias("net_$"),
        (pl.col("annualized_return_pct") * 100).round(1).alias("ann_ret_%"),
    )

    # Polars default print is clean enough for a terminal
    pl.Config.set_tbl_cols(15)
    pl.Config.set_tbl_width_chars(120)
    print(display_cols)

    # ── Portfolio stats ──────────────────────────────────────────────────
    stats = compute_portfolio_stats(trade_summary)

    print("\n" + "-" * 72)
    print("  PORTFOLIO SUMMARY")
    print("-" * 72)
    print(f"  Total trades        : {stats['total_trades']}")
    print(f"  Winners / Losers    : {stats['winners']} / {stats['losers']}")
    print(f"  Win rate            : {stats['win_rate']:.1%}")
    print(f"  Total net PnL       : ${stats['total_net_pnl_usd']:,.2f}  "
          f"(on ${NOTIONAL_PER_TRADE:,.0f} notional per trade)")
    print(f"  Avg net PnL         : {stats['avg_net_pnl_pct'] * 100:.4f}%")
    print(f"  Median net PnL      : {stats['median_net_pnl_pct'] * 100:.4f}%")
    print(f"  Best trade          : {stats['best_trade_pct'] * 100:.4f}%")
    print(f"  Worst trade         : {stats['worst_trade_pct'] * 100:.4f}%")
    print(f"  Avg duration        : {stats['avg_duration_hours']:.1f} hours")
    print(f"  Avg ann. return     : {stats['avg_annualized_return_pct'] * 100:.1f}%")
    print(f"  Total gross yield   : {stats['total_gross_yield_pct'] * 100:.4f}%")
    print(f"  Total fees          : {stats['total_fees_pct'] * 100:.4f}%")
    print("-" * 72)

    if args.enhanced_report:
        print("\n> Enhanced research diagnostics ...")

        wf = run_walk_forward_validation(
            df,
            gates=AcceptanceGates(
                min_avg_oos_edge=WF_MIN_AVG_OOS_EDGE,
                min_windows_passing=WF_MIN_WINDOWS_PASSING,
                min_trades_per_window=WF_MIN_TRADES_PER_WINDOW,
                min_signal_to_noise=WF_MIN_SIGNAL_TO_NOISE,
            ),
        )

        print(f"  Walk-forward windows: {wf['windows']}")
        print(f"  Windows passing:      {wf['windows_passing']}")
        print(f"  Acceptance:           {'PASS' if wf['accepted'] else 'FAIL'}")

        quotes = [
            VenueQuote(
                venue="binance",
                bid=float(df['spot_close'].tail(1).item() * 0.9999),
                ask=float(df['spot_close'].tail(1).item() * 1.0001),
                depth_usd=2_000_000.0,
                fee_bps=6.0,
                latency_ms=55,
                reliability=0.995,
            ),
            VenueQuote(
                venue="backup_venue",
                bid=float(df['spot_close'].tail(1).item() * 0.9998),
                ask=float(df['spot_close'].tail(1).item() * 1.0002),
                depth_usd=1_200_000.0,
                fee_bps=5.5,
                latency_ms=95,
                reliability=0.985,
            ),
        ]
        intent = OrderIntent(
            symbol="SOLUSDT",
            side="buy",
            quantity=NOTIONAL_PER_TRADE,
            urgency=0.55,
            max_slippage_bps=8.0,
        )
        plan = route_order(intent, quotes)
        print(
            f"  Routed venue/order:  {plan.venue}/{plan.order_type} "
            f"(cost {plan.expected_cost_bps:.2f} bps, fill {plan.fill_probability:.1%})"
        )

        equity_curve = trade_summary["net_pnl_usd"].cum_sum()
        peak = equity_curve.cum_max()
        drawdown = ((peak - equity_curve) / MAX_GROSS_EXPOSURE_USD).fill_null(0.0)
        max_dd = float(drawdown.max()) if trade_summary.height > 0 else 0.0

        avg_staleness = (
            float(df["funding_staleness_minutes"].mean() or 0.0)
            if "funding_staleness_minutes" in df.columns
            else 0.0
        )

        risk = RiskEngine(
            RiskLimits(
                max_gross_exposure_usd=MAX_GROSS_EXPOSURE_USD,
                max_symbol_concentration=MAX_SYMBOL_CONCENTRATION,
                max_drawdown_pct=MAX_DRAWDOWN_PCT,
                max_data_staleness_minutes=MAX_FUNDING_STALENESS_MINUTES,
                max_latency_ms=MAX_VENUE_LATENCY_MS,
            )
        )
        decision = risk.evaluate(
            RiskState(
                gross_exposure_usd=min(
                    MAX_GROSS_EXPOSURE_USD,
                    max(1, stats["total_trades"]) * NOTIONAL_PER_TRADE,
                ),
                symbol_concentration=1.0,
                drawdown_pct=max_dd,
                data_staleness_minutes=int(avg_staleness),
                venue_latency_ms=55,
            )
        )

        print(f"  Risk allow new risk: {'YES' if decision.allow_new_risk else 'NO'}")
        print(f"  Risk de-risk needed: {'YES' if decision.derisk_required else 'NO'}")
        print(f"  Risk kill switch:    {'YES' if decision.kill_switch else 'NO'}")
        if decision.reasons:
            for reason in decision.reasons:
                print(f"  - {reason}")


if __name__ == "__main__":
    main()
