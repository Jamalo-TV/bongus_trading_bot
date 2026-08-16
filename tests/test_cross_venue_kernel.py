from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from bongus.research.cross_venue.kernel import (
    daily_reporting_spread_bps,
    discrete_funding_cashflow,
    evaluate_primary_episode,
)
from bongus.research.cross_venue.schema import (
    CanonicalAsset,
    CommissionSchedule,
    FundingPriceKind,
    FundingSettlement,
    MatchedBaseEpisode,
    ReservedCapital,
    Venue,
)


def _settlement(
    venue: Venue,
    *,
    rate: str,
    price: str | None,
    asset: CanonicalAsset = CanonicalAsset.BTC,
) -> FundingSettlement:
    return FundingSettlement(
        event_id=f"{venue.value}-{rate}",
        venue=venue,
        canonical_asset=asset,
        contract_id="BTCUSDT:PERPETUAL" if venue is Venue.BINANCE else "core:BTC",
        settlement_time_ns=100,
        available_time_ns=101,
        rate=Decimal(rate),
        settlement_price=Decimal(price) if price is not None else None,
        price_kind=(FundingPriceKind.MARK if venue is Venue.BINANCE else FundingPriceKind.ORACLE),
    )


def test_unequal_interval_reporting_regression_is_negative_twelve_bps_per_day() -> None:
    # Binance short: +4 bp every 8h => +12 bp/day.
    # Hyperliquid long: +1 bp every 1h => -24 bp/day.
    assert daily_reporting_spread_bps(
        short_rate="0.0004",
        short_interval_hours="8",
        long_rate="0.0001",
        long_interval_hours="1",
    ) == Decimal("-12.00000")


def test_discrete_funding_uses_actual_event_price_and_position_sign() -> None:
    event = _settlement(Venue.HYPERLIQUID, rate="0.01", price="100")
    assert discrete_funding_cashflow(event, signed_base_quantity="1") == Decimal("-1.00")
    assert discrete_funding_cashflow(event, signed_base_quantity="-1") == Decimal("1.00")
    with pytest.raises(ValueError, match="settlement price"):
        discrete_funding_cashflow(
            _settlement(Venue.HYPERLIQUID, rate="0.01", price=None),
            signed_base_quantity="-1",
        )


def test_primary_episode_accounts_for_prices_four_commissions_costs_and_reserved_capital() -> None:
    episode = MatchedBaseEpisode(
        canonical_asset=CanonicalAsset.BTC,
        base_quantity=Decimal("1"),
        binance_entry_price=Decimal("100"),
        binance_exit_price=Decimal("110"),
        hyperliquid_entry_price=Decimal("102"),
        hyperliquid_exit_price=Decimal("108"),
        commissions=CommissionSchedule(
            binance_entry_rate=Decimal("0.001"),
            binance_exit_rate=Decimal("0.001"),
            hyperliquid_entry_rate=Decimal("0.001"),
            hyperliquid_exit_rate=Decimal("0.001"),
        ),
        reserved_capital=ReservedCapital(
            binance_collateral_usd=Decimal("50"),
            hyperliquid_collateral_usd=Decimal("60"),
            liquidation_buffers_usd=Decimal("20"),
            idle_transfer_buffer_usd=Decimal("10"),
        ),
        holding_period_days=Decimal("30"),
        binance_funding_events=(_settlement(Venue.BINANCE, rate="0.01", price="100"),),
        hyperliquid_funding_events=(_settlement(Venue.HYPERLIQUID, rate="0.02", price="100"),),
        stablecoin_conversion_cost_usd=Decimal("0.5"),
        collateral_opportunity_cost_usd=Decimal("0.2"),
        repair_failure_cost_usd=Decimal("0.3"),
    )
    result = evaluate_primary_episode(episode)
    assert result.binance_price_pnl_usd == Decimal("10")
    assert result.hyperliquid_price_pnl_usd == Decimal("-6")
    assert result.binance_funding_pnl_usd == Decimal("-1.00")
    assert result.hyperliquid_funding_pnl_usd == Decimal("2.00")
    assert result.binance_entry_commission_usd == Decimal("0.100")
    assert result.binance_exit_commission_usd == Decimal("0.110")
    assert result.hyperliquid_entry_commission_usd == Decimal("0.102")
    assert result.hyperliquid_exit_commission_usd == Decimal("0.108")
    assert result.total_commissions_usd == Decimal("0.420")
    assert result.net_pnl_usd == Decimal("3.580")
    assert result.total_reserved_capital_usd == Decimal("140")
    assert result.return_on_reserved_capital == Decimal("3.580") / Decimal("140")
    assert result.simple_annualized_return == (Decimal("3.580") / Decimal("140") * Decimal("365") / Decimal("30"))


def test_episode_rejects_wrong_venue_asset_and_float_inputs() -> None:
    wrong_asset = _settlement(
        Venue.BINANCE,
        rate="0.01",
        price="100",
        asset=CanonicalAsset.ETH,
    )
    base = MatchedBaseEpisode(
        canonical_asset=CanonicalAsset.BTC,
        base_quantity=Decimal("1"),
        binance_entry_price=Decimal("100"),
        binance_exit_price=Decimal("100"),
        hyperliquid_entry_price=Decimal("100"),
        hyperliquid_exit_price=Decimal("100"),
        commissions=CommissionSchedule(Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0")),
        reserved_capital=ReservedCapital(Decimal("1"), Decimal("1"), Decimal("0"), Decimal("0")),
        holding_period_days=Decimal("1"),
    )
    with pytest.raises(ValueError, match="does not match"):
        replace(base, binance_funding_events=(wrong_asset,))
    with pytest.raises(TypeError, match="Decimal"):
        replace(base, base_quantity=1.0)
    with pytest.raises(ValueError, match="matched base"):
        replace(base, hyperliquid_contract_multiplier=Decimal("2"))
    event = _settlement(Venue.BINANCE, rate="0.01", price="100")
    with pytest.raises(ValueError, match="more than once"):
        replace(base, binance_funding_events=(event, event))
