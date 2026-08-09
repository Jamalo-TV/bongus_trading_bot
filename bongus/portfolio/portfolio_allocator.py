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
    regime_health: float = 1.0
    current_notional_usd: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OpenPosition:
    symbol: str
    notional_usd: float
    ann_funding: float
    qty: float = 0.0
    recovery_state: str | None = None


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
    exit_urgencies: dict[str, float] = field(default_factory=dict)
    exit_quantities: dict[str, float] = field(default_factory=dict)


class PortfolioAllocator:
    def __init__(
        self,
        arg1: Any,
        arg2: Any | None = None,
        capital_per_slot_usd: float = CAPITAL_PER_SLOT_USD,
        per_symbol_cap_usd: float = MAX_NOTIONAL_PER_TRADE,
    ):
        self._legacy_mode = not isinstance(arg1, dict)
        if self._legacy_mode:
            self._depth = arg1
            self._funding = arg2
            self._capital_per_slot = capital_per_slot_usd
            self.config: dict[str, Any] = {
                "scanner_min_depth_multiplier": LIQUIDITY_FILTER_MULTIPLIER,
                "per_symbol_notional_cap_usd": per_symbol_cap_usd,
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
        # 1. Liquidity clamp
        depth_capacity = candidate.depth_usd / max(self.config.get("scanner_min_depth_multiplier", 1.0), 1.0)
        per_symbol_cap = float(self.config.get("per_symbol_notional_cap_usd", 5000.0))
        target_positions = max(self.config.get("target_concurrent_positions", 1), 1)

        # 2. Equity-scaled Fractional-Kelly Volatility Sizing
        if "account_equity_usd" in self.config:
            account_equity = float(self.config.get("account_equity_usd", 0.0))
            base_slot = (account_equity / target_positions) * KELLY_FRACTION
            # Volatility dampener: reduce size as volatility rises beyond a normal 15s threshold.
            vol_dampener = 1.0 / (1.0 + max(0.0, candidate.realized_volatility - 0.0005) * 200.0)
            sized_notional = base_slot * vol_dampener
        else:
            # Preserve legacy config semantics when equity is not supplied:
            # per-symbol cap remains the effective slot size, with a mild volatility haircut.
            sized_notional = per_symbol_cap / max(1.0, 1.0 + candidate.realized_volatility * 50.0)

        # 3. Traditional caps
        gross_budget = self.config.get("max_gross_exposure_usd", 0.0) / target_positions

        return max(0.0, min(depth_capacity, sized_notional, per_symbol_cap, gross_budget))

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

            # Hard Gate: Regime Filter
            if candidate.regime_health < float(self.config.get("min_regime_health", 0.4)):
                reasons.append("toxic_regime")

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
                    "regime_health": candidate.regime_health,
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
        notional_overrides: dict[str, float] | None = None,
    ) -> AllocationDecision:
        blocked_symbols = blocked_symbols or set()
        notional_overrides = notional_overrides or {}
        open_symbols = {position.symbol for position in open_positions}
        hold = list(open_symbols)
        enter: list[tuple[str, float]] = []
        exit: list[tuple[str, str]] = []
        rejected: dict[str, list[str]] = {}
        rotation_targets: dict[str, str] = {}
        rotation_notionals: dict[str, float] = {}

        ranked = self._funding.get_ranked() if self._funding is not None else []
        base_target_notional = min(
            self._capital_per_slot * TARGET_LEVERAGE * max(0.1, notional_scale),
            MAX_NOTIONAL_PER_TRADE,
            self.config.get("per_symbol_notional_cap_usd", MAX_NOTIONAL_PER_TRADE),
        )
        free_slots = max(0, MAX_CONCURRENT_POSITIONS - len(open_positions))

        for symbol, ann_funding in ranked:
            reasons: list[str] = []
            target_notional = float(notional_overrides.get(symbol, base_target_notional))
            if symbol in blocked_symbols:
                reasons.append("blocked")
            if symbol in open_symbols:
                reasons.append("already_open")
            entry_depth = self._depth.get_entry_depth(symbol) if self._depth is not None else 0.0
            required_depth = target_notional * LIQUIDITY_FILTER_MULTIPLIER
            if entry_depth < required_depth:
                reasons.append("low_entry_depth")
            if reasons:
                rejected[symbol] = reasons
                continue
            if free_slots <= 0:
                rejected[symbol] = ["no_free_slots"]
                break
            enter.append((symbol, target_notional))
            free_slots -= 1

        if not enter and open_positions and ranked:
            best_symbol, best_rate = ranked[0]
            if best_symbol not in blocked_symbols and best_symbol not in open_symbols:
                managed = [p for p in open_positions if (getattr(p, "recovery_state", None) or "").lower() != "manual_review"]
                if managed:
                    weakest = min(managed, key=lambda position: position.ann_funding)
                    if (best_rate - weakest.ann_funding) >= rotation_min_gap_ann:
                        exit.append((weakest.symbol, "rotation"))
                        rotation_targets[weakest.symbol] = best_symbol
                        rotation_notionals[weakest.symbol] = float(notional_overrides.get(best_symbol, base_target_notional))

        return AllocationDecision(
            enter=enter,
            exit=exit,
            hold=hold,
            rejected=rejected,
            rotation_targets=rotation_targets,
            rotation_notionals=rotation_notionals,
        )

    def decide(self, arg1: Any, open_positions: Any | None = None, **kwargs: Any) -> AllocationDecision:
        if self._legacy_mode:
            return self._decide_legacy(arg1, **kwargs)
        assert isinstance(open_positions, dict)
        return self._decide_canonical(arg1, open_positions)
