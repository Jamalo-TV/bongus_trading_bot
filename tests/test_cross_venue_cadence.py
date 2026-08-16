from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from bongus.research.cross_venue.cadence import (
    COLLECTION_CADENCE,
    NANOSECONDS_PER_HOUR,
    NANOSECONDS_PER_SECOND,
    CadenceAnchor,
    CadenceObservation,
    DecisionAnchorEvidence,
    audit_cadence,
    audit_decision_anchors,
    cadence_for_dataset,
    reconcile_finalized_funding,
)
from bongus.research.cross_venue.schema import (
    CanonicalAsset,
    FundingPriceKind,
    FundingSettlement,
    Venue,
)

BASE_TIME_NS = 1_700_000_000_000_000_000


def _settlement(
    *,
    event_id: str = "funding-1",
    rate: str = "0.0001",
    venue: Venue = Venue.BINANCE,
) -> FundingSettlement:
    return FundingSettlement(
        event_id=event_id,
        venue=venue,
        canonical_asset=CanonicalAsset.BTC,
        contract_id=("binance:BTCUSDT:perpetual" if venue is Venue.BINANCE else "hyperliquid:BTC:core-perpetual"),
        settlement_time_ns=BASE_TIME_NS,
        available_time_ns=BASE_TIME_NS + NANOSECONDS_PER_SECOND,
        rate=Decimal(rate),
        settlement_price=Decimal("40000"),
        price_kind=(FundingPriceKind.MARK if venue is Venue.BINANCE else FundingPriceKind.ORACLE),
    )


def test_preregistered_collection_cadence_is_complete_and_exact() -> None:
    assert cadence_for_dataset("bbo").normal_interval_ns == NANOSECONDS_PER_SECOND
    top20 = cadence_for_dataset("top20_book")
    assert top20.normal_interval_ns == 30 * NANOSECONDS_PER_SECOND
    assert top20.burst_interval_ns == NANOSECONDS_PER_SECOND
    assert top20.burst_window_ns == 5 * 60 * NANOSECONDS_PER_SECOND
    assert cadence_for_dataset("final_funding_settlements").mode == "event_driven"
    assert cadence_for_dataset("contract_metadata").on_change is True
    assert cadence_for_dataset("storage_health").normal_interval_ns == 48 * NANOSECONDS_PER_HOUR
    assert len({contract.name for contract in COLLECTION_CADENCE}) == len(COLLECTION_CADENCE)


def test_cadence_audit_preserves_missing_late_and_flagged_anchors() -> None:
    anchors = tuple(
        CadenceAnchor(
            anchor_id=f"anchor-{index}",
            dataset="bbo",
            venue=Venue.BINANCE,
            canonical_asset=CanonicalAsset.BTC,
            scheduled_time_ns=BASE_TIME_NS + index * NANOSECONDS_PER_SECOND,
        )
        for index in range(4)
    )
    observations = (
        CadenceObservation(
            "anchor-0",
            "event-0",
            BASE_TIME_NS,
            BASE_TIME_NS + 1,
            BASE_TIME_NS + 2,
        ),
        CadenceObservation(
            "anchor-1",
            "event-1",
            BASE_TIME_NS + NANOSECONDS_PER_SECOND,
            BASE_TIME_NS + NANOSECONDS_PER_SECOND + 1,
            BASE_TIME_NS + 4 * NANOSECONDS_PER_SECOND,
        ),
        CadenceObservation(
            "anchor-2",
            "event-2",
            BASE_TIME_NS + 2 * NANOSECONDS_PER_SECOND,
            BASE_TIME_NS + 2 * NANOSECONDS_PER_SECOND + 1,
            BASE_TIME_NS + 2 * NANOSECONDS_PER_SECOND + 2,
            ("gap",),
        ),
    )
    report = audit_cadence(anchors, observations)
    assert report.missing_anchor_ids == ("anchor-3",)
    assert report.late_anchor_ids == ("anchor-1",)
    assert report.quality_flagged_anchor_ids == ("anchor-2",)
    assert report.timely_anchors == 1
    assert report.passes_99_percent_gate is False
    assert len(report.report_sha256) == 64


def test_decision_anchor_gate_requires_99_percent_complete_fresh_and_no_future_join() -> None:
    anchors = tuple(
        DecisionAnchorEvidence(
            anchor_id=f"decision-{index}",
            canonical_asset=CanonicalAsset.BTC,
            decision_time_ns=BASE_TIME_NS + index,
            binance_available_time_ns=(BASE_TIME_NS + index - 10 if index < 99 else None),
            hyperliquid_available_time_ns=(BASE_TIME_NS + index - 9 if index < 99 else None),
            freshness_limit_ns=20,
            skew_limit_ns=5,
        )
        for index in range(100)
    )
    report = audit_decision_anchors(anchors)
    assert report.coverage_fraction == Decimal("0.99")
    assert report.fresh_fraction == Decimal("1")
    assert report.passes_data_gate is True

    future = replace(
        anchors[0],
        binance_available_time_ns=anchors[0].decision_time_ns + 1,
    )
    failed = audit_decision_anchors((future,) + anchors[1:])
    assert failed.future_join_anchor_ids == ("decision-0",)
    assert failed.passes_data_gate is False


def test_finalized_funding_reconciliation_is_exact_and_requires_100_percent() -> None:
    history = (_settlement(), _settlement(event_id="funding-2", venue=Venue.HYPERLIQUID))
    exact = reconcile_finalized_funding(history, history)
    assert exact.reconciled_fraction == Decimal("1")
    assert exact.passes_100_percent_gate is True

    changed = replace(history[0], event_id="changed", rate=Decimal("0.0002"))
    failed = reconcile_finalized_funding((changed, history[1]), history)
    assert failed.matched_events == 1
    assert len(failed.mismatched_keys) == 1
    assert failed.passes_100_percent_gate is False


def test_finalized_funding_conflicting_duplicates_are_never_hidden() -> None:
    original = _settlement()
    conflict = replace(original, event_id="other-id", rate=Decimal("0.0003"))
    report = reconcile_finalized_funding((original, conflict), (original,))
    assert len(report.conflicting_collected_keys) == 1
    assert report.passes_100_percent_gate is False
