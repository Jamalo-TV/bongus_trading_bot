"""Point-in-time forecasts for discrete funding settlements.

The live scanner historically treated annualized funding as a continuous yield.
This module instead forecasts each exchange settlement as an uncertain cash
flow.  It is deliberately small and deterministic so it can serve as the
baseline that richer research models must beat out of sample.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import math
from statistics import NormalDist, median
from typing import Iterable, Literal

from bongus.market_data.funding_calendar import FundingCalendar


UTC = timezone.utc
FundingDirection = Literal["long_spot_short_perp", "short_spot_long_perp"]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class FundingObservation:
    symbol: str
    available_at: datetime
    annualized_rate: float
    premium_index: float | None = None
    basis_pct: float | None = None
    imbalance: float | None = None
    realized_volatility: float | None = None
    source_event_id: str = ""


@dataclass(frozen=True, slots=True)
class SettlementPaymentForecast:
    symbol: str
    settlement_time: datetime
    mean_rate: float
    standard_deviation: float
    lower_rate: float
    upper_rate: float
    favourable_sign_probability: float
    expected_payment_usd: float
    lower_payment_usd: float


@dataclass(frozen=True, slots=True)
class SettlementForecast:
    symbol: str
    decision_time: datetime
    direction: FundingDirection
    interval_hours: int
    sample_count: int
    latest_input_time: datetime | None
    payments: tuple[SettlementPaymentForecast, ...]
    valid: bool
    reason_codes: tuple[str, ...] = ()
    metadata: dict[str, float | int | str] = field(default_factory=dict)

    @property
    def expected_payment_usd(self) -> float:
        return sum(item.expected_payment_usd for item in self.payments)

    @property
    def lower_payment_usd(self) -> float:
        return sum(item.lower_payment_usd for item in self.payments)


@dataclass(frozen=True, slots=True)
class ForecastCalibration:
    sample_count: int
    mean_absolute_error: float
    sign_brier_score: float
    interval_coverage: float


class SettlementFundingModel:
    """Robust, causal baseline for prospective funding payments.

    Observations may be loaded out of order for research, but ``forecast``
    filters by ``available_at <= decision_time``.  This makes future data
    physically inaccessible to the prediction kernel and testable as an
    invariant.
    """

    def __init__(
        self,
        *,
        max_observations_per_symbol: int = 20_000,
        lookback_hours: float = 14 * 24,
        decay_per_settlement: float = 0.82,
        uncertainty_floor_rate: float = 1e-6,
        confidence_z: float = 1.6448536269514722,
    ) -> None:
        if max_observations_per_symbol < 2:
            raise ValueError("max_observations_per_symbol must be at least two")
        if lookback_hours <= 0.0:
            raise ValueError("lookback_hours must be positive")
        if not 0.0 <= decay_per_settlement <= 1.0:
            raise ValueError("decay_per_settlement must be between zero and one")
        if uncertainty_floor_rate <= 0.0:
            raise ValueError("uncertainty_floor_rate must be positive")
        self._history: dict[str, deque[FundingObservation]] = defaultdict(
            lambda: deque(maxlen=max_observations_per_symbol)
        )
        self.lookback_hours = float(lookback_hours)
        self.decay_per_settlement = float(decay_per_settlement)
        self.uncertainty_floor_rate = float(uncertainty_floor_rate)
        self.confidence_z = float(confidence_z)

    @staticmethod
    def _validate_observation(observation: FundingObservation) -> FundingObservation:
        symbol = observation.symbol.strip().upper()
        if not symbol:
            raise ValueError("funding observation requires a symbol")
        rate = float(observation.annualized_rate)
        if not math.isfinite(rate):
            raise ValueError("annualized_rate must be finite")
        for name in ("premium_index", "basis_pct", "imbalance", "realized_volatility"):
            value = getattr(observation, name)
            if value is not None and not math.isfinite(float(value)):
                raise ValueError(f"{name} must be finite when supplied")
        return FundingObservation(
            symbol=symbol,
            available_at=_as_utc(observation.available_at),
            annualized_rate=rate,
            premium_index=observation.premium_index,
            basis_pct=observation.basis_pct,
            imbalance=observation.imbalance,
            realized_volatility=observation.realized_volatility,
            source_event_id=observation.source_event_id,
        )

    def observe(self, observation: FundingObservation) -> None:
        self._history[observation.symbol.strip().upper()].append(
            self._validate_observation(observation)
        )

    def observe_many(self, observations: Iterable[FundingObservation]) -> None:
        for observation in observations:
            self.observe(observation)

    @staticmethod
    def _weighted_mean(values: list[float], decay: float = 0.94) -> float:
        weights = [decay ** (len(values) - index - 1) for index in range(len(values))]
        total_weight = sum(weights)
        return sum(value * weight for value, weight in zip(values, weights, strict=True)) / total_weight

    @staticmethod
    def _robust_standard_deviation(values: list[float], centre: float) -> float:
        if len(values) < 2:
            return 0.0
        absolute_deviations = [abs(value - centre) for value in values]
        mad_sigma = 1.4826 * median(absolute_deviations)
        sample_sigma = math.sqrt(
            sum((value - centre) ** 2 for value in values) / max(1, len(values) - 1)
        )
        # The larger estimate is conservative under a short/noisy sample.
        return max(mad_sigma, sample_sigma)

    def _causal_history(self, symbol: str, decision_time: datetime) -> list[FundingObservation]:
        cutoff = decision_time - timedelta(hours=self.lookback_hours)
        return sorted(
            (
                item
                for item in self._history.get(symbol.upper(), ())
                if cutoff <= item.available_at <= decision_time
            ),
            key=lambda item: (item.available_at, item.source_event_id),
        )

    def forecast(
        self,
        *,
        symbol: str,
        decision_time: datetime,
        horizon_hours: float,
        notional_usd: float,
        direction: FundingDirection,
        calendar: FundingCalendar,
    ) -> SettlementForecast:
        symbol = symbol.strip().upper()
        decision_time = _as_utc(decision_time)
        reasons: list[str] = []
        if not symbol:
            reasons.append("missing_symbol")
        if direction not in ("long_spot_short_perp", "short_spot_long_perp"):
            reasons.append("unknown_direction")
        if not math.isfinite(horizon_hours) or horizon_hours <= 0.0:
            reasons.append("invalid_horizon")
        if not math.isfinite(notional_usd) or notional_usd <= 0.0:
            reasons.append("invalid_notional")

        history = self._causal_history(symbol, decision_time) if symbol else []
        if not history:
            reasons.append("missing_point_in_time_history")
        interval_hours = calendar.interval_hours(symbol) if symbol else 8
        if reasons:
            return SettlementForecast(
                symbol=symbol,
                decision_time=decision_time,
                direction=direction,
                interval_hours=interval_hours,
                sample_count=len(history),
                latest_input_time=history[-1].available_at if history else None,
                payments=(),
                valid=False,
                reason_codes=tuple(dict.fromkeys(reasons)),
            )

        annual_periods = 365.0 * 24.0 / interval_hours
        raw_rates = [item.annualized_rate / annual_periods for item in history]
        latest = raw_rates[-1]
        weighted = self._weighted_mean(raw_rates)
        robust_centre = median(raw_rates)

        # Keep the baseline parsimonious: current exchange indication carries
        # most weight, history shrinks spikes, and optional live microstructure
        # has only a tightly bounded adjustment.
        micro_adjustment = 0.0
        latest_observation = history[-1]
        if latest_observation.imbalance is not None:
            micro_adjustment += max(-0.10, min(0.10, latest_observation.imbalance)) * abs(latest) * 0.05
        if latest_observation.basis_pct is not None:
            micro_adjustment += max(-0.001, min(0.001, latest_observation.basis_pct)) * 0.01
        next_mean = 0.60 * latest + 0.30 * weighted + 0.10 * robust_centre + micro_adjustment
        sigma = max(
            self.uncertainty_floor_rate,
            self._robust_standard_deviation(raw_rates, weighted),
            abs(next_mean) * (0.50 / math.sqrt(max(1, len(raw_rates)))),
        )

        horizon_end = decision_time + timedelta(hours=horizon_hours)
        settlements = calendar.settlements_between(symbol, decision_time, horizon_end)
        if not settlements:
            return SettlementForecast(
                symbol=symbol,
                decision_time=decision_time,
                direction=direction,
                interval_hours=interval_hours,
                sample_count=len(history),
                latest_input_time=history[-1].available_at,
                payments=(),
                valid=True,
                reason_codes=("no_settlement_in_horizon",),
                metadata={"annual_periods": annual_periods},
            )

        direction_sign = 1.0 if direction == "long_spot_short_perp" else -1.0
        payments: list[SettlementPaymentForecast] = []
        normal = NormalDist()
        for index, settlement_time in enumerate(settlements):
            persistence = self.decay_per_settlement**index
            mean_rate = robust_centre + (next_mean - robust_centre) * persistence
            mean_rate = calendar.clamp_rate(symbol, mean_rate)
            settlement_sigma = sigma * math.sqrt(index + 1)
            lower_rate = calendar.clamp_rate(
                symbol, mean_rate - self.confidence_z * settlement_sigma
            )
            upper_rate = calendar.clamp_rate(
                symbol, mean_rate + self.confidence_z * settlement_sigma
            )
            favourable_mean = direction_sign * mean_rate
            favourable_probability = normal.cdf(
                favourable_mean / max(settlement_sigma, self.uncertainty_floor_rate)
            )
            # Lower cash is taken in the strategy direction; for the reverse
            # trade the upper market rate is the adverse funding bound.
            favourable_lower_rate = (
                lower_rate if direction_sign > 0.0 else -upper_rate
            )
            payments.append(
                SettlementPaymentForecast(
                    symbol=symbol,
                    settlement_time=settlement_time,
                    mean_rate=mean_rate,
                    standard_deviation=settlement_sigma,
                    lower_rate=lower_rate,
                    upper_rate=upper_rate,
                    favourable_sign_probability=favourable_probability,
                    expected_payment_usd=direction_sign * mean_rate * notional_usd,
                    lower_payment_usd=favourable_lower_rate * notional_usd,
                )
            )

        return SettlementForecast(
            symbol=symbol,
            decision_time=decision_time,
            direction=direction,
            interval_hours=interval_hours,
            sample_count=len(history),
            latest_input_time=history[-1].available_at,
            payments=tuple(payments),
            valid=True,
            metadata={
                "annual_periods": annual_periods,
                "latest_raw_rate": latest,
                "robust_centre_raw_rate": robust_centre,
                "next_mean_raw_rate": next_mean,
            },
        )


def calibration_report(
    forecasts_and_actuals: Iterable[tuple[SettlementPaymentForecast, float]],
) -> ForecastCalibration:
    rows = list(forecasts_and_actuals)
    if not rows:
        return ForecastCalibration(0, math.nan, math.nan, math.nan)
    absolute_errors: list[float] = []
    brier: list[float] = []
    covered = 0
    for forecast, actual_rate in rows:
        actual = float(actual_rate)
        if not math.isfinite(actual):
            raise ValueError("actual funding rate must be finite")
        absolute_errors.append(abs(forecast.mean_rate - actual))
        actual_favourable = 1.0 if actual >= 0.0 else 0.0
        brier.append((forecast.favourable_sign_probability - actual_favourable) ** 2)
        covered += int(forecast.lower_rate <= actual <= forecast.upper_rate)
    return ForecastCalibration(
        sample_count=len(rows),
        mean_absolute_error=sum(absolute_errors) / len(rows),
        sign_brier_score=sum(brier) / len(rows),
        interval_coverage=covered / len(rows),
    )
