from __future__ import annotations

import json
import hashlib
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import msgpack
import pytest
import zmq

from bongus.engine.state_store import StateReader, StateWriter
from bongus.core.config_manager import ConfigManager
from bongus.ipc.execution import ExecutionClient
from bongus.ipc.protocol import (
    CONFIG_SYNC_INTENT,
    DEFAULT_MAX_UNHEDGED_NOTIONAL_MS,
    EXECUTION_PROTOCOL_VERSION,
    ExecutionProtocolError,
    build_command_envelope,
    build_config_sync_envelope,
    command_hash,
    deterministic_client_order_id,
    validate_ack,
)


GOLDEN_FIXTURE = Path(__file__).parent / "fixtures" / "execution_command_v2.json"
CONFIG_SYNC_GOLDEN_FIXTURE = (
    Path(__file__).parent / "fixtures" / "config_sync_command_v2.json"
)
CONFIG_SYNC_SCHEMA = (
    Path(__file__).parents[1] / "execution_engine" / "config_sync_schema_v2.json"
)


def _payload(intent_id: str = "intent-1", *, quantity: float = 1.0) -> dict:
    return {
        "intent": "ENTER_LONG",
        "intent_id": intent_id,
        "symbol": "BTCUSDT",
        "quantity": quantity,
        "urgency": 0.5,
        "max_slippage_bps": 5.0,
        "exposure_scale": 1.0,
        "account_id": "account-a",
        "environment": "paper",
        "strategy_id": "funding-v2",
        "cycle_id": "cycle-1",
        "config_version_hash": "config-abc",
    }


def _client(writer: StateWriter, socket: MagicMock, *, ttl_ms: int = 30_000) -> ExecutionClient:
    with patch("bongus.ipc.execution.zmq.Context") as context_class:
        context = MagicMock()
        context_class.return_value = context
        context.socket.return_value = socket
        return ExecutionClient(
            state_writer=writer,
            producer_id="test-producer",
            command_ttl_ms=ttl_ms,
        )


def test_envelope_has_required_context_and_deterministic_leg_ids() -> None:
    envelope = build_command_envelope(
        _payload(),
        producer_id="test-producer",
        sequence=7,
        ttl_ms=5_000,
        created_at_ms=1_000,
    )
    assert envelope["schema_version"] == EXECUTION_PROTOCOL_VERSION
    assert envelope["sequence"] == 7
    assert envelope["deadline_at_ms"] == 6_000
    assert envelope["spot_leg_id"] == "intent-1:spot"
    assert envelope["perp_leg_id"] == "intent-1:perp"
    assert envelope["spot_client_order_id"] == deterministic_client_order_id("intent-1", "spot")
    assert envelope["perp_client_order_id"] == deterministic_client_order_id("intent-1", "perp")
    assert len(envelope["command_hash"]) == 64
    assert envelope["route_policy"] == "legacy_dual_maker"
    assert envelope["route_model_version"] == "legacy-v1"
    assert envelope["max_unhedged_notional_ms"] == DEFAULT_MAX_UNHEDGED_NOTIONAL_MS
    assert envelope["route_slice_count"] == 1


def test_cross_language_v2_golden_envelope() -> None:
    fixture = json.loads(GOLDEN_FIXTURE.read_text(encoding="utf-8"))
    envelope = build_command_envelope(
        fixture["payload"],
        producer_id=fixture["producer_id"],
        sequence=fixture["sequence"],
        ttl_ms=fixture["ttl_ms"],
        created_at_ms=fixture["created_at_ms"],
    )
    assert fixture["protocol_version"] == EXECUTION_PROTOCOL_VERSION
    assert envelope == fixture["envelope"]
    assert command_hash(envelope) == fixture["envelope"]["command_hash"]


def test_cross_language_v2_config_sync_golden_envelope() -> None:
    fixture = json.loads(CONFIG_SYNC_GOLDEN_FIXTURE.read_text(encoding="utf-8"))
    envelope = build_config_sync_envelope(
        fixture["payload"],
        producer_id=fixture["producer_id"],
        sequence=fixture["sequence"],
        ttl_ms=fixture["ttl_ms"],
        created_at_ms=fixture["created_at_ms"],
    )
    assert fixture["protocol_version"] == EXECUTION_PROTOCOL_VERSION
    assert envelope == fixture["envelope"]
    assert command_hash(envelope) == fixture["envelope"]["command_hash"]


def test_config_sync_schema_matches_every_effective_config_key(tmp_path) -> None:
    schema = json.loads(CONFIG_SYNC_SCHEMA.read_text(encoding="utf-8"))
    assert schema["schema_version"] == EXECUTION_PROTOCOL_VERSION
    assert set(schema["allowed_keys"]) == ConfigManager.allowed_keys()

    manager = ConfigManager(tmp_path / "absent-live-config.json")
    snapshot = manager.canonical_snapshot()
    assert snapshot.values == manager.snapshot()
    assert snapshot.canonical_bytes == snapshot.canonical_json.encode("utf-8")
    assert snapshot.sha256 == hashlib.sha256(snapshot.canonical_bytes).hexdigest()
    assert snapshot.sha256 == manager.version_hash
    assert snapshot.canonical_json == json.dumps(
        snapshot.values,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def test_config_sync_rejects_noncanonical_mismatched_and_unknown_documents() -> None:
    fixture = json.loads(CONFIG_SYNC_GOLDEN_FIXTURE.read_text(encoding="utf-8"))
    payload = fixture["payload"]
    with pytest.raises(ExecutionProtocolError, match="does not match"):
        build_config_sync_envelope(
            {**payload, "config_canonical_json": payload["config_canonical_json"] + " "},
            producer_id="test-producer",
            sequence=1,
            ttl_ms=5_000,
        )

    noncanonical = '{"pause_new_entries": true, "max_gross_exposure_usd": 10000, "per_symbol_notional_cap_usd": 2500}'
    with pytest.raises(ExecutionProtocolError, match="not canonical"):
        build_config_sync_envelope(
            {
                **payload,
                "config_canonical_json": noncanonical,
                "config_version_hash": hashlib.sha256(noncanonical.encode()).hexdigest(),
            },
            producer_id="test-producer",
            sequence=1,
            ttl_ms=5_000,
        )

    unknown = '{"future_bypass":true,"max_gross_exposure_usd":10000,"pause_new_entries":true,"per_symbol_notional_cap_usd":2500}'
    with pytest.raises(ExecutionProtocolError, match="unknown effective-config key"):
        build_config_sync_envelope(
            {
                **payload,
                "config_canonical_json": unknown,
                "config_version_hash": hashlib.sha256(unknown.encode()).hexdigest(),
            },
            producer_id="test-producer",
            sequence=1,
            ttl_ms=5_000,
        )


def test_unknown_command_fields_and_generated_field_injection_fail_closed() -> None:
    with pytest.raises(ExecutionProtocolError, match="unknown risk-command field"):
        build_command_envelope(
            {**_payload(), "future_field": "silently-dangerous"},
            producer_id="test-producer",
            sequence=1,
            ttl_ms=5_000,
        )
    with pytest.raises(ExecutionProtocolError, match="unknown risk-command field"):
        build_command_envelope(
            {**_payload(), "sequence": 999},
            producer_id="test-producer",
            sequence=1,
            ttl_ms=5_000,
        )
    with pytest.raises(ExecutionProtocolError, match="must be boolean"):
        build_command_envelope(
            {**_payload(), "skip_spot_leg": "false"},
            producer_id="test-producer",
            sequence=1,
            ttl_ms=5_000,
        )


def test_hash_covers_deterministic_leg_and_client_order_ids() -> None:
    envelope = build_command_envelope(
        _payload(),
        producer_id="test-producer",
        sequence=7,
        ttl_ms=5_000,
        created_at_ms=1_000,
    )
    baseline_hash = command_hash(envelope)
    envelope["spot_client_order_id"] = "bngs_s_wrong"
    assert command_hash(envelope) != baseline_hash


def test_nonpromoted_route_and_invalid_hedge_budget_fail_closed() -> None:
    with pytest.raises(ExecutionProtocolError, match="promotion gates"):
        build_command_envelope(
            {**_payload(), "route_policy": "simultaneous_ioc"},
            producer_id="test-producer",
            sequence=1,
            ttl_ms=5_000,
        )
    with pytest.raises(ExecutionProtocolError, match="finite"):
        build_command_envelope(
            {**_payload(), "max_unhedged_notional_ms": float("inf")},
            producer_id="test-producer",
            sequence=1,
            ttl_ms=5_000,
        )


def test_route_fields_are_immutable_command_semantics() -> None:
    baseline = build_command_envelope(
        _payload(),
        producer_id="test-producer",
        sequence=1,
        ttl_ms=5_000,
        created_at_ms=1_000,
    )
    changed = build_command_envelope(
        {**_payload(), "max_unhedged_notional_ms": 4_000_000.0},
        producer_id="test-producer",
        sequence=2,
        ttl_ms=5_000,
        created_at_ms=1_000,
    )
    assert baseline["command_hash"] != changed["command_hash"]


def test_outbox_is_durable_before_socket_send(tmp_path) -> None:
    db_path = str(tmp_path / "state.db")
    writer = StateWriter(db_path)
    reader = StateReader(db_path)
    socket = MagicMock()

    def assert_ready_before_send(packed: bytes, flags: int) -> None:
        assert flags == zmq.NOBLOCK
        envelope = msgpack.unpackb(packed, raw=False)
        row = reader.get_execution_command_outbox(intent_id="intent-1")[0]
        assert row["state"] == "READY"
        assert row["envelope"] == envelope

    socket.send.side_effect = assert_ready_before_send
    client = _client(writer, socket)
    assert client.send_order_intent(_payload()) is True
    assert reader.get_execution_command_outbox(intent_id="intent-1")[0]["state"] == "SENT"


def test_config_sync_helper_uses_durable_outbox_and_typed_ack(tmp_path) -> None:
    fixture = json.loads(CONFIG_SYNC_GOLDEN_FIXTURE.read_text(encoding="utf-8"))
    db_path = str(tmp_path / "state.db")
    writer = StateWriter(db_path)
    reader = StateReader(db_path)
    socket = MagicMock()
    client = _client(writer, socket)
    client.command_context = {
        "account_id": "account-a",
        "environment": "paper",
        "strategy_id": "funding-v2",
    }
    assert client.send_config_sync(
        intent_id="durable-config-sync-1",
        cycle_id="config-cycle-durable",
        canonical_json=fixture["payload"]["config_canonical_json"],
        config_version_hash=fixture["payload"]["config_version_hash"],
    )
    row = reader.get_execution_command_outbox(intent_id="durable-config-sync-1")[0]
    assert row["state"] == "SENT"
    assert row["intent_type"] == CONFIG_SYNC_INTENT
    sent = msgpack.unpackb(socket.send.call_args.args[0], raw=False)
    assert sent == row["envelope"]
    assert sent["config_canonical_json"] == fixture["payload"]["config_canonical_json"]

    ack = {
        "event": "ConfigAck",
        "schema_version": EXECUTION_PROTOCOL_VERSION,
        "intent_id": "durable-config-sync-1",
        "producer_id": "test-producer",
        "sequence": sent["sequence"],
        "account_id": "account-a",
        "environment": "paper",
        "strategy_id": "funding-v2",
        "cycle_id": "config-cycle-durable",
        "config_version_hash": fixture["payload"]["config_version_hash"],
        "command_hash": sent["command_hash"],
        "ack_status": "TERMINAL",
        "reason": "",
        "event_time_ms": sent["created_at_ms"] + 1,
        "replay": False,
        "declared_config_hash": fixture["payload"]["config_version_hash"],
        "applied_config_hash": fixture["payload"]["config_version_hash"],
        "config_status": "APPLIED",
    }
    assert validate_ack(ack) == ("durable-config-sync-1", "TERMINAL")
    assert client.handle_ack(
        {
            **ack,
            "telemetry_schema_version": 1,
            "telemetry_sequence": 42,
            "telemetry_ack_required": True,
            "telemetry_replay": False,
        }
    )
    assert reader.get_execution_command_outbox(intent_id="durable-config-sync-1")[0][
        "state"
    ] == "TERMINAL"

    with pytest.raises(ExecutionProtocolError, match="inconsistent"):
        validate_ack({**ack, "applied_config_hash": "0" * 64})


def test_send_drop_is_replayable_and_conflicting_duplicate_fails_closed(tmp_path) -> None:
    db_path = str(tmp_path / "state.db")
    writer = StateWriter(db_path)
    reader = StateReader(db_path)
    socket = MagicMock()
    socket.send.side_effect = zmq.ZMQError(zmq.EAGAIN)
    client = _client(writer, socket)

    assert client.send_order_intent(_payload()) is False
    row = reader.get_execution_command_outbox(intent_id="intent-1")[0]
    assert row["state"] == "SEND_FAILED"
    socket.reset_mock()
    assert client.send_order_intent(_payload(quantity=2.0)) is False
    socket.send.assert_not_called()


def test_ack_progress_is_monotonic_and_terminal(tmp_path) -> None:
    db_path = str(tmp_path / "state.db")
    writer = StateWriter(db_path)
    reader = StateReader(db_path)
    socket = MagicMock()
    client = _client(writer, socket)
    assert client.send_order_intent(_payload())
    envelope = reader.get_execution_command_outbox(intent_id="intent-1")[0]["envelope"]

    for state in ("RECEIVED", "VALIDATED", "SUBMITTED", "TERMINAL"):
        assert client.handle_ack(
            {
                "schema_version": EXECUTION_PROTOCOL_VERSION,
                "intent_id": "intent-1",
                "command_hash": envelope["command_hash"],
                "ack_status": state,
                "reason": "" if state != "TERMINAL" else "filled_cycle",
            }
        )
    assert reader.get_execution_command_outbox(intent_id="intent-1")[0]["state"] == "TERMINAL"

    # A delayed intermediate ACK cannot move a terminal command backwards.
    assert client.handle_ack(
        {
            "schema_version": EXECUTION_PROTOCOL_VERSION,
            "intent_id": "intent-1",
            "command_hash": envelope["command_hash"],
            "ack_status": "SUBMITTED",
        }
    )
    assert reader.get_execution_command_outbox(intent_id="intent-1")[0]["state"] == "TERMINAL"


def test_restart_replays_exact_envelope_but_never_expired_command(tmp_path) -> None:
    db_path = str(tmp_path / "state.db")
    writer = StateWriter(db_path)
    first_socket = MagicMock()
    first_socket.send.side_effect = zmq.ZMQError(zmq.EAGAIN)
    first_client = _client(writer, first_socket, ttl_ms=60_000)
    assert first_client.send_order_intent(_payload("restart-intent")) is False
    persisted = writer.get_replayable_execution_commands()[0]["envelope"]
    writer.close()

    restarted_writer = StateWriter(db_path)
    replay_socket = MagicMock()
    restarted = _client(restarted_writer, replay_socket)
    result = restarted.replay_pending(now_ms=int(persisted["created_at_ms"]) + 1)
    assert result == {"sent": 1, "expired": 0, "failed": 0}
    replayed = msgpack.unpackb(replay_socket.send.call_args.args[0], raw=False)
    assert replayed == persisted

    expired_writer = StateWriter(str(tmp_path / "expired.db"))
    expired_socket = MagicMock()
    expired_client = _client(expired_writer, expired_socket, ttl_ms=1)
    assert expired_client.send_order_intent(_payload("expired-intent"))
    expired_socket.reset_mock()
    result = expired_client.replay_pending(now_ms=int(time.time() * 1000) + 10_000)
    assert result["expired"] == 1
    expired_socket.send.assert_not_called()


def test_restart_does_not_replay_entry_while_risk_increase_is_blocked(tmp_path) -> None:
    writer = StateWriter(str(tmp_path / "state.db"))
    failed_socket = MagicMock()
    failed_socket.send.side_effect = zmq.ZMQError(zmq.EAGAIN)
    client = _client(writer, failed_socket, ttl_ms=60_000)
    assert client.send_order_intent(_payload("blocked-entry")) is False

    replay_socket = MagicMock()
    restarted = _client(writer, replay_socket, ttl_ms=60_000)
    blocked = restarted.replay_pending(allow_risk_increase=False)

    assert blocked == {"sent": 0, "expired": 0, "failed": 0, "blocked": 1}
    replay_socket.send.assert_not_called()
    assert len(writer.get_replayable_execution_commands()) == 1

    allowed = restarted.replay_pending(allow_risk_increase=True)
    assert allowed == {"sent": 1, "expired": 0, "failed": 0}
    replay_socket.send.assert_called_once()


def test_unknown_ack_version_is_rejected(tmp_path) -> None:
    writer = StateWriter(str(tmp_path / "state.db"))
    client = _client(writer, MagicMock())
    assert client.send_order_intent(_payload())
    with pytest.raises(ValueError, match="schema_version"):
        client.handle_ack(
            {
                "schema_version": 999,
                "intent_id": "intent-1",
                "ack_status": "RECEIVED",
            }
        )


def test_unknown_ack_field_is_rejected(tmp_path) -> None:
    writer = StateWriter(str(tmp_path / "state.db"))
    client = _client(writer, MagicMock())
    assert client.send_order_intent(_payload())
    envelope = writer.get_replayable_execution_commands()[0]["envelope"]
    with pytest.raises(ValueError, match="unknown ACK field"):
        client.handle_ack(
            {
                "schema_version": EXECUTION_PROTOCOL_VERSION,
                "intent_id": "intent-1",
                "command_hash": envelope["command_hash"],
                "ack_status": "RECEIVED",
                "future_field": True,
            }
        )
