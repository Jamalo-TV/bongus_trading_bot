"""Durable, scoped incident coordination with proof-gated recovery.

An incident is not a transient boolean.  It has an owner, bounded recipe,
persistent retry budget and an append-only audit trail.  Recovery can propose a
clear, but exposure/accounting incidents remain blocked until exchange/state
invariants are explicitly proven (and critical incidents are acknowledged).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping
from uuid import uuid4

from bongus.supervisor.models import IncidentScope, IncidentSeverity, IncidentState
from bongus.supervisor.store import SupervisorStore


class IncidentTransitionError(RuntimeError):
    pass


_SEVERITY_ORDER = {
    IncidentSeverity.INFO: 0,
    IncidentSeverity.WARNING: 1,
    IncidentSeverity.CRITICAL: 2,
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _decode_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    decoded = json.loads(value)
    return decoded if isinstance(decoded, dict) else {"value": decoded}


@dataclass(frozen=True, slots=True)
class IncidentObservation:
    incident_key: str
    category: str
    scope_type: IncidentScope
    scope_value: str
    severity: IncidentSeverity
    owner: str
    recipe_id: str
    max_attempts: int = 5
    requires_ack: bool = False
    evidence: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    success: bool
    invariants_proven: bool
    note: str
    evidence: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class RecoveryRecipe:
    recipe_id: str
    handler: Callable[[dict[str, Any]], RecoveryResult]
    base_backoff_seconds: float = 30.0
    max_backoff_seconds: float = 1_800.0
    auto_resolve_allowed: bool = False


class IncidentCoordinator:
    def __init__(self, store: SupervisorStore) -> None:
        self.store = store
        self._recipes: dict[str, RecoveryRecipe] = {}
        self._recover_interrupted_attempts()

    def _recover_interrupted_attempts(self) -> None:
        """Make a process crash during a claimed recovery restart-safe.

        ``RECOVERING`` is an in-process lease: no handler can still own it when
        a new coordinator is constructed for the supervisor process.  The
        already-counted attempt remains counted.  A remaining budget returns
        the incident to ``WAITING`` for a bounded retry; an exhausted budget is
        terminal and still requires operator intervention.
        """

        now = _utc_now()
        conn = self.store.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                "SELECT * FROM supervisor_incidents WHERE state = 'RECOVERING'"
            ).fetchall()
            for row in rows:
                exhausted = int(row["attempt_count"]) >= int(row["max_attempts"])
                next_state = (
                    IncidentState.EXHAUSTED if exhausted else IncidentState.WAITING
                )
                next_attempt_at = None if exhausted else _iso(now)
                conn.execute(
                    """UPDATE supervisor_incidents
                       SET state = ?, next_attempt_at = ?,
                           last_error = ?, updated_at = ?, version = version + 1
                       WHERE incident_id = ? AND state = 'RECOVERING'""",
                    (
                        next_state.value,
                        next_attempt_at,
                        "recovery interrupted by supervisor restart",
                        _iso(now),
                        str(row["incident_id"]),
                    ),
                )
                self._append_event(
                    conn,
                    incident_id=str(row["incident_id"]),
                    event_time=now,
                    event_type="ATTEMPT_INTERRUPTED",
                    actor="coordinator_startup",
                    prior_state=IncidentState.RECOVERING.value,
                    next_state=next_state.value,
                    note="claimed recovery was interrupted by supervisor restart",
                    details={
                        "attempt": int(row["attempt_count"]),
                        "max_attempts": int(row["max_attempts"]),
                    },
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def register_recipe(self, recipe: RecoveryRecipe) -> None:
        if not recipe.recipe_id.strip():
            raise ValueError("recipe_id is required")
        if recipe.base_backoff_seconds < 0 or recipe.max_backoff_seconds < recipe.base_backoff_seconds:
            raise ValueError("invalid recipe backoff")
        self._recipes[recipe.recipe_id] = recipe

    def observe(
        self,
        observation: IncidentObservation,
        *,
        now: datetime | None = None,
        actor: str = "detector",
    ) -> dict[str, Any]:
        timestamp = now or _utc_now()
        self._validate_observation(observation)
        conn = self.store.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM supervisor_incidents WHERE incident_key = ? AND state != 'RESOLVED'",
                (observation.incident_key,),
            ).fetchone()
            if row is None:
                incident_id = uuid4().hex
                evidence = dict(observation.evidence or {})
                conn.execute(
                    """INSERT INTO supervisor_incidents
                       (incident_id, incident_key, category, scope_type, scope_value,
                        severity, owner, recipe_id, state, occurrences, attempt_count,
                        max_attempts, next_attempt_at, requires_ack, evidence_json,
                        opened_at, updated_at, version)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?, ?, ?, ?, ?, 1)""",
                    (
                        incident_id,
                        observation.incident_key,
                        observation.category,
                        observation.scope_type.value,
                        observation.scope_value,
                        observation.severity.value,
                        observation.owner,
                        observation.recipe_id,
                        IncidentState.OPEN.value,
                        observation.max_attempts,
                        _iso(timestamp),
                        int(observation.requires_ack),
                        json.dumps(evidence, sort_keys=True, separators=(",", ":")),
                        _iso(timestamp),
                        _iso(timestamp),
                    ),
                )
                self._append_event(
                    conn,
                    incident_id=incident_id,
                    event_time=timestamp,
                    event_type="OBSERVED",
                    actor=actor,
                    prior_state="",
                    next_state=IncidentState.OPEN.value,
                    note="incident opened",
                    details=evidence,
                )
            else:
                incident_id = str(row["incident_id"])
                current_severity = IncidentSeverity(str(row["severity"]))
                severity = max(
                    (current_severity, observation.severity),
                    key=lambda item: _SEVERITY_ORDER[item],
                )
                merged_evidence = _decode_json(row["evidence_json"])
                merged_evidence.update(dict(observation.evidence or {}))
                conn.execute(
                    """UPDATE supervisor_incidents
                       SET severity = ?, occurrences = occurrences + 1,
                           owner = ?, requires_ack = MAX(requires_ack, ?),
                           evidence_json = ?, updated_at = ?, version = version + 1
                       WHERE incident_id = ?""",
                    (
                        severity.value,
                        observation.owner,
                        int(observation.requires_ack),
                        json.dumps(merged_evidence, sort_keys=True, separators=(",", ":")),
                        _iso(timestamp),
                        incident_id,
                    ),
                )
                self._append_event(
                    conn,
                    incident_id=incident_id,
                    event_time=timestamp,
                    event_type="REOBSERVED",
                    actor=actor,
                    prior_state=str(row["state"]),
                    next_state=str(row["state"]),
                    note="active incident observed again",
                    details=dict(observation.evidence or {}),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        incident = self.get(incident_id)
        if incident is None:  # pragma: no cover - transactional invariant
            raise IncidentTransitionError("incident vanished after commit")
        return incident

    def due(self, *, now: datetime | None = None, limit: int = 25) -> list[dict[str, Any]]:
        timestamp = _iso(now or _utc_now())
        rows = self.store.conn.execute(
            """SELECT * FROM supervisor_incidents
               WHERE state IN ('OPEN', 'WAITING')
                 AND attempt_count < max_attempts
                 AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
               ORDER BY CASE severity WHEN 'CRITICAL' THEN 0 WHEN 'WARNING' THEN 1 ELSE 2 END,
                        opened_at, incident_id
               LIMIT ?""",
            (timestamp, max(1, int(limit))),
        ).fetchall()
        return [self._decode(row) for row in rows]

    def run_due(self, *, now: datetime | None = None, limit: int = 25) -> list[dict[str, Any]]:
        timestamp = now or _utc_now()
        results: list[dict[str, Any]] = []
        for incident in self.due(now=timestamp, limit=limit):
            recipe = self._recipes.get(str(incident["recipe_id"]))
            if recipe is None:
                results.append(
                    self._finish_attempt(
                        str(incident["incident_id"]),
                        RecoveryResult(False, False, "recovery recipe is not registered"),
                        recipe=None,
                        now=timestamp,
                    )
                )
                continue
            claimed = self._claim(str(incident["incident_id"]), now=timestamp)
            if claimed is None:
                continue
            try:
                outcome = recipe.handler(claimed)
            except Exception as exc:  # recovery failures must become durable state
                outcome = RecoveryResult(False, False, f"recipe raised {type(exc).__name__}: {exc}")
            results.append(self._finish_attempt(str(incident["incident_id"]), outcome, recipe=recipe, now=timestamp))
        return results

    def acknowledge(
        self,
        incident_id: str,
        *,
        acknowledged_by: str,
        now: datetime | None = None,
        note: str = "operator acknowledged verified recovery",
    ) -> dict[str, Any]:
        if not acknowledged_by.strip():
            raise IncidentTransitionError("acknowledged_by is required")
        timestamp = now or _utc_now()
        conn = self.store.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM supervisor_incidents WHERE incident_id = ?",
                (incident_id,),
            ).fetchone()
            if row is None or str(row["state"]) != IncidentState.ACK_REQUIRED.value:
                raise IncidentTransitionError("incident is not awaiting acknowledgement")
            evidence = _decode_json(row["evidence_json"])
            if not bool(evidence.get("invariants_proven")):
                raise IncidentTransitionError("cannot acknowledge without invariant proof")
            conn.execute(
                """UPDATE supervisor_incidents SET state = ?, resolved_at = ?,
                   acknowledged_at = ?, acknowledged_by = ?, updated_at = ?, version = version + 1
                   WHERE incident_id = ?""",
                (
                    IncidentState.RESOLVED.value,
                    _iso(timestamp),
                    _iso(timestamp),
                    acknowledged_by,
                    _iso(timestamp),
                    incident_id,
                ),
            )
            self._append_event(
                conn,
                incident_id=incident_id,
                event_time=timestamp,
                event_type="ACKNOWLEDGED",
                actor=acknowledged_by,
                prior_state=IncidentState.ACK_REQUIRED.value,
                next_state=IncidentState.RESOLVED.value,
                note=note,
                details={},
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        incident = self.get(incident_id)
        assert incident is not None
        return incident

    def get(self, incident_id: str) -> dict[str, Any] | None:
        row = self.store.conn.execute(
            "SELECT * FROM supervisor_incidents WHERE incident_id = ?",
            (incident_id,),
        ).fetchone()
        return None if row is None else self._decode(row)

    def list_active(self, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.store.conn.execute(
            """SELECT * FROM supervisor_incidents WHERE state != 'RESOLVED'
               ORDER BY CASE severity WHEN 'CRITICAL' THEN 0 WHEN 'WARNING' THEN 1 ELSE 2 END,
                        opened_at, incident_id LIMIT ?""",
            (max(1, int(limit)),),
        ).fetchall()
        return [self._decode(row) for row in rows]

    def events(self, incident_id: str) -> list[dict[str, Any]]:
        rows = self.store.conn.execute(
            """SELECT * FROM supervisor_incident_events WHERE incident_id = ?
               ORDER BY event_time, event_id""",
            (incident_id,),
        ).fetchall()
        return [
            {
                **dict(row),
                "details": _decode_json(row["details_json"]),
            }
            for row in rows
        ]

    def _claim(self, incident_id: str, *, now: datetime) -> dict[str, Any] | None:
        conn = self.store.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM supervisor_incidents WHERE incident_id = ?",
                (incident_id,),
            ).fetchone()
            if row is None or str(row["state"]) not in {
                IncidentState.OPEN.value,
                IncidentState.WAITING.value,
            }:
                conn.rollback()
                return None
            if int(row["attempt_count"]) >= int(row["max_attempts"]):
                conn.rollback()
                return None
            prior_state = str(row["state"])
            conn.execute(
                """UPDATE supervisor_incidents SET state = ?, attempt_count = attempt_count + 1,
                   updated_at = ?, version = version + 1 WHERE incident_id = ?""",
                (IncidentState.RECOVERING.value, _iso(now), incident_id),
            )
            self._append_event(
                conn,
                incident_id=incident_id,
                event_time=now,
                event_type="ATTEMPT_STARTED",
                actor="coordinator",
                prior_state=prior_state,
                next_state=IncidentState.RECOVERING.value,
                note="bounded recovery attempt started",
                details={"attempt": int(row["attempt_count"]) + 1},
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return self.get(incident_id)

    def _finish_attempt(
        self,
        incident_id: str,
        outcome: RecoveryResult,
        *,
        recipe: RecoveryRecipe | None,
        now: datetime,
    ) -> dict[str, Any]:
        # Missing recipes have not been claimed yet; claim them so retry budgets
        # and audit history remain consistent across restarts.
        current = self.get(incident_id)
        if current is None:
            raise IncidentTransitionError("unknown incident")
        if current["state"] != IncidentState.RECOVERING.value:
            claimed = self._claim(incident_id, now=now)
            if claimed is None:
                return current
            current = claimed

        evidence = dict(current.get("evidence", {}))
        evidence.update(dict(outcome.evidence or {}))
        evidence["invariants_proven"] = bool(outcome.invariants_proven)
        attempts = int(current["attempt_count"])
        max_attempts = int(current["max_attempts"])
        requires_ack = bool(current["requires_ack"]) or current["severity"] == IncidentSeverity.CRITICAL.value

        if outcome.success and outcome.invariants_proven:
            if requires_ack or recipe is None or not recipe.auto_resolve_allowed:
                next_state = IncidentState.ACK_REQUIRED
                resolved_at: str | None = None
            else:
                next_state = IncidentState.RESOLVED
                resolved_at = _iso(now)
            next_attempt_at: str | None = None
        elif attempts >= max_attempts:
            next_state = IncidentState.EXHAUSTED
            next_attempt_at = None
            resolved_at = None
        else:
            next_state = IncidentState.WAITING
            base = 30.0 if recipe is None else recipe.base_backoff_seconds
            ceiling = 1_800.0 if recipe is None else recipe.max_backoff_seconds
            delay = min(ceiling, base * (2 ** max(0, attempts - 1)))
            next_attempt_at = _iso(now + timedelta(seconds=delay))
            resolved_at = None

        conn = self.store.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT state FROM supervisor_incidents WHERE incident_id = ?",
                (incident_id,),
            ).fetchone()
            if row is None or str(row["state"]) != IncidentState.RECOVERING.value:
                raise IncidentTransitionError("incident is not in a recoverable claimed state")
            conn.execute(
                """UPDATE supervisor_incidents SET state = ?, next_attempt_at = ?,
                   last_error = ?, evidence_json = ?, resolved_at = ?, updated_at = ?,
                   version = version + 1 WHERE incident_id = ?""",
                (
                    next_state.value,
                    next_attempt_at,
                    "" if outcome.success else outcome.note,
                    json.dumps(evidence, sort_keys=True, separators=(",", ":")),
                    resolved_at,
                    _iso(now),
                    incident_id,
                ),
            )
            self._append_event(
                conn,
                incident_id=incident_id,
                event_time=now,
                event_type="ATTEMPT_SUCCEEDED" if outcome.success else "ATTEMPT_FAILED",
                actor="coordinator",
                prior_state=IncidentState.RECOVERING.value,
                next_state=next_state.value,
                note=outcome.note,
                details={"invariants_proven": outcome.invariants_proven, **dict(outcome.evidence or {})},
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        result = self.get(incident_id)
        assert result is not None
        return result

    @staticmethod
    def _append_event(
        conn: sqlite3.Connection,
        *,
        incident_id: str,
        event_time: datetime,
        event_type: str,
        actor: str,
        prior_state: str,
        next_state: str,
        note: str,
        details: Mapping[str, Any],
    ) -> None:
        conn.execute(
            """INSERT INTO supervisor_incident_events
               (event_id, incident_id, event_time, event_type, actor, prior_state,
                next_state, note, details_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                uuid4().hex,
                incident_id,
                _iso(event_time),
                event_type,
                actor,
                prior_state,
                next_state,
                note,
                json.dumps(dict(details), sort_keys=True, separators=(",", ":")),
            ),
        )

    @staticmethod
    def _validate_observation(observation: IncidentObservation) -> None:
        for value, name in (
            (observation.incident_key, "incident_key"),
            (observation.category, "category"),
            (observation.owner, "owner"),
            (observation.recipe_id, "recipe_id"),
        ):
            if not value.strip():
                raise ValueError(f"{name} is required")
        if observation.max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if observation.scope_type is IncidentScope.GLOBAL:
            if observation.scope_value.strip():
                raise ValueError("global incidents must not carry a scope value")
        elif not observation.scope_value.strip():
            raise ValueError("non-global incidents require a scope value")

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        payload = dict(row)
        payload["requires_ack"] = bool(payload["requires_ack"])
        payload["evidence"] = _decode_json(payload.pop("evidence_json"))
        return payload
