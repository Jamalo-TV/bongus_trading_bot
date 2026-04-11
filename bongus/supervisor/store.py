from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

from bongus.supervisor.models import RecommendationStatus, SupervisorRecommendation

SUPERVISOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS supervisor_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    scheduled_for TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    UNIQUE(kind, scheduled_for)
);

CREATE TABLE IF NOT EXISTS supervisor_recommendations (
    recommendation_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    report_kind TEXT NOT NULL,
    kind TEXT NOT NULL,
    target_key TEXT NOT NULL,
    current_value TEXT NOT NULL,
    proposed_value TEXT NOT NULL,
    rationale TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS supervisor_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_key TEXT NOT NULL,
    severity TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    message TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS supervisor_runtime (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SupervisorStore:
    def __init__(self, db_path: str = "state.db") -> None:
        self.db_path = db_path
        self.conn = _connect(db_path)
        self.conn.executescript(SUPERVISOR_SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def has_report_for_slot(self, kind: str, scheduled_for: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM supervisor_reports WHERE kind = ? AND scheduled_for = ?",
            (kind, scheduled_for),
        ).fetchone()
        return row is not None

    def record_report(self, kind: str, scheduled_for: str, title: str, message: str, payload: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT OR IGNORE INTO supervisor_reports
               (kind, scheduled_for, sent_at, title, message, payload_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (kind, scheduled_for, _now(), title, message, json.dumps(payload, sort_keys=True)),
        )
        self.conn.commit()

    def save_recommendations(self, recommendations: Iterable[SupervisorRecommendation]) -> None:
        now = _now()
        rows = [
            (
                rec.recommendation_id,
                rec.created_at,
                now,
                rec.report_kind,
                rec.kind,
                rec.target_key,
                json.dumps(rec.current_value),
                json.dumps(rec.proposed_value),
                rec.rationale,
                rec.expires_at,
                rec.status,
                json.dumps(rec.metadata, sort_keys=True),
            )
            for rec in recommendations
        ]
        if not rows:
            return
        self.conn.executemany(
            """INSERT OR REPLACE INTO supervisor_recommendations
               (recommendation_id, created_at, updated_at, report_kind, kind, target_key,
                current_value, proposed_value, rationale, expires_at, status, metadata_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        self.conn.commit()

    def get_recommendation(self, recommendation_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM supervisor_recommendations WHERE recommendation_id = ?",
            (recommendation_id,),
        ).fetchone()
        return self._decode_recommendation(row) if row else None

    def list_recommendations(
        self,
        statuses: Iterable[str] | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM supervisor_recommendations"
        params: list[Any] = []
        if statuses:
            status_list = list(statuses)
            placeholders = ", ".join("?" for _ in status_list)
            query += f" WHERE status IN ({placeholders})"
            params.extend(status_list)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, tuple(params)).fetchall()
        return [self._decode_recommendation(row) for row in rows]

    def update_recommendation_status(self, recommendation_id: str, status: str) -> bool:
        result = self.conn.execute(
            """UPDATE supervisor_recommendations
               SET status = ?, updated_at = ?
               WHERE recommendation_id = ?""",
            (status, _now(), recommendation_id),
        )
        self.conn.commit()
        return result.rowcount > 0

    def purge_expired_recommendations(self, now_iso: str) -> None:
        self.conn.execute(
            """UPDATE supervisor_recommendations
               SET status = ?, updated_at = ?
               WHERE status = ? AND expires_at < ?""",
            (RecommendationStatus.EXPIRED.value, _now(), RecommendationStatus.PENDING.value, now_iso),
        )
        self.conn.commit()

    def should_emit_alert(self, alert_key: str, cooldown_seconds: int, now: datetime | None = None) -> bool:
        row = self.conn.execute(
            "SELECT sent_at FROM supervisor_alerts WHERE alert_key = ? ORDER BY id DESC LIMIT 1",
            (alert_key,),
        ).fetchone()
        if row is None:
            return True

        sent_at = datetime.fromisoformat(row["sent_at"])
        current = now or datetime.now(timezone.utc)
        return (current - sent_at).total_seconds() >= cooldown_seconds

    def record_alert(self, alert_key: str, severity: str, message: str, payload: dict[str, Any] | None = None) -> None:
        self.conn.execute(
            """INSERT INTO supervisor_alerts (alert_key, severity, sent_at, message, payload_json)
               VALUES (?, ?, ?, ?, ?)""",
            (alert_key, severity, _now(), message, json.dumps(payload or {}, sort_keys=True)),
        )
        self.conn.commit()

    def get_runtime_value(self, key: str, default: str = "") -> str:
        row = self.conn.execute(
            "SELECT value FROM supervisor_runtime WHERE key = ?",
            (key,),
        ).fetchone()
        return str(row["value"]) if row else default

    def set_runtime_value(self, key: str, value: str) -> None:
        self.conn.execute(
            """INSERT INTO supervisor_runtime (key, value, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (key, value, _now()),
        )
        self.conn.commit()

    def get_state_table_timestamps(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for table in ("positions", "portfolio_stats", "risk_state"):
            row = self.conn.execute(
                f"SELECT MAX(updated_at) AS updated_at FROM {table}"
            ).fetchone()
            if row and row["updated_at"]:
                result[table] = str(row["updated_at"])
        return result

    @staticmethod
    def _decode_recommendation(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "recommendation_id": row["recommendation_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "report_kind": row["report_kind"],
            "kind": row["kind"],
            "target_key": row["target_key"],
            "current_value": json.loads(row["current_value"]),
            "proposed_value": json.loads(row["proposed_value"]),
            "rationale": row["rationale"],
            "expires_at": row["expires_at"],
            "status": row["status"],
            "metadata": json.loads(row["metadata_json"]),
        }
