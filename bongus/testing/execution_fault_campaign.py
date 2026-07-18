"""Seeded fault campaign for the two-leg execution reference model.

The campaign deliberately injects the failure classes that are hard to cover
with example tests: duplicate delivery, within-leg reordering, stream drops,
event-id collisions, cancel/fill ambiguity, and crash/restart at arbitrary
states.  Exchange reconciliation is always the final authority.

This is a verification tool, not a live trading component.  A release gate can
request one million traces without making the ordinary unit suite slow.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from concurrent.futures import ProcessPoolExecutor
import json
import random
import time
from typing import Any

from bongus.engine.leg_state_machine import (
    ExecutionInvariantError,
    HedgeCycleState,
    Leg,
    LegUpdate,
)


@dataclass(slots=True)
class FaultCampaignResult:
    schema_version: int
    seed: int
    traces_requested: int
    traces_completed: int = 0
    events_applied: int = 0
    duplicate_deliveries: int = 0
    duplicate_exchange_effects: int = 0
    stale_deliveries: int = 0
    dropped_deliveries: int = 0
    crash_restarts: int = 0
    event_id_collisions: int = 0
    expected_rejections: int = 0
    cancel_fill_ambiguities: int = 0
    safe_completions: int = 0
    blocked_ambiguous_completions: int = 0
    invariant_failures: int = 0
    first_failure: str = ""
    elapsed_seconds: float = 0.0

    @property
    def passed(self) -> bool:
        return (
            self.traces_completed == self.traces_requested
            and self.invariant_failures == 0
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["passed"] = self.passed
        return payload


def _update(
    *,
    trace: int,
    label: str,
    leg: Leg,
    status: str,
    cumulative: Decimal,
    sequence: int,
    verified: bool = False,
) -> LegUpdate:
    return LegUpdate(
        event_id=f"t{trace}:{label}",
        leg=leg,
        status=status,
        cumulative_quantity=cumulative,
        event_time_ms=1_000 + sequence,
        sequence=sequence,
        order_id=f"order-{trace}-{leg.value}",
        client_order_id=f"bngs_{leg.value[0]}_{trace:024x}"[-36:],
        exchange_verified=verified,
        source="reconciliation" if verified else "stream",
    )


def _assert_invariants(cycle: HedgeCycleState) -> None:
    tolerance = cycle.quantity_tolerance
    for state in (cycle.spot, cycle.perp):
        if state.cumulative_quantity < 0:
            raise AssertionError("negative cumulative fill")
        if state.cumulative_quantity > state.target_quantity + tolerance:
            raise AssertionError("unrecorded overfill")

    recomputed = (
        cycle.current_spot_quantity
        + cycle.current_perp_quantity * cycle.perp_delta_multiplier
    )
    if cycle.mismatch_quantity != recomputed:
        raise AssertionError("delta mismatch is not derived from leg truth")

    repair = cycle.residual_repair(emergency_reduce=True)
    if abs(cycle.mismatch_quantity) <= tolerance:
        if repair is not None:
            raise AssertionError("repair emitted for a neutral cycle")
    else:
        if repair is None or repair.quantity <= 0:
            raise AssertionError("non-neutral cycle has no positive repair")
        if repair.leg is Leg.PERP:
            repaired_delta = repair.quantity * cycle.perp_delta_multiplier
        else:
            repaired_delta = repair.quantity
        if abs(repaired_delta - abs(cycle.mismatch_quantity)) > tolerance:
            raise AssertionError("repair does not exactly match residual delta")

    if cycle.safe_to_project_complete:
        if not cycle.verified_terminal or not cycle.hedged:
            raise AssertionError("unsafe cycle projected complete")
        if cycle.breaches or cycle.spot.breaches or cycle.perp.breaches:
            raise AssertionError("breached cycle projected complete")


def _make_cycle(trace: int) -> HedgeCycleState:
    multiplier = Decimal("0.1") if trace % 11 == 0 else Decimal("1")
    direction = (
        "short_spot_long_perp" if trace % 7 == 0 else "long_spot_short_perp"
    )
    if trace % 5:
        return HedgeCycleState.entry(
            f"fault-{trace}",
            "1",
            direction=direction,
            perp_delta_multiplier=multiplier,
        )
    spot = Decimal("-1") if direction == "short_spot_long_perp" else Decimal("1")
    perp = -(spot / multiplier)
    return HedgeCycleState.exit(
        f"fault-{trace}",
        spot_quantity=spot,
        perp_quantity=perp,
        perp_delta_multiplier=multiplier,
    )


def _run_trace(trace: int, rng: random.Random, result: FaultCampaignResult) -> None:
    cycle = _make_cycle(trace)
    partial_ratio = (Decimal("0.25"), Decimal("0.5"), Decimal("0.75"))[trace % 3]
    ambiguous_cancel = trace % 17 == 0

    events: list[LegUpdate] = []
    for leg, target, offset in (
        (Leg.SPOT, cycle.spot.target_quantity, 0),
        (Leg.PERP, cycle.perp.target_quantity, 10),
    ):
        partial = target * partial_ratio
        events.append(
            _update(
                trace=trace,
                label=f"{leg.value}-partial",
                leg=leg,
                status="PARTIALLY_FILLED",
                cumulative=partial,
                sequence=offset + 1,
            )
        )
        if ambiguous_cancel and leg is Leg.SPOT:
            events.append(
                _update(
                    trace=trace,
                    label="spot-cancel",
                    leg=leg,
                    status="CANCELED",
                    cumulative=partial,
                    sequence=offset + 2,
                    verified=True,
                )
            )
        else:
            events.append(
                _update(
                    trace=trace,
                    label=f"{leg.value}-fill",
                    leg=leg,
                    status="FILLED",
                    cumulative=target,
                    sequence=offset + 2,
                )
            )

    # Cross-leg ordering is arbitrary.  Every fourth trace also reverses each
    # leg's causal delivery order, which must turn the older cumulative event
    # into stale evidence rather than regress state.
    rng.shuffle(events)
    if trace % 4 == 0:
        events.sort(key=lambda event: (event.leg.value, -int(event.sequence or 0)))

    # A stream message can disappear; reconciliation below still proves final
    # exchange state.  Never drop every stream event so the trace remains useful.
    if trace % 9 == 0 and len(events) > 1:
        drop_candidates = [
            index
            for index, event in enumerate(events)
            if not event.event_id.endswith(":spot-cancel")
        ]
        events.pop(drop_candidates[trace % len(drop_candidates)])
        result.dropped_deliveries += 1

    duplicate_index = trace % len(events) if trace % 3 == 0 else -1
    restart_index = trace % (len(events) + 1)
    collision_injected = False

    for index, event in enumerate(events):
        transition = cycle.apply(event)
        result.events_applied += int(transition.applied)
        result.stale_deliveries += int(transition.stale)
        _assert_invariants(cycle)

        if index == duplicate_index:
            duplicate = cycle.apply(event)
            if not duplicate.duplicate or duplicate.applied:
                result.duplicate_exchange_effects += int(duplicate.applied)
                raise AssertionError("duplicate delivery changed state")
            result.duplicate_deliveries += 1

        if trace % 29 == 0 and not collision_injected:
            collision_injected = True
            collision = LegUpdate(
                event_id=event.event_id,
                leg=event.leg,
                status=event.status,
                cumulative_quantity=Decimal(str(event.cumulative_quantity)) + Decimal("0.01"),
                event_time_ms=event.event_time_ms + 1,
                sequence=(event.sequence or 0) + 1,
                order_id=event.order_id,
                client_order_id=event.client_order_id,
            )
            try:
                cycle.apply(collision)
            except ExecutionInvariantError:
                result.expected_rejections += 1
                result.event_id_collisions += 1
            else:
                raise AssertionError("event-id collision was accepted")

        if index == restart_index:
            snapshot = json.loads(
                json.dumps(cycle.to_snapshot(), sort_keys=True, separators=(",", ":"))
            )
            cycle = HedgeCycleState.from_snapshot(snapshot)
            result.crash_restarts += 1
            _assert_invariants(cycle)

    if restart_index == len(events):
        cycle = HedgeCycleState.from_snapshot(cycle.to_snapshot())
        result.crash_restarts += 1

    # A final exchange query is authoritative after any stream drop/reorder.
    # In the cancel-race case this exposes a late fill and deliberately prevents
    # the projection from claiming a clean completion.
    for leg, target, offset in (
        (Leg.SPOT, cycle.spot.target_quantity, 100),
        (Leg.PERP, cycle.perp.target_quantity, 110),
    ):
        reconciliation = _update(
            trace=trace,
            label=f"{leg.value}-reconciled",
            leg=leg,
            status="FILLED",
            cumulative=target,
            sequence=offset,
            verified=True,
        )
        cycle.apply(reconciliation)
        result.events_applied += 1
        _assert_invariants(cycle)

    if ambiguous_cancel:
        result.cancel_fill_ambiguities += 1
        if cycle.safe_to_project_complete:
            raise AssertionError("cancel/fill ambiguity projected as safe")
        if "spot:late_fill_after_terminal" not in cycle.breaches:
            raise AssertionError("cancel/fill ambiguity was not recorded")
        result.blocked_ambiguous_completions += 1
    else:
        if not cycle.safe_to_project_complete:
            raise AssertionError(f"clean reconciled cycle did not complete: {cycle.breaches}")
        result.safe_completions += 1


def run_execution_fault_campaign(
    *,
    traces: int,
    seed: int = 20_260_718,
    fail_fast: bool = True,
) -> FaultCampaignResult:
    """Run ``traces`` deterministic randomized execution histories."""

    if traces <= 0:
        raise ValueError("traces must be positive")
    result = FaultCampaignResult(
        schema_version=1,
        seed=int(seed),
        traces_requested=int(traces),
    )
    rng = random.Random(seed)
    started = time.perf_counter()
    for trace in range(traces):
        try:
            _run_trace(trace, rng, result)
            result.traces_completed += 1
        except Exception as exc:  # noqa: BLE001 - this is a fault harness boundary
            result.invariant_failures += 1
            if not result.first_failure:
                result.first_failure = f"trace={trace}: {type(exc).__name__}: {exc}"
            if fail_fast:
                break
    result.elapsed_seconds = round(time.perf_counter() - started, 6)
    return result


def _run_campaign_shard(arguments: tuple[int, int]) -> FaultCampaignResult:
    traces, seed = arguments
    return run_execution_fault_campaign(traces=traces, seed=seed)


def run_parallel_execution_fault_campaign(
    *,
    traces: int,
    seed: int = 20_260_718,
    workers: int = 1,
) -> FaultCampaignResult:
    """Run the same deterministic campaign across independent process shards."""

    if workers <= 1:
        return run_execution_fault_campaign(traces=traces, seed=seed)
    if traces <= 0:
        raise ValueError("traces must be positive")
    worker_count = min(int(workers), int(traces))
    base, remainder = divmod(int(traces), worker_count)
    shards = [
        (base + int(index < remainder), seed + index * 1_000_003)
        for index in range(worker_count)
    ]
    started = time.perf_counter()
    with ProcessPoolExecutor(max_workers=worker_count) as executor:
        results = list(executor.map(_run_campaign_shard, shards))

    aggregate = FaultCampaignResult(
        schema_version=1,
        seed=seed,
        traces_requested=traces,
    )
    count_fields = (
        "traces_completed",
        "events_applied",
        "duplicate_deliveries",
        "duplicate_exchange_effects",
        "stale_deliveries",
        "dropped_deliveries",
        "crash_restarts",
        "event_id_collisions",
        "expected_rejections",
        "cancel_fill_ambiguities",
        "safe_completions",
        "blocked_ambiguous_completions",
        "invariant_failures",
    )
    for field_name in count_fields:
        setattr(
            aggregate,
            field_name,
            sum(int(getattr(result, field_name)) for result in results),
        )
    for index, result in enumerate(results):
        if result.first_failure:
            aggregate.first_failure = f"worker={index}: {result.first_failure}"
            break
    aggregate.elapsed_seconds = round(time.perf_counter() - started, 6)
    return aggregate
