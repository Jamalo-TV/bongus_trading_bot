"""Order-book depth tracking for both canonical and legacy runtimes."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

_TOP_N = 20


@dataclass(slots=True)
class DepthSnapshot:
    symbol: str
    bid_price: float = 0.0
    ask_price: float = 0.0
    bid_depth_usd: float = 0.0
    ask_depth_usd: float = 0.0
    imbalance: float = 0.0
    updated_at: str = ""

    @property
    def mid_price(self) -> float:
        if self.bid_price <= 0 or self.ask_price <= 0:
            return 0.0
        return (self.bid_price + self.ask_price) / 2.0

    @property
    def spread_bps(self) -> float:
        mid = self.mid_price
        if mid <= 0:
            return 10_000.0
        return (self.ask_price - self.bid_price) / mid * 10_000.0

    @property
    def depth_usd(self) -> float:
        return min(self.bid_depth_usd, self.ask_depth_usd)


@dataclass(slots=True)
class _SymbolDepth:
    spot_bid_usd: float = 0.0
    spot_ask_usd: float = 0.0
    perp_bid_usd: float = 0.0
    perp_ask_usd: float = 0.0
    spot_best_bid: float = 0.0
    spot_best_ask: float = 0.0
    perp_best_bid: float = 0.0
    perp_best_ask: float = 0.0
    ws_spot_updated: float = 0.0
    ws_perp_updated: float = 0.0
    spot_updated: float = 0.0
    perp_updated: float = 0.0


class DepthTracker:
    def __init__(self) -> None:
        self._snapshots: dict[str, DepthSnapshot] = {}
        self._depths: dict[str, _SymbolDepth] = {}

    def on_book_ticker(self, symbol: str, bid_price: float, ask_price: float) -> None:
        snapshot = self._snapshots.setdefault(symbol, DepthSnapshot(symbol=symbol))
        snapshot.bid_price = bid_price
        snapshot.ask_price = ask_price
        snapshot.updated_at = datetime.now(timezone.utc).isoformat()

    def on_l2_depth(self, symbol: str, bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> None:
        snapshot = self._snapshots.setdefault(symbol, DepthSnapshot(symbol=symbol))
        snapshot.bid_depth_usd = sum(price * qty for price, qty in bids[:5])
        snapshot.ask_depth_usd = sum(price * qty for price, qty in asks[:5])
        total = snapshot.bid_depth_usd + snapshot.ask_depth_usd
        snapshot.imbalance = 0.0 if total <= 0 else (snapshot.bid_depth_usd - snapshot.ask_depth_usd) / total
        if bids:
            snapshot.bid_price = bids[0][0]
        if asks:
            snapshot.ask_price = asks[0][0]
        snapshot.updated_at = datetime.now(timezone.utc).isoformat()

    def on_l2depth(
        self,
        symbol: str,
        market: str,
        bids: list[tuple[float, float]],
        asks: list[tuple[float, float]],
    ) -> None:
        depth = self._depths.setdefault(symbol, _SymbolDepth())
        bid_usd = sum(price * qty for price, qty in bids[:_TOP_N])
        ask_usd = sum(price * qty for price, qty in asks[:_TOP_N])
        best_bid = bids[0][0] if bids else 0.0
        best_ask = asks[0][0] if asks else 0.0
        now = time.time()

        if market == "spot":
            depth.spot_bid_usd = bid_usd
            depth.spot_ask_usd = ask_usd
            depth.spot_best_bid = best_bid
            depth.spot_best_ask = best_ask
            depth.ws_spot_updated = now
            depth.spot_updated = now
        elif market == "perp":
            depth.perp_bid_usd = bid_usd
            depth.perp_ask_usd = ask_usd
            depth.perp_best_bid = best_bid
            depth.perp_best_ask = best_ask
            depth.ws_perp_updated = now
            depth.perp_updated = now

    def set_rest_snapshot(
        self,
        symbol: str,
        *,
        spot_depth_usd: float,
        perp_depth_usd: float,
        spot_bid_price: float = 0.0,
        spot_ask_price: float = 0.0,
        perp_bid_price: float = 0.0,
        perp_ask_price: float = 0.0,
    ) -> None:
        depth = self._depths.setdefault(symbol, _SymbolDepth())
        now = time.time()
        ws_stale_seconds = 60.0
        spot_ws_stale = (now - depth.ws_spot_updated > ws_stale_seconds) if depth.ws_spot_updated > 0 else True
        perp_ws_stale = (now - depth.ws_perp_updated > ws_stale_seconds) if depth.ws_perp_updated > 0 else True

        if spot_ws_stale:
            spot_updated = False
            if spot_depth_usd > 0:
                depth.spot_bid_usd = spot_depth_usd
                depth.spot_ask_usd = spot_depth_usd
                spot_updated = True
            if spot_bid_price > 0.0:
                depth.spot_best_bid = spot_bid_price
                spot_updated = True
            if spot_ask_price > 0.0:
                depth.spot_best_ask = spot_ask_price
                spot_updated = True
            if spot_updated:
                depth.spot_updated = now
        if perp_ws_stale:
            perp_updated = False
            if perp_depth_usd > 0:
                depth.perp_bid_usd = perp_depth_usd
                depth.perp_ask_usd = perp_depth_usd
                perp_updated = True
            if perp_bid_price > 0.0:
                depth.perp_best_bid = perp_bid_price
                perp_updated = True
            if perp_ask_price > 0.0:
                depth.perp_best_ask = perp_ask_price
                perp_updated = True
            if perp_updated:
                depth.perp_updated = now

    def set_rest_depth(self, symbol: str, spot_depth_usd: float, perp_depth_usd: float) -> None:
        self.set_rest_snapshot(
            symbol,
            spot_depth_usd=spot_depth_usd,
            perp_depth_usd=perp_depth_usd,
        )

    def get_entry_depth(self, symbol: str) -> float:
        depth = self._depths.get(symbol, _SymbolDepth())
        return min(depth.spot_ask_usd, depth.perp_bid_usd)

    def get_exit_depth(self, symbol: str) -> float:
        depth = self._depths.get(symbol, _SymbolDepth())
        return min(depth.spot_bid_usd, depth.perp_ask_usd)

    def spot_ask_depth(self, symbol: str) -> float:
        return self._depths.get(symbol, _SymbolDepth()).spot_ask_usd

    def spot_bid_depth(self, symbol: str) -> float:
        return self._depths.get(symbol, _SymbolDepth()).spot_bid_usd

    def perp_bid_depth(self, symbol: str) -> float:
        return self._depths.get(symbol, _SymbolDepth()).perp_bid_usd

    def perp_ask_depth(self, symbol: str) -> float:
        return self._depths.get(symbol, _SymbolDepth()).perp_ask_usd

    def spot_mid_price(self, symbol: str) -> float:
        depth = self._depths.get(symbol, _SymbolDepth())
        if depth.spot_best_bid <= 0.0 or depth.spot_best_ask <= 0.0:
            return 0.0
        return (depth.spot_best_bid + depth.spot_best_ask) / 2.0

    def perp_mid_price(self, symbol: str) -> float:
        depth = self._depths.get(symbol, _SymbolDepth())
        if depth.perp_best_bid <= 0.0 or depth.perp_best_ask <= 0.0:
            return 0.0
        return (depth.perp_best_bid + depth.perp_best_ask) / 2.0

    @staticmethod
    def _spread_bps(bid_price: float, ask_price: float) -> float:
        if bid_price <= 0.0 or ask_price <= 0.0 or ask_price < bid_price:
            return float("inf")
        mid = (bid_price + ask_price) / 2.0
        if mid <= 0.0:
            return float("inf")
        return (ask_price - bid_price) / mid * 10_000.0

    def spot_spread_bps(self, symbol: str) -> float:
        depth = self._depths.get(symbol, _SymbolDepth())
        return self._spread_bps(depth.spot_best_bid, depth.spot_best_ask)

    def perp_spread_bps(self, symbol: str) -> float:
        depth = self._depths.get(symbol, _SymbolDepth())
        return self._spread_bps(depth.perp_best_bid, depth.perp_best_ask)

    def entry_spread_bps(self, symbol: str) -> float:
        return self.spot_spread_bps(symbol) + self.perp_spread_bps(symbol)

    def entry_data_age_seconds(self, symbol: str) -> float:
        depth = self._depths.get(symbol)
        if depth is None or depth.spot_updated <= 0.0 or depth.perp_updated <= 0.0:
            return float("inf")
        now = time.time()
        return max(now - depth.spot_updated, now - depth.perp_updated)

    def has_entry_book(self, symbol: str) -> bool:
        depth = self._depths.get(symbol)
        if depth is None:
            return False
        return (
            depth.spot_best_ask > 0.0
            and depth.spot_best_bid > 0.0
            and depth.perp_best_ask > 0.0
            and depth.perp_best_bid > 0.0
            and depth.spot_ask_usd > 0.0
            and depth.perp_bid_usd > 0.0
        )

    def basis_pct(self, symbol: str) -> float | None:
        spot_mid = self.spot_mid_price(symbol)
        perp_mid = self.perp_mid_price(symbol)
        if spot_mid <= 0.0 or perp_mid <= 0.0:
            return None
        return (perp_mid - spot_mid) / spot_mid

    def get(self, symbol: str) -> DepthSnapshot | None:
        return self._snapshots.get(symbol)

    def snapshot(self) -> dict[str, DepthSnapshot]:
        return dict(self._snapshots)

    def as_dict(self, symbol: str) -> dict[str, Any]:
        snap = self._snapshots.get(symbol)
        if snap is None:
            return {}
        return {
            "symbol": snap.symbol,
            "bid_price": snap.bid_price,
            "ask_price": snap.ask_price,
            "spread_bps": snap.spread_bps,
            "depth_usd": snap.depth_usd,
            "imbalance": snap.imbalance,
            "updated_at": snap.updated_at,
        }
