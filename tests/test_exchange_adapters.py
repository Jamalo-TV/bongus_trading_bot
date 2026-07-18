from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from bongus.exchanges.base import AccountRef, ContractType, Venue
from bongus.exchanges.binance_read_only import BinanceReadOnlyAdapter
from bongus.exchanges.bybit_read_only import BybitReadOnlyAdapter
from bongus.exchanges.multi_venue_monitor import VenueFundingLeg, compare_cross_venue_funding


NOW = datetime(2026, 7, 18, tzinfo=timezone.utc)


def binance_contract_payload():
    return {
        "symbols": [
            {
                "symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT",
                "marginAsset": "USDT", "contractType": "PERPETUAL", "status": "TRADING",
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                    {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5"},
                ],
            }
        ]
    }


def bybit_contract_payload(inverse=False):
    return {
        "result": {
            "list": [
                {
                    "symbol": "BTCUSD" if inverse else "BTCUSDT",
                    "baseCoin": "BTC", "quoteCoin": "USD" if inverse else "USDT",
                    "settleCoin": "BTC" if inverse else "USDT",
                    "contractType": "InversePerpetual" if inverse else "LinearPerpetual",
                    "status": "Trading", "contractSize": "1", "fundingInterval": 480,
                    "priceFilter": {"tickSize": "0.5"},
                    "lotSizeFilter": {"qtyStep": "1" if inverse else "0.001", "minOrderQty": "1", "minNotionalValue": "5"},
                }
            ]
        }
    }


def test_contract_normalization_preserves_linear_and_inverse_semantics() -> None:
    binance = BinanceReadOnlyAdapter().normalize_contracts(binance_contract_payload(), observed_at=NOW)[0]
    inverse = BybitReadOnlyAdapter().normalize_contracts(bybit_contract_payload(True), observed_at=NOW)[0]
    assert binance.contract_type is ContractType.LINEAR_PERPETUAL
    assert binance.notional_usd("0.1", "50000") == Decimal("5000.0")
    assert inverse.contract_type is ContractType.INVERSE_PERPETUAL
    assert inverse.notional_usd("1000", "50000") == Decimal("1000")
    assert inverse.base_exposure("1000", "50000") == Decimal("0.02")


def test_funding_and_positions_are_account_and_venue_isolated() -> None:
    adapter = BinanceReadOnlyAdapter()
    contract = adapter.normalize_contracts(binance_contract_payload(), observed_at=NOW)[0]
    contracts = {contract.symbol: contract}
    quote = adapter.normalize_funding(
        [{"symbol": "BTCUSDT", "lastFundingRate": "0.0001", "nextFundingTime": int(NOW.timestamp() * 1000)}],
        contracts=contracts, observed_at=NOW,
    )[0]
    assert quote.annualized_rate == Decimal("0.1095")
    account = AccountRef(Venue.BINANCE, "paper", "isolated-a")
    position = adapter.normalize_positions(
        account,
        [{"symbol": "BTCUSDT", "positionAmt": "-0.1", "markPrice": "50000", "entryPrice": "51000"}],
        contracts=contracts,
    )[0]
    assert position.signed_base_exposure == Decimal("-0.1")
    assert position.notional_usd == Decimal("5000.0")
    with pytest.raises(ValueError, match="does not match"):
        adapter.normalize_balances(AccountRef(Venue.BYBIT, "paper", "b"), {"balances": []})
    assert not hasattr(adapter, "place_order")


def test_cross_venue_monitor_subtracts_all_costs_and_capacity() -> None:
    binance_adapter = BinanceReadOnlyAdapter()
    binance_contract = binance_adapter.normalize_contracts(binance_contract_payload(), observed_at=NOW)[0]
    binance_quote = binance_adapter.normalize_funding(
        [{"symbol": "BTCUSDT", "lastFundingRate": "0.0004", "nextFundingTime": int(NOW.timestamp() * 1000)}],
        contracts={"BTCUSDT": binance_contract}, observed_at=NOW,
    )[0]
    bybit_adapter = BybitReadOnlyAdapter()
    bybit_contract = bybit_adapter.normalize_contracts(bybit_contract_payload(), observed_at=NOW)[0]
    bybit_quote = bybit_adapter.normalize_funding(
        {"result": {"list": [{"symbol": "BTCUSDT", "fundingRate": "-0.0002", "nextFundingTime": str(int(NOW.timestamp() * 1000))}]}},
        contracts={"BTCUSDT": bybit_contract}, observed_at=NOW,
    )[0]
    legs = [
        VenueFundingLeg(AccountRef(Venue.BINANCE, "paper", "a"), binance_quote, Decimal("5000"), Decimal("2")),
        VenueFundingLeg(AccountRef(Venue.BYBIT, "paper", "b"), bybit_quote, Decimal("3000"), Decimal("2"), transfer_cost_bps=Decimal("1")),
    ]
    opportunities = compare_cross_venue_funding(legs, minimum_net_edge_bps="1")
    best = opportunities[0]
    assert best.capacity_usd == Decimal("3000")
    assert best.eligible
    assert best.long_venue is Venue.BYBIT and best.short_venue is Venue.BINANCE
