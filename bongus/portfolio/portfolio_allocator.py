"""Liquidity-aware top-N portfolio construction."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bongus.core.config import (
    CAPITAL_PER_SLOT_USD,
    LIQUIDITY_FILTER_MULTIPLIER,
    MAX_CONCURRENT_POSITIONS,
    MAX_NOTIONAL_PER_TRADE,
    ROTATION_MIN_GAP_ANN,
    TARGET_LEVERAGE,
)
from bongus.portfolio.correlation_breaker import CorrelationState, evaluate_cluster_caps

KELLY_FRACTION = 0.50


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
class OpenPosition:
    symbol: str
    notional_usd: float
    ann_funding: float


@dataclass(slots=True)
class AllocationDecision:
    selected: list[dict[str, Any]] = field(default_factory=list)
    exits: list[str] = field(default_factory=list)
    rejected: dict[str, list[str]] = field(default_factory=dict)
    enter: list[tuple[str, float]] = field(default_factory=list)
    exit: list[tuple[str, str]] = field(default_factory=list)
    hold: list[str] = field(default_factory=list)
    rotation_targets: dict[str, str] = field(default_factory=dict)
    rotation_notionals: dict[str, float] = field(default_factory=dict)


class PortfolioAllocator:
    def __init__(self, arg1: Any, arg2: Any | None = None, capital_per_slot_usd: float = CAPITAL_PER_SLOT_USD):
        self._legacy_mode = not isinstance(arg1, dict)
        if self._legacy_mode:
            self._depth = arg1
            self._funding = arg2
            self._capital_per_slot = capital_per_slot_usd
            self.config: dict[str, Any] = {
                "scanner_min_depth_multiplier": LIQUIDITY_FILTER_MULTIPLIER,
                "per_symbol_notional_cap_usd": MAX_NOTIONAL_PER_TRADE,
                "max_gross_exposure_usd": MAX_CONCURRENT_POSITIONS * CAPITAL_PER_SLOT_USD * TARGET_LEVERAGE,
                "target_concurrent_positions": MAX_CONCURRENT_POSITIONS,
                "min_top_n": 1,
                "max_top_n": MAX_CONCURRENT_POSITIONS,
                "per_cluster_notional_cap_usd": MAX_CONCURRENT_POSITIONS * CAPITAL_PER_SLOT_USD * TARGET_LEVERAGE,
                "min_expected_edge_bps": 0.0,
                "min_incremental_portfolio_edge_bps": 0.0,
            }
        else:
            self.config = arg1

    def _size_for_candidate(self, candidate: RankedCandidate) -> float:
        depth_capacity = candidate.depth_usd / max(self.config.get("scanner_min_depth_multiplier", 1.0), 1.0)
        vol_cap = self.config.get("per_symbol_notional_cap_usd", 0.0) / max(1.0, 1.0 + candidate.realized_volatility * 50.0)
        gross_budget = self.config.get("max_gross_exposure_usd", 0.0) / max(self.config.get("target_concurrent_positions", 1), 1)
        return max(0.0, min(depth_capacity, vol_cap, gross_budget))

    def _decide_canonical(self, ranked: list[RankedCandidate], open_positions: dict[str, float]) -> AllocationDecision:
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

    def _decide_legacy(
        self,
        open_positions: list[OpenPosition],
        *,
        blocked_symbols: set[str] | None = None,
        notional_scale: float = 1.0,
        rotation_min_gap_ann: float = ROTATION_MIN_GAP_ANN,
    ) -> AllocationDecision:
        blocked_symbols = blocked_symbols or set()
        open_symbols = {position.symbol for position in open_positions}
        hold = list(open_symbols)
        enter: list[tuple[str, float]] = []
        exit: list[tuple[str, str]] = []
        rotation_targets: dict[str, str] = {}
        rotation_notionals: dict[str, float] = {}

        ranked = self._funding.get_ranked() if self._funding is not None else []
        target_notional = min(self._capital_per_slot * TARGET_LEVERAGE * max(0.1, notional_scale), MAX_NOTIONAL_PER_TRADE)
        free_slots = max(0, MAX_CONCURRENT_POSITIONS - len(open_positions))

        for symbol, ann_funding in ranked:
            if free_slots <= 0:
                break
            if symbol in blocked_symbols or symbol in open_symbols:
                continue
            entry_depth = self._depth.get_entry_depth(symbol) if self._depth is not None else 0.0
            if entry_depth < target_notional * LIQUIDITY_FILTER_MULTIPLIER:
                continue
            enter.append((symbol, target_notional))
            free_slots -= 1

        if not enter and open_positions and ranked:
            best_symbol, best_rate = ranked[0]
            if best_symbol not in blocked_symbols and best_symbol not in open_symbols:
                weakest = min(open_positions, key=lambda position: position.ann_funding)
                if (best_rate - weakest.ann_funding) >= rotation_min_gap_ann:
                    exit.append((weakest.symbol, "rotation"))
                    rotation_targets[weakest.symbol] = best_symbol
                    rotation_notionals[weakest.symbol] = target_notional

        return AllocationDecision(
            enter=enter,
            exit=exit,
            hold=hold,
            rotation_targets=rotation_targets,
            rotation_notionals=rotation_notionals,
        )

    def decide(self, arg1: Any, open_positions: Any | None = None, **kwargs: Any) -> AllocationDecision:
        if self._legacy_mode:
            return self._decide_legacy(arg1, **kwargs)
        assert isinstance(open_positions, dict)
        return self._decide_canonical(arg1, open_positions)
