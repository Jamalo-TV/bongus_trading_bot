from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bongus.research.feature_store import (
    CalibratedLinearFundingModel,
    FeatureDriftMonitor,
    FeatureRecord,
    MarketFeatureInput,
    PointInTimeFeatureStore,
    build_rich_funding_features,
    purged_walk_forward_splits,
)


NOW = datetime(2026, 7, 18, tzinfo=timezone.utc)


def test_rich_features_include_settlement_oi_cross_section_without_future_values() -> None:
    features = build_rich_funding_features(
        MarketFeatureInput(
            "BTCUSDT", NOW, 0.001, 0.0012, 0.0005, 101, 100, 0.01,
            0.2, 0.03, 110, 100, 120, 8,
            {"BTCUSDT": 0.001, "ETHUSDT": -0.001},
        )
    )
    assert features["open_interest_change"] == pytest.approx(0.1)
    assert features["settlement_elapsed_fraction"] == pytest.approx(0.75)
    assert features["cross_section_funding_zscore"] > 0


def test_feature_store_is_causal_immutable_and_staleness_aware(tmp_path) -> None:
    store = PointInTimeFeatureStore(str(tmp_path / "features.db"))
    past = FeatureRecord("BTCUSDT", NOW, NOW + timedelta(seconds=1), "e1", {"premium": 1.0})
    future = FeatureRecord(
        "BTCUSDT", NOW + timedelta(minutes=2), NOW + timedelta(minutes=2), "e2", {"premium": 99.0}
    )
    assert store.append(past) and store.append(future)
    assert not store.append(past)
    chosen = store.as_of("BTCUSDT", NOW + timedelta(minutes=1))
    assert chosen is not None and chosen.source_event_id == "e1"
    assert store.as_of(
        "BTCUSDT", NOW + timedelta(minutes=5), max_age=timedelta(seconds=30)
    ) is None
    with pytest.raises(ValueError, match="future/target"):
        store.append(FeatureRecord("BTCUSDT", NOW, NOW, "bad", {"future_return": 1.0}))
    with pytest.raises(ValueError, match="content collision"):
        store.append(FeatureRecord("BTCUSDT", NOW, NOW + timedelta(seconds=1), "e1", {"premium": 2.0}))
    store.close()


def test_drift_monitor_fails_closed_on_shift_or_missingness() -> None:
    monitor = FeatureDriftMonitor()
    stable = monitor.evaluate(
        [{"x": value} for value in (0.0, 1.0, 2.0, 3.0)],
        [{"x": value} for value in (0.1, 1.1, 2.1, 3.1)],
    )
    assert not stable.drifted
    shifted = monitor.evaluate(
        [{"x": value} for value in (0.0, 1.0, 2.0, 3.0)],
        [{"x": value} for value in (10.0, 11.0, 12.0, 13.0)],
    )
    assert shifted.drifted and "feature_drift:x" in shifted.blockers


def test_linear_model_is_reproducible_and_emits_uncertainty_hash() -> None:
    rows = [{"x": float(index)} for index in range(10)]
    labels = [2.0 * index + 1.0 + (0.01 if index % 2 else -0.01) for index in range(10)]
    first = CalibratedLinearFundingModel(["x"])
    second = CalibratedLinearFundingModel(["x"])
    assert first.fit(rows, labels) == second.fit(rows, labels)
    prediction = first.predict({"x": 10.0})
    assert prediction.mean_rate == pytest.approx(21.0, abs=0.05)
    assert prediction.lower_rate < prediction.mean_rate < prediction.upper_rate


def test_purged_walk_forward_removes_overlapping_labels_and_embargo() -> None:
    decisions = [NOW + timedelta(days=index) for index in range(12)]
    label_ends = [value + timedelta(days=2) for value in decisions]
    splits = purged_walk_forward_splits(
        decisions,
        label_ends,
        minimum_train_size=3,
        test_size=3,
        embargo=timedelta(days=1),
    )
    assert splits
    for train, test in splits:
        cutoff = decisions[test[0]] - timedelta(days=1)
        assert all(label_ends[index] < cutoff for index in train)
