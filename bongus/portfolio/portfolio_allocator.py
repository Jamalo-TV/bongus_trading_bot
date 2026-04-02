"""Liquidity-aware top-N portfolio construction."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bongus.portfolio.correlation_breaker import CorrelationState, evaluate_cluster_caps


@dataclass(slots=True)
class RankedCandidate:
    symbol: str
    cluster: str
    rank: int
    total_score: float
    predicted_net_edge_bps: float
    depth_usd: float
    realized_volatility: float
    current_notional_usd: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AllocationDecision:
    selected: list[dict[str, Any]]
    exits: list[str]
    rejected: dict[str, list[str]]


class PortfolioAllocator:
    def __init__(self, config: dict[str, Any]):
        self.config = config

    def _size_for_candidate(self, candidate: RankedCandidate) -> float:
        depth_capacity = candidate.depth_usd / max(self.config.get("scanner_min_depth_multiplier", 1.0), 1.0)
        vol_cap = self.config.get("per_symbol_notional_cap_usd", 0.0) / max(1.0, 1.0 + candidate.realized_volatility * 50.0)
        gross_budget = self.config.get("max_gross_exposure_usd", 0.0) / max(self.config.get("target_concurrent_positions", 1), 1)
        return max(0.0, min(depth_capacity, vol_cap, gross_budget))

    def decide(
        self,
        ranked: list[RankedCandidate],
        open_positions: dict[str, float],
    ) -> AllocationDecision:
        selected: list[dict[str, Any]] = []
        rejected: dict[str, list[str]] = {}
        selected_symbols: set[str] = set()
        clustered: dict[str, float] = {}
        gross_selected = 0.0
        target_count = int(self.config.get("target_concurrent_positions", 0))
        top_n = max(self.config.get("min_top_n", 0), min(target_count, self.config.get("max_top_n", target_count)))

        for candidate in ranked:
            reasons: list[str] = []
            size_usd = self._size_for_candidate(candidate)
            if size_usd <= 0:
                reasons.append("zero_capacity")
            if candidate.predicted_net_edge_bps < self.config.get("min_expected_edge_bps", 0.0):
                reasons.append("insufficient_edge")
            if len(selected) >= top_n:
                reasons.append("top_n_reached")

            correlation = evaluate_cluster_caps(
                CorrelationState(gross_selected, clustered),
                cluster=candidate.cluster,
                incremental_notional_usd=size_usd,
                gross_cap_usd=self.config.get("max_gross_exposure_usd", 0.0),
                cluster_cap_usd=self.config.get("per_cluster_notional_cap_usd", 0.0),
            )
            if not correlation.allowed:
                reasons.extend(correlation.reasons)

            incremental_edge = candidate.predicted_net_edge_bps * size_usd / 10_000.0
            if incremental_edge < self.config.get("min_incremental_portfolio_edge_bps", 0.0) * size_usd / 10_000.0:
                reasons.append("incremental_edge")

            if reasons:
                rejected[candidate.symbol] = reasons
                continue

            selected.append(
                {
                    "symbol": candidate.symbol,
                    "cluster": candidate.cluster,
                    "target_notional_usd": round(size_usd, 2),
                    "rank": candidate.rank,
                    "predicted_net_edge_bps": candidate.predicted_net_edge_bps,
                    "total_score": candidate.total_score,
                }
            )
            selected_symbols.add(candidate.symbol)
            gross_selected += size_usd
            clustered[candidate.cluster] = clustered.get(candidate.cluster, 0.0) + size_usd

        exits = sorted(symbol for symbol in open_positions if symbol not in selected_symbols)
        return AllocationDecision(selected=selected, exits=exits, rejected=rejected)
