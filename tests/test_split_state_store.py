from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bongus.core.config import _resolve_runtime_data_root
from bongus.core.config import STORAGE_COMPONENT_BUDGETS_BYTES
from bongus.engine.cooldown_manager import CooldownManager
from bongus.engine.account_truth import normalize_binance_account_truth
from bongus.engine.database_backup import create_verified_backup
from bongus.engine.offline_storage_migration import (
    MANIFEST_FILENAME,
    execute_migration,
)
from bongus.engine.offline_storage_migration import TABLE_ROUTES
from bongus.engine.split_state_store import (
    ROLE_NAMES,
    SplitStateReader,
    SplitStateWriter,
    SplitStoreError,
    initialize_role_database,
)
from bongus.engine.state_store import (
    APPLICATION_ID,
    CURRENT_SCHEMA_VERSION,
    TERMINAL_RECONCILED,
    StateWriter,
    Trade,
)
from bongus.market_data.feed_recovery import FeedCursorStore, FeedSource, FeedState
from bongus.portfolio.capital_reservations import CapitalReservationBook


def _paths(tmp_path: Path) -> dict[str, Path]:
    return {role: tmp_path / role for role in ROLE_NAMES}


def test_exact_account_truth_routes_to_restart_state_only(split_store) -> None:
    writer, reader, _paths_by_role = split_store
    raw = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "binance_signed_account_snapshot_v1.json"
        ).read_text(encoding="utf-8")
    )
    truth = normalize_binance_account_truth(
        raw,
        account_id="binance-fixture",
        environment="testnet",
        now=datetime(2026, 8, 15, 12, 1, tzinfo=timezone.utc),
    )

    assert writer.record_account_truth_snapshot(truth)
    restored = reader.get_latest_account_truth(
        account_id="binance-fixture",
        environment="testnet",
        now="2026-08-15T12:01:30+00:00",
    )
    assert restored is not None and restored["ready"] is True
    assert restored["usd_m_futures"]["positions"][0]["leverage"] == "2"
    assert writer.state.conn.execute(
        "SELECT COUNT(*) FROM account_truth_snapshots"
    ).fetchone()[0] == 1
    assert writer.audit.conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='account_truth_snapshots'"
    ).fetchone()[0] == 0


@pytest.fixture
def split_store(tmp_path: Path):
    paths = _paths(tmp_path)
    writer = SplitStateWriter(
        state_path=str(paths["state.db"]),
        audit_path=str(paths["audit.db"]),
        research_path=str(paths["research.db"]),
    )
    reader = SplitStateReader(
        state_path=str(paths["state.db"]),
        audit_path=str(paths["audit.db"]),
        research_path=str(paths["research.db"]),
    )
    try:
        yield writer, reader, paths
    finally:
        reader.close()
        writer.close()


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest_digest(payload: dict[str, object]) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "manifest_sha256"}
    encoded = json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _published_migration(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = tmp_path / "legacy" / "state.db"
    source.parent.mkdir()
    writer = StateWriter(db_path=str(source))
    try:
        CooldownManager(connection=writer.conn).close()
        FeedCursorStore(connection=writer.conn).close()
        CapitalReservationBook(connection=writer.conn).close()
    finally:
        writer.close()
    with sqlite3.connect(source) as connection:
        assert connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    backup = create_verified_backup(
        source,
        tmp_path / "backups",
        required_headroom_bytes=0,
        backup_budget_bytes=100_000_000,
    )
    output = tmp_path / "published"
    execute_migration(
        source,
        backup.manifest_path,
        output,
        required_headroom_bytes=0,
    )
    return source, output, output / MANIFEST_FILENAME


def test_split_store_creates_exact_role_schemas(split_store) -> None:
    writer, _reader, _paths_by_role = split_store
    role_writers = {
        "state.db": writer.state,
        "audit.db": writer.audit,
        "research.db": writer.research,
    }

    actual_by_role: dict[str, set[str]] = {}
    for role, role_writer in role_writers.items():
        expected = {
            table_name
            for table_name, route in TABLE_ROUTES.items()
            if route.database == role
        }
        actual = _table_names(role_writer.conn)
        actual_by_role[role] = actual

        assert actual == expected
        assert role_writer.conn.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
        assert role_writer.conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert role_writer.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert role_writer.conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
        assert role_writer.conn.execute("PRAGMA synchronous").fetchone()[0] == (
            1 if role == "research.db" else 2
        )

    assert set().union(*actual_by_role.values()) == set(TABLE_ROUTES)
    assert actual_by_role["state.db"].isdisjoint(actual_by_role["audit.db"])
    assert actual_by_role["state.db"].isdisjoint(actual_by_role["research.db"])
    assert actual_by_role["audit.db"].isdisjoint(actual_by_role["research.db"])


def test_writer_and_reader_route_each_storage_tier(split_store) -> None:
    writer, reader, _paths_by_role = split_store

    writer.set_risk("storage_test", "state-only")
    writer.record_execution_event(
        {
            "symbol": "BTCUSDT",
            "client_order_id": "split-audit-event",
            "status": "FILLED",
            "event_time": "2026-01-01T00:00:00+00:00",
        }
    )
    writer.record_market_sample(
        symbol="BTCUSDT",
        sample_minute="2026-01-01T00:00:00+00:00",
        ann_funding=0.15,
        basis_pct=0.001,
        mark_price=65_000.0,
        minute_notional_volume=125_000.0,
    )

    assert reader.get_risk()["storage_test"] == "state-only"
    assert reader.get_execution_events(limit=10)[0]["client_order_id"] == "split-audit-event"
    assert reader.get_market_samples(symbol="BTCUSDT", limit=10)[0]["mark_price"] == 65_000.0
    assert writer.state.conn.execute("SELECT COUNT(*) FROM risk_state").fetchone()[0] == 1
    assert writer.audit.conn.execute("SELECT COUNT(*) FROM execution_events").fetchone()[0] == 1
    assert writer.research.conn.execute("SELECT COUNT(*) FROM market_samples").fetchone()[0] == 1


def test_research_retention_rolls_minutes_to_bounded_hourly_evidence(
    split_store,
) -> None:
    writer, reader, _paths_by_role = split_store
    old_hour = datetime.now(timezone.utc).replace(
        minute=0,
        second=0,
        microsecond=0,
    ) - timedelta(days=8)
    for minute, funding, basis, mark, volume in (
        (5, 0.10, 0.001, 100.0, 1_000.0),
        (35, 0.30, 0.003, 104.0, 2_000.0),
    ):
        writer.record_market_sample(
            symbol="BTCUSDT",
            sample_minute=old_hour.replace(minute=minute).isoformat(),
            ann_funding=funding,
            basis_pct=basis,
            mark_price=mark,
            minute_notional_volume=volume,
        )

    result = writer.prune_optional_retention(
        market_retention_days=7,
        health_retention_days=7,
        snapshot_retention_days=2,
        feature_retention_days=3,
        general_retention_days=30,
    )

    assert result["market_samples_deleted"] == 2
    assert reader.get_market_samples(symbol="BTCUSDT") == []
    hourly = reader.get_market_hourly_aggregates(symbol="BTCUSDT")
    assert len(hourly) == 1
    assert hourly[0]["sample_count"] == 2
    assert hourly[0]["ann_funding_avg"] == pytest.approx(0.20)
    assert hourly[0]["basis_pct_avg"] == pytest.approx(0.002)
    assert hourly[0]["mark_price_avg"] == pytest.approx(102.0)
    assert hourly[0]["notional_volume_sum"] == pytest.approx(3_000.0)


def test_accelerated_thirty_day_and_one_year_retention_soak_survives_restarts(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    writer = SplitStateWriter(
        state_path=str(paths["state.db"]),
        audit_path=str(paths["audit.db"]),
        research_path=str(paths["research.db"]),
    )
    try:
        writer.set_risk("soak_tier_a_marker", "preserved")
        writer.record_execution_event(
            {
                "symbol": "BTCUSDT",
                "client_order_id": "soak-audit-marker",
                "status": "FILLED",
                "event_time": now.isoformat(),
            }
        )
        start = now - timedelta(days=30)
        writer.research.conn.executemany(
            """INSERT INTO market_samples
                   (sample_minute, symbol, ann_funding, basis_pct,
                    mark_price, minute_notional_volume)
               VALUES (?, 'BTCUSDT', 0.12, 0.001, 100.0, 1000.0)""",
            (
                ((start + timedelta(minutes=index)).isoformat(),)
                for index in range(30 * 24 * 60)
            ),
        )
        writer.research.conn.commit()
    finally:
        writer.close()


    # First restart: compact thirty days of minute evidence into the required
    # seven-day minute plus bounded hourly tiers.
    writer = SplitStateWriter(
        state_path=str(paths["state.db"]),
        audit_path=str(paths["audit.db"]),
        research_path=str(paths["research.db"]),
    )
    try:
        result = writer.prune_optional_retention(
            market_retention_days=7,
            health_retention_days=7,
            snapshot_retention_days=2,
            feature_retention_days=3,
            general_retention_days=30,
        )
        assert result["market_samples_deleted"] >= 22 * 24 * 60
        minute_count = writer.research.conn.execute(
            "SELECT COUNT(*) FROM market_samples"
        ).fetchone()[0]
        hourly_count = writer.research.conn.execute(
            "SELECT COUNT(*) FROM market_hourly_aggregates"
        ).fetchone()[0]
        assert minute_count <= 7 * 24 * 60 + 1
        assert 22 * 24 <= hourly_count <= 24 * 24

        # Accelerate a full year of already-compacted hourly evidence. The
        # next restart must retain at most the ninety-day policy window.
        hourly_start = now.replace(minute=0) - timedelta(days=365)
        refreshed_at = now.isoformat()
        writer.research.conn.executemany(
            """INSERT OR REPLACE INTO market_hourly_aggregates (
                   bucket_hour, symbol, sample_count,
                   ann_funding_avg, ann_funding_min, ann_funding_max,
                   basis_pct_avg, basis_pct_min, basis_pct_max,
                   mark_price_avg, mark_price_min, mark_price_max,
                   notional_volume_sum, source_first_minute,
                   source_last_minute, refreshed_at
               ) VALUES (?, 'BTCUSDT', 60, 0.12, 0.10, 0.14,
                         0.001, 0.0005, 0.0015, 100.0, 99.0, 101.0,
                         60000.0, ?, ?, ?)""",
            (
                (
                    (hourly_start + timedelta(hours=index)).isoformat(),
                    (hourly_start + timedelta(hours=index)).isoformat(),
                    (
                        hourly_start
                        + timedelta(hours=index, minutes=59)
                    ).isoformat(),
                    refreshed_at,
                )
                for index in range(365 * 24)
            ),
        )
        writer.research.conn.commit()
    finally:
        writer.close()

    writer = SplitStateWriter(
        state_path=str(paths["state.db"]),
        audit_path=str(paths["audit.db"]),
        research_path=str(paths["research.db"]),
    )
    try:
        result = writer.prune_optional_retention(
            market_retention_days=7,
            health_retention_days=7,
            snapshot_retention_days=2,
            feature_retention_days=3,
            general_retention_days=30,
        )
        assert result["market_hourly_aggregates_deleted"] >= 274 * 24
        retained_hourly = writer.research.conn.execute(
            "SELECT COUNT(*) FROM market_hourly_aggregates"
        ).fetchone()[0]
        assert retained_hourly <= 90 * 24 + 1
        assert paths["research.db"].stat().st_size < 50_000_000
        for role_writer in (writer.state, writer.audit, writer.research):
            assert role_writer.conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        stored_marker = writer.state.conn.execute(
            "SELECT value FROM risk_state WHERE key='soak_tier_a_marker'"
        ).fetchone()[0]
        assert stored_marker == "preserved"
        assert writer.audit.conn.execute(
            "SELECT COUNT(*) FROM execution_events "
            "WHERE client_order_id='soak-audit-marker'"
        ).fetchone()[0] == 1
    finally:
        writer.close()


def test_production_shape_research_evidence_stays_below_capacity_for_72_hours(
    tmp_path: Path,
) -> None:
    """Measure the real SQLite row shape, then accelerate a 72-hour window.

    Production persists at most fifteen candidates per evidence cycle. Each
    candidate has one score and seven shadow rows. Payloads here are
    intentionally at or above the sizes observed during the pre-fix soak; both
    the 60-second shadow profile and exhaustive 15-second canonical profile are
    projected through the hourly-pruned window.
    """

    paths = _paths(tmp_path)
    writer = SplitStateWriter(
        state_path=str(paths["state.db"]),
        audit_path=str(paths["audit.db"]),
        research_path=str(paths["research.db"]),
    )
    try:
        research = writer.research.conn
        page_size = int(research.execute("PRAGMA page_size").fetchone()[0])
        base_bytes = int(research.execute("PRAGMA page_count").fetchone()[0]) * page_size
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        symbols = [f"S{index:02d}USDT" for index in range(15)]
        candidate_metrics = json.dumps(
            {
                "features": "x" * 3_900,
                "source": "production-shape-capacity-test",
            },
            separators=(",", ":"),
        )
        score_components = json.dumps(
            {"components": "s" * 450},
            separators=(",", ":"),
        )
        shadow_metadata = json.dumps(
            {"route_and_model_evidence": "m" * 1_150},
            separators=(",", ":"),
        )
        assert len(candidate_metrics) >= 3_900
        assert len(shadow_metadata) >= 1_150

        measured_hours = 6
        cycles = measured_hours * 60
        candidate_rows: list[tuple[object, ...]] = []
        score_rows: list[tuple[object, ...]] = []
        shadow_rows: list[tuple[object, ...]] = []
        for cycle_index in range(cycles):
            event_time = (now + timedelta(minutes=cycle_index)).isoformat()
            cycle_id = f"capacity:{event_time}"
            for rank, symbol in enumerate(symbols, start=1):
                candidate_rows.append(
                    (
                        cycle_id,
                        symbol,
                        event_time,
                        "long_spot_short_perp",
                        0,
                        "rejected",
                        "LARGE_CAP",
                        rank,
                        '["below threshold"]',
                        candidate_metrics,
                    )
                )
                score_rows.append(
                    (
                        cycle_id,
                        symbol,
                        event_time,
                        0.25,
                        12.0,
                        rank,
                        0,
                        8.0,
                        score_components,
                    )
                )
                for shadow_index in range(7):
                    shadow_rows.append(
                        (
                            event_time,
                            f"{cycle_id}:{symbol}:{shadow_index}",
                            symbol,
                            "hold",
                            0.5,
                            0.25,
                            0.0,
                            0,
                            shadow_metadata,
                        )
                    )

        research.executemany(
            """INSERT INTO candidate_snapshots
                   (cycle_id, symbol, snapshot_time, direction, accepted,
                    status, cluster, rank, rejection_reasons, metrics_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            candidate_rows,
        )
        research.executemany(
            """INSERT INTO opportunity_scores
                   (cycle_id, symbol, score_time, total_score,
                    predicted_net_edge_bps, rank, selected,
                    expected_holding_hours, component_scores_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            score_rows,
        )
        research.executemany(
            """INSERT INTO model_shadow_decisions
                   (decision_time, trade_id, symbol, action, hold_score,
                    exit_score, incremental_value_usd, recommended,
                    metadata_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            shadow_rows,
        )
        research.commit()
        research.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        measured_bytes = (
            int(research.execute("PRAGMA page_count").fetchone()[0]) * page_size
        )
        measured_growth = measured_bytes - base_bytes
        assert measured_growth > 0

        hourly_growth_at_60_seconds = measured_growth / measured_hours
        research_budget = int(STORAGE_COMPONENT_BUDGETS_BYTES["research"])
        hard_cap = (
            int(research.execute("PRAGMA max_page_count").fetchone()[0])
            * page_size
        )
        assert hard_cap >= research_budget - page_size
        projected_peaks: dict[str, float] = {}
        for stage, cadence_seconds in (
            ("shadow", 60),
            ("canonical", 15),
        ):
            cadence_multiplier = 60 / cadence_seconds
            simulated_live_hours = 0
            simulated_peak_bytes = float(base_bytes)
            for _hour in range(72):
                # Hourly maintenance with one-day retention has a conservative
                # 25-hour pre-prune peak (24 retained hours plus the new hour).
                simulated_live_hours = min(25, simulated_live_hours + 1)
                simulated_peak_bytes = max(
                    simulated_peak_bytes,
                    base_bytes
                    + hourly_growth_at_60_seconds
                    * cadence_multiplier
                    * simulated_live_hours,
                )
            projected_peaks[stage] = simulated_peak_bytes

        assert projected_peaks["shadow"] < research_budget * 0.20
        assert projected_peaks["canonical"] < research_budget * 0.80
        assert research.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        writer.close()


def test_online_retention_bounds_rows_market_window_and_wal(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    writer = SplitStateWriter(
        state_path=str(paths["state.db"]),
        audit_path=str(paths["audit.db"]),
        research_path=str(paths["research.db"]),
    )
    try:
        research = writer.research.conn
        old_start = datetime.now(timezone.utc).replace(
            minute=0,
            second=0,
            microsecond=0,
        ) - timedelta(days=10)
        candidate_payload = json.dumps({"payload": "x" * 3_900})
        research.executemany(
            """INSERT INTO candidate_snapshots
                   (cycle_id, symbol, snapshot_time, direction, accepted,
                    status, cluster, rank, rejection_reasons, metrics_json)
               VALUES (?, 'BTCUSDT', ?, 'long', 0, 'rejected', 'BTC', 1,
                       '[]', ?)""",
            (
                (f"bounded:{index}", (old_start + timedelta(minutes=index)).isoformat(), candidate_payload)
                for index in range(25)
            ),
        )
        research.executemany(
            """INSERT INTO model_shadow_decisions
                   (decision_time, trade_id, symbol, action, metadata_json)
               VALUES (?, ?, 'BTCUSDT', 'hold', '{}')""",
            (
                ((old_start + timedelta(minutes=index)).isoformat(), f"shadow:{index}")
                for index in range(25)
            ),
        )
        research.executemany(
            """INSERT INTO market_samples
                   (sample_minute, symbol, ann_funding, basis_pct,
                    mark_price, minute_notional_volume)
               VALUES (?, 'BTCUSDT', 0.1, 0.001, 100.0, 1000.0)""",
            (
                ((old_start + timedelta(hours=index)).isoformat(),)
                for index in range(48)
            ),
        )
        research.commit()
        research.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        result = writer.prune_optional_retention(
            market_retention_days=7,
            health_retention_days=7,
            snapshot_retention_days=1,
            feature_retention_days=3,
            general_retention_days=30,
            max_rows_per_table=10,
            market_aggregation_max_hours=6,
        )

        assert result["candidate_snapshots_deleted"] == 10
        assert result["model_shadow_decisions_deleted"] == 10
        assert result["market_samples_deleted"] == 6
        assert research.execute(
            "SELECT COUNT(*) FROM candidate_snapshots"
        ).fetchone()[0] == 15
        assert research.execute(
            "SELECT COUNT(*) FROM model_shadow_decisions"
        ).fetchone()[0] == 15
        assert research.execute("SELECT COUNT(*) FROM market_samples").fetchone()[0] == 42
        wal_path = Path(f"{paths['research.db']}-wal")
        assert not wal_path.exists() or wal_path.stat().st_size < 5_000_000
        assert research.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        writer.close()


def test_lifecycle_evidence_survives_projection_failure_and_retry_repairs_state(
    split_store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer, reader, _paths_by_role = split_store
    position = {
        "symbol": "BTCUSDT",
        "side": "LONG_SPOT_SHORT_PERP",
        "spot_entry": 100.0,
        "perp_entry": 101.0,
        "qty": 1.0,
        "direction": "long",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    writer.upsert_pending_intent(
        intent_id="intent-entry",
        symbol="BTCUSDT",
        intent_type="ENTER",
        direction="long",
        status="SUBMITTED",
        quantity=1.0,
    )

    def fail_entry_projection(**_kwargs: object) -> None:
        raise sqlite3.OperationalError("forced state projection failure")

    with monkeypatch.context() as patch:
        patch.setattr(writer.state, "upsert_position", fail_entry_projection)
        with pytest.raises(sqlite3.OperationalError, match="forced state projection failure"):
            writer.project_entry_lifecycle(
                event_key="entry:intent-entry",
                intent_id="intent-entry",
                event_time="2026-01-01T00:00:00+00:00",
                position_fields=position,
                evidence={"cycle_id": "cycle-entry", "telemetry_sequence": 51},
            )

    assert writer.audit.conn.execute(
        "SELECT COUNT(*) FROM lifecycle_events WHERE event_key='entry:intent-entry'"
    ).fetchone()[0] == 1
    assert reader.get_positions() == []
    assert [row["intent_id"] for row in reader.get_pending_intents()] == ["intent-entry"]

    assert writer.project_entry_lifecycle(
        event_key="entry:intent-entry",
        intent_id="intent-entry",
        event_time="2026-01-01T00:00:00+00:00",
        position_fields=position,
        evidence={"cycle_id": "cycle-entry", "telemetry_sequence": 51},
    ) is False
    assert [row["symbol"] for row in reader.get_positions()] == ["BTCUSDT"]
    assert reader.get_pending_intents() == []
    entry_tombstone = reader.get_pending_intent("intent-entry")
    assert entry_tombstone is not None
    assert entry_tombstone["lifecycle_state"] == TERMINAL_RECONCILED
    assert entry_tombstone["terminal_sequence_watermark"] == 51

    writer.upsert_pending_intent(
        intent_id="intent-exit",
        symbol="BTCUSDT",
        intent_type="EXIT",
        direction="long",
        status="SUBMITTED",
        quantity=1.0,
    )
    trade = Trade(
        symbol="BTCUSDT",
        side="LONG_SPOT_SHORT_PERP",
        entry_time="2026-01-01T00:00:00+00:00",
        exit_time="2026-01-01T08:00:00+00:00",
        entry_price=100.5,
        exit_price=102.0,
        qty=1.0,
        net_pnl_usd=1.0,
    )

    def fail_exit_projection(_symbol: str, *, commit: bool = True) -> None:
        del commit
        raise sqlite3.OperationalError("forced exit projection failure")

    with monkeypatch.context() as patch:
        patch.setattr(writer.state, "remove_position", fail_exit_projection)
        with pytest.raises(sqlite3.OperationalError, match="forced exit projection failure"):
            writer.project_exit_lifecycle(
                event_key="exit:intent-exit",
                intent_id="intent-exit",
                event_time=trade.exit_time,
                trade=trade,
                evidence={"cycle_id": "cycle-exit"},
            )

    assert writer.audit.conn.execute(
        "SELECT COUNT(*) FROM lifecycle_events WHERE event_key='exit:intent-exit'"
    ).fetchone()[0] == 1
    assert writer.audit.conn.execute("SELECT COUNT(*) FROM trade_history").fetchone()[0] == 1
    assert [row["symbol"] for row in reader.get_positions()] == ["BTCUSDT"]

    assert writer.project_exit_lifecycle(
        event_key="exit:intent-exit",
        intent_id="intent-exit",
        event_time=trade.exit_time,
        trade=trade,
        evidence={"cycle_id": "cycle-exit"},
    ) is False
    assert reader.get_positions() == []
    assert reader.get_pending_intents() == []
    assert len(reader.get_trades(limit=10, session_scoped=False)) == 1


def test_partial_exit_audit_precedes_residual_projection_and_retry_repairs_state(
    split_store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer, reader, _paths_by_role = split_store
    entry_time = "2026-01-01T00:00:00+00:00"
    position = {
        "symbol": "BTCUSDT",
        "side": "LONG_SPOT_SHORT_PERP",
        "spot_entry": 100.0,
        "perp_entry": 101.0,
        "qty": 1.0,
        "direction": "long",
        "updated_at": entry_time,
    }
    writer.upsert_pending_intent(
        intent_id="entry-split-partial",
        symbol="BTCUSDT",
        intent_type="ENTER_LONG",
        direction="long",
        status="SUBMITTED",
        quantity=1.0,
    )
    writer.project_entry_lifecycle(
        event_key="entry:split-partial",
        intent_id="entry-split-partial",
        event_time=entry_time,
        position_fields=position,
        evidence={},
    )
    writer.upsert_pending_intent(
        intent_id="split-partial-exit",
        symbol="BTCUSDT",
        intent_type="EXIT_LONG",
        direction="long",
        status="SUBMITTED",
        quantity=0.25,
    )
    residual = {**position, "qty": 0.75}

    def fail_residual_projection(**_kwargs: object) -> None:
        raise sqlite3.OperationalError("forced partial projection failure")

    with monkeypatch.context() as patch:
        patch.setattr(writer.state, "upsert_position", fail_residual_projection)
        with pytest.raises(
            sqlite3.OperationalError,
            match="forced partial projection failure",
        ):
            writer.project_partial_exit_lifecycle(
                event_key="partial_exit:split-partial-exit",
                intent_id="split-partial-exit",
                event_time="2026-01-01T04:00:00+00:00",
                remaining_position_fields=residual,
                evidence={"exit_quantity": 0.25},
            )

    assert writer.audit.conn.execute(
        "SELECT COUNT(*) FROM lifecycle_events "
        "WHERE event_key='partial_exit:split-partial-exit'"
    ).fetchone()[0] == 1
    assert reader.get_positions()[0]["qty"] == pytest.approx(1.0)
    assert [row["intent_id"] for row in reader.get_pending_intents()] == [
        "split-partial-exit"
    ]

    assert writer.project_partial_exit_lifecycle(
        event_key="partial_exit:split-partial-exit",
        intent_id="split-partial-exit",
        event_time="2026-01-01T04:00:00+00:00",
        remaining_position_fields=residual,
        evidence={"exit_quantity": 0.25},
    ) is False
    assert reader.get_positions()[0]["qty"] == pytest.approx(0.75)
    assert reader.get_pending_intents() == []
    assert reader.get_trades(limit=10, session_scoped=False) == []
    assert reader.get_partial_exit_lifecycle_events("BTCUSDT")[0][
        "remaining_position"
    ]["qty"] == pytest.approx(0.75)

    proof = writer.rebuild_lifecycle_projections(
        authoritative_positions=[reader.get_positions()[0]]
    )
    assert proof["event_count"] == 2
    assert proof["position_count"] == 1
    assert proof["trade_count"] == 0
    rebuilt_tombstones = reader.get_pending_intents(
        include_tombstones=True,
        lifecycle_states=[TERMINAL_RECONCILED],
        limit=10,
    )
    assert {row["intent_id"] for row in rebuilt_tombstones} == {
        "entry-split-partial",
        "split-partial-exit",
    }
    assert all(
        str(row["tombstone_reason"]).startswith("lifecycle_rebuild:")
        for row in rebuilt_tombstones
    )


def test_statement_evidence_commit_precedes_cursor_and_retry_repairs_cursor(split_store) -> None:
    writer, reader, _paths_by_role = split_store
    cursor_conn = writer.state._statement_conn
    cursor_conn.executescript(
        """
        CREATE TEMP TRIGGER force_statement_cursor_failure
        BEFORE INSERT ON exchange_statement_cursors
        BEGIN
            SELECT RAISE(ABORT, 'forced statement cursor failure');
        END;
        """
    )

    payload = {
        "symbol": "BTCUSDT",
        "incomeType": "FUNDING_FEE",
        "income": "1.2500",
        "asset": "USDT",
        "info": "funding fee",
        "time": 1_767_225_600_123,
        "tranId": 12345,
        "tradeId": "",
    }
    context = {
        "account_id": "binance-testnet-main",
        "trading_mode": "testnet",
        "strategy_id": "funding-arbitrage-v2",
        "venue": "BINANCE",
        "runtime_mode": "SAFE_MODE",
        "session_id": "split-statement-test",
    }
    with pytest.raises(sqlite3.IntegrityError, match="forced statement cursor failure"):
        writer.record_binance_futures_income_statement(payload, **context)

    assert writer.audit._statement_conn.execute(
        "SELECT COUNT(*) FROM exchange_statement_entries"
    ).fetchone()[0] == 1
    assert writer.audit._statement_conn.execute(
        "SELECT COUNT(*) FROM economic_ledger_events"
    ).fetchone()[0] == 1
    assert cursor_conn.execute("SELECT COUNT(*) FROM exchange_statement_cursors").fetchone()[0] == 0

    cursor_conn.execute("DROP TRIGGER force_statement_cursor_failure")
    cursor_conn.commit()
    replay = writer.record_binance_futures_income_statement(payload, **context)

    assert replay.inserted is False
    assert replay.duplicate is True
    assert replay.cursor_advanced is True
    assert len(reader.get_exchange_statement_entries(account_id=context["account_id"])) == 1
    cursor = reader.get_exchange_statement_cursor(
        venue=context["venue"],
        account_id=context["account_id"],
        statement_source="BINANCE_FUTURES_INCOME",
    )
    assert cursor is not None
    assert cursor["exchange_transaction_id"] == "12345"


def test_feed_cursor_and_recovery_events_use_state_and_audit_files(split_store) -> None:
    writer, _reader, _paths_by_role = split_store
    feed_store = FeedCursorStore(
        connection=writer._feed_recovery_conn,
        event_connection=writer._feed_recovery_event_conn,
        lock=writer._guard_lock,
    )
    source = FeedSource("binance", "depth", "BTCUSDT")
    try:
        gap = feed_store.record_gap(
            source,
            prior_sequence=100,
            first_sequence=102,
            final_sequence=105,
            previous_final_sequence=100,
            reason="test_gap",
        )
        proof = feed_store.record_readiness_proof(
            source,
            final_sequence=200,
            is_snapshot=True,
        )
    finally:
        feed_store.close()

    assert gap.state is FeedState.GAPPED
    assert proof.state is FeedState.READY
    cursor = writer.state._feed_recovery_conn.execute(
        "SELECT state, last_sequence FROM feed_cursors WHERE source_key=?",
        (source.key,),
    ).fetchone()
    assert cursor is not None
    assert (cursor["state"], cursor["last_sequence"]) == (FeedState.READY.value, 200)
    event_rows = writer.audit._feed_recovery_conn.execute(
        "SELECT event_type FROM feed_recovery_events WHERE source_key=? ORDER BY event_id",
        (source.key,),
    ).fetchall()
    assert [str(row["event_type"]) for row in event_rows] == [
        "RANGED_GAP_RECORDED",
        "RANGED_READINESS_PROVEN",
    ]
    assert "feed_recovery_events" not in _table_names(writer.state.conn)
    assert "feed_cursors" not in _table_names(writer.audit.conn)


def test_split_runtime_rejects_a_legacy_monolithic_database(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    legacy_writer = StateWriter(db_path=str(paths["state.db"]))
    legacy_writer.close()

    with pytest.raises(SplitStoreError, match="run the verified offline storage migration"):
        SplitStateWriter(
            state_path=str(paths["state.db"]),
            audit_path=str(paths["audit.db"]),
            research_path=str(paths["research.db"]),
        )

    assert not paths["audit.db"].exists()
    assert not paths["research.db"].exists()


def test_published_migration_requires_stopped_writer_activation_and_remains_manifest_bound(
    tmp_path: Path,
) -> None:
    _source, output, manifest_path = _published_migration(tmp_path)
    paths = _paths(output)

    with pytest.raises(SplitStoreError, match="has not completed its stopped activation"):
        SplitStateReader(
            state_path=str(paths["state.db"]),
            audit_path=str(paths["audit.db"]),
            research_path=str(paths["research.db"]),
        )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    writer = SplitStateWriter(
        state_path=str(paths["state.db"]),
        audit_path=str(paths["audit.db"]),
        research_path=str(paths["research.db"]),
    )
    writer.close()
    with sqlite3.connect(paths["state.db"]) as connection:
        marker = dict(
            connection.execute(
                "SELECT key, value FROM schema_meta "
                "WHERE key LIKE 'split_store_activation_%'"
            ).fetchall()
        )
    assert marker == {
        "split_store_activation_identity": manifest["manifest_sha256"],
        "split_store_activation_mode": "migration-manifest-v1",
    }

    reader = SplitStateReader(
        state_path=str(paths["state.db"]),
        audit_path=str(paths["audit.db"]),
        research_path=str(paths["research.db"]),
    )
    reader.close()

    manifest["created_at"] = "2099-01-01T00:00:00+00:00"
    manifest["manifest_sha256"] = _manifest_digest(manifest)
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(SplitStoreError, match="marker does not match its manifest"):
        SplitStateReader(
            state_path=str(paths["state.db"]),
            audit_path=str(paths["audit.db"]),
            research_path=str(paths["research.db"]),
        )


def test_first_activation_rejects_a_tampered_migration_before_any_writable_open(
    tmp_path: Path,
) -> None:
    _source, output, _manifest = _published_migration(tmp_path)
    paths = _paths(output)
    with sqlite3.connect(paths["audit.db"]) as connection:
        connection.execute("CREATE TABLE injected_table (id INTEGER PRIMARY KEY)")

    with pytest.raises(SplitStoreError, match="(size|hash) does not match manifest"):
        SplitStateWriter(
            state_path=str(paths["state.db"]),
            audit_path=str(paths["audit.db"]),
            research_path=str(paths["research.db"]),
        )
    with sqlite3.connect(paths["state.db"]) as connection:
        marker_count = connection.execute(
            "SELECT COUNT(*) FROM schema_meta "
            "WHERE key LIKE 'split_store_activation_%'"
        ).fetchone()[0]
    assert marker_count == 0


def test_flush_commits_critical_state_before_optional_research_failure(
    split_store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer, _reader, paths = split_store
    writer.state.conn.execute(
        "INSERT INTO risk_state(key, value, updated_at) VALUES (?, ?, ?)",
        ("critical-before-research", "persisted", "2026-08-09T12:00:00+00:00"),
    )

    def fail_optional_flush() -> None:
        raise sqlite3.OperationalError("forced optional research flush failure")

    monkeypatch.setattr(writer.research, "flush", fail_optional_flush)
    with pytest.raises(sqlite3.OperationalError, match="optional research flush failure"):
        writer.flush()

    with sqlite3.connect(paths["state.db"]) as connection:
        value = connection.execute(
            "SELECT value FROM risk_state WHERE key='critical-before-research'"
        ).fetchone()[0]
    assert value == "persisted"
