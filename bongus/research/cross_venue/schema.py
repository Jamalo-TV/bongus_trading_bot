"""Exact, venue-aware contracts for the isolated cross-venue research service.

Economic values are represented as :class:`~decimal.Decimal` in memory and as
plain decimal strings on the wire.  Binary floating-point values are rejected
at the boundary so a JSON decoder cannot silently introduce rounding error.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, fields, is_dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Final, Literal, Mapping

SCHEMA_VERSION: Final[str] = "bongus-cross-venue-v1"
PUBLIC_ENVIRONMENT: Final[str] = "mainnet-public"

ExactDecimalInput = Decimal | str | int


def _validate_public_envelope(schema_version: str, environment: str) -> None:
    if schema_version != SCHEMA_VERSION or environment != PUBLIC_ENVIRONMENT:
        raise ValueError("cross-venue contracts require the fixed public v1 envelope")


def exact_decimal(value: ExactDecimalInput, field_name: str) -> Decimal:
    """Return one finite Decimal while refusing binary floats and booleans."""

    if isinstance(value, bool) or not isinstance(value, (Decimal, str, int)):
        raise TypeError(f"{field_name} must be a Decimal, decimal string, or integer")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{field_name} must be a finite decimal")
    return result


def nonnegative_decimal(value: ExactDecimalInput, field_name: str) -> Decimal:
    result = exact_decimal(value, field_name)
    if result < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return result


def positive_decimal(value: ExactDecimalInput, field_name: str) -> Decimal:
    result = exact_decimal(value, field_name)
    if result <= 0:
        raise ValueError(f"{field_name} must be positive")
    return result


def epoch_nanoseconds(value: int | str, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TypeError(f"{field_name} must be exact epoch nanoseconds")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be exact epoch nanoseconds") from exc
    if str(value).strip() != str(result) and not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer string")
    if result < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return result


def decimal_text(value: Decimal) -> str:
    """Canonical non-exponent decimal text used by persisted contracts."""

    if not value.is_finite():
        raise ValueError("wire decimal must be finite")
    if value == 0:
        return "0"
    return format(value, "f")


def exact_wire(value: object) -> object:
    """Convert a schema value to JSON-compatible values without any floats."""

    if isinstance(value, Decimal):
        return decimal_text(value)
    if isinstance(value, StrEnum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: exact_wire(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): exact_wire(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [exact_wire(item) for item in value]
    if isinstance(value, float):
        raise TypeError("binary floats are forbidden in exact wire contracts")
    return value


def deterministic_event_id(*parts: str) -> str:
    if not parts or any(not isinstance(part, str) or not part.strip() for part in parts):
        raise ValueError("event identity parts must be non-empty strings")
    digest = hashlib.sha256()
    for part in parts:
        encoded = str(part).encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return f"cv1-{digest.hexdigest()}"


class Venue(StrEnum):
    BINANCE = "binance"
    HYPERLIQUID = "hyperliquid"


class CanonicalAsset(StrEnum):
    BTC = "BTC"
    ETH = "ETH"
    SOL = "SOL"
    XRP = "XRP"
    DOGE = "DOGE"


class FundingPriceKind(StrEnum):
    MARK = "mark"
    ORACLE = "oracle"


@dataclass(frozen=True, slots=True)
class InstrumentMapping:
    canonical_asset: CanonicalAsset
    binance_symbol: str
    binance_contract_id: str
    hyperliquid_coin: str
    hyperliquid_contract_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.canonical_asset, CanonicalAsset):
            raise TypeError("canonical_asset must use the fixed CanonicalAsset enum")
        values = (
            self.binance_symbol,
            self.binance_contract_id,
            self.hyperliquid_coin,
            self.hyperliquid_contract_id,
        )
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError("instrument mapping identifiers must be non-empty")


@dataclass(frozen=True, slots=True)
class ContractMetadata:
    venue: Venue
    canonical_asset: CanonicalAsset
    venue_symbol: str
    contract_id: str
    base_asset: str
    quote_asset: str
    settlement_asset: str
    contract_multiplier: Decimal
    quantity_step: Decimal
    price_tick: Decimal | None
    funding_interval_hours: Decimal
    status: str
    schema_version: str = SCHEMA_VERSION
    environment: str = PUBLIC_ENVIRONMENT
    product_family: Literal["core-linear-perpetual"] = "core-linear-perpetual"

    def __post_init__(self) -> None:
        _validate_public_envelope(self.schema_version, self.environment)
        if not isinstance(self.venue, Venue) or not isinstance(self.canonical_asset, CanonicalAsset):
            raise TypeError("contract venue and canonical asset must use fixed enums")
        if self.product_family != "core-linear-perpetual":
            raise ValueError("only core linear perpetual contracts are supported")
        for name in (
            "venue_symbol",
            "contract_id",
            "base_asset",
            "quote_asset",
            "settlement_asset",
            "status",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty")
        object.__setattr__(
            self,
            "contract_multiplier",
            positive_decimal(self.contract_multiplier, "contract_multiplier"),
        )
        object.__setattr__(
            self,
            "quantity_step",
            positive_decimal(self.quantity_step, "quantity_step"),
        )
        if self.price_tick is not None:
            object.__setattr__(self, "price_tick", positive_decimal(self.price_tick, "price_tick"))
        object.__setattr__(
            self,
            "funding_interval_hours",
            positive_decimal(self.funding_interval_hours, "funding_interval_hours"),
        )


@dataclass(frozen=True, slots=True)
class FundingQuote:
    event_id: str
    venue: Venue
    canonical_asset: CanonicalAsset
    contract_id: str
    rate: Decimal
    interval_hours: Decimal
    source_time_ns: int
    available_time_ns: int
    next_settlement_time_ns: int | None = None
    oracle_price: Decimal | None = None
    mark_price: Decimal | None = None
    schema_version: str = SCHEMA_VERSION
    environment: str = PUBLIC_ENVIRONMENT

    def __post_init__(self) -> None:
        _validate_public_envelope(self.schema_version, self.environment)
        if not isinstance(self.venue, Venue) or not isinstance(self.canonical_asset, CanonicalAsset):
            raise TypeError("funding quote venue and canonical asset must use fixed enums")
        if (
            not isinstance(self.event_id, str)
            or not self.event_id.strip()
            or not isinstance(self.contract_id, str)
            or not self.contract_id.strip()
        ):
            raise ValueError("funding quote identity is required")
        object.__setattr__(self, "rate", exact_decimal(self.rate, "rate"))
        object.__setattr__(self, "interval_hours", positive_decimal(self.interval_hours, "interval_hours"))
        source = epoch_nanoseconds(self.source_time_ns, "source_time_ns")
        available = epoch_nanoseconds(self.available_time_ns, "available_time_ns")
        if available < source:
            raise ValueError("funding quote cannot be available before its source time")
        object.__setattr__(self, "source_time_ns", source)
        object.__setattr__(self, "available_time_ns", available)
        if self.next_settlement_time_ns is not None:
            next_settlement = epoch_nanoseconds(self.next_settlement_time_ns, "next_settlement_time_ns")
            if next_settlement < source:
                raise ValueError("next settlement cannot precede the quote source time")
            object.__setattr__(
                self,
                "next_settlement_time_ns",
                next_settlement,
            )
        for name in ("oracle_price", "mark_price"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, positive_decimal(value, name))


@dataclass(frozen=True, slots=True)
class FundingSettlement:
    event_id: str
    venue: Venue
    canonical_asset: CanonicalAsset
    contract_id: str
    settlement_time_ns: int
    available_time_ns: int
    rate: Decimal
    settlement_price: Decimal | None
    price_kind: FundingPriceKind
    contract_multiplier: Decimal = Decimal("1")
    schema_version: str = SCHEMA_VERSION
    environment: str = PUBLIC_ENVIRONMENT

    def __post_init__(self) -> None:
        _validate_public_envelope(self.schema_version, self.environment)
        if (
            not isinstance(self.venue, Venue)
            or not isinstance(self.canonical_asset, CanonicalAsset)
            or not isinstance(self.price_kind, FundingPriceKind)
        ):
            raise TypeError("funding settlement enum fields must use fixed enums")
        if (
            not isinstance(self.event_id, str)
            or not self.event_id.strip()
            or not isinstance(self.contract_id, str)
            or not self.contract_id.strip()
        ):
            raise ValueError("funding settlement identity is required")
        settlement = epoch_nanoseconds(self.settlement_time_ns, "settlement_time_ns")
        available = epoch_nanoseconds(self.available_time_ns, "available_time_ns")
        if available < settlement:
            raise ValueError("funding settlement cannot be available before settlement")
        object.__setattr__(self, "settlement_time_ns", settlement)
        object.__setattr__(self, "available_time_ns", available)
        object.__setattr__(self, "rate", exact_decimal(self.rate, "rate"))
        if self.settlement_price is not None:
            object.__setattr__(
                self,
                "settlement_price",
                positive_decimal(self.settlement_price, "settlement_price"),
            )
        object.__setattr__(
            self,
            "contract_multiplier",
            positive_decimal(self.contract_multiplier, "contract_multiplier"),
        )


@dataclass(frozen=True, slots=True)
class BboSnapshot:
    event_id: str
    venue: Venue
    canonical_asset: CanonicalAsset
    contract_id: str
    source_time_ns: int
    available_time_ns: int
    bid_price: Decimal
    bid_quantity: Decimal
    ask_price: Decimal
    ask_quantity: Decimal
    schema_version: str = SCHEMA_VERSION
    environment: str = PUBLIC_ENVIRONMENT

    def __post_init__(self) -> None:
        _validate_public_envelope(self.schema_version, self.environment)
        if not isinstance(self.venue, Venue) or not isinstance(self.canonical_asset, CanonicalAsset):
            raise TypeError("BBO venue and canonical asset must use fixed enums")
        if (
            not isinstance(self.event_id, str)
            or not self.event_id.strip()
            or not isinstance(self.contract_id, str)
            or not self.contract_id.strip()
        ):
            raise ValueError("BBO identity is required")
        source = epoch_nanoseconds(self.source_time_ns, "source_time_ns")
        available = epoch_nanoseconds(self.available_time_ns, "available_time_ns")
        if available < source:
            raise ValueError("BBO cannot be available before source time")
        object.__setattr__(self, "source_time_ns", source)
        object.__setattr__(self, "available_time_ns", available)
        for name in ("bid_price", "bid_quantity", "ask_price", "ask_quantity"):
            object.__setattr__(self, name, positive_decimal(getattr(self, name), name))
        if self.bid_price >= self.ask_price:
            raise ValueError("BBO must have bid below ask")


@dataclass(frozen=True, slots=True)
class CommissionSchedule:
    binance_entry_rate: Decimal
    binance_exit_rate: Decimal
    hyperliquid_entry_rate: Decimal
    hyperliquid_exit_rate: Decimal

    def __post_init__(self) -> None:
        for name in (
            "binance_entry_rate",
            "binance_exit_rate",
            "hyperliquid_entry_rate",
            "hyperliquid_exit_rate",
        ):
            rate = nonnegative_decimal(getattr(self, name), name)
            if rate >= 1:
                raise ValueError(f"{name} must be below one")
            object.__setattr__(self, name, rate)


@dataclass(frozen=True, slots=True)
class ReservedCapital:
    binance_collateral_usd: Decimal
    hyperliquid_collateral_usd: Decimal
    liquidation_buffers_usd: Decimal
    idle_transfer_buffer_usd: Decimal

    def __post_init__(self) -> None:
        for name in (
            "binance_collateral_usd",
            "hyperliquid_collateral_usd",
            "liquidation_buffers_usd",
            "idle_transfer_buffer_usd",
        ):
            object.__setattr__(self, name, nonnegative_decimal(getattr(self, name), name))
        if self.total_usd <= 0:
            raise ValueError("total reserved capital must be positive")

    @property
    def total_usd(self) -> Decimal:
        return (
            self.binance_collateral_usd
            + self.hyperliquid_collateral_usd
            + self.liquidation_buffers_usd
            + self.idle_transfer_buffer_usd
        )


@dataclass(frozen=True, slots=True)
class MatchedBaseEpisode:
    """One normalized static episode with equal effective base on both legs."""

    canonical_asset: CanonicalAsset
    base_quantity: Decimal
    binance_entry_price: Decimal
    binance_exit_price: Decimal
    hyperliquid_entry_price: Decimal
    hyperliquid_exit_price: Decimal
    commissions: CommissionSchedule
    reserved_capital: ReservedCapital
    holding_period_days: Decimal
    binance_funding_events: tuple[FundingSettlement, ...] = ()
    hyperliquid_funding_events: tuple[FundingSettlement, ...] = ()
    stablecoin_conversion_cost_usd: Decimal = Decimal("0")
    collateral_opportunity_cost_usd: Decimal = Decimal("0")
    repair_failure_cost_usd: Decimal = Decimal("0")
    binance_contract_multiplier: Decimal = Decimal("1")
    hyperliquid_contract_multiplier: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        if not isinstance(self.canonical_asset, CanonicalAsset):
            raise TypeError("episode canonical_asset must use the fixed enum")
        if not isinstance(self.commissions, CommissionSchedule) or not isinstance(
            self.reserved_capital, ReservedCapital
        ):
            raise TypeError("episode requires exact commission and reserved-capital contracts")
        if not isinstance(self.binance_funding_events, tuple) or not isinstance(self.hyperliquid_funding_events, tuple):
            raise TypeError("episode funding collections must be immutable tuples")
        object.__setattr__(self, "base_quantity", positive_decimal(self.base_quantity, "base_quantity"))
        for name in (
            "binance_entry_price",
            "binance_exit_price",
            "hyperliquid_entry_price",
            "hyperliquid_exit_price",
            "holding_period_days",
            "binance_contract_multiplier",
            "hyperliquid_contract_multiplier",
        ):
            object.__setattr__(self, name, positive_decimal(getattr(self, name), name))
        for name in (
            "stablecoin_conversion_cost_usd",
            "collateral_opportunity_cost_usd",
            "repair_failure_cost_usd",
        ):
            object.__setattr__(self, name, nonnegative_decimal(getattr(self, name), name))
        if self.binance_contract_multiplier != self.hyperliquid_contract_multiplier:
            raise ValueError("episode contract quantities do not produce matched base exposure")
        seen_event_ids: set[str] = set()
        for event in self.binance_funding_events:
            if not isinstance(event, FundingSettlement):
                raise TypeError("episode funding collections require settlement contracts")
            if event.venue is not Venue.BINANCE or event.canonical_asset is not self.canonical_asset:
                raise ValueError("Binance funding event does not match the episode")
            if event.contract_multiplier != self.binance_contract_multiplier:
                raise ValueError("Binance funding multiplier does not match the episode")
            if event.event_id in seen_event_ids:
                raise ValueError("funding events cannot be counted more than once")
            seen_event_ids.add(event.event_id)
        for event in self.hyperliquid_funding_events:
            if not isinstance(event, FundingSettlement):
                raise TypeError("episode funding collections require settlement contracts")
            if event.venue is not Venue.HYPERLIQUID or event.canonical_asset is not self.canonical_asset:
                raise ValueError("Hyperliquid funding event does not match the episode")
            if event.contract_multiplier != self.hyperliquid_contract_multiplier:
                raise ValueError("Hyperliquid funding multiplier does not match the episode")
            if event.event_id in seen_event_ids:
                raise ValueError("funding events cannot be counted more than once")
            seen_event_ids.add(event.event_id)


@dataclass(frozen=True, slots=True)
class EpisodePnl:
    binance_price_pnl_usd: Decimal
    hyperliquid_price_pnl_usd: Decimal
    binance_funding_pnl_usd: Decimal
    hyperliquid_funding_pnl_usd: Decimal
    binance_entry_commission_usd: Decimal
    binance_exit_commission_usd: Decimal
    hyperliquid_entry_commission_usd: Decimal
    hyperliquid_exit_commission_usd: Decimal
    total_commissions_usd: Decimal
    stablecoin_conversion_cost_usd: Decimal
    collateral_opportunity_cost_usd: Decimal
    repair_failure_cost_usd: Decimal
    net_pnl_usd: Decimal
    total_reserved_capital_usd: Decimal
    return_on_reserved_capital: Decimal
    simple_annualized_return: Decimal

    def __post_init__(self) -> None:
        for field in fields(self):
            object.__setattr__(
                self,
                field.name,
                exact_decimal(getattr(self, field.name), field.name),
            )
        if self.total_reserved_capital_usd <= 0:
            raise ValueError("total_reserved_capital_usd must be positive")


__all__ = [
    "BboSnapshot",
    "CanonicalAsset",
    "CommissionSchedule",
    "ContractMetadata",
    "EpisodePnl",
    "ExactDecimalInput",
    "FundingPriceKind",
    "FundingQuote",
    "FundingSettlement",
    "InstrumentMapping",
    "MatchedBaseEpisode",
    "PUBLIC_ENVIRONMENT",
    "ReservedCapital",
    "SCHEMA_VERSION",
    "Venue",
    "decimal_text",
    "deterministic_event_id",
    "epoch_nanoseconds",
    "exact_decimal",
    "exact_wire",
    "nonnegative_decimal",
    "positive_decimal",
]
