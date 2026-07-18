"""Per-symbol funding settlement calendars and exchange funding constraints."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import math
from typing import Any, Iterable


UTC = timezone.utc
DEFAULT_FUNDING_INTERVAL_HOURS = 8


def _utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(slots=True)
class FundingSchedule:
    symbol: str
    interval_hours: int = DEFAULT_FUNDING_INTERVAL_HOURS
    rate_cap: float | None = None
    rate_floor: float | None = None
    next_funding_time: datetime | None = None
    updated_at: datetime | None = None


class FundingCalendar:
    """Maintain point-in-time settlement metadata for each perpetual symbol.

    Binance's premium-index response supplies the next settlement timestamp,
    while funding-info supplies adjusted interval/cap/floor metadata.  The
    explicit next timestamp is authoritative; UTC interval alignment is only a
    conservative fallback for cold start and replay fixtures.
    """

    def __init__(self, default_interval_hours: int = DEFAULT_FUNDING_INTERVAL_HOURS) -> None:
        if default_interval_hours <= 0:
            raise ValueError("default funding interval must be positive")
        self._default_interval_hours = int(default_interval_hours)
        self._schedules: dict[str, FundingSchedule] = {}

    def _schedule(self, symbol: str) -> FundingSchedule:
        normalized = symbol.upper()
        if normalized not in self._schedules:
            self._schedules[normalized] = FundingSchedule(
                symbol=normalized,
                interval_hours=self._default_interval_hours,
            )
        return self._schedules[normalized]

    def update_funding_info(
        self,
        rows: Iterable[dict[str, Any]],
        *,
        observed_at: datetime | None = None,
    ) -> None:
        observed_at = _utc(observed_at)
        for row in rows:
            symbol = str(row.get("symbol") or "").upper()
            if not symbol:
                continue
            schedule = self._schedule(symbol)
            interval_raw = row.get("fundingIntervalHours")
            if interval_raw not in (None, ""):
                interval = int(interval_raw)
                if interval <= 0 or interval > 24:
                    raise ValueError(f"invalid funding interval for {symbol}: {interval}")
                schedule.interval_hours = interval
            cap_raw = row.get("adjustedFundingRateCap")
            floor_raw = row.get("adjustedFundingRateFloor")
            if cap_raw not in (None, ""):
                schedule.rate_cap = float(cap_raw)
            if floor_raw not in (None, ""):
                schedule.rate_floor = float(floor_raw)
            if (
                schedule.rate_cap is not None
                and schedule.rate_floor is not None
                and schedule.rate_floor > schedule.rate_cap
            ):
                raise ValueError(f"funding floor exceeds cap for {symbol}")
            schedule.updated_at = observed_at

    def update_premium_index(
        self,
        row: dict[str, Any],
        *,
        observed_at: datetime | None = None,
    ) -> None:
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            return
        observed_at = _utc(observed_at)
        schedule = self._schedule(symbol)
        next_time_raw = row.get("nextFundingTime")
        if next_time_raw not in (None, ""):
            next_timestamp = float(next_time_raw) / 1000.0
            next_time = datetime.fromtimestamp(next_timestamp, tz=UTC)
            # Reject obviously stale metadata but retain the previous valid
            # schedule rather than moving settlement time backwards.
            if next_time >= observed_at - timedelta(minutes=1):
                schedule.next_funding_time = next_time
        schedule.updated_at = observed_at

    def interval_hours(self, symbol: str) -> int:
        return self._schedule(symbol).interval_hours

    def next_settlement(
        self,
        symbol: str,
        *,
        after: datetime | None = None,
    ) -> datetime:
        after = _utc(after)
        schedule = self._schedule(symbol)
        interval = timedelta(hours=schedule.interval_hours)
        candidate = schedule.next_funding_time
        if candidate is not None:
            candidate = _utc(candidate)
            interval_seconds = interval.total_seconds()
            steps = math.floor((after - candidate).total_seconds() / interval_seconds) + 1
            candidate += interval * steps
            if candidate <= after:
                candidate += interval
            return candidate

        interval_seconds = schedule.interval_hours * 60 * 60
        epoch_seconds = int(after.timestamp())
        next_epoch = ((epoch_seconds // interval_seconds) + 1) * interval_seconds
        return datetime.fromtimestamp(next_epoch, tz=UTC)

    def previous_settlement(
        self,
        symbol: str,
        *,
        before: datetime | None = None,
    ) -> datetime:
        before = _utc(before)
        interval = timedelta(hours=self.interval_hours(symbol))
        return self.next_settlement(symbol, after=before) - interval

    def minutes_to_next(self, symbol: str, *, now: datetime | None = None) -> float:
        now = _utc(now)
        return max(0.0, (self.next_settlement(symbol, after=now) - now).total_seconds() / 60.0)

    def settlements_between(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        *,
        max_settlements: int = 1000,
    ) -> list[datetime]:
        """Return settlement instants for which ``start < instant <= end``."""

        start = _utc(start)
        end = _utc(end)
        if end <= start:
            return []
        if max_settlements <= 0:
            raise ValueError("max_settlements must be positive")
        interval = timedelta(hours=self.interval_hours(symbol))
        current = self.next_settlement(symbol, after=start)
        result: list[datetime] = []
        while current <= end:
            result.append(current)
            if len(result) >= max_settlements:
                raise ValueError("settlement range exceeds safety limit")
            current += interval
        return result

    def clamp_rate(self, symbol: str, rate: float) -> float:
        schedule = self._schedule(symbol)
        if schedule.rate_cap is not None:
            rate = min(rate, schedule.rate_cap)
        if schedule.rate_floor is not None:
            rate = max(rate, schedule.rate_floor)
        return rate

    def snapshot(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for symbol, schedule in sorted(self._schedules.items()):
            payload = asdict(schedule)
            for key in ("next_funding_time", "updated_at"):
                value = payload[key]
                payload[key] = value.isoformat() if value is not None else None
            result[symbol] = payload
        return result
