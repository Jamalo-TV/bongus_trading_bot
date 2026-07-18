from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from bongus.research.experiment_registry import (
    ExperimentManifest,
    ExperimentRegistry,
    ExperimentRegistryError,
    MetricDefinition,
)


NOW = datetime(2026, 7, 18, tzinfo=timezone.utc)


def manifest(**overrides):
    values = dict(
        experiment_id="route-v1",
        hypothesis="adaptive route lowers reconciled total cost",
        primary_metric="net_value",
        metrics=(
            MetricDefinition("net_value", True, minimum_effect=0.1),
            MetricDefinition(
                "hedge_risk", False, is_guardrail=True, maximum_adverse_effect=0.0
            ),
        ),
        data_checksums={"sample.parquet": "a" * 64},
        code_version="commit-1",
        config_hash="b" * 64,
        model_hash="c" * 64,
        random_seed=7,
        minimum_samples_per_arm=2,
        maximum_sequential_looks=2,
    )
    values.update(overrides)
    return ExperimentManifest(**values)


def test_manifest_is_immutable_and_assignment_is_deterministic(tmp_path) -> None:
    registry = ExperimentRegistry(str(tmp_path / "experiments.db"))
    first_hash = registry.register(manifest())
    assert registry.register(manifest()) == first_hash
    first = registry.assign("route-v1", "cycle-1", now=NOW)
    assert registry.assign("route-v1", "cycle-1", now=NOW) == first
    registry.close()

    restored = ExperimentRegistry(str(tmp_path / "experiments.db"))
    assert restored.assign("route-v1", "cycle-1", now=NOW) == first
    with pytest.raises(ExperimentRegistryError, match="immutable"):
        restored.register(replace(manifest(), hypothesis="changed after seeing results"))
    restored.close()


def test_unregistered_metrics_and_observation_collisions_fail_closed(tmp_path) -> None:
    registry = ExperimentRegistry(str(tmp_path / "experiments.db"))
    registry.register(manifest())
    with pytest.raises(ExperimentRegistryError, match="not preregistered"):
        registry.observe(
            experiment_id="route-v1", observation_id="o1", unit_id="u1",
            metric_name="secret_metric", value=1, observed_at=NOW,
        )
    assert registry.observe(
        experiment_id="route-v1", observation_id="o1", unit_id="u1",
        metric_name="net_value", value=1, observed_at=NOW,
    )
    assert not registry.observe(
        experiment_id="route-v1", observation_id="o1", unit_id="u1",
        metric_name="net_value", value=1, observed_at=NOW,
    )
    with pytest.raises(ExperimentRegistryError, match="content collision"):
        registry.observe(
            experiment_id="route-v1", observation_id="o1", unit_id="u1",
            metric_name="net_value", value=2, observed_at=NOW,
        )
    registry.close()


def _populate_balanced(registry: ExperimentRegistry) -> None:
    # Find deterministic members in each arm, then give treatment a clear
    # preregistered uplift and identical guardrail values.
    members = {"control": [], "treatment": []}
    index = 0
    while min(len(values) for values in members.values()) < 8:
        unit = f"unit-{index}"
        cohort = registry.assign("route-v1", unit, now=NOW)
        if len(members[cohort]) < 8:
            members[cohort].append(unit)
        index += 1
    for cohort, units in members.items():
        for offset, unit in enumerate(units):
            registry.observe(
                experiment_id="route-v1", observation_id=f"{unit}-value", unit_id=unit,
                metric_name="net_value", value=(20 + offset if cohort == "treatment" else offset),
                observed_at=NOW,
            )
            registry.observe(
                experiment_id="route-v1", observation_id=f"{unit}-risk", unit_id=unit,
                metric_name="hedge_risk", value=1, observed_at=NOW,
            )


def test_evaluation_controls_metric_family_sequential_looks_and_promotion(tmp_path) -> None:
    registry = ExperimentRegistry(str(tmp_path / "experiments.db"))
    registry.register(manifest(treatment_allocation=0.5))
    _populate_balanced(registry)
    result = registry.evaluate("route-v1")
    assert result.status == "PASSED"
    assert result.adjusted_alpha == pytest.approx(0.05 / 2 / 2)
    promotion = registry.promote(
        experiment_id="route-v1",
        artifact_hash="d" * 64,
        target_scope="paper",
        evaluation=result,
    )
    assert promotion.startswith("promotion-")
    with pytest.raises(ExperimentRegistryError, match="live promotion"):
        registry.promote(
            experiment_id="route-v1",
            artifact_hash="e" * 64,
            target_scope="live",
            evaluation=result,
        )
    registry.close()


def test_insufficient_data_and_sample_ratio_mismatch_block_promotion(tmp_path) -> None:
    registry = ExperimentRegistry(str(tmp_path / "experiments.db"))
    registry.register(manifest(minimum_samples_per_arm=100))
    for index in range(10):
        registry.assign("route-v1", f"small-{index}", now=NOW)
    result = registry.evaluate("route-v1")
    assert result.status == "INSUFFICIENT_DATA"
    with pytest.raises(ExperimentRegistryError, match="PASSED"):
        registry.promote(
            experiment_id="route-v1",
            artifact_hash="f" * 64,
            target_scope="paper",
            evaluation=result,
        )
    registry.close()
