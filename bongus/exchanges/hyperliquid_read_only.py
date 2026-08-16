"""Pure normalization of approved Hyperliquid public info responses."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal

from bongus.research.cross_venue.normalization import (
    CrossVenueNormalizationError,
    mapping_for_asset,
    select_hyperliquid_core_rows,
)
from bongus.research.cross_venue.schema import (
    BboSnapshot,
    CanonicalAsset,
    ContractMetadata,
    FundingPriceKind,
    FundingQuote,
    FundingSettlement,
    Venue,
    deterministic_event_id,
    epoch_nanoseconds,
    exact_decimal,
    positive_decimal,
)


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise CrossVenueNormalizationError(f"{field_name} must be an object")
    return value


def _sequence(value: object, field_name: str) -> Sequence[object]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise CrossVenueNormalizationError(f"{field_name} must be an array")
    return value


def _milliseconds_to_nanoseconds(value: object, field_name: str) -> int:
    if not isinstance(value, (int, str)) or isinstance(value, bool):
        raise CrossVenueNormalizationError(f"{field_name} must be exact milliseconds")
    return epoch_nanoseconds(value, field_name) * 1_000_000


def _optional_positive(value: object, field_name: str) -> Decimal | None:
    if value in (None, ""):
        return None
    if not isinstance(value, (Decimal, str, int)) or isinstance(value, bool):
        raise CrossVenueNormalizationError(f"{field_name} must be an exact decimal")
    return positive_decimal(value, field_name)


class HyperliquidReadOnlyAdapter:
    """Response parser with no network, account, wallet, or mutation surface."""

    venue = Venue.HYPERLIQUID

    def normalize_meta_and_asset_contexts(
        self,
        payload: object,
        *,
        source_time_ns: int | str,
        available_time_ns: int | str,
    ) -> tuple[tuple[ContractMetadata, ...], tuple[FundingQuote, ...]]:
        root = _sequence(payload, "metaAndAssetCtxs response")
        if len(root) != 2:
            raise CrossVenueNormalizationError("metaAndAssetCtxs must contain metadata and contexts")
        metadata = _mapping(root[0], "Hyperliquid metadata")
        universe = _sequence(metadata.get("universe"), "Hyperliquid universe")
        contexts = _sequence(root[1], "Hyperliquid asset contexts")
        if len(universe) != len(contexts):
            raise CrossVenueNormalizationError("Hyperliquid universe/context lengths differ")
        selected = select_hyperliquid_core_rows(universe)
        source = epoch_nanoseconds(source_time_ns, "source_time_ns")
        available = epoch_nanoseconds(available_time_ns, "available_time_ns")
        contracts: list[ContractMetadata] = []
        quotes: list[FundingQuote] = []
        for asset in CanonicalAsset:
            index, row = selected[asset]
            context = _mapping(contexts[index], f"Hyperliquid context[{index}]")
            mapping = mapping_for_asset(asset)
            decimals_value = row.get("szDecimals")
            if not isinstance(decimals_value, (int, str)) or isinstance(decimals_value, bool):
                raise CrossVenueNormalizationError(f"{asset.value} szDecimals must be exact")
            size_decimals = int(decimals_value)
            if size_decimals < 0 or size_decimals > 18 or str(decimals_value).strip() != str(size_decimals):
                raise CrossVenueNormalizationError(f"{asset.value} szDecimals is invalid")
            delisted_value = row.get("isDelisted")
            if delisted_value not in (None, False, True):
                raise CrossVenueNormalizationError(f"{asset.value} isDelisted must be a boolean")
            quantity_step = Decimal(1).scaleb(-size_decimals)
            contract = ContractMetadata(
                venue=Venue.HYPERLIQUID,
                canonical_asset=asset,
                venue_symbol=mapping.hyperliquid_coin,
                contract_id=mapping.hyperliquid_contract_id,
                base_asset=asset.value,
                quote_asset="USDT",
                settlement_asset="USDC",
                contract_multiplier=Decimal("1"),
                quantity_step=quantity_step,
                price_tick=None,
                funding_interval_hours=Decimal("1"),
                status="DELISTED" if delisted_value is True else "TRADING",
            )
            funding_value = context.get("funding")
            if not isinstance(funding_value, (Decimal, str, int)) or isinstance(funding_value, bool):
                raise CrossVenueNormalizationError(f"{asset.value} funding must be exact")
            quote = FundingQuote(
                event_id=deterministic_event_id(
                    Venue.HYPERLIQUID.value,
                    contract.contract_id,
                    "funding_quote",
                    str(source),
                ),
                venue=Venue.HYPERLIQUID,
                canonical_asset=asset,
                contract_id=contract.contract_id,
                rate=exact_decimal(funding_value, "funding"),
                interval_hours=Decimal("1"),
                source_time_ns=source,
                available_time_ns=available,
                oracle_price=_optional_positive(context.get("oraclePx"), "oraclePx"),
                mark_price=_optional_positive(context.get("markPx"), "markPx"),
            )
            contracts.append(contract)
            quotes.append(quote)
        return tuple(contracts), tuple(quotes)

    def normalize_funding_history(
        self,
        payload: object,
        *,
        asset: CanonicalAsset | str,
        available_time_ns: int | str,
    ) -> tuple[FundingSettlement, ...]:
        mapping = mapping_for_asset(asset)
        available = epoch_nanoseconds(available_time_ns, "available_time_ns")
        result: list[FundingSettlement] = []
        for index, value in enumerate(_sequence(payload, "Hyperliquid funding history")):
            row = _mapping(value, f"Hyperliquid funding history[{index}]")
            coin_value = row.get("coin")
            if not isinstance(coin_value, str):
                raise CrossVenueNormalizationError("funding history requires an explicit coin")
            coin = coin_value.strip()
            if coin != mapping.hyperliquid_coin:
                raise CrossVenueNormalizationError("funding history contains a different product")
            settlement = _milliseconds_to_nanoseconds(row.get("time"), "time")
            rate_value = row.get("fundingRate")
            if not isinstance(rate_value, (Decimal, str, int)) or isinstance(rate_value, bool):
                raise CrossVenueNormalizationError("fundingRate must be exact")
            result.append(
                FundingSettlement(
                    event_id=deterministic_event_id(
                        Venue.HYPERLIQUID.value,
                        mapping.hyperliquid_contract_id,
                        "funding_settlement",
                        str(settlement),
                    ),
                    venue=Venue.HYPERLIQUID,
                    canonical_asset=mapping.canonical_asset,
                    contract_id=mapping.hyperliquid_contract_id,
                    settlement_time_ns=settlement,
                    available_time_ns=available,
                    rate=exact_decimal(rate_value, "fundingRate"),
                    settlement_price=None,
                    price_kind=FundingPriceKind.ORACLE,
                )
            )
        return tuple(sorted(result, key=lambda item: item.settlement_time_ns))

    def normalize_l2_book(
        self,
        payload: object,
        *,
        asset: CanonicalAsset | str,
        available_time_ns: int | str,
    ) -> BboSnapshot:
        mapping = mapping_for_asset(asset)
        root = _mapping(payload, "Hyperliquid l2Book")
        if str(root.get("coin") or "").strip() != mapping.hyperliquid_coin:
            raise CrossVenueNormalizationError("l2Book product does not match the request")
        source = _milliseconds_to_nanoseconds(root.get("time"), "time")
        available = epoch_nanoseconds(available_time_ns, "available_time_ns")
        levels = _sequence(root.get("levels"), "l2Book levels")
        if len(levels) != 2:
            raise CrossVenueNormalizationError("l2Book must contain bid and ask arrays")
        bids = _sequence(levels[0], "l2Book bids")
        asks = _sequence(levels[1], "l2Book asks")
        if not bids or not asks:
            raise CrossVenueNormalizationError("l2Book requires both sides")

        def level(value: object, field_name: str) -> tuple[Decimal, Decimal]:
            row = _mapping(value, field_name)
            price = row.get("px")
            quantity = row.get("sz")
            if (
                not isinstance(price, (Decimal, str, int))
                or isinstance(price, bool)
                or not isinstance(quantity, (Decimal, str, int))
                or isinstance(quantity, bool)
            ):
                raise CrossVenueNormalizationError(f"{field_name} must use exact decimals")
            return positive_decimal(price, "px"), positive_decimal(quantity, "sz")

        bid_price, bid_quantity = max((level(item, "bid") for item in bids), key=lambda item: item[0])
        ask_price, ask_quantity = min((level(item, "ask") for item in asks), key=lambda item: item[0])
        return BboSnapshot(
            event_id=deterministic_event_id(
                Venue.HYPERLIQUID.value,
                mapping.hyperliquid_contract_id,
                "bbo",
                str(source),
            ),
            venue=Venue.HYPERLIQUID,
            canonical_asset=mapping.canonical_asset,
            contract_id=mapping.hyperliquid_contract_id,
            source_time_ns=source,
            available_time_ns=available,
            bid_price=bid_price,
            bid_quantity=bid_quantity,
            ask_price=ask_price,
            ask_quantity=ask_quantity,
        )


__all__ = ["HyperliquidReadOnlyAdapter"]
