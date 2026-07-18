"""Pure Binance response normalization; no order endpoint exists here."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
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


def _filter(filters: list[dict[str, Any]], filter_type: str) -> dict[str, Any]:
    return next((row for row in filters if row.get("filterType") == filter_type), {})


class BinanceReadOnlyAdapter:
    venue = Venue.BINANCE

    def normalize_contracts(
        self, payload: dict, *, observed_at: datetime
    ) -> Sequence[ContractSpec]:
        del observed_at
        result: list[ContractSpec] = []
        for row in payload.get("symbols") or []:
            symbol = str(row.get("symbol") or "").upper()
            base = str(row.get("baseAsset") or "").upper()
            quote = str(row.get("quoteAsset") or "").upper()
            settlement = str(row.get("marginAsset") or quote).upper()
            if not symbol or not base or not quote:
                continue
            filters = list(row.get("filters") or [])
            price = _filter(filters, "PRICE_FILTER")
            lot = _filter(filters, "LOT_SIZE")
            notional = _filter(filters, "NOTIONAL") or _filter(filters, "MIN_NOTIONAL")
            contract_type = (
                ContractType.SPOT
                if "contractType" not in row
                else ContractType.LINEAR_PERPETUAL
            )
            result.append(
                ContractSpec(
                    venue=self.venue,
                    symbol=symbol,
                    contract_type=contract_type,
                    base_asset=base,
                    quote_asset=quote,
                    settlement_asset=settlement,
                    contract_multiplier=decimal_value(row.get("contractSize", "1"), "contractSize"),
                    quantity_step=decimal_value(lot.get("stepSize", "0"), "stepSize"),
                    minimum_quantity=decimal_value(lot.get("minQty", "0"), "minQty"),
                    minimum_notional=decimal_value(
                        notional.get("notional") or notional.get("minNotional") or "0",
                        "minNotional",
                    ),
                    price_tick=decimal_value(price.get("tickSize", "0"), "tickSize"),
                    funding_interval_hours=int(row.get("fundingIntervalHours") or 8),
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
        rows = payload if isinstance(payload, list) else [payload]
        result: list[FundingQuote] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").upper()
            contract = contracts.get(symbol)
            if contract is None or contract.contract_type is ContractType.SPOT:
                continue
            result.append(
                FundingQuote(
                    venue=self.venue,
                    symbol=symbol,
                    raw_rate=decimal_value(
                        row.get("nextFundingRate") or row.get("lastFundingRate") or "0",
                        "fundingRate",
                    ),
                    interval_hours=contract.funding_interval_hours,
                    next_settlement_time=utc_timestamp_milliseconds(
                        row.get("nextFundingTime"), fallback=observed_at
                    ),
                    observed_at=observed_at.astimezone(timezone.utc),
                )
            )
        return result

    def normalize_balances(
        self, account: AccountRef, payload: object
    ) -> Sequence[NormalizedBalance]:
        if account.venue is not self.venue:
            raise ValueError("account venue does not match Binance adapter")
        rows: list[dict[str, Any]] = []
        wallet = "futures"
        if isinstance(payload, dict) and isinstance(payload.get("balances"), list):
            rows = list(payload["balances"])
            wallet = "spot"
        elif isinstance(payload, dict) and isinstance(payload.get("assets"), list):
            rows = list(payload["assets"])
        result: list[NormalizedBalance] = []
        for row in rows:
            asset = str(row.get("asset") or "").upper()
            if not asset:
                continue
            if wallet == "spot":
                available = decimal_value(row.get("free", "0"), "free")
                locked = decimal_value(row.get("locked", "0"), "locked")
                total = available + locked
            else:
                total = decimal_value(
                    row.get("walletBalance") or row.get("marginBalance") or "0",
                    "walletBalance",
                )
                available = decimal_value(
                    row.get("availableBalance") or total, "availableBalance"
                )
            result.append(NormalizedBalance(account, asset, wallet, total, available))
        return result

    def normalize_positions(
        self,
        account: AccountRef,
        payload: object,
        *,
        contracts: dict[str, ContractSpec],
    ) -> Sequence[NormalizedPosition]:
        if account.venue is not self.venue:
            raise ValueError("account venue does not match Binance adapter")
        rows = payload if isinstance(payload, list) else []
        result: list[NormalizedPosition] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").upper()
            contract = contracts.get(symbol)
            if contract is None:
                continue
            quantity = decimal_value(row.get("positionAmt", "0"), "positionAmt")
            if quantity == 0:
                continue
            liquidation_raw = row.get("liquidationPrice")
            liquidation = (
                decimal_value(liquidation_raw, "liquidationPrice")
                if liquidation_raw not in (None, "", "0", 0)
                else None
            )
            result.append(
                NormalizedPosition(
                    account=account,
                    venue_symbol=symbol,
                    contract=contract,
                    signed_quantity=quantity,
                    mark_price=decimal_value(row.get("markPrice", "0"), "markPrice"),
                    entry_price=decimal_value(row.get("entryPrice", "0"), "entryPrice"),
                    liquidation_price=liquidation,
                    initial_margin=decimal_value(row.get("initialMargin", "0"), "initialMargin"),
                    maintenance_margin=decimal_value(
                        row.get("maintMargin", "0"), "maintMargin"
                    ),
                )
            )
        return result

