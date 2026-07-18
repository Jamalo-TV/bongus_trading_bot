"""Fail-closed Binance symbol metadata and exact Decimal order filters.

The live runtimes historically copied a subset of exchange filters into float
dictionaries.  This module is deliberately side-effect free: it parses a full
exchange-info snapshot atomically and can validate or normalize proposed
orders without changing the active router.  Decimal arithmetic prevents tick
and lot errors caused by binary floating-point rounding.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from typing import Any, Callable, Literal, Mapping

Market = Literal["spot", "perp"]
OrderSide = Literal["BUY", "SELL"]
OrderType = Literal["LIMIT", "MARKET"]

DEFAULT_METADATA_TTL_SECONDS = 3_600.0
TRADING_STATUS = "TRADING"
_ZERO = Decimal("0")


class IncompleteExchangeMetadata(ValueError):
    """Raised when exchange-info cannot prove every required constraint."""


def _decimal(value: Any, field_name: str, *, allow_zero: bool = False) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise IncompleteExchangeMetadata(f"{field_name}:invalid_decimal") from exc
    if not parsed.is_finite():
        raise IncompleteExchangeMetadata(f"{field_name}:non_finite")
    if parsed < _ZERO or (parsed == _ZERO and not allow_zero):
        raise IncompleteExchangeMetadata(f"{field_name}:not_positive")
    return parsed


def _optional_decimal(value: Any, field_name: str) -> Decimal | None:
    if value in (None, ""):
        return None
    parsed = _decimal(value, field_name, allow_zero=True)
    return None if parsed == _ZERO else parsed


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return bool(value)


@dataclass(frozen=True, slots=True)
class DecimalGrid:
    minimum: Decimal
    maximum: Decimal
    step: Decimal

    def __post_init__(self) -> None:
        if self.minimum < _ZERO:
            raise IncompleteExchangeMetadata("grid:negative_minimum")
        if self.maximum <= _ZERO or self.maximum < self.minimum:
            raise IncompleteExchangeMetadata("grid:invalid_maximum")
        if self.step <= _ZERO:
            raise IncompleteExchangeMetadata("grid:invalid_step")

    def aligned(self, value: Decimal) -> bool:
        return value >= _ZERO and value % self.step == _ZERO

    def floor(self, value: Decimal) -> Decimal:
        return (value / self.step).to_integral_value(rounding=ROUND_FLOOR) * self.step

    def ceil(self, value: Decimal) -> Decimal:
        return (value / self.step).to_integral_value(rounding=ROUND_CEILING) * self.step


@dataclass(frozen=True, slots=True)
class NotionalFilter:
    minimum: Decimal
    maximum: Decimal | None
    apply_min_to_market: bool
    apply_max_to_market: bool

    def __post_init__(self) -> None:
        if self.minimum <= _ZERO:
            raise IncompleteExchangeMetadata("notional:invalid_minimum")
        if self.maximum is not None and self.maximum < self.minimum:
            raise IncompleteExchangeMetadata("notional:maximum_below_minimum")


@dataclass(frozen=True, slots=True)
class SymbolOrderMetadata:
    symbol: str
    market: Market
    status: str
    price: DecimalGrid
    lot: DecimalGrid
    market_lot: DecimalGrid | None
    notional: NotionalFilter
    received_at: float
    source_hash: str

    @property
    def tradable(self) -> bool:
        return self.status == TRADING_STATUS


@dataclass(frozen=True, slots=True)
class MetadataUpdateResult:
    market: Market
    accepted_symbols: tuple[str, ...]
    rejected_symbols: Mapping[str, tuple[str, ...]]
    changed_symbols: tuple[str, ...]
    removed_symbols: tuple[str, ...]
    source_hash: str


@dataclass(frozen=True, slots=True)
class OrderFilterResult:
    accepted: bool
    symbol: str
    market: str
    side: str
    order_type: str
    quantity: Decimal | None
    price: Decimal | None
    notional: Decimal | None
    metadata_age_seconds: float
    reasons: tuple[str, ...]
    normalized: bool = False


def _canonical_hash(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def parse_binance_symbol_metadata(
    payload: Mapping[str, Any],
    market: Market,
    *,
    received_at: float,
    source_hash: str | None = None,
) -> SymbolOrderMetadata:
    """Parse required PRICE/LOT/NOTIONAL/status fields or raise.

    ``MIN_NOTIONAL`` and the newer ``NOTIONAL`` representation are both
    supported.  ``MARKET_LOT_SIZE`` is additive; if its step is disabled (zero)
    the LOT_SIZE step remains authoritative while its non-zero min/max bounds
    are retained.
    """

    symbol = str(payload.get("symbol", "")).strip().upper()
    status = str(payload.get("status", "")).strip().upper()
    if not math.isfinite(received_at):
        raise IncompleteExchangeMetadata("received_at:non_finite")
    if not symbol:
        raise IncompleteExchangeMetadata("symbol:missing")
    if not status:
        raise IncompleteExchangeMetadata("status:missing")
    if market not in ("spot", "perp"):
        raise IncompleteExchangeMetadata("market:unsupported")

    raw_filters = payload.get("filters")
    if not isinstance(raw_filters, list):
        raise IncompleteExchangeMetadata("filters:missing")
    filters: dict[str, Mapping[str, Any]] = {}
    for item in raw_filters:
        if not isinstance(item, Mapping):
            continue
        filter_type = str(item.get("filterType", "")).upper()
        if filter_type:
            filters[filter_type] = item

    price_filter = filters.get("PRICE_FILTER")
    lot_filter = filters.get("LOT_SIZE")
    if price_filter is None:
        raise IncompleteExchangeMetadata("PRICE_FILTER:missing")
    if lot_filter is None:
        raise IncompleteExchangeMetadata("LOT_SIZE:missing")

    price_grid = DecimalGrid(
        minimum=_decimal(price_filter.get("minPrice"), "PRICE_FILTER.minPrice", allow_zero=True),
        maximum=_decimal(price_filter.get("maxPrice"), "PRICE_FILTER.maxPrice"),
        step=_decimal(price_filter.get("tickSize"), "PRICE_FILTER.tickSize"),
    )
    lot_grid = DecimalGrid(
        minimum=_decimal(lot_filter.get("minQty"), "LOT_SIZE.minQty", allow_zero=True),
        maximum=_decimal(lot_filter.get("maxQty"), "LOT_SIZE.maxQty"),
        step=_decimal(lot_filter.get("stepSize"), "LOT_SIZE.stepSize"),
    )

    market_lot_grid: DecimalGrid | None = None
    market_lot_filter = filters.get("MARKET_LOT_SIZE")
    if market_lot_filter is not None:
        market_step = _decimal(
            market_lot_filter.get("stepSize"),
            "MARKET_LOT_SIZE.stepSize",
            allow_zero=True,
        )
        market_lot_grid = DecimalGrid(
            minimum=_decimal(
                market_lot_filter.get("minQty"),
                "MARKET_LOT_SIZE.minQty",
                allow_zero=True,
            ),
            maximum=_decimal(market_lot_filter.get("maxQty"), "MARKET_LOT_SIZE.maxQty"),
            step=lot_grid.step if market_step == _ZERO else market_step,
        )

    notional_filter = filters.get("NOTIONAL")
    if notional_filter is not None:
        notional = NotionalFilter(
            minimum=_decimal(notional_filter.get("minNotional"), "NOTIONAL.minNotional"),
            maximum=_optional_decimal(notional_filter.get("maxNotional"), "NOTIONAL.maxNotional"),
            apply_min_to_market=_bool(notional_filter.get("applyMinToMarket"), True),
            apply_max_to_market=_bool(notional_filter.get("applyMaxToMarket"), True),
        )
    else:
        min_notional_filter = filters.get("MIN_NOTIONAL")
        if min_notional_filter is None:
            raise IncompleteExchangeMetadata("MIN_NOTIONAL/NOTIONAL:missing")
        raw_minimum = min_notional_filter.get("minNotional", min_notional_filter.get("notional"))
        notional = NotionalFilter(
            minimum=_decimal(raw_minimum, "MIN_NOTIONAL.minNotional"),
            maximum=None,
            apply_min_to_market=_bool(min_notional_filter.get("applyToMarket"), True),
            apply_max_to_market=False,
        )

    return SymbolOrderMetadata(
        symbol=symbol,
        market=market,
        status=status,
        price=price_grid,
        lot=lot_grid,
        market_lot=market_lot_grid,
        notional=notional,
        received_at=float(received_at),
        source_hash=source_hash or _canonical_hash(payload),
    )


class ExchangeFilterRegistry:
    """Atomic, TTL-bound registry of complete per-market symbol filters."""

    def __init__(
        self,
        *,
        metadata_ttl_seconds: float = DEFAULT_METADATA_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not math.isfinite(metadata_ttl_seconds) or metadata_ttl_seconds <= 0.0:
            raise ValueError("metadata_ttl_seconds must be positive and finite")
        self._metadata_ttl_seconds = float(metadata_ttl_seconds)
        self._clock = clock
        self._metadata: dict[tuple[Market, str], SymbolOrderMetadata] = {}
        self._invalid: dict[tuple[Market, str], tuple[str, ...]] = {}

    def replace_market(
        self,
        market: Market,
        exchange_info: Mapping[str, Any],
        *,
        received_at: float | None = None,
    ) -> MetadataUpdateResult:
        """Replace one market from a single complete exchange-info snapshot.

        Parsing happens into temporary maps first.  Readers therefore never see
        a half-applied snapshot, and a malformed symbol cannot retain stale
        formerly-valid filters.
        """

        if market not in ("spot", "perp"):
            raise ValueError(f"unsupported market: {market}")
        timestamp = self._clock() if received_at is None else float(received_at)
        if not math.isfinite(timestamp):
            raise ValueError("received_at must be finite")
        source_hash = _canonical_hash(exchange_info)
        raw_symbols = exchange_info.get("symbols")
        if not isinstance(raw_symbols, list):
            # Do not discard a known-good snapshot because the transport body
            # itself is incomplete.  The caller receives a hard failure and can
            # keep entries blocked once their TTL expires.
            raise IncompleteExchangeMetadata("exchange_info.symbols:missing")

        next_metadata: dict[tuple[Market, str], SymbolOrderMetadata] = {}
        next_invalid: dict[tuple[Market, str], tuple[str, ...]] = {}
        seen_symbols: set[str] = set()
        for raw_symbol in raw_symbols:
            if not isinstance(raw_symbol, Mapping):
                continue
            symbol = str(raw_symbol.get("symbol", "")).strip().upper() or "<UNKNOWN>"
            key = (market, symbol)
            if symbol in seen_symbols:
                next_metadata.pop(key, None)
                next_invalid[key] = ("symbol:duplicate",)
                continue
            seen_symbols.add(symbol)
            try:
                parsed = parse_binance_symbol_metadata(
                    raw_symbol,
                    market,
                    received_at=timestamp,
                    source_hash=_canonical_hash(raw_symbol),
                )
            except IncompleteExchangeMetadata as exc:
                next_invalid[key] = (str(exc),)
            else:
                next_metadata[(market, parsed.symbol)] = parsed

        # A valid empty exchange-info response is not a safe replacement.
        if not next_metadata and not next_invalid:
            raise IncompleteExchangeMetadata("exchange_info.symbols:empty")

        previous_fingerprints: dict[tuple[Market, str], tuple[str, object]] = {
            key: ("valid", value.source_hash)
            for key, value in self._metadata.items()
            if key[0] == market
        }
        previous_fingerprints.update(
            {
                key: ("invalid", reasons)
                for key, reasons in self._invalid.items()
                if key[0] == market
            }
        )
        next_fingerprints: dict[tuple[Market, str], tuple[str, object]] = {
            key: ("valid", value.source_hash) for key, value in next_metadata.items()
        }
        next_fingerprints.update(
            {key: ("invalid", reasons) for key, reasons in next_invalid.items()}
        )
        changed_symbols = tuple(
            sorted(
                key[1]
                for key, fingerprint in next_fingerprints.items()
                if previous_fingerprints.get(key) != fingerprint
            )
        )
        removed_symbols = tuple(sorted(key[1] for key in previous_fingerprints.keys() - next_fingerprints.keys()))

        self._metadata = {key: value for key, value in self._metadata.items() if key[0] != market}
        self._invalid = {key: value for key, value in self._invalid.items() if key[0] != market}
        self._metadata.update(next_metadata)
        self._invalid.update(next_invalid)
        return MetadataUpdateResult(
            market=market,
            accepted_symbols=tuple(sorted(key[1] for key in next_metadata)),
            rejected_symbols={key[1]: reasons for key, reasons in sorted(next_invalid.items())},
            changed_symbols=changed_symbols,
            removed_symbols=removed_symbols,
            source_hash=source_hash,
        )

    def get(self, symbol: str, market: Market) -> SymbolOrderMetadata | None:
        return self._metadata.get((market, symbol.upper()))

    def validate_order(
        self,
        *,
        symbol: str,
        market: Market,
        side: OrderSide,
        order_type: OrderType,
        quantity: Decimal | str | float | int,
        price: Decimal | str | float | int | None = None,
        reference_price: Decimal | str | float | int | None = None,
        now: float | None = None,
        normalize: bool = False,
    ) -> OrderFilterResult:
        reasons: list[str] = []
        normalized_symbol = symbol.upper()
        normalized_side = str(side).upper()
        normalized_type = str(order_type).upper()
        checked_at = self._clock() if now is None else float(now)
        metadata = self._metadata.get((market, normalized_symbol))
        if metadata is None:
            invalid_reasons = self._invalid.get((market, normalized_symbol))
            reasons.extend(invalid_reasons or ("metadata_missing",))
            return OrderFilterResult(
                False,
                normalized_symbol,
                market,
                normalized_side,
                normalized_type,
                None,
                None,
                None,
                math.inf,
                tuple(reasons),
                normalize,
            )

        age = max(0.0, checked_at - metadata.received_at)
        if not math.isfinite(checked_at) or checked_at < metadata.received_at:
            reasons.append("metadata_clock_invalid")
        elif age > self._metadata_ttl_seconds:
            reasons.append("metadata_stale")
        if not metadata.tradable:
            reasons.append(f"symbol_status:{metadata.status}")
        if normalized_side not in ("BUY", "SELL"):
            reasons.append("side_unsupported")
        if normalized_type not in ("LIMIT", "MARKET"):
            reasons.append("order_type_unsupported")

        parsed_quantity = self._parse_order_decimal(quantity, "quantity", reasons)
        parsed_price = self._parse_order_decimal(price, "price", reasons) if price is not None else None
        parsed_reference = (
            self._parse_order_decimal(reference_price, "reference_price", reasons)
            if reference_price is not None
            else None
        )

        quantity_grid = metadata.market_lot if normalized_type == "MARKET" and metadata.market_lot else metadata.lot
        if parsed_quantity is not None:
            if normalize:
                parsed_quantity = quantity_grid.floor(parsed_quantity)
            if parsed_quantity < quantity_grid.minimum:
                reasons.append("quantity_below_minimum")
            if parsed_quantity > quantity_grid.maximum:
                reasons.append("quantity_above_maximum")
            if not quantity_grid.aligned(parsed_quantity):
                reasons.append("quantity_off_step")

        if normalized_type == "LIMIT":
            if parsed_price is None:
                reasons.append("limit_price_missing")
            else:
                if normalize:
                    parsed_price = (
                        metadata.price.floor(parsed_price)
                        if normalized_side == "BUY"
                        else metadata.price.ceil(parsed_price)
                    )
                if parsed_price < metadata.price.minimum:
                    reasons.append("price_below_minimum")
                if parsed_price > metadata.price.maximum:
                    reasons.append("price_above_maximum")
                if not metadata.price.aligned(parsed_price):
                    reasons.append("price_off_tick")

        notional_price = parsed_price if normalized_type == "LIMIT" else parsed_reference
        notional: Decimal | None = None
        minimum_applies = normalized_type != "MARKET" or metadata.notional.apply_min_to_market
        maximum_applies = normalized_type != "MARKET" or metadata.notional.apply_max_to_market
        if parsed_quantity is not None and notional_price is not None:
            notional = parsed_quantity * notional_price
            if minimum_applies and notional < metadata.notional.minimum:
                reasons.append("notional_below_minimum")
            if (
                maximum_applies
                and metadata.notional.maximum is not None
                and notional > metadata.notional.maximum
            ):
                reasons.append("notional_above_maximum")
        elif minimum_applies or (maximum_applies and metadata.notional.maximum is not None):
            reasons.append("notional_price_missing")

        return OrderFilterResult(
            accepted=not reasons,
            symbol=normalized_symbol,
            market=market,
            side=normalized_side,
            order_type=normalized_type,
            quantity=parsed_quantity,
            price=parsed_price,
            notional=notional,
            metadata_age_seconds=age,
            reasons=tuple(dict.fromkeys(reasons)),
            normalized=normalize,
        )

    @staticmethod
    def _parse_order_decimal(value: Any, field_name: str, reasons: list[str]) -> Decimal | None:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            reasons.append(f"{field_name}_invalid")
            return None
        if not parsed.is_finite() or parsed <= _ZERO:
            reasons.append(f"{field_name}_invalid")
            return None
        return parsed
