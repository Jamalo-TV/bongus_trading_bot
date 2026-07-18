from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bongus.supervisor.incidents import (
    IncidentCoordinator,
    IncidentObservation,
    IncidentTransitionError,
    RecoveryRecipe,
    RecoveryResult,
)
from bongus.supervisor.models import IncidentScope, IncidentSeverity, IncidentState
from bongus.supervisor.store import SupervisorStore


NOW = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def coordinator(tmp_path):
    store = SupervisorStore(str(tmp_path / "state.db"))
    result = IncidentCoordinator(store)
    yield result
    store.close()


def observation(**overrides):
    values = {
        "incident_key": "feed:BTCUSDT:gap",
        "category": "feed_gap",
        "scope_type": IncidentScope.SYMBOL,
        "scope_value": "BTCUSDT",
        "severity": IncidentSeverity.WARNING,
        "owner": "market-data",
        "recipe_id": "backfill",
        "max_attempts": 3,
        "evidence": {"expected_sequence": 10, "actual_sequence": 12},
    }
    values.update(overrides)
    return IncidentObservation(**values)


def test_observe_is_durable_deduplicated_and_monotonically_escalates(coordinator) -> None:
    first = coordinator.observe(observation(), now=NOW)
    second = coordinator.observe(
        observation(severity=IncidentSeverity.CRITICAL, evidence={"actual_sequence": 15}),
        now=NOW + timedelta(seconds=1),
    )
    assert second["incident_id"] == first["incident_id"]
    assert second["occurrences"] == 2
    assert second["severity"] == IncidentSeverity.CRITICAL.value
    assert second["evidence"]["expected_sequence"] == 10
    assert second["evidence"]["actual_sequence"] == 15
    assert [item["event_type"] for item in coordinator.events(first["incident_id"])] == [
        "OBSERVED",
        "REOBSERVED",
    ]


def test_failed_recipe_uses_persistent_exponential_backoff(coordinator) -> None:
    calls = []
    coordinator.register_recipe(
        RecoveryRecipe(
            "backfill",
            lambda incident: calls.append(incident["attempt_count"])
            or RecoveryResult(False, False, "endpoint unavailable"),
            base_backoff_seconds=10,
            max_backoff_seconds=100,
        )
    )
    incident = coordinator.observe(observation(), now=NOW)
    first = coordinator.run_due(now=NOW)
    assert first[0]["state"] == IncidentState.WAITING.value
    assert first[0]["next_attempt_at"] == (NOW + timedelta(seconds=10)).isoformat()
    assert coordinator.run_due(now=NOW + timedelta(seconds=9)) == []
    second = coordinator.run_due(now=NOW + timedelta(seconds=10))
    assert second[0]["next_attempt_at"] == (NOW + timedelta(seconds=30)).isoformat()
    assert calls == [1, 2]
    assert coordinator.get(incident["incident_id"])["attempt_count"] == 2


def test_retry_exhaustion_is_terminal_until_operator_intervention(coordinator) -> None:
    coordinator.register_recipe(
        RecoveryRecipe("backfill", lambda _: RecoveryResult(False, False, "still broken"), base_backoff_seconds=1)
    )
    incident = coordinator.observe(observation(max_attempts=2), now=NOW)
    coordinator.run_due(now=NOW)
    exhausted = coordinator.run_due(now=NOW + timedelta(seconds=1))[0]
    assert exhausted["state"] == IncidentState.EXHAUSTED.value
    assert coordinator.run_due(now=NOW + timedelta(days=1)) == []
    assert coordinator.get(incident["incident_id"])["attempt_count"] == 2


def test_critical_success_needs_proof_and_operator_ack(coordinator) -> None:
    coordinator.register_recipe(
        RecoveryRecipe(
            "backfill",
            lambda _: RecoveryResult(True, True, "gap backfilled and sequence reconciled", {"cursor": 15}),
            auto_resolve_allowed=True,
        )
    )
    incident = coordinator.observe(observation(severity=IncidentSeverity.CRITICAL), now=NOW)
    recovered = coordinator.run_due(now=NOW)[0]
    assert recovered["state"] == IncidentState.ACK_REQUIRED.value
    assert recovered["evidence"]["invariants_proven"] is True
    acknowledged = coordinator.acknowledge(
        incident["incident_id"], acknowledged_by="operator@example", now=NOW + timedelta(minutes=1)
    )
    assert acknowledged["state"] == IncidentState.RESOLVED.value
    assert acknowledged["acknowledged_by"] == "operator@example"


def test_success_without_invariant_proof_never_clears(coordinator) -> None:
    coordinator.register_recipe(
        RecoveryRecipe(
            "backfill",
            lambda _: RecoveryResult(True, False, "request returned 200 but cursor was not checked"),
            base_backoff_seconds=5,
            auto_resolve_allowed=True,
        )
    )
    incident = coordinator.observe(observation(), now=NOW)
    result = coordinator.run_due(now=NOW)[0]
    assert result["state"] == IncidentState.WAITING.value
    with pytest.raises(IncidentTransitionError):
        coordinator.acknowledge(incident["incident_id"], acknowledged_by="operator", now=NOW)


def test_low_risk_recipe_can_auto_resolve_only_when_explicitly_allowed(coordinator) -> None:
    coordinator.register_recipe(
        RecoveryRecipe(
            "backfill",
            lambda _: RecoveryResult(True, True, "symbol feed recovered"),
            auto_resolve_allowed=True,
        )
    )
    incident = coordinator.observe(observation(severity=IncidentSeverity.INFO), now=NOW)
    resolved = coordinator.run_due(now=NOW)[0]
    assert resolved["state"] == IncidentState.RESOLVED.value
    assert coordinator.get(incident["incident_id"])["resolved_at"] == NOW.isoformat()


def test_scope_validation_prevents_ambiguous_global_or_symbol_incidents(coordinator) -> None:
    with pytest.raises(ValueError, match="global incidents"):
        coordinator.observe(
            observation(scope_type=IncidentScope.GLOBAL, scope_value="BTCUSDT"),
            now=NOW,
        )
    with pytest.raises(ValueError, match="require a scope value"):
        coordinator.observe(observation(scope_value=""), now=NOW)


def test_restart_uses_same_durable_retry_budget(tmp_path) -> None:
    path = str(tmp_path / "state.db")
    first_store = SupervisorStore(path)
    first = IncidentCoordinator(first_store)
    first.register_recipe(
        RecoveryRecipe("backfill", lambda _: RecoveryResult(False, False, "failure"), base_backoff_seconds=10)
    )
    incident = first.observe(observation(), now=NOW)
    first.run_due(now=NOW)
    first_store.close()

    second_store = SupervisorStore(path)
    second = IncidentCoordinator(second_store)
    assert second.run_due(now=NOW + timedelta(seconds=9)) == []
    restored = second.get(incident["incident_id"])
    assert restored is not None and restored["attempt_count"] == 1
    second_store.close()


def test_restart_campaign_preserves_every_incident_state_scope_and_ack_rule(
    tmp_path,
) -> None:
    """Reopen every durable state, including a crash during recovery claim."""

    expected_after_restart = {
        IncidentState.OPEN: IncidentState.OPEN,
        IncidentState.RECOVERING: IncidentState.WAITING,
        IncidentState.WAITING: IncidentState.WAITING,
        IncidentState.ACK_REQUIRED: IncidentState.ACK_REQUIRED,
        IncidentState.EXHAUSTED: IncidentState.EXHAUSTED,
        IncidentState.RESOLVED: IncidentState.RESOLVED,
    }

    for index, initial_state in enumerate(IncidentState):
        path = str(tmp_path / f"incident_{initial_state.value.lower()}.db")
        first_store = SupervisorStore(path)
        first = IncidentCoordinator(first_store)
        scoped = observation(
            incident_key=f"feed:BTCUSDT:{index}",
            scope_type=IncidentScope.SYMBOL,
            scope_value="BTCUSDT",
            severity=(
                IncidentSeverity.CRITICAL
                if initial_state is IncidentState.ACK_REQUIRED
                else IncidentSeverity.WARNING
            ),
            max_attempts=1 if initial_state is IncidentState.EXHAUSTED else 3,
        )
        incident = first.observe(scoped, now=NOW)

        if initial_state is IncidentState.RECOVERING:
            claimed = first._claim(incident["incident_id"], now=NOW)
            assert claimed is not None
        elif initial_state in {IncidentState.WAITING, IncidentState.EXHAUSTED}:
            first.register_recipe(
                RecoveryRecipe(
                    "backfill",
                    lambda _: RecoveryResult(False, False, "still broken"),
                    base_backoff_seconds=60,
                )
            )
            transitioned = first.run_due(now=NOW)[0]
            assert transitioned["state"] == initial_state.value
        elif initial_state in {IncidentState.ACK_REQUIRED, IncidentState.RESOLVED}:
            first.register_recipe(
                RecoveryRecipe(
                    "backfill",
                    lambda _: RecoveryResult(
                        True,
                        True,
                        "authoritative cursor and invariant proof complete",
                    ),
                    auto_resolve_allowed=True,
                )
            )
            transitioned = first.run_due(now=NOW)[0]
            assert transitioned["state"] == initial_state.value

        first_store.close()
        reopened_store = SupervisorStore(path)
        reopened = IncidentCoordinator(reopened_store)
        restored = reopened.get(incident["incident_id"])
        assert restored is not None
        assert restored["state"] == expected_after_restart[initial_state].value
        assert restored["scope_type"] == IncidentScope.SYMBOL.value
        assert restored["scope_value"] == "BTCUSDT"

        if initial_state is IncidentState.RECOVERING:
            assert restored["attempt_count"] == 1
            assert reopened.events(incident["incident_id"])[-1]["event_type"] == (
                "ATTEMPT_INTERRUPTED"
            )
        if initial_state is IncidentState.ACK_REQUIRED:
            with pytest.raises(IncidentTransitionError, match="acknowledged_by"):
                reopened.acknowledge(
                    incident["incident_id"], acknowledged_by="", now=NOW
                )
            acknowledged = reopened.acknowledge(
                incident["incident_id"],
                acknowledged_by="operator@example",
                now=NOW + timedelta(minutes=1),
            )
            assert acknowledged["state"] == IncidentState.RESOLVED.value
            assert acknowledged["acknowledged_by"] == "operator@example"
        else:
            with pytest.raises(IncidentTransitionError):
                reopened.acknowledge(
                    incident["incident_id"],
                    acknowledged_by="operator@example",
                    now=NOW,
                )
        reopened_store.close()
