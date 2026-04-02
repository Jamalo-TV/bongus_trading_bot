"""Market-health heuristics used by the opportunity ranker."""

from __future__ import annotations


def compute_regime_health(spread_bps: float, realized_volatility: float, data_staleness_seconds: float) -> float:
    spread_penalty = min(1.0, max(0.0, spread_bps / 25.0))
    vol_penalty = min(1.0, max(0.0, realized_volatility * 100.0))
    stale_penalty = min(1.0, max(0.0, data_staleness_seconds / 60.0))
    score = 1.0 - (0.4 * spread_penalty + 0.4 * vol_penalty + 0.2 * stale_penalty)
    return max(0.0, min(1.0, score))
