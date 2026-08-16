from __future__ import annotations

from decimal import Decimal
from typing import cast

import pytest

from bongus.exchanges.hyperliquid_read_only import HyperliquidReadOnlyAdapter
from bongus.research.cross_venue.normalization import (
    FIXED_V1_MAPPINGS,
    AmbiguousProductError,
    CrossVenueNormalizationError,
    ExcludedProductError,
    normalize_binance_contracts,
    normalize_binance_funding_history,
    normalize_binance_funding_intervals,
    normalize_binance_premium_index,
    select_hyperliquid_core_rows,
)
from bongus.research.cross_venue.schema import CanonicalAsset, FundingPriceKind, Venue


def _binance_exchange_info() -> dict[str, object]:
    return {
        "symbols": [
            {
                "symbol": f"{asset.value}USDT",
                "baseAsset": asset.value,
                "quoteAsset": "USDT",
                "marginAsset": "USDT",
                "contractType": "PERPETUAL",
                "status": "TRADING",
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                    {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                ],
            }
            for asset in CanonicalAsset
        ]
        + [
            {
                "symbol": "BTCUSD_PERP",
                "baseAsset": "BTC",
                "quoteAsset": "USD",
                "marginAsset": "BTC",
                "contractType": "PERPETUAL",
                "filters": [],
            }
        ]
    }


def _hyperliquid_meta() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    universe: list[dict[str, object]] = [{"name": asset.value, "szDecimals": 5} for asset in CanonicalAsset]
    contexts: list[dict[str, object]] = [
        {
            "funding": "0.0001",
            "oraclePx": str(100 + index),
            "markPx": str(100 + index) + ".1",
        }
        for index, _asset in enumerate(CanonicalAsset)
    ]
    return universe, contexts


def test_fixed_mapping_is_explicit_and_complete() -> None:
    assert tuple(FIXED_V1_MAPPINGS) == tuple(CanonicalAsset)
    assert FIXED_V1_MAPPINGS[CanonicalAsset.BTC].binance_symbol == "BTCUSDT"
    assert FIXED_V1_MAPPINGS[CanonicalAsset.BTC].hyperliquid_contract_id == "core:BTC"


def test_hyperliquid_selection_ignores_aliases_and_rejects_ambiguity_or_non_core_flags() -> None:
    universe, _ = _hyperliquid_meta()
    selected = select_hyperliquid_core_rows(
        universe
        + [
            {"name": "xyz:BTC", "szDecimals": 5, "isHip3": True},
            {"name": "@BTC", "szDecimals": 5, "isHyperp": True},
            {"name": "BTC-PERP", "szDecimals": 5},
        ]
    )
    assert tuple(selected) == tuple(CanonicalAsset)
    with pytest.raises(AmbiguousProductError, match="duplicate"):
        select_hyperliquid_core_rows(universe + [{"name": "BTC", "szDecimals": 5}])
    flagged = [dict(row) for row in universe]
    flagged[0]["isHyperp"] = True
    with pytest.raises(ExcludedProductError, match="non-core"):
        select_hyperliquid_core_rows(flagged)
    malformed_flag = [dict(row) for row in universe]
    malformed_flag[0]["isHip3"] = "false"
    with pytest.raises(ExcludedProductError, match="non-core"):
        select_hyperliquid_core_rows(malformed_flag)


def test_binance_contracts_use_authoritative_intervals_and_exact_decimals() -> None:
    intervals = normalize_binance_funding_intervals(
        [{"symbol": "SOLUSDT", "fundingIntervalHours": 4}],
        standard_interval_hours="8",
    )
    contracts = normalize_binance_contracts(
        _binance_exchange_info(),
        funding_interval_hours=intervals,
    )
    assert contracts[CanonicalAsset.BTC].funding_interval_hours == Decimal("8")
    assert contracts[CanonicalAsset.SOL].funding_interval_hours == Decimal("4")
    assert contracts[CanonicalAsset.BTC].quantity_step == Decimal("0.001")
    with pytest.raises(TypeError, match="Decimal"):
        normalize_binance_funding_intervals([], standard_interval_hours=cast(Decimal | str | int, 8.0))


def test_binance_final_funding_and_quotes_remain_venue_aware() -> None:
    intervals = normalize_binance_funding_intervals([], standard_interval_hours="8")
    contracts = normalize_binance_contracts(
        _binance_exchange_info(),
        funding_interval_hours=intervals,
    )
    settlement_ms = 1_700_000_000_000
    available_ns = settlement_ms * 1_000_000 + 1
    settlements = normalize_binance_funding_history(
        [
            {
                "symbol": "BTCUSDT",
                "fundingTime": settlement_ms,
                "fundingRate": "0.0004",
                "markPrice": "50000.25",
            }
        ],
        contracts=contracts,
        available_time_ns=available_ns,
    )
    assert settlements[0].venue is Venue.BINANCE
    assert settlements[0].rate == Decimal("0.0004")
    assert settlements[0].settlement_price == Decimal("50000.25")
    assert settlements[0].price_kind is FundingPriceKind.MARK
    quotes = normalize_binance_premium_index(
        {
            "symbol": "BTCUSDT",
            "time": settlement_ms,
            "nextFundingTime": settlement_ms + 8 * 60 * 60 * 1000,
            "lastFundingRate": "0.0003",
            "markPrice": "50001",
        },
        contracts=contracts,
        available_time_ns=available_ns,
    )
    assert quotes[0].interval_hours == Decimal("8")


def test_hyperliquid_adapter_parses_only_fixed_core_contracts_and_public_context() -> None:
    universe, contexts = _hyperliquid_meta()
    source_ns = 1_700_000_000_000_000_000
    adapter = HyperliquidReadOnlyAdapter()
    contracts, quotes = adapter.normalize_meta_and_asset_contexts(
        [{"universe": universe}, contexts],
        source_time_ns=source_ns,
        available_time_ns=source_ns + 1,
    )
    assert len(contracts) == len(quotes) == 5
    btc_contract = next(item for item in contracts if item.canonical_asset is CanonicalAsset.BTC)
    btc_quote = next(item for item in quotes if item.canonical_asset is CanonicalAsset.BTC)
    assert btc_contract.quote_asset == "USDT"
    assert btc_contract.settlement_asset == "USDC"
    assert btc_contract.funding_interval_hours == Decimal("1")
    assert btc_quote.oracle_price == Decimal("100")

    settlement_ms = 1_700_000_000_000
    history = adapter.normalize_funding_history(
        [{"coin": "BTC", "time": settlement_ms, "fundingRate": "0.0001"}],
        asset="BTC",
        available_time_ns=settlement_ms * 1_000_000 + 1,
    )
    assert history[0].price_kind is FundingPriceKind.ORACLE
    assert history[0].settlement_price is None
    with pytest.raises(CrossVenueNormalizationError, match="explicit coin"):
        adapter.normalize_funding_history(
            [{"time": settlement_ms, "fundingRate": "0.0001"}],
            asset="BTC",
            available_time_ns=settlement_ms * 1_000_000 + 1,
        )
    book = adapter.normalize_l2_book(
        {
            "coin": "BTC",
            "time": settlement_ms,
            "levels": [
                [{"px": "99", "sz": "2"}, {"px": "100", "sz": "1"}],
                [{"px": "102", "sz": "2"}, {"px": "101", "sz": "1"}],
            ],
        },
        asset="BTC",
        available_time_ns=settlement_ms * 1_000_000 + 1,
    )
    assert (book.bid_price, book.ask_price) == (Decimal("100"), Decimal("101"))


def test_hyperliquid_context_shape_and_float_values_fail_closed() -> None:
    universe, contexts = _hyperliquid_meta()
    adapter = HyperliquidReadOnlyAdapter()
    with pytest.raises(CrossVenueNormalizationError, match="lengths differ"):
        adapter.normalize_meta_and_asset_contexts(
            [{"universe": universe}, contexts[:-1]],
            source_time_ns=10,
            available_time_ns=11,
        )
    contexts[0]["funding"] = 0.0001
    with pytest.raises(CrossVenueNormalizationError, match="funding must be exact"):
        adapter.normalize_meta_and_asset_contexts(
            [{"universe": universe}, contexts],
            source_time_ns=10,
            available_time_ns=11,
        )
