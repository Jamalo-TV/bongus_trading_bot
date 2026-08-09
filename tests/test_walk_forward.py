"""Causal walk-forward validation tests."""

from dataclasses import replace

from datetime import datetime, timedelta, timezone
import hashlib

import polars as pl
import pytest

from bongus.core.config import FUNDING_PERIODS_PER_YEAR
from bongus.research.event_replay import ReplayDatasetManifest
from bongus.strategies.strategy import StrategyParameters
from scripts.walk_forward import (
    AcceptanceGates,
    CanonicalReplayFold,
    run_canonical_walk_forward_replay,
    run_walk_forward_validation,
)


def _sample_df(rows: int = 10_000) -> pl.DataFrame:
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    timestamps = [start + timedelta(minutes=i) for i in range(rows)]
    spot = [100.0 + i * 0.00001 for i in range(rows)]
    active = [(i % 240) < 120 for i in range(rows)]
    # Entry at a 0.5% premium and exit after favorable convergence to -0.1%.
    perp = [s * (1.005 if on else 0.999) for s, on in zip(spot, active)]
    funding = [0.0004 if on else 0.0 for on in active]
    snapshots = [
        timestamp.minute == 0 and timestamp.hour in {0, 8, 16}
        for timestamp in timestamps
    ]
    return pl.DataFrame({
        "timestamp": timestamps,
        "spot_close": spot,
        "perp_close": perp,
        "funding_rate": funding,
        "funding_snapshot": snapshots,
    })


def _permissive_gates() -> AcceptanceGates:
    return AcceptanceGates(
        min_avg_oos_edge=-1.0,
        min_windows_passing=1,
        min_trades_per_window=1,
        min_signal_to_noise=-1_000_001.0,
    )


def test_walk_forward_uses_completed_net_trades_and_embargo():
    summary = run_walk_forward_validation(
        _sample_df(),
        gates=_permissive_gates(),
        train_rows=4_000,
        test_rows=1_000,
        step_rows=1_000,
        embargo_rows=30,
    )

    assert summary["windows"] > 0
    assert summary["windows_passing"] > 0
    assert summary["accepted"]
    assert summary["total_trades"] > 0
    assert summary["total_trades"] < summary["windows"] * 1_000
    first = summary["results"][0]
    assert first.train_trades > 0
    assert first.trades > 0
    assert first.embargo_rows == 30
    assert first.train_end < first.test_start
    assert first.avg_signal_to_noise != 1.0


def test_parameter_selection_does_not_read_test_window():
    base = _sample_df(rows=5_030)
    changed_test = (
        base.with_row_index("_row")
        .with_columns(
            pl.when(pl.col("_row") >= 4_030)
            .then(pl.col("perp_close") * 1.02)
            .otherwise(pl.col("perp_close"))
            .alias("perp_close")
        )
        .drop("_row")
    )
    kwargs = {
        "gates": _permissive_gates(),
        "train_rows": 4_000,
        "test_rows": 1_000,
        "step_rows": 1_000,
        "embargo_rows": 30,
    }

    original = run_walk_forward_validation(base, **kwargs)["results"][0]
    perturbed = run_walk_forward_validation(changed_test, **kwargs)["results"][0]

    assert original.selected_entry_ann_funding == perturbed.selected_entry_ann_funding
    assert original.selected_entry_premium == perturbed.selected_entry_premium
    assert original.train_trades == perturbed.train_trades
    assert original.train_avg_realized_edge == perturbed.train_avg_realized_edge


def test_training_outcomes_select_the_better_candidate():
    rows = 5_030
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    timestamps = [start + timedelta(minutes=i) for i in range(rows)]
    spot = [100.0] * rows
    funding: list[float] = []
    perp: list[float] = []
    for index in range(rows):
        cycle = index // 240
        active = index % 240 < 120
        high_quality = cycle % 2 == 1
        annualized = 0.40 if high_quality else 0.15
        funding.append(annualized / FUNDING_PERIODS_PER_YEAR if active else 0.0)
        if active:
            perp.append(100.5)
        else:
            # Low-funding cycles widen and lose; high-funding cycles converge.
            perp.append(99.9 if high_quality else 101.0)
    data = pl.DataFrame({
        "timestamp": timestamps,
        "spot_close": spot,
        "perp_close": perp,
        "funding_rate": funding,
        "funding_snapshot": [
            timestamp.minute == 0 and timestamp.hour in {0, 8, 16}
            for timestamp in timestamps
        ],
    })
    relaxed = StrategyParameters(
        entry_ann_funding_threshold=0.10,
        entry_premium_threshold=0.001,
    )
    selective = StrategyParameters(
        entry_ann_funding_threshold=0.30,
        entry_premium_threshold=0.001,
    )

    result = run_walk_forward_validation(
        data,
        gates=_permissive_gates(),
        train_rows=4_000,
        test_rows=1_000,
        step_rows=1_000,
        embargo_rows=30,
        candidates=(relaxed, selective),
    )["results"][0]

    assert result.selected_entry_ann_funding == 0.30


def test_walk_forward_is_deterministic_for_immutable_input():
    kwargs = {
        "gates": _permissive_gates(),
        "train_rows": 2_000,
        "test_rows": 500,
        "step_rows": 500,
        "embargo_rows": 30,
    }
    data = _sample_df(rows=4_000)

    first = run_walk_forward_validation(data, **kwargs)
    second = run_walk_forward_validation(data, **kwargs)

    assert first == second


def test_canonical_walk_forward_verifies_manifests_and_embargo(tmp_path):
    train = tmp_path / "train.events"
    test = tmp_path / "test.events"
    train.write_bytes(b"train")
    test.write_bytes(b"test")
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def manifest(name, range_start, range_end):
        data = (tmp_path / name).read_bytes()
        return ReplayDatasetManifest(
            symbols=("BTCUSDT",),
            venue_contracts={"BTCUSDT": "BINANCE:BTCUSDT-PERP"},
            source="fixture",
            retrieved_at=range_end + timedelta(days=1),
            range_start=range_start,
            range_end=range_end,
            cadence="event",
            universe_construction="point-in-time",
            listing_delisting_treatment="explicit",
            file_sha256={name: hashlib.sha256(data).hexdigest()},
        )

    train_manifest = manifest("train.events", start, start + timedelta(hours=1))
    test_manifest = manifest(
        "test.events", start + timedelta(hours=2), start + timedelta(hours=3)
    )
    fold = CanonicalReplayFold(
        train_events=(),
        train_manifest=train_manifest,
        train_root=tmp_path,
        test_events=(),
        test_manifest=test_manifest,
        test_root=tmp_path,
        embargo_seconds=3_600,
    )

    results = run_canonical_walk_forward_replay([fold])
    assert len(results) == 1
    assert results[0].train.manifest_hash == train_manifest.manifest_hash
    assert results[0].test.manifest_hash == test_manifest.manifest_hash
    assert results[0].config_hash

    with pytest.raises(ValueError, match="purged embargo"):
        run_canonical_walk_forward_replay(
            [replace(fold, embargo_seconds=3_601)]
        )
