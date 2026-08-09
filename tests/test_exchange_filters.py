from __future__ import annotations

from decimal import Decimal

import pytest

from bongus.engine.exchange_filters import (
    ExchangeFilterRegistry,
    IncompleteExchangeMetadata,
)


def _symbol(
    *,
    status: str = "TRADING",
    min_notional: str = "5",
    max_notional: str = "10000",
) -> dict:
    return {
        "symbol": "BTCUSDT",
        "status": status,
        "filters": [
            {
                "filterType": "PRICE_FILTER",
                "minPrice": "0.01",
                "maxPrice": "1000000",
                "tickSize": "0.01",
            },
            {
                "filterType": "LOT_SIZE",
                "minQty": "0.001",
                "maxQty": "100",
                "stepSize": "0.001",
            },
            {
                "filterType": "MARKET_LOT_SIZE",
                "minQty": "0.01",
                "maxQty": "10",
                "stepSize": "0.01",
            },
            {
                "filterType": "NOTIONAL",
                "minNotional": min_notional,
                "maxNotional": max_notional,
                "applyMinToMarket": True,
                "applyMaxToMarket": True,
            },
        ],
    }


def _registry(*, now: float = 100.0, status: str = "TRADING") -> ExchangeFilterRegistry:
    registry = ExchangeFilterRegistry(metadata_ttl_seconds=60.0, clock=lambda: now)
    registry.replace_market("spot", {"symbols": [_symbol(status=status)]}, received_at=100.0)
    return registry


def test_exact_decimal_limit_filters_accept_aligned_order() -> None:
    result = _registry().validate_order(
        symbol="BTCUSDT",
        market="spot",
        side="BUY",
        order_type="LIMIT",
        quantity="0.123",
        price="100.30",
    )
    assert result.accepted
    assert result.quantity == Decimal("0.123")
    assert result.price == Decimal("100.30")
    assert result.notional == Decimal("12.33690")


@pytest.mark.parametrize(
    ("quantity", "price", "reason"),
    [
        ("0.0005", "10000", "quantity_below_minimum"),
        ("100.001", "100", "quantity_above_maximum"),
        ("0.0015", "10000", "quantity_off_step"),
        ("0.1", "0.001", "price_below_minimum"),
        ("0.1", "1000000.01", "price_above_maximum"),
        ("0.1", "100.005", "price_off_tick"),
        ("0.001", "100", "notional_below_minimum"),
        ("100", "101", "notional_above_maximum"),
    ],
)
def test_limit_boundaries_fail_with_specific_reason(quantity: str, price: str, reason: str) -> None:
    result = _registry().validate_order(
        symbol="BTCUSDT",
        market="spot",
        side="BUY",
        order_type="LIMIT",
        quantity=quantity,
        price=price,
    )
    assert not result.accepted
    assert reason in result.reasons


def test_market_lot_and_reference_notional_are_checked() -> None:
    registry = _registry()
    off_step = registry.validate_order(
        symbol="BTCUSDT",
        market="spot",
        side="SELL",
        order_type="MARKET",
        quantity="0.015",
        reference_price="1000",
    )
    over_max = registry.validate_order(
        symbol="BTCUSDT",
        market="spot",
        side="SELL",
        order_type="MARKET",
        quantity="10.01",
        reference_price="1000",
    )
    missing_reference = registry.validate_order(
        symbol="BTCUSDT",
        market="spot",
        side="SELL",
        order_type="MARKET",
        quantity="1.00",
    )
    assert "quantity_off_step" in off_step.reasons
    assert "quantity_above_maximum" in over_max.reasons
    assert "notional_price_missing" in missing_reference.reasons


def test_normalization_is_side_safe_and_never_uses_binary_float_grid_math() -> None:
    registry = _registry()
    buy = registry.validate_order(
        symbol="BTCUSDT",
        market="spot",
        side="BUY",
        order_type="LIMIT",
        quantity=0.1239,
        price=100.309,
        normalize=True,
    )
    sell = registry.validate_order(
        symbol="BTCUSDT",
        market="spot",
        side="SELL",
        order_type="LIMIT",
        quantity=0.1239,
        price=100.301,
        normalize=True,
    )
    assert buy.accepted and sell.accepted
    assert buy.quantity == Decimal("0.123")
    assert sell.quantity == Decimal("0.123")
    assert buy.price == Decimal("100.30")
    assert sell.price == Decimal("100.31")


def test_missing_stale_and_non_trading_metadata_fail_closed() -> None:
    missing = _registry().validate_order(
        symbol="ETHUSDT",
        market="spot",
        side="BUY",
        order_type="LIMIT",
        quantity="1",
        price="100",
    )
    stale = _registry(now=161.0).validate_order(
        symbol="BTCUSDT",
        market="spot",
        side="BUY",
        order_type="LIMIT",
        quantity="1",
        price="100",
    )
    halted = _registry(status="BREAK").validate_order(
        symbol="BTCUSDT",
        market="spot",
        side="BUY",
        order_type="LIMIT",
        quantity="1",
        price="100",
    )
    assert missing.reasons == ("metadata_missing",)
    assert "metadata_stale" in stale.reasons
    assert "symbol_status:BREAK" in halted.reasons


def test_malformed_new_snapshot_removes_old_symbol_metadata_atomically() -> None:
    registry = _registry()
    broken = _symbol()
    broken["filters"] = [item for item in broken["filters"] if item["filterType"] != "LOT_SIZE"]

    update = registry.replace_market("spot", {"symbols": [broken]}, received_at=101.0)
    result = registry.validate_order(
        symbol="BTCUSDT",
        market="spot",
        side="BUY",
        order_type="LIMIT",
        quantity="1",
        price="100",
        now=101.0,
    )

    assert update.accepted_symbols == ()
    assert update.rejected_symbols["BTCUSDT"] == ("LOT_SIZE:missing",)
    assert not result.accepted
    assert result.reasons == ("LOT_SIZE:missing",)


def test_transport_level_incomplete_snapshot_does_not_half_replace_registry() -> None:
    registry = _registry()
    with pytest.raises(IncompleteExchangeMetadata, match="symbols:missing"):
        registry.replace_market("spot", {"timezone": "UTC"}, received_at=101.0)
    assert registry.get("BTCUSDT", "spot") is not None


def test_duplicate_symbol_metadata_fails_closed() -> None:
    registry = _registry()
    update = registry.replace_market(
        "spot",
        {"symbols": [_symbol(), _symbol()]},
        received_at=101.0,
    )
    assert update.accepted_symbols == ()
    assert update.rejected_symbols["BTCUSDT"] == ("symbol:duplicate",)
    assert registry.get("BTCUSDT", "spot") is None


def test_metadata_changes_and_removed_symbols_are_explicit_and_applied_atomically() -> None:
    eth = _symbol()
    eth["symbol"] = "ETHUSDT"
    registry = ExchangeFilterRegistry(clock=lambda: 100.0)
    first = registry.replace_market("spot", {"symbols": [_symbol(), eth]}, received_at=100.0)
    assert first.changed_symbols == ("BTCUSDT", "ETHUSDT")

    changed_btc = _symbol()
    changed_btc["filters"][0]["tickSize"] = "0.10"
    second = registry.replace_market("spot", {"symbols": [changed_btc]}, received_at=101.0)

    assert second.changed_symbols == ("BTCUSDT",)
    assert second.removed_symbols == ("ETHUSDT",)
    assert registry.get("ETHUSDT", "spot") is None
    btc = registry.get("BTCUSDT", "spot")
    assert btc is not None
    assert btc.price.step == Decimal("0.10")


def test_futures_min_notional_variant_and_zero_market_step_use_lot_grid() -> None:
    symbol = _symbol()
    symbol["filters"] = [
        {
            **item,
            "stepSize": "0" if item["filterType"] == "MARKET_LOT_SIZE" else item.get("stepSize"),
        }
        if item["filterType"] == "MARKET_LOT_SIZE"
        else item
        for item in symbol["filters"]
        if item["filterType"] != "NOTIONAL"
    ]
    symbol["filters"].append({"filterType": "MIN_NOTIONAL", "notional": "5"})
    registry = ExchangeFilterRegistry(clock=lambda: 100.0)
    registry.replace_market("perp", {"symbols": [symbol]}, received_at=100.0)

    result = registry.validate_order(
        symbol="BTCUSDT",
        market="perp",
        side="SELL",
        order_type="MARKET",
        quantity="0.011",
        reference_price="1000",
    )
    assert result.accepted
    assert result.quantity == Decimal("0.011")


def test_all_generated_aligned_tick_and_lot_values_pass_grid_checks() -> None:
    registry = _registry()
    for quantity_units in range(10, 200, 7):
        quantity = Decimal(quantity_units) * Decimal("0.001")
        for price_ticks in range(10_000, 10_100, 11):
            price = Decimal(price_ticks) * Decimal("0.01")
            result = registry.validate_order(
                symbol="BTCUSDT",
                market="spot",
                side="BUY",
                order_type="LIMIT",
                quantity=quantity,
                price=price,
            )
            # Small generated notionals can legitimately fail only the minimum.
            assert set(result.reasons).issubset({"notional_below_minimum"})


def test_spot_and_perp_market_lot_filters_never_substitute_for_each_other() -> None:
    spot = _symbol()
    perp = _symbol()
    for item in perp["filters"]:
        if item["filterType"] == "MARKET_LOT_SIZE":
            item["minQty"] = "0.001"
            item["stepSize"] = "0.001"
    registry = ExchangeFilterRegistry(clock=lambda: 100.0)
    registry.replace_market("spot", {"symbols": [spot]}, received_at=100.0)
    registry.replace_market("perp", {"symbols": [perp]}, received_at=100.0)

    spot_result = registry.validate_order(
        symbol="BTCUSDT",
        market="spot",
        side="BUY",
        order_type="MARKET",
        quantity="0.011",
        reference_price="1000",
    )
    perp_result = registry.validate_order(
        symbol="BTCUSDT",
        market="perp",
        side="SELL",
        order_type="MARKET",
        quantity="0.011",
        reference_price="1000",
    )
    assert "quantity_off_step" in spot_result.reasons
    assert perp_result.accepted
