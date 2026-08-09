"""Explicit economic units for funding-arbitrage decisions.

Funding has two deliberately separate representations:

* :class:`RawSettlementRate` is the exchange rate paid once at a discrete
  settlement instant.  Cash flow is ``raw_rate * liable_leg_notional``.
* :class:`AnnualizedReportingRate` is a reporting-only convention and is
  always ``raw_rate * 1095`` (three settlements per day times 365 days).

The reporting multiplier never changes with a symbol's current funding
interval.  Actual cash-flow timing belongs to the authoritative settlement
calendar, not to the reporting unit conversion.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable


FUNDING_REPORTING_PERIODS_PER_YEAR = 3 * 365


def _finite(value: float, name: str) -> float:
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError(f"{name} must be finite")
    return numeric


def _nonnegative(value: float, name: str) -> float:
    numeric = _finite(value, name)
    if numeric < 0.0:
        raise ValueError(f"{name} must be non-negative")
    return numeric


@dataclass(frozen=True, slots=True)
class RawSettlementRate:
    """Signed rate for exactly one exchange funding settlement."""

    value: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _finite(self.value, "raw settlement rate"))

    @property
    def reporting_annualized(self) -> "AnnualizedReportingRate":
        return AnnualizedReportingRate(
            self.value * FUNDING_REPORTING_PERIODS_PER_YEAR
        )

    @property
    def annualized_reporting_rate(self) -> "AnnualizedReportingRate":
        return self.reporting_annualized

    def cashflow_usd(
        self,
        liable_leg: "LegNotionalUsd | float",
        *,
        direction_sign: float = 1.0,
        eligibility_probability: float = 1.0,
    ) -> float:
        notional = (
            liable_leg.value
            if isinstance(liable_leg, LegNotionalUsd)
            else _nonnegative(liable_leg, "liable leg notional")
        )
        sign = _finite(direction_sign, "direction sign")
        probability = _finite(
            eligibility_probability, "eligibility probability"
        )
        if not 0.0 <= probability <= 1.0:
            raise ValueError("eligibility probability must be between zero and one")
        return self.value * sign * probability * notional


@dataclass(frozen=True, slots=True)
class AnnualizedReportingRate:
    """Signed reporting rate using the fixed raw-rate-times-1095 convention."""

    value: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "value", _finite(self.value, "annualized reporting rate")
        )

    @classmethod
    def from_raw(
        cls, raw_rate: RawSettlementRate | float
    ) -> "AnnualizedReportingRate":
        raw = raw_rate.value if isinstance(raw_rate, RawSettlementRate) else float(raw_rate)
        return cls(_finite(raw, "raw settlement rate") * FUNDING_REPORTING_PERIODS_PER_YEAR)

    @property
    def raw_settlement(self) -> RawSettlementRate:
        return RawSettlementRate(self.value / FUNDING_REPORTING_PERIODS_PER_YEAR)

    @property
    def raw_settlement_rate(self) -> RawSettlementRate:
        return self.raw_settlement


@dataclass(frozen=True, slots=True)
class LegNotionalUsd:
    """Absolute USD notional of one spot or perpetual leg."""

    value: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _nonnegative(self.value, "leg notional"))

    @property
    def amount_usd(self) -> float:
        return self.value

    @property
    def usd(self) -> float:
        return self.value


@dataclass(frozen=True, slots=True)
class PairGrossNotionalUsd:
    """Sum of the absolute USD notionals of every leg in a pair."""

    value: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "value", _nonnegative(self.value, "pair gross notional")
        )

    @classmethod
    def from_legs(
        cls, legs: Iterable[LegNotionalUsd | float]
    ) -> "PairGrossNotionalUsd":
        total = 0.0
        for leg in legs:
            value = leg.value if isinstance(leg, LegNotionalUsd) else float(leg)
            total += abs(_finite(value, "leg notional"))
        return cls(total)

    @classmethod
    def from_matched_legs(
        cls,
        spot_leg: LegNotionalUsd | float,
        perp_leg: LegNotionalUsd | float,
    ) -> "PairGrossNotionalUsd":
        return cls.from_legs((spot_leg, perp_leg))

    @property
    def amount_usd(self) -> float:
        return self.value

    @property
    def usd(self) -> float:
        return self.value


@dataclass(frozen=True, slots=True)
class CollateralUsd:
    """USD collateral committed or available to the paired position."""

    value: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _nonnegative(self.value, "collateral"))

    @property
    def amount_usd(self) -> float:
        return self.value

    @property
    def usd(self) -> float:
        return self.value


@dataclass(frozen=True, slots=True)
class MarginExposureUsd:
    """Absolute USD exposure supported by derivatives margin."""

    value: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "value", _nonnegative(self.value, "margin exposure")
        )

    @property
    def amount_usd(self) -> float:
        return self.value

    @property
    def usd(self) -> float:
        return self.value


@dataclass(frozen=True, slots=True)
class EconomicUnitSnapshot:
    """A checked unit bundle for one matched spot/perpetual position."""

    spot_leg: LegNotionalUsd
    perp_leg: LegNotionalUsd
    pair_gross: PairGrossNotionalUsd
    collateral: CollateralUsd
    margin_exposure: MarginExposureUsd

    def __post_init__(self) -> None:
        exact_pair_gross = self.spot_leg.value + self.perp_leg.value
        if not math.isclose(
            self.pair_gross.value,
            exact_pair_gross,
            rel_tol=1e-12,
            abs_tol=1e-9,
        ):
            raise ValueError("pair gross must equal both absolute leg notionals")

    @classmethod
    def matched(
        cls,
        *,
        leg_notional_usd: float,
        collateral_usd: float,
        margin_exposure_usd: float,
    ) -> "EconomicUnitSnapshot":
        spot = LegNotionalUsd(leg_notional_usd)
        perp = LegNotionalUsd(leg_notional_usd)
        return cls(
            spot_leg=spot,
            perp_leg=perp,
            pair_gross=PairGrossNotionalUsd.from_matched_legs(spot, perp),
            collateral=CollateralUsd(collateral_usd),
            margin_exposure=MarginExposureUsd(margin_exposure_usd),
        )


def annualized_reporting_rate(raw_rate: RawSettlementRate | float) -> float:
    """Return the fixed ``raw settlement rate * 1095`` reporting value."""

    return AnnualizedReportingRate.from_raw(raw_rate).value


def raw_settlement_rate(
    reporting_rate: AnnualizedReportingRate | float,
) -> float:
    """Invert the reporting conversion without consulting an interval."""

    annualized = (
        reporting_rate.value
        if isinstance(reporting_rate, AnnualizedReportingRate)
        else _finite(reporting_rate, "annualized reporting rate")
    )
    return annualized / FUNDING_REPORTING_PERIODS_PER_YEAR


# Compatibility aliases retain the unit name while accommodating both common
# capitalizations in call sites and serialized schema documentation.
LegNotionalUSD = LegNotionalUsd
PairGrossNotionalUSD = PairGrossNotionalUsd
CollateralUSD = CollateralUsd
MarginExposureUSD = MarginExposureUsd
ReportingAnnualizedRate = AnnualizedReportingRate
FundingSettlementRate = RawSettlementRate
ReportingRate1095 = AnnualizedReportingRate
LegNotional = LegNotionalUsd
PairGross = PairGrossNotionalUsd
Collateral = CollateralUsd
MarginExposure = MarginExposureUsd


__all__ = [
    "FUNDING_REPORTING_PERIODS_PER_YEAR",
    "AnnualizedReportingRate",
    "CollateralUSD",
    "CollateralUsd",
    "Collateral",
    "EconomicUnitSnapshot",
    "FundingSettlementRate",
    "LegNotional",
    "LegNotionalUSD",
    "LegNotionalUsd",
    "MarginExposureUSD",
    "MarginExposureUsd",
    "MarginExposure",
    "PairGross",
    "PairGrossNotionalUSD",
    "PairGrossNotionalUsd",
    "RawSettlementRate",
    "ReportingAnnualizedRate",
    "ReportingRate1095",
    "annualized_reporting_rate",
    "raw_settlement_rate",
]
