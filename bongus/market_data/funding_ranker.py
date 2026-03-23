"""Funding rate ranker — single REST call, filtered to monitored symbols, sorted highest-first.

Uses asyncio.to_thread to run the blocking requests.get call off the event loop.
Does NOT open parallel requests — Binance returns all symbols in one response.
"""

import asyncio
import logging

import requests

logger = logging.getLogger(__name__)

_ENDPOINT = "https://fapi.binance.com/fapi/v1/premiumIndex"
_FUNDING_PERIODS_PER_YEAR = 1095  # 3 per day × 365


class FundingRanker:
    def __init__(self, symbols: list[str]) -> None:
        self._symbols: set[str] = set(symbols)
        self._rates: dict[str, float] = {s: 0.0 for s in symbols}

    async def refresh(self) -> None:
        """Fetch all funding rates in a single request and update the cache.

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

        for item in data:
            symbol = item.get("symbol", "")
            if symbol not in self._symbols:
                continue
            raw_rate = float(item.get("lastFundingRate", 0.0))
            self._rates[symbol] = raw_rate * _FUNDING_PERIODS_PER_YEAR

    def get_rate(self, symbol: str) -> float:
        """Return annualized funding rate for symbol, or 0.0 if not tracked."""
        return self._rates.get(symbol, 0.0)

    def get_ranked(self) -> list[tuple[str, float]]:
        """Return all monitored symbols sorted by annualized rate, highest first."""
        return sorted(self._rates.items(), key=lambda x: x[1], reverse=True)

    async def run_forever(self, interval_s: int = 60) -> None:
        """Refresh funding rates on a fixed interval. Runs indefinitely."""
        while True:
            await self.refresh()
            await asyncio.sleep(interval_s)
