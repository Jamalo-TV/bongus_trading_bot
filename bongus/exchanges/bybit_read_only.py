"""Pure Bybit response normalization with linear/inverse contract semantics."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from bongus.exchanges.base import (
    AccountRef,
    ContractSpec,
    ContractType,
    FundingQuote,
    NormalizedBalance,
    NormalizedPosition,
    Venue,
    decimal_value,
    utc_timestamp_milliseconds,
)


def _rows(payload: object) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    result = payload.get("result")
    if isinstance(result, dict) and isinstance(result.get("list"), list):
        return [row for row in result["list"] if isinstance(row, dict)]
    return []


class BybitReadOnlyAdapter:
    venue = Venue.BYBIT

    def normalize_contracts(
        self, payload: dict, *, observed_at: datetime
    ) -> Sequence[ContractSpec]:
        del observed_at
        result: list[ContractSpec] = []
        for row in _rows(payload):
            symbol = str(row.get("symbol") or "").upper()
            base = str(row.get("baseCoin") or "").upper()
            quote = str(row.get("quoteCoin") or "").upper()
            settlement = str(row.get("settleCoin") or quote).upper()
            if not symbol or not base or not quote:
                continue
            contract_label = str(row.get("contractType") or "").lower()
            is_inverse = settlement == base or "inverse" in contract_label
            price_filter = row.get("priceFilter") or {}
            lot_filter = row.get("lotSizeFilter") or {}
            result.append(
                ContractSpec(
                    venue=self.venue,
                    symbol=symbol,
                    contract_type=(
                        ContractType.INVERSE_PERPETUAL
                        if is_inverse
                        else ContractType.LINEAR_PERPETUAL
                    ),
                    base_asset=base,
                    quote_asset=quote,
                    settlement_asset=settlement,
                    contract_multiplier=decimal_value(
                        row.get("contractSize") or "1", "contractSize"
                    ),
                    quantity_step=decimal_value(
                        lot_filter.get("qtyStep") or "0", "qtyStep"
                    ),
                    minimum_quantity=decimal_value(
                        lot_filter.get("minOrderQty") or "0", "minOrderQty"
                    ),
                    minimum_notional=decimal_value(
                        lot_filter.get("minNotionalValue") or "0", "minNotionalValue"
                    ),
                    price_tick=decimal_value(
                        price_filter.get("tickSize") or "0", "tickSize"
                    ),
                    funding_interval_hours=max(
                        1, int(row.get("fundingInterval") or 480) // 60
                    ),
                    status=str(row.get("status") or "UNKNOWN").upper(),
                )
            )
        return result

    def normalize_funding(
        self,
        payload: object,
        *,
        contracts: dict[str, ContractSpec],
        observed_at: datetime,
    ) -> Sequence[FundingQuote]:
        result: list[FundingQuote] = []
        for row in _rows(payload):
            symbol = str(row.get("symbol") or "").upper()
            contract = contracts.get(symbol)
            if contract is None:
                continue
            next_time = utc_timestamp_milliseconds(
                row.get("nextFundingTime"),
                fallback=observed_at + timedelta(hours=contract.funding_interval_hours),
            )
            result.append(
                FundingQuote(
                    self.venue,
                    symbol,
                    decimal_value(row.get("fundingRate") or "0", "fundingRate"),
                    contract.funding_interval_hours,
                    next_time,
                    observed_at.astimezone(timezone.utc),
                )
            )
        return result

    def normalize_balances(
        self, account: AccountRef, payload: object
    ) -> Sequence[NormalizedBalance]:
        if account.venue is not self.venue:
            raise ValueError("account venue does not match Bybit adapter")
        result: list[NormalizedBalance] = []
        for wallet in _rows(payload):
            wallet_type = str(wallet.get("accountType") or "unified").lower()
            for row in wallet.get("coin") or []:
                asset = str(row.get("coin") or "").upper()
                if not asset:
                    continue
                total = decimal_value(row.get("walletBalance") or "0", "walletBalance")
                available = decimal_value(
                    row.get("availableToWithdraw") or row.get("availableBalance") or "0",
                    "availableBalance",
                )
                result.append(
                    NormalizedBalance(
                        account,
                        asset,
                        wallet_type,
                        total,
                        available,
                        decimal_value(row.get("borrowAmount") or "0", "borrowAmount"),
                        decimal_value(row.get("accruedInterest") or "0", "accruedInterest"),
                    )
                )
        return result

    def normalize_positions(
        self,
        account: AccountRef,
        payload: object,
        *,
        contracts: dict[str, ContractSpec],
    ) -> Sequence[NormalizedPosition]:
        if account.venue is not self.venue:
            raise ValueError("account venue does not match Bybit adapter")
        result: list[NormalizedPosition] = []
        for row in _rows(payload):
            symbol = str(row.get("symbol") or "").upper()
            contract = contracts.get(symbol)
            if contract is None:
                continue
            size = decimal_value(row.get("size") or "0", "size")
            if size == 0:
                continue
            signed = size if str(row.get("side") or "").lower() == "buy" else -size
            liquidation_raw = row.get("liqPrice")
            result.append(
                NormalizedPosition(
                    account,
                    symbol,
                    contract,
                    signed,
                    decimal_value(row.get("markPrice") or "0", "markPrice"),
                    decimal_value(row.get("avgPrice") or "0", "avgPrice"),
                    (
                        decimal_value(liquidation_raw, "liqPrice")
                        if liquidation_raw not in (None, "", "0", 0)
                        else None
                    ),
                    decimal_value(row.get("positionIM") or "0", "positionIM"),
                    decimal_value(row.get("positionMM") or "0", "positionMM"),
                )
            )
        return result

