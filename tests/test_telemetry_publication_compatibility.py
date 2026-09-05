"""Terminal replay bookkeeping must preserve the deployed split-store contract."""

import json
import sqlite3

import pytest

from bongus.engine.split_state_store import SplitStateWriter
from bongus.engine.state_store import TELEMETRY_PUBLICATION_META_PREFIX, StateWriter
from tests.test_split_state_store import _paths, _published_migration

# Derived independently from b17b32f's 42 TABLE_ROUTES, application id BONG,
# schema version 18 and fresh-split-v1 activation contract. Do not regenerate
# this fixture from the current runtime: it represents a deployed predecessor.
PREDECESSOR_FRESH_IDENTITY = "2862bff21435316400b401024eb06ba9ff1c85a40acd2959e6d04578779856e6"


def _event(sequence=101):
    return {
        "event": "EmergencyExitState",
        "state": "FLAT",
        "publication_id": "emergency:legacy-exit:FLAT",
        "symbol": "BTCUSDT",
        "telemetry_sequence": sequence,
        "telemetry_schema_version": 3,
        "telemetry_ack_required": True,
    }


def _writer(paths):
    return SplitStateWriter(
        state_path=str(paths["state.db"]),
        audit_path=str(paths["audit.db"]),
        research_path=str(paths["research.db"]),
    )


@pytest.mark.parametrize("mode", ["fresh", "migrated"])
def test_predecessor_split_trio_retains_identity_and_history_on_publication_replay(tmp_path, mode):
    manifest_before = None
    manifest_path = None
    if mode == "migrated":
        _, output, manifest_path = _published_migration(tmp_path)
        paths = _paths(output)
        manifest_before = manifest_path.read_bytes()
    else:
        paths = _paths(tmp_path)
    predecessor = _writer(paths)
    try:
        tables = {
            role: tuple(row[0] for row in owner.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ))
            for role, owner in (
                ("state.db", predecessor.state),
                ("audit.db", predecessor.audit),
                ("research.db", predecessor.research),
            )
        }
        assert sum(map(len, tables.values())) == 42
        assert "telemetry_publications" not in tables["state.db"]
        marker = dict(predecessor.state.conn.execute(
            "SELECT key, value FROM schema_meta WHERE key LIKE 'split_store_activation_%'"
        ))
        if mode == "fresh":
            assert marker["split_store_activation_identity"] == PREDECESSOR_FRESH_IDENTITY
        predecessor.record_execution_event({
            "symbol": "BTCUSDT", "event_type": "PREDECESSOR_HISTORY", "status": "FILLED",
        })
        history = predecessor.audit.conn.execute("SELECT * FROM execution_events").fetchall()
        assert len(history) == 1
        assert predecessor.state.conn.execute(
            "SELECT COUNT(*) FROM schema_meta WHERE key GLOB ?",
            (TELEMETRY_PUBLICATION_META_PREFIX + "*",),
        ).fetchone()[0] == 0
    finally:
        predecessor.close()

    writer = _writer(paths)
    try:
        assert writer.append_durable_telemetry_receipt(_event())
        writer.complete_durable_telemetry(101)
    finally:
        writer.close()
    restarted = _writer(paths)
    try:
        assert not restarted.append_durable_telemetry_receipt(_event(102))
        assert restarted.pending_durable_telemetry_events() == []
        assert dict(restarted.state.conn.execute(
            "SELECT key, value FROM schema_meta WHERE key LIKE 'split_store_activation_%'"
        )) == marker
        assert restarted.audit.conn.execute("SELECT * FROM execution_events").fetchall() == history
        metadata = json.loads(restarted.state.conn.execute(
            "SELECT value FROM schema_meta WHERE key=?",
            (TELEMETRY_PUBLICATION_META_PREFIX + _event()["publication_id"],),
        ).fetchone()[0])
        assert set(metadata) == {"event_hash", "status"}
        assert metadata["status"] == "PROCESSED"
        for role, expected in tables.items():
            with sqlite3.connect(paths[role]) as conn:
                assert tuple(row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )) == expected
        if manifest_before is not None:
            assert manifest_path is not None
            assert manifest_path.read_bytes() == manifest_before
    finally:
        restarted.close()


@pytest.mark.parametrize("invalid", [
    "not-json", "[]", "null", '{}',
    '{"event_hash":"' + "a" * 64 + '","status":"UNKNOWN"}',
    '{"event_hash":"' + "A" * 64 + '","status":"PROCESSED"}',
    '{"event_hash":"short","status":"PROCESSED"}',
    '{"event_hash":"' + "a" * 64 + '","status":"PROCESSED","extra":1}',
    '{"event_hash":"' + "a" * 64 + '", "status":"PROCESSED"}',
])
def test_corrupt_publication_metadata_cannot_complete_or_ack_receipt(tmp_path, invalid):
    writer = StateWriter(db_path=str(tmp_path / "state.db"))
    try:
        assert writer.append_durable_telemetry_receipt(_event())
        writer.conn.execute(
            "UPDATE schema_meta SET value=? WHERE key=?",
            (invalid, TELEMETRY_PUBLICATION_META_PREFIX + _event()["publication_id"]),
        )
        writer.conn.commit()
        with pytest.raises(ValueError, match="invalid durable telemetry publication metadata"):
            writer.complete_durable_telemetry(101)
        with pytest.raises(ValueError, match="invalid durable telemetry publication metadata"):
            writer.append_durable_telemetry_receipt(_event(102))
        assert [tuple(row) for row in writer.conn.execute(
            "SELECT telemetry_sequence, status FROM telemetry_receipts ORDER BY telemetry_sequence"
        )] == [(101, "PROCESSING")]
    finally:
        writer.close()


def test_missing_publication_identity_does_not_checkpoint_receipt(tmp_path):
    writer = StateWriter(db_path=str(tmp_path / "state.db"))
    try:
        writer.append_durable_telemetry_receipt(_event())
        writer.conn.execute(
            "DELETE FROM schema_meta WHERE key=?",
            (TELEMETRY_PUBLICATION_META_PREFIX + _event()["publication_id"],),
        )
        writer.conn.commit()
        with pytest.raises(ValueError, match="publication unavailable"):
            writer.complete_durable_telemetry(101)
        assert writer.pending_durable_telemetry_events()[0]["telemetry_sequence"] == 101
    finally:
        writer.close()


@pytest.mark.parametrize("phase", ["begin", "complete"])
def test_publication_and_receipt_checkpoint_roll_back_together(tmp_path, phase):
    writer = StateWriter(db_path=str(tmp_path / "state.db"))
    try:
        if phase == "complete":
            writer.append_durable_telemetry_receipt(_event())
            writer.conn.execute(
                "CREATE TRIGGER reject_publication_checkpoint BEFORE UPDATE ON schema_meta "
                "BEGIN SELECT RAISE(ABORT, 'injected publication write failure'); END"
            )
        else:
            writer.conn.execute(
                "CREATE TRIGGER reject_receipt BEFORE INSERT ON telemetry_receipts "
                "BEGIN SELECT RAISE(ABORT, 'injected receipt write failure'); END"
            )
        writer.conn.commit()
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            if phase == "complete":
                writer.complete_durable_telemetry(101)
            else:
                writer.append_durable_telemetry_receipt(_event())
        receipts = [tuple(row) for row in writer.conn.execute(
            "SELECT telemetry_sequence, status FROM telemetry_receipts"
        )]
        publications = [json.loads(row[0]) for row in writer.conn.execute(
            "SELECT value FROM schema_meta WHERE key GLOB ?",
            (TELEMETRY_PUBLICATION_META_PREFIX + "*",),
        )]
        if phase == "complete":
            assert receipts == [(101, "PROCESSING")]
            assert len(publications) == 1 and publications[0]["status"] == "PROCESSING"
        else:
            assert receipts == publications == []
    finally:
        writer.close()
