from __future__ import annotations

import json
from decimal import Decimal

import pytest

from bongus.engine.leg_state_machine import (
    ExecutionInvariantError,
    HedgeCycleState,
    Leg,
    LegStatus,
    LegUpdate,
)


def update(
    event_id: str,
    leg: Leg,
    status: str,
    cumulative: str,
    *,
    sequence: int,
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
        client_order_id=f"bng-cycle-{leg.value}",
        exchange_verified=verified,
    )


def test_partial_fills_are_cumulative_and_repairs_only_the_residual() -> None:
    cycle = HedgeCycleState.entry("cycle-1", "2")
    first = cycle.apply(update("s1", Leg.SPOT, "PARTIALLY_FILLED", "0.75", sequence=1))
    assert first.fill_delta == Decimal("0.75")
    assert cycle.mismatch_quantity == Decimal("0.75")
    repair = cycle.residual_repair()
    assert repair is not None
    assert repair.leg is Leg.PERP
    assert repair.side == "SELL"
    assert repair.quantity == Decimal("0.75")

    cycle.apply(update("p1", Leg.PERP, "PARTIALLY_FILLED", "0.50", sequence=1))
    repair = cycle.residual_repair()
    assert repair is not None
    assert repair.quantity == Decimal("0.25")


def test_duplicate_is_idempotent_and_event_id_collision_is_rejected() -> None:
    cycle = HedgeCycleState.entry("cycle-1", "1")
    event = update("same", Leg.SPOT, "PARTIALLY_FILLED", "0.4", sequence=1)
    assert cycle.apply(event).applied
    duplicate = cycle.apply(event)
    assert duplicate.duplicate and not duplicate.applied
    assert cycle.spot.cumulative_quantity == Decimal("0.4")

    with pytest.raises(ExecutionInvariantError, match="event_id collision"):
        cycle.apply(update("same", Leg.SPOT, "PARTIALLY_FILLED", "0.5", sequence=2))


def test_reordered_regressive_event_is_stale_but_newer_regression_is_invalid() -> None:
    cycle = HedgeCycleState.entry("cycle-1", "1")
    cycle.apply(update("new", Leg.SPOT, "PARTIALLY_FILLED", "0.6", sequence=10))
    stale = cycle.apply(update("old", Leg.SPOT, "PARTIALLY_FILLED", "0.2", sequence=2))
    assert stale.stale
    assert cycle.spot.cumulative_quantity == Decimal("0.6")

    with pytest.raises(ExecutionInvariantError, match="cumulative quantity regressed"):
        cycle.apply(update("bad", Leg.SPOT, "PARTIALLY_FILLED", "0.5", sequence=11))


def test_cancel_ambiguity_records_late_fill_and_refuses_safe_completion() -> None:
    cycle = HedgeCycleState.entry("cycle-1", "1")
    cycle.apply(update("cancel", Leg.SPOT, "CANCELED", "0.2", sequence=2, verified=True))
    late = cycle.apply(update("late", Leg.SPOT, "CANCELED", "0.4", sequence=3, verified=True))
    assert "late_fill_after_terminal" in late.breaches
    assert cycle.spot.cumulative_quantity == Decimal("0.4")
    assert not cycle.safe_to_project_complete


def test_terminal_requires_both_exchange_reconciled_legs() -> None:
    cycle = HedgeCycleState.entry("cycle-1", "1")
    cycle.apply(update("s", Leg.SPOT, "FILLED", "1", sequence=1))
    cycle.apply(update("p", Leg.PERP, "FILLED", "1", sequence=1))
    assert cycle.hedged
    assert not cycle.verified_terminal

    # Reconciliation events use new IDs and the same cumulative exchange truth.
    cycle.apply(update("sr", Leg.SPOT, "FILLED", "1", sequence=2, verified=True))
    cycle.apply(update("pr", Leg.PERP, "FILLED", "1", sequence=2, verified=True))
    assert cycle.verified_terminal
    assert cycle.safe_to_project_complete


def test_exit_cycle_tracks_starting_inventory_and_finishes_flat() -> None:
    cycle = HedgeCycleState.exit("exit-1", spot_quantity="2", perp_quantity="-2")
    cycle.apply(update("s", Leg.SPOT, "FILLED", "2", sequence=1, verified=True))
    assert cycle.mismatch_quantity == Decimal("-2")
    repair = cycle.residual_repair(prefer_leg=Leg.PERP)
    assert repair is not None and repair.side == "BUY" and repair.quantity == Decimal("2")
    cycle.apply(update("p", Leg.PERP, "FILLED", "2", sequence=1, verified=True))
    assert cycle.current_spot_quantity == 0
    assert cycle.current_perp_quantity == 0
    assert cycle.safe_to_project_complete


def test_multiplier_and_emergency_reduce_only_semantics() -> None:
    cycle = HedgeCycleState.entry("cycle-1", "1", perp_delta_multiplier="0.1")
    cycle.apply(update("s", Leg.SPOT, "PARTIALLY_FILLED", "0.5", sequence=1))
    repair = cycle.residual_repair(emergency_reduce=True)
    assert repair is not None
    assert repair.quantity == Decimal("5")
    assert repair.reduce_only


def test_overfill_is_preserved_as_evidence_and_blocks_projection() -> None:
    cycle = HedgeCycleState.entry("cycle-1", "1")
    transition = cycle.apply(update("over", Leg.SPOT, "FILLED", "1.1", sequence=1, verified=True))
    assert "overfill" in transition.breaches
    assert cycle.spot.cumulative_quantity == Decimal("1.1")
    assert not cycle.safe_to_project_complete


def test_unhedged_notional_time_integrates_piecewise() -> None:
    cycle = HedgeCycleState.entry("cycle-1", "1")
    cycle.observe_risk(now_ms=1_000, mark_price="100")
    cycle.apply(update("s", Leg.SPOT, "PARTIALLY_FILLED", "0.5", sequence=1))
    cycle.observe_risk(now_ms=2_000, mark_price="100")
    cycle.observe_risk(now_ms=4_000, mark_price="110")
    assert cycle.risk_notional_ms == Decimal("100000")


def test_snapshot_round_trip_preserves_idempotency_and_is_deterministic() -> None:
    cycle = HedgeCycleState.entry("cycle-1", "1")
    event = update("s", Leg.SPOT, "PARTIALLY_FILLED", "0.25", sequence=1)
    cycle.apply(event)
    encoded = json.dumps(cycle.to_snapshot(), sort_keys=True, separators=(",", ":"))
    restored = HedgeCycleState.from_snapshot(json.loads(encoded))
    assert restored.to_snapshot() == cycle.to_snapshot()
    assert restored.apply(event).duplicate

