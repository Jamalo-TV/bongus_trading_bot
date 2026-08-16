from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bongus.engine.account_truth import normalize_binance_account_truth
from bongus.engine.state_store import StateReader, StateWriter

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_shared_fixture_normalizes_to_exact_venue_separated_truth() -> None:
    raw = _load("binance_signed_account_snapshot_v1.json")
    expected = _load("binance_account_truth_v1.json")
    truth = normalize_binance_account_truth(
        raw,
        account_id="binance-fixture",
        environment="testnet",
        now=datetime(2026, 8, 15, 12, 1, tzinfo=timezone.utc),
    )

    actual = truth.to_dict()
    for key in (
        "schema_version",
        "account_id",
        "environment",
        "captured_at",
        "availability_time",
        "expires_at",
        "status",
        "standard_spot_status",
        "usd_m_futures_status",
        "missing_fields",
        "standard_spot",
        "usd_m_futures",
    ):
        assert actual[key] == expected[key]
    assert truth.ready
    assert truth.raw_snapshot == raw
    assert truth.standard_spot["balances"][0]["free"] == "1234.56"
    assert truth.usd_m_futures["margin_ratio"] == "0.005"
    assert truth.usd_m_futures["positions"][0]["liquidation_price"] == "120000"


def test_missing_individual_futures_field_remains_unknown() -> None:
    raw = _load("binance_signed_account_snapshot_v1.json")
    del raw["position_risk"][0]["liquidationPrice"]
    truth = normalize_binance_account_truth(
        raw,
        account_id="binance-fixture",
        environment="live",
        now=datetime(2026, 8, 15, 12, 1, tzinfo=timezone.utc),
    )

    assert not truth.ready
    assert truth.status == "UNKNOWN"
    assert truth.usd_m_futures_status == "UNKNOWN"
    assert truth.usd_m_futures["positions"][0]["liquidation_price"] is None
    assert (
        "usd_m_futures.positions[BTCUSDT|BOTH].liquidation_price"
        in truth.missing_fields
    )


def test_float_account_quantity_is_not_silently_normalized() -> None:
    raw = _load("binance_signed_account_snapshot_v1.json")
    raw["spot_account"]["balances"][0]["free"] = 1234.56
    truth = normalize_binance_account_truth(
        raw,
        account_id="binance-fixture",
        environment="live",
        now=datetime(2026, 8, 15, 12, 1, tzinfo=timezone.utc),
    )

    assert truth.standard_spot["balances"][0]["free"] is None
    assert "standard_spot.balances[0].free" in truth.missing_fields
    assert not truth.ready


def test_stale_availability_is_not_fresh_truth() -> None:
    raw = _load("binance_signed_account_snapshot_v1.json")
    truth = normalize_binance_account_truth(
        raw,
        account_id="binance-fixture",
        environment="live",
        now=datetime(2026, 8, 15, 12, 2, 1, tzinfo=timezone.utc),
    )

    assert truth.status == "STALE"
    assert truth.standard_spot_status == "STALE"
    assert truth.usd_m_futures_status == "STALE"
    assert not truth.ready


def test_account_truth_persistence_restarts_exact_and_rechecks_freshness(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    raw = _load("binance_signed_account_snapshot_v1.json")
    truth = normalize_binance_account_truth(
        raw,
        account_id="binance-fixture",
        environment="testnet",
        now=datetime(2026, 8, 15, 12, 1, tzinfo=timezone.utc),
    )
    writer = StateWriter(str(db_path))
    try:
        assert writer.record_account_truth_snapshot(truth)
        assert not writer.record_account_truth_snapshot(truth)
    finally:
        writer.close()

    reader = StateReader(str(db_path))
    try:
        fresh = reader.get_latest_account_truth(
            account_id="binance-fixture",
            environment="testnet",
            now="2026-08-15T12:01:30+00:00",
        )
        assert fresh is not None and fresh["ready"] is True
        assert fresh["standard_spot"]["balances"][0]["free"] == "1234.56"
        assert fresh["raw_snapshot"]["spot_account"]["balances"][0]["free"] == "1234.5600"

        stale = reader.get_latest_account_truth(
            account_id="binance-fixture",
            environment="testnet",
            now="2026-08-15T12:02:01+00:00",
        )
        assert stale is not None and stale["ready"] is False
        assert stale["stored_status"] == "COMPLETE"
        assert stale["status"] == "STALE"
        assert stale["standard_spot_status"] == "STALE"
        assert stale["usd_m_futures_status"] == "STALE"
    finally:
        reader.close()


def test_newer_unknown_snapshot_supersedes_older_complete_restart_truth(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    complete_raw = _load("binance_signed_account_snapshot_v1.json")
    complete = normalize_binance_account_truth(
        complete_raw,
        account_id="binance-fixture",
        environment="live",
        now=datetime(2026, 8, 15, 12, 1, tzinfo=timezone.utc),
    )
    unknown_raw = _load("binance_signed_account_snapshot_v1.json")
    unknown_raw.pop("availability_time")
    del unknown_raw["position_risk"][0]["liquidationPrice"]
    unknown = normalize_binance_account_truth(
        unknown_raw,
        account_id="binance-fixture",
        environment="live",
        now=datetime(2026, 8, 15, 12, 1, tzinfo=timezone.utc),
    )

    writer = StateWriter(str(db_path))
    try:
        writer.record_account_truth_snapshot(complete)
        writer.record_account_truth_snapshot(unknown)
    finally:
        writer.close()

    reader = StateReader(str(db_path))
    try:
        restored = reader.get_latest_account_truth(
            account_id="binance-fixture",
            environment="live",
            now="2026-08-15T12:01:30+00:00",
        )
        assert restored is not None
        assert restored["snapshot_id"] == unknown.snapshot_id
        assert restored["status"] == "UNKNOWN"
        assert restored["ready"] is False
    finally:
        reader.close()


def test_restart_truth_history_is_bounded_per_account_scope(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    writer = StateWriter(str(db_path))
    latest_snapshot_id = ""
    try:
        for offset in range(6):
            observed = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc) + timedelta(
                seconds=offset
            )
            raw = _load("binance_signed_account_snapshot_v1.json")
            raw["captured_at"] = observed.isoformat()
            raw["availability_time"] = observed.isoformat()
            truth = normalize_binance_account_truth(
                raw,
                account_id="binance-fixture",
                environment="testnet",
                now=observed,
            )
            writer.record_account_truth_snapshot(truth)
            latest_snapshot_id = truth.snapshot_id
        assert writer.conn.execute(
            "SELECT COUNT(*) FROM account_truth_snapshots "
            "WHERE account_id='binance-fixture' AND environment='testnet'"
        ).fetchone()[0] == 4
    finally:
        writer.close()

    reader = StateReader(str(db_path))
    try:
        latest = reader.get_latest_account_truth(
            account_id="binance-fixture",
            environment="testnet",
            now="2026-08-15T12:00:06+00:00",
        )
        assert latest is not None
        assert latest["snapshot_id"] == latest_snapshot_id
    finally:
        reader.close()


def test_restart_truth_hash_mismatch_fails_closed(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    raw = _load("binance_signed_account_snapshot_v1.json")
    truth = normalize_binance_account_truth(
        raw,
        account_id="binance-fixture",
        environment="live",
        now=datetime(2026, 8, 15, 12, 1, tzinfo=timezone.utc),
    )
    writer = StateWriter(str(db_path))
    try:
        writer.record_account_truth_snapshot(truth)
        writer.conn.execute(
            "UPDATE account_truth_snapshots SET standard_spot_json='{}' "
            "WHERE snapshot_id=?",
            (truth.snapshot_id,),
        )
        writer.conn.commit()
    finally:
        writer.close()

    reader = StateReader(str(db_path))
    try:
        restored = reader.get_latest_account_truth(
            account_id="binance-fixture",
            environment="live",
            now="2026-08-15T12:01:30+00:00",
        )
        assert restored is not None
        assert restored["integrity_valid"] is False
        assert restored["status"] == "UNKNOWN"
        assert restored["ready"] is False
    finally:
        reader.close()
