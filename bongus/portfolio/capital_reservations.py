"""Durable central capital and repair/exit reservation book.

Reservations are conservative claims, not optimistic balance estimates.  An
entry may use only capital left after existing claims, hedge-repair capacity,
exit costs and the liquidation buffer.  Once an order might have reached the
exchange, its reservation cannot expire or be released until a terminal
exchange effect is proven.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any


class ReservationError(RuntimeError):
    pass


class ReservationPurpose(str, Enum):
    ENTRY = "ENTRY"
    ROTATION_ENTRY = "ROTATION_ENTRY"
    HEDGE_REPAIR = "HEDGE_REPAIR"
    EXIT = "EXIT"
    TREASURY = "TREASURY"


class ReservationState(str, Enum):
    RESERVED = "RESERVED"
    DISPATCHED = "DISPATCHED"
    UNKNOWN = "UNKNOWN"
    RELEASED = "RELEASED"

    @property
    def consumes_capital(self) -> bool:
        return self is not ReservationState.RELEASED


def _decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ReservationError(f"{name} must be numeric")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ReservationError(f"{name} must be a finite decimal") from exc
    if not result.is_finite():
        raise ReservationError(f"{name} must be a finite decimal")
    return result


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class CapitalState:
    equity_usd: Decimal | str | float
    spot_cash_available_usd: Decimal | str | float
    futures_margin_available_usd: Decimal | str | float
    current_pair_gross_usd: Decimal | str | float
    max_pair_gross_usd: Decimal | str | float
    current_initial_margin_usd: Decimal | str | float = Decimal("0")
    current_maintenance_margin_usd: Decimal | str | float = Decimal("0")
    # Request-symbol-specific value from an authoritative borrowability query.
    # Unknown availability is zero, never an optimistic account-equity proxy.
    spot_borrow_available_usd: Decimal | str | float = Decimal("0")


@dataclass(frozen=True, slots=True)
class ReservationPolicy:
    repair_reserve_usd: Decimal | str | float
    exit_reserve_usd: Decimal | str | float
    minimum_liquidation_buffer_usd: Decimal | str | float
    max_margin_utilization: Decimal | str | float = Decimal("0.80")


@dataclass(frozen=True, slots=True)
class ReservationRequest:
    reservation_id: str
    purpose: ReservationPurpose
    symbol: str
    cycle_id: str
    spot_cash_usd: Decimal | str | float
    futures_margin_usd: Decimal | str | float
    fees_usd: Decimal | str | float
    pair_gross_increment_usd: Decimal | str | float
    config_version: str
    expires_at: str | None = None
    metadata: dict[str, Any] | None = None
    spot_borrow_usd: Decimal | str | float = Decimal("0")

    def normalized(self) -> dict[str, Any]:
        if not self.reservation_id.strip() or not self.symbol.strip() or not self.config_version.strip():
            raise ReservationError("reservation_id, symbol and config_version are required")
        values = {
            "spot_cash_usd": _decimal(self.spot_cash_usd, "spot_cash_usd"),
            "spot_borrow_usd": _decimal(self.spot_borrow_usd, "spot_borrow_usd"),
            "futures_margin_usd": _decimal(self.futures_margin_usd, "futures_margin_usd"),
            "fees_usd": _decimal(self.fees_usd, "fees_usd"),
            "pair_gross_increment_usd": _decimal(self.pair_gross_increment_usd, "pair_gross_increment_usd"),
        }
        if any(value < 0 for value in values.values()):
            raise ReservationError("reservation requirements must be non-negative")
        if self.purpose in {ReservationPurpose.ENTRY, ReservationPurpose.ROTATION_ENTRY}:
            if not self.cycle_id.strip() or values["pair_gross_increment_usd"] <= 0:
                raise ReservationError("entry reservations require cycle_id and positive pair gross")
        payload: dict[str, Any] = {
            "reservation_id": self.reservation_id.strip(),
            "purpose": self.purpose.value,
            "symbol": self.symbol.strip().upper(),
            "cycle_id": self.cycle_id.strip(),
            **{key: str(value) for key, value in values.items()},
            "config_version": self.config_version.strip(),
            "expires_at": self.expires_at,
            "metadata": self.metadata or {},
        }
        return payload


@dataclass(frozen=True, slots=True)
class CapitalProjection:
    reserved_spot_cash_usd: Decimal
    reserved_spot_borrow_usd: Decimal
    reserved_futures_margin_usd: Decimal
    reserved_fees_usd: Decimal
    reserved_pair_gross_usd: Decimal
    entry_spot_cash_remaining_usd: Decimal
    entry_spot_borrow_remaining_usd: Decimal
    entry_futures_margin_remaining_usd: Decimal
    pair_gross_remaining_usd: Decimal
    liquidation_buffer_after_reservations_usd: Decimal


@dataclass(frozen=True, slots=True)
class ReservationDecision:
    allowed: bool
    duplicate: bool
    reservation_id: str
    reasons: tuple[str, ...]
    projection: CapitalProjection


_SCHEMA = """
CREATE TABLE IF NOT EXISTS capital_reservations (
    reservation_id TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    purpose TEXT NOT NULL,
    symbol TEXT NOT NULL,
    cycle_id TEXT NOT NULL,
    spot_cash_usd TEXT NOT NULL,
    spot_borrow_usd TEXT NOT NULL DEFAULT '0',
    futures_margin_usd TEXT NOT NULL,
    fees_usd TEXT NOT NULL,
    pair_gross_increment_usd TEXT NOT NULL,
    config_version TEXT NOT NULL,
    state TEXT NOT NULL,
    expires_at TEXT,
    exchange_terminal_proven INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    released_at TEXT,
    release_reason TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_capital_reservation_active
ON capital_reservations(state, purpose, symbol);

CREATE TABLE IF NOT EXISTS capital_reservation_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id TEXT NOT NULL,
    event_time TEXT NOT NULL,
    prior_state TEXT NOT NULL,
    next_state TEXT NOT NULL,
    reason TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    FOREIGN KEY(reservation_id) REFERENCES capital_reservations(reservation_id)
);
"""


class CapitalReservationBook:
    def __init__(
        self,
        db_path: str = "state.db",
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        self.db_path = db_path
        self._owns_connection = connection is None
        self.conn = connection or sqlite3.connect(
            db_path,
            timeout=30,
            check_same_thread=False,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript(_SCHEMA)
        columns = {
            str(row[1])
            for row in self.conn.execute("PRAGMA table_info(capital_reservations)")
        }
        if "spot_borrow_usd" not in columns:
            self.conn.execute(
                "ALTER TABLE capital_reservations "
                "ADD COLUMN spot_borrow_usd TEXT NOT NULL DEFAULT '0'"
            )
            # The request hash covers the complete normalized claim. Preserve
            # replay idempotency for reservations created before the borrow
            # dimension existed by deterministically rehashing them with zero.
            legacy_rows = self.conn.execute(
                """SELECT reservation_id, purpose, symbol, cycle_id,
                          spot_cash_usd, futures_margin_usd, fees_usd,
                          pair_gross_increment_usd, config_version, expires_at,
                          metadata_json
                   FROM capital_reservations"""
            ).fetchall()
            for row in legacy_rows:
                normalized = {
                    "reservation_id": str(row["reservation_id"]),
                    "purpose": str(row["purpose"]),
                    "symbol": str(row["symbol"]),
                    "cycle_id": str(row["cycle_id"]),
                    "spot_cash_usd": str(row["spot_cash_usd"]),
                    "spot_borrow_usd": "0",
                    "futures_margin_usd": str(row["futures_margin_usd"]),
                    "fees_usd": str(row["fees_usd"]),
                    "pair_gross_increment_usd": str(
                        row["pair_gross_increment_usd"]
                    ),
                    "config_version": str(row["config_version"]),
                    "expires_at": row["expires_at"],
                    "metadata": json.loads(str(row["metadata_json"])),
                }
                request_hash = hashlib.sha256(
                    json.dumps(
                        normalized, sort_keys=True, separators=(",", ":")
                    ).encode()
                ).hexdigest()
                self.conn.execute(
                    "UPDATE capital_reservations SET request_hash=? "
                    "WHERE reservation_id=?",
                    (request_hash, normalized["reservation_id"]),
                )
            self.conn.commit()

    def close(self) -> None:
        if self._owns_connection:
            self.conn.close()

    def reserve(
        self,
        request: ReservationRequest,
        *,
        capital: CapitalState,
        policy: ReservationPolicy,
        now: datetime | None = None,
    ) -> ReservationDecision:
        timestamp = now or datetime.now(timezone.utc)
        normalized = request.normalized()
        request_hash = hashlib.sha256(
            json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self._expire_safe_unsent(timestamp)
            existing = self.conn.execute(
                "SELECT request_hash, state FROM capital_reservations WHERE reservation_id = ?",
                (normalized["reservation_id"],),
            ).fetchone()
            if existing is not None:
                if str(existing["request_hash"]) != request_hash:
                    raise ReservationError("reservation_id content collision")
                projection = self._projection(
                    capital, policy, borrow_symbol=normalized["symbol"]
                )
                self.conn.commit()
                return ReservationDecision(
                    allowed=str(existing["state"]) != ReservationState.RELEASED.value,
                    duplicate=True,
                    reservation_id=normalized["reservation_id"],
                    reasons=(),
                    projection=projection,
                )

            projection = self._projection(
                capital, policy, borrow_symbol=normalized["symbol"]
            )
            reasons = self._admission_reasons(normalized, capital, policy, projection)
            if reasons:
                self.conn.rollback()
                return ReservationDecision(False, False, normalized["reservation_id"], tuple(reasons), projection)

            now_iso = timestamp.astimezone(timezone.utc).isoformat()
            self.conn.execute(
                """INSERT INTO capital_reservations
                   (reservation_id, request_hash, purpose, symbol, cycle_id,
                    spot_cash_usd, spot_borrow_usd, futures_margin_usd, fees_usd,
                    pair_gross_increment_usd, config_version, state, expires_at,
                    metadata_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    normalized["reservation_id"],
                    request_hash,
                    normalized["purpose"],
                    normalized["symbol"],
                    normalized["cycle_id"],
                    normalized["spot_cash_usd"],
                    normalized["spot_borrow_usd"],
                    normalized["futures_margin_usd"],
                    normalized["fees_usd"],
                    normalized["pair_gross_increment_usd"],
                    normalized["config_version"],
                    ReservationState.RESERVED.value,
                    normalized["expires_at"],
                    json.dumps(normalized["metadata"], sort_keys=True, separators=(",", ":")),
                    now_iso,
                    now_iso,
                ),
            )
            self._event(normalized["reservation_id"], "", ReservationState.RESERVED.value, "admitted", {})
            result_projection = self._projection(
                capital, policy, borrow_symbol=normalized["symbol"]
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return ReservationDecision(True, False, normalized["reservation_id"], (), result_projection)

    def mark_dispatched(self, reservation_id: str, *, evidence: dict[str, Any] | None = None) -> None:
        self._transition(
            reservation_id,
            allowed_from={ReservationState.RESERVED},
            next_state=ReservationState.DISPATCHED,
            reason="order_may_have_reached_exchange",
            evidence=evidence or {},
        )

    def mark_delivery_unknown(self, reservation_id: str, *, evidence: dict[str, Any] | None = None) -> None:
        self._transition(
            reservation_id,
            allowed_from={ReservationState.RESERVED, ReservationState.DISPATCHED, ReservationState.UNKNOWN},
            next_state=ReservationState.UNKNOWN,
            reason="exchange_effect_unknown",
            evidence=evidence or {},
        )

    def release(
        self,
        reservation_id: str,
        *,
        reason: str,
        exchange_terminal_proven: bool,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        row = self.conn.execute(
            "SELECT state FROM capital_reservations WHERE reservation_id = ?",
            (reservation_id,),
        ).fetchone()
        if row is None:
            raise ReservationError("unknown reservation")
        state = ReservationState(str(row["state"]))
        if state in {ReservationState.DISPATCHED, ReservationState.UNKNOWN} and not exchange_terminal_proven:
            raise ReservationError("exchange terminal proof is required before releasing dispatched capital")
        self._transition(
            reservation_id,
            allowed_from={ReservationState.RESERVED, ReservationState.DISPATCHED, ReservationState.UNKNOWN},
            next_state=ReservationState.RELEASED,
            reason=reason,
            evidence=evidence or {},
            exchange_terminal_proven=exchange_terminal_proven,
        )

    def active(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM capital_reservations WHERE state != 'RELEASED' ORDER BY created_at, reservation_id"
        ).fetchall()
        return [self._decode(row) for row in rows]

    def projection(self, capital: CapitalState, policy: ReservationPolicy) -> CapitalProjection:
        return self._projection(capital, policy)

    def _projection(
        self,
        capital: CapitalState,
        policy: ReservationPolicy,
        *,
        borrow_symbol: str | None = None,
    ) -> CapitalProjection:
        rows = self.conn.execute(
            """SELECT symbol, spot_cash_usd, spot_borrow_usd,
                      futures_margin_usd, fees_usd, pair_gross_increment_usd
               FROM capital_reservations WHERE state != 'RELEASED'"""
        ).fetchall()
        reserved_spot = sum((_decimal(row["spot_cash_usd"], "spot_cash_usd") for row in rows), Decimal("0"))
        reserved_borrow = sum(
            (
                _decimal(row["spot_borrow_usd"], "spot_borrow_usd")
                for row in rows
                if borrow_symbol is None
                or str(row["symbol"]).upper() == borrow_symbol.upper()
            ),
            Decimal("0"),
        )
        reserved_margin = sum(
            (_decimal(row["futures_margin_usd"], "futures_margin_usd") for row in rows), Decimal("0")
        )
        reserved_fees = sum((_decimal(row["fees_usd"], "fees_usd") for row in rows), Decimal("0"))
        reserved_gross = sum(
            (_decimal(row["pair_gross_increment_usd"], "pair_gross_increment_usd") for row in rows), Decimal("0")
        )
        spot_available = _decimal(capital.spot_cash_available_usd, "spot_cash_available_usd")
        borrow_available = _decimal(
            capital.spot_borrow_available_usd, "spot_borrow_available_usd"
        )
        futures_available = _decimal(capital.futures_margin_available_usd, "futures_margin_available_usd")
        repair = _decimal(policy.repair_reserve_usd, "repair_reserve_usd")
        exit_reserve = _decimal(policy.exit_reserve_usd, "exit_reserve_usd")
        liquidation = _decimal(policy.minimum_liquidation_buffer_usd, "minimum_liquidation_buffer_usd")
        current_gross = _decimal(capital.current_pair_gross_usd, "current_pair_gross_usd")
        gross_cap = _decimal(capital.max_pair_gross_usd, "max_pair_gross_usd")
        return CapitalProjection(
            reserved_spot_cash_usd=reserved_spot,
            reserved_spot_borrow_usd=reserved_borrow,
            reserved_futures_margin_usd=reserved_margin,
            reserved_fees_usd=reserved_fees,
            reserved_pair_gross_usd=reserved_gross,
            entry_spot_cash_remaining_usd=max(
                Decimal("0"), spot_available - reserved_spot - reserved_fees - repair - exit_reserve
            ),
            entry_spot_borrow_remaining_usd=max(
                Decimal("0"), borrow_available - reserved_borrow
            ),
            entry_futures_margin_remaining_usd=max(
                Decimal("0"), futures_available - reserved_margin - repair - exit_reserve - liquidation
            ),
            pair_gross_remaining_usd=max(Decimal("0"), gross_cap - current_gross - reserved_gross),
            liquidation_buffer_after_reservations_usd=futures_available - reserved_margin,
        )

    @staticmethod
    def _admission_reasons(
        request: dict[str, Any],
        capital: CapitalState,
        policy: ReservationPolicy,
        projection: CapitalProjection,
    ) -> list[str]:
        purpose = ReservationPurpose(request["purpose"])
        required_spot = _decimal(request["spot_cash_usd"], "spot_cash_usd") + _decimal(request["fees_usd"], "fees_usd")
        required_borrow = _decimal(request["spot_borrow_usd"], "spot_borrow_usd")
        required_margin = _decimal(request["futures_margin_usd"], "futures_margin_usd")
        required_gross = _decimal(request["pair_gross_increment_usd"], "pair_gross_increment_usd")
        entry_like = purpose in {ReservationPurpose.ENTRY, ReservationPurpose.ROTATION_ENTRY, ReservationPurpose.TREASURY}
        reasons: list[str] = []
        if entry_like:
            if required_spot > projection.entry_spot_cash_remaining_usd:
                reasons.append("spot_cash_after_repair_exit_reserves")
            if required_borrow > projection.entry_spot_borrow_remaining_usd:
                reasons.append("spot_borrow_availability")
            if required_margin > projection.entry_futures_margin_remaining_usd:
                reasons.append("futures_margin_after_repair_exit_liquidation_reserves")
            if required_gross > projection.pair_gross_remaining_usd:
                reasons.append("pair_gross_cap")
        else:
            # Risk-reducing repair and exit may consume their protected reserve,
            # but never spend capital that does not actually exist or the hard
            # liquidation buffer.
            raw_spot = _decimal(capital.spot_cash_available_usd, "spot_cash_available_usd")
            raw_borrow = _decimal(
                capital.spot_borrow_available_usd, "spot_borrow_available_usd"
            )
            raw_margin = _decimal(capital.futures_margin_available_usd, "futures_margin_available_usd")
            minimum_buffer = _decimal(policy.minimum_liquidation_buffer_usd, "minimum_liquidation_buffer_usd")
            if required_spot > raw_spot - projection.reserved_spot_cash_usd - projection.reserved_fees_usd:
                reasons.append("insufficient_raw_spot_cash_for_repair_or_exit")
            if required_borrow > raw_borrow - projection.reserved_spot_borrow_usd:
                reasons.append("insufficient_raw_spot_borrow_for_repair_or_exit")
            if required_margin > raw_margin - projection.reserved_futures_margin_usd - minimum_buffer:
                reasons.append("insufficient_raw_margin_for_repair_or_exit")

        equity = _decimal(capital.equity_usd, "equity_usd")
        max_utilization = _decimal(policy.max_margin_utilization, "max_margin_utilization")
        current_initial = _decimal(capital.current_initial_margin_usd, "current_initial_margin_usd")
        if entry_like and equity > 0 and current_initial + projection.reserved_futures_margin_usd + required_margin > equity * max_utilization:
            reasons.append("initial_margin_utilization_cap")
        return reasons

    def _expire_safe_unsent(self, now: datetime) -> None:
        now_iso = now.astimezone(timezone.utc).isoformat()
        rows = self.conn.execute(
            """SELECT reservation_id FROM capital_reservations
               WHERE state = 'RESERVED' AND expires_at IS NOT NULL AND expires_at <= ?""",
            (now_iso,),
        ).fetchall()
        for row in rows:
            reservation_id = str(row["reservation_id"])
            self.conn.execute(
                """UPDATE capital_reservations SET state = 'RELEASED', released_at = ?,
                   release_reason = 'expired_before_dispatch', updated_at = ? WHERE reservation_id = ?""",
                (now_iso, now_iso, reservation_id),
            )
            self._event(reservation_id, ReservationState.RESERVED.value, ReservationState.RELEASED.value, "expired_before_dispatch", {})

    def _transition(
        self,
        reservation_id: str,
        *,
        allowed_from: set[ReservationState],
        next_state: ReservationState,
        reason: str,
        evidence: dict[str, Any],
        exchange_terminal_proven: bool = False,
    ) -> None:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT state FROM capital_reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            if row is None:
                raise ReservationError("unknown reservation")
            prior = ReservationState(str(row["state"]))
            if prior is next_state:
                self.conn.commit()
                return
            if prior not in allowed_from:
                raise ReservationError(f"invalid reservation transition {prior.value} -> {next_state.value}")
            now_iso = _now_iso()
            self.conn.execute(
                """UPDATE capital_reservations SET state = ?, updated_at = ?,
                   exchange_terminal_proven = MAX(exchange_terminal_proven, ?),
                   released_at = CASE WHEN ? = 'RELEASED' THEN ? ELSE released_at END,
                   release_reason = CASE WHEN ? = 'RELEASED' THEN ? ELSE release_reason END
                   WHERE reservation_id = ?""",
                (
                    next_state.value,
                    now_iso,
                    int(exchange_terminal_proven),
                    next_state.value,
                    now_iso,
                    next_state.value,
                    reason,
                    reservation_id,
                ),
            )
            self._event(reservation_id, prior.value, next_state.value, reason, evidence)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _event(self, reservation_id: str, prior: str, next_state: str, reason: str, evidence: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT INTO capital_reservation_events
               (reservation_id, event_time, prior_state, next_state, reason, evidence_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (reservation_id, _now_iso(), prior, next_state, reason, json.dumps(evidence, sort_keys=True)),
        )

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        payload = dict(row)
        payload["exchange_terminal_proven"] = bool(payload["exchange_terminal_proven"])
        payload["metadata"] = json.loads(payload.pop("metadata_json"))
        return payload
