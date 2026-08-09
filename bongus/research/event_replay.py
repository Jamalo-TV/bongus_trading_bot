"""Causal event-replay foundation backed by the canonical DecisionEngine.

The replay consumes events in availability order, reconstructs sequence-aware
spot and perpetual books, and invokes exactly the same decision engine used by
runtime adapters.  Data gaps stop validation by default; callers may opt into
explicit outage modelling, in which case entry remains blocked until a fresh
snapshot repairs the affected stream.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
from typing import Iterable, Literal, Mapping, TypeAlias

from bongus.domain.units import AnnualizedReportingRate, LegNotionalUsd, RawSettlementRate
from bongus.engine.leg_state_machine import (
    ExecutionInvariantError,
    HedgeCycleState,
    LegUpdate,
)
from bongus.market_data.depth_tracker import BookMarket, DepthTracker
from bongus.strategies.decision_engine import (
    Decision,
    DecisionEngine,
    DecisionRequest,
    PortfolioSelection,
)


BookUpdateKind = Literal["snapshot", "diff"]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("replay timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class ReplayDatasetManifest:
    """Immutable provenance required before a dataset can support validation."""

    symbols: tuple[str, ...]
    venue_contracts: Mapping[str, str]
    source: str
    retrieved_at: datetime
    range_start: datetime
    range_end: datetime
    cadence: str
    universe_construction: str
    listing_delisting_treatment: str
    file_sha256: Mapping[str, str]
    timezone_name: str = "UTC"

    def __post_init__(self) -> None:
        if not self.symbols:
            raise ValueError("manifest requires at least one symbol")
        if any(not symbol.strip() for symbol in self.symbols):
            raise ValueError("manifest symbols must be non-empty")
        normalized_symbols = tuple(symbol.strip().upper() for symbol in self.symbols)
        if normalized_symbols != self.symbols:
            raise ValueError("manifest symbols must be normalized uppercase")
        if len(set(normalized_symbols)) != len(normalized_symbols):
            raise ValueError("manifest symbols must be unique")
        if any(symbol not in self.venue_contracts for symbol in normalized_symbols):
            raise ValueError("manifest requires a venue contract for every symbol")
        if not self.source.strip():
            raise ValueError("manifest source is required")
        start = _utc(self.range_start)
        end = _utc(self.range_end)
        retrieved = _utc(self.retrieved_at)
        if end < start:
            raise ValueError("manifest range_end precedes range_start")
        if retrieved < end:
            raise ValueError("manifest retrieval predates dataset range end")
        if self.timezone_name.upper() != "UTC":
            raise ValueError("replay datasets must declare UTC")
        for name, value in (
            ("cadence", self.cadence),
            ("universe_construction", self.universe_construction),
            ("listing_delisting_treatment", self.listing_delisting_treatment),
        ):
            if not value.strip():
                raise ValueError(f"manifest {name} is required")
        if not self.file_sha256:
            raise ValueError("manifest requires at least one file hash")
        for relative_path, digest in self.file_sha256.items():
            if Path(relative_path).is_absolute() or ".." in Path(relative_path).parts:
                raise ValueError("manifest file paths must be relative and contained")
            if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest.lower()):
                raise ValueError("manifest file hashes must be SHA-256 hex digests")

    @property
    def manifest_hash(self) -> str:
        payload = asdict(self)
        for key in ("retrieved_at", "range_start", "range_end"):
            payload[key] = _utc(payload[key]).isoformat()
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def verify_files(self, root: str | Path) -> tuple[str, ...]:
        root_path = Path(root).resolve()
        failures: list[str] = []
        for relative_path, expected_hash in sorted(self.file_sha256.items()):
            path = (root_path / relative_path).resolve()
            try:
                path.relative_to(root_path)
            except ValueError:
                failures.append(f"outside_root:{relative_path}")
                continue
            if not path.is_file():
                failures.append(f"missing:{relative_path}")
                continue
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest().lower() != expected_hash.lower():
                failures.append(f"hash_mismatch:{relative_path}")
        return tuple(failures)


@dataclass(frozen=True, slots=True)
class BookReplayEvent:
    event_id: str
    symbol: str
    market: BookMarket
    event_time: datetime
    available_at: datetime
    update_kind: BookUpdateKind
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]
    final_update_id: int
    first_update_id: int | None = None
    previous_final_update_id: int | None = None


@dataclass(frozen=True, slots=True)
class DecisionReplayEvent:
    event_id: str
    available_at: datetime
    request: DecisionRequest
    selection_cycle_id: str = ""


@dataclass(frozen=True, slots=True)
class FundingSettlementReplayEvent:
    event_id: str
    symbol: str
    settlement_time: datetime
    available_at: datetime
    raw_rate: RawSettlementRate
    liable_leg_notional: LegNotionalUsd
    direction: Literal["long_spot_short_perp", "short_spot_long_perp"]
    eligible: bool = True


@dataclass(frozen=True, slots=True)
class OutageReplayEvent:
    event_id: str
    available_at: datetime
    symbol: str
    markets: tuple[BookMarket, ...] = ("spot", "perp")


@dataclass(frozen=True, slots=True)
class MarketMetadataReplayEvent:
    """Point-in-time venue/account inputs that a decision may not self-assert.

    A metadata event represents one causally available exchange-info/account
    snapshot.  Replay replaces the corresponding values on ``DecisionRequest``
    so optimistic values embedded in a research row cannot bypass production
    calendar, filter, quota, collateral, margin, fee, borrow, or liquidation
    assumptions.
    """

    event_id: str
    symbol: str
    event_time: datetime
    available_at: datetime
    listed: bool
    calendar_authoritative: bool
    funding_interval_hours: float
    spot_filters_valid: bool
    perp_filters_valid: bool
    spot_filter_version: str
    perp_filter_version: str
    rate_limit_budget: int
    collateral_available_usd: float
    margin_available_usd: float
    spot_taker_fee_pct: float
    perp_taker_fee_pct: float
    borrow_cost_bps_per_hour: float = 0.0
    collateral_cost_bps_per_hour: float = 0.0
    liquidation_tail_bps_per_settlement: float = 0.0


@dataclass(frozen=True, slots=True)
class ReferenceMarketReplayEvent:
    """Causally available trade/mark/index/premium and funding bounds."""

    event_id: str
    symbol: str
    event_time: datetime
    available_at: datetime
    trade_price: float
    mark_price: float
    index_price: float
    premium_index: float
    funding_cap: float
    funding_floor: float


ReplayService = Literal["market_ws", "rest", "user_stream", "persistence"]


@dataclass(frozen=True, slots=True)
class ServiceStateReplayEvent:
    """Explicitly model an outage or recovery without dropping the row."""

    event_id: str
    available_at: datetime
    service: ReplayService
    available: bool
    symbol: str = ""


@dataclass(frozen=True, slots=True)
class ExecutionCycleStartReplayEvent:
    """Start a reserved two-leg cycle with end-to-end latency evidence."""

    event_id: str
    cycle_id: str
    symbol: str
    available_at: datetime
    decision_time: datetime
    ipc_time: datetime
    rest_send_time: datetime
    ack_time: datetime
    operation: Literal["entry", "exit"]
    direction: Literal["long_spot_short_perp", "short_spot_long_perp"]
    target_quantity: str
    reservation_id: str
    collateral_reserved_usd: float
    margin_reserved_usd: float
    starting_spot_quantity: str = "0"
    starting_perp_quantity: str = "0"
    rotation_id: str = ""
    rotation_role: Literal["", "exit", "entry"] = ""


@dataclass(frozen=True, slots=True)
class ExecutionLegReplayEvent:
    """Exchange leg transition plus queue/cost evidence at availability time."""

    event_id: str
    cycle_id: str
    symbol: str
    available_at: datetime
    update: LegUpdate
    mark_price: float
    fill_price: float = 0.0
    queue_ahead_quantity: float = 0.0
    fee_usd: float = 0.0
    spread_cost_usd: float = 0.0
    impact_cost_usd: float = 0.0
    adverse_markout_usd: float = 0.0
    operational_loss_usd: float = 0.0


ReplayEvent: TypeAlias = (
    BookReplayEvent
    | DecisionReplayEvent
    | FundingSettlementReplayEvent
    | OutageReplayEvent
    | MarketMetadataReplayEvent
    | ReferenceMarketReplayEvent
    | ServiceStateReplayEvent
    | ExecutionCycleStartReplayEvent
    | ExecutionLegReplayEvent
)


@dataclass(frozen=True, slots=True)
class ReplayDataQualityFailure:
    event_id: str
    reason: str
    symbol: str = ""
    market: str = ""


class ReplayDataQualityError(RuntimeError):
    def __init__(self, failure: ReplayDataQualityFailure) -> None:
        self.failure = failure
        super().__init__(
            f"{failure.event_id}: {failure.reason}"
            + (f" ({failure.symbol}/{failure.market})" if failure.symbol else "")
        )


@dataclass(frozen=True, slots=True)
class ReplaySettlementCashflow:
    event_id: str
    symbol: str
    settlement_time: datetime
    raw_rate: RawSettlementRate
    reporting_annualized_rate: AnnualizedReportingRate
    cashflow_usd: float


@dataclass(frozen=True, slots=True)
class ReplayExecutionLatency:
    cycle_id: str
    decision_to_ipc_ms: float
    ipc_to_rest_ms: float
    rest_to_ack_ms: float
    decision_to_ack_ms: float


@dataclass(frozen=True, slots=True)
class ReplayExecutionCycle:
    cycle_id: str
    symbol: str
    operation: str
    reservation_id: str
    safe_to_project_complete: bool
    mismatch_quantity: str
    unhedged_notional_ms: str
    last_queue_ahead_quantity: float
    breaches: tuple[str, ...]
    snapshot: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ReplayPnlAttribution:
    funding_usd: float = 0.0
    basis_usd: float = 0.0
    fees_usd: float = 0.0
    spread_usd: float = 0.0
    impact_usd: float = 0.0
    adverse_markout_usd: float = 0.0
    operational_usd: float = 0.0
    total_usd: float = 0.0


@dataclass(frozen=True, slots=True)
class ReplayResult:
    decisions: tuple[Decision, ...]
    selections: tuple[PortfolioSelection, ...]
    settlement_cashflows: tuple[ReplaySettlementCashflow, ...]
    data_quality_failures: tuple[ReplayDataQualityFailure, ...]
    processed_events: int
    duplicate_events: int
    manifest_hash: str = ""
    execution_cycles: tuple[ReplayExecutionCycle, ...] = ()
    execution_latency_samples: tuple[ReplayExecutionLatency, ...] = ()
    operational_blocks: tuple[str, ...] = ()
    pnl_attribution: ReplayPnlAttribution = ReplayPnlAttribution()


@dataclass(frozen=True, slots=True)
class EventReplayConfig:
    model_data_quality_failures_as_outages: bool = False
    max_book_levels: int = 20

    def __post_init__(self) -> None:
        if self.max_book_levels <= 0:
            raise ValueError("max_book_levels must be positive")


@dataclass(slots=True)
class _ReplayBook:
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    last_update_id: int | None = None
    ready: bool = False


@dataclass(frozen=True, slots=True)
class _ReplayMarketMetadata:
    observed_at: datetime
    listed: bool
    calendar_authoritative: bool
    funding_interval_hours: float
    spot_filters_valid: bool
    perp_filters_valid: bool
    spot_filter_version: str
    perp_filter_version: str
    rate_limit_budget: int
    collateral_available_usd: float
    margin_available_usd: float
    spot_taker_fee_pct: float
    perp_taker_fee_pct: float
    borrow_cost_bps_per_hour: float
    collateral_cost_bps_per_hour: float
    liquidation_tail_bps_per_settlement: float


@dataclass(frozen=True, slots=True)
class _ReplayReferenceMarket:
    observed_at: datetime
    trade_price: float
    mark_price: float
    index_price: float
    premium_index: float
    funding_cap: float
    funding_floor: float


@dataclass(slots=True)
class _ReplayCycleContext:
    symbol: str
    operation: str
    reservation_id: str
    rotation_id: str
    rotation_role: str
    state: HedgeCycleState
    last_queue_ahead_quantity: float = 0.0


class EventReplay:
    """Sequence-correct causal replay using :class:`DecisionEngine`."""

    def __init__(
        self,
        decision_engine: DecisionEngine,
        *,
        config: EventReplayConfig | None = None,
    ) -> None:
        self.decision_engine = decision_engine
        self.config = config or EventReplayConfig()

    @staticmethod
    def _apply_levels(
        target: dict[float, float], levels: tuple[tuple[float, float], ...]
    ) -> None:
        for raw_price, raw_quantity in levels:
            price = float(raw_price)
            quantity = float(raw_quantity)
            if (
                not math.isfinite(price)
                or not math.isfinite(quantity)
                or price <= 0.0
                or quantity < 0.0
            ):
                raise ValueError("invalid price or quantity in replay book")
            if quantity == 0.0:
                target.pop(price, None)
            else:
                target[price] = quantity

    def _published_levels(
        self, book: _ReplayBook
    ) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        bids = sorted(book.bids.items(), key=lambda item: item[0], reverse=True)[
            : self.config.max_book_levels
        ]
        asks = sorted(book.asks.items(), key=lambda item: item[0])[
            : self.config.max_book_levels
        ]
        return bids, asks

    def _record_failure(
        self,
        failure: ReplayDataQualityFailure,
        failures: list[ReplayDataQualityFailure],
    ) -> None:
        failures.append(failure)
        if not self.config.model_data_quality_failures_as_outages:
            raise ReplayDataQualityError(failure)

    def run(self, events: Iterable[ReplayEvent]) -> ReplayResult:
        tracker = DepthTracker(clock=lambda: 0.0)
        books: dict[tuple[str, BookMarket], _ReplayBook] = {}
        market_metadata: dict[str, _ReplayMarketMetadata] = {}
        reference_markets: dict[str, _ReplayReferenceMarket] = {}
        service_outages: set[tuple[str, str]] = set()
        cycles: dict[str, _ReplayCycleContext] = {}
        active_reservations: set[str] = set()
        completed_rotation_exits: set[str] = set()
        latency_samples: list[ReplayExecutionLatency] = []
        operational_blocks: list[str] = []
        fee_cost_usd = 0.0
        spread_cost_usd = 0.0
        impact_cost_usd = 0.0
        adverse_markout_usd = 0.0
        operational_loss_usd = 0.0
        seen_event_ids: set[str] = set()
        seen_funding_effects: set[tuple[str, datetime, str]] = set()
        decisions: list[Decision] = []
        decisions_by_cycle: dict[str, list[Decision]] = {}
        portfolio_gross_by_cycle: dict[str, list[float]] = {}
        occupied_slots_by_cycle: dict[str, list[float]] = {}
        collateral_by_cycle: dict[str, list[float]] = {}
        margin_by_cycle: dict[str, list[float]] = {}
        cashflows: list[ReplaySettlementCashflow] = []
        failures: list[ReplayDataQualityFailure] = []
        processed = 0
        duplicates = 0
        previous_available_at: datetime | None = None

        for event in events:
            available_at = _utc(event.available_at)
            if previous_available_at is not None and available_at < previous_available_at:
                self._record_failure(
                    ReplayDataQualityFailure(
                        event.event_id, "events_not_in_availability_order"
                    ),
                    failures,
                )
            previous_available_at = available_at
            if not event.event_id.strip():
                self._record_failure(
                    ReplayDataQualityFailure("<missing>", "missing_event_id"),
                    failures,
                )
                continue
            if event.event_id in seen_event_ids:
                duplicates += 1
                continue
            seen_event_ids.add(event.event_id)
            processed += 1

            if isinstance(event, BookReplayEvent):
                symbol = event.symbol.strip().upper()
                event_time = _utc(event.event_time)
                key = (symbol, event.market)
                book = books.setdefault(key, _ReplayBook())
                if event_time > available_at:
                    book.ready = False
                    self._record_failure(
                        ReplayDataQualityFailure(
                            event.event_id,
                            "book_available_before_exchange_event",
                            symbol,
                            event.market,
                        ),
                        failures,
                    )
                    continue
                if event.update_kind == "snapshot":
                    book.bids.clear()
                    book.asks.clear()
                    try:
                        self._apply_levels(book.bids, event.bids)
                        self._apply_levels(book.asks, event.asks)
                    except ValueError:
                        book.ready = False
                        self._record_failure(
                            ReplayDataQualityFailure(
                                event.event_id,
                                "invalid_book_snapshot",
                                symbol,
                                event.market,
                            ),
                            failures,
                        )
                        continue
                    book.last_update_id = int(event.final_update_id)
                    book.ready = True
                elif event.update_kind == "diff":
                    if book.last_update_id is None or not book.ready:
                        self._record_failure(
                            ReplayDataQualityFailure(
                                event.event_id,
                                "diff_before_snapshot",
                                symbol,
                                event.market,
                            ),
                            failures,
                        )
                        continue
                    if int(event.final_update_id) <= book.last_update_id:
                        # Sequence-level duplicates are idempotent even if the
                        # transport assigned a different envelope event ID.
                        duplicates += 1
                        continue
                    expected = book.last_update_id + 1
                    first_update = (
                        int(event.first_update_id)
                        if event.first_update_id is not None
                        else int(event.final_update_id)
                    )
                    predecessor_matches = (
                        event.previous_final_update_id is None
                        or int(event.previous_final_update_id) == book.last_update_id
                    )
                    covers_expected = first_update <= expected <= int(event.final_update_id)
                    if not predecessor_matches or not covers_expected:
                        book.ready = False
                        self._record_failure(
                            ReplayDataQualityFailure(
                                event.event_id,
                                "book_sequence_gap",
                                symbol,
                                event.market,
                            ),
                            failures,
                        )
                        continue
                    try:
                        self._apply_levels(book.bids, event.bids)
                        self._apply_levels(book.asks, event.asks)
                    except ValueError:
                        book.ready = False
                        self._record_failure(
                            ReplayDataQualityFailure(
                                event.event_id,
                                "invalid_book_diff",
                                symbol,
                                event.market,
                            ),
                            failures,
                        )
                        continue
                    book.last_update_id = int(event.final_update_id)
                else:
                    book.ready = False
                    self._record_failure(
                        ReplayDataQualityFailure(
                            event.event_id,
                            "unknown_book_update_kind",
                            symbol,
                            event.market,
                        ),
                        failures,
                    )
                    continue

                bids, asks = self._published_levels(book)
                if not bids or not asks or bids[0][0] > asks[0][0]:
                    book.ready = False
                    self._record_failure(
                        ReplayDataQualityFailure(
                            event.event_id,
                            "invalid_reconstructed_book",
                            symbol,
                            event.market,
                        ),
                        failures,
                    )
                    continue
                tracker.on_l2depth(
                    symbol,
                    event.market,
                    bids,
                    asks,
                    received_at=available_at.timestamp(),
                )
                continue

            if isinstance(event, MarketMetadataReplayEvent):
                symbol = event.symbol.strip().upper()
                event_time = _utc(event.event_time)
                invalid_reason = ""
                numeric_values = {
                    "funding_interval_hours": event.funding_interval_hours,
                    "collateral_available_usd": event.collateral_available_usd,
                    "margin_available_usd": event.margin_available_usd,
                    "spot_taker_fee_pct": event.spot_taker_fee_pct,
                    "perp_taker_fee_pct": event.perp_taker_fee_pct,
                    "borrow_cost_bps_per_hour": event.borrow_cost_bps_per_hour,
                    "collateral_cost_bps_per_hour": (
                        event.collateral_cost_bps_per_hour
                    ),
                    "liquidation_tail_bps_per_settlement": (
                        event.liquidation_tail_bps_per_settlement
                    ),
                }
                if not symbol:
                    invalid_reason = "missing_metadata_symbol"
                elif event_time > available_at:
                    invalid_reason = "metadata_available_before_exchange_event"
                elif any(
                    not math.isfinite(float(value)) or float(value) < 0.0
                    for value in numeric_values.values()
                ):
                    invalid_reason = "invalid_market_metadata_numeric_value"
                elif event.funding_interval_hours <= 0.0:
                    invalid_reason = "invalid_funding_interval"
                elif (
                    isinstance(event.rate_limit_budget, bool)
                    or not isinstance(event.rate_limit_budget, int)
                    or event.rate_limit_budget < 0
                ):
                    invalid_reason = "invalid_rate_limit_budget"
                elif event.spot_filters_valid and not event.spot_filter_version.strip():
                    invalid_reason = "missing_spot_filter_version"
                elif event.perp_filters_valid and not event.perp_filter_version.strip():
                    invalid_reason = "missing_perp_filter_version"
                if invalid_reason:
                    self._record_failure(
                        ReplayDataQualityFailure(
                            event.event_id,
                            invalid_reason,
                            symbol,
                        ),
                        failures,
                    )
                    continue
                market_metadata[symbol] = _ReplayMarketMetadata(
                    observed_at=available_at,
                    listed=bool(event.listed),
                    calendar_authoritative=bool(event.calendar_authoritative),
                    funding_interval_hours=float(event.funding_interval_hours),
                    spot_filters_valid=bool(event.spot_filters_valid),
                    perp_filters_valid=bool(event.perp_filters_valid),
                    spot_filter_version=event.spot_filter_version.strip(),
                    perp_filter_version=event.perp_filter_version.strip(),
                    rate_limit_budget=int(event.rate_limit_budget),
                    collateral_available_usd=float(event.collateral_available_usd),
                    margin_available_usd=float(event.margin_available_usd),
                    spot_taker_fee_pct=float(event.spot_taker_fee_pct),
                    perp_taker_fee_pct=float(event.perp_taker_fee_pct),
                    borrow_cost_bps_per_hour=float(event.borrow_cost_bps_per_hour),
                    collateral_cost_bps_per_hour=float(
                        event.collateral_cost_bps_per_hour
                    ),
                    liquidation_tail_bps_per_settlement=float(
                        event.liquidation_tail_bps_per_settlement
                    ),
                )
                continue

            if isinstance(event, ReferenceMarketReplayEvent):
                symbol = event.symbol.strip().upper()
                event_time = _utc(event.event_time)
                numeric_values = (
                    event.trade_price,
                    event.mark_price,
                    event.index_price,
                    event.premium_index,
                    event.funding_cap,
                    event.funding_floor,
                )
                reason = ""
                if not symbol:
                    reason = "missing_reference_market_symbol"
                elif event_time > available_at:
                    reason = "reference_market_available_before_exchange_event"
                elif any(not math.isfinite(float(value)) for value in numeric_values):
                    reason = "invalid_reference_market_value"
                elif min(
                    event.trade_price,
                    event.mark_price,
                    event.index_price,
                ) <= 0.0:
                    reason = "non_positive_reference_market_price"
                elif event.funding_floor > event.funding_cap:
                    reason = "invalid_funding_bounds"
                if reason:
                    self._record_failure(
                        ReplayDataQualityFailure(event.event_id, reason, symbol),
                        failures,
                    )
                    continue
                reference_markets[symbol] = _ReplayReferenceMarket(
                    observed_at=available_at,
                    trade_price=float(event.trade_price),
                    mark_price=float(event.mark_price),
                    index_price=float(event.index_price),
                    premium_index=float(event.premium_index),
                    funding_cap=float(event.funding_cap),
                    funding_floor=float(event.funding_floor),
                )
                continue

            if isinstance(event, ServiceStateReplayEvent):
                symbol = event.symbol.strip().upper()
                key = (event.service, symbol)
                if event.available:
                    service_outages.discard(key)
                else:
                    service_outages.add(key)
                continue

            if isinstance(event, ExecutionCycleStartReplayEvent):
                symbol = event.symbol.strip().upper()
                timestamps = tuple(
                    _utc(value)
                    for value in (
                        event.decision_time,
                        event.ipc_time,
                        event.rest_send_time,
                        event.ack_time,
                        event.available_at,
                    )
                )
                invalid_reason = ""
                if not event.cycle_id.strip() or not symbol:
                    invalid_reason = "missing_execution_cycle_identity"
                elif event.cycle_id in cycles:
                    invalid_reason = "duplicate_execution_cycle_id"
                elif timestamps != tuple(sorted(timestamps)):
                    invalid_reason = "execution_latency_timestamp_regression"
                elif not event.reservation_id.strip():
                    invalid_reason = "missing_execution_reservation"
                elif event.reservation_id in active_reservations:
                    invalid_reason = "duplicate_active_reservation"
                elif event.operation == "entry" and event.direction != "long_spot_short_perp":
                    invalid_reason = "reverse_short_spot_entry_disabled"
                elif (
                    event.rotation_role == "entry"
                    and (
                        not event.rotation_id.strip()
                        or event.rotation_id not in completed_rotation_exits
                    )
                ):
                    invalid_reason = "rotation_entry_before_exit_confirmed"
                elif any(
                    not math.isfinite(float(value)) or float(value) < 0.0
                    for value in (
                        event.collateral_reserved_usd,
                        event.margin_reserved_usd,
                    )
                ):
                    invalid_reason = "invalid_execution_reservation_amount"
                if invalid_reason:
                    self._record_failure(
                        ReplayDataQualityFailure(
                            event.event_id,
                            invalid_reason,
                            symbol,
                        ),
                        failures,
                    )
                    continue
                if any(
                    (service, symbol) in service_outages
                    or (service, "") in service_outages
                    for service in ("rest", "persistence")
                ):
                    operational_blocks.append(
                        f"{event.cycle_id}:execution_service_unavailable"
                    )
                    continue
                try:
                    if event.operation == "entry":
                        state = HedgeCycleState.entry(
                            event.cycle_id,
                            event.target_quantity,
                            direction=event.direction,
                        )
                    else:
                        state = HedgeCycleState.exit(
                            event.cycle_id,
                            spot_quantity=event.starting_spot_quantity,
                            perp_quantity=event.starting_perp_quantity,
                        )
                except ExecutionInvariantError as exc:
                    self._record_failure(
                        ReplayDataQualityFailure(
                            event.event_id,
                            f"invalid_execution_cycle:{exc}",
                            symbol,
                        ),
                        failures,
                    )
                    continue
                cycles[event.cycle_id] = _ReplayCycleContext(
                    symbol=symbol,
                    operation=event.operation,
                    reservation_id=event.reservation_id,
                    rotation_id=event.rotation_id,
                    rotation_role=event.rotation_role,
                    state=state,
                )
                active_reservations.add(event.reservation_id)
                decision_time, ipc_time, rest_time, ack_time, _ = timestamps
                latency_samples.append(
                    ReplayExecutionLatency(
                        cycle_id=event.cycle_id,
                        decision_to_ipc_ms=(ipc_time - decision_time).total_seconds()
                        * 1_000.0,
                        ipc_to_rest_ms=(rest_time - ipc_time).total_seconds()
                        * 1_000.0,
                        rest_to_ack_ms=(ack_time - rest_time).total_seconds()
                        * 1_000.0,
                        decision_to_ack_ms=(ack_time - decision_time).total_seconds()
                        * 1_000.0,
                    )
                )
                continue

            if isinstance(event, ExecutionLegReplayEvent):
                symbol = event.symbol.strip().upper()
                context = cycles.get(event.cycle_id)
                invalid_reason = ""
                numeric_values = (
                    event.mark_price,
                    event.fill_price,
                    event.queue_ahead_quantity,
                    event.fee_usd,
                    event.spread_cost_usd,
                    event.impact_cost_usd,
                    event.adverse_markout_usd,
                    event.operational_loss_usd,
                )
                try:
                    cumulative_quantity = float(event.update.cumulative_quantity)
                except (TypeError, ValueError, OverflowError):
                    cumulative_quantity = math.nan
                if context is None:
                    invalid_reason = "execution_leg_before_cycle"
                elif context.symbol != symbol:
                    invalid_reason = "execution_leg_symbol_mismatch"
                elif event.update.event_id != event.event_id:
                    invalid_reason = "execution_leg_event_identity_mismatch"
                elif any(
                    not math.isfinite(float(value)) or float(value) < 0.0
                    for value in numeric_values
                ):
                    invalid_reason = "invalid_execution_leg_numeric_value"
                elif event.mark_price <= 0.0:
                    invalid_reason = "non_positive_execution_mark"
                elif cumulative_quantity > 0.0 and event.fill_price <= 0.0:
                    invalid_reason = "missing_execution_fill_price"
                elif int(event.update.event_time_ms) > int(
                    available_at.timestamp() * 1_000
                ):
                    invalid_reason = "execution_update_available_before_exchange_event"
                if invalid_reason:
                    self._record_failure(
                        ReplayDataQualityFailure(
                            event.event_id,
                            invalid_reason,
                            symbol,
                        ),
                        failures,
                    )
                    continue
                assert context is not None
                if (
                    event.update.source == "stream"
                    and (
                        ("user_stream", symbol) in service_outages
                        or ("user_stream", "") in service_outages
                    )
                ):
                    operational_blocks.append(
                        f"{event.event_id}:user_stream_unavailable"
                    )
                    continue
                observation_ms = int(available_at.timestamp() * 1_000)
                try:
                    context.state.observe_risk(
                        now_ms=observation_ms,
                        mark_price=event.mark_price,
                    )
                    transition = context.state.apply(event.update)
                    context.state.observe_risk(
                        now_ms=observation_ms,
                        mark_price=event.mark_price,
                    )
                except ExecutionInvariantError as exc:
                    self._record_failure(
                        ReplayDataQualityFailure(
                            event.event_id,
                            f"execution_invariant:{exc}",
                            symbol,
                        ),
                        failures,
                    )
                    continue
                context.last_queue_ahead_quantity = float(
                    event.queue_ahead_quantity
                )
                if transition.applied and transition.fill_delta > Decimal("0"):
                    fee_cost_usd += float(event.fee_usd)
                    spread_cost_usd += float(event.spread_cost_usd)
                    impact_cost_usd += float(event.impact_cost_usd)
                    adverse_markout_usd += float(event.adverse_markout_usd)
                    operational_loss_usd += float(event.operational_loss_usd)
                if context.state.safe_to_project_complete:
                    active_reservations.discard(context.reservation_id)
                    if context.rotation_role == "exit" and context.rotation_id:
                        completed_rotation_exits.add(context.rotation_id)
                continue

            if isinstance(event, OutageReplayEvent):
                symbol = event.symbol.strip().upper()
                for market in event.markets:
                    books.setdefault((symbol, market), _ReplayBook()).ready = False
                continue

            if isinstance(event, FundingSettlementReplayEvent):
                symbol = event.symbol.strip().upper()
                settlement_time = _utc(event.settlement_time)
                reference_market = reference_markets.get(symbol)
                if reference_market is None:
                    self._record_failure(
                        ReplayDataQualityFailure(
                            event.event_id,
                            "missing_funding_cap_floor_reference",
                            symbol,
                        ),
                        failures,
                    )
                    continue
                if not (
                    reference_market.funding_floor
                    <= event.raw_rate.value
                    <= reference_market.funding_cap
                ):
                    self._record_failure(
                        ReplayDataQualityFailure(
                            event.event_id,
                            "funding_rate_outside_exchange_bounds",
                            symbol,
                        ),
                        failures,
                    )
                    continue
                funding_key = (
                    symbol,
                    settlement_time,
                    event.direction,
                )
                if settlement_time > available_at:
                    self._record_failure(
                        ReplayDataQualityFailure(
                            event.event_id,
                            "settlement_available_before_exchange_event",
                            event.symbol.strip().upper(),
                        ),
                        failures,
                    )
                    continue
                if funding_key in seen_funding_effects:
                    duplicates += 1
                    continue
                seen_funding_effects.add(funding_key)
                sign = 1.0 if event.direction == "long_spot_short_perp" else -1.0
                cashflow = (
                    event.raw_rate.cashflow_usd(
                        event.liable_leg_notional,
                        direction_sign=sign,
                    )
                    if event.eligible
                    else 0.0
                )
                cashflows.append(
                    ReplaySettlementCashflow(
                        event_id=event.event_id,
                        symbol=event.symbol.strip().upper(),
                        settlement_time=settlement_time,
                        raw_rate=event.raw_rate,
                        reporting_annualized_rate=event.raw_rate.reporting_annualized,
                        cashflow_usd=cashflow,
                    )
                )
                continue

            request = replace(
                event.request,
                surface="replay",
                entry_capacity=None,
                exit_capacity=None,
                spot_spread_bps=None,
                perp_spread_bps=None,
            )
            symbol = request.symbol.strip().upper()
            decision_time = _utc(request.decision_time)
            if decision_time > available_at:
                self._record_failure(
                    ReplayDataQualityFailure(
                        event.event_id,
                        "decision_available_before_decision_time",
                        symbol,
                    ),
                    failures,
                )
                continue
            metadata = market_metadata.get(symbol)
            if metadata is None:
                self._record_failure(
                    ReplayDataQualityFailure(
                        event.event_id,
                        "missing_authoritative_market_metadata",
                        symbol,
                    ),
                    failures,
                )
                request = replace(
                    request,
                    calendar_authoritative=False,
                    calendar_observed_at=None,
                    spot_filters_valid=False,
                    perp_filters_valid=False,
                    filters_observed_at=None,
                    rate_limit_budget=0,
                    collateral_available_usd=0.0,
                    margin_available_usd=0.0,
                    borrow_cost_bps_per_hour=0.0,
                    collateral_cost_bps_per_hour=0.0,
                    liquidation_tail_bps_per_settlement=0.0,
                )
            else:
                metadata_mismatch = ""
                if not math.isclose(
                    float(request.settlement_forecast.interval_hours),
                    metadata.funding_interval_hours,
                    rel_tol=0.0,
                    abs_tol=1e-12,
                ):
                    metadata_mismatch = "funding_interval_metadata_mismatch"
                elif not math.isclose(
                    self.decision_engine.config.spot_taker_fee_pct,
                    metadata.spot_taker_fee_pct,
                    rel_tol=0.0,
                    abs_tol=1e-15,
                ) or not math.isclose(
                    self.decision_engine.config.perp_taker_fee_pct,
                    metadata.perp_taker_fee_pct,
                    rel_tol=0.0,
                    abs_tol=1e-15,
                ):
                    metadata_mismatch = "fee_tier_policy_mismatch"
                if metadata_mismatch:
                    self._record_failure(
                        ReplayDataQualityFailure(
                            event.event_id,
                            metadata_mismatch,
                            symbol,
                        ),
                        failures,
                    )
                metadata_valid = not metadata_mismatch and metadata.listed
                request = replace(
                    request,
                    calendar_authoritative=(
                        metadata_valid and metadata.calendar_authoritative
                    ),
                    calendar_observed_at=metadata.observed_at,
                    spot_filters_valid=(
                        metadata_valid and metadata.spot_filters_valid
                    ),
                    perp_filters_valid=(
                        metadata_valid and metadata.perp_filters_valid
                    ),
                    filters_observed_at=metadata.observed_at,
                    rate_limit_budget=(
                        metadata.rate_limit_budget if metadata_valid else 0
                    ),
                    collateral_available_usd=(
                        metadata.collateral_available_usd if metadata_valid else 0.0
                    ),
                    margin_available_usd=(
                        metadata.margin_available_usd if metadata_valid else 0.0
                    ),
                    borrow_cost_bps_per_hour=metadata.borrow_cost_bps_per_hour,
                    collateral_cost_bps_per_hour=(
                        metadata.collateral_cost_bps_per_hour
                    ),
                    liquidation_tail_bps_per_settlement=(
                        metadata.liquidation_tail_bps_per_settlement
                    ),
                )
            execution_services_ready = not any(
                (service, symbol) in service_outages
                or (service, "") in service_outages
                for service in ("rest", "user_stream", "persistence")
            )
            if not execution_services_ready:
                # The canonical engine's rate-limit/capacity admission gate is
                # the deterministic no-entry representation for explicitly
                # modeled execution-service outages.
                request = replace(request, rate_limit_budget=0)
            pair_ready = all(
                books.get((symbol, market), _ReplayBook()).ready
                for market in ("spot", "perp")
            ) and not (
                ("market_ws", symbol) in service_outages
                or ("market_ws", "") in service_outages
            )
            if pair_ready:
                decision = self.decision_engine.decide(
                    request,
                    depth_tracker=tracker,
                    book_check_time=available_at.timestamp(),
                )
            else:
                decision = self.decision_engine.decide(request)
            decisions.append(decision)
            selection_cycle_id = (
                event.selection_cycle_id.strip()
                or f"decision-time:{decision_time.isoformat()}"
            )
            decisions_by_cycle.setdefault(selection_cycle_id, []).append(decision)
            portfolio_gross_by_cycle.setdefault(selection_cycle_id, []).append(
                float(request.current_portfolio_pair_gross_usd)
            )
            occupied_slots_by_cycle.setdefault(selection_cycle_id, []).append(
                float(request.current_open_slots)
            )
            collateral_by_cycle.setdefault(selection_cycle_id, []).append(
                float(request.collateral_available_usd)
            )
            margin_by_cycle.setdefault(selection_cycle_id, []).append(
                float(request.margin_available_usd)
            )

        selections_list: list[PortfolioSelection] = []
        for cycle_id, cycle_decisions in decisions_by_cycle.items():
            gross_values = portfolio_gross_by_cycle[cycle_id]
            current_pair_gross = (
                max(gross_values)
                if gross_values
                and all(math.isfinite(value) and value >= 0.0 for value in gross_values)
                else math.nan
            )
            occupied_values = occupied_slots_by_cycle[cycle_id]
            occupied_count = (
                int(max(occupied_values))
                if occupied_values
                and all(
                    math.isfinite(value)
                    and value >= 0.0
                    and value.is_integer()
                    for value in occupied_values
                )
                else self.decision_engine.config.effective_max_slots
            )
            collateral_values = collateral_by_cycle[cycle_id]
            margin_values = margin_by_cycle[cycle_id]
            selections_list.append(
                self.decision_engine.select_entries(
                    cycle_decisions,
                    open_symbols=tuple(
                        f"__REPLAY_OCCUPIED_SLOT_{index}"
                        for index in range(occupied_count)
                    ),
                    current_portfolio_pair_gross_usd=current_pair_gross,
                    available_collateral_usd=(
                        min(collateral_values)
                        if collateral_values
                        and all(
                            math.isfinite(value) and value >= 0.0
                            for value in collateral_values
                        )
                        else math.nan
                    ),
                    available_margin_usd=(
                        min(margin_values)
                        if margin_values
                        and all(
                            math.isfinite(value) and value >= 0.0
                            for value in margin_values
                        )
                        else math.nan
                    ),
                )
            )
        selections = tuple(selections_list)

        execution_cycles = tuple(
            ReplayExecutionCycle(
                cycle_id=cycle_id,
                symbol=context.symbol,
                operation=context.operation,
                reservation_id=context.reservation_id,
                safe_to_project_complete=context.state.safe_to_project_complete,
                mismatch_quantity=str(context.state.mismatch_quantity),
                unhedged_notional_ms=str(context.state.risk_notional_ms),
                last_queue_ahead_quantity=context.last_queue_ahead_quantity,
                breaches=tuple(context.state.breaches),
                snapshot=context.state.to_snapshot(),
            )
            for cycle_id, context in sorted(cycles.items())
        )
        funding_usd = sum(item.cashflow_usd for item in cashflows)
        total_usd = (
            funding_usd
            - fee_cost_usd
            - spread_cost_usd
            - impact_cost_usd
            - adverse_markout_usd
            - operational_loss_usd
        )

        return ReplayResult(
            decisions=tuple(decisions),
            selections=selections,
            settlement_cashflows=tuple(cashflows),
            data_quality_failures=tuple(failures),
            processed_events=processed,
            duplicate_events=duplicates,
            execution_cycles=execution_cycles,
            execution_latency_samples=tuple(latency_samples),
            operational_blocks=tuple(operational_blocks),
            pnl_attribution=ReplayPnlAttribution(
                funding_usd=funding_usd,
                fees_usd=fee_cost_usd,
                spread_usd=spread_cost_usd,
                impact_usd=impact_cost_usd,
                adverse_markout_usd=adverse_markout_usd,
                operational_usd=operational_loss_usd,
                total_usd=total_usd,
            ),
        )

    def run_validated(
        self,
        events: Iterable[ReplayEvent],
        *,
        manifest: ReplayDatasetManifest,
        dataset_root: str | Path,
    ) -> ReplayResult:
        """Replay only after the immutable dataset manifest is verified."""

        failures = manifest.verify_files(dataset_root)
        if failures:
            raise ValueError(
                "replay dataset manifest verification failed: "
                + ", ".join(failures)
            )
        materialized_events = tuple(events)
        manifest_symbols = set(manifest.symbols)
        range_start = _utc(manifest.range_start)
        range_end = _utc(manifest.range_end)
        retrieved_at = _utc(manifest.retrieved_at)
        event_failures: list[str] = []
        for event in materialized_events:
            if isinstance(event, DecisionReplayEvent):
                symbol = event.request.symbol.strip().upper()
                event_time = _utc(event.request.decision_time)
            elif isinstance(event, FundingSettlementReplayEvent):
                symbol = event.symbol.strip().upper()
                event_time = _utc(event.settlement_time)
            elif isinstance(
                event,
                BookReplayEvent
                | MarketMetadataReplayEvent
                | ReferenceMarketReplayEvent,
            ):
                symbol = event.symbol.strip().upper()
                event_time = _utc(event.event_time)
            elif isinstance(event, ExecutionCycleStartReplayEvent):
                symbol = event.symbol.strip().upper()
                event_time = _utc(event.decision_time)
            elif isinstance(event, ExecutionLegReplayEvent):
                symbol = event.symbol.strip().upper()
                event_time = datetime.fromtimestamp(
                    int(event.update.event_time_ms) / 1_000.0,
                    tz=timezone.utc,
                )
            else:
                symbol = event.symbol.strip().upper()
                event_time = _utc(event.available_at)
            if symbol and symbol not in manifest_symbols:
                event_failures.append(
                    f"symbol_not_in_manifest:{event.event_id}:{symbol}"
                )
            if event_time < range_start or event_time > range_end:
                event_failures.append(
                    f"event_outside_manifest_range:{event.event_id}"
                )
            if _utc(event.available_at) > retrieved_at:
                event_failures.append(
                    f"event_available_after_retrieval:{event.event_id}"
                )
        if event_failures:
            raise ValueError(
                "replay events violate dataset manifest: "
                + ", ".join(event_failures)
            )
        result = self.run(materialized_events)
        return replace(result, manifest_hash=manifest.manifest_hash)


__all__ = [
    "BookReplayEvent",
    "DecisionReplayEvent",
    "EventReplay",
    "EventReplayConfig",
    "ExecutionCycleStartReplayEvent",
    "ExecutionLegReplayEvent",
    "FundingSettlementReplayEvent",
    "MarketMetadataReplayEvent",
    "OutageReplayEvent",
    "ReferenceMarketReplayEvent",
    "ReplayDataQualityError",
    "ReplayDataQualityFailure",
    "ReplayDatasetManifest",
    "ReplayEvent",
    "ReplayExecutionCycle",
    "ReplayExecutionLatency",
    "ReplayPnlAttribution",
    "ReplayResult",
    "ReplaySettlementCashflow",
    "ServiceStateReplayEvent",
]
