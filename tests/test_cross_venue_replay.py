from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from bongus.research.cross_venue.replay import (
    FixtureReplay,
    FixtureReplayError,
    load_raw_snapshot_fixture,
)
from bongus.research.cross_venue.schema import CanonicalAsset, Venue
from bongus.research.cross_venue.storage import ResearchStore

FIXTURE = Path(__file__).parent / "fixtures" / "cross_venue" / "raw_snapshots.json"


def test_fixture_replay_orders_by_availability_and_never_exposes_future_data() -> None:
    records = load_raw_snapshot_fixture(FIXTURE)
    observations: list[tuple[str, str | None]] = []

    def observe(record, index) -> None:
        hyperliquid = index.latest(
            dataset="funding_quote",
            venue=Venue.HYPERLIQUID,
            canonical_asset=CanonicalAsset.BTC,
            as_of_time_ns=record.available_time_ns,
        )
        observations.append((record.event_id, hyperliquid.event_id if hyperliquid is not None else None))

    replay = FixtureReplay()
    result = replay.run(records, handler=observe)
    repeated = replay.run(records, handler=observe)
    assert result.input_reordered is True
    assert result.processed_events == 2
    assert result.exact_duplicates == 0
    assert result.report_sha256 == repeated.report_sha256
    assert observations[0] == ("fixture-binance-btc-100", None)
    assert observations[1][0] == "fixture-hyperliquid-btc-200"


def test_fixture_replay_persists_only_to_research_database(tmp_path: Path) -> None:
    records = load_raw_snapshot_fixture(FIXTURE)
    with ResearchStore(tmp_path / "research.db") as store:
        result = FixtureReplay().run(records, store=store)
        assert result.processed_events == 2
        assert [item.event_id for item in store.iter_raw_snapshots()] == [
            "fixture-binance-btc-100",
            "fixture-hyperliquid-btc-200",
        ]


def test_fixture_replay_deduplicates_exact_rows_and_rejects_conflicts() -> None:
    records = load_raw_snapshot_fixture(FIXTURE)
    result = FixtureReplay().run(records + (records[0],))
    assert result.processed_events == 2
    assert result.exact_duplicates == 1
    with pytest.raises(FixtureReplayError, match="conflicting duplicate"):
        FixtureReplay().run(records + (replace(records[0], payload_bytes=b'{"different":"payload"}'),))


def test_fixture_record_requires_receive_and_availability_causality() -> None:
    record = load_raw_snapshot_fixture(FIXTURE)[0]
    with pytest.raises(ValueError, match="capture <= receive"):
        replace(record, receive_time_ns=record.capture_time_ns - 1)


def test_fixture_content_hash_detects_payload_tampering(tmp_path: Path) -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["snapshots"][0]["payload"]["funding"] = "0.9"
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(FixtureReplayError, match="content hash mismatch"):
        load_raw_snapshot_fixture(tampered)
