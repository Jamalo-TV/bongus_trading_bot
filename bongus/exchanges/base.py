"""Common contract/account semantics for read-only venue adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Protocol, Sequence


class Venue(StrEnum):
    BINANCE = "binance"
    BYBIT = "bybit"


class ContractType(StrEnum):
    SPOT = "spot"
    LINEAR_PERPETUAL = "linear_perpetual"
    INVERSE_PERPETUAL = "inverse_perpetual"


def decimal_value(value: Decimal | str | int | float, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


@dataclass(frozen=True, slots=True)
class AccountRef:
    venue: Venue
    environment: str
    account_id: str

    @property
    def key(self) -> str:
        if not self.environment.strip() or not self.account_id.strip():
            raise ValueError("environment and account_id are required")
        return f"{self.venue.value}:{self.environment.lower()}:{self.account_id}"


@dataclass(frozen=True, slots=True)
class ContractSpec:
    venue: Venue
    symbol: str
    contract_type: ContractType
    base_asset: str
    quote_asset: str
    settlement_asset: str
    contract_multiplier: Decimal
    quantity_step: Decimal
    minimum_quantity: Decimal
    minimum_notional: Decimal
    price_tick: Decimal
    funding_interval_hours: int = 8
    status: str = "TRADING"

    def __post_init__(self) -> None:
        if not self.symbol.strip() or not self.base_asset.strip() or not self.quote_asset.strip():
            raise ValueError("contract symbol/base/quote are required")
        if self.contract_multiplier <= 0 or self.quantity_step <= 0 or self.price_tick <= 0:
            raise ValueError("contract multiplier, quantity step and price tick must be positive")
        if self.minimum_quantity < 0 or self.minimum_notional < 0:
            raise ValueError("contract minimums must be non-negative")
        if self.funding_interval_hours <= 0 or self.funding_interval_hours > 24:
            raise ValueError("funding interval must be in (0, 24]")

    def notional_usd(self, quantity: Decimal | str | float, price: Decimal | str | float) -> Decimal:
        quantity_value = abs(decimal_value(quantity, "quantity"))
        price_value = decimal_value(price, "price")
        if price_value <= 0:
            raise ValueError("price must be positive")
        if self.contract_type is ContractType.INVERSE_PERPETUAL:
            # Inverse contract multipliers are quote-value per contract.
            return quantity_value * self.contract_multiplier
        return quantity_value * self.contract_multiplier * price_value

    def base_exposure(self, quantity: Decimal | str | float, price: Decimal | str | float) -> Decimal:
        quantity_value = decimal_value(quantity, "quantity")
        price_value = decimal_value(price, "price")
        if price_value <= 0:
            raise ValueError("price must be positive")
        if self.contract_type is ContractType.INVERSE_PERPETUAL:
            return quantity_value * self.contract_multiplier / price_value
        return quantity_value * self.contract_multiplier


@dataclass(frozen=True, slots=True)
class FundingQuote:
    venue: Venue
    symbol: str
    raw_rate: Decimal
    interval_hours: int
    next_settlement_time: datetime
    observed_at: datetime
    cap: Decimal | None = None
    floor: Decimal | None = None

    @property
    def annualized_rate(self) -> Decimal:
        return self.raw_rate * Decimal(365 * 24) / Decimal(self.interval_hours)


@dataclass(frozen=True, slots=True)
class NormalizedBalance:
    account: AccountRef
    asset: str
    wallet: str
    total: Decimal
    available: Decimal
    borrowed: Decimal = Decimal("0")
    interest: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class NormalizedPosition:
    account: AccountRef
    venue_symbol: str
    contract: ContractSpec
    signed_quantity: Decimal
    mark_price: Decimal
    entry_price: Decimal
    liquidation_price: Decimal | None
    initial_margin: Decimal
    maintenance_margin: Decimal

    @property
    def signed_base_exposure(self) -> Decimal:
        return self.contract.base_exposure(self.signed_quantity, self.mark_price)

    @property
    def notional_usd(self) -> Decimal:
        return self.contract.notional_usd(self.signed_quantity, self.mark_price)


class ReadOnlyVenueAdapter(Protocol):
    """Intentionally omits any order/transfer mutation API."""

    venue: Venue

    def normalize_contracts(self, payload: dict, *, observed_at: datetime) -> Sequence[ContractSpec]:
        ...

    def normalize_funding(
        self,
        payload: object,
        *,
        contracts: dict[str, ContractSpec],
        observed_at: datetime,
    ) -> Sequence[FundingQuote]:
        ...

    def normalize_balances(
        self,
        account: AccountRef,
        payload: object,
    ) -> Sequence[NormalizedBalance]:
        ...

    def normalize_positions(
        self,
        account: AccountRef,
        payload: object,
        *,
        contracts: dict[str, ContractSpec],
    ) -> Sequence[NormalizedPosition]:
        ...


def utc_timestamp_milliseconds(value: object, *, fallback: datetime) -> datetime:
    try:
        milliseconds = int(str(value))
    except (TypeError, ValueError):
        return fallback.astimezone(timezone.utc)
    return datetime.fromtimestamp(milliseconds / 1000.0, tz=timezone.utc)

