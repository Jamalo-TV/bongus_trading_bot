"""
Generate synthetic 1-minute OHLCV data for spot & perp, plus 8-hour funding
rates, and write them as Parquet files into data/.

Usage:
    python generate_sample_data.py
"""

import os
import math
import random
from datetime import datetime, timedelta, timezone

import polars as pl

from bongus.core.config import FUNDING_INTERVAL_HOURS, FUNDING_SNAPSHOT_HOURS

# ── Parameters ────────────────────────────────────────────────────────────────
DAYS = 90
INITIAL_PRICE = 150.0  # e.g. SOL/USDT
VOLATILITY_PER_MINUTE = 0.0003  # std‑dev of 1m log‑returns
PREMIUM_MEAN = 0.0008  # avg perp premium over spot (0.08%)
PREMIUM_STD = 0.0006

# Funding rate distribution (per‑period, not annualized).
# We bias positive so there are genuine arb opportunities.
FUNDING_MEAN = 0.0008   # 0.08% per 8h → ~88% annualized
FUNDING_STD = 0.0006

SEED = 42
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data")


def _generate_minute_timestamps(days: int) -> list[datetime]:
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    return [start + timedelta(minutes=m) for m in range(days * 24 * 60)]


def _random_walk(n: int, start: float, vol: float, rng: random.Random) -> list[float]:
    prices = [start]
    for _ in range(n - 1):
        ret = rng.gauss(0, vol)
        prices.append(prices[-1] * math.exp(ret))
    return prices


def generate_spot(timestamps: list[datetime], rng: random.Random) -> pl.DataFrame:
    n = len(timestamps)
    closes = _random_walk(n, INITIAL_PRICE, VOLATILITY_PER_MINUTE, rng)

    # Derive synthetic OHLCV from close
    opens = [closes[0]] + closes[:-1]
    highs = [max(o, c) * (1 + abs(rng.gauss(0, 0.0001))) for o, c in zip(opens, closes)]
    lows = [min(o, c) * (1 - abs(rng.gauss(0, 0.0001))) for o, c in zip(opens, closes)]
    volumes = [abs(rng.gauss(500_000, 200_000)) for _ in range(n)]

    return pl.DataFrame({
        "timestamp": timestamps,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })


def generate_perp(spot_df: pl.DataFrame, rng: random.Random) -> pl.DataFrame:
    spot_closes = spot_df["close"].to_list()
    n = len(spot_closes)

    # Perp = Spot × (1 + time-varying premium)
    premiums = []
    p = PREMIUM_MEAN
    for _ in range(n):
        p += rng.gauss(0, 0.00002)  # slow mean-reverting drift
        p = max(-0.002, min(0.004, p))  # clamp
        premiums.append(p)

    closes = [s * (1 + p) for s, p in zip(spot_closes, premiums)]
    opens = [closes[0]] + closes[:-1]
    highs = [max(o, c) * (1 + abs(rng.gauss(0, 0.0001))) for o, c in zip(opens, closes)]
    lows = [min(o, c) * (1 - abs(rng.gauss(0, 0.0001))) for o, c in zip(opens, closes)]
    volumes = [abs(rng.gauss(800_000, 300_000)) for _ in range(n)]

    return pl.DataFrame({
        "timestamp": spot_df["timestamp"],
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })


def generate_funding(timestamps: list[datetime], rng: random.Random) -> pl.DataFrame:
    # Only keep rows that fall on snapshot hours
    snapshot_ts = [
        t for t in timestamps
        if t.hour in FUNDING_SNAPSHOT_HOURS and t.minute == 0
    ]

    rates: list[float] = []
    rate = FUNDING_MEAN
    for _ in snapshot_ts:
        rate += rng.gauss(0, 0.0002)
        rate = max(-0.001, min(0.003, rate))  # clamp to realistic bounds
        rates.append(rate)

    return pl.DataFrame({
        "timestamp": snapshot_ts,
        "funding_rate": rates,
    })


def main() -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    rng = random.Random(SEED)

    print(f"Generating {DAYS} days of synthetic 1m data ...")
    timestamps = _generate_minute_timestamps(DAYS)

    spot_df = generate_spot(timestamps, rng)
    perp_df = generate_perp(spot_df, rng)
    funding_df = generate_funding(timestamps, rng)

    spot_path = os.path.join(OUTPUT_DIR, "spot_1m.parquet")
    perp_path = os.path.join(OUTPUT_DIR, "perp_1m.parquet")
    funding_path = os.path.join(OUTPUT_DIR, "funding_rates.parquet")

    spot_df.write_parquet(spot_path)
    perp_df.write_parquet(perp_path)
    funding_df.write_parquet(funding_path)

    print(f"  spot_1m.parquet      : {len(spot_df):>8,} rows")
    print(f"  perp_1m.parquet      : {len(perp_df):>8,} rows")
    print(f"  funding_rates.parquet: {len(funding_df):>8,} rows")
    print("Done.")


if __name__ == "__main__":
    main()
