from __future__ import annotations

import math

import pytest

from bongus.engine.cost_calibration import (
    CostMarkoutCalibrator,
    RouteCostObservation,
    adverse_markout_bps,
    observations_from_execution_quality,
)


def _observation(
    sample_id: str,
    total: float,
    *,
    symbol: str = "BTCUSDT",
    route: str = "maker",
    regime: str = "calm",
    market: str = "spot",
    fee: float | None = None,
    impact: float = 0.0,
    markout: float = 0.0,
) -> RouteCostObservation:
    return RouteCostObservation(
        sample_id=sample_id,
        symbol=symbol,
        market=market,
        route=route,
        regime=regime,
        fee_bps=total if fee is None else fee,
        spread_bps=0.0,
        impact_bps=impact,
        markout_bps=markout,
        notional_usd=2_500.0,
        markout_horizon_seconds=60.0,
    )


def test_adverse_markout_sign_is_direction_aware() -> None:
    assert adverse_markout_bps("BUY", 100.0, 99.0) == pytest.approx(100.0)
    assert adverse_markout_bps("BUY", 100.0, 101.0) == pytest.approx(-100.0)
    assert adverse_markout_bps("SELL", 100.0, 101.0) == pytest.approx(100.0)
    assert adverse_markout_bps("SELL", 100.0, 99.0) == pytest.approx(-100.0)


def test_observation_ids_are_idempotent_and_collisions_are_rejected() -> None:
    calibrator = CostMarkoutCalibrator()
    observation = _observation("fill-1", 5.0)
    assert calibrator.add_observation(observation)
    assert not calibrator.add_observation(observation)
    with pytest.raises(ValueError, match="sample_id collision"):
        calibrator.add_observation(_observation("fill-1", 6.0))


def test_sparse_exact_bucket_is_shrunk_toward_broad_route_evidence() -> None:
    calibrator = CostMarkoutCalibrator(prior_strength=20.0)
    calibrator.add_observations(_observation(f"btc-{index}", 10.0) for index in range(100))
    calibrator.add_observation(_observation("alt-1", -10.0, symbol="ALTUSDT"))

    prediction = calibrator.predict(symbol="ALTUSDT", market="spot", route="maker", regime="calm")

    assert prediction is not None
    assert prediction.exact_sample_count == 1
    assert prediction.exact_bucket_weight == pytest.approx(1.0 / 21.0)
    assert prediction.predicted_mean_bps > 5.0
    assert prediction.conservative_cost_bps >= prediction.predicted_mean_bps
    assert not prediction.ready_for_bucket_gate
    assert not prediction.eligible_for_live_use


def test_dense_exact_bucket_can_override_parent_but_default_remains_measurement_only() -> None:
    calibrator = CostMarkoutCalibrator(prior_strength=10.0, minimum_bucket_samples=100)
    calibrator.add_observations(
        _observation(f"parent-{index}", 20.0, symbol="ETHUSDT", regime="volatile")
        for index in range(200)
    )
    calibrator.add_observations(
        _observation(f"exact-{index}", 2.0)
        for index in range(100)
    )
    prediction = calibrator.predict(symbol="BTCUSDT", market="spot", route="maker", regime="calm")
    assert prediction is not None
    assert prediction.predicted_mean_bps < 5.0
    assert prediction.ready_for_bucket_gate
    assert prediction.measurement_only
    assert not prediction.eligible_for_live_use


def test_total_quantile_is_calibrated_directly_not_sum_of_component_tails() -> None:
    calibrator = CostMarkoutCalibrator(prior_strength=5.0, uncertainty_floor_bps=0.0)
    for index in range(200):
        calibrator.add_observation(
            _observation(
                f"exclusive-{index}",
                0.0,
                fee=10.0 if index % 2 == 0 else 0.0,
                impact=0.0 if index % 2 == 0 else 10.0,
            )
        )
    prediction = calibrator.predict(symbol="BTCUSDT", market="spot", route="maker", regime="calm")
    assert prediction is not None
    marginal_tail_sum = sum(prediction.component_p90_bps.values())
    assert prediction.predicted_mean_bps == pytest.approx(10.0)
    assert prediction.conservative_cost_bps == pytest.approx(10.0)
    assert marginal_tail_sum > prediction.conservative_cost_bps


def test_unseen_bucket_uses_parent_with_maximum_sparse_uncertainty() -> None:
    calibrator = CostMarkoutCalibrator(uncertainty_floor_bps=2.0)
    calibrator.add_observations(_observation(f"sample-{index}", 5.0) for index in range(20))
    prediction = calibrator.predict(
        symbol="NEWUSDT",
        market="spot",
        route="maker",
        regime="illiquid",
    )
    assert prediction is not None
    assert prediction.exact_sample_count == 0
    assert prediction.predicted_mean_bps == pytest.approx(5.0)
    assert prediction.conservative_cost_bps > 7.0
    assert not prediction.ready_for_bucket_gate


def test_holdout_diagnostics_report_bias_error_and_coverage() -> None:
    calibrator = CostMarkoutCalibrator(uncertainty_floor_bps=0.0)
    calibrator.add_observations(_observation(f"train-{index}", 5.0) for index in range(20))
    diagnostics = calibrator.evaluate_holdout(
        [_observation("holdout-1", 5.0), _observation("holdout-2", 6.0)]
    )
    assert diagnostics.sample_count == 2
    assert diagnostics.median_bias_bps == pytest.approx(-0.5)
    assert diagnostics.mean_absolute_error_bps == pytest.approx(0.5)
    assert diagnostics.mape_pct == pytest.approx((0.0 + 1.0 / 6.0 * 100.0) / 2.0)
    assert diagnostics.conservative_coverage == pytest.approx(0.5)


def test_model_version_is_order_independent_and_changes_with_content() -> None:
    first = CostMarkoutCalibrator()
    second = CostMarkoutCalibrator()
    samples = [_observation("a", 1.0), _observation("b", 2.0)]
    first.add_observations(samples)
    second.add_observations(reversed(samples))
    assert first.model_version == second.model_version
    second.add_observation(_observation("c", 3.0))
    assert first.model_version != second.model_version


def test_execution_quality_adapter_excludes_incomplete_measurements() -> None:
    complete = {
        "sample_id": "fill-1:60s",
        "sample_time": "2026-01-01T00:01:00+00:00",
        "symbol": "BTCUSDT",
        "metadata": {
            "measurement_complete": True,
            "market": "spot",
            "route": "legacy_dual_maker",
            "regime": "calm",
            "fee_bps": 1.0,
            "spread_cost_bps": 0.5,
            "impact_bps": 0.25,
            "markout_bps": 0.75,
            "legging_bps": 0.0,
            "notional_usd": 1_000.0,
            "markout_horizon_seconds": 60.0,
        },
    }
    incomplete = {
        **complete,
        "sample_id": "fill-2:60s",
        "metadata": {**complete["metadata"], "measurement_complete": False},
    }
    observations = observations_from_execution_quality([incomplete, complete, complete])
    assert len(observations) == 1
    assert observations[0].sample_id == "fill-1:60s"
    assert observations[0].total_cost_bps == pytest.approx(2.5)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_cost_components_are_rejected(value: float) -> None:
    with pytest.raises(ValueError, match="fee_bps must be finite"):
        _observation("bad", value)
