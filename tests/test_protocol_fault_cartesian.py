from __future__ import annotations

from itertools import permutations
import json
from pathlib import Path

import pytest

from bongus.core.config_manager import ConfigManager
from bongus.engine.leg_state_machine import (
    HedgeCycleState,
    Leg,
    LegStatus,
    LegUpdate,
)
from bongus.engine.state_store import StateReader, StateWriter
from bongus.ipc.protocol import ACK_STATES, CONFIG_SYNC_INTENT, RISK_CHANGING_INTENTS


def _risk_payload(intent: str, intent_id: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "intent": intent,
        "intent_id": intent_id,
        "symbol": "BTCUSDT",
        "quantity": 1.0,
        "requested_quantity_decimal": "1",
        "urgency": 0.5,
        "max_slippage_bps": 5.0,
        "exposure_scale": 1.0,
        "account_id": "account-a",
        "environment": "paper",
        "strategy_id": "funding-v2",
        "cycle_id": f"cycle-{intent_id}",
        "config_version_hash": "config-abc",
    }
    if intent.startswith("EXIT_"):
        payload.update(
            {
                "direction": "long" if intent == "EXIT_LONG" else "short",
                "spot_quantity": 1.0,
                "perp_quantity": 1.0,
                "actual_spot_inventory_decimal": "1",
                "actual_futures_inventory_decimal": "1",
                "exit_spot_quantity_decimal": "1",
                "exit_futures_quantity_decimal": "1",
            }
        )
    return payload


def _config_payload(tmp_path: Path, intent_id: str) -> dict[str, object]:
    snapshot = ConfigManager(tmp_path / "absent-live-config.json").canonical_snapshot()
    return {
        "intent": CONFIG_SYNC_INTENT,
        "intent_id": intent_id,
        "account_id": "account-a",
        "environment": "paper",
        "strategy_id": "funding-v2",
        "cycle_id": f"cycle-{intent_id}",
        "config_version_hash": snapshot.sha256,
        "config_canonical_json": snapshot.canonical_json,
    }


def _ack(envelope: dict[str, object], status: str) -> dict[str, object]:
    event: dict[str, object] = {
        "schema_version": 3,
        "intent_id": envelope["intent_id"],
        "command_hash": envelope["command_hash"],
        "ack_status": status,
        "reason": "fault-matrix",
    }
    if envelope["intent"] == CONFIG_SYNC_INTENT:
        config_hash = str(envelope["config_version_hash"])
        event.update(
            {
                "event": "ConfigAck",
                "config_version_hash": config_hash,
                "declared_config_hash": config_hash,
                "applied_config_hash": config_hash if status != "REJECTED" else "",
                "config_status": "APPLIED" if status != "REJECTED" else "REJECTED",
            }
        )
    return event


def _outbox_rows(writer: StateWriter) -> list[dict[str, object]]:
    return [
        dict(row)
        for row in writer._command_conn.execute(
            """SELECT intent_id, sequence, state, command_hash
               FROM execution_command_outbox ORDER BY sequence"""
        ).fetchall()
    ]


def _outbox_state(writer: StateWriter, intent_id: str) -> str:
    row = writer._command_conn.execute(
        "SELECT state FROM execution_command_outbox WHERE intent_id = ?",
        (intent_id,),
    ).fetchone()
    assert row is not None
    return str(row["state"])


def test_drop_duplicate_and_reorder_every_durable_intent_and_ack(tmp_path: Path) -> None:
    db_path = str(tmp_path / "state.db")
    writer = StateWriter(db_path)
    payloads = [
        _risk_payload(intent, f"intent-{intent.lower()}")
        for intent in sorted(RISK_CHANGING_INTENTS)
    ]
    payloads.append(_config_payload(tmp_path, "intent-config-sync"))
    envelopes: list[dict[str, object]] = []
    try:
        # Every command is durably committed, then its transport delivery is
        # dropped. Re-reserving the exact semantic command is a duplicate and
        # returns the original immutable envelope/sequence.
        for payload in reversed(payloads):
            envelope = writer.reserve_execution_command(
                dict(payload),
                producer_id="fault-matrix",
                ttl_ms=60_000,
                created_at_ms=1_000,
            )
            envelopes.append(envelope)
            writer.mark_execution_command_send_failed(
                str(envelope["intent_id"]), "injected_transport_drop"
            )
            duplicate = writer.reserve_execution_command(
                dict(payload),
                producer_id="fault-matrix",
                ttl_ms=60_000,
                created_at_ms=9_999,
            )
            assert duplicate == envelope
        assert len(_outbox_rows(writer)) == len(payloads)
    finally:
        writer.close()

    restarted = StateWriter(db_path)
    try:
        replay = restarted.get_replayable_execution_commands()
        # Input order was deliberately reversed, while restart replay is always
        # the durable producer sequence—never socket arrival order.
        assert [row["sequence"] for row in replay] == list(
            range(1, len(payloads) + 1)
        )
        assert [row["envelope"] for row in replay] == envelopes

        intermediate = ("RECEIVED", "VALIDATED", "SUBMITTED")
        for row in replay:
            envelope = row["envelope"]
            intent_id = str(envelope["intent_id"])
            # Config sync has one typed terminal/rejected ACK. Risk commands
            # additionally emit the three monotonic intermediate states.
            if envelope["intent"] != CONFIG_SYNC_INTENT:
                for status in intermediate:
                    restarted.apply_execution_command_ack(_ack(envelope, status))
            restarted.apply_execution_command_ack(_ack(envelope, "TERMINAL"))
            restarted.apply_execution_command_ack(_ack(envelope, "TERMINAL"))
            assert _outbox_state(restarted, intent_id) == "TERMINAL"

        # Drop each ACK kind independently for every risk-changing intent. A
        # missing intermediate cannot stop later terminal truth; a missing
        # terminal/rejection leaves the command replayable and nonterminal.
        causal_ack_path = (*intermediate, "TERMINAL")
        for intent_index, intent in enumerate(sorted(RISK_CHANGING_INTENTS)):
            for omitted in sorted(ACK_STATES):
                matrix_id = f"drop-ack-{intent_index}-{omitted.lower()}"
                envelope = restarted.reserve_execution_command(
                    _risk_payload(intent, matrix_id),
                    producer_id="fault-matrix",
                    ttl_ms=60_000,
                    created_at_ms=1_500 + intent_index,
                )
                branch = (
                    (*intermediate, "REJECTED")
                    if omitted == "REJECTED"
                    else causal_ack_path
                )
                for status in branch:
                    if status == omitted:
                        continue
                    restarted.apply_execution_command_ack(_ack(envelope, status))
                    restarted.apply_execution_command_ack(_ack(envelope, status))
                expected = (
                    "SUBMITTED"
                    if omitted in {"TERMINAL", "REJECTED"}
                    else branch[-1]
                )
                assert _outbox_state(restarted, matrix_id) == expected

        # Exercise all six reorderings and duplicates for each non-config
        # command on fresh rows. The maximum observed progress wins.
        for command_index, intent in enumerate(sorted(RISK_CHANGING_INTENTS)):
            for permutation_index, ordering in enumerate(permutations(intermediate)):
                matrix_id = f"ack-{command_index}-{permutation_index}"
                envelope = restarted.reserve_execution_command(
                    _risk_payload(intent, matrix_id),
                    producer_id="fault-matrix",
                    ttl_ms=60_000,
                    created_at_ms=2_000 + permutation_index,
                )
                for status in ordering:
                    restarted.apply_execution_command_ack(_ack(envelope, status))
                    restarted.apply_execution_command_ack(_ack(envelope, status))
                assert _outbox_state(restarted, matrix_id) == "SUBMITTED"
                restarted.apply_execution_command_ack(_ack(envelope, "REJECTED"))
                assert _outbox_state(restarted, matrix_id) == "REJECTED"
                with pytest.raises(ValueError, match="terminal ACK conflict"):
                    restarted.apply_execution_command_ack(_ack(envelope, "TERMINAL"))

        assert ACK_STATES == {
            "RECEIVED",
            "VALIDATED",
            "SUBMITTED",
            "TERMINAL",
            "REJECTED",
        }
    finally:
        restarted.close()


def _leg_update(
    *,
    leg: Leg,
    status: LegStatus,
    cumulative: str,
    sequence: int,
    event_id: str,
    verified: bool = False,
) -> LegUpdate:
    return LegUpdate(
        event_id=event_id,
        leg=leg,
        status=status,
        cumulative_quantity=cumulative,
        event_time_ms=1_000 + sequence,
        sequence=sequence,
        order_id=f"order-{leg.value}",
        client_order_id=f"client-{leg.value}",
        exchange_verified=verified,
    )


def test_drop_duplicate_and_reorder_every_exchange_leg_state() -> None:
    exchange_states = {
        LegStatus.WORKING: "0",
        LegStatus.PARTIAL: "0.25",
        LegStatus.CANCEL_PENDING: "0.25",
        LegStatus.FILLED: "1",
        LegStatus.CANCELLED: "0.25",
        LegStatus.EXPIRED: "0.25",
        LegStatus.REJECTED: "0",
    }
    for leg in Leg:
        for index, (status, cumulative) in enumerate(exchange_states.items(), start=1):
            update = _leg_update(
                leg=leg,
                status=status,
                cumulative=cumulative,
                sequence=index,
                event_id=f"{leg.value}-{status.value}",
            )

            duplicate_cycle = HedgeCycleState.entry(
                f"duplicate-{leg.value}-{status.value}", "1"
            )
            first = duplicate_cycle.apply(update)
            duplicate = duplicate_cycle.apply(update)
            assert first.applied
            assert duplicate.duplicate and not duplicate.applied

            # Reconciliation arriving first makes every delayed stream state
            # stale, including a same-quantity FILLED update.
            reordered = HedgeCycleState.entry(
                f"reorder-{leg.value}-{status.value}", "1"
            )
            reordered.apply(
                _leg_update(
                    leg=leg,
                    status=LegStatus.FILLED,
                    cumulative="1",
                    sequence=100,
                    event_id=f"{leg.value}-authoritative-{status.value}",
                    verified=True,
                )
            )
            delayed = reordered.apply(update)
            assert delayed.stale and not delayed.applied

            # Dropping the selected stream update never grants completion. Both
            # legs need later authoritative terminal reconciliation.
            dropped = HedgeCycleState.entry(
                f"drop-{leg.value}-{status.value}", "1"
            )
            assert not dropped.safe_to_project_complete
            for reconciled_leg in Leg:
                dropped.apply(
                    _leg_update(
                        leg=reconciled_leg,
                        status=LegStatus.FILLED,
                        cumulative="1",
                        sequence=200 + index,
                        event_id=(
                            f"{leg.value}-{status.value}-reconcile-"
                            f"{reconciled_leg.value}"
                        ),
                        verified=True,
                    )
                )
            assert dropped.safe_to_project_complete


def _snapshot_after_crash(
    cycle: HedgeCycleState,
) -> HedgeCycleState:
    return HedgeCycleState.from_snapshot(
        json.loads(json.dumps(cycle.to_snapshot(), sort_keys=True))
    )


def test_crash_before_and_after_every_execution_transition(tmp_path: Path) -> None:
    def stage_cycle(stage: str) -> tuple[HedgeCycleState, LegUpdate]:
        cycle = HedgeCycleState.entry(f"crash-{stage}", "1")
        if stage in {"partial_fill", "cancel"}:
            cycle.apply(
                _leg_update(
                    leg=Leg.SPOT,
                    status=LegStatus.PARTIAL,
                    cumulative="0.1",
                    sequence=1,
                    event_id=f"{stage}-seed-partial",
                )
            )
        if stage == "cancel":
            transition = _leg_update(
                leg=Leg.SPOT,
                status=LegStatus.CANCELLED,
                cumulative="0.1",
                sequence=2,
                event_id="cancel-transition",
                verified=True,
            )
        elif stage == "first_fill":
            transition = _leg_update(
                leg=Leg.SPOT,
                status=LegStatus.PARTIAL,
                cumulative="0.1",
                sequence=1,
                event_id="first-fill-transition",
            )
        elif stage == "partial_fill":
            transition = _leg_update(
                leg=Leg.SPOT,
                status=LegStatus.PARTIAL,
                cumulative="0.5",
                sequence=2,
                event_id="partial-fill-transition",
            )
        elif stage == "hedge":
            cycle.apply(
                _leg_update(
                    leg=Leg.SPOT,
                    status=LegStatus.FILLED,
                    cumulative="1",
                    sequence=1,
                    event_id="hedge-seed-spot",
                )
            )
            transition = _leg_update(
                leg=Leg.PERP,
                status=LegStatus.FILLED,
                cumulative="1",
                sequence=2,
                event_id="hedge-transition",
            )
        elif stage == "terminal_event":
            cycle.apply(
                _leg_update(
                    leg=Leg.SPOT,
                    status=LegStatus.FILLED,
                    cumulative="1",
                    sequence=1,
                    event_id="terminal-seed-spot",
                    verified=True,
                )
            )
            transition = _leg_update(
                leg=Leg.PERP,
                status=LegStatus.FILLED,
                cumulative="1",
                sequence=2,
                event_id="terminal-transition",
                verified=True,
            )
        else:
            raise AssertionError(stage)
        return cycle, transition

    for stage in ("first_fill", "partial_fill", "cancel", "hedge", "terminal_event"):
        before_cycle, before_transition = stage_cycle(stage)
        crashed_before = _snapshot_after_crash(before_cycle)
        crashed_before.apply(before_transition)

        after_cycle, after_transition = stage_cycle(stage)
        after_cycle.apply(after_transition)
        crashed_after = _snapshot_after_crash(after_cycle)
        duplicate = crashed_after.apply(after_transition)
        assert duplicate.duplicate and not duplicate.applied
        assert crashed_before.to_snapshot() == crashed_after.to_snapshot()

    # Intent commit and REST-submit ACK are exercised through actual SQLite
    # close/reopen boundaries on both sides of the transition.
    for boundary in ("intent_commit", "rest_submit"):
        for crash_side in ("before", "after"):
            path = str(tmp_path / f"{boundary}-{crash_side}.db")
            payload = _risk_payload("ENTER_LONG", f"{boundary}-{crash_side}")
            writer = StateWriter(path)
            envelope: dict[str, object] | None = None
            if boundary == "intent_commit" and crash_side == "after":
                envelope = writer.reserve_execution_command(
                    payload,
                    producer_id="crash-matrix",
                    ttl_ms=60_000,
                    created_at_ms=1_000,
                )
            elif boundary == "rest_submit":
                envelope = writer.reserve_execution_command(
                    payload,
                    producer_id="crash-matrix",
                    ttl_ms=60_000,
                    created_at_ms=1_000,
                )
                if crash_side == "after":
                    writer.apply_execution_command_ack(_ack(envelope, "SUBMITTED"))
            writer.close()

            restarted = StateWriter(path)
            try:
                if envelope is None:
                    envelope = restarted.reserve_execution_command(
                        payload,
                        producer_id="crash-matrix",
                        ttl_ms=60_000,
                        created_at_ms=1_000,
                    )
                if boundary == "rest_submit":
                    restarted.apply_execution_command_ack(_ack(envelope, "SUBMITTED"))
                    assert _outbox_state(
                        restarted, str(envelope["intent_id"])
                    ) == "SUBMITTED"
                else:
                    assert len(_outbox_rows(restarted)) == 1
            finally:
                restarted.close()

    # Projection commit is atomic and idempotent on either side of process
    # death: there is exactly one event and one projection.
    for crash_side in ("before", "after"):
        path = str(tmp_path / f"projection-{crash_side}.db")
        position = {
            "symbol": "ETHUSDT",
            "side": "LONG_SPOT_SHORT_PERP",
            "direction": "long",
            "spot_entry": 100.0,
            "perp_entry": 101.0,
            "qty": 1.0,
            "hedge_ratio": 1.0,
            "trading_mode": "paper",
        }
        writer = StateWriter(path)
        if crash_side == "after":
            assert writer.project_entry_lifecycle(
                event_key="projection-entry",
                intent_id="projection-intent",
                event_time="2026-07-18T12:00:00+00:00",
                position_fields=position,
                evidence={"exchange_trade_id": "entry-trade"},
            )
        writer.close()

        restarted = StateWriter(path)
        reader = StateReader(path)
        try:
            inserted = restarted.project_entry_lifecycle(
                event_key="projection-entry",
                intent_id="projection-intent",
                event_time="2026-07-18T12:00:00+00:00",
                position_fields=position,
                evidence={"exchange_trade_id": "entry-trade"},
            )
            assert inserted is (crash_side == "before")
            assert [row["symbol"] for row in reader.get_positions()] == ["ETHUSDT"]
            assert restarted.conn.execute(
                "SELECT COUNT(*) FROM lifecycle_events"
            ).fetchone()[0] == 1
        finally:
            reader.close()
            restarted.close()
