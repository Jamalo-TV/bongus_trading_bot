"""Order-book depth tracking for both canonical and legacy runtimes."""

from __future__ import annotations

import math
import time
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


@dataclass(frozen=True, slots=True)
class ExecutionBookSnapshot:
    """Point-in-time BBO evidence used by normalized TCA.

    ``complete`` is evidence quality, not trading permission.  Callers retain
    ``None`` for unavailable prices rather than converting a missing book into
    a zero-valued market observation.
    """

    symbol: str
    market: BookMarket
    side: OrderSide
    captured_at: str
    bid: float | None
    ask: float | None
    mid: float | None
    executable_price: float | None
    executable_depth_usd: float | None
    event_age_seconds: float | None
    connection_id: str | None
    final_update_id: int | None
    complete: bool
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
    spot_timing: _MarketDataTiming | None = None
    perp_timing: _MarketDataTiming | None = None


@dataclass(frozen=True, slots=True)
class _MarketDataTiming:
    connection_id: str
    exchange_event_time_ms: float | None
    freshness_time_ms: float
    receive_time_ms: float
    process_time_ms: float
    persist_time_ms: float | None
    first_update_id: int | None
    last_update_id: int | None
    final_update_id: int | None
    previous_final_update_id: int | None
    is_snapshot: bool
    sequence_contiguous: bool
    legacy: bool
    errors: tuple[str, ...] = ()


class DepthTracker:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        allow_legacy_timing: bool = False,
    ) -> None:
        self._snapshots: dict[str, DepthSnapshot] = {}
        self._depths: dict[str, _SymbolDepth] = {}
        self._clock = clock
        self._wall_clock = wall_clock
        self._allow_legacy_timing = bool(allow_legacy_timing)

    @property
    def requires_timing_envelope(self) -> bool:
        return not self._allow_legacy_timing

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
        connection_id: str | None = None,
        exchange_event_time_ms: int | float | None = None,
        receive_time_ms: int | float | None = None,
        process_time_ms: int | float | None = None,
        persist_time_ms: int | float | None = None,
        first_update_id: int | None = None,
        last_update_id: int | None = None,
        final_update_id: int | None = None,
        previous_final_update_id: int | None = None,
        is_snapshot: bool | None = None,
        sequence_contiguous: bool | None = None,
    ) -> None:
        depth = self._depths.setdefault(symbol, _SymbolDepth())
        normalized_bids, normalized_asks, errors = self._validate_levels(bids, asks)
        bid_usd = sum(price * qty for price, qty in normalized_bids)
        ask_usd = sum(price * qty for price, qty in normalized_asks)
        best_bid = normalized_bids[0][0] if normalized_bids else 0.0
        best_ask = normalized_asks[0][0] if normalized_asks else 0.0
        book_now = self._clock() if received_at is None else float(received_at)
        legacy_wall_now = self._wall_clock()

        # An invalid update replaces the prior executable view.  Retaining an
        # older good book after a malformed/crossed update would make the book
        # look fresh and is therefore unsafe.  Legacy per-side aggregates keep
        # any independently valid side for monitoring compatibility; all new
        # executable-capacity APIs reject the complete snapshot via ``errors``.

        if market == "spot":
            prior_timing = depth.spot_timing
            timing = self._build_market_timing(
                prior_timing,
                market="spot",
                received_at=received_at,
                connection_id=connection_id,
                exchange_event_time_ms=exchange_event_time_ms,
                receive_time_ms=receive_time_ms,
                process_time_ms=process_time_ms,
                persist_time_ms=persist_time_ms,
                first_update_id=first_update_id,
                last_update_id=last_update_id,
                final_update_id=final_update_id,
                previous_final_update_id=previous_final_update_id,
                is_snapshot=is_snapshot,
                sequence_contiguous=sequence_contiguous,
            )
            depth.spot_bid_usd = bid_usd
            depth.spot_ask_usd = ask_usd
            depth.spot_best_bid = best_bid
            depth.spot_best_ask = best_ask
            depth.spot_bids = normalized_bids
            depth.spot_asks = normalized_asks
            depth.spot_book_errors = errors
            depth.spot_timing = timing
            depth.spot_book_updated = book_now
            depth.ws_spot_updated = legacy_wall_now
            depth.spot_updated = timing.freshness_time_ms / 1_000.0
        elif market == "perp":
            prior_timing = depth.perp_timing
            timing = self._build_market_timing(
                prior_timing,
                market="perp",
                received_at=received_at,
                connection_id=connection_id,
                exchange_event_time_ms=exchange_event_time_ms,
                receive_time_ms=receive_time_ms,
                process_time_ms=process_time_ms,
                persist_time_ms=persist_time_ms,
                first_update_id=first_update_id,
                last_update_id=last_update_id,
                final_update_id=final_update_id,
                previous_final_update_id=previous_final_update_id,
                is_snapshot=is_snapshot,
                sequence_contiguous=sequence_contiguous,
            )
            depth.perp_bid_usd = bid_usd
            depth.perp_ask_usd = ask_usd
            depth.perp_best_bid = best_bid
            depth.perp_best_ask = best_ask
            depth.perp_bids = normalized_bids
            depth.perp_asks = normalized_asks
            depth.perp_book_errors = errors
            depth.perp_timing = timing
            depth.perp_book_updated = book_now
            depth.ws_perp_updated = legacy_wall_now
            depth.perp_updated = timing.freshness_time_ms / 1_000.0

    @staticmethod
    def _timestamp_ms(value: int | float | None) -> float | None:
        if isinstance(value, bool) or value is None:
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(numeric) or numeric <= 0.0:
            return None
        return numeric

    @staticmethod
    def _update_id(value: int | None) -> int | None:
        if isinstance(value, bool) or value is None:
            return None
        try:
            numeric = int(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return numeric if numeric >= 0 else None

    def _build_market_timing(
        self,
        previous: _MarketDataTiming | None,
        *,
        market: BookMarket,
        received_at: float | None,
        connection_id: str | None,
        exchange_event_time_ms: int | float | None,
        receive_time_ms: int | float | None,
        process_time_ms: int | float | None,
        persist_time_ms: int | float | None,
        first_update_id: int | None,
        last_update_id: int | None,
        final_update_id: int | None,
        previous_final_update_id: int | None,
        is_snapshot: bool | None,
        sequence_contiguous: bool | None,
    ) -> _MarketDataTiming:
        wall_now_ms = self._wall_clock() * 1_000.0
        explicit_legacy = received_at is not None and exchange_event_time_ms is None
        if exchange_event_time_ms is None and (
            self._allow_legacy_timing or explicit_legacy
        ):
            return _MarketDataTiming(
                connection_id=str(connection_id or "legacy"),
                exchange_event_time_ms=wall_now_ms,
                freshness_time_ms=wall_now_ms,
                receive_time_ms=wall_now_ms,
                process_time_ms=wall_now_ms,
                persist_time_ms=wall_now_ms,
                first_update_id=self._update_id(first_update_id),
                last_update_id=self._update_id(last_update_id),
                final_update_id=self._update_id(final_update_id),
                previous_final_update_id=self._update_id(
                    previous_final_update_id
                ),
                is_snapshot=bool(is_snapshot),
                sequence_contiguous=True,
                legacy=True,
            )

        normalized_connection_id = str(connection_id or "").strip()
        exchange_ms = self._timestamp_ms(exchange_event_time_ms)
        receive_ms = self._timestamp_ms(receive_time_ms)
        process_ms = self._timestamp_ms(process_time_ms)
        persist_ms = self._timestamp_ms(persist_time_ms)
        first_id = self._update_id(first_update_id)
        last_id = self._update_id(last_update_id)
        final_id = self._update_id(final_update_id)
        previous_final_id = self._update_id(previous_final_update_id)
        snapshot = is_snapshot is True
        contiguous = sequence_contiguous is True
        errors: list[str] = []

        if not normalized_connection_id:
            errors.append("missing_connection_id")
        receive_time_is_exchange_proxy = (
            market == "spot" and snapshot and exchange_ms is None
        )
        if exchange_ms is None and not receive_time_is_exchange_proxy:
            errors.append("missing_exchange_event_time")
        if receive_ms is None:
            errors.append("missing_receive_time")
        if process_ms is None:
            errors.append("missing_process_time")
        if (
            receive_ms is not None
            and process_ms is not None
            and (
                (exchange_ms is not None and not exchange_ms <= receive_ms <= process_ms)
                or (exchange_ms is None and receive_ms > process_ms)
                or (persist_ms is not None and process_ms > persist_ms)
            )
        ):
            errors.append("non_causal_timing_envelope")
        if final_id is None:
            errors.append("missing_final_update_id")
        if not (snapshot or contiguous):
            errors.append("depth_sequence_not_contiguous")

        if previous is not None and not previous.errors and final_id is not None:
            if previous.connection_id != normalized_connection_id and not snapshot:
                errors.append("connection_changed_without_snapshot")
            elif previous.connection_id == normalized_connection_id and not snapshot:
                prior_final_id = previous.final_update_id
                if prior_final_id is None:
                    errors.append("missing_sequence_baseline")
                else:
                    expected = prior_final_id + 1
                    if final_id <= prior_final_id:
                        errors.append("depth_sequence_regressed")
                    if (
                        previous_final_id is not None
                        and previous_final_id != prior_final_id
                    ):
                        errors.append("previous_final_update_id_mismatch")
                    if first_id is not None and not first_id <= expected <= final_id:
                        errors.append("depth_update_range_gap")
                    if previous_final_id is None and first_id is None:
                        errors.append("missing_depth_update_range")
        elif not snapshot:
            errors.append("missing_sequence_baseline")

        return _MarketDataTiming(
            connection_id=normalized_connection_id,
            exchange_event_time_ms=exchange_ms,
            freshness_time_ms=(
                exchange_ms
                if exchange_ms is not None
                else receive_ms
                if receive_ms is not None
                else 0.0
            ),
            receive_time_ms=receive_ms or 0.0,
            process_time_ms=process_ms or 0.0,
            persist_time_ms=persist_ms,
            first_update_id=first_id,
            last_update_id=last_id,
            final_update_id=final_id,
            previous_final_update_id=previous_final_id,
            is_snapshot=snapshot,
            sequence_contiguous=contiguous,
            legacy=False,
            errors=tuple(dict.fromkeys(errors)),
        )

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
        now = self._wall_clock()
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
                if self._allow_legacy_timing:
                    now_ms = now * 1_000.0
                    depth.spot_timing = _MarketDataTiming(
                        connection_id="legacy-rest",
                        exchange_event_time_ms=now_ms,
                        freshness_time_ms=now_ms,
                        receive_time_ms=now_ms,
                        process_time_ms=now_ms,
                        persist_time_ms=now_ms,
                        first_update_id=None,
                        last_update_id=None,
                        final_update_id=None,
                        previous_final_update_id=None,
                        is_snapshot=True,
                        sequence_contiguous=True,
                        legacy=True,
                    )
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
                if self._allow_legacy_timing:
                    now_ms = now * 1_000.0
                    depth.perp_timing = _MarketDataTiming(
                        connection_id="legacy-rest",
                        exchange_event_time_ms=now_ms,
                        freshness_time_ms=now_ms,
                        receive_time_ms=now_ms,
                        process_time_ms=now_ms,
                        persist_time_ms=now_ms,
                        first_update_id=None,
                        last_update_id=None,
                        final_update_id=None,
                        previous_final_update_id=None,
                        is_snapshot=True,
                        sequence_contiguous=True,
                        legacy=True,
                    )

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

    def execution_book_snapshot(
        self,
        symbol: str,
        market: BookMarket,
        side: OrderSide,
    ) -> ExecutionBookSnapshot:
        """Capture one causal BBO/depth record without inventing a limit price."""

        normalized_symbol = str(symbol or "").strip().upper()
        captured_at = datetime.fromtimestamp(
            self._wall_clock(), tz=timezone.utc
        ).isoformat()
        depth = self._depths.get(normalized_symbol)
        reasons: list[str] = []
        if depth is None:
            reasons.append("missing_book")
            return ExecutionBookSnapshot(
                symbol=normalized_symbol,
                market=market,
                side=side,
                captured_at=captured_at,
                bid=None,
                ask=None,
                mid=None,
                executable_price=None,
                executable_depth_usd=None,
                event_age_seconds=None,
                connection_id=None,
                final_update_id=None,
                complete=False,
                rejection_reasons=tuple(reasons),
            )
        if market == "spot":
            bid = depth.spot_best_bid
            ask = depth.spot_best_ask
            timing = depth.spot_timing
            book_errors = depth.spot_book_errors
            executable_depth = depth.spot_ask_usd if side == "buy" else depth.spot_bid_usd
        elif market == "perp":
            bid = depth.perp_best_bid
            ask = depth.perp_best_ask
            timing = depth.perp_timing
            book_errors = depth.perp_book_errors
            executable_depth = depth.perp_ask_usd if side == "buy" else depth.perp_bid_usd
        else:
            raise ValueError("market must be spot or perp")
        if side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        reasons.extend(book_errors)
        if timing is None:
            reasons.append("missing_timing_envelope")
            event_age = None
        else:
            reasons.extend(timing.errors)
            age = self._market_timing_age_seconds(timing)
            event_age = age if math.isfinite(age) else None
            if event_age is None:
                reasons.append("event_age_unknown")
        if bid <= 0.0 or ask <= 0.0 or ask < bid:
            reasons.append("invalid_bbo")
        if executable_depth <= 0.0:
            reasons.append("missing_executable_depth")
        reasons = list(dict.fromkeys(reasons))
        complete = not reasons
        normalized_bid = bid if bid > 0.0 else None
        normalized_ask = ask if ask > 0.0 else None
        mid = (
            (bid + ask) / 2.0
            if bid > 0.0 and ask > 0.0 and ask >= bid
            else None
        )
        executable_price = normalized_ask if side == "buy" else normalized_bid
        return ExecutionBookSnapshot(
            symbol=normalized_symbol,
            market=market,
            side=side,
            captured_at=captured_at,
            bid=normalized_bid,
            ask=normalized_ask,
            mid=mid,
            executable_price=executable_price,
            executable_depth_usd=(
                executable_depth if executable_depth > 0.0 else None
            ),
            event_age_seconds=event_age,
            connection_id=(timing.connection_id if timing is not None else None),
            final_update_id=(timing.final_update_id if timing is not None else None),
            complete=complete,
            rejection_reasons=tuple(reasons),
        )

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

    def entry_leg_spreads_bps(self, symbol: str) -> tuple[float, float]:
        """Return executable spot and perpetual spreads without aggregation."""

        return self.spot_spread_bps(symbol), self.perp_spread_bps(symbol)

    def entry_spread_bps(self, symbol: str) -> float:
        """Return the combined pair spread used by scanner gates and logs.

        This is an additive two-leg metric, not a representative per-leg
        spread. Executable cost code must use :meth:`entry_leg_spreads_bps`.
        """

        spot_spread_bps, perp_spread_bps = self.entry_leg_spreads_bps(symbol)
        return spot_spread_bps + perp_spread_bps

    def entry_data_age_seconds(self, symbol: str) -> float:
        depth = self._depths.get(symbol)
        if depth is None:
            return float("inf")
        if (
            depth.spot_timing is not None
            and depth.perp_timing is not None
            and depth.spot_timing.legacy
            and depth.perp_timing.legacy
        ):
            if depth.spot_updated <= 0.0 or depth.perp_updated <= 0.0:
                return float("inf")
            now = self._wall_clock()
            return max(now - depth.spot_updated, now - depth.perp_updated)
        spot_age = self._market_timing_age_seconds(depth.spot_timing)
        perp_age = self._market_timing_age_seconds(depth.perp_timing)
        return max(spot_age, perp_age)

    def market_event_age_seconds(
        self,
        *,
        connection_id: str | None,
        exchange_event_time_ms: int | float | None,
        receive_time_ms: int | float | None,
        process_time_ms: int | float | None,
        persist_time_ms: int | float | None,
        allow_missing_exchange_event_time: bool = False,
    ) -> float:
        """Validate a non-book market event and return exchange-event age."""

        if exchange_event_time_ms is None and self._allow_legacy_timing:
            return 0.0
        normalized_connection_id = str(connection_id or "").strip()
        exchange_ms = self._timestamp_ms(exchange_event_time_ms)
        receive_ms = self._timestamp_ms(receive_time_ms)
        process_ms = self._timestamp_ms(process_time_ms)
        persist_ms = self._timestamp_ms(persist_time_ms)
        if (
            not normalized_connection_id
            or receive_ms is None
            or process_ms is None
            or (exchange_ms is None and not allow_missing_exchange_event_time)
        ):
            return float("inf")
        if exchange_ms is not None and not exchange_ms <= receive_ms <= process_ms:
            return float("inf")
        if exchange_ms is None and receive_ms > process_ms:
            return float("inf")
        if persist_ms is not None and process_ms > persist_ms:
            return float("inf")
        freshness_ms = exchange_ms if exchange_ms is not None else receive_ms
        age_seconds = (self._wall_clock() * 1_000.0 - freshness_ms) / 1_000.0
        return age_seconds if math.isfinite(age_seconds) and age_seconds >= 0.0 else float("inf")

    def _market_timing_age_seconds(
        self,
        timing: _MarketDataTiming | None,
    ) -> float:
        if timing is None or timing.errors:
            return float("inf")
        if timing.legacy:
            age = self._wall_clock() - timing.freshness_time_ms / 1_000.0
        else:
            age = (
                self._wall_clock() * 1_000.0 - timing.freshness_time_ms
            ) / 1_000.0
        return age if math.isfinite(age) and age >= 0.0 else float("inf")

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
            timing = depth.spot_timing
            reasons.extend(depth.spot_book_errors)
        elif market == "perp":
            bids, asks = depth.perp_bids, depth.perp_asks
            updated_at = depth.perp_book_updated
            timing = depth.perp_timing
            reasons.extend(depth.perp_book_errors)
        else:
            reasons.append("unknown_market")
            return self._empty_leg(symbol, market, side, requested, reasons)

        if timing is not None and timing.legacy:
            checked_at = self._clock() if now is None else float(now)
            age = math.inf if updated_at <= 0.0 else max(0.0, checked_at - updated_at)
            if updated_at <= 0.0:
                reasons.append("missing_book_timestamp")
            elif not math.isfinite(checked_at) or checked_at < updated_at:
                reasons.append("book_clock_invalid")
            elif age > max_age_seconds:
                reasons.append("stale_book")
        else:
            if timing is None:
                reasons.append("missing_timing_envelope")
            else:
                reasons.extend(timing.errors)
            age = self._market_timing_age_seconds(timing)
            if math.isfinite(age) and age > max_age_seconds:
                reasons.append("stale_book")
            elif not math.isfinite(age) and timing is not None and not timing.errors:
                reasons.append("exchange_event_clock_invalid")

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
        books_present = (
            depth.spot_best_ask > 0.0
            and depth.spot_best_bid > 0.0
            and depth.perp_best_ask > 0.0
            and depth.perp_best_bid > 0.0
            and depth.spot_ask_usd > 0.0
            and depth.perp_bid_usd > 0.0
        )
        if not books_present:
            return False
        return all(
            timing is not None and not timing.errors
            for timing in (depth.spot_timing, depth.perp_timing)
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
