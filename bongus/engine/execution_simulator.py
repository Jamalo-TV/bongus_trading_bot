"""Seeded route-policy simulator used for offline and shadow validation."""

from __future__ import annotations

from dataclasses import dataclass
import random
from statistics import fmean, quantiles

from bongus.engine.route_optimizer import RouteEstimate, RoutePolicy


@dataclass(frozen=True, slots=True)
class SimulationScenario:
    trials: int = 1_000
    seed: int = 1
    cost_noise_bps: float = 1.0
    latency_jitter_fraction: float = 0.20
    adverse_tail_probability: float = 0.02
    adverse_tail_bps: float = 20.0


@dataclass(frozen=True, slots=True)
class SimulatedRouteOutcome:
    policy: RoutePolicy
    trials: int
    mean_cost_bps: float
    p95_cost_bps: float
    mean_completion_ms: float
    hedge_budget_breach_rate: float


def simulate_route(
    estimate: RouteEstimate,
    *,
    hedge_budget_notional_ms: float,
    scenario: SimulationScenario = SimulationScenario(),
) -> SimulatedRouteOutcome:
    if scenario.trials < 20:
        raise ValueError("simulation requires at least 20 trials")
    rng = random.Random(scenario.seed)
    costs: list[float] = []
    latencies: list[float] = []
    breaches = 0
    for _ in range(scenario.trials):
        tail = (
            scenario.adverse_tail_bps
            if rng.random() < scenario.adverse_tail_probability
            else 0.0
        )
        cost = max(
            0.0,
            rng.gauss(estimate.total_objective_bps, scenario.cost_noise_bps) + tail,
        )
        latency = max(
            0.0,
            estimate.expected_completion_ms
            * (1.0 + rng.uniform(-scenario.latency_jitter_fraction, scenario.latency_jitter_fraction)),
        )
        risk_scale = latency / max(1.0, estimate.expected_completion_ms)
        risk = estimate.hedge_risk_notional_ms * risk_scale
        breaches += int(risk > hedge_budget_notional_ms)
        costs.append(cost)
        latencies.append(latency)
    return SimulatedRouteOutcome(
        policy=estimate.policy,
        trials=scenario.trials,
        mean_cost_bps=fmean(costs),
        p95_cost_bps=quantiles(costs, n=20, method="inclusive")[18],
        mean_completion_ms=fmean(latencies),
        hedge_budget_breach_rate=breaches / scenario.trials,
    )

