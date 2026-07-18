"""Read-only cross-venue opportunity comparison with explicit frictions."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from bongus.exchanges.base import AccountRef, FundingQuote, Venue, decimal_value


@dataclass(frozen=True, slots=True)
class VenueFundingLeg:
    account: AccountRef
    quote: FundingQuote
    executable_capacity_usd: Decimal
    round_trip_cost_bps: Decimal
    borrow_cost_bps: Decimal = Decimal("0")
    transfer_cost_bps: Decimal = Decimal("0")
    counterparty_risk_bps: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class CrossVenueOpportunity:
    symbol: str
    long_venue: Venue
    short_venue: Venue
    capacity_usd: Decimal
    gross_rate_spread: Decimal
    net_edge_bps: Decimal
    eligible: bool
    reason_codes: tuple[str, ...]


def compare_cross_venue_funding(
    legs: Iterable[VenueFundingLeg],
    *,
    minimum_net_edge_bps: Decimal | str | float,
) -> list[CrossVenueOpportunity]:
    threshold = decimal_value(minimum_net_edge_bps, "minimum_net_edge_bps")
    grouped: dict[str, list[VenueFundingLeg]] = {}
    for leg in legs:
        # Account/environment form part of every leg; accidentally netting two
        # venues into one anonymous balance is impossible.
        _ = leg.account.key
        if leg.account.venue is not leg.quote.venue:
            raise ValueError("funding quote and account venue must match")
        grouped.setdefault(leg.quote.symbol.upper(), []).append(leg)
    result: list[CrossVenueOpportunity] = []
    for symbol, symbol_legs in grouped.items():
        for long_leg in symbol_legs:
            for short_leg in symbol_legs:
                if long_leg.quote.venue is short_leg.quote.venue:
                    continue
                # Long perp pays positive funding while short perp receives it;
                # spread is therefore short rate minus long rate.
                gross_spread = short_leg.quote.raw_rate - long_leg.quote.raw_rate
                common_interval = max(
                    long_leg.quote.interval_hours, short_leg.quote.interval_hours
                )
                normalized_spread = gross_spread * Decimal(24) / Decimal(common_interval)
                total_cost_bps = sum(
                    (
                        long_leg.round_trip_cost_bps,
                        short_leg.round_trip_cost_bps,
                        long_leg.borrow_cost_bps,
                        short_leg.borrow_cost_bps,
                        long_leg.transfer_cost_bps,
                        short_leg.transfer_cost_bps,
                        long_leg.counterparty_risk_bps,
                        short_leg.counterparty_risk_bps,
                    ),
                    Decimal("0"),
                )
                net_bps = normalized_spread * Decimal(10_000) - total_cost_bps
                capacity = min(
                    long_leg.executable_capacity_usd,
                    short_leg.executable_capacity_usd,
                )
                reasons: list[str] = []
                if capacity <= 0:
                    reasons.append("no_cross_venue_capacity")
                if net_bps < threshold:
                    reasons.append("net_edge_below_threshold")
                result.append(
                    CrossVenueOpportunity(
                        symbol,
                        long_leg.quote.venue,
                        short_leg.quote.venue,
                        capacity,
                        gross_spread,
                        net_bps,
                        not reasons,
                        tuple(reasons),
                    )
                )
    return sorted(
        result,
        key=lambda item: (item.net_edge_bps, item.symbol, item.long_venue.value),
        reverse=True,
    )
