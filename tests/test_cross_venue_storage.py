from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from bongus.research.cross_venue.schema import CanonicalAsset, ReservedCapital, Venue
from bongus.research.cross_venue.storage import (
    RESEARCH_SCHEMA_VERSION,
    ConflictingResearchEventError,
    OpportunitySnapshot,
    RawSnapshotRecord,
    ResearchDatabasePathError,
    ResearchStorageError,
    ResearchStore,
)


def _raw_snapshot(payload: bytes = b'{"funding":"0.0001"}') -> RawSnapshotRecord:
    return RawSnapshotRecord(
        event_id="raw-btc-1",
        dataset="funding_quote",
        venue=Venue.HYPERLIQUID,
        canonical_asset=CanonicalAsset.BTC,
        venue_symbol="BTC",
        contract_id="core:BTC",
        endpoint="/info",
        request_method="POST",
        source_time_ns=90,
        capture_time_ns=100,
        receive_time_ns=110,
        available_time_ns=120,
        persistence_time_ns=130,
        http_status=200,
        response_headers={"Content-Type": "application/json", "X-Trace": "fixture"},
        payload_bytes=payload,
        code_sha256="0" * 64,
        configuration_sha256="1" * 64,
        sequence_id="fixture-1",
        connection_id="fixture",
    )


def _opportunity() -> OpportunitySnapshot:
    return OpportunitySnapshot(
        event_id="opportunity-btc-1",
        canonical_asset=CanonicalAsset.BTC,
        capture_time_ns=200,
        receive_time_ns=210,
        available_time_ns=220,
        persistence_time_ns=230,
        source_event_ids=("binance-quote-1", "hyperliquid-quote-1"),
        matched_base_quantity=Decimal("0.025"),
        binance_long_entry_price=Decimal("50000"),
        hyperliquid_short_entry_price=Decimal("50001"),
        holding_period_days=Decimal("30"),
        expected_funding_pnl_usd=Decimal("12"),
        expected_executable_price_pnl_usd=Decimal("-1"),
        expected_commissions_usd=Decimal("2"),
        stablecoin_conversion_cost_usd=Decimal("0.5"),
        collateral_opportunity_cost_usd=Decimal("0.25"),
        repair_failure_cost_usd=Decimal("0.25"),
        reserved_capital=ReservedCapital(
            binance_collateral_usd=Decimal("50"),
            hyperliquid_collateral_usd=Decimal("60"),
            liquidation_buffers_usd=Decimal("20"),
            idle_transfer_buffer_usd=Decimal("10"),
        ),
        code_sha256="0" * 64,
        configuration_sha256="1" * 64,
    )


def test_research_store_requires_dedicated_name_and_tracks_migration(tmp_path: Path) -> None:
    with pytest.raises(ResearchDatabasePathError, match="dedicated research.db"):
        ResearchStore(tmp_path / "state.db")

    path = tmp_path / "research.db"
    with ResearchStore(path) as store:
        assert store.schema_version == RESEARCH_SCHEMA_VERSION == 1
        migrations = store.migration_metadata()
        assert migrations[0]["name"] == "initial_append_only_research_store"
        assert len(str(migrations[0]["checksum_sha256"])) == 64
        applied_at = migrations[0]["applied_at_unix_ns"]
        assert isinstance(applied_at, int)
        assert applied_at > 0
        with sqlite3.connect(path) as separate:
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                separate.execute("UPDATE research_schema_migrations SET name = 'changed' WHERE version = 1")


def test_raw_snapshots_store_status_headers_hash_and_reject_mutation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "research.db"
    record = _raw_snapshot()
    with ResearchStore(path) as store:
        assert store.append_raw_snapshot(record) is True
        assert store.append_raw_snapshot(record) is False
        row = store.execute_readonly(
            """
            SELECT http_status, response_headers_json, content_sha256
            FROM raw_snapshots WHERE event_id = ?
            """,
            (record.event_id,),
        )[0]
        assert row["http_status"] == 200
        assert json.loads(row["response_headers_json"])["content-type"] == "application/json"
        assert row["content_sha256"] == hashlib.sha256(record.payload_bytes).hexdigest()
        assert tuple(store.iter_raw_snapshots()) == (record,)
        with pytest.raises(ConflictingResearchEventError, match="conflicting"):
            store.append_raw_snapshot(replace(record, payload_bytes=b'{"funding":"0.2"}'))
        with pytest.raises(ResearchStorageError, match="read-only"):
            store.execute_readonly("DELETE FROM raw_snapshots")

        with sqlite3.connect(path) as separate:
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                separate.execute(
                    "UPDATE raw_snapshots SET http_status = 201 WHERE event_id = ?",
                    (record.event_id,),
                )
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                separate.execute("DELETE FROM raw_snapshots WHERE event_id = ?", (record.event_id,))


def test_opportunity_persists_exact_total_reserved_capital_denominator(
    tmp_path: Path,
) -> None:
    record = _opportunity()
    assert record.expected_net_pnl_usd == Decimal("8.00")
    assert record.total_reserved_capital_usd == Decimal("140")
    assert record.expected_return_on_reserved_capital == Decimal("8") / Decimal("140")

    with ResearchStore(tmp_path / "research.db") as store:
        store.append_raw_snapshot(
            replace(
                _raw_snapshot(),
                event_id="binance-quote-1",
                venue=Venue.BINANCE,
                venue_symbol="BTCUSDT",
                contract_id="BTCUSDT:PERPETUAL",
                endpoint="/fapi/v1/premiumIndex",
                request_method="GET",
            )
        )
        store.append_raw_snapshot(replace(_raw_snapshot(), event_id="hyperliquid-quote-1"))
        assert store.append_opportunity_snapshot(record) is True
        stored = tuple(store.iter_opportunity_snapshots())
        assert stored == (record,)
        row = store.execute_readonly(
            """
            SELECT total_reserved_capital_usd, expected_net_pnl_usd,
                   expected_return_on_reserved_capital
            FROM opportunity_snapshots
            """
        )[0]
        assert row["total_reserved_capital_usd"] == "140"
        assert row["expected_net_pnl_usd"] == "8.00"
        assert Decimal(row["expected_return_on_reserved_capital"]) == Decimal("8") / Decimal("140")


def test_opportunity_rejects_missing_or_future_source_joins(tmp_path: Path) -> None:
    record = _opportunity()
    with ResearchStore(tmp_path / "research.db") as store:
        with pytest.raises(ResearchStorageError, match="must already exist"):
            store.append_opportunity_snapshot(record)
        store.append_raw_snapshot(
            replace(
                _raw_snapshot(),
                event_id="binance-quote-1",
                capture_time_ns=205,
                receive_time_ns=210,
                available_time_ns=215,
                persistence_time_ns=220,
            )
        )
        store.append_raw_snapshot(replace(_raw_snapshot(), event_id="hyperliquid-quote-1"))
        with pytest.raises(ResearchStorageError, match="before its availability"):
            store.append_opportunity_snapshot(record)


def test_opportunity_read_detects_derived_value_tampering(tmp_path: Path) -> None:
    path = tmp_path / "research.db"
    record = _opportunity()
    with ResearchStore(path) as store:
        store.append_raw_snapshot(replace(_raw_snapshot(), event_id="binance-quote-1"))
        store.append_raw_snapshot(replace(_raw_snapshot(), event_id="hyperliquid-quote-1"))
        store.append_opportunity_snapshot(record)
        with sqlite3.connect(path) as separate:
            separate.execute("DROP TRIGGER opportunity_snapshots_no_update")
            separate.execute(
                """
                UPDATE opportunity_snapshots
                SET total_reserved_capital_usd = '1'
                WHERE event_id = ?
                """,
                (record.event_id,),
            )
        with pytest.raises(ResearchStorageError, match="derived-value integrity"):
            tuple(store.iter_opportunity_snapshots())


def test_snapshot_timestamp_chain_and_exact_payload_fail_closed() -> None:
    record = _raw_snapshot()
    with pytest.raises(ValueError, match="capture <= receive"):
        replace(record, available_time_ns=105)
    with pytest.raises(TypeError, match="exact byte"):
        replace(record, payload_bytes="not-bytes")
    with pytest.raises(ValueError, match="Binance-long"):
        replace(_opportunity(), long_venue=Venue.HYPERLIQUID)
