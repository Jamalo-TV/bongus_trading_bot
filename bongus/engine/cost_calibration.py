"""Measurement-only route cost and post-fill markout calibration.

Predictions are conditioned on market, route, symbol, and liquidity regime.
Sparse buckets are shrunk through successively broader parents instead of
trusting a handful of favorable fills.  Nothing in this module mutates live
configuration or changes routing; the default ``measurement_only`` flag keeps
every prediction ineligible for live gate relaxation.
"""

from __future__ import annotations

import hashlib
import math
import statistics
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

_COMPONENTS = ("fee_bps", "spread_bps", "impact_bps", "markout_bps", "legging_bps")


@dataclass(frozen=True, slots=True)
class RouteCostObservation:
    sample_id: str
    symbol: str
    market: str
    route: str
    regime: str
    fee_bps: float
    spread_bps: float
    impact_bps: float
    markout_bps: float
    legging_bps: float = 0.0
    notional_usd: float = 0.0
    observed_at: str = ""
    markout_horizon_seconds: float = 0.0

    def __post_init__(self) -> None:
        normalized = {
            "sample_id": self.sample_id.strip(),
            "symbol": self.symbol.strip().upper(),
            "market": self.market.strip().lower(),
            "route": self.route.strip().lower(),
            "regime": self.regime.strip().lower(),
        }
        for field_name, value in normalized.items():
            if not value:
                raise ValueError(f"{field_name} must not be empty")
            object.__setattr__(self, field_name, value)
        for field_name in _COMPONENTS:
            value = float(getattr(self, field_name))
            if not math.isfinite(value):
                raise ValueError(f"{field_name} must be finite")
            object.__setattr__(self, field_name, value)
        if not math.isfinite(self.notional_usd) or self.notional_usd < 0.0:
            raise ValueError("notional_usd must be finite and non-negative")
        if not math.isfinite(self.markout_horizon_seconds) or self.markout_horizon_seconds < 0.0:
            raise ValueError("markout_horizon_seconds must be finite and non-negative")

    @property
    def total_cost_bps(self) -> float:
        return sum(float(getattr(self, component)) for component in _COMPONENTS)


@dataclass(frozen=True, slots=True)
class CostCalibrationPrediction:
    symbol: str
    market: str
    route: str
    regime: str
    predicted_mean_bps: float
    conservative_cost_bps: float
    component_mean_bps: Mapping[str, float]
    component_p90_bps: Mapping[str, float]
    exact_sample_count: int
    symbol_route_sample_count: int
    route_regime_sample_count: int
    route_sample_count: int
    global_sample_count: int
    exact_bucket_weight: float
    quantile: float
    ready_for_bucket_gate: bool
    eligible_for_live_use: bool
    measurement_only: bool
    model_version: str


@dataclass(frozen=True, slots=True)
class CalibrationDiagnostics:
    sample_count: int
    median_bias_bps: float
    mean_absolute_error_bps: float
    mape_pct: float
    conservative_coverage: float
    target_coverage: float


@dataclass(frozen=True, slots=True)
class _Estimate:
    mean: float
    quantile: float
    count: int


def adverse_markout_bps(side: str, fill_price: float, future_mid_price: float) -> float:
    """Return signed post-fill adverse selection (positive is worse)."""

    normalized_side = side.strip().upper()
    if normalized_side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    if not all(math.isfinite(value) and value > 0.0 for value in (fill_price, future_mid_price)):
        raise ValueError("fill and future mid prices must be positive and finite")
    if normalized_side == "BUY":
        return (fill_price - future_mid_price) / fill_price * 10_000.0
    return (future_mid_price - fill_price) / fill_price * 10_000.0


def observation_from_execution_quality(
    row: Mapping[str, Any],
) -> RouteCostObservation | None:
    """Normalize one durable execution-quality row for calibration.

    Incomplete measurements are deliberately excluded.  In particular, an
    unavailable fee conversion or missing future midpoint must never be
    converted to a favorable zero-cost sample.
    """

    metadata_value = row.get("metadata", {})
    if not isinstance(metadata_value, Mapping):
        return None
    metadata = dict(metadata_value)
    if not bool(metadata.get("measurement_complete", False)):
        return None
    sample_id = str(row.get("sample_id") or metadata.get("sample_id") or "").strip()
    market = str(metadata.get("market") or "").strip().lower()
    route = str(metadata.get("route") or "").strip().lower()
    regime = str(metadata.get("regime") or "").strip().lower()
    if not all((sample_id, market, route, regime)):
        return None
    try:
        return RouteCostObservation(
            sample_id=sample_id,
            symbol=str(row.get("symbol") or ""),
            market=market,
            route=route,
            regime=regime,
            fee_bps=float(metadata["fee_bps"]),
            spread_bps=float(metadata["spread_cost_bps"]),
            impact_bps=float(metadata["impact_bps"]),
            markout_bps=float(metadata["markout_bps"]),
            legging_bps=float(metadata.get("legging_bps", 0.0)),
            notional_usd=float(metadata["notional_usd"]),
            observed_at=str(row.get("sample_time") or ""),
            markout_horizon_seconds=float(metadata["markout_horizon_seconds"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def observations_from_execution_quality(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[RouteCostObservation, ...]:
    """Return only complete, valid, idempotently keyed cost observations."""

    observations: dict[str, RouteCostObservation] = {}
    for row in rows:
        observation = observation_from_execution_quality(row)
        if observation is None:
            continue
        existing = observations.get(observation.sample_id)
        if existing is not None and existing != observation:
            raise ValueError(f"sample_id collision: {observation.sample_id}")
        observations[observation.sample_id] = observation
    return tuple(observations[key] for key in sorted(observations))


def _quantile(values: list[float], probability: float) -> float:
    if not values:
        raise ValueError("quantile requires observations")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


class CostMarkoutCalibrator:
    """Hierarchical empirical calibrator with conservative sparse shrinkage."""

    def __init__(
        self,
        *,
        prior_strength: float = 20.0,
        conservative_quantile: float = 0.90,
        minimum_bucket_samples: int = 100,
        uncertainty_floor_bps: float = 0.5,
        measurement_only: bool = True,
    ) -> None:
        if not math.isfinite(prior_strength) or prior_strength <= 0.0:
            raise ValueError("prior_strength must be positive and finite")
        if not 0.5 < conservative_quantile < 1.0:
            raise ValueError("conservative_quantile must be between 0.5 and 1")
        if minimum_bucket_samples <= 0:
            raise ValueError("minimum_bucket_samples must be positive")
        if not math.isfinite(uncertainty_floor_bps) or uncertainty_floor_bps < 0.0:
            raise ValueError("uncertainty_floor_bps must be finite and non-negative")
        self.prior_strength = float(prior_strength)
        self.conservative_quantile = float(conservative_quantile)
        self.minimum_bucket_samples = int(minimum_bucket_samples)
        self.uncertainty_floor_bps = float(uncertainty_floor_bps)
        self.measurement_only = bool(measurement_only)
        self._observations: dict[str, RouteCostObservation] = {}

    def add_observation(self, observation: RouteCostObservation) -> bool:
        """Add one immutable sample; exact duplicates are idempotent."""

        existing = self._observations.get(observation.sample_id)
        if existing is not None:
            if existing != observation:
                raise ValueError(f"sample_id collision: {observation.sample_id}")
            return False
        self._observations[observation.sample_id] = observation
        return True

    def add_observations(self, observations: Iterable[RouteCostObservation]) -> int:
        added = 0
        for observation in observations:
            added += int(self.add_observation(observation))
        return added

    @property
    def sample_count(self) -> int:
        return len(self._observations)

    @property
    def model_version(self) -> str:
        digest = hashlib.sha256()
        digest.update(
            (
                f"{self.prior_strength}|{self.conservative_quantile}|"
                f"{self.minimum_bucket_samples}|{self.uncertainty_floor_bps}"
            ).encode("utf-8")
        )
        for sample_id in sorted(self._observations):
            observation = self._observations[sample_id]
            digest.update(repr(observation).encode("utf-8"))
        return digest.hexdigest()[:16]

    def predict(
        self,
        *,
        symbol: str,
        market: str,
        route: str,
        regime: str,
    ) -> CostCalibrationPrediction | None:
        """Predict total p90 cost without summing marginal component p90s.

        Summing component quantiles double-counts tail reserves because fee,
        spread, impact, markout, and legging losses are dependent.  The total
        cost distribution is calibrated directly; component estimates are
        retained only for attribution.
        """

        if not self._observations:
            return None
        symbol_key = symbol.strip().upper()
        market_key = market.strip().lower()
        route_key = route.strip().lower()
        regime_key = regime.strip().lower()
        if not all((symbol_key, market_key, route_key, regime_key)):
            raise ValueError("prediction dimensions must not be empty")

        observations = list(self._observations.values())
        market_samples = [item for item in observations if item.market == market_key]
        route_samples = [
            item for item in market_samples
            if item.route == route_key
        ]
        symbol_route_samples = [item for item in route_samples if item.symbol == symbol_key]
        route_regime_samples = [item for item in route_samples if item.regime == regime_key]
        exact_samples = [item for item in symbol_route_samples if item.regime == regime_key]

        component_means: dict[str, float] = {}
        component_quantiles: dict[str, float] = {}
        for component in _COMPONENTS:
            estimate = self._hierarchical_estimate(
                component,
                observations,
                market_samples,
                route_samples,
                symbol_route_samples,
                route_regime_samples,
                exact_samples,
            )
            component_means[component] = estimate.mean
            component_quantiles[component] = estimate.quantile
        total_estimate = self._hierarchical_estimate(
            "total_cost_bps",
            observations,
            market_samples,
            route_samples,
            symbol_route_samples,
            route_regime_samples,
            exact_samples,
        )

        global_totals = [item.total_cost_bps for item in observations]
        global_scale = statistics.pstdev(global_totals) if len(global_totals) > 1 else 0.0
        evidence_count = len(exact_samples)
        if evidence_count == 0:
            # A missing exact bucket receives maximum uncertainty even when its
            # parent route has many samples.
            evidence_count = 1
        scale = max(global_scale, self.uncertainty_floor_bps)
        uncertainty_z = statistics.NormalDist().inv_cdf(self.conservative_quantile)
        uncertainty = uncertainty_z * scale / math.sqrt(evidence_count)
        conservative = max(0.0, total_estimate.mean, total_estimate.quantile + uncertainty)
        exact_weight = len(exact_samples) / (len(exact_samples) + self.prior_strength)
        ready = len(exact_samples) >= self.minimum_bucket_samples
        return CostCalibrationPrediction(
            symbol=symbol_key,
            market=market_key,
            route=route_key,
            regime=regime_key,
            predicted_mean_bps=total_estimate.mean,
            conservative_cost_bps=conservative,
            component_mean_bps=component_means,
            component_p90_bps=component_quantiles,
            exact_sample_count=len(exact_samples),
            symbol_route_sample_count=len(symbol_route_samples),
            route_regime_sample_count=len(route_regime_samples),
            route_sample_count=len(route_samples),
            global_sample_count=len(observations),
            exact_bucket_weight=exact_weight,
            quantile=self.conservative_quantile,
            ready_for_bucket_gate=ready,
            eligible_for_live_use=ready and not self.measurement_only,
            measurement_only=self.measurement_only,
            model_version=self.model_version,
        )

    def _hierarchical_estimate(
        self,
        field_name: str,
        global_samples: list[RouteCostObservation],
        market_samples: list[RouteCostObservation],
        route_samples: list[RouteCostObservation],
        symbol_route_samples: list[RouteCostObservation],
        route_regime_samples: list[RouteCostObservation],
        exact_samples: list[RouteCostObservation],
    ) -> _Estimate:
        estimate = self._raw_estimate(field_name, global_samples)
        estimate = self._shrink(field_name, market_samples, estimate)
        estimate = self._shrink(field_name, route_samples, estimate)

        symbol_estimate = self._shrink(field_name, symbol_route_samples, estimate)
        regime_estimate = self._shrink(field_name, route_regime_samples, estimate)
        if symbol_route_samples and route_regime_samples:
            symbol_weight = min(len(symbol_route_samples), self.prior_strength)
            regime_weight = min(len(route_regime_samples), self.prior_strength)
            parent = _Estimate(
                mean=(symbol_estimate.mean * symbol_weight + regime_estimate.mean * regime_weight)
                / (symbol_weight + regime_weight),
                quantile=(
                    symbol_estimate.quantile * symbol_weight
                    + regime_estimate.quantile * regime_weight
                )
                / (symbol_weight + regime_weight),
                count=len(symbol_route_samples) + len(route_regime_samples),
            )
        elif symbol_route_samples:
            parent = symbol_estimate
        elif route_regime_samples:
            parent = regime_estimate
        else:
            parent = estimate
        return self._shrink(field_name, exact_samples, parent)

    def _raw_estimate(self, field_name: str, samples: list[RouteCostObservation]) -> _Estimate:
        values = [self._value(sample, field_name) for sample in samples]
        return _Estimate(
            mean=statistics.fmean(values),
            quantile=_quantile(values, self.conservative_quantile),
            count=len(samples),
        )

    def _shrink(
        self,
        field_name: str,
        samples: list[RouteCostObservation],
        prior: _Estimate,
    ) -> _Estimate:
        if not samples:
            return prior
        empirical = self._raw_estimate(field_name, samples)
        weight = empirical.count / (empirical.count + self.prior_strength)
        return _Estimate(
            mean=weight * empirical.mean + (1.0 - weight) * prior.mean,
            quantile=weight * empirical.quantile + (1.0 - weight) * prior.quantile,
            count=empirical.count,
        )

    @staticmethod
    def _value(sample: RouteCostObservation, field_name: str) -> float:
        if field_name == "total_cost_bps":
            return sample.total_cost_bps
        return float(getattr(sample, field_name))

    def evaluate_holdout(self, observations: Iterable[RouteCostObservation]) -> CalibrationDiagnostics:
        actual: list[float] = []
        predicted: list[float] = []
        conservative: list[float] = []
        for observation in observations:
            prediction = self.predict(
                symbol=observation.symbol,
                market=observation.market,
                route=observation.route,
                regime=observation.regime,
            )
            if prediction is None:
                continue
            actual.append(observation.total_cost_bps)
            predicted.append(prediction.predicted_mean_bps)
            conservative.append(prediction.conservative_cost_bps)
        if not actual:
            raise ValueError("holdout evaluation requires at least one prediction")
        errors = [prediction - outcome for prediction, outcome in zip(predicted, actual, strict=True)]
        absolute_errors = [abs(error) for error in errors]
        percentage_errors = [
            abs(error) / max(abs(outcome), 1e-9) * 100.0
            for error, outcome in zip(errors, actual, strict=True)
        ]
        covered = sum(
            outcome <= upper
            for outcome, upper in zip(actual, conservative, strict=True)
        )
        return CalibrationDiagnostics(
            sample_count=len(actual),
            median_bias_bps=statistics.median(errors),
            mean_absolute_error_bps=statistics.fmean(absolute_errors),
            mape_pct=statistics.fmean(percentage_errors),
            conservative_coverage=covered / len(actual),
            target_coverage=self.conservative_quantile,
        )
