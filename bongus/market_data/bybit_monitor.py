"""Bybit funding rate monitor — single REST call for all linear symbols.

Fetches funding rates from Bybit for cross-validation of signal confidence.
Uses asyncio.to_thread to run the blocking requests.get call off the event loop.
Does NOT open parallel requests — Bybit returns all linear symbols in one response.
"""

import asyncio
import logging
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

_ENDPOINT = "https://api.bybit.com/v5/market/tickers?category=linear"
_FUNDING_PERIODS_PER_YEAR = 1095  # 3 per day × 365
# If no successful refresh within one funding interval, treat rates as unknown
_MAX_STALENESS_SECONDS = 8 * 60 * 60  # 8 hours


class BybitFundingMonitor:
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

    async def refresh(self) -> dict[str, float] | None:
        """Fetch all linear symbol funding rates in a single request and update the cache.

        Bybit /v5/market/tickers?category=linear returns every linear (perp) market.
        We parse result.list[].symbol and result.list[].fundingRate, then annualize
        by multiplying by 1095 (3 settlements per day × 365 days).

        Returns a snapshot of the updated rates dict on success, or None on HTTP failure.
        """
        try:
            resp = await asyncio.to_thread(
                requests.get, _ENDPOINT, timeout=10
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("BybitFundingMonitor: HTTP request failed: %s", exc)
            return None

        items = data.get("result", {}).get("list", [])

        if self._dynamic:
            for item in items:
                symbol = item.get("symbol", "")
                if not symbol:
                    continue
                if symbol not in self._symbols:
                    self._symbols.add(symbol)
                    self._rates.setdefault(symbol, 0.0)
                raw_rate = float(item.get("fundingRate") or 0.0)
                self._rates[symbol] = raw_rate * _FUNDING_PERIODS_PER_YEAR
        else:
            for item in items:
                symbol = item.get("symbol", "")
                if symbol not in self._symbols:
                    continue
                raw_rate = float(item.get("fundingRate") or 0.0)
                self._rates[symbol] = raw_rate * _FUNDING_PERIODS_PER_YEAR

        self._last_successful_refresh = datetime.now(timezone.utc)
        return dict(self._rates)

    def get_rates(self) -> dict[str, float] | None:
        """Return a snapshot of all cached rates without making an HTTP call.

        Returns None when the cache is stale (no successful refresh yet, or
        last refresh was more than 8 hours ago).
        """
        if self._is_stale():
            return None
        return dict(self._rates)

    def get_rate(self, symbol: str) -> float | None:
        """Return annualized funding rate for symbol, or None if not found or stale.

        Returns None when:
        - No successful refresh has been completed yet.
        - The last successful refresh was more than 8 hours ago.
        - The symbol is not present in the cached rates.
        """
        if self._is_stale():
            logger.warning(
                "BybitFundingMonitor: rate cache is stale — returning None for %s", symbol
            )
            return None
        return self._rates.get(symbol)

    async def run_forever(self, interval_s: int = 60) -> None:
        """Refresh funding rates on a fixed interval. Runs indefinitely."""
        while True:
            await self.refresh()
            await asyncio.sleep(interval_s)
