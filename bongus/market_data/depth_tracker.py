"""In-memory order-book and spread tracker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


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


class DepthTracker:
    def __init__(self) -> None:
        self._snapshots: dict[str, DepthSnapshot] = {}

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
