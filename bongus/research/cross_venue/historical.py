"""Deterministic B0 historical-feasibility and early-abandonment screen."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Final, Literal, cast

from bongus.research.cross_venue.evaluation import PREDECLARED_UNIVERSE, PREREGISTRATION_PATH
from bongus.research.cross_venue.normalization import mapping_for_asset
from bongus.research.cross_venue.schema import (
    CanonicalAsset,
    Venue,
    decimal_text,
    epoch_nanoseconds,
    exact_decimal,
    nonnegative_decimal,
    positive_decimal,
)
from bongus.research.cross_venue.storage import canonical_json_bytes

HISTORICAL_ARTIFACT_SCHEMA: Final[str] = "bongus-cross-venue-b0-history-v1"
HISTORICAL_REPORT_SCHEMA: Final[str] = "bongus-cross-venue-b0-report-v1"
NANOSECONDS_PER_HOUR: Final[int] = 3_600_000_000_000
NANOSECONDS_PER_DAY: Final[int] = 86_400_000_000_000
DAYS_PER_YEAR: Final[Decimal] = Decimal("365")
_SHA256_CHARACTERS: Final[frozenset[str]] = frozenset("0123456789abcdef")

HistoricalVerdict = Literal["CONTINUE", "ABANDON", "INSUFFICIENT_EVIDENCE"]
OracleDirection = Literal["binance_long_hyperliquid_short", "binance_short_hyperliquid_long"]


class HistoricalArtifactError(ValueError):
    """An immutable B0 input or output violates its exact contract."""


def _reject_nonfinite_json(value: str) -> object:
    raise HistoricalArtifactError(f"non-finite JSON value is forbidden: {value}")


def _reject_json_float(value: str) -> object:
    raise HistoricalArtifactError(f"binary JSON numbers are forbidden; use exact strings: {value}")


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise HistoricalArtifactError(f"{field_name} must be an object")
    return cast(Mapping[str, object], value)


def _sequence(value: object, field_name: str) -> Sequence[object]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise HistoricalArtifactError(f"{field_name} must be an array")
    return value


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise HistoricalArtifactError(f"{field_name} must be canonical non-empty text")
    return value


def _sha256(value: object, field_name: str) -> str:
    normalized = _text(value, field_name)
    if len(normalized) != 64 or any(character not in _SHA256_CHARACTERS for character in normalized):
        raise HistoricalArtifactError(f"{field_name} must be lowercase SHA-256 hex")
    return normalized


def _exact_string(value: object, field_name: str) -> Decimal:
    if not isinstance(value, str):
        raise HistoricalArtifactError(f"{field_name} must be an exact decimal string")
    decimal = exact_decimal(value, field_name)
    if decimal_text(decimal) != value:
        raise HistoricalArtifactError(f"{field_name} must use canonical non-exponent decimal text")
    return decimal


def _positive_string(value: object, field_name: str) -> Decimal:
    decimal = _exact_string(value, field_name)
    if decimal <= 0:
        raise HistoricalArtifactError(f"{field_name} must be positive")
    return decimal


def _nanosecond_string(value: object, field_name: str) -> int:
    if not isinstance(value, str):
        raise HistoricalArtifactError(f"{field_name} must be an exact epoch-nanosecond string")
    return epoch_nanoseconds(value, field_name)


@dataclass(frozen=True, slots=True)
class HistoricalScreenPolicy:
    hold_days: int = 30
    minimum_common_history_days: int = 90
    minimum_complete_windows_per_asset: int = 3
    minimum_total_complete_windows: int = 15
    minimum_coverage_fraction: Decimal = Decimal("0.99")
    rarely_covers_fraction: Decimal = Decimal("0.25")
    target_notional_per_leg_usd: Decimal = Decimal("1250")
    binance_entry_taker_rate: Decimal = Decimal("0.0005")
    binance_exit_taker_rate: Decimal = Decimal("0.0005")
    hyperliquid_entry_taker_rate: Decimal = Decimal("0.0005")
    hyperliquid_exit_taker_rate: Decimal = Decimal("0.0005")
    stablecoin_conversion_cost_rate: Decimal = Decimal("0.001")
    collateral_opportunity_cost_annual_rate: Decimal = Decimal("0.05")
    total_reserved_capital_usd: Decimal = Decimal("2000")
    repair_failure_cost_rate: Decimal = Decimal("0.0005")

    def __post_init__(self) -> None:
        for field_name in (
            "hold_days",
            "minimum_common_history_days",
            "minimum_complete_windows_per_asset",
            "minimum_total_complete_windows",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")
        if self.minimum_total_complete_windows < (
            self.minimum_complete_windows_per_asset * len(PREDECLARED_UNIVERSE)
        ):
            raise ValueError("minimum total windows cannot undercut the per-asset fixed-universe gate")
        for field_name in (
            "minimum_coverage_fraction",
            "rarely_covers_fraction",
            "binance_entry_taker_rate",
            "binance_exit_taker_rate",
            "hyperliquid_entry_taker_rate",
            "hyperliquid_exit_taker_rate",
            "stablecoin_conversion_cost_rate",
            "collateral_opportunity_cost_annual_rate",
            "repair_failure_cost_rate",
        ):
            value = nonnegative_decimal(getattr(self, field_name), field_name)
            object.__setattr__(self, field_name, value)
        for field_name in ("minimum_coverage_fraction", "rarely_covers_fraction"):
            if getattr(self, field_name) > 1:
                raise ValueError(f"{field_name} cannot exceed one")
        for field_name in ("target_notional_per_leg_usd", "total_reserved_capital_usd"):
            object.__setattr__(
                self,
                field_name,
                positive_decimal(getattr(self, field_name), field_name),
            )

    @property
    def four_commission_rate(self) -> Decimal:
        return (
            self.binance_entry_taker_rate
            + self.binance_exit_taker_rate
            + self.hyperliquid_entry_taker_rate
            + self.hyperliquid_exit_taker_rate
        )

    @property
    def fixed_cost_rate(self) -> Decimal:
        return self.four_commission_rate + self.stablecoin_conversion_cost_rate + self.repair_failure_cost_rate

    @property
    def collateral_cost_rate_per_day(self) -> Decimal:
        return (
            self.collateral_opportunity_cost_annual_rate
            * self.total_reserved_capital_usd
            / self.target_notional_per_leg_usd
            / DAYS_PER_YEAR
        )

    def total_cost_rate(self, holding_days: int | Decimal) -> Decimal:
        days = positive_decimal(holding_days, "holding_days")
        return self.fixed_cost_rate + self.collateral_cost_rate_per_day * days

    def as_wire(self) -> Mapping[str, object]:
        return {
            "artifact_schema": HISTORICAL_ARTIFACT_SCHEMA,
            "report_schema": HISTORICAL_REPORT_SCHEMA,
            "universe": [asset.value for asset in PREDECLARED_UNIVERSE],
            "primary_direction": "binance_long_hyperliquid_short",
            "oracle_direction_rule": "best_of_primary_and_exact_reverse_per_window_after_costs",
            "holding_period_days": self.hold_days,
            "window_rule": "non_overlapping_from_common_interval_coverage_start",
            "minimum_common_history_days": self.minimum_common_history_days,
            "minimum_complete_windows_per_asset": self.minimum_complete_windows_per_asset,
            "minimum_total_complete_windows": self.minimum_total_complete_windows,
            "minimum_interval_time_coverage": decimal_text(self.minimum_coverage_fraction),
            "rarely_covers_at_or_below_fraction": decimal_text(self.rarely_covers_fraction),
            "event_quality_flags_allowed": 0,
            "duplicate_events_allowed": 0,
            "costs": {
                "target_notional_per_leg_usd": decimal_text(self.target_notional_per_leg_usd),
                "binance_entry_taker_rate": decimal_text(self.binance_entry_taker_rate),
                "binance_exit_taker_rate": decimal_text(self.binance_exit_taker_rate),
                "hyperliquid_entry_taker_rate": decimal_text(self.hyperliquid_entry_taker_rate),
                "hyperliquid_exit_taker_rate": decimal_text(self.hyperliquid_exit_taker_rate),
                "stablecoin_conversion_cost_rate": decimal_text(self.stablecoin_conversion_cost_rate),
                "collateral_opportunity_cost_annual_rate": decimal_text(
                    self.collateral_opportunity_cost_annual_rate
                ),
                "total_reserved_capital_usd": decimal_text(self.total_reserved_capital_usd),
                "repair_failure_cost_rate": decimal_text(self.repair_failure_cost_rate),
            },
            "optimistic_assumptions": {
                "favorable_basis_pnl_rate": "0",
                "slippage_rate": "0",
                "liquidity": "perfect",
            },
            "verdicts": ["CONTINUE", "ABANDON", "INSUFFICIENT_EVIDENCE"],
            "grant_live_authority": False,
        }


@dataclass(frozen=True, slots=True)
class HistoricalFundingEvent:
    event_id: str
    venue: Venue
    canonical_asset: CanonicalAsset
    venue_symbol: str
    contract_id: str
    settlement_time_ns: int
    available_time_ns: int
    funding_rate: Decimal
    funding_interval_hours: Decimal
    source_payload_sha256: str
    quality_flags: tuple[str, ...]

    @property
    def interval_nanoseconds(self) -> int:
        value = self.funding_interval_hours * NANOSECONDS_PER_HOUR
        integral = int(value)
        if value != integral:
            raise HistoricalArtifactError("funding interval must resolve to exact nanoseconds")
        return integral

    def identity(self) -> tuple[Venue, CanonicalAsset, int]:
        return self.venue, self.canonical_asset, self.settlement_time_ns

    def as_wire(self) -> Mapping[str, object]:
        price_kind = "mark" if self.venue is Venue.BINANCE else "oracle"
        return {
            "event_id": self.event_id,
            "venue": self.venue.value,
            "canonical_asset": self.canonical_asset.value,
            "venue_symbol": self.venue_symbol,
            "contract_id": self.contract_id,
            "settlement_time_ns": str(self.settlement_time_ns),
            "available_time_ns": str(self.available_time_ns),
            "funding_rate": decimal_text(self.funding_rate),
            "funding_interval_hours": decimal_text(self.funding_interval_hours),
            "finalized": True,
            "price_kind": price_kind,
            "source_payload_sha256": self.source_payload_sha256,
            "quality_flags": list(self.quality_flags),
        }


@dataclass(frozen=True, slots=True)
class HistoricalFundingArtifact:
    artifact_id: str
    source_manifest_sha256: Mapping[str, str]
    events: tuple[HistoricalFundingEvent, ...]
    content_sha256: str
    file_sha256: str


def load_historical_screen_policy(
    path: str | Path = PREREGISTRATION_PATH,
) -> tuple[HistoricalScreenPolicy, str]:
    preregistration_path = Path(path).resolve()
    raw = preregistration_path.read_bytes()
    try:
        payload = json.loads(
            raw,
            parse_float=_reject_json_float,
            parse_int=int,
            parse_constant=_reject_nonfinite_json,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HistoricalArtifactError("preregistration is not valid UTF-8 JSON") from exc
    root = _mapping(payload, "preregistration")
    if root.get("protocol_id") != "binance-hyperliquid-v1" or root.get("status") != "frozen_before_forward_oos":
        raise HistoricalArtifactError("historical screen requires the frozen v1 preregistration")
    policy = HistoricalScreenPolicy()
    if root.get("historical_feasibility") != policy.as_wire():
        raise HistoricalArtifactError("code and preregistered B0 historical contract do not match")
    preregistration_sha256 = hashlib.sha256(canonical_json_bytes(root)).hexdigest()
    return policy, preregistration_sha256


def seal_historical_artifact(content: Mapping[str, object]) -> Mapping[str, object]:
    """Create the canonical hash envelope for an already-offline source artifact."""

    normalized = cast(Mapping[str, object], json.loads(canonical_json_bytes(content)))
    return {
        "schema_version": HISTORICAL_ARTIFACT_SCHEMA,
        "content": normalized,
        "content_sha256": hashlib.sha256(canonical_json_bytes(normalized)).hexdigest(),
    }


def _parse_event(value: object, index: int) -> HistoricalFundingEvent:
    row = _mapping(value, f"events[{index}]")
    required_keys = {
        "event_id",
        "venue",
        "canonical_asset",
        "venue_symbol",
        "contract_id",
        "settlement_time_ns",
        "available_time_ns",
        "funding_rate",
        "funding_interval_hours",
        "finalized",
        "price_kind",
        "source_payload_sha256",
        "quality_flags",
    }
    if set(row) != required_keys:
        raise HistoricalArtifactError(f"events[{index}] does not have the exact finalized-funding schema")
    try:
        venue = Venue(_text(row.get("venue"), f"events[{index}].venue"))
        asset = CanonicalAsset(_text(row.get("canonical_asset"), f"events[{index}].canonical_asset"))
    except ValueError as exc:
        raise HistoricalArtifactError(f"events[{index}] is outside the fixed venues/universe") from exc
    mapping = mapping_for_asset(asset)
    expected_symbol = mapping.binance_symbol if venue is Venue.BINANCE else mapping.hyperliquid_coin
    expected_contract = mapping.binance_contract_id if venue is Venue.BINANCE else mapping.hyperliquid_contract_id
    if row.get("venue_symbol") != expected_symbol or row.get("contract_id") != expected_contract:
        raise HistoricalArtifactError(f"events[{index}] does not use the fixed explicit instrument mapping")
    expected_price_kind = "mark" if venue is Venue.BINANCE else "oracle"
    if row.get("price_kind") != expected_price_kind:
        raise HistoricalArtifactError(f"events[{index}] has the wrong venue funding-price kind")
    if row.get("finalized") is not True:
        raise HistoricalArtifactError(f"events[{index}] is not finalized funding history")
    settlement = _nanosecond_string(row.get("settlement_time_ns"), f"events[{index}].settlement_time_ns")
    available = _nanosecond_string(row.get("available_time_ns"), f"events[{index}].available_time_ns")
    if available < settlement:
        raise HistoricalArtifactError(f"events[{index}] is available before settlement")
    interval = _positive_string(row.get("funding_interval_hours"), f"events[{index}].funding_interval_hours")
    allowed_intervals = {Decimal("1")} if venue is Venue.HYPERLIQUID else {
        Decimal("1"),
        Decimal("2"),
        Decimal("4"),
        Decimal("8"),
    }
    if interval not in allowed_intervals:
        raise HistoricalArtifactError(f"events[{index}] has an unsupported actual funding interval")
    flags = tuple(
        _text(flag, f"events[{index}].quality_flags")
        for flag in _sequence(row.get("quality_flags"), f"events[{index}].quality_flags")
    )
    event = HistoricalFundingEvent(
        event_id=_text(row.get("event_id"), f"events[{index}].event_id"),
        venue=venue,
        canonical_asset=asset,
        venue_symbol=expected_symbol,
        contract_id=expected_contract,
        settlement_time_ns=settlement,
        available_time_ns=available,
        funding_rate=_exact_string(row.get("funding_rate"), f"events[{index}].funding_rate"),
        funding_interval_hours=interval,
        source_payload_sha256=_sha256(
            row.get("source_payload_sha256"), f"events[{index}].source_payload_sha256"
        ),
        quality_flags=flags,
    )
    if event.settlement_time_ns % event.interval_nanoseconds:
        raise HistoricalArtifactError(f"events[{index}] is not aligned to its declared actual interval")
    return event


def load_historical_artifact(path: str | Path) -> HistoricalFundingArtifact:
    artifact_path = Path(path).resolve()
    if artifact_path.is_symlink() or not artifact_path.is_file():
        raise HistoricalArtifactError("historical input must be a regular local artifact")
    raw = artifact_path.read_bytes()
    try:
        payload = json.loads(
            raw,
            parse_float=_reject_json_float,
            parse_int=int,
            parse_constant=_reject_nonfinite_json,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HistoricalArtifactError("historical artifact is not valid UTF-8 JSON") from exc
    root = _mapping(payload, "historical artifact")
    if canonical_json_bytes(root) + b"\n" != raw:
        raise HistoricalArtifactError("historical artifact must use canonical immutable JSON encoding")
    if set(root) != {"schema_version", "content", "content_sha256"}:
        raise HistoricalArtifactError("historical artifact envelope is not exact")
    if root.get("schema_version") != HISTORICAL_ARTIFACT_SCHEMA:
        raise HistoricalArtifactError("unsupported historical artifact schema")
    content = _mapping(root.get("content"), "historical artifact content")
    if set(content) != {"artifact_id", "universe", "source_manifest_sha256", "events"}:
        raise HistoricalArtifactError("historical artifact content schema is not exact")
    claimed_content_sha256 = _sha256(root.get("content_sha256"), "content_sha256")
    actual_content_sha256 = hashlib.sha256(canonical_json_bytes(content)).hexdigest()
    if claimed_content_sha256 != actual_content_sha256:
        raise HistoricalArtifactError("historical artifact content hash mismatch")
    if content.get("universe") != [asset.value for asset in PREDECLARED_UNIVERSE]:
        raise HistoricalArtifactError("historical artifact must declare the fixed v1 universe in order")
    source_hashes = _mapping(content.get("source_manifest_sha256"), "source_manifest_sha256")
    if set(source_hashes) != {Venue.BINANCE.value, Venue.HYPERLIQUID.value}:
        raise HistoricalArtifactError("historical artifact requires both venue source-manifest hashes")
    normalized_source_hashes = {
        venue.value: _sha256(source_hashes.get(venue.value), f"source_manifest_sha256.{venue.value}")
        for venue in Venue
    }
    events = tuple(
        _parse_event(value, index)
        for index, value in enumerate(_sequence(content.get("events"), "events"))
    )
    return HistoricalFundingArtifact(
        artifact_id=_text(content.get("artifact_id"), "artifact_id"),
        source_manifest_sha256=normalized_source_hashes,
        events=events,
        content_sha256=actual_content_sha256,
        file_sha256=hashlib.sha256(raw).hexdigest(),
    )


def _coverage_fraction(
    events: Sequence[HistoricalFundingEvent],
    start_ns: int,
    end_ns: int,
) -> Decimal:
    intervals: list[tuple[int, int]] = []
    for event in events:
        interval_start = max(start_ns, event.settlement_time_ns - event.interval_nanoseconds)
        interval_end = min(end_ns, event.settlement_time_ns)
        if interval_start < interval_end:
            intervals.append((interval_start, interval_end))
    covered = 0
    cursor_start: int | None = None
    cursor_end: int | None = None
    for interval_start, interval_end in sorted(intervals):
        if cursor_start is None or cursor_end is None:
            cursor_start, cursor_end = interval_start, interval_end
        elif interval_start > cursor_end:
            covered += cursor_end - cursor_start
            cursor_start, cursor_end = interval_start, interval_end
        else:
            cursor_end = max(cursor_end, interval_end)
    if cursor_start is not None and cursor_end is not None:
        covered += cursor_end - cursor_start
    return Decimal(covered) / Decimal(end_ns - start_ns)


def _break_even_days(
    gross_rate_per_day: Decimal,
    policy: HistoricalScreenPolicy,
) -> Decimal | None:
    net_daily_carry = gross_rate_per_day - policy.collateral_cost_rate_per_day
    if net_daily_carry <= 0:
        return None
    return policy.fixed_cost_rate / net_daily_carry


def _normalized_venue_summary(events: Sequence[HistoricalFundingEvent]) -> Mapping[str, object]:
    interval_hours = sum((event.funding_interval_hours for event in events), Decimal("0"))
    rate = sum((event.funding_rate for event in events), Decimal("0"))
    hourly_rate = rate / interval_hours if interval_hours > 0 else Decimal("0")
    return {
        "settlement_count": len(events),
        "actual_intervals_hours": sorted({decimal_text(event.funding_interval_hours) for event in events}),
        "sum_discrete_settlement_rates": decimal_text(rate),
        "interval_normalized_hourly_rate": decimal_text(hourly_rate),
        "interval_normalized_daily_rate": decimal_text(hourly_rate * Decimal("24")),
    }


def evaluate_historical_feasibility(
    artifact: HistoricalFundingArtifact,
    *,
    policy: HistoricalScreenPolicy,
    preregistration_sha256: str,
) -> Mapping[str, object]:
    preregistration_hash = _sha256(preregistration_sha256, "preregistration_sha256")
    quality_reasons: set[str] = set()
    duplicate_event_ids = 0
    duplicate_settlements = 0
    flagged_events = sum(bool(event.quality_flags) for event in artifact.events)
    if flagged_events:
        quality_reasons.add("quality_flagged_finalized_funding")

    seen_ids: dict[str, HistoricalFundingEvent] = {}
    seen_settlements: dict[tuple[Venue, CanonicalAsset, int], HistoricalFundingEvent] = {}
    unique_events: list[HistoricalFundingEvent] = []
    for event in sorted(
        artifact.events,
        key=lambda item: (item.canonical_asset.value, item.venue.value, item.settlement_time_ns, item.event_id),
    ):
        prior_id = seen_ids.get(event.event_id)
        if prior_id is not None:
            duplicate_event_ids += 1
            quality_reasons.add("duplicate_event_id")
            continue
        seen_ids[event.event_id] = event
        identity = event.identity()
        prior_settlement = seen_settlements.get(identity)
        if prior_settlement is not None:
            duplicate_settlements += 1
            quality_reasons.add("duplicate_venue_asset_settlement")
            continue
        seen_settlements[identity] = event
        unique_events.append(event)

    grouped: dict[tuple[CanonicalAsset, Venue], list[HistoricalFundingEvent]] = {
        (asset, venue): [] for asset in PREDECLARED_UNIVERSE for venue in Venue
    }
    for event in unique_events:
        grouped[(event.canonical_asset, event.venue)].append(event)
    for key, values in grouped.items():
        values.sort(key=lambda item: (item.settlement_time_ns, item.event_id))
        if not values:
            quality_reasons.add(f"missing_{key[0].value.lower()}_{key[1].value}_history")

    hold_ns = policy.hold_days * NANOSECONDS_PER_DAY
    minimum_history_ns = policy.minimum_common_history_days * NANOSECONDS_PER_DAY
    total_cost_rate = policy.total_cost_rate(policy.hold_days)
    asset_reports: list[Mapping[str, object]] = []
    all_window_oracle_net_rates: list[Decimal] = []
    all_window_primary_net_rates: list[Decimal] = []
    all_window_oracle_gross_rates: list[Decimal] = []

    for asset in PREDECLARED_UNIVERSE:
        binance_events = grouped[(asset, Venue.BINANCE)]
        hyperliquid_events = grouped[(asset, Venue.HYPERLIQUID)]
        windows: list[Mapping[str, object]] = []
        common_start: int | None = None
        common_end: int | None = None
        overall_binance_coverage = Decimal("0")
        overall_hyperliquid_coverage = Decimal("0")
        if binance_events and hyperliquid_events:
            common_start = max(
                binance_events[0].settlement_time_ns - binance_events[0].interval_nanoseconds,
                hyperliquid_events[0].settlement_time_ns - hyperliquid_events[0].interval_nanoseconds,
            )
            common_end = min(binance_events[-1].settlement_time_ns, hyperliquid_events[-1].settlement_time_ns)
            if common_end <= common_start or common_end - common_start < minimum_history_ns:
                quality_reasons.add(f"{asset.value.lower()}_common_history_below_minimum")
            if common_end > common_start:
                overall_binance_coverage = _coverage_fraction(binance_events, common_start, common_end)
                overall_hyperliquid_coverage = _coverage_fraction(
                    hyperliquid_events,
                    common_start,
                    common_end,
                )
                if overall_binance_coverage < policy.minimum_coverage_fraction:
                    quality_reasons.add(f"{asset.value.lower()}_binance_interval_coverage_below_minimum")
                if overall_hyperliquid_coverage < policy.minimum_coverage_fraction:
                    quality_reasons.add(f"{asset.value.lower()}_hyperliquid_interval_coverage_below_minimum")
            window_start = common_start
            while window_start + hold_ns <= common_end:
                window_end = window_start + hold_ns
                binance_coverage = _coverage_fraction(binance_events, window_start, window_end)
                hyperliquid_coverage = _coverage_fraction(hyperliquid_events, window_start, window_end)
                if (
                    binance_coverage >= policy.minimum_coverage_fraction
                    and hyperliquid_coverage >= policy.minimum_coverage_fraction
                ):
                    binance_rate = sum(
                        (
                            event.funding_rate
                            for event in binance_events
                            if window_start < event.settlement_time_ns <= window_end
                        ),
                        Decimal("0"),
                    )
                    hyperliquid_rate = sum(
                        (
                            event.funding_rate
                            for event in hyperliquid_events
                            if window_start < event.settlement_time_ns <= window_end
                        ),
                        Decimal("0"),
                    )
                    primary_gross_rate = hyperliquid_rate - binance_rate
                    reverse_gross_rate = -primary_gross_rate
                    oracle_direction: OracleDirection = (
                        "binance_long_hyperliquid_short"
                        if primary_gross_rate >= reverse_gross_rate
                        else "binance_short_hyperliquid_long"
                    )
                    oracle_gross_rate = max(primary_gross_rate, reverse_gross_rate)
                    primary_net_rate = primary_gross_rate - total_cost_rate
                    oracle_net_rate = oracle_gross_rate - total_cost_rate
                    primary_net_pnl_usd = primary_net_rate * policy.target_notional_per_leg_usd
                    oracle_net_pnl_usd = oracle_net_rate * policy.target_notional_per_leg_usd
                    all_window_primary_net_rates.append(primary_net_rate)
                    all_window_oracle_gross_rates.append(oracle_gross_rate)
                    all_window_oracle_net_rates.append(oracle_net_rate)
                    windows.append(
                        {
                            "window_start_ns": str(window_start),
                            "window_end_ns": str(window_end),
                            "holding_period_days": str(policy.hold_days),
                            "binance_interval_time_coverage": decimal_text(binance_coverage),
                            "hyperliquid_interval_time_coverage": decimal_text(hyperliquid_coverage),
                            "binance_sum_discrete_rates": decimal_text(binance_rate),
                            "hyperliquid_sum_discrete_rates": decimal_text(hyperliquid_rate),
                            "primary_gross_rate": decimal_text(primary_gross_rate),
                            "primary_net_rate_after_all_costs": decimal_text(primary_net_rate),
                            "primary_net_return_on_total_reserved_capital": decimal_text(
                                primary_net_pnl_usd / policy.total_reserved_capital_usd
                            ),
                            "oracle_direction": oracle_direction,
                            "oracle_gross_rate": decimal_text(oracle_gross_rate),
                            "oracle_net_rate_after_all_costs": decimal_text(oracle_net_rate),
                            "oracle_net_pnl_usd": decimal_text(oracle_net_pnl_usd),
                            "oracle_net_return_on_total_reserved_capital": decimal_text(
                                oracle_net_pnl_usd / policy.total_reserved_capital_usd
                            ),
                            "covers_all_costs": oracle_net_rate > 0,
                        }
                    )
                window_start = window_end
        if len(windows) < policy.minimum_complete_windows_per_asset:
            quality_reasons.add(f"{asset.value.lower()}_complete_30d_windows_below_minimum")
        primary_gross_daily = (
            sum((Decimal(cast(str, window["primary_gross_rate"])) for window in windows), Decimal("0"))
            / Decimal(len(windows) * policy.hold_days)
            if windows
            else Decimal("0")
        )
        oracle_gross_daily = (
            sum((Decimal(cast(str, window["oracle_gross_rate"])) for window in windows), Decimal("0"))
            / Decimal(len(windows) * policy.hold_days)
            if windows
            else Decimal("0")
        )
        primary_break_even = _break_even_days(primary_gross_daily, policy)
        oracle_break_even = _break_even_days(oracle_gross_daily, policy)
        asset_reports.append(
            {
                "canonical_asset": asset.value,
                "common_history_start_ns": str(common_start) if common_start is not None else None,
                "common_history_end_ns": str(common_end) if common_end is not None else None,
                "binance_common_interval_time_coverage": decimal_text(overall_binance_coverage),
                "hyperliquid_common_interval_time_coverage": decimal_text(overall_hyperliquid_coverage),
                "complete_window_count": len(windows),
                "binance": _normalized_venue_summary(binance_events),
                "hyperliquid": _normalized_venue_summary(hyperliquid_events),
                "primary_break_even_holding_days": (
                    decimal_text(primary_break_even) if primary_break_even is not None else None
                ),
                "oracle_break_even_holding_days": (
                    decimal_text(oracle_break_even) if oracle_break_even is not None else None
                ),
                "windows": windows,
            }
        )

    complete_window_count = len(all_window_oracle_net_rates)
    if complete_window_count < policy.minimum_total_complete_windows:
        quality_reasons.add("fixed_universe_complete_30d_windows_below_minimum")
    profitable_window_count = sum(rate > 0 for rate in all_window_oracle_net_rates)
    profitable_fraction = (
        Decimal(profitable_window_count) / Decimal(complete_window_count)
        if complete_window_count
        else Decimal("0")
    )
    aggregate_oracle_net_rate = sum(all_window_oracle_net_rates, Decimal("0"))
    aggregate_primary_net_rate = sum(all_window_primary_net_rates, Decimal("0"))
    mean_oracle_net_rate = (
        aggregate_oracle_net_rate / Decimal(complete_window_count)
        if complete_window_count
        else Decimal("0")
    )
    mean_primary_net_rate = (
        aggregate_primary_net_rate / Decimal(complete_window_count)
        if complete_window_count
        else Decimal("0")
    )
    mean_oracle_gross_rate = (
        sum(all_window_oracle_gross_rates, Decimal("0")) / Decimal(complete_window_count)
        if complete_window_count
        else Decimal("0")
    )

    reasons: list[str]
    verdict: HistoricalVerdict
    if quality_reasons:
        verdict = "INSUFFICIENT_EVIDENCE"
        reasons = sorted(quality_reasons)
    elif aggregate_oracle_net_rate <= 0:
        verdict = "ABANDON"
        reasons = ["optimistic_ex_post_oracle_non_positive_after_all_costs"]
    elif profitable_fraction <= policy.rarely_covers_fraction:
        verdict = "ABANDON"
        reasons = ["optimistic_ex_post_oracle_rarely_covers_all_costs_over_30d_holds"]
    else:
        verdict = "CONTINUE"
        reasons = ["optimistic_oracle_clears_b0_futility_gate_only"]

    policy_wire = policy.as_wire()
    policy_sha256 = hashlib.sha256(canonical_json_bytes(policy_wire)).hexdigest()
    code_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    body: dict[str, object] = {
        "schema_version": HISTORICAL_REPORT_SCHEMA,
        "authority": "historical_futility_screen_only",
        "grants_live_authority": False,
        "artifact_id": artifact.artifact_id,
        "input_file_sha256": artifact.file_sha256,
        "input_content_sha256": artifact.content_sha256,
        "source_manifest_sha256": dict(artifact.source_manifest_sha256),
        "preregistration_sha256": preregistration_hash,
        "policy_sha256": policy_sha256,
        "code_sha256": code_sha256,
        "verdict": verdict,
        "verdict_reasons": reasons,
        "quality": {
            "passes": not quality_reasons,
            "failure_reasons": sorted(quality_reasons),
            "input_event_count": len(artifact.events),
            "unique_event_count": len(unique_events),
            "quality_flagged_event_count": flagged_events,
            "duplicate_event_id_count": duplicate_event_ids,
            "duplicate_settlement_count": duplicate_settlements,
            "complete_30d_window_count": complete_window_count,
        },
        "assumptions": policy_wire,
        "costs_per_30d_hold": {
            "four_commission_rate": decimal_text(policy.four_commission_rate),
            "four_commissions_usd": decimal_text(
                policy.four_commission_rate * policy.target_notional_per_leg_usd
            ),
            "stablecoin_conversion_cost_rate": decimal_text(policy.stablecoin_conversion_cost_rate),
            "collateral_opportunity_cost_rate": decimal_text(
                policy.collateral_cost_rate_per_day * Decimal(policy.hold_days)
            ),
            "repair_failure_cost_rate": decimal_text(policy.repair_failure_cost_rate),
            "total_cost_rate": decimal_text(total_cost_rate),
            "total_cost_usd": decimal_text(total_cost_rate * policy.target_notional_per_leg_usd),
            "favorable_basis_pnl_usd": "0",
            "slippage_cost_usd": "0",
        },
        "aggregate": {
            "complete_30d_window_count": complete_window_count,
            "oracle_profitable_window_count": profitable_window_count,
            "oracle_profitable_window_fraction": decimal_text(profitable_fraction),
            "rarely_covers_threshold_at_or_below": decimal_text(policy.rarely_covers_fraction),
            "primary_net_rate_after_all_costs": decimal_text(mean_primary_net_rate),
            "oracle_net_rate_after_all_costs": decimal_text(mean_oracle_net_rate),
            "primary_cumulative_net_rate_after_all_costs": decimal_text(aggregate_primary_net_rate),
            "oracle_cumulative_net_rate_after_all_costs": decimal_text(aggregate_oracle_net_rate),
            "primary_net_return_on_total_reserved_capital": decimal_text(
                mean_primary_net_rate
                * policy.target_notional_per_leg_usd
                / policy.total_reserved_capital_usd
            ),
            "oracle_net_return_on_total_reserved_capital": decimal_text(
                mean_oracle_net_rate
                * policy.target_notional_per_leg_usd
                / policy.total_reserved_capital_usd
            ),
            "oracle_gross_rate": decimal_text(mean_oracle_gross_rate),
        },
        "assets": asset_reports,
    }
    body["report_sha256"] = hashlib.sha256(canonical_json_bytes(body)).hexdigest()
    return body


def write_historical_report(report: Mapping[str, object], path: str | Path) -> Path:
    output = Path(path).resolve()
    if output.exists() or output.is_symlink():
        raise HistoricalArtifactError(f"refusing to replace immutable historical report: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    encoded = canonical_json_bytes(report) + b"\n"
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=output.parent,
            prefix=f".{output.name}.",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary_name, output)
        except FileExistsError as exc:
            raise HistoricalArtifactError(f"refusing to replace immutable historical report: {output}") from exc
        Path(temporary_name).unlink()
        temporary_name = None
        directory_flag = int(getattr(os, "O_DIRECTORY", 0))
        if directory_flag:
            descriptor = os.open(output.parent, os.O_RDONLY | directory_flag)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
    return output


def verify_historical_report(path: str | Path) -> Mapping[str, object]:
    report_path = Path(path).resolve()
    raw = report_path.read_bytes()
    try:
        payload = json.loads(
            raw,
            parse_float=_reject_json_float,
            parse_int=int,
            parse_constant=_reject_nonfinite_json,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HistoricalArtifactError("historical report is not valid UTF-8 JSON") from exc
    root = _mapping(payload, "historical report")
    if canonical_json_bytes(root) + b"\n" != raw:
        raise HistoricalArtifactError("historical report is not canonical immutable JSON")
    if root.get("schema_version") != HISTORICAL_REPORT_SCHEMA:
        raise HistoricalArtifactError("unsupported historical report schema")
    claimed = _sha256(root.get("report_sha256"), "report_sha256")
    body = {key: value for key, value in root.items() if key != "report_sha256"}
    if claimed != hashlib.sha256(canonical_json_bytes(body)).hexdigest():
        raise HistoricalArtifactError("historical report hash mismatch")
    if root.get("verdict") not in {"CONTINUE", "ABANDON", "INSUFFICIENT_EVIDENCE"}:
        raise HistoricalArtifactError("historical report verdict is invalid")
    if root.get("grants_live_authority") is not False:
        raise HistoricalArtifactError("historical report cannot grant live authority")
    return root


__all__ = [
    "HISTORICAL_ARTIFACT_SCHEMA",
    "HISTORICAL_REPORT_SCHEMA",
    "HistoricalArtifactError",
    "HistoricalFundingArtifact",
    "HistoricalFundingEvent",
    "HistoricalScreenPolicy",
    "evaluate_historical_feasibility",
    "load_historical_artifact",
    "load_historical_screen_policy",
    "seal_historical_artifact",
    "verify_historical_report",
    "write_historical_report",
]
