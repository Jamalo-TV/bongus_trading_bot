from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bongus.market_data.feed_recovery import (
    ExchangeConditionClassifier,
    FeedCursorStore,
    FeedSource,
    FeedState,
    RateLimitBudget,
    evaluate_exchange_clock,
)


NOW = datetime(2026, 7, 18, tzinfo=timezone.utc)


def test_gap_is_durable_scoped_and_requires_complete_contiguous_backfill(tmp_path) -> None:
    path = str(tmp_path / "feed.db")
    source = FeedSource("binance", "depth", "BTCUSDT")
    other = FeedSource("binance", "depth", "ETHUSDT")
    store = FeedCursorStore(path)
    assert store.ingest(source, 10, now=NOW).accepted
    assert store.ingest(other, 50, now=NOW).accepted
    gap = store.ingest(source, 14, now=NOW)
    assert gap.state is FeedState.GAPPED
    assert (gap.missing_from, gap.missing_to) == (11, 13)
    assert not store.apply_backfill(source, [11, 13], now=NOW).accepted
    store.close()

    restored = FeedCursorStore(path)
    assert restored.snapshot(source)[0]["state"] == FeedState.GAPPED.value
    applied = restored.apply_backfill(source, [11, 12, 13], now=NOW)
    assert applied.accepted and applied.sequence == 13
    assert restored.snapshot(other)[0]["state"] == FeedState.READY.value
    assert restored.ingest(source, 14, now=NOW).accepted
    restored.close()


def test_duplicate_regression_has_no_cursor_effect_and_backfill_is_bounded(tmp_path) -> None:
    store = FeedCursorStore(str(tmp_path / "feed.db"), max_backfill_events=2)
    source = FeedSource("binance", "orders", "BTCUSDT")
    store.ingest(source, 1, now=NOW)
    duplicate = store.ingest(source, 1, now=NOW)
    assert duplicate.duplicate and not duplicate.accepted
    store.ingest(source, 5, now=NOW)
    with pytest.raises(ValueError, match="safety bound"):
        store.apply_backfill(source, [2, 3, 4], now=NOW)
    store.close()


def test_rate_limit_budget_honours_retry_after_without_storm() -> None:
    budget = RateLimitBudget(capacity=2, refill_per_second=1)
    assert budget.acquire(now=NOW)
    assert budget.acquire(now=NOW)
    assert not budget.acquire(now=NOW)
    budget.impose_retry_after(10, now=NOW)
    assert not budget.acquire(now=NOW + timedelta(seconds=5))
    assert budget.acquire(now=NOW + timedelta(seconds=10))


def test_api_conditions_and_clock_sync_are_fail_closed() -> None:
    limited = ExchangeConditionClassifier.classify(status_code=429, retry_after_seconds=7, now=NOW)
    assert limited.state is FeedState.THROTTLED
    assert limited.retry_at == NOW + timedelta(seconds=7)
    maintenance = ExchangeConditionClassifier.classify(status_code=503, message="maintenance", now=NOW)
    assert maintenance.state is FeedState.MAINTENANCE
    changed = ExchangeConditionClassifier.classify(
        status_code=400, exchange_code=-1013, message="LOT_SIZE", symbol="BTCUSDT", now=NOW
    )
    assert changed.state is FeedState.FILTER_CHANGED and changed.symbol_scoped
    safe = evaluate_exchange_clock(local_send_ms=1000, local_receive_ms=1100, exchange_time_ms=1050)
    assert safe.safe and safe.offset_ms == 0
    unsafe = evaluate_exchange_clock(local_send_ms=1000, local_receive_ms=1100, exchange_time_ms=5000)
    assert not unsafe.safe and unsafe.reason == "clock_offset_exceeds_limit"


@pytest.mark.parametrize("status_code", [500, 501, 502, 503, 504, 505, 599])
def test_every_5xx_is_global_bounded_backoff(status_code: int) -> None:
    condition = ExchangeConditionClassifier.classify(status_code=status_code, now=NOW)

    assert condition.state is FeedState.MAINTENANCE
    assert condition.reason_code == "exchange_server_error"
    assert condition.retry_at == NOW + timedelta(seconds=30)
    assert not condition.symbol_scoped


def test_ip_ban_uses_longer_global_backoff() -> None:
    condition = ExchangeConditionClassifier.classify(status_code=418, now=NOW)

    assert condition.state is FeedState.THROTTLED
    assert condition.reason_code == "ip_banned"
    assert condition.retry_at == NOW + timedelta(seconds=300)
    assert not condition.symbol_scoped


def test_filter_change_is_detected_by_metadata_hash(tmp_path) -> None:
    store = FeedCursorStore(str(tmp_path / "feed.db"))
    source = FeedSource("binance", "exchange_info", "BTCUSDT")
    assert store.classify_metadata(source, {"tick": "0.1"}, now=NOW) is None
    assert store.classify_metadata(source, {"tick": "0.1"}, now=NOW) is None
    change = store.classify_metadata(source, {"tick": "0.01"}, now=NOW)
    assert change is not None and change.state is FeedState.FILTER_CHANGED
    store.close()


def test_ranged_depth_gap_never_invents_plus_one_events_and_requires_proof(tmp_path) -> None:
    path = str(tmp_path / "feed.db")
    source = FeedSource("binance", "depth_perp", "BTCUSDT")
    store = FeedCursorStore(path)
    gap = store.record_gap(
        source,
        prior_sequence=100,
        first_sequence=105,
        final_sequence=110,
        previous_final_sequence=104,
        reason="depth_sequence_gap",
        now=NOW,
    )
    assert gap.state is FeedState.GAPPED
    row = store.snapshot(source)[0]
    assert row["gap_from"] is None
    assert row["gap_to"] is None
    store.close()

    restored = FeedCursorStore(path)
    rejected = restored.record_readiness_proof(
        source,
        final_sequence=120,
        contiguous=False,
        is_snapshot=False,
        now=NOW + timedelta(seconds=1),
    )
    assert not rejected.accepted
    assert restored.snapshot(source)[0]["state"] == FeedState.GAPPED.value

    proven = restored.record_readiness_proof(
        source,
        first_sequence=111,
        final_sequence=120,
        previous_final_sequence=110,
        contiguous=True,
        now=NOW + timedelta(seconds=2),
    )
    assert proven.accepted and proven.state is FeedState.READY
    assert restored.snapshot(source)[0]["state"] == FeedState.READY.value
    restored.close()


def test_untradable_source_retirement_is_durable_but_does_not_grant_readiness(tmp_path) -> None:
    path = str(tmp_path / "feed.db")
    source = FeedSource("binance", "depth_spot", "NILUSDT")
    store = FeedCursorStore(path)
    store.record_gap(
        source,
        prior_sequence=100,
        first_sequence=105,
        final_sequence=110,
        reason="depth_sequence_gap",
        now=NOW,
    )

    assert store.retire_source(source, now=NOW + timedelta(seconds=1))
    row = store.snapshot(source)[0]
    assert row["state"] == FeedState.COLD.value
    assert row["last_sequence"] is None
    assert not store.retire_source(source, now=NOW + timedelta(seconds=2))
    store.close()

    restored = FeedCursorStore(path)
    row = restored.snapshot(source)[0]
    assert row["state"] == FeedState.COLD.value
    assert row["last_sequence"] is None
    restored.close()
