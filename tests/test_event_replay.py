from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib

import pytest

from bongus.core.config import TAKER_FEE_PERP, TAKER_FEE_SPOT
from bongus.domain.units import LegNotionalUsd, RawSettlementRate
from bongus.engine.leg_state_machine import Leg, LegStatus, LegUpdate
from bongus.market_data.settlement_model import (
    SettlementForecast,
    SettlementPaymentForecast,
)
from bongus.research.event_replay import (
    BookReplayEvent,
    DecisionReplayEvent,
    EventReplay,
    EventReplayConfig,
    ExecutionCycleStartReplayEvent,
    ExecutionLegReplayEvent,
    FundingSettlementReplayEvent,
    MarketMetadataReplayEvent,
    ReferenceMarketReplayEvent,
    ReplayDataQualityError,
    ReplayDatasetManifest,
    ServiceStateReplayEvent,
)
from bongus.strategies.decision_engine import DecisionEngine, DecisionRequest


UTC = timezone.utc


def _request(now: datetime, *, surface: str = "paper") -> DecisionRequest:
    payments = tuple(
        SettlementPaymentForecast(
            symbol="BTCUSDT",
            settlement_time=now + timedelta(hours=8 * (index + 1)),
            mean_rate=0.004,
            standard_deviation=0.0005,
            lower_rate=0.003,
            upper_rate=0.005,
            favourable_sign_probability=0.99,
            expected_payment_usd=10.0,
            lower_payment_usd=7.5,
        )
        for index in range(2)
    )
    forecast = SettlementForecast(
        symbol="BTCUSDT",
        decision_time=now,
        direction="long_spot_short_perp",
        interval_hours=8,
        sample_count=100,
        latest_input_time=now,
        payments=payments,
        valid=True,
    )
    return DecisionRequest(
        symbol="BTCUSDT",
        decision_time=now,
        direction="long_spot_short_perp",
        requested_leg_notional_usd=2_500.0,
        settlement_forecast=forecast,
        surface=surface,  # type: ignore[arg-type]
        forecast_confidence=0.95,
        calendar_authoritative=True,
        calendar_observed_at=now,
        spot_filters_valid=True,
        perp_filters_valid=True,
        filters_observed_at=now,
        rate_limit_budget=20,
        collateral_available_usd=2_500.0,
        margin_available_usd=1_250.0,
    )


def _snapshot(
    *, event_id: str, market: str, at: datetime, update_id: int
) -> BookReplayEvent:
    if market == "spot":
        bids = ((99.9, 100.0),)
        asks = ((100.0, 100.0),)
    else:
        bids = ((100.1, 100.0),)
        asks = ((100.2, 100.0),)
    return BookReplayEvent(
        event_id=event_id,
        symbol="BTCUSDT",
        market=market,  # type: ignore[arg-type]
        event_time=at,
        available_at=at,
        update_kind="snapshot",
        bids=bids,
        asks=asks,
        final_update_id=update_id,
    )


def _metadata(
    at: datetime, **overrides: object
) -> MarketMetadataReplayEvent:
    values: dict[str, object] = {
        "event_id": "metadata-1",
        "symbol": "BTCUSDT",
        "event_time": at,
        "available_at": at,
        "listed": True,
        "calendar_authoritative": True,
        "funding_interval_hours": 8.0,
        "spot_filters_valid": True,
        "perp_filters_valid": True,
        "spot_filter_version": "spot-exchange-info-1",
        "perp_filter_version": "perp-exchange-info-1",
        "rate_limit_budget": 20,
        "collateral_available_usd": 2_500.0,
        "margin_available_usd": 1_250.0,
        "spot_taker_fee_pct": TAKER_FEE_SPOT,
        "perp_taker_fee_pct": TAKER_FEE_PERP,
    }
    values.update(overrides)
    return MarketMetadataReplayEvent(**values)  # type: ignore[arg-type]


def _reference(at: datetime, *, event_id: str = "reference-1") -> ReferenceMarketReplayEvent:
    return ReferenceMarketReplayEvent(
        event_id=event_id,
        symbol="BTCUSDT",
        event_time=at,
        available_at=at,
        trade_price=100.0,
        mark_price=100.1,
        index_price=100.0,
        premium_index=0.001,
        funding_cap=0.01,
        funding_floor=-0.01,
    )


def test_replay_uses_same_engine_decision_and_deduplicates_effects() -> None:
    book_time = datetime(2026, 1, 1, tzinfo=UTC)
    decision_time = book_time + timedelta(seconds=1)
    engine = DecisionEngine()
    request = _request(decision_time)
    replay = EventReplay(engine)
    funding_event = FundingSettlementReplayEvent(
        event_id="funding-1",
        symbol="BTCUSDT",
        settlement_time=decision_time,
        available_at=decision_time,
        raw_rate=RawSettlementRate(0.001),
        liable_leg_notional=LegNotionalUsd(2_500.0),
        direction="long_spot_short_perp",
    )
    events = [
        _metadata(book_time),
        _reference(book_time),
        _snapshot(event_id="spot-1", market="spot", at=book_time, update_id=10),
        _snapshot(event_id="perp-1", market="perp", at=book_time, update_id=20),
        DecisionReplayEvent("decision-1", decision_time, request),
        funding_event,
        funding_event,
        replace(funding_event, event_id="funding-redelivery"),
    ]
    result = replay.run(events)
    assert len(result.decisions) == 1
    assert len(result.selections) == 1
    assert result.selections[0].selected == result.decisions
    assert result.decisions[0].eligible
    assert result.decisions[0].config_hash == engine.config_hash
    assert result.duplicate_events == 2
    assert len(result.settlement_cashflows) == 1
    assert result.settlement_cashflows[0].cashflow_usd == pytest.approx(2.5)
    assert result.settlement_cashflows[0].reporting_annualized_rate.value == pytest.approx(
        1.095
    )


def test_execution_replay_enforces_exit_first_rotation_and_reconciles_partial_legs() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)

    def cycle_start(
        event_id: str,
        cycle_id: str,
        at: datetime,
        *,
        operation: str,
        rotation_role: str,
    ) -> ExecutionCycleStartReplayEvent:
        return ExecutionCycleStartReplayEvent(
            event_id=event_id,
            cycle_id=cycle_id,
            symbol="BTCUSDT",
            decision_time=at,
            ipc_time=at + timedelta(milliseconds=10),
            rest_send_time=at + timedelta(milliseconds=20),
            ack_time=at + timedelta(milliseconds=30),
            available_at=at + timedelta(milliseconds=30),
            operation=operation,  # type: ignore[arg-type]
            direction="long_spot_short_perp",
            target_quantity="1",
            reservation_id=f"reservation-{cycle_id}",
            collateral_reserved_usd=100.0,
            margin_reserved_usd=50.0,
            starting_spot_quantity=("1" if operation == "exit" else "0"),
            starting_perp_quantity=("-1" if operation == "exit" else "0"),
            rotation_id="rotation-1",
            rotation_role=rotation_role,  # type: ignore[arg-type]
        )

    premature_entry = cycle_start(
        "entry-too-early",
        "entry-cycle",
        start,
        operation="entry",
        rotation_role="entry",
    )
    with pytest.raises(ReplayDataQualityError, match="rotation_entry_before_exit_confirmed"):
        EventReplay(DecisionEngine()).run([premature_entry])

    exit_start = cycle_start(
        "exit-start",
        "exit-cycle",
        start,
        operation="exit",
        rotation_role="exit",
    )
    spot_exit_at = start + timedelta(milliseconds=100)
    perp_exit_at = start + timedelta(milliseconds=200)
    spot_exit = ExecutionLegReplayEvent(
        event_id="exit-spot-filled",
        cycle_id="exit-cycle",
        symbol="BTCUSDT",
        available_at=spot_exit_at,
        update=LegUpdate(
            event_id="exit-spot-filled",
            leg=Leg.SPOT,
            status=LegStatus.FILLED,
            cumulative_quantity="1",
            event_time_ms=int(spot_exit_at.timestamp() * 1_000),
            sequence=1,
            exchange_verified=True,
        ),
        mark_price=100.0,
        fill_price=99.9,
        fee_usd=0.10,
    )
    perp_exit = ExecutionLegReplayEvent(
        event_id="exit-perp-filled",
        cycle_id="exit-cycle",
        symbol="BTCUSDT",
        available_at=perp_exit_at,
        update=LegUpdate(
            event_id="exit-perp-filled",
            leg=Leg.PERP,
            status=LegStatus.FILLED,
            cumulative_quantity="1",
            event_time_ms=int(perp_exit_at.timestamp() * 1_000),
            sequence=1,
            exchange_verified=True,
        ),
        mark_price=100.0,
        fill_price=100.1,
        fee_usd=0.10,
    )
    entry_at = start + timedelta(milliseconds=300)
    entry_start = cycle_start(
        "entry-start",
        "entry-cycle",
        entry_at,
        operation="entry",
        rotation_role="entry",
    )
    spot_partial_at = entry_at + timedelta(milliseconds=100)
    perp_fill_at = entry_at + timedelta(milliseconds=600)
    spot_fill_at = entry_at + timedelta(milliseconds=1_100)
    spot_partial = ExecutionLegReplayEvent(
        event_id="entry-spot-partial",
        cycle_id="entry-cycle",
        symbol="BTCUSDT",
        available_at=spot_partial_at,
        update=LegUpdate(
            event_id="entry-spot-partial",
            leg=Leg.SPOT,
            status=LegStatus.PARTIAL,
            cumulative_quantity="0.4",
            event_time_ms=int(spot_partial_at.timestamp() * 1_000),
            sequence=1,
            exchange_verified=False,
        ),
        mark_price=100.0,
        fill_price=100.0,
        queue_ahead_quantity=0.6,
        fee_usd=0.04,
        spread_cost_usd=0.01,
    )
    perp_fill = ExecutionLegReplayEvent(
        event_id="entry-perp-filled",
        cycle_id="entry-cycle",
        symbol="BTCUSDT",
        available_at=perp_fill_at,
        update=LegUpdate(
            event_id="entry-perp-filled",
            leg=Leg.PERP,
            status=LegStatus.FILLED,
            cumulative_quantity="1",
            event_time_ms=int(perp_fill_at.timestamp() * 1_000),
            sequence=1,
            exchange_verified=True,
        ),
        mark_price=100.0,
        fill_price=100.1,
        fee_usd=0.10,
        impact_cost_usd=0.02,
    )
    spot_fill = ExecutionLegReplayEvent(
        event_id="entry-spot-filled",
        cycle_id="entry-cycle",
        symbol="BTCUSDT",
        available_at=spot_fill_at,
        update=LegUpdate(
            event_id="entry-spot-filled",
            leg=Leg.SPOT,
            status=LegStatus.FILLED,
            cumulative_quantity="1",
            event_time_ms=int(spot_fill_at.timestamp() * 1_000),
            sequence=2,
            exchange_verified=True,
        ),
        mark_price=100.0,
        fill_price=100.0,
        fee_usd=0.06,
        adverse_markout_usd=0.03,
    )

    result = EventReplay(DecisionEngine()).run(
        [
            exit_start,
            spot_exit,
            perp_exit,
            entry_start,
            spot_partial,
            perp_fill,
            spot_fill,
        ]
    )

    assert len(result.execution_cycles) == 2
    assert all(cycle.safe_to_project_complete for cycle in result.execution_cycles)
    entry_cycle = next(
        cycle for cycle in result.execution_cycles if cycle.cycle_id == "entry-cycle"
    )
    assert Decimal(entry_cycle.unhedged_notional_ms) > 0
    assert entry_cycle.last_queue_ahead_quantity == 0.0
    assert result.execution_latency_samples[0].decision_to_ack_ms == pytest.approx(30.0)
    assert result.pnl_attribution.fees_usd == pytest.approx(0.40)
    assert result.pnl_attribution.total_usd == pytest.approx(-0.46)


def test_explicit_user_stream_outage_drops_stream_update_until_backfill() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    cycle = ExecutionCycleStartReplayEvent(
        event_id="entry-start",
        cycle_id="entry-cycle",
        symbol="BTCUSDT",
        available_at=start + timedelta(milliseconds=30),
        decision_time=start,
        ipc_time=start + timedelta(milliseconds=10),
        rest_send_time=start + timedelta(milliseconds=20),
        ack_time=start + timedelta(milliseconds=30),
        operation="entry",
        direction="long_spot_short_perp",
        target_quantity="1",
        reservation_id="reservation-1",
        collateral_reserved_usd=100.0,
        margin_reserved_usd=50.0,
    )
    outage_at = start + timedelta(milliseconds=40)
    dropped = ExecutionLegReplayEvent(
        event_id="stream-spot-fill",
        cycle_id="entry-cycle",
        symbol="BTCUSDT",
        available_at=start + timedelta(milliseconds=50),
        update=LegUpdate(
            event_id="stream-spot-fill",
            leg=Leg.SPOT,
            status=LegStatus.FILLED,
            cumulative_quantity="1",
            event_time_ms=int((start + timedelta(milliseconds=45)).timestamp() * 1_000),
            exchange_verified=True,
            source="stream",
        ),
        mark_price=100.0,
        fill_price=100.0,
    )
    result = EventReplay(DecisionEngine()).run(
        [
            cycle,
            ServiceStateReplayEvent(
                event_id="user-stream-down",
                available_at=outage_at,
                service="user_stream",
                available=False,
            ),
            dropped,
        ]
    )
    assert result.operational_blocks == (
        "stream-spot-fill:user_stream_unavailable",
    )
    assert not result.execution_cycles[0].safe_to_project_complete


def test_sequence_gap_stops_validation_unless_explicitly_modelled() -> None:
    at = datetime(2026, 1, 1, tzinfo=UTC)
    gap = BookReplayEvent(
        event_id="spot-gap",
        symbol="BTCUSDT",
        market="spot",
        event_time=at + timedelta(seconds=1),
        available_at=at + timedelta(seconds=1),
        update_kind="diff",
        bids=((99.8, 100.0),),
        asks=(),
        first_update_id=12,
        final_update_id=12,
        previous_final_update_id=10,
    )
    with pytest.raises(ReplayDataQualityError, match="book_sequence_gap"):
        EventReplay(DecisionEngine()).run(
            [_snapshot(event_id="spot-1", market="spot", at=at, update_id=10), gap]
        )

    decision_time = at + timedelta(seconds=2)
    result = EventReplay(
        DecisionEngine(),
        config=EventReplayConfig(model_data_quality_failures_as_outages=True),
    ).run(
        [
            _metadata(at),
            _snapshot(event_id="spot-1", market="spot", at=at, update_id=10),
            _snapshot(event_id="perp-1", market="perp", at=at, update_id=20),
            gap,
            DecisionReplayEvent("decision-1", decision_time, _request(decision_time)),
        ]
    )
    assert result.data_quality_failures[0].reason == "book_sequence_gap"
    assert not result.decisions[0].eligible
    assert "missing_entry_paired_books" in result.decisions[0].reason_codes


def test_replay_overrides_surface_without_changing_decision_reasons() -> None:
    book_time = datetime(2026, 1, 1, tzinfo=UTC)
    decision_time = book_time + timedelta(seconds=1)
    engine = DecisionEngine()
    base = _request(decision_time, surface="live")
    events = [
        _metadata(book_time),
        _snapshot(event_id="spot-1", market="spot", at=book_time, update_id=1),
        _snapshot(event_id="perp-1", market="perp", at=book_time, update_id=1),
    ]
    live_label = EventReplay(engine).run(
        [*events, DecisionReplayEvent("decision-live", decision_time, base)]
    ).decisions[0]
    replay_label = EventReplay(engine).run(
        [
            *events,
            DecisionReplayEvent(
                "decision-replay",
                decision_time,
                replace(base, surface="replay"),
            ),
        ]
    ).decisions[0]
    assert live_label == replay_label


def test_replay_market_metadata_is_authoritative_and_delisting_blocks_entry() -> None:
    book_time = datetime(2026, 1, 1, tzinfo=UTC)
    decision_time = book_time + timedelta(seconds=1)
    request = _request(decision_time)
    result = EventReplay(DecisionEngine()).run(
        [
            _metadata(book_time, listed=False),
            _snapshot(event_id="spot-1", market="spot", at=book_time, update_id=1),
            _snapshot(event_id="perp-1", market="perp", at=book_time, update_id=1),
            DecisionReplayEvent("decision-delisted", decision_time, request),
        ]
    )

    assert not result.decisions[0].eligible
    assert "missing_authoritative_calendar" in result.decisions[0].reason_codes
    assert "spot_filters_unavailable" in result.decisions[0].reason_codes
    assert "no_executable_account_capacity" in result.decisions[0].reason_codes


def test_missing_or_incompatible_market_metadata_fails_validation() -> None:
    book_time = datetime(2026, 1, 1, tzinfo=UTC)
    decision_time = book_time + timedelta(seconds=1)
    decision = DecisionReplayEvent(
        "decision-1",
        decision_time,
        _request(decision_time),
    )
    books = [
        _snapshot(event_id="spot-1", market="spot", at=book_time, update_id=1),
        _snapshot(event_id="perp-1", market="perp", at=book_time, update_id=1),
    ]

    with pytest.raises(ReplayDataQualityError, match="missing_authoritative_market_metadata"):
        EventReplay(DecisionEngine()).run([*books, decision])

    explicit_outage = EventReplay(
        DecisionEngine(),
        config=EventReplayConfig(model_data_quality_failures_as_outages=True),
    ).run([*books, decision])
    assert not explicit_outage.decisions[0].eligible
    assert explicit_outage.data_quality_failures[0].reason == (
        "missing_authoritative_market_metadata"
    )

    with pytest.raises(ReplayDataQualityError, match="fee_tier_policy_mismatch"):
        EventReplay(DecisionEngine()).run(
            [
                _metadata(book_time, spot_taker_fee_pct=TAKER_FEE_SPOT * 2.0),
                *books,
                decision,
            ]
        )


def test_manifest_hash_and_file_verification_are_deterministic(tmp_path) -> None:
    data = tmp_path / "btc.bin"
    data.write_bytes(b"causal-data")
    digest = hashlib.sha256(data.read_bytes()).hexdigest()
    now = datetime(2026, 1, 2, tzinfo=UTC)
    manifest = ReplayDatasetManifest(
        symbols=("BTCUSDT",),
        venue_contracts={"BTCUSDT": "BINANCE:BTCUSDT-PERP"},
        source="fixture",
        retrieved_at=now,
        range_start=now - timedelta(days=1),
        range_end=now,
        cadence="event",
        universe_construction="point-in-time listed symbols",
        listing_delisting_treatment="explicit listing intervals",
        file_sha256={"btc.bin": digest},
    )
    same = replace(manifest)
    assert manifest.manifest_hash == same.manifest_hash
    assert manifest.verify_files(tmp_path) == ()
    validated = EventReplay(DecisionEngine()).run_validated(
        [], manifest=manifest, dataset_root=tmp_path
    )
    assert validated.manifest_hash == manifest.manifest_hash
    with pytest.raises(ValueError, match="symbol_not_in_manifest"):
        EventReplay(DecisionEngine()).run_validated(
            [
                DecisionReplayEvent(
                    "foreign-symbol",
                    now,
                    replace(_request(now), symbol="ETHUSDT"),
                )
            ],
            manifest=manifest,
            dataset_root=tmp_path,
        )
    with pytest.raises(ValueError, match="event_outside_manifest_range"):
        EventReplay(DecisionEngine()).run_validated(
            [
                _snapshot(
                    event_id="out-of-range",
                    market="spot",
                    at=now + timedelta(seconds=1),
                    update_id=1,
                )
            ],
            manifest=manifest,
            dataset_root=tmp_path,
        )
    data.write_bytes(b"changed")
    assert manifest.verify_files(tmp_path) == ("hash_mismatch:btc.bin",)
    with pytest.raises(ValueError, match="manifest verification failed"):
        EventReplay(DecisionEngine()).run_validated(
            [], manifest=manifest, dataset_root=tmp_path
        )
