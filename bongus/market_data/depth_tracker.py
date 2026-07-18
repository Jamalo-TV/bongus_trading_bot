"""Order-book depth tracking for both canonical and legacy runtimes."""

from __future__ import annotations

import time
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Sequence

_TOP_N = 20
DEFAULT_EXECUTABLE_BOOK_TTL_SECONDS = 5.0

BookMarket = Literal["spot", "perp"]
OrderSide = Literal["buy", "sell"]
PairDirection = Literal["long_spot_short_perp", "short_spot_long_perp"]
PairOperation = Literal["entry", "exit"]


@dataclass(frozen=True, slots=True)
class ExecutableLegCapacity:
    """Size-aware executable capacity for one leg of a paired order.

    ``available_notional_usd`` is the displayed capacity on the consumable side
    of the validated top-20 book.  ``executable_notional_usd`` is capped at the
    requested size.  A caller must check ``fully_executable`` before routing;
    partial displayed liquidity is reported for measurement, never treated as
    permission to trade.
    """

    symbol: str
    market: BookMarket
    side: OrderSide
    requested_notional_usd: float
    available_notional_usd: float
    executable_notional_usd: float
    base_quantity: float
    average_price: float
    worst_price: float
    impact_bps: float
    book_age_seconds: float
    fully_executable: bool
    rejection_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExecutablePairCapacity:
    """Bottleneck executable capacity for a delta-neutral two-leg action."""

    symbol: str
    direction: PairDirection
    operation: PairOperation
    requested_notional_usd: float
    available_notional_usd: float
    executable_notional_usd: float
    fully_executable: bool
    spot: ExecutableLegCapacity
    perp: ExecutableLegCapacity
    rejection_reasons: tuple[str, ...] = ()


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
    spot_book_updated: float = 0.0
    perp_book_updated: float = 0.0
    spot_bids: tuple[tuple[float, float], ...] = ()
    spot_asks: tuple[tuple[float, float], ...] = ()
    perp_bids: tuple[tuple[float, float], ...] = ()
    perp_asks: tuple[tuple[float, float], ...] = ()
    spot_book_errors: tuple[str, ...] = ("missing_book",)
    perp_book_errors: tuple[str, ...] = ("missing_book",)


class DepthTracker:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._snapshots: dict[str, DepthSnapshot] = {}
        self._depths: dict[str, _SymbolDepth] = {}
        self._clock = clock

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
        *,
        received_at: float | None = None,
    ) -> None:
        depth = self._depths.setdefault(symbol, _SymbolDepth())
        normalized_bids, normalized_asks, errors = self._validate_levels(bids, asks)
        bid_usd = sum(price * qty for price, qty in normalized_bids)
        ask_usd = sum(price * qty for price, qty in normalized_asks)
        best_bid = normalized_bids[0][0] if normalized_bids else 0.0
        best_ask = normalized_asks[0][0] if normalized_asks else 0.0
        book_now = self._clock() if received_at is None else float(received_at)
        legacy_wall_now = time.time()

        # An invalid update replaces the prior executable view.  Retaining an
        # older good book after a malformed/crossed update would make the book
        # look fresh and is therefore unsafe.  Legacy per-side aggregates keep
        # any independently valid side for monitoring compatibility; all new
        # executable-capacity APIs reject the complete snapshot via ``errors``.

        if market == "spot":
            depth.spot_bid_usd = bid_usd
            depth.spot_ask_usd = ask_usd
            depth.spot_best_bid = best_bid
            depth.spot_best_ask = best_ask
            depth.spot_bids = normalized_bids
            depth.spot_asks = normalized_asks
            depth.spot_book_errors = errors
            depth.spot_book_updated = book_now
            depth.ws_spot_updated = legacy_wall_now
            depth.spot_updated = legacy_wall_now
        elif market == "perp":
            depth.perp_bid_usd = bid_usd
            depth.perp_ask_usd = ask_usd
            depth.perp_best_bid = best_bid
            depth.perp_best_ask = best_ask
            depth.perp_bids = normalized_bids
            depth.perp_asks = normalized_asks
            depth.perp_book_errors = errors
            depth.perp_book_updated = book_now
            depth.ws_perp_updated = legacy_wall_now
            depth.perp_updated = legacy_wall_now

    @staticmethod
    def _validate_levels(
        bids: Sequence[Sequence[float]],
        asks: Sequence[Sequence[float]],
    ) -> tuple[tuple[tuple[float, float], ...], tuple[tuple[float, float], ...], tuple[str, ...]]:
        errors: list[str] = []

        def normalize(side: str, raw: Sequence[Sequence[float]]) -> tuple[tuple[float, float], ...]:
            values: list[tuple[float, float]] = []
            for level in raw[:_TOP_N]:
                if len(level) < 2:
                    errors.append(f"{side}_malformed_level")
                    continue
                try:
                    price = float(level[0])
                    quantity = float(level[1])
                except (TypeError, ValueError, OverflowError):
                    errors.append(f"{side}_non_numeric_level")
                    continue
                if not math.isfinite(price) or not math.isfinite(quantity):
                    errors.append(f"{side}_non_finite_level")
                    continue
                if price <= 0.0 or quantity <= 0.0:
                    errors.append(f"{side}_zero_or_negative_level")
                    continue
                values.append((price, quantity))
            if not values:
                errors.append(f"{side}_empty")
            return tuple(values)

        clean_bids = normalize("bid", bids)
        clean_asks = normalize("ask", asks)
        if any(clean_bids[index][0] < clean_bids[index + 1][0] for index in range(len(clean_bids) - 1)):
            errors.append("bids_unsorted")
        if any(clean_asks[index][0] > clean_asks[index + 1][0] for index in range(len(clean_asks) - 1)):
            errors.append("asks_unsorted")
        if clean_bids and clean_asks and clean_bids[0][0] > clean_asks[0][0]:
            errors.append("crossed_book")
        return clean_bids, clean_asks, tuple(dict.fromkeys(errors))

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

    def executable_leg_capacity(
        self,
        symbol: str,
        market: BookMarket,
        side: OrderSide,
        requested_notional_usd: float,
        *,
        max_age_seconds: float = DEFAULT_EXECUTABLE_BOOK_TTL_SECONDS,
        now: float | None = None,
    ) -> ExecutableLegCapacity:
        """Walk a fresh, validated book and estimate fillability at size.

        This method intentionally does not use REST aggregate depth.  A volume
        proxy cannot establish a size-aware VWAP or worst execution price.
        """

        requested = float(requested_notional_usd)
        reasons: list[str] = []
        if not math.isfinite(requested) or requested <= 0.0:
            reasons.append("invalid_requested_notional")
        if not math.isfinite(max_age_seconds) or max_age_seconds <= 0.0:
            reasons.append("invalid_book_ttl")

        depth = self._depths.get(symbol)
        if depth is None:
            reasons.append("missing_book")
            return self._empty_leg(symbol, market, side, requested, reasons)

        if market == "spot":
            bids, asks = depth.spot_bids, depth.spot_asks
            updated_at = depth.spot_book_updated
            reasons.extend(depth.spot_book_errors)
        elif market == "perp":
            bids, asks = depth.perp_bids, depth.perp_asks
            updated_at = depth.perp_book_updated
            reasons.extend(depth.perp_book_errors)
        else:
            reasons.append("unknown_market")
            return self._empty_leg(symbol, market, side, requested, reasons)

        checked_at = self._clock() if now is None else float(now)
        age = math.inf if updated_at <= 0.0 else max(0.0, checked_at - updated_at)
        if updated_at <= 0.0:
            reasons.append("missing_book_timestamp")
        elif not math.isfinite(checked_at) or checked_at < updated_at:
            reasons.append("book_clock_invalid")
        elif age > max_age_seconds:
            reasons.append("stale_book")

        levels = asks if side == "buy" else bids
        if side not in ("buy", "sell"):
            reasons.append("unknown_side")
        if not levels:
            reasons.append("empty_executable_side")

        reasons = list(dict.fromkeys(reasons))
        if reasons:
            return self._empty_leg(symbol, market, side, requested, reasons, age=age)

        available = sum(price * quantity for price, quantity in levels)
        remaining = requested
        executed_quote = 0.0
        executed_base = 0.0
        worst_price = 0.0
        for price, quantity in levels:
            level_quote = price * quantity
            take_quote = min(remaining, level_quote)
            if take_quote <= 0.0:
                break
            executed_quote += take_quote
            executed_base += take_quote / price
            worst_price = price
            remaining -= take_quote
            if remaining <= max(1e-9, requested * 1e-12):
                remaining = 0.0
                break

        average_price = 0.0 if executed_base <= 0.0 else executed_quote / executed_base
        best_price = levels[0][0]
        if average_price <= 0.0 or best_price <= 0.0:
            impact_bps = math.inf
        elif side == "buy":
            impact_bps = max(0.0, (average_price / best_price - 1.0) * 10_000.0)
        else:
            impact_bps = max(0.0, (1.0 - average_price / best_price) * 10_000.0)

        fully_executable = remaining <= 0.0
        fill_reasons: tuple[str, ...] = () if fully_executable else ("insufficient_displayed_depth",)
        return ExecutableLegCapacity(
            symbol=symbol,
            market=market,
            side=side,
            requested_notional_usd=requested,
            available_notional_usd=available,
            executable_notional_usd=executed_quote,
            base_quantity=executed_base,
            average_price=average_price,
            worst_price=worst_price,
            impact_bps=impact_bps,
            book_age_seconds=age,
            fully_executable=fully_executable,
            rejection_reasons=fill_reasons,
        )

    @staticmethod
    def _empty_leg(
        symbol: str,
        market: BookMarket,
        side: OrderSide,
        requested: float,
        reasons: Sequence[str],
        *,
        age: float = math.inf,
    ) -> ExecutableLegCapacity:
        return ExecutableLegCapacity(
            symbol=symbol,
            market=market,
            side=side,
            requested_notional_usd=requested,
            available_notional_usd=0.0,
            executable_notional_usd=0.0,
            base_quantity=0.0,
            average_price=0.0,
            worst_price=0.0,
            impact_bps=math.inf,
            book_age_seconds=age,
            fully_executable=False,
            rejection_reasons=tuple(dict.fromkeys(reasons)),
        )

    def executable_pair_capacity(
        self,
        symbol: str,
        requested_notional_usd: float,
        *,
        direction: PairDirection = "long_spot_short_perp",
        operation: PairOperation = "entry",
        max_age_seconds: float = DEFAULT_EXECUTABLE_BOOK_TTL_SECONDS,
        now: float | None = None,
    ) -> ExecutablePairCapacity:
        """Return independently walked spot/perp capacity and the bottleneck."""

        leg_sides: dict[tuple[PairDirection, PairOperation], tuple[OrderSide, OrderSide]] = {
            ("long_spot_short_perp", "entry"): ("buy", "sell"),
            ("long_spot_short_perp", "exit"): ("sell", "buy"),
            ("short_spot_long_perp", "entry"): ("sell", "buy"),
            ("short_spot_long_perp", "exit"): ("buy", "sell"),
        }
        sides = leg_sides.get((direction, operation))
        if sides is None:
            # Keep the return type total and fail closed for unrecognized input.
            spot_side: OrderSide = "buy"
            perp_side: OrderSide = "sell"
            spot = self._empty_leg(symbol, "spot", spot_side, float(requested_notional_usd), ("unknown_pair_action",))
            perp = self._empty_leg(symbol, "perp", perp_side, float(requested_notional_usd), ("unknown_pair_action",))
        else:
            spot = self.executable_leg_capacity(
                symbol,
                "spot",
                sides[0],
                requested_notional_usd,
                max_age_seconds=max_age_seconds,
                now=now,
            )
            perp = self.executable_leg_capacity(
                symbol,
                "perp",
                sides[1],
                requested_notional_usd,
                max_age_seconds=max_age_seconds,
                now=now,
            )

        reasons = tuple(
            [f"spot:{reason}" for reason in spot.rejection_reasons]
            + [f"perp:{reason}" for reason in perp.rejection_reasons]
        )
        available = min(spot.available_notional_usd, perp.available_notional_usd)
        executable = min(spot.executable_notional_usd, perp.executable_notional_usd)
        return ExecutablePairCapacity(
            symbol=symbol,
            direction=direction,
            operation=operation,
            requested_notional_usd=float(requested_notional_usd),
            available_notional_usd=available,
            executable_notional_usd=executable,
            fully_executable=spot.fully_executable and perp.fully_executable,
            spot=spot,
            perp=perp,
            rejection_reasons=reasons,
        )

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
