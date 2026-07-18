"""Reconciled daily economics and transition-only alert reporting."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import json
import sqlite3
from typing import Any

from bongus.engine.economic_ledger import EconomicLedgerProjection, project_economic_ledger


@dataclass(frozen=True, slots=True)
class ReconciledDailyReport:
    start_time: str
    end_time: str
    reconciled: bool
    event_count: int
    fill_count: int
    funding_usd: Decimal
    commissions_usd: Decimal
    borrow_interest_usd: Decimal
    net_economic_effect_usd: Decimal
    gross_fill_notional_usd: Decimal
    unvalued_event_count: int
    capital_utilization: float
    open_incidents: int
    critical_incidents: int
    blockers: tuple[str, ...]


def build_reconciled_daily_report(
    conn: sqlite3.Connection,
    *,
    start_time: datetime,
    end_time: datetime,
    reconciliation_matched: bool,
    reserved_capital_usd: Decimal | str | float = Decimal("0"),
    account_equity_usd: Decimal | str | float = Decimal("0"),
    open_incidents: int = 0,
    critical_incidents: int = 0,
    account_id: str | None = None,
    trading_mode: str | None = None,
    strategy_id: str | None = None,
) -> ReconciledDailyReport:
    if start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=timezone.utc)
    if end_time.tzinfo is None:
        end_time = end_time.replace(tzinfo=timezone.utc)
    if end_time <= start_time:
        raise ValueError("daily report end_time must be after start_time")
    # The economic-ledger reader returns named dictionaries.  StateWriter
    # already configures this, but standalone/reporting connections may not.
    conn.row_factory = sqlite3.Row
    projection: EconomicLedgerProjection = project_economic_ledger(
        conn,
        account_id=account_id,
        trading_mode=trading_mode,
        strategy_id=strategy_id,
        start_time=start_time.astimezone(timezone.utc).isoformat(),
        end_time=end_time.astimezone(timezone.utc).isoformat(),
    )
    values = projection.economic_effect_usd_by_type
    equity = Decimal(str(account_equity_usd))
    reserved = Decimal(str(reserved_capital_usd))
    utilization = float(reserved / equity) if equity > 0 else 0.0
    blockers: list[str] = []
    if not reconciliation_matched:
        blockers.append("economic_ledger_reconciliation_failed")
    if projection.unvalued_economic_event_count:
        blockers.append("unvalued_economic_events")
    if critical_incidents:
        blockers.append("critical_incidents_open")
    return ReconciledDailyReport(
        start_time=start_time.astimezone(timezone.utc).isoformat(),
        end_time=end_time.astimezone(timezone.utc).isoformat(),
        reconciled=reconciliation_matched,
        event_count=projection.event_count,
        fill_count=projection.fill_count,
        funding_usd=values.get("FUNDING", Decimal("0")),
        commissions_usd=values.get("COMMISSION", Decimal("0")),
        borrow_interest_usd=values.get("BORROW_INTEREST", Decimal("0")),
        net_economic_effect_usd=projection.total_economic_effect_usd,
        gross_fill_notional_usd=projection.gross_fill_notional_usd,
        unvalued_event_count=projection.unvalued_economic_event_count,
        capital_utilization=utilization,
        open_incidents=max(0, int(open_incidents)),
        critical_incidents=max(0, int(critical_incidents)),
        blockers=tuple(blockers),
    )


class AlertTransitionTracker:
    """Persist alert state and emit only OPENED/UPDATED/RESOLVED changes."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS alert_states (
                alert_key TEXT PRIMARY KEY,
                active INTEGER NOT NULL,
                severity INTEGER NOT NULL,
                evidence_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        self.conn.commit()

    def observe(
        self,
        *,
        alert_key: str,
        active: bool,
        severity: int,
        evidence: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> str | None:
        if not alert_key.strip():
            raise ValueError("alert_key is required")
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        severity = max(0, int(severity))
        row = self.conn.execute(
            "SELECT active, severity, evidence_json FROM alert_states WHERE alert_key = ?",
            (alert_key,),
        ).fetchone()
        payload = json.dumps(evidence or {}, sort_keys=True, separators=(",", ":"))
        transition: str | None = None
        if row is None:
            transition = "OPENED" if active else None
        else:
            prior_active = bool(row[0])
            prior_severity = int(row[1])
            prior_payload = str(row[2])
            if active and not prior_active:
                transition = "OPENED"
            elif not active and prior_active:
                transition = "RESOLVED"
            elif active and (severity > prior_severity or payload != prior_payload):
                transition = "UPDATED"
        self.conn.execute(
            """INSERT INTO alert_states(alert_key, active, severity, evidence_json, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(alert_key) DO UPDATE SET active=excluded.active,
               severity=excluded.severity, evidence_json=excluded.evidence_json,
               updated_at=excluded.updated_at""",
            (alert_key, int(active), severity, payload, now.isoformat()),
        )
        self.conn.commit()
        return transition
