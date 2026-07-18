"""Deterministic reference model for a two-leg hedge execution cycle.

The Rust order actor is the latency-sensitive implementation.  This module is
the deliberately small, serializable oracle used by recovery, deterministic
simulation and cross-language contract tests.  It treats exchange cumulative
quantity as authoritative, makes duplicate effects idempotent, and never calls
a cycle complete until both legs have been reconciled with the exchange.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Mapping


class ExecutionInvariantError(ValueError):
    """Raised when accepting an event would hide an execution invariant breach."""


class Leg(str, Enum):
    SPOT = "spot"
    PERP = "perp"


class LegStatus(str, Enum):
    CREATED = "created"
    WORKING = "working"
    PARTIAL = "partial"
    CANCEL_PENDING = "cancel_pending"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    REJECTED = "rejected"
    UNKNOWN = "unknown"

    @property
    def terminal(self) -> bool:
        return self in {
            LegStatus.FILLED,
            LegStatus.CANCELLED,
            LegStatus.EXPIRED,
            LegStatus.REJECTED,
        }


_STATUS_ALIASES: dict[str, LegStatus] = {
    "CREATED": LegStatus.CREATED,
    "PENDING_NEW": LegStatus.WORKING,
    "NEW": LegStatus.WORKING,
    "OPEN": LegStatus.WORKING,
    "WORKING": LegStatus.WORKING,
    "PARTIAL": LegStatus.PARTIAL,
    "PARTIALLY_FILLED": LegStatus.PARTIAL,
    "PENDING_CANCEL": LegStatus.CANCEL_PENDING,
    "CANCEL_PENDING": LegStatus.CANCEL_PENDING,
    "FILLED": LegStatus.FILLED,
    "CANCELED": LegStatus.CANCELLED,
    "CANCELLED": LegStatus.CANCELLED,
    "EXPIRED": LegStatus.EXPIRED,
    "EXPIRED_IN_MATCH": LegStatus.EXPIRED,
    "REJECTED": LegStatus.REJECTED,
}


def _decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ExecutionInvariantError(f"{name} must be numeric")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ExecutionInvariantError(f"{name} must be a finite decimal") from exc
    if not result.is_finite():
        raise ExecutionInvariantError(f"{name} must be a finite decimal")
    return result


def _status(value: str | LegStatus) -> LegStatus:
    if isinstance(value, LegStatus):
        return value
    return _STATUS_ALIASES.get(str(value).strip().upper(), LegStatus.UNKNOWN)


def _sign(value: Decimal) -> Decimal:
    return Decimal("1") if value >= 0 else Decimal("-1")


@dataclass(frozen=True, slots=True)
class LegUpdate:
    event_id: str
    leg: Leg
    status: LegStatus | str
    cumulative_quantity: Decimal | str | float
    event_time_ms: int
    order_id: str = ""
    client_order_id: str = ""
    sequence: int | None = None
    exchange_verified: bool = False
    source: str = "stream"

    def fingerprint(self) -> str:
        payload = {
            "client_order_id": self.client_order_id,
            "cumulative_quantity": str(_decimal(self.cumulative_quantity, "cumulative_quantity")),
            "event_time_ms": int(self.event_time_ms),
            "exchange_verified": bool(self.exchange_verified),
            "leg": self.leg.value,
            "order_id": self.order_id,
            "sequence": self.sequence,
            "source": self.source,
            "status": _status(self.status).value,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class Transition:
    applied: bool
    duplicate: bool
    stale: bool
    fill_delta: Decimal
    previous_status: LegStatus
    current_status: LegStatus
    mismatch_quantity: Decimal
    breaches: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RepairIntent:
    leg: Leg
    side: str
    quantity: Decimal
    reduce_only: bool
    reason: str


@dataclass(slots=True)
class LegExecutionState:
    leg: Leg
    target_signed_quantity: Decimal
    cumulative_quantity: Decimal = Decimal("0")
    status: LegStatus = LegStatus.CREATED
    order_id: str = ""
    client_order_id: str = ""
    last_sequence: int | None = None
    last_event_time_ms: int = 0
    exchange_verified: bool = False
    breaches: list[str] = field(default_factory=list)

    @property
    def target_quantity(self) -> Decimal:
        return abs(self.target_signed_quantity)

    @property
    def signed_filled_quantity(self) -> Decimal:
        return _sign(self.target_signed_quantity) * self.cumulative_quantity

    @property
    def remaining_quantity(self) -> Decimal:
        return max(Decimal("0"), self.target_quantity - self.cumulative_quantity)

    @property
    def verified_terminal(self) -> bool:
        return self.exchange_verified and self.status.terminal


@dataclass(slots=True)
class HedgeCycleState:
    cycle_id: str
    spot: LegExecutionState
    perp: LegExecutionState
    initial_spot_quantity: Decimal = Decimal("0")
    initial_perp_quantity: Decimal = Decimal("0")
    perp_delta_multiplier: Decimal = Decimal("1")
    quantity_tolerance: Decimal = Decimal("0.00000001")
    seen_events: dict[str, str] = field(default_factory=dict)
    breaches: list[str] = field(default_factory=list)
    risk_notional_ms: Decimal = Decimal("0")
    _risk_observed_at_ms: int | None = None
    _risk_notional: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        self.initial_spot_quantity = _decimal(self.initial_spot_quantity, "initial_spot_quantity")
        self.initial_perp_quantity = _decimal(self.initial_perp_quantity, "initial_perp_quantity")
        self.perp_delta_multiplier = _decimal(self.perp_delta_multiplier, "perp_delta_multiplier")
        self.quantity_tolerance = _decimal(self.quantity_tolerance, "quantity_tolerance")
        if not self.cycle_id.strip():
            raise ExecutionInvariantError("cycle_id is required")
        if self.perp_delta_multiplier <= 0:
            raise ExecutionInvariantError("perp_delta_multiplier must be positive")
        if self.quantity_tolerance < 0:
            raise ExecutionInvariantError("quantity_tolerance must be non-negative")
        if self.spot.leg is not Leg.SPOT or self.perp.leg is not Leg.PERP:
            raise ExecutionInvariantError("cycle must contain one spot and one perp leg")
        if abs(self.target_end_delta) > self.quantity_tolerance:
            raise ExecutionInvariantError("target transactions do not finish delta neutral")

    @classmethod
    def entry(
        cls,
        cycle_id: str,
        quantity: Decimal | str | float,
        *,
        direction: str = "long_spot_short_perp",
        perp_delta_multiplier: Decimal | str | float = Decimal("1"),
        quantity_tolerance: Decimal | str | float = Decimal("0.00000001"),
    ) -> HedgeCycleState:
        qty = _decimal(quantity, "quantity")
        multiplier = _decimal(perp_delta_multiplier, "perp_delta_multiplier")
        if qty <= 0 or multiplier <= 0:
            raise ExecutionInvariantError("quantity and multiplier must be positive")
        normalized = direction.strip().lower()
        if normalized == "long_spot_short_perp":
            spot_target = qty
        elif normalized == "short_spot_long_perp":
            spot_target = -qty
        else:
            raise ExecutionInvariantError("unsupported hedge direction")
        perp_target = -(spot_target / multiplier)
        return cls(
            cycle_id=cycle_id,
            spot=LegExecutionState(Leg.SPOT, spot_target),
            perp=LegExecutionState(Leg.PERP, perp_target),
            perp_delta_multiplier=multiplier,
            quantity_tolerance=_decimal(quantity_tolerance, "quantity_tolerance"),
        )

    @classmethod
    def exit(
        cls,
        cycle_id: str,
        *,
        spot_quantity: Decimal | str | float,
        perp_quantity: Decimal | str | float,
        perp_delta_multiplier: Decimal | str | float = Decimal("1"),
        quantity_tolerance: Decimal | str | float = Decimal("0.00000001"),
    ) -> HedgeCycleState:
        spot = _decimal(spot_quantity, "spot_quantity")
        perp = _decimal(perp_quantity, "perp_quantity")
        multiplier = _decimal(perp_delta_multiplier, "perp_delta_multiplier")
        tolerance = _decimal(quantity_tolerance, "quantity_tolerance")
        if abs(spot + perp * multiplier) > tolerance:
            raise ExecutionInvariantError("starting position is not delta neutral")
        return cls(
            cycle_id=cycle_id,
            spot=LegExecutionState(Leg.SPOT, -spot),
            perp=LegExecutionState(Leg.PERP, -perp),
            initial_spot_quantity=spot,
            initial_perp_quantity=perp,
            perp_delta_multiplier=multiplier,
            quantity_tolerance=tolerance,
        )

    @property
    def target_end_delta(self) -> Decimal:
        return (
            self.initial_spot_quantity
            + self.spot.target_signed_quantity
            + (self.initial_perp_quantity + self.perp.target_signed_quantity)
            * self.perp_delta_multiplier
        )

    @property
    def current_spot_quantity(self) -> Decimal:
        return self.initial_spot_quantity + self.spot.signed_filled_quantity

    @property
    def current_perp_quantity(self) -> Decimal:
        return self.initial_perp_quantity + self.perp.signed_filled_quantity

    @property
    def mismatch_quantity(self) -> Decimal:
        return self.current_spot_quantity + self.current_perp_quantity * self.perp_delta_multiplier

    @property
    def hedged(self) -> bool:
        return abs(self.mismatch_quantity) <= self.quantity_tolerance

    @property
    def verified_terminal(self) -> bool:
        return self.spot.verified_terminal and self.perp.verified_terminal and self.hedged

    @property
    def safe_to_project_complete(self) -> bool:
        return self.verified_terminal and not self.breaches and not self.spot.breaches and not self.perp.breaches

    def _leg(self, leg: Leg) -> LegExecutionState:
        return self.spot if leg is Leg.SPOT else self.perp

    def apply(self, update: LegUpdate) -> Transition:
        if not update.event_id.strip():
            raise ExecutionInvariantError("event_id is required")
        fingerprint = update.fingerprint()
        prior = self.seen_events.get(update.event_id)
        state = self._leg(update.leg)
        if prior is not None:
            if prior != fingerprint:
                raise ExecutionInvariantError(f"event_id collision: {update.event_id}")
            return Transition(
                applied=False,
                duplicate=True,
                stale=False,
                fill_delta=Decimal("0"),
                previous_status=state.status,
                current_status=state.status,
                mismatch_quantity=self.mismatch_quantity,
            )

        cumulative = _decimal(update.cumulative_quantity, "cumulative_quantity")
        if cumulative < 0:
            raise ExecutionInvariantError("cumulative_quantity must be non-negative")
        incoming_status = _status(update.status)
        stale_sequence = (
            update.sequence is not None
            and state.last_sequence is not None
            and update.sequence < state.last_sequence
        )
        stale_time = update.event_time_ms < state.last_event_time_ms
        if cumulative < state.cumulative_quantity:
            if stale_sequence or stale_time:
                self.seen_events[update.event_id] = fingerprint
                return Transition(
                    applied=False,
                    duplicate=False,
                    stale=True,
                    fill_delta=Decimal("0"),
                    previous_status=state.status,
                    current_status=state.status,
                    mismatch_quantity=self.mismatch_quantity,
                )
            raise ExecutionInvariantError("newer cumulative quantity regressed")

        previous_status = state.status
        fill_delta = cumulative - state.cumulative_quantity
        event_breaches: list[str] = []
        if incoming_status is LegStatus.UNKNOWN:
            event_breaches.append("unknown_exchange_status")
        if cumulative > state.target_quantity + self.quantity_tolerance:
            event_breaches.append("overfill")
        if fill_delta > 0 and previous_status.terminal:
            event_breaches.append("late_fill_after_terminal")
        if incoming_status is LegStatus.FILLED and cumulative + self.quantity_tolerance < state.target_quantity:
            event_breaches.append("filled_status_below_target")
        if incoming_status is not LegStatus.FILLED and cumulative >= state.target_quantity - self.quantity_tolerance:
            # Quantity is more reliable than a delayed status label.  Preserve
            # the inconsistency as evidence but expose the economic terminal.
            event_breaches.append("target_filled_with_nonfilled_status")
            incoming_status = LegStatus.FILLED

        if stale_sequence and fill_delta == 0:
            self.seen_events[update.event_id] = fingerprint
            return Transition(
                applied=False,
                duplicate=False,
                stale=True,
                fill_delta=Decimal("0"),
                previous_status=previous_status,
                current_status=previous_status,
                mismatch_quantity=self.mismatch_quantity,
            )

        for identity_name, prior_value, incoming_value in (
            ("order_id", state.order_id, update.order_id),
            ("client_order_id", state.client_order_id, update.client_order_id),
        ):
            if prior_value and incoming_value and prior_value != incoming_value:
                raise ExecutionInvariantError(f"{identity_name} changed within a leg")

        state.cumulative_quantity = cumulative
        state.status = incoming_status
        state.order_id = state.order_id or update.order_id
        state.client_order_id = state.client_order_id or update.client_order_id
        if update.sequence is not None:
            state.last_sequence = max(state.last_sequence or update.sequence, update.sequence)
        state.last_event_time_ms = max(state.last_event_time_ms, int(update.event_time_ms))
        state.exchange_verified = bool(update.exchange_verified)
        state.breaches.extend(item for item in event_breaches if item not in state.breaches)
        self.breaches.extend(
            f"{update.leg.value}:{item}"
            for item in event_breaches
            if f"{update.leg.value}:{item}" not in self.breaches
        )
        self.seen_events[update.event_id] = fingerprint
        return Transition(
            applied=True,
            duplicate=False,
            stale=False,
            fill_delta=fill_delta,
            previous_status=previous_status,
            current_status=state.status,
            mismatch_quantity=self.mismatch_quantity,
            breaches=tuple(event_breaches),
        )

    def residual_repair(self, *, prefer_leg: Leg = Leg.PERP, emergency_reduce: bool = False) -> RepairIntent | None:
        mismatch = self.mismatch_quantity
        if abs(mismatch) <= self.quantity_tolerance:
            return None

        # Positive mismatch is long delta and needs a sell; negative mismatch
        # needs a buy.  Perpetual quantity is converted through its multiplier.
        side = "SELL" if mismatch > 0 else "BUY"
        leg = prefer_leg
        quantity = abs(mismatch)
        if leg is Leg.PERP:
            quantity /= self.perp_delta_multiplier
        return RepairIntent(
            leg=leg,
            side=side,
            quantity=quantity,
            reduce_only=bool(emergency_reduce and leg is Leg.PERP),
            reason="emergency_delta_reduction" if emergency_reduce else "residual_hedge",
        )

    def observe_risk(self, *, now_ms: int, mark_price: Decimal | str | float) -> Decimal:
        price = _decimal(mark_price, "mark_price")
        if price <= 0:
            raise ExecutionInvariantError("mark_price must be positive")
        if self._risk_observed_at_ms is not None:
            if now_ms < self._risk_observed_at_ms:
                raise ExecutionInvariantError("risk observation time regressed")
            self.risk_notional_ms += self._risk_notional * Decimal(now_ms - self._risk_observed_at_ms)
        self._risk_observed_at_ms = int(now_ms)
        self._risk_notional = abs(self.mismatch_quantity) * price
        return self.risk_notional_ms

    def to_snapshot(self) -> dict[str, Any]:
        def leg_payload(state: LegExecutionState) -> dict[str, Any]:
            payload = asdict(state)
            payload["leg"] = state.leg.value
            payload["status"] = state.status.value
            for key in ("target_signed_quantity", "cumulative_quantity"):
                payload[key] = str(payload[key])
            return payload

        return {
            "schema_version": 1,
            "cycle_id": self.cycle_id,
            "spot": leg_payload(self.spot),
            "perp": leg_payload(self.perp),
            "initial_spot_quantity": str(self.initial_spot_quantity),
            "initial_perp_quantity": str(self.initial_perp_quantity),
            "perp_delta_multiplier": str(self.perp_delta_multiplier),
            "quantity_tolerance": str(self.quantity_tolerance),
            "seen_events": dict(sorted(self.seen_events.items())),
            "breaches": list(self.breaches),
            "risk_notional_ms": str(self.risk_notional_ms),
            "risk_observed_at_ms": self._risk_observed_at_ms,
            "risk_notional": str(self._risk_notional),
        }

    @classmethod
    def from_snapshot(cls, payload: Mapping[str, Any]) -> HedgeCycleState:
        if int(payload.get("schema_version", 0)) != 1:
            raise ExecutionInvariantError("unsupported hedge-cycle snapshot schema")

        def leg_state(raw: Mapping[str, Any], expected: Leg) -> LegExecutionState:
            if Leg(str(raw.get("leg"))) is not expected:
                raise ExecutionInvariantError("snapshot leg identity mismatch")
            return LegExecutionState(
                leg=expected,
                target_signed_quantity=_decimal(raw.get("target_signed_quantity"), "target_signed_quantity"),
                cumulative_quantity=_decimal(raw.get("cumulative_quantity", "0"), "cumulative_quantity"),
                status=LegStatus(str(raw.get("status", LegStatus.UNKNOWN.value))),
                order_id=str(raw.get("order_id", "")),
                client_order_id=str(raw.get("client_order_id", "")),
                last_sequence=raw.get("last_sequence"),
                last_event_time_ms=int(raw.get("last_event_time_ms", 0)),
                exchange_verified=bool(raw.get("exchange_verified", False)),
                breaches=[str(item) for item in raw.get("breaches", [])],
            )

        cycle = cls(
            cycle_id=str(payload.get("cycle_id", "")),
            spot=leg_state(dict(payload.get("spot", {})), Leg.SPOT),
            perp=leg_state(dict(payload.get("perp", {})), Leg.PERP),
            initial_spot_quantity=_decimal(payload.get("initial_spot_quantity", "0"), "initial_spot_quantity"),
            initial_perp_quantity=_decimal(payload.get("initial_perp_quantity", "0"), "initial_perp_quantity"),
            perp_delta_multiplier=_decimal(payload.get("perp_delta_multiplier", "1"), "perp_delta_multiplier"),
            quantity_tolerance=_decimal(payload.get("quantity_tolerance", "0.00000001"), "quantity_tolerance"),
            seen_events={str(key): str(value) for key, value in dict(payload.get("seen_events", {})).items()},
            breaches=[str(item) for item in payload.get("breaches", [])],
            risk_notional_ms=_decimal(payload.get("risk_notional_ms", "0"), "risk_notional_ms"),
        )
        observed = payload.get("risk_observed_at_ms")
        cycle._risk_observed_at_ms = None if observed is None else int(observed)
        cycle._risk_notional = _decimal(payload.get("risk_notional", "0"), "risk_notional")
        return cycle

