"""Tests for data_loader.py – alignment, forward-fill, snapshot marking."""

import os
import tempfile
from datetime import datetime, timedelta, timezone

import polars as pl

from bongus.core.config import FUNDING_SNAPSHOT_HOURS
from bongus.data.loader import load_data


def _write_temp_parquets():
    """Create minimal parquet files and return their paths."""
    tmpdir = tempfile.mkdtemp()

    # 10 minutes of data starting at a snapshot hour
    timestamps = [
        datetime(2025, 1, 1, 0, m, tzinfo=timezone.utc) for m in range(10)
    ]

    spot = pl.DataFrame({
        "timestamp": timestamps,
        "open": [100.0] * 10,
        "high": [101.0] * 10,
        "low": [99.0] * 10,
        "close": [100.5] * 10,
        "volume": [1000.0] * 10,
    })

    perp = pl.DataFrame({
        "timestamp": timestamps,
        "open": [100.1] * 10,
        "high": [101.1] * 10,
        "low": [99.1] * 10,
        "close": [100.6] * 10,
        "volume": [1500.0] * 10,
    })

    # Only one funding snapshot at minute 0
    funding = pl.DataFrame({
        "timestamp": [timestamps[0]],
        "funding_rate": [0.001],
    })

    spot_path = os.path.join(tmpdir, "spot.parquet")
    perp_path = os.path.join(tmpdir, "perp.parquet")
    funding_path = os.path.join(tmpdir, "funding.parquet")

    spot.write_parquet(spot_path)
    perp.write_parquet(perp_path)
    funding.write_parquet(funding_path)

    return spot_path, perp_path, funding_path


def test_no_null_funding_after_forward_fill():
    spot_path, perp_path, funding_path = _write_temp_parquets()
    df = load_data(spot_path, perp_path, funding_path)
    assert df["funding_rate"].null_count() == 0, "funding_rate should have no nulls"


def test_all_rows_on_one_minute_cadence():
    spot_path, perp_path, funding_path = _write_temp_parquets()
    df = load_data(spot_path, perp_path, funding_path)

    diffs = df["timestamp"].diff().drop_nulls()
    expected_delta = timedelta(minutes=1)
    assert (diffs == expected_delta).all(), "All rows should be exactly 1 minute apart"


def test_funding_snapshot_flag():
    spot_path, perp_path, funding_path = _write_temp_parquets()
    df = load_data(spot_path, perp_path, funding_path)

    # Minute 0 at hour 0 is a snapshot; minutes 1-9 are not
    snapshots = df.filter(pl.col("funding_snapshot"))
    assert snapshots.height == 1, f"Expected 1 snapshot row, got {snapshots.height}"
    assert snapshots["timestamp"][0].hour in FUNDING_SNAPSHOT_HOURS


def test_output_columns():
    spot_path, perp_path, funding_path = _write_temp_parquets()
    df = load_data(spot_path, perp_path, funding_path)
    expected = {"timestamp", "spot_close", "perp_close", "funding_rate", "funding_snapshot"}
    assert set(df.columns) == expected
