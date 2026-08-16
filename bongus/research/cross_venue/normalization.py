"""Fixed v1 instrument mappings and Binance public-response normalization."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from types import MappingProxyType
from typing import Final

from bongus.research.cross_venue.schema import (
    CanonicalAsset,
    ContractMetadata,
    FundingPriceKind,
    FundingQuote,
    FundingSettlement,
    InstrumentMapping,
    Venue,
    deterministic_event_id,
    epoch_nanoseconds,
    exact_decimal,
    positive_decimal,
)


class CrossVenueNormalizationError(ValueError):
    """Raised when a public payload cannot satisfy the frozen v1 contract."""


class AmbiguousProductError(CrossVenueNormalizationError):
    pass


class ExcludedProductError(CrossVenueNormalizationError):
    pass


_MAPPINGS: Final[dict[CanonicalAsset, InstrumentMapping]] = {
    CanonicalAsset.BTC: InstrumentMapping(
        canonical_asset=CanonicalAsset.BTC,
        binance_symbol="BTCUSDT",
        binance_contract_id="BTCUSDT:PERPETUAL",
        hyperliquid_coin="BTC",
        hyperliquid_contract_id="core:BTC",
    ),
    CanonicalAsset.ETH: InstrumentMapping(
        canonical_asset=CanonicalAsset.ETH,
        binance_symbol="ETHUSDT",
        binance_contract_id="ETHUSDT:PERPETUAL",
        hyperliquid_coin="ETH",
        hyperliquid_contract_id="core:ETH",
    ),
    CanonicalAsset.SOL: InstrumentMapping(
        canonical_asset=CanonicalAsset.SOL,
        binance_symbol="SOLUSDT",
        binance_contract_id="SOLUSDT:PERPETUAL",
        hyperliquid_coin="SOL",
        hyperliquid_contract_id="core:SOL",
    ),
    CanonicalAsset.XRP: InstrumentMapping(
        canonical_asset=CanonicalAsset.XRP,
        binance_symbol="XRPUSDT",
        binance_contract_id="XRPUSDT:PERPETUAL",
        hyperliquid_coin="XRP",
        hyperliquid_contract_id="core:XRP",
    ),
    CanonicalAsset.DOGE: InstrumentMapping(
        canonical_asset=CanonicalAsset.DOGE,
        binance_symbol="DOGEUSDT",
        binance_contract_id="DOGEUSDT:PERPETUAL",
        hyperliquid_coin="DOGE",
        hyperliquid_contract_id="core:DOGE",
    ),
}
FIXED_V1_MAPPINGS: Final[Mapping[CanonicalAsset, InstrumentMapping]] = MappingProxyType(_MAPPINGS)
_BY_BINANCE: Final[dict[str, InstrumentMapping]] = {
    mapping.binance_symbol: mapping for mapping in FIXED_V1_MAPPINGS.values()
}
_BY_HYPERLIQUID: Final[dict[str, InstrumentMapping]] = {
    mapping.hyperliquid_coin: mapping for mapping in FIXED_V1_MAPPINGS.values()
}


def mapping_for_asset(asset: CanonicalAsset | str) -> InstrumentMapping:
    try:
        canonical = asset if isinstance(asset, CanonicalAsset) else CanonicalAsset(str(asset).strip().upper())
    except ValueError as exc:
        raise ExcludedProductError(f"asset is outside the fixed v1 universe: {asset!r}") from exc
    return FIXED_V1_MAPPINGS[canonical]


def mapping_for_binance_symbol(symbol: str) -> InstrumentMapping:
    normalized = str(symbol).strip().upper()
    try:
        return _BY_BINANCE[normalized]
    except KeyError as exc:
        raise ExcludedProductError(f"Binance symbol is outside the fixed v1 universe: {symbol!r}") from exc


def mapping_for_hyperliquid_coin(coin: str) -> InstrumentMapping:
    normalized = str(coin).strip().upper()
    try:
        return _BY_HYPERLIQUID[normalized]
    except KeyError as exc:
        raise ExcludedProductError(f"Hyperliquid product is not an approved core perp: {coin!r}") from exc


def _mapping_row(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise CrossVenueNormalizationError(f"{field_name} must be an object")
    return value


def _sequence(value: object, field_name: str) -> Sequence[object]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise CrossVenueNormalizationError(f"{field_name} must be an array")
    return value


def _milliseconds_to_nanoseconds(value: object, field_name: str) -> int:
    milliseconds = epoch_nanoseconds(value if isinstance(value, (int, str)) else "", field_name)
    return milliseconds * 1_000_000


def _optional_decimal(value: object, field_name: str) -> Decimal | None:
    if value in (None, ""):
        return None
    if isinstance(value, (Decimal, str, int)) and not isinstance(value, bool):
        return positive_decimal(value, field_name)
    raise CrossVenueNormalizationError(f"{field_name} must be an exact decimal string")


def _required_decimal(
    value: object,
    field_name: str,
    *,
    default: str | None = None,
) -> Decimal:
    if value in (None, ""):
        if default is None:
            raise CrossVenueNormalizationError(f"{field_name} must be an exact decimal string")
        value = default
    if isinstance(value, (Decimal, str, int)) and not isinstance(value, bool):
        return exact_decimal(value, field_name)
    raise CrossVenueNormalizationError(f"{field_name} must be an exact decimal string")


def select_hyperliquid_core_rows(
    universe: object,
    *,
    require_complete: bool = True,
) -> Mapping[CanonicalAsset, tuple[int, Mapping[str, object]]]:
    """Select each approved core product exactly once and reject aliases."""

    selected: dict[CanonicalAsset, tuple[int, Mapping[str, object]]] = {}
    for index, value in enumerate(_sequence(universe, "Hyperliquid universe")):
        row = _mapping_row(value, f"Hyperliquid universe[{index}]")
        raw_name = row.get("name")
        if not isinstance(raw_name, str):
            continue
        name = raw_name.strip()
        mapping = _BY_HYPERLIQUID.get(name)
        if mapping is None:
            continue
        flagged_non_core = (
            row.get("isHip3") not in (None, False)
            or row.get("isHyperp") not in (None, False)
            or str(row.get("dex") or "").strip().lower() not in {"", "core", "hyperliquid"}
            or str(row.get("productType") or "").strip().lower()
            not in {"", "core", "perp", "perpetual", "linear_perpetual"}
        )
        if flagged_non_core:
            raise ExcludedProductError(f"approved symbol {name} is marked as a non-core product")
        if mapping.canonical_asset in selected:
            raise AmbiguousProductError(f"duplicate Hyperliquid core product: {name}")
        selected[mapping.canonical_asset] = (index, row)
    if require_complete:
        missing = [asset.value for asset in CanonicalAsset if asset not in selected]
        if missing:
            raise CrossVenueNormalizationError(
                "Hyperliquid core universe is missing fixed v1 products: " + ", ".join(missing)
            )
    return MappingProxyType(selected)


def normalize_binance_funding_intervals(
    payload: object,
    *,
    standard_interval_hours: Decimal | str | int,
) -> Mapping[CanonicalAsset, Decimal]:
    """Apply Binance's explicit adjusted intervals over an explicit standard."""

    standard = positive_decimal(standard_interval_hours, "standard_interval_hours")
    result = {asset: standard for asset in CanonicalAsset}
    seen: set[CanonicalAsset] = set()
    for index, value in enumerate(_sequence(payload, "Binance funding info")):
        row = _mapping_row(value, f"Binance funding info[{index}]")
        symbol = str(row.get("symbol") or "").strip().upper()
        mapping = _BY_BINANCE.get(symbol)
        if mapping is None:
            continue
        if mapping.canonical_asset in seen:
            raise AmbiguousProductError(f"duplicate Binance funding interval for {symbol}")
        interval_value = row.get("fundingIntervalHours")
        if not isinstance(interval_value, (Decimal, str, int)) or isinstance(interval_value, bool):
            raise CrossVenueNormalizationError(f"missing exact funding interval for {symbol}")
        result[mapping.canonical_asset] = positive_decimal(interval_value, "fundingIntervalHours")
        seen.add(mapping.canonical_asset)
    return MappingProxyType(result)


def normalize_binance_contracts(
    payload: object,
    *,
    funding_interval_hours: Mapping[CanonicalAsset, Decimal],
) -> Mapping[CanonicalAsset, ContractMetadata]:
    root = _mapping_row(payload, "Binance exchange info")
    rows = _sequence(root.get("symbols"), "Binance exchange info symbols")
    result: dict[CanonicalAsset, ContractMetadata] = {}
    for index, value in enumerate(rows):
        row = _mapping_row(value, f"Binance symbol[{index}]")
        symbol = str(row.get("symbol") or "").strip().upper()
        mapping = _BY_BINANCE.get(symbol)
        if mapping is None:
            continue
        if mapping.canonical_asset in result:
            raise AmbiguousProductError(f"duplicate Binance contract: {symbol}")
        if (
            str(row.get("contractType") or "").strip().upper() != "PERPETUAL"
            or str(row.get("baseAsset") or "").strip().upper() != mapping.canonical_asset.value
            or str(row.get("quoteAsset") or "").strip().upper() != "USDT"
            or str(row.get("marginAsset") or "").strip().upper() != "USDT"
        ):
            raise ExcludedProductError(f"{symbol} is not the approved USDT linear perpetual")
        filters = _sequence(row.get("filters"), f"{symbol} filters")
        by_type: dict[str, Mapping[str, object]] = {}
        for filter_value in filters:
            filter_row = _mapping_row(filter_value, f"{symbol} filter")
            filter_type = str(filter_row.get("filterType") or "").strip().upper()
            if filter_type:
                if filter_type in by_type:
                    raise AmbiguousProductError(f"duplicate Binance {filter_type} filter for {symbol}")
                by_type[filter_type] = filter_row
        lot = by_type.get("LOT_SIZE")
        price = by_type.get("PRICE_FILTER")
        if lot is None or price is None:
            raise CrossVenueNormalizationError(f"{symbol} is missing exact lot or price filters")
        interval = funding_interval_hours.get(mapping.canonical_asset)
        if interval is None:
            raise CrossVenueNormalizationError(f"{symbol} is missing an authoritative funding interval")
        result[mapping.canonical_asset] = ContractMetadata(
            venue=Venue.BINANCE,
            canonical_asset=mapping.canonical_asset,
            venue_symbol=mapping.binance_symbol,
            contract_id=mapping.binance_contract_id,
            base_asset=mapping.canonical_asset.value,
            quote_asset="USDT",
            settlement_asset="USDT",
            contract_multiplier=_required_decimal(row.get("contractSize"), "contractSize", default="1"),
            quantity_step=_required_decimal(lot.get("stepSize"), "stepSize"),
            price_tick=_required_decimal(price.get("tickSize"), "tickSize"),
            funding_interval_hours=interval,
            status=str(row.get("status") or "UNKNOWN").strip().upper(),
        )
    missing = [asset.value for asset in CanonicalAsset if asset not in result]
    if missing:
        raise CrossVenueNormalizationError("Binance exchange info is missing fixed v1 products: " + ", ".join(missing))
    return MappingProxyType(result)


def normalize_binance_funding_history(
    payload: object,
    *,
    contracts: Mapping[CanonicalAsset, ContractMetadata],
    available_time_ns: int | str,
) -> tuple[FundingSettlement, ...]:
    available = epoch_nanoseconds(available_time_ns, "available_time_ns")
    result: list[FundingSettlement] = []
    for index, value in enumerate(_sequence(payload, "Binance funding history")):
        row = _mapping_row(value, f"Binance funding history[{index}]")
        symbol = str(row.get("symbol") or "").strip().upper()
        mapping = _BY_BINANCE.get(symbol)
        if mapping is None:
            continue
        contract = contracts.get(mapping.canonical_asset)
        if contract is None or contract.venue is not Venue.BINANCE:
            raise CrossVenueNormalizationError(f"missing Binance contract metadata for {symbol}")
        settlement = _milliseconds_to_nanoseconds(row.get("fundingTime"), "fundingTime")
        rate_value = row.get("fundingRate")
        if not isinstance(rate_value, (Decimal, str, int)) or isinstance(rate_value, bool):
            raise CrossVenueNormalizationError("fundingRate must be exact")
        result.append(
            FundingSettlement(
                event_id=deterministic_event_id(
                    Venue.BINANCE.value,
                    contract.contract_id,
                    "funding_settlement",
                    str(settlement),
                ),
                venue=Venue.BINANCE,
                canonical_asset=mapping.canonical_asset,
                contract_id=contract.contract_id,
                settlement_time_ns=settlement,
                available_time_ns=available,
                rate=exact_decimal(rate_value, "fundingRate"),
                settlement_price=_optional_decimal(row.get("markPrice"), "markPrice"),
                price_kind=FundingPriceKind.MARK,
                contract_multiplier=contract.contract_multiplier,
            )
        )
    return tuple(sorted(result, key=lambda item: (item.settlement_time_ns, item.canonical_asset.value)))


def normalize_binance_premium_index(
    payload: object,
    *,
    contracts: Mapping[CanonicalAsset, ContractMetadata],
    available_time_ns: int | str,
) -> tuple[FundingQuote, ...]:
    available = epoch_nanoseconds(available_time_ns, "available_time_ns")
    rows: Sequence[object]
    if isinstance(payload, Mapping):
        rows = (payload,)
    else:
        rows = _sequence(payload, "Binance premium index")
    result: list[FundingQuote] = []
    for index, value in enumerate(rows):
        row = _mapping_row(value, f"Binance premium index[{index}]")
        mapping = _BY_BINANCE.get(str(row.get("symbol") or "").strip().upper())
        if mapping is None:
            continue
        contract = contracts.get(mapping.canonical_asset)
        if contract is None:
            raise CrossVenueNormalizationError("premium index lacks contract metadata")
        source_ms = row.get("time")
        if source_ms in (None, ""):
            source_ms = row.get("nextFundingTime")
        source = _milliseconds_to_nanoseconds(source_ms, "time")
        rate_value = row.get("lastFundingRate")
        if not isinstance(rate_value, (Decimal, str, int)) or isinstance(rate_value, bool):
            raise CrossVenueNormalizationError("lastFundingRate must be exact")
        next_settlement_raw = row.get("nextFundingTime")
        next_settlement = (
            _milliseconds_to_nanoseconds(next_settlement_raw, "nextFundingTime")
            if next_settlement_raw not in (None, "")
            else None
        )
        result.append(
            FundingQuote(
                event_id=deterministic_event_id(
                    Venue.BINANCE.value,
                    contract.contract_id,
                    "funding_quote",
                    str(source),
                ),
                venue=Venue.BINANCE,
                canonical_asset=mapping.canonical_asset,
                contract_id=contract.contract_id,
                rate=exact_decimal(rate_value, "lastFundingRate"),
                interval_hours=contract.funding_interval_hours,
                source_time_ns=source,
                available_time_ns=available,
                next_settlement_time_ns=next_settlement,
                mark_price=_optional_decimal(row.get("markPrice"), "markPrice"),
            )
        )
    return tuple(sorted(result, key=lambda item: item.canonical_asset.value))


__all__ = [
    "AmbiguousProductError",
    "CrossVenueNormalizationError",
    "ExcludedProductError",
    "FIXED_V1_MAPPINGS",
    "mapping_for_asset",
    "mapping_for_binance_symbol",
    "mapping_for_hyperliquid_coin",
    "normalize_binance_contracts",
    "normalize_binance_funding_history",
    "normalize_binance_funding_intervals",
    "normalize_binance_premium_index",
    "select_hyperliquid_core_rows",
]
