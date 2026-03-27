"""Funding rate ranker — single REST call, filtered to monitored symbols, sorted highest-first.

Uses asyncio.to_thread to run the blocking requests.get call off the event loop.
Does NOT open parallel requests — Binance returns all symbols in one response.
"""

import asyncio
import logging
from datetime import datetime, timezone

import requests

from bongus.core.config import MAX_MONITORED_SYMBOLS

logger = logging.getLogger(__name__)

_ENDPOINT = "https://fapi.binance.com/fapi/v1/premiumIndex"
_FUNDING_PERIODS_PER_YEAR = 1095  # 3 per day × 365
# If no successful refresh within one funding interval, treat rates as unknown (conservative floor)
_MAX_STALENESS_SECONDS = 8 * 60 * 60  # 8 hours


class FundingRanker:
    def __init__(self, symbols: list[str] | None = None) -> None:
        self._dynamic: bool = symbols is None
        self._symbols: set[str] = set(symbols) if symbols is not None else set()
        self._rates: dict[str, float] = {s: 0.0 for s in self._symbols}
        self._last_successful_refresh: datetime | None = None

    def _is_stale(self) -> bool:
        if self._last_successful_refresh is None:
            return True
        age = (datetime.now(timezone.utc) - self._last_successful_refresh).total_seconds()
        return age > _MAX_STALENESS_SECONDS

    async def refresh(self) -> None:
        """Fetch all funding rates in a single request and update the cache.

        Uses nextFundingRate (predicted rate for upcoming settlement) for ranking
        and entry/rotation decisions. lastFundingRate is what was actually paid
        at the last snapshot and can diverge significantly in volatile conditions.

        Binance /fapi/v1/premiumIndex with no symbol param returns every market.
        We filter in Python for our monitored symbols.
        """
        try:
            resp = await asyncio.to_thread(
                requests.get, _ENDPOINT, timeout=10
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("FundingRanker: HTTP request failed: %s", exc)
            return

        if self._dynamic:
            # In dynamic mode populate _symbols from all perp markets returned,
            # capped at MAX_MONITORED_SYMBOLS.
            for item in data:
                symbol = item.get("symbol", "")
                if not symbol:
                    continue
                if symbol not in self._symbols:
                    if len(self._symbols) >= MAX_MONITORED_SYMBOLS:
                        continue
                    self._symbols.add(symbol)
                    self._rates.setdefault(symbol, 0.0)
                raw_rate = float(
                    item.get("nextFundingRate") or item.get("lastFundingRate", 0.0)
                )
                self._rates[symbol] = raw_rate * _FUNDING_PERIODS_PER_YEAR
        else:
            for item in data:
                symbol = item.get("symbol", "")
                if symbol not in self._symbols:
                    continue
                # Prefer nextFundingRate (forward-looking) for entry/rotation decisions.
                # Fall back to lastFundingRate if nextFundingRate is absent (e.g. just after settlement).
                raw_rate = float(
                    item.get("nextFundingRate") or item.get("lastFundingRate", 0.0)
                )
                self._rates[symbol] = raw_rate * _FUNDING_PERIODS_PER_YEAR

        self._last_successful_refresh = datetime.now(timezone.utc)

    def update_rate(self, symbol: str, next_funding_rate: float) -> None:
        """Update the annualized rate for a symbol from a live WS markPriceUpdate.

        Called by RustDataSubscriber on every MarkPrice event (~1s cadence),
        providing sub-minute funding rate resolution vs the 60s REST fallback.
        Resets the staleness clock so get_rate() doesn't return 0.0.

        In dynamic mode, adds new symbols seen in WS events to _symbols if
        the cap has not been reached.
        """
        if symbol not in self._symbols:
            if self._dynamic and len(self._symbols) < MAX_MONITORED_SYMBOLS:
                self._symbols.add(symbol)
                self._rates.setdefault(symbol, 0.0)
            else:
                return
        self._rates[symbol] = float(next_funding_rate) * _FUNDING_PERIODS_PER_YEAR
        self._last_successful_refresh = datetime.now(timezone.utc)

    def get_top_n(self, n: int) -> list[str]:
        """Return the top-N symbols by annualized funding rate, highest first.

        Used by DepthTracker to determine which symbols warrant a WS depth stream.
        Returns an empty list when the rate cache is stale.
        """
        ranked = self.get_ranked()
        return [sym for sym, _ in ranked[:n]]

    def has_symbol(self, symbol: str) -> bool:
        """Return True if the symbol is in the tracked set (regardless of staleness)."""
        return symbol in self._symbols

    def get_rate(self, symbol: str) -> float:
        """Return annualized funding rate for symbol.

        Returns 0.0 if not tracked or if the cache is older than one funding
        interval — conservative floor that prevents stale data from keeping
        positions open or triggering incorrect rotations.
        """
        if self._is_stale():
            logger.warning("FundingRanker: rate cache is stale — returning 0.0 for %s", symbol)
            return 0.0
        return self._rates.get(symbol, 0.0)

    def get_ranked(self) -> list[tuple[str, float]]:
        """Return all monitored symbols sorted by annualized rate magnitude (if inverse enabled) or absolute rate, highest first."""
        if self._is_stale():
            logger.warning("FundingRanker: rate cache is stale — returning empty ranking")
            return []

        # Import dynamically to avoid circular import issues if any
        from bongus.core.config import INVERSE_FUNDING_ENABLED
        if INVERSE_FUNDING_ENABLED:
            return sorted(self._rates.items(), key=lambda x: abs(x[1]), reverse=True)
        else:
            return sorted(self._rates.items(), key=lambda x: x[1], reverse=True)

    async def run_forever(self, interval_s: int = 60) -> None:
        """Refresh funding rates on a fixed interval. Runs indefinitely."""
        while True:
            await self.refresh()
            await asyncio.sleep(interval_s)
