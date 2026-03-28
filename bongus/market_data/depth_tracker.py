"""Per-symbol order book depth tracker with separate spot and perp caches.

Maintains four USD-denominated depth caches per symbol:
  spot_bid_usd  — bid-side depth of spot book (used for exit: selling spot)
  spot_ask_usd  — ask-side depth of spot book (used for entry: buying spot)
  perp_bid_usd  — bid-side depth of perp book (used for entry: shorting perp)
  perp_ask_usd  — ask-side depth of perp book (used for exit: covering perp short)

Binance returns qty in base asset units (e.g., BTC). Multiplying price × qty
yields USD notional.

Supports REST fallback via set_rest_depth() for when WebSocket depth is unavailable.
WebSocket depth takes priority - only overwritten by REST if WS depth is 0.
"""

from dataclasses import dataclass
from typing import Optional
import logging
import time

logger = logging.getLogger(__name__)

_TOP_N = 20  # Increased to capture more of the order book


@dataclass
class _SymbolDepth:
    spot_bid_usd: float = 0.0
    spot_ask_usd: float = 0.0
    perp_bid_usd: float = 0.0
    perp_ask_usd: float = 0.0
    ws_spot_updated: float = 0.0  # timestamp of last WS update
    ws_perp_updated: float = 0.0


class DepthTracker:
    def __init__(self) -> None:
        self._depths: dict[str, _SymbolDepth] = {}

    def on_l2depth(
        self,
        symbol: str,
        market: str,
        bids: list[tuple[float, float]],
        asks: list[tuple[float, float]],
    ) -> None:
        """Update depth caches for a symbol from a L2Depth event.

        market: "spot" or "perp" (matches MarketType serde output from Rust)
        bids/asks: list of (price, qty) tuples — qty in base asset units
        """
        if symbol not in self._depths:
            self._depths[symbol] = _SymbolDepth()

        bid_usd = sum(p * q for p, q in bids[:_TOP_N])
        ask_usd = sum(p * q for p, q in asks[:_TOP_N])

        depth = self._depths[symbol]
        now = time.time()
        if market == "spot":
            depth.spot_bid_usd = bid_usd
            depth.spot_ask_usd = ask_usd
            depth.ws_spot_updated = now
        elif market == "perp":
            depth.perp_bid_usd = bid_usd
            depth.perp_ask_usd = ask_usd
            depth.ws_perp_updated = now

    def set_rest_depth(
        self,
        symbol: str,
        spot_depth_usd: float,
        perp_depth_usd: float,
    ) -> None:
        """Set depth from REST fallback (e.g., Binance /depth endpoint).

        Only overwrites if:
        1. WebSocket data is stale (> 60s old), OR
        2. WebSocket data is zero (not yet received)

        This ensures WebSocket always takes priority when available.
        """
        if symbol not in self._depths:
            self._depths[symbol] = _SymbolDepth()

        depth = self._depths[symbol]
        now = time.time()
        ws_stale_seconds = 60.0

        # Only use REST if WS is stale or zero
        spot_ws_stale = (now - depth.ws_spot_updated > ws_stale_seconds) if depth.ws_spot_updated > 0 else True
        perp_ws_stale = (now - depth.ws_perp_updated > ws_stale_seconds) if depth.ws_perp_updated > 0 else True

        # Update spot if WS is stale and REST has valid data
        if (spot_ws_stale or depth.spot_bid_usd == 0) and spot_depth_usd > 0:
            depth.spot_bid_usd = spot_depth_usd
            depth.spot_ask_usd = spot_depth_usd
            logger.debug("REST fallback applied for %s spot: %.0f USD", symbol, spot_depth_usd)

        # Update perp if WS is stale and REST has valid data
        if (perp_ws_stale or depth.perp_bid_usd == 0) and perp_depth_usd > 0:
            depth.perp_bid_usd = perp_depth_usd
            depth.perp_ask_usd = perp_depth_usd
            logger.debug("REST fallback applied for %s perp: %.0f USD", symbol, perp_depth_usd)

    # ── Entry/Exit convenience methods ────────────────────────────────────────

    def get_entry_depth(self, symbol: str) -> float:
        """Bottleneck USD depth for entering a delta-neutral position.

        Entry = buy spot (hits asks) + short perp (hits bids).
        Returns min(spot_ask, perp_bid).
        """
        d = self._depths.get(symbol, _SymbolDepth())
        return min(d.spot_ask_usd, d.perp_bid_usd)

    def get_exit_depth(self, symbol: str) -> float:
        """Bottleneck USD depth for exiting a delta-neutral position.

        Exit = sell spot (hits bids) + cover perp short (hits asks).
        Returns min(spot_bid, perp_ask).
        """
        d = self._depths.get(symbol, _SymbolDepth())
        return min(d.spot_bid_usd, d.perp_ask_usd)

    # ── Individual cache accessors ────────────────────────────────────────────

    def spot_ask_depth(self, symbol: str) -> float:
        return self._depths.get(symbol, _SymbolDepth()).spot_ask_usd

    def spot_bid_depth(self, symbol: str) -> float:
        return self._depths.get(symbol, _SymbolDepth()).spot_bid_usd

    def perp_bid_depth(self, symbol: str) -> float:
        return self._depths.get(symbol, _SymbolDepth()).perp_bid_usd

    def perp_ask_depth(self, symbol: str) -> float:
        return self._depths.get(symbol, _SymbolDepth()).perp_ask_usd
