"""Scanner and opportunity-ranking helpers for live trading."""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import quantiles
from typing import Any

import requests

from bongus.core.config import DEFAULT_CLUSTER, MAX_FUNDING_SCAN_SYMBOLS, PORTFOLIO_CLUSTER_MAP
from bongus.engine.cost_model import CostContext, estimate_trade_edge
from bongus.engine.state_store import CandidateSnapshot, OpportunityScore

logger = logging.getLogger(__name__)

_ENDPOINT = "https://fapi.binance.com/fapi/v1/premiumIndex"
_FUNDING_PERIODS_PER_YEAR = 1095
_MAX_STALENESS_SECONDS = 8 * 60 * 60
_MAX_RETRIES = 3
_BASE_RETRY_DELAY_S = 1.0


@dataclass(slots=True)
class MarketCandidate:
    symbol: str
    annualized_funding: float
    basis_pct: float
    spread_bps: float
    depth_usd: float
    realized_volatility: float
    basis_stability: float
    regime_health: float
    listing_age_days: float
    data_staleness_seconds: float
    has_spot: bool = True
    is_delist_risk: bool = False
    toxicity_bps: float = 0.0
    cluster: str = DEFAULT_CLUSTER
    imbalance: float = 0.0
    mark_price: float = 0.0
    direction: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class FundingRanker:
    def __init__(self, symbols: list[str] | None = None, *, dynamic: bool | None = None) -> None:
        # dynamic=None: infer from whether symbols is None
        # dynamic=True: expand beyond seed symbols; dynamic=False: fixed universe
        self._dynamic = dynamic if dynamic is not None else (symbols is None)
        self._symbols: set[str] = set(symbols) if symbols is not None else set()
        self._rates: dict[str, float] = {symbol: 0.0 for symbol in self._symbols}
        self._allowed_symbols: set[str] | None = None
        self._last_successful_refresh: datetime | None = None
        self._last_error = ""
        self._consecutive_failures = 0

    def set_allowed_symbols(self, symbols: set[str] | list[str] | None) -> None:
        if symbols is None:
            self._allowed_symbols = None
            return
        normalized = {str(symbol).upper() for symbol in symbols if str(symbol).strip()}
        self._allowed_symbols = normalized
        self._symbols.intersection_update(normalized)
        self._rates = {
            symbol: rate for symbol, rate in self._rates.items() if symbol in normalized
        }

    def _can_track_symbol(self, symbol: str) -> bool:
        if self._allowed_symbols is not None and symbol not in self._allowed_symbols:
            return False
        if symbol in self._symbols:
            return True
        if not self._dynamic:
            return False
        max_symbols = int(MAX_FUNDING_SCAN_SYMBOLS)
        return max_symbols <= 0 or len(self._symbols) < max_symbols

    def _is_stale(self) -> bool:
        if self._last_successful_refresh is None:
            return True
        age = (datetime.now(timezone.utc) - self._last_successful_refresh).total_seconds()
        return age > _MAX_STALENESS_SECONDS

    async def refresh(self) -> None:
        data = None
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                response = await asyncio.to_thread(requests.get, _ENDPOINT, timeout=10)
                if hasattr(response, "raise_for_status"):
                    response.raise_for_status()
                data = response.json()
                self._last_error = ""
                self._consecutive_failures = 0
                break
            except Exception as exc:
                last_exc = exc
                self._consecutive_failures += 1
                if attempt + 1 < _MAX_RETRIES:
                    await asyncio.sleep(_BASE_RETRY_DELAY_S * (2 ** attempt))
        if data is None:
            self._last_error = str(last_exc) if last_exc is not None else "unknown error"
            return

        for item in data:
            symbol = str(item.get("symbol", "")).upper()
            if not symbol:
                continue
            if symbol not in self._symbols:
                if not self._can_track_symbol(symbol):
                    continue
                self._symbols.add(symbol)
                self._rates.setdefault(symbol, 0.0)
            raw_rate = float(item.get("nextFundingRate") or item.get("lastFundingRate", 0.0))
            self._rates[symbol] = raw_rate * _FUNDING_PERIODS_PER_YEAR

        self._last_successful_refresh = datetime.now(timezone.utc)

    def update_rate(self, symbol: str, next_funding_rate: float) -> None:
        symbol = symbol.upper()
        if symbol not in self._symbols:
            if not self._can_track_symbol(symbol):
                return
            self._symbols.add(symbol)
            self._rates.setdefault(symbol, 0.0)
        self._rates[symbol] = float(next_funding_rate) * _FUNDING_PERIODS_PER_YEAR
        self._last_successful_refresh = datetime.now(timezone.utc)
        self._last_error = ""
        self._consecutive_failures = 0

    def status_snapshot(self) -> dict[str, Any]:
        age_seconds = None
        if self._last_successful_refresh is not None:
            age_seconds = (datetime.now(timezone.utc) - self._last_successful_refresh).total_seconds()
        stale = self._is_stale()
        return {
            "funding_staleness_status": "stale" if stale else "fresh",
            "funding_last_refresh_at": self._last_successful_refresh.isoformat() if self._last_successful_refresh else "",
            "funding_last_refresh_age_s": age_seconds,
            "funding_consecutive_failures": self._consecutive_failures,
            "funding_last_error": self._last_error,
        }

    def get_top_n(self, n: int) -> list[str]:
        return [symbol for symbol, _ in self.get_ranked()[:n]]

    def has_symbol(self, symbol: str) -> bool:
        return symbol.upper() in self._symbols

    def get_rate(self, symbol: str) -> float:
        if self._is_stale():
            return 0.0
        return self._rates.get(symbol.upper(), 0.0)

    def get_ranked(self) -> list[tuple[str, float]]:
        if self._is_stale():
            return []
        return sorted(self._rates.items(), key=lambda item: item[1], reverse=True)

    async def run_forever(self, interval_s: int = 60) -> None:
        while True:
            await self.refresh()
            await asyncio.sleep(interval_s)


def direction_from_funding(annualized_funding: float) -> str:
    return "LONG_SPOT_SHORT_PERP" if annualized_funding >= 0 else "SHORT_SPOT_LONG_PERP"


def evaluate_candidate(candidate: MarketCandidate, cfg: dict[str, Any]) -> CandidateSnapshot:
    reasons: list[str] = []
    if cfg.get("scanner_require_spot_and_perp", True) and not candidate.has_spot:
        reasons.append("missing_spot_pair")
    if candidate.depth_usd < max(cfg.get("scanner_min_depth_usd", 0.0), cfg.get("scanner_min_depth_multiplier", 1.0) * cfg.get("notional_per_trade", 0.0)):
        reasons.append("low_depth")
    if candidate.spread_bps > cfg.get("scanner_max_spread_bps", math.inf):
        reasons.append("wide_spread")
    if candidate.listing_age_days < cfg.get("scanner_min_listing_age_days", 0):
        reasons.append("new_listing")
    if candidate.data_staleness_seconds > cfg.get("scanner_max_data_stale_seconds", math.inf):
        reasons.append("stale_data")
    if candidate.is_delist_risk:
        reasons.append("delist_risk")
    if candidate.toxicity_bps > cfg.get("scanner_max_toxic_spread_bps", math.inf):
        reasons.append("structural_toxicity")

    status = "accepted" if not reasons else "rejected"
    direction = candidate.direction or direction_from_funding(candidate.annualized_funding)
    metrics = {
        "annualized_funding": candidate.annualized_funding,
        "basis_pct": candidate.basis_pct,
        "spread_bps": candidate.spread_bps,
        "depth_usd": candidate.depth_usd,
        "realized_volatility": candidate.realized_volatility,
        "basis_stability": candidate.basis_stability,
        "regime_health": candidate.regime_health,
        "listing_age_days": candidate.listing_age_days,
        "data_staleness_seconds": candidate.data_staleness_seconds,
        "toxicity_bps": candidate.toxicity_bps,
        "imbalance": candidate.imbalance,
        "mark_price": candidate.mark_price,
        **candidate.metadata,
    }
    return CandidateSnapshot(
        cycle_id="",
        symbol=candidate.symbol,
        direction=direction,
        accepted=not reasons,
        status=status,
        cluster=candidate.cluster or PORTFOLIO_CLUSTER_MAP.get(candidate.symbol, DEFAULT_CLUSTER),
        rejection_reasons=reasons,
        metrics=metrics,
        snapshot_time=datetime.now(timezone.utc).isoformat(),
    )


def _winsorized_percentile(values: list[float], value: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return 1.0
    q = quantiles(values, n=100, method="inclusive")
    lower = q[4]
    upper = q[94]
    clipped = min(max(value, lower), upper)
    less = sum(1 for item in values if item <= clipped)
    return less / len(values)


def rank_candidates(
    cycle_id: str,
    candidates: list[MarketCandidate],
    cfg: dict[str, Any],
) -> tuple[list[CandidateSnapshot], list[OpportunityScore]]:
    snapshots: list[CandidateSnapshot] = []
    accepted: list[tuple[MarketCandidate, CandidateSnapshot]] = []

    for candidate in candidates:
        snapshot = evaluate_candidate(candidate, cfg)
        snapshot.cycle_id = cycle_id
        snapshots.append(snapshot)
        if snapshot.accepted:
            accepted.append((candidate, snapshot))

    if not accepted:
        return snapshots, []

    funding_edges: list[float] = []
    depths: list[float] = []
    spreads: list[float] = []
    vols: list[float] = []
    basis_scores: list[float] = []
    regime_scores: list[float] = []
    edges: dict[str, float] = {}

    for candidate, _ in accepted:
        edge = estimate_trade_edge(
            candidate.annualized_funding,
            CostContext(
                size_usd=float(cfg.get("notional_per_trade", 0.0)),
                depth_usd=candidate.depth_usd,
                spread_bps=candidate.spread_bps,
            ),
        )
        predicted_bps = edge.net_edge_pct * 10_000.0
        edges[candidate.symbol] = predicted_bps
        funding_edges.append(predicted_bps)
        depths.append(candidate.depth_usd)
        spreads.append(-candidate.spread_bps)
        vols.append(-candidate.realized_volatility)
        basis_scores.append(candidate.basis_stability)
        regime_scores.append(candidate.regime_health)

    weights = cfg.get("ranker_weights", {})
    ranked: list[tuple[MarketCandidate, CandidateSnapshot, float, dict[str, float]]] = []
    for candidate, snapshot in accepted:
        components = {
            "net_edge": _winsorized_percentile(funding_edges, edges[candidate.symbol]),
            "depth": _winsorized_percentile(depths, candidate.depth_usd),
            "spread": _winsorized_percentile(spreads, -candidate.spread_bps),
            "volatility": _winsorized_percentile(vols, -candidate.realized_volatility),
            "basis_stability": _winsorized_percentile(basis_scores, candidate.basis_stability),
            "regime_health": _winsorized_percentile(regime_scores, candidate.regime_health),
        }
        total_score = sum(components[name] * float(weights.get(name, 0.0)) for name in components)
        ranked.append((candidate, snapshot, total_score, components))

    ranked.sort(key=lambda item: (item[2], edges[item[0].symbol]), reverse=True)

    scores: list[OpportunityScore] = []
    for rank, (candidate, snapshot, total_score, components) in enumerate(ranked, start=1):
        snapshot.rank = rank
        scores.append(
            OpportunityScore(
                cycle_id=cycle_id,
                symbol=candidate.symbol,
                total_score=total_score,
                predicted_net_edge_bps=edges[candidate.symbol],
                rank=rank,
                selected=False,
                component_scores=components,
                expected_holding_hours=8.0,
            )
        )

    return snapshots, scores
