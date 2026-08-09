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

from bongus.core.binance_endpoints import get_rest_base_urls
from bongus.core.config import (
    DEFAULT_CLUSTER,
    FUNDING_PERIODS_PER_YEAR,
    MAX_FUNDING_SCAN_SYMBOLS,
    PORTFOLIO_CLUSTER_MAP,
)
from bongus.engine.cost_model import CostContext, estimate_trade_edge
from bongus.engine.state_store import CandidateSnapshot, OpportunityScore
from bongus.market_data.funding_calendar import FundingCalendar

logger = logging.getLogger(__name__)

_MAX_STALENESS_SECONDS = 8 * 60 * 60
_MAX_RETRIES = 3
_BASE_RETRY_DELAY_S = 1.0
_FUNDING_INFO_REFRESH_SECONDS = 6 * 60 * 60


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
        self._raw_rates: dict[str, float] = {symbol: 0.0 for symbol in self._symbols}
        self._last_update_by_symbol: dict[str, datetime] = {}
        self._event_time_by_symbol: dict[str, datetime] = {}
        self._source_event_id_by_symbol: dict[str, str] = {}
        self._allowed_symbols: set[str] | None = None
        self._last_successful_refresh: datetime | None = None
        self._last_error = ""
        self._consecutive_failures = 0
        self._endpoint = f"{get_rest_base_urls()[0]}/fapi/v1/premiumIndex"
        self._funding_info_endpoint = f"{get_rest_base_urls()[0]}/fapi/v1/fundingInfo"
        self._last_funding_info_refresh: datetime | None = None
        self._funding_info_last_error = ""
        self.calendar = FundingCalendar()

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
        self._raw_rates = {
            symbol: rate for symbol, rate in self._raw_rates.items() if symbol in normalized
        }
        self._last_update_by_symbol = {
            symbol: updated_at
            for symbol, updated_at in self._last_update_by_symbol.items()
            if symbol in normalized
        }
        self._event_time_by_symbol = {
            symbol: event_time
            for symbol, event_time in self._event_time_by_symbol.items()
            if symbol in normalized
        }
        self._source_event_id_by_symbol = {
            symbol: event_id
            for symbol, event_id in self._source_event_id_by_symbol.items()
            if symbol in normalized
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

    def _last_update_for_symbol(self, symbol: str) -> datetime | None:
        # The global fallback preserves compatibility with restored/test state
        # written before per-symbol freshness was introduced.  Every current
        # REST/WS update populates the symbol map, so one live symbol cannot
        # freshen another in normal operation.
        return self._last_update_by_symbol.get(symbol.upper(), self._last_successful_refresh)

    def _is_stale(self, symbol: str | None = None) -> bool:
        updated_at = (
            self._last_successful_refresh
            if symbol is None
            else self._last_update_for_symbol(symbol)
        )
        if updated_at is None:
            return True
        age = (datetime.now(timezone.utc) - updated_at).total_seconds()
        return age > _MAX_STALENESS_SECONDS

    def _annualize(self, symbol: str, raw_rate: float) -> float:
        """Return the fixed reporting rate, independent of settlement timing.

        ``FundingCalendar`` remains authoritative for when cashflow settles.
        Reporting and ranking use the repository-wide raw-rate-times-1095
        convention even when Binance advertises a non-eight-hour interval.
        """

        del symbol
        return raw_rate * FUNDING_PERIODS_PER_YEAR

    async def _refresh_funding_info_if_due(self, now: datetime) -> None:
        if (
            self._last_funding_info_refresh is not None
            and (now - self._last_funding_info_refresh).total_seconds()
            < _FUNDING_INFO_REFRESH_SECONDS
        ):
            return
        try:
            response = await asyncio.to_thread(
                requests.get,
                self._funding_info_endpoint,
                timeout=10,
            )
            if hasattr(response, "raise_for_status"):
                response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError("fundingInfo response must be a list")
            self.calendar.update_funding_info(payload, observed_at=now)
            for symbol, raw_rate in self._raw_rates.items():
                self._rates[symbol] = self._annualize(symbol, raw_rate)
            self._last_funding_info_refresh = now
            self._funding_info_last_error = ""
        except Exception as exc:
            # Premium-index freshness remains independently usable.  The
            # default eight-hour schedule is conservative and the metadata
            # failure is exposed to risk/observability.
            self._funding_info_last_error = str(exc)
            logger.warning("Failed to refresh funding interval metadata: %s", exc)

    async def refresh(self) -> None:
        data = None
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                response = await asyncio.to_thread(requests.get, self._endpoint, timeout=10)
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

        refreshed_at = datetime.now(timezone.utc)
        await self._refresh_funding_info_if_due(refreshed_at)
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
            raw_rate = self.calendar.clamp_rate(symbol, raw_rate)
            self._raw_rates[symbol] = raw_rate
            self._rates[symbol] = self._annualize(symbol, raw_rate)
            self.calendar.update_premium_index(item, observed_at=refreshed_at)
            self._last_update_by_symbol[symbol] = refreshed_at
            event_time_ms = item.get("time")
            try:
                event_time = datetime.fromtimestamp(
                    float(event_time_ms) / 1_000.0,
                    tz=timezone.utc,
                )
                event_id = (
                    f"binance:premium-index:{symbol}:{event_time.isoformat()}:"
                    f"{raw_rate:.17g}:{item.get('nextFundingTime', '')}"
                )
            except (TypeError, ValueError, OSError, OverflowError):
                event_time = refreshed_at
                event_id = (
                    f"binance:premium-index-state:{symbol}:{raw_rate:.17g}:"
                    f"{item.get('nextFundingTime', '')}"
                )
            if self._source_event_id_by_symbol.get(symbol) == event_id:
                event_time = self._event_time_by_symbol.get(symbol, event_time)
            self._event_time_by_symbol[symbol] = event_time
            self._source_event_id_by_symbol[symbol] = event_id

        self._last_successful_refresh = refreshed_at

    def update_rate(
        self,
        symbol: str,
        next_funding_rate: float,
        *,
        next_funding_time_ms: int | float | None = None,
        event_time_ms: int | float | None = None,
        observed_at: datetime | None = None,
        source_event_id: str = "",
    ) -> None:
        symbol = symbol.upper()
        if symbol not in self._symbols:
            if not self._can_track_symbol(symbol):
                return
            self._symbols.add(symbol)
            self._rates.setdefault(symbol, 0.0)
        raw_rate = self.calendar.clamp_rate(symbol, float(next_funding_rate))
        self._raw_rates[symbol] = raw_rate
        self._rates[symbol] = self._annualize(symbol, raw_rate)
        updated_at = observed_at or datetime.now(timezone.utc)
        if updated_at.tzinfo is None or updated_at.utcoffset() is None:
            raise ValueError("funding observed_at must be timezone-aware")
        updated_at = updated_at.astimezone(timezone.utc)
        try:
            if event_time_ms is None:
                raise ValueError("missing event time")
            event_time = datetime.fromtimestamp(
                float(event_time_ms) / 1_000.0,
                tz=timezone.utc,
            )
        except (TypeError, ValueError, OSError, OverflowError):
            event_time = updated_at
        if next_funding_time_ms is not None:
            self.calendar.update_premium_index(
                {
                    "symbol": symbol,
                    "nextFundingTime": next_funding_time_ms,
                },
                observed_at=updated_at,
            )
        self._last_update_by_symbol[symbol] = updated_at
        if source_event_id.strip():
            event_id = source_event_id.strip()
        elif event_time_ms is not None:
            event_id = (
                f"binance:mark-price:{symbol}:{event_time.isoformat()}:"
                f"{raw_rate:.17g}:{next_funding_time_ms or ''}"
            )
        else:
            # The current Rust mark-price envelope does not expose Binance's
            # exchange event timestamp.  Treat an unchanged economic state as
            # one observation instead of inventing a fresh sample every
            # subscriber callback.
            event_id = (
                f"binance:mark-price-state:{symbol}:{raw_rate:.17g}:"
                f"{next_funding_time_ms or ''}"
            )
        if self._source_event_id_by_symbol.get(symbol) == event_id:
            event_time = self._event_time_by_symbol.get(symbol, event_time)
        self._event_time_by_symbol[symbol] = event_time
        self._source_event_id_by_symbol[symbol] = event_id
        self._last_successful_refresh = updated_at
        self._last_error = ""
        self._consecutive_failures = 0

    def status_snapshot(self) -> dict[str, Any]:
        age_seconds = None
        if self._last_successful_refresh is not None:
            age_seconds = (datetime.now(timezone.utc) - self._last_successful_refresh).total_seconds()
        stale = self._is_stale()
        stale_symbols = sorted(symbol for symbol in self._symbols if self._is_stale(symbol))
        fresh_symbol_count = max(0, len(self._symbols) - len(stale_symbols))
        funding_info_age_s = (
            (datetime.now(timezone.utc) - self._last_funding_info_refresh).total_seconds()
            if self._last_funding_info_refresh is not None
            else None
        )
        funding_metadata_ready = (
            funding_info_age_s is not None
            and funding_info_age_s <= _FUNDING_INFO_REFRESH_SECONDS * 2
        )
        return {
            "funding_staleness_status": "stale" if stale else "fresh",
            "funding_last_refresh_at": self._last_successful_refresh.isoformat() if self._last_successful_refresh else "",
            "funding_last_refresh_age_s": age_seconds,
            "funding_consecutive_failures": self._consecutive_failures,
            "funding_last_error": self._last_error,
            "funding_fresh_symbol_count": fresh_symbol_count,
            "funding_stale_symbol_count": len(stale_symbols),
            "funding_stale_symbols": stale_symbols,
            "funding_info_last_refresh_at": (
                self._last_funding_info_refresh.isoformat()
                if self._last_funding_info_refresh is not None
                else ""
            ),
            "funding_info_last_error": self._funding_info_last_error,
            "funding_info_age_s": funding_info_age_s,
            "funding_metadata_ready": funding_metadata_ready,
        }

    def minutes_to_next_settlement(self, symbol: str) -> float:
        return self.calendar.minutes_to_next(symbol)

    def minutes_since_last_settlement(self, symbol: str) -> float:
        now = datetime.now(timezone.utc)
        previous = self.calendar.previous_settlement(symbol, before=now)
        return max(0.0, (now - previous).total_seconds() / 60.0)

    def get_top_n(self, n: int) -> list[str]:
        return [symbol for symbol, _ in self.get_ranked()[:n]]

    def has_symbol(self, symbol: str) -> bool:
        return symbol.upper() in self._symbols

    def get_rate(self, symbol: str) -> float:
        if self._is_stale(symbol):
            return 0.0
        return self._rates.get(symbol.upper(), 0.0)

    def last_observed_rate(self, symbol: str) -> float | None:
        """Return the stored reporting rate without laundering its freshness."""

        return self._rates.get(symbol.upper())

    def get_raw_rate(self, symbol: str) -> float:
        """Return the per-settlement rate, never an annualized proxy."""

        if self._is_stale(symbol):
            return 0.0
        return self._raw_rates.get(symbol.upper(), 0.0)

    def data_age_seconds(self, symbol: str) -> float:
        updated_at = self._last_update_for_symbol(symbol.upper())
        if updated_at is None:
            return math.inf
        return max(0.0, (datetime.now(timezone.utc) - updated_at).total_seconds())

    def rate_observed_at(self, symbol: str) -> datetime | None:
        """Return the point-in-time timestamp backing a symbol's rate.

        Opportunity adapters use the timestamp directly so causality and
        staleness are validated inside the canonical kernel rather than being
        inferred from a mode-specific age calculation.
        """

        return self._last_update_for_symbol(symbol.upper())

    def rate_event_time(self, symbol: str) -> datetime | None:
        """Return the exchange event time without replacing availability time."""

        return self._event_time_by_symbol.get(symbol.upper())

    def rate_source_event_id(self, symbol: str) -> str:
        """Return the immutable identity of the last rate-bearing event."""

        return self._source_event_id_by_symbol.get(symbol.upper(), "")

    def funding_info_observed_at(self) -> datetime | None:
        """Return the last successful authoritative interval-info refresh."""

        return self._last_funding_info_refresh

    def get_ranked(self) -> list[tuple[str, float]]:
        fresh_rates = [
            (symbol, rate)
            for symbol, rate in self._rates.items()
            if not self._is_stale(symbol)
        ]
        return sorted(fresh_rates, key=lambda item: item[1], reverse=True)

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
