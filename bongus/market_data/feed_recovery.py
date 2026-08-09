"""Durable feed/API recovery primitives.

The module keeps recovery scoped to one venue/stream/symbol.  It never marks a
gapped stream ready until a contiguous bounded backfill proves the cursor, and
it centralizes retry-after/ban/clock/maintenance handling to prevent retry
storms.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import hashlib
import json
import math
import sqlite3
import threading
from _thread import RLock as RLockType
from typing import Any, Iterable, Mapping


UTC = timezone.utc


def _utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class FeedState(StrEnum):
    COLD = "COLD"
    READY = "READY"
    GAPPED = "GAPPED"
    BACKFILLING = "BACKFILLING"
    THROTTLED = "THROTTLED"
    MAINTENANCE = "MAINTENANCE"
    CLOCK_SKEW = "CLOCK_SKEW"
    FILTER_CHANGED = "FILTER_CHANGED"


@dataclass(frozen=True, slots=True)
class FeedSource:
    venue: str
    stream: str
    symbol: str = ""

    @property
    def key(self) -> str:
        values = [self.venue.strip().lower(), self.stream.strip().lower(), self.symbol.strip().upper()]
        if not values[0] or not values[1]:
            raise ValueError("feed source requires venue and stream")
        return ":".join(values)


@dataclass(frozen=True, slots=True)
class SequenceResult:
    source_key: str
    accepted: bool
    duplicate: bool
    state: FeedState
    prior_sequence: int | None
    sequence: int
    missing_from: int | None = None
    missing_to: int | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ApiCondition:
    state: FeedState
    retry_at: datetime | None
    reason_code: str
    symbol_scoped: bool
    evidence: dict[str, Any]


_CURSOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS feed_cursors (
    source_key TEXT PRIMARY KEY,
    venue TEXT NOT NULL,
    stream TEXT NOT NULL,
    symbol TEXT NOT NULL,
    last_sequence INTEGER,
    state TEXT NOT NULL,
    gap_from INTEGER,
    gap_to INTEGER,
    last_event_time TEXT,
    metadata_hash TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
"""

_EVENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS feed_recovery_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key TEXT NOT NULL,
    event_time TEXT NOT NULL,
    event_type TEXT NOT NULL,
    prior_sequence INTEGER,
    next_sequence INTEGER,
    details_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_feed_recovery_events_source_time
ON feed_recovery_events(source_key, event_time, event_id);
"""

_SCHEMA = _CURSOR_SCHEMA + _EVENT_SCHEMA


class FeedCursorStore:
    """SQLite-backed sequence checkpoint and bounded backfill verifier.

    ``ingest`` is intentionally limited to streams whose sequence is a scalar
    that increments by exactly one.  Ranged depth protocols (including Binance
    ``U``/``u``/``pu`` updates) must use :meth:`record_gap` and
    :meth:`record_readiness_proof`; those methods preserve the exchange's range
    evidence without inventing missing ``+1`` events.
    """

    def __init__(
        self,
        db_path: str = "state.db",
        *,
        max_backfill_events: int = 10_000,
        connection: sqlite3.Connection | None = None,
        event_connection: sqlite3.Connection | None = None,
        lock: RLockType | None = None,
    ) -> None:
        if max_backfill_events <= 0:
            raise ValueError("max_backfill_events must be positive")
        self._owns_connection = connection is None
        self.conn = connection or sqlite3.connect(
            db_path,
            timeout=30,
            check_same_thread=False,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self._event_conn = event_connection or self.conn
        self._split_event_connection = self._event_conn is not self.conn
        self.conn.executescript(_CURSOR_SCHEMA)
        self._event_conn.executescript(_EVENT_SCHEMA)
        self.max_backfill_events = int(max_backfill_events)
        self._lock = lock or threading.RLock()

    def close(self) -> None:
        if self._owns_connection:
            self.conn.close()

    def _row(self, source: FeedSource) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM feed_cursors WHERE source_key = ?", (source.key,)
        ).fetchone()

    def _event(
        self,
        source_key: str,
        event_type: str,
        prior_sequence: int | None,
        next_sequence: int | None,
        details: Mapping[str, Any],
        now: datetime,
    ) -> None:
        self._event_conn.execute(
            """INSERT INTO feed_recovery_events
               (source_key, event_time, event_type, prior_sequence, next_sequence, details_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                source_key,
                now.isoformat(),
                event_type,
                prior_sequence,
                next_sequence,
                json.dumps(dict(details), sort_keys=True, separators=(",", ":")),
            ),
        )
        if self._split_event_connection:
            # Immutable recovery evidence precedes the mutable cursor update.
            # A retry can repair a lagging cursor, while the reverse ordering
            # could permanently skip undocumented feed data.
            self._event_conn.commit()

    def ingest(
        self,
        source: FeedSource,
        sequence: int,
        *,
        event_time: datetime | None = None,
        now: datetime | None = None,
    ) -> SequenceResult:
        now = _utc(now)
        event_time = _utc(event_time or now)
        if isinstance(sequence, bool) or int(sequence) < 0:
            raise ValueError("sequence must be a non-negative integer")
        sequence = int(sequence)
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._row(source)
                prior = int(row["last_sequence"]) if row is not None and row["last_sequence"] is not None else None
                if row is None:
                    self.conn.execute(
                        """INSERT INTO feed_cursors
                           (source_key, venue, stream, symbol, last_sequence, state,
                            last_event_time, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            source.key,
                            source.venue.lower(),
                            source.stream.lower(),
                            source.symbol.upper(),
                            sequence,
                            FeedState.READY.value,
                            event_time.isoformat(),
                            now.isoformat(),
                        ),
                    )
                    self._event(source.key, "CURSOR_INITIALIZED", None, sequence, {}, now)
                    result = SequenceResult(source.key, True, False, FeedState.READY, None, sequence)
                elif prior is not None and sequence <= prior:
                    self._event(
                        source.key,
                        "DUPLICATE_OR_STALE",
                        prior,
                        sequence,
                        {"cursor_unchanged": True},
                        now,
                    )
                    result = SequenceResult(
                        source.key,
                        False,
                        True,
                        FeedState(str(row["state"])),
                        prior,
                        sequence,
                        reason="duplicate_or_stale_sequence",
                    )
                elif prior is not None and sequence == prior + 1 and str(row["state"]) != FeedState.GAPPED.value:
                    self.conn.execute(
                        """UPDATE feed_cursors SET last_sequence = ?, state = ?,
                           last_event_time = ?, updated_at = ? WHERE source_key = ?""",
                        (sequence, FeedState.READY.value, event_time.isoformat(), now.isoformat(), source.key),
                    )
                    result = SequenceResult(source.key, True, False, FeedState.READY, prior, sequence)
                else:
                    gap_from = (prior + 1) if prior is not None else sequence
                    gap_to = sequence - 1
                    self.conn.execute(
                        """UPDATE feed_cursors SET state = ?, gap_from = ?, gap_to = ?,
                           updated_at = ? WHERE source_key = ?""",
                        (FeedState.GAPPED.value, gap_from, gap_to, now.isoformat(), source.key),
                    )
                    self._event(
                        source.key,
                        "GAP_DETECTED",
                        prior,
                        sequence,
                        {"missing_from": gap_from, "missing_to": gap_to},
                        now,
                    )
                    result = SequenceResult(
                        source.key,
                        False,
                        False,
                        FeedState.GAPPED,
                        prior,
                        sequence,
                        gap_from,
                        gap_to,
                        "contiguous_backfill_required",
                    )
                self.conn.commit()
                return result
            except Exception:
                self.conn.rollback()
                raise

    @staticmethod
    def _optional_sequence(value: int | None, field: str) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or int(value) < 0:
            raise ValueError(f"{field} must be a non-negative integer")
        return int(value)

    def _record_gap_locked(
        self,
        source: FeedSource,
        *,
        prior_sequence: int | None,
        first_sequence: int | None,
        final_sequence: int | None,
        previous_final_sequence: int | None,
        reason: str,
        now: datetime,
    ) -> SequenceResult:
        row = self._row(source)
        stored_prior = (
            int(row["last_sequence"])
            if row is not None and row["last_sequence"] is not None
            else None
        )
        effective_prior = prior_sequence if prior_sequence is not None else stored_prior
        self.conn.execute(
            """
            INSERT INTO feed_cursors
                (source_key, venue, stream, symbol, last_sequence, state,
                 gap_from, gap_to, last_event_time, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
            ON CONFLICT(source_key) DO UPDATE SET
                last_sequence=COALESCE(excluded.last_sequence, feed_cursors.last_sequence),
                state=excluded.state,
                gap_from=NULL,
                gap_to=NULL,
                last_event_time=excluded.last_event_time,
                updated_at=excluded.updated_at
            """,
            (
                source.key,
                source.venue.lower(),
                source.stream.lower(),
                source.symbol.upper(),
                effective_prior,
                FeedState.GAPPED.value,
                now.isoformat(),
                now.isoformat(),
            ),
        )
        self._event(
            source.key,
            "RANGED_GAP_RECORDED",
            effective_prior,
            final_sequence,
            {
                "first_sequence": first_sequence,
                "final_sequence": final_sequence,
                "previous_final_sequence": previous_final_sequence,
                "reason": str(reason),
                "sequence_model": "ranged",
                "readiness_proof_required": True,
            },
            now,
        )
        return SequenceResult(
            source.key,
            False,
            False,
            FeedState.GAPPED,
            effective_prior,
            final_sequence if final_sequence is not None else (effective_prior or 0),
            reason="fresh_snapshot_or_contiguous_range_proof_required",
        )

    def record_gap(
        self,
        source: FeedSource,
        *,
        prior_sequence: int | None = None,
        first_sequence: int | None = None,
        final_sequence: int | None = None,
        previous_final_sequence: int | None = None,
        reason: str = "sequence_gap",
        now: datetime | None = None,
    ) -> SequenceResult:
        """Persist a ranged-stream gap without assuming scalar ``+1`` IDs."""

        now = _utc(now)
        prior_sequence = self._optional_sequence(prior_sequence, "prior_sequence")
        first_sequence = self._optional_sequence(first_sequence, "first_sequence")
        final_sequence = self._optional_sequence(final_sequence, "final_sequence")
        previous_final_sequence = self._optional_sequence(
            previous_final_sequence,
            "previous_final_sequence",
        )
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                result = self._record_gap_locked(
                    source,
                    prior_sequence=prior_sequence,
                    first_sequence=first_sequence,
                    final_sequence=final_sequence,
                    previous_final_sequence=previous_final_sequence,
                    reason=reason,
                    now=now,
                )
                self.conn.commit()
                return result
            except Exception:
                self.conn.rollback()
                raise

    def record_gap_batch(
        self,
        gaps: Iterable[tuple[FeedSource, Mapping[str, Any]]],
        *,
        now: datetime | None = None,
    ) -> list[SequenceResult]:
        """Atomically invalidate related ranged streams (for example both legs)."""

        timestamp = _utc(now)
        normalized: list[tuple[FeedSource, dict[str, Any]]] = []
        for source, evidence in gaps:
            normalized.append(
                (
                    source,
                    {
                        "prior_sequence": self._optional_sequence(
                            evidence.get("prior_sequence"), "prior_sequence"
                        ),
                        "first_sequence": self._optional_sequence(
                            evidence.get("first_sequence"), "first_sequence"
                        ),
                        "final_sequence": self._optional_sequence(
                            evidence.get("final_sequence"), "final_sequence"
                        ),
                        "previous_final_sequence": self._optional_sequence(
                            evidence.get("previous_final_sequence"),
                            "previous_final_sequence",
                        ),
                        "reason": str(evidence.get("reason") or "sequence_gap"),
                    },
                )
            )
        if not normalized:
            raise ValueError("at least one ranged gap is required")
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                results = [
                    self._record_gap_locked(source, now=timestamp, **evidence)
                    for source, evidence in normalized
                ]
                self.conn.commit()
                return results
            except Exception:
                self.conn.rollback()
                raise

    def retire_source(
        self,
        source: FeedSource,
        *,
        reason: str = "source_no_longer_tradable",
        now: datetime | None = None,
    ) -> bool:
        """Retire stale recovery state for a source that cannot be subscribed.

        This is not a readiness proof. It removes an obsolete gap only after an
        authoritative symbol universe says the source is no longer tradable.
        A later relisting starts from COLD and must establish fresh market data.
        """

        timestamp = _utc(now)
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._row(source)
                if row is None:
                    self.conn.rollback()
                    return False
                already_retired = (
                    str(row["state"]) == FeedState.COLD.value
                    and row["last_sequence"] is None
                )
                self.conn.execute(
                    """UPDATE feed_cursors SET last_sequence = NULL, state = ?,
                       gap_from = NULL, gap_to = NULL, last_event_time = NULL,
                       updated_at = ? WHERE source_key = ?""",
                    (FeedState.COLD.value, timestamp.isoformat(), source.key),
                )
                if not already_retired:
                    self._event(
                        source.key,
                        "SOURCE_RETIRED",
                        row["last_sequence"],
                        None,
                        {"reason": str(reason), "readiness_granted": False},
                        timestamp,
                    )
                self.conn.commit()
                return not already_retired
            except Exception:
                self.conn.rollback()
                raise

    def record_readiness_proof(
        self,
        source: FeedSource,
        *,
        final_sequence: int,
        first_sequence: int | None = None,
        previous_final_sequence: int | None = None,
        is_snapshot: bool = False,
        contiguous: bool = False,
        now: datetime | None = None,
    ) -> SequenceResult:
        """Mark a ranged stream ready only with explicit fresh evidence."""

        now = _utc(now)
        final_sequence_value = self._optional_sequence(final_sequence, "final_sequence")
        assert final_sequence_value is not None
        first_sequence = self._optional_sequence(first_sequence, "first_sequence")
        previous_final_sequence = self._optional_sequence(
            previous_final_sequence,
            "previous_final_sequence",
        )
        proof_kind = "snapshot" if is_snapshot else "contiguous_range"
        proof_valid = bool(is_snapshot or contiguous)
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._row(source)
                prior = (
                    int(row["last_sequence"])
                    if row is not None and row["last_sequence"] is not None
                    else None
                )
                prior_state = (
                    FeedState(str(row["state"])) if row is not None else FeedState.COLD
                )
                stale = (
                    prior is not None
                    and final_sequence_value <= prior
                    and not is_snapshot
                )
                if not proof_valid or stale:
                    if row is None:
                        self.conn.execute(
                            """
                            INSERT INTO feed_cursors
                                (source_key, venue, stream, symbol, state, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                source.key,
                                source.venue.lower(),
                                source.stream.lower(),
                                source.symbol.upper(),
                                FeedState.COLD.value,
                                now.isoformat(),
                            ),
                        )
                    self._event(
                        source.key,
                        "READINESS_PROOF_REJECTED",
                        prior,
                        final_sequence_value,
                        {
                            "is_snapshot": bool(is_snapshot),
                            "contiguous": bool(contiguous),
                            "stale": stale,
                        },
                        now,
                    )
                    self.conn.commit()
                    return SequenceResult(
                        source.key,
                        False,
                        stale,
                        prior_state,
                        prior,
                        final_sequence_value,
                        reason=(
                            "stale_readiness_proof"
                            if stale
                            else "snapshot_or_contiguous_range_proof_required"
                        ),
                    )

                self.conn.execute(
                    """
                    INSERT INTO feed_cursors
                        (source_key, venue, stream, symbol, last_sequence, state,
                         gap_from, gap_to, last_event_time, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                    ON CONFLICT(source_key) DO UPDATE SET
                        last_sequence=excluded.last_sequence,
                        state=excluded.state,
                        gap_from=NULL,
                        gap_to=NULL,
                        last_event_time=excluded.last_event_time,
                        updated_at=excluded.updated_at
                    """,
                    (
                        source.key,
                        source.venue.lower(),
                        source.stream.lower(),
                        source.symbol.upper(),
                        final_sequence_value,
                        FeedState.READY.value,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                self._event(
                    source.key,
                    "RANGED_READINESS_PROVEN",
                    prior,
                    final_sequence_value,
                    {
                        "proof_kind": proof_kind,
                        "first_sequence": first_sequence,
                        "previous_final_sequence": previous_final_sequence,
                        "prior_state": prior_state.value,
                    },
                    now,
                )
                self.conn.commit()
                return SequenceResult(
                    source.key,
                    True,
                    False,
                    FeedState.READY,
                    prior,
                    final_sequence_value,
                    reason=f"fresh_{proof_kind}_proven",
                )
            except Exception:
                self.conn.rollback()
                raise

    def apply_backfill(
        self,
        source: FeedSource,
        sequences: Iterable[int],
        *,
        now: datetime | None = None,
    ) -> SequenceResult:
        now = _utc(now)
        values = [int(value) for value in sequences]
        if len(values) > self.max_backfill_events:
            raise ValueError("backfill exceeds configured safety bound")
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._row(source)
                if row is None or row["last_sequence"] is None:
                    raise ValueError("cannot backfill an uninitialized source")
                prior = int(row["last_sequence"])
                gap_from = row["gap_from"]
                gap_to = row["gap_to"]
                if str(row["state"]) != FeedState.GAPPED.value or gap_from is None or gap_to is None:
                    raise ValueError("source does not have a pending gap")
                expected = list(range(int(gap_from), int(gap_to) + 1))
                if values != expected:
                    self._event(
                        source.key,
                        "BACKFILL_REJECTED",
                        prior,
                        values[-1] if values else None,
                        {"expected": expected, "received": values},
                        now,
                    )
                    self.conn.commit()
                    return SequenceResult(
                        source.key,
                        False,
                        False,
                        FeedState.GAPPED,
                        prior,
                        values[-1] if values else prior,
                        int(gap_from),
                        int(gap_to),
                        "backfill_not_contiguous_or_complete",
                    )
                final_sequence = int(gap_to)
                self.conn.execute(
                    """UPDATE feed_cursors SET last_sequence = ?, state = ?, gap_from = NULL,
                       gap_to = NULL, updated_at = ? WHERE source_key = ?""",
                    (final_sequence, FeedState.READY.value, now.isoformat(), source.key),
                )
                self._event(
                    source.key,
                    "BACKFILL_APPLIED",
                    prior,
                    final_sequence,
                    {"event_count": len(values)},
                    now,
                )
                self.conn.commit()
                return SequenceResult(
                    source.key,
                    True,
                    False,
                    FeedState.READY,
                    prior,
                    final_sequence,
                    reason="gap_replayed_and_cursor_proven",
                )
            except Exception:
                self.conn.rollback()
                raise

    def classify_metadata(
        self,
        source: FeedSource,
        metadata: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> ApiCondition | None:
        now = _utc(now)
        digest = hashlib.sha256(
            json.dumps(dict(metadata), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with self._lock:
            row = self._row(source)
            prior_hash = str(row["metadata_hash"] or "") if row is not None else ""
            if row is None:
                self.conn.execute(
                    """INSERT INTO feed_cursors
                       (source_key, venue, stream, symbol, state, metadata_hash, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        source.key,
                        source.venue.lower(),
                        source.stream.lower(),
                        source.symbol.upper(),
                        FeedState.COLD.value,
                        digest,
                        now.isoformat(),
                    ),
                )
                self.conn.commit()
                return None
            if digest == prior_hash:
                return None
            self.conn.execute(
                "UPDATE feed_cursors SET metadata_hash = ?, state = ?, updated_at = ? WHERE source_key = ?",
                (digest, FeedState.FILTER_CHANGED.value, now.isoformat(), source.key),
            )
            self._event(
                source.key,
                "FILTER_METADATA_CHANGED",
                row["last_sequence"],
                row["last_sequence"],
                {"prior_hash": prior_hash, "next_hash": digest},
                now,
            )
            self.conn.commit()
        return ApiCondition(
            FeedState.FILTER_CHANGED,
            None,
            "exchange_filter_metadata_changed",
            bool(source.symbol),
            {"prior_hash": prior_hash, "next_hash": digest},
        )

    def snapshot(self, source: FeedSource | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if source is None:
                rows = self.conn.execute(
                    "SELECT * FROM feed_cursors ORDER BY source_key"
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM feed_cursors WHERE source_key = ?", (source.key,)
                ).fetchall()
            return [dict(row) for row in rows]


class RateLimitBudget:
    """Deterministic token budget with explicit server backoff."""

    def __init__(self, *, capacity: float, refill_per_second: float) -> None:
        if capacity <= 0.0 or refill_per_second <= 0.0:
            raise ValueError("rate-limit capacity and refill rate must be positive")
        self.capacity = float(capacity)
        self.refill_per_second = float(refill_per_second)
        self.tokens = float(capacity)
        self.last_refill: datetime | None = None
        self.blocked_until: datetime | None = None

    def _refill(self, now: datetime) -> None:
        if self.last_refill is None:
            self.last_refill = now
            return
        elapsed = max(0.0, (now - self.last_refill).total_seconds())
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_second)
        self.last_refill = now

    def acquire(self, *, weight: float = 1.0, now: datetime | None = None) -> bool:
        now = _utc(now)
        if not math.isfinite(weight) or weight <= 0.0:
            raise ValueError("request weight must be positive and finite")
        self._refill(now)
        if self.blocked_until is not None and now < self.blocked_until:
            return False
        if self.tokens + 1e-12 < weight:
            return False
        self.tokens -= weight
        return True

    def impose_retry_after(
        self,
        seconds: float,
        *,
        now: datetime | None = None,
    ) -> datetime:
        now = _utc(now)
        if not math.isfinite(seconds) or seconds < 0.0:
            raise ValueError("retry-after must be finite and non-negative")
        candidate = now + timedelta(seconds=seconds)
        if self.blocked_until is None or candidate > self.blocked_until:
            self.blocked_until = candidate
        return self.blocked_until


class ExchangeConditionClassifier:
    @staticmethod
    def classify(
        *,
        status_code: int,
        exchange_code: int | None = None,
        message: str = "",
        retry_after_seconds: float | None = None,
        symbol: str = "",
        now: datetime | None = None,
    ) -> ApiCondition:
        now = _utc(now)
        lowered = message.lower()
        retry_at: datetime | None = None
        if retry_after_seconds is not None:
            retry_at = now + timedelta(seconds=max(0.0, float(retry_after_seconds)))
        if status_code in {418, 429}:
            return ApiCondition(
                FeedState.THROTTLED,
                retry_at or now + timedelta(seconds=60 if status_code == 429 else 300),
                "rate_limited" if status_code == 429 else "ip_banned",
                False,
                {"status_code": status_code, "exchange_code": exchange_code},
            )
        if 500 <= status_code <= 599 or "maintenance" in lowered or "service unavailable" in lowered:
            maintenance_named = "maintenance" in lowered or "service unavailable" in lowered
            return ApiCondition(
                FeedState.MAINTENANCE,
                retry_at or now + timedelta(seconds=30),
                "exchange_maintenance" if maintenance_named else "exchange_server_error",
                False,
                {"status_code": status_code, "message": message[:200]},
            )
        if exchange_code == -1021 or "timestamp" in lowered or "clock" in lowered:
            return ApiCondition(
                FeedState.CLOCK_SKEW,
                now,
                "exchange_clock_skew",
                False,
                {"status_code": status_code, "exchange_code": exchange_code},
            )
        if exchange_code in {-1013, -1111} or "lot_size" in lowered or "tick_size" in lowered:
            return ApiCondition(
                FeedState.FILTER_CHANGED,
                None,
                "exchange_filter_rejection",
                bool(symbol),
                {"symbol": symbol.upper(), "exchange_code": exchange_code, "message": message[:200]},
            )
        return ApiCondition(
            FeedState.READY if 200 <= status_code < 300 else FeedState.THROTTLED,
            retry_at,
            "ok" if 200 <= status_code < 300 else "transient_api_error",
            bool(symbol),
            {"status_code": status_code, "exchange_code": exchange_code},
        )


@dataclass(frozen=True, slots=True)
class ClockSyncResult:
    safe: bool
    offset_ms: float
    round_trip_ms: float
    reason: str


def evaluate_exchange_clock(
    *,
    local_send_ms: float,
    local_receive_ms: float,
    exchange_time_ms: float,
    max_absolute_offset_ms: float = 500.0,
    max_round_trip_ms: float = 2_000.0,
) -> ClockSyncResult:
    values = (local_send_ms, local_receive_ms, exchange_time_ms)
    if any(not math.isfinite(float(value)) for value in values) or local_receive_ms < local_send_ms:
        return ClockSyncResult(False, math.inf, math.inf, "invalid_clock_sample")
    round_trip = local_receive_ms - local_send_ms
    midpoint = (local_send_ms + local_receive_ms) / 2.0
    offset = exchange_time_ms - midpoint
    if round_trip > max_round_trip_ms:
        return ClockSyncResult(False, offset, round_trip, "clock_sample_latency_too_high")
    if abs(offset) > max_absolute_offset_ms:
        return ClockSyncResult(False, offset, round_trip, "clock_offset_exceeds_limit")
    return ClockSyncResult(True, offset, round_trip, "clock_synchronized")
