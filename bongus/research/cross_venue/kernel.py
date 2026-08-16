"""Pure Decimal cashflow kernel for the frozen cross-venue research policy."""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from bongus.research.cross_venue.schema import (
    EpisodePnl,
    ExactDecimalInput,
    FundingSettlement,
    MatchedBaseEpisode,
    Venue,
    exact_decimal,
    positive_decimal,
)

HOURS_PER_DAY = Decimal("24")
BASIS_POINTS_PER_UNIT = Decimal("10000")
DAYS_PER_YEAR = Decimal("365")


def hourly_reporting_spread(
    *,
    short_rate: ExactDecimalInput,
    short_interval_hours: ExactDecimalInput,
    long_rate: ExactDecimalInput,
    long_interval_hours: ExactDecimalInput,
) -> Decimal:
    """Reporting-only short-minus-long spread normalized by each interval."""

    short = exact_decimal(short_rate, "short_rate")
    long = exact_decimal(long_rate, "long_rate")
    short_interval = positive_decimal(short_interval_hours, "short_interval_hours")
    long_interval = positive_decimal(long_interval_hours, "long_interval_hours")
    return short / short_interval - long / long_interval


def daily_reporting_spread_bps(
    *,
    short_rate: ExactDecimalInput,
    short_interval_hours: ExactDecimalInput,
    long_rate: ExactDecimalInput,
    long_interval_hours: ExactDecimalInput,
) -> Decimal:
    return (
        hourly_reporting_spread(
            short_rate=short_rate,
            short_interval_hours=short_interval_hours,
            long_rate=long_rate,
            long_interval_hours=long_interval_hours,
        )
        * HOURS_PER_DAY
        * BASIS_POINTS_PER_UNIT
    )


def discrete_funding_cashflow(
    event: FundingSettlement,
    *,
    signed_base_quantity: ExactDecimalInput,
) -> Decimal:
    """Calculate one actual settlement; positive rates charge longs and pay shorts."""

    quantity = exact_decimal(signed_base_quantity, "signed_base_quantity")
    if quantity == 0:
        return Decimal("0")
    if event.settlement_price is None:
        raise ValueError("actual funding PnL requires the venue settlement price")
    notional = abs(quantity) * event.contract_multiplier * event.settlement_price
    direction_sign = Decimal("1") if quantity > 0 else Decimal("-1")
    return -direction_sign * event.rate * notional


def sum_discrete_funding(
    events: Iterable[FundingSettlement],
    *,
    signed_base_quantity: ExactDecimalInput,
    expected_venue: Venue | None = None,
) -> Decimal:
    total = Decimal("0")
    seen_event_ids: set[str] = set()
    for event in events:
        if event.event_id in seen_event_ids:
            raise ValueError("funding events cannot be counted more than once")
        seen_event_ids.add(event.event_id)
        if expected_venue is not None and event.venue is not expected_venue:
            raise ValueError("funding event venue does not match the requested position")
        total += discrete_funding_cashflow(event, signed_base_quantity=signed_base_quantity)
    return total


def evaluate_primary_episode(episode: MatchedBaseEpisode) -> EpisodePnl:
    """Evaluate matched-base Binance-long/Hyperliquid-short episode economics.

    Executable entry/exit price PnL already includes basis movement and spread
    costs.  No additional basis or combined-spread term is added here.
    """

    quantity = episode.base_quantity
    binance_multiplier = episode.binance_contract_multiplier
    hyperliquid_multiplier = episode.hyperliquid_contract_multiplier

    binance_price_pnl = quantity * binance_multiplier * (episode.binance_exit_price - episode.binance_entry_price)
    hyperliquid_price_pnl = (
        quantity * hyperliquid_multiplier * (episode.hyperliquid_entry_price - episode.hyperliquid_exit_price)
    )
    binance_funding = sum_discrete_funding(
        episode.binance_funding_events,
        signed_base_quantity=quantity,
        expected_venue=Venue.BINANCE,
    )
    hyperliquid_funding = sum_discrete_funding(
        episode.hyperliquid_funding_events,
        signed_base_quantity=-quantity,
        expected_venue=Venue.HYPERLIQUID,
    )

    binance_entry_commission = (
        quantity * binance_multiplier * episode.binance_entry_price * episode.commissions.binance_entry_rate
    )
    binance_exit_commission = (
        quantity * binance_multiplier * episode.binance_exit_price * episode.commissions.binance_exit_rate
    )
    hyperliquid_entry_commission = (
        quantity * hyperliquid_multiplier * episode.hyperliquid_entry_price * episode.commissions.hyperliquid_entry_rate
    )
    hyperliquid_exit_commission = (
        quantity * hyperliquid_multiplier * episode.hyperliquid_exit_price * episode.commissions.hyperliquid_exit_rate
    )
    total_commissions = (
        binance_entry_commission + binance_exit_commission + hyperliquid_entry_commission + hyperliquid_exit_commission
    )
    net_pnl = (
        binance_price_pnl
        + hyperliquid_price_pnl
        + binance_funding
        + hyperliquid_funding
        - total_commissions
        - episode.stablecoin_conversion_cost_usd
        - episode.collateral_opportunity_cost_usd
        - episode.repair_failure_cost_usd
    )
    reserved = episode.reserved_capital.total_usd
    return_on_reserved = net_pnl / reserved
    annualized = return_on_reserved * DAYS_PER_YEAR / episode.holding_period_days
    return EpisodePnl(
        binance_price_pnl_usd=binance_price_pnl,
        hyperliquid_price_pnl_usd=hyperliquid_price_pnl,
        binance_funding_pnl_usd=binance_funding,
        hyperliquid_funding_pnl_usd=hyperliquid_funding,
        binance_entry_commission_usd=binance_entry_commission,
        binance_exit_commission_usd=binance_exit_commission,
        hyperliquid_entry_commission_usd=hyperliquid_entry_commission,
        hyperliquid_exit_commission_usd=hyperliquid_exit_commission,
        total_commissions_usd=total_commissions,
        stablecoin_conversion_cost_usd=episode.stablecoin_conversion_cost_usd,
        collateral_opportunity_cost_usd=episode.collateral_opportunity_cost_usd,
        repair_failure_cost_usd=episode.repair_failure_cost_usd,
        net_pnl_usd=net_pnl,
        total_reserved_capital_usd=reserved,
        return_on_reserved_capital=return_on_reserved,
        simple_annualized_return=annualized,
    )


__all__ = [
    "daily_reporting_spread_bps",
    "discrete_funding_cashflow",
    "evaluate_primary_episode",
    "hourly_reporting_spread",
    "sum_discrete_funding",
]
