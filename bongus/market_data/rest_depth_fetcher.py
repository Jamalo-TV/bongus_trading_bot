"""REST-based depth fetcher as fallback when WebSocket depth is unavailable.

Binance provides 24h ticker data that includes quote volume, which we can use
as a proxy for liquidity when the WebSocket L2Depth feed isn't providing data.

Additionally, we poll the /fapi/v1/depth endpoint for futures depth as a fallback.
"""

import asyncio
import logging
import time

import requests

logger = logging.getLogger(__name__)

# Top N levels to sum for depth estimation
_TOP_N = 20  # Increased to capture more of the order book
# How long to trust REST depth before requiring WS refresh (seconds)
_REST_DEPTH_TTL_SEC = 30.0


class RestDepthFetcher:
    """Fetches depth data from Binance REST APIs as a fallback."""

    def __init__(self, symbols: list[str]) -> None:
        self._symbols = [s.upper() for s in symbols]
        self._last_fetch: dict[str, float] = {}
        self._spot_depths: dict[str, float] = {}   # symbol -> USD depth
        self._perp_depths: dict[str, float] = {}   # symbol -> USD depth
        self._spot_quotes: dict[str, tuple[float, float]] = {}
        self._perp_quotes: dict[str, tuple[float, float]] = {}
        self._running = False

    def _get_spot_depth_usd(self, symbol: str) -> float:
        """Get cached spot depth in USD, or 0 if stale/missing."""
        if symbol not in self._spot_depths:
            return 0.0
        age = time.time() - self._last_fetch.get(symbol, 0)
        if age > _REST_DEPTH_TTL_SEC:
            return 0.0
        return self._spot_depths.get(symbol, 0.0)

    def _get_perp_depth_usd(self, symbol: str) -> float:
        """Get cached perp depth in USD, or 0 if stale/missing."""
        if symbol not in self._perp_depths:
            return 0.0
        age = time.time() - self._last_fetch.get(symbol, 0)
        if age > _REST_DEPTH_TTL_SEC:
            return 0.0
        return self._perp_depths.get(symbol, 0.0)

    def get_entry_depth(self, symbol: str) -> float:
        """Return min(spot_ask, perp_bid) from REST fallback. Returns 0 if stale."""
        return min(self._get_spot_depth_usd(symbol), self._get_perp_depth_usd(symbol))

    def get_exit_depth(self, symbol: str) -> float:
        """Return min(spot_bid, perp_ask) from REST fallback. Returns 0 if stale."""
        return min(self._get_spot_depth_usd(symbol), self._get_perp_depth_usd(symbol))

    async def fetch_depth_for_symbol(self, symbol: str) -> None:
        """Fetch depth from both spot and perp REST endpoints for a symbol."""
        try:
            # Fetch spot depth from ticker 24hr (quote volume as liquidity proxy)
            spot_depth, spot_best_bid, spot_best_ask = await self._fetch_spot_depth(symbol)

            # Fetch perp depth from order book endpoint
            perp_depth, perp_best_bid, perp_best_ask = await self._fetch_perp_depth(symbol)

            # Store results - each symbol writes its own keys; no cross-symbol conflict
            self._spot_depths[symbol] = spot_depth
            self._perp_depths[symbol] = perp_depth
            self._spot_quotes[symbol] = (spot_best_bid, spot_best_ask)
            self._perp_quotes[symbol] = (perp_best_bid, perp_best_ask)
            self._last_fetch[symbol] = time.time()

            logger.debug(
                "REST depth for %s: spot=%.0f perp=%.0f",
                symbol, spot_depth, perp_depth
            )
        except Exception as exc:
            logger.warning("Failed to fetch REST depth for %s: %s", symbol, exc)

    async def _fetch_spot_depth(self, symbol: str) -> tuple[float, float, float]:
        """Fetch spot order book depth from Binance API.
        
        Strategy:
        1. Try order book depth endpoint (most accurate for top levels)
        2. Fall back to quote volume estimate if depth endpoint fails
        """
        try:
            # First try order book depth endpoint
            depth_resp = await asyncio.to_thread(
                requests.get,
                "https://api.binance.com/api/v3/depth",
                params={"symbol": symbol, "limit": 20},  # Get more levels for better estimate
                timeout=5,
            )
            if depth_resp.ok:
                depth_data = depth_resp.json()
                bids = depth_data.get("bids", [])
                asks = depth_data.get("asks", [])
                # Sum top levels: price * qty
                bid_usd = sum(float(p) * float(q) for p, q in bids[:_TOP_N])
                ask_usd = sum(float(p) * float(q) for p, q in asks[:_TOP_N])
                # Use the lower of the two as the bottleneck
                best_bid = float(bids[0][0]) if bids else 0.0
                best_ask = float(asks[0][0]) if asks else 0.0
                return min(bid_usd, ask_usd), best_bid, best_ask

            # Fallback: use quote volume from 24hr ticker
            resp = await asyncio.to_thread(
                requests.get,
                f"https://api.binance.com/api/v3/ticker/24hr",
                params={"symbol": symbol},
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json()
            quote_volume = float(data.get("quoteVolume", 0))
            # Quote volume represents 24h of trading - scale down to estimate
            # instantaneous depth. Use 1% as rough factor for available liquidity
            return quote_volume * 0.01, 0.0, 0.0
        except Exception as exc:
            logger.debug("Spot depth fetch failed for %s: %s", symbol, exc)
            return 0.0, 0.0, 0.0

    async def _fetch_perp_depth(self, symbol: str) -> tuple[float, float, float]:
        """Fetch perp order book depth from Binance Futures API."""
        try:
            resp = await asyncio.to_thread(
                requests.get,
"https://fapi.binance.com/fapi/v1/depth",
                params={"symbol": symbol, "limit": _TOP_N},
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json()

            bids = data.get("bids", [])
            asks = data.get("asks", [])

            # Sum the configured number of levels: price * qty
            bid_usd = sum(float(p) * float(q) for p, q in bids[:_TOP_N])
            ask_usd = sum(float(p) * float(q) for p, q in asks[:_TOP_N])

            # Return the bottleneck (lower of bid/ask depth)
            best_bid = float(bids[0][0]) if bids else 0.0
            best_ask = float(asks[0][0]) if asks else 0.0
            return min(bid_usd, ask_usd), best_bid, best_ask
        except Exception as exc:
            logger.debug("Perp depth fetch failed for %s: %s", symbol, exc)
            return 0.0, 0.0, 0.0

    async def refresh_all(self) -> None:
        """Fetch depth for all monitored symbols concurrently."""
        tasks = [self.fetch_depth_for_symbol(s) for s in self._symbols]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def run_forever(self, interval_s: float = 30.0) -> None:
        """Continuously poll REST API for depth data."""
        self._running = True
        while self._running:
            try:
                await self.refresh_all()
            except Exception as exc:
                logger.error("Error in REST depth polling: %s", exc)
            await asyncio.sleep(interval_s)

    def update_symbols(self, symbols: list[str]) -> None:
        """Replace the symbol list dynamically and prune caches for removed symbols."""
        new_set = {s.upper() for s in symbols}
        self._symbols = list(new_set)
        for stale in list(self._last_fetch.keys()):
            if stale not in new_set:
                self._last_fetch.pop(stale, None)
                self._spot_depths.pop(stale, None)
                self._perp_depths.pop(stale, None)
                self._spot_quotes.pop(stale, None)
                self._perp_quotes.pop(stale, None)

    def get_snapshot(self, symbol: str) -> dict[str, float]:
        spot_best_bid, spot_best_ask = self._spot_quotes.get(symbol, (0.0, 0.0))
        perp_best_bid, perp_best_ask = self._perp_quotes.get(symbol, (0.0, 0.0))
        return {
            "spot_depth_usd": self._spot_depths.get(symbol, 0.0),
            "perp_depth_usd": self._perp_depths.get(symbol, 0.0),
            "spot_best_bid": spot_best_bid,
            "spot_best_ask": spot_best_ask,
            "perp_best_bid": perp_best_bid,
            "perp_best_ask": perp_best_ask,
        }

    def stop(self) -> None:
        """Stop the polling loop."""
        self._running = False

    def has_fresh_depth(self, symbol: str) -> bool:
        """Check if we have non-stale depth data for a symbol."""
        return (
            symbol in self._last_fetch
            and time.time() - self._last_fetch[symbol] < _REST_DEPTH_TTL_SEC
        )
