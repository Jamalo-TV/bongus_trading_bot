"""Simple cluster-cap enforcement for correlated trades."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class CorrelationState:
    gross_exposure_usd: float
    clustered_exposure_usd: dict[str, float]


@dataclass(slots=True)
class CorrelationDecision:
    allowed: bool
    reasons: list[str]


def evaluate_cluster_caps(
    state: CorrelationState,
    cluster: str,
    incremental_notional_usd: float,
    gross_cap_usd: float,
    cluster_cap_usd: float,
) -> CorrelationDecision:
    reasons: list[str] = []
    if state.gross_exposure_usd + incremental_notional_usd > gross_cap_usd:
        reasons.append("gross_cap")
    if state.clustered_exposure_usd.get(cluster, 0.0) + incremental_notional_usd > cluster_cap_usd:
        reasons.append("cluster_cap")
    return CorrelationDecision(allowed=not reasons, reasons=reasons)
