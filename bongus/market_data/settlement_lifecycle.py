"""Exact funding-settlement eligibility and exchange-credit reconciliation.

Forecasts estimate prospective payments; this module answers the separate,
pass/fail lifecycle question: was a filled hedge open at the exact exchange
settlement, what point-in-time rate was known, and did an authoritative
exchange statement actually credit/debit the account?  Expected cash is never
promoted to realized cash when the statement is absent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal


UTC = timezone.utc
FundingDirection = Literal["long_spot_short_perp", "short_spot_long_perp"]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _decimal(value: Decimal | str | float, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


@dataclass(frozen=True, slots=True)
class PositionEligibilityWindow:
    cycle_id: str
    symbol: str
    direction: FundingDirection
    funding_notional_usd: Decimal | str | float
    opened_at: datetime
    closed_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.cycle_id.strip() or not self.symbol.strip():
            raise ValueError("cycle_id and symbol are required")
        if self.direction not in (
            "long_spot_short_perp",
            "short_spot_long_perp",
        ):
            raise ValueError("unsupported funding direction")
        if _decimal(self.funding_notional_usd, "funding_notional_usd") <= 0:
            raise ValueError("funding_notional_usd must be positive")
        opened_at = _utc(self.opened_at)
        if self.closed_at is not None and _utc(self.closed_at) < opened_at:
            raise ValueError("closed_at precedes opened_at")

    def eligible_at(self, settlement_time: datetime) -> bool:
        """Use the conservative interval ``opened_at < t <= closed_at``.

        An entry fill timestamp equal to the settlement is not assumed to have
        participated.  A close at the settlement remains eligible because the
        position was held immediately before that terminal fill timestamp.
        Exchange statement evidence remains final authority in either case.
        """

        settlement = _utc(settlement_time)
        return _utc(self.opened_at) < settlement and (
            self.closed_at is None or settlement <= _utc(self.closed_at)
        )


@dataclass(frozen=True, slots=True)
class SettlementRateUpdate:
    symbol: str
    settlement_time: datetime
    available_at: datetime
    raw_rate: Decimal | str | float
    source_event_id: str

    def __post_init__(self) -> None:
        if not self.symbol.strip() or not self.source_event_id.strip():
            raise ValueError("symbol and source_event_id are required")
        _decimal(self.raw_rate, "raw_rate")


@dataclass(frozen=True, slots=True)
class SettlementLifecycleResult:
    symbol: str
    settlement_time: datetime
    eligible_cycle_ids: tuple[str, ...]
    eligible_notional_usd: Decimal
    applied_rate: Decimal | None
    rate_available_at: datetime | None
    expected_cash_usd: Decimal
    credited_cash_usd: Decimal
    exchange_event_id: str
    reconciled: bool
    reason_codes: tuple[str, ...]

    @property
    def eligible(self) -> bool:
        return bool(self.eligible_cycle_ids)


class FundingSettlementLifecycle:
    """Deterministic point-in-time settlement lifecycle oracle."""

    def __init__(self) -> None:
        self._windows: dict[str, PositionEligibilityWindow] = {}
        self._rates: dict[str, SettlementRateUpdate] = {}

    def add_window(self, window: PositionEligibilityWindow) -> None:
        prior = self._windows.get(window.cycle_id)
        if prior is not None and prior != window:
            raise ValueError(f"cycle window identity collision: {window.cycle_id}")
        self._windows[window.cycle_id] = window

    def observe_rate(self, update: SettlementRateUpdate) -> None:
        prior = self._rates.get(update.source_event_id)
        if prior is not None and prior != update:
            raise ValueError(
                f"settlement-rate identity collision: {update.source_event_id}"
            )
        self._rates[update.source_event_id] = update

    def evaluate(
        self,
        *,
        symbol: str,
        settlement_time: datetime,
        exchange_amount_usd: Decimal | str | float | None = None,
        exchange_event_id: str = "",
        cash_tolerance_usd: Decimal | str | float = Decimal("0.01"),
    ) -> SettlementLifecycleResult:
        normalized_symbol = symbol.strip().upper()
        if not normalized_symbol:
            raise ValueError("symbol is required")
        settlement = _utc(settlement_time)
        tolerance = _decimal(cash_tolerance_usd, "cash_tolerance_usd")
        if tolerance < 0:
            raise ValueError("cash_tolerance_usd must be non-negative")

        eligible_windows = sorted(
            (
                window
                for window in self._windows.values()
                if window.symbol.upper() == normalized_symbol
                and window.eligible_at(settlement)
            ),
            key=lambda window: window.cycle_id,
        )
        updates = sorted(
            (
                update
                for update in self._rates.values()
                if update.symbol.upper() == normalized_symbol
                and _utc(update.settlement_time) == settlement
                and _utc(update.available_at) <= settlement
            ),
            key=lambda update: (_utc(update.available_at), update.source_event_id),
        )

        reasons: list[str] = []
        if not eligible_windows:
            reasons.append("ineligible_at_settlement")
        if not updates:
            reasons.append("missing_point_in_time_rate")

        applied_rate = _decimal(updates[-1].raw_rate, "raw_rate") if updates else None
        rate_available_at = _utc(updates[-1].available_at) if updates else None
        if len(updates) >= 2:
            first_rate = _decimal(updates[0].raw_rate, "raw_rate")
            latest_rate = _decimal(updates[-1].raw_rate, "raw_rate")
            if first_rate * latest_rate < 0:
                reasons.append("rate_reversed_before_settlement")

        expected = Decimal("0")
        eligible_notional = Decimal("0")
        if applied_rate is not None:
            for window in eligible_windows:
                notional = _decimal(
                    window.funding_notional_usd, "funding_notional_usd"
                )
                eligible_notional += notional
                direction_sign = (
                    Decimal("1")
                    if window.direction == "long_spot_short_perp"
                    else Decimal("-1")
                )
                expected += direction_sign * applied_rate * notional

        statement_present = exchange_amount_usd is not None or bool(
            exchange_event_id.strip()
        )
        if statement_present and (
            exchange_amount_usd is None or not exchange_event_id.strip()
        ):
            raise ValueError(
                "exchange amount and stable exchange_event_id must be supplied together"
            )
        credited = (
            _decimal(exchange_amount_usd, "exchange_amount_usd")
            if exchange_amount_usd is not None
            else Decimal("0")
        )

        reconciled = True
        if eligible_windows and applied_rate is not None:
            if not statement_present:
                reasons.append("missing_exchange_funding_statement")
                reconciled = False
            elif abs(credited - expected) > tolerance:
                reasons.append("exchange_amount_mismatch")
                reconciled = False
        elif statement_present and abs(credited) > tolerance:
            reasons.append("unexpected_exchange_funding_statement")
            reconciled = False

        return SettlementLifecycleResult(
            symbol=normalized_symbol,
            settlement_time=settlement,
            eligible_cycle_ids=tuple(
                window.cycle_id for window in eligible_windows
            ),
            eligible_notional_usd=eligible_notional,
            applied_rate=applied_rate,
            rate_available_at=rate_available_at,
            expected_cash_usd=expected,
            credited_cash_usd=credited,
            exchange_event_id=exchange_event_id.strip(),
            reconciled=reconciled,
            reason_codes=tuple(dict.fromkeys(reasons)),
        )
