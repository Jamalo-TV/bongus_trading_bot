"""Reconciled daily economics and transition-only alert reporting."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from bongus.engine.economic_ledger import (
    BORROW_INTEREST,
    COMMISSION,
    DEPOSIT,
    FUNDING,
    INTERNAL_TRANSFER,
    REALIZED_PNL,
    WITHDRAWAL,
    DecimalInput,
    EconomicLedgerProjection,
    LedgerAvailabilityGap,
    project_economic_ledger,
    read_economic_availability_gaps,
)

PNL_INCOMPLETE = "PNL_INCOMPLETE"
PROJECTED = "PROJECTED"
FINALIZED = "FINALIZED"

_FUNNEL_STAGES = (
    "observed",
    "data_complete",
    "common_qty",
    "depth",
    "positive_cost",
    "risk",
    "sent",
    "ack",
    "filled",
    "funded",
    "closed",
    "reconciled",
)
_TCA_MARKOUT_HORIZONS = ("1s", "5s", "30s", "300s", "settlement")


@dataclass(frozen=True, slots=True)
class StatementAvailabilityGap:
    statement_key: str
    statement_source: str
    statement_type: str
    symbol: str
    event_time: str
    recorded_at: str
    gap_seconds: Decimal


@dataclass(frozen=True, slots=True)
class FunnelStageSummary:
    numerator: int
    denominator: int
    event_count: int
    conversion_rate: Decimal | None


@dataclass(frozen=True, slots=True)
class DailyTcaSummary:
    intent_count: int
    leg_count: int
    terminal_intent_count: int
    complete_intent_count: int
    unknown_unhedged_intent_count: int
    markout_complete_leg_count_by_horizon: dict[str, int]


@dataclass(frozen=True, slots=True)
class DailyNavClose:
    """Exact consolidated daily NAV equation.

    Deposits, withdrawals, commissions and borrow interest are non-negative
    magnitudes.  Realized PnL, funding, unrealized change and stablecoin FX are
    signed.  Internal transfers are reported separately and must be exactly
    zero after consolidation; they never enter the NAV equation.
    """

    status: str
    opening_nav_usd: Decimal | None
    closing_nav_usd: Decimal | None
    external_deposits_usd: Decimal | None
    external_withdrawals_usd: Decimal | None
    realized_price_pnl_usd: Decimal | None
    actual_funding_usd: Decimal | None
    commission_cost_usd: Decimal | None
    borrow_interest_cost_usd: Decimal | None
    unrealized_pnl_change_usd: Decimal | None
    stablecoin_fx_movement_usd: Decimal | None
    internal_transfers_usd: Decimal | None
    projected_closing_nav_usd: Decimal | None
    equation_difference_usd: Decimal | None
    tolerance_usd: Decimal | None
    missing_components: tuple[str, ...]
    blockers: tuple[str, ...]


def _optional_decimal(value: DecimalInput | None, field_name: str) -> Decimal | None:
    if value is None or (isinstance(value, str) and value.strip().upper() == "UNKNOWN"):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite decimal or UNKNOWN") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field_name} must be a finite decimal or UNKNOWN")
    return parsed


def calculate_daily_nav_close(
    *,
    opening_nav_usd: DecimalInput | None,
    closing_nav_usd: DecimalInput | None,
    external_deposits_usd: DecimalInput | None,
    external_withdrawals_usd: DecimalInput | None,
    realized_price_pnl_usd: DecimalInput | None,
    actual_funding_usd: DecimalInput | None,
    commission_cost_usd: DecimalInput | None,
    borrow_interest_cost_usd: DecimalInput | None,
    unrealized_pnl_change_usd: DecimalInput | None,
    stablecoin_fx_movement_usd: DecimalInput | None,
    internal_transfers_usd: DecimalInput | None,
    tolerance_usd: DecimalInput | None = None,
) -> DailyNavClose:
    """Calculate a daily NAV close without ever substituting zero for UNKNOWN.

    A complete driver set without a closing snapshot is ``PROJECTED``.  It is
    ``FINALIZED`` only when the actual close and tolerance are available, the
    equation closes inside tolerance, and consolidated internal transfers net
    exactly to zero.  Any delayed/missing component remains ``PNL_INCOMPLETE``.
    """

    values = {
        "opening_nav_usd": _optional_decimal(opening_nav_usd, "opening_nav_usd"),
        "closing_nav_usd": _optional_decimal(closing_nav_usd, "closing_nav_usd"),
        "external_deposits_usd": _optional_decimal(external_deposits_usd, "external_deposits_usd"),
        "external_withdrawals_usd": _optional_decimal(external_withdrawals_usd, "external_withdrawals_usd"),
        "realized_price_pnl_usd": _optional_decimal(realized_price_pnl_usd, "realized_price_pnl_usd"),
        "actual_funding_usd": _optional_decimal(actual_funding_usd, "actual_funding_usd"),
        "commission_cost_usd": _optional_decimal(commission_cost_usd, "commission_cost_usd"),
        "borrow_interest_cost_usd": _optional_decimal(borrow_interest_cost_usd, "borrow_interest_cost_usd"),
        "unrealized_pnl_change_usd": _optional_decimal(unrealized_pnl_change_usd, "unrealized_pnl_change_usd"),
        "stablecoin_fx_movement_usd": _optional_decimal(stablecoin_fx_movement_usd, "stablecoin_fx_movement_usd"),
        "internal_transfers_usd": _optional_decimal(internal_transfers_usd, "internal_transfers_usd"),
        "tolerance_usd": _optional_decimal(tolerance_usd, "tolerance_usd"),
    }
    for field_name in (
        "external_deposits_usd",
        "external_withdrawals_usd",
        "commission_cost_usd",
        "borrow_interest_cost_usd",
        "tolerance_usd",
    ):
        value = values[field_name]
        if value is not None and value < 0:
            raise ValueError(f"{field_name} must be non-negative")

    driver_names = (
        "opening_nav_usd",
        "external_deposits_usd",
        "external_withdrawals_usd",
        "realized_price_pnl_usd",
        "actual_funding_usd",
        "commission_cost_usd",
        "borrow_interest_cost_usd",
        "unrealized_pnl_change_usd",
        "stablecoin_fx_movement_usd",
        "internal_transfers_usd",
    )
    missing = [name for name in driver_names if values[name] is None]
    blockers: list[str] = []
    internal_transfers = values["internal_transfers_usd"]
    if internal_transfers is not None and internal_transfers != 0:
        blockers.append("internal_transfers_not_net_zero")

    projected_close: Decimal | None = None
    if not missing:
        opening = values["opening_nav_usd"]
        deposits = values["external_deposits_usd"]
        withdrawals = values["external_withdrawals_usd"]
        realized = values["realized_price_pnl_usd"]
        funding = values["actual_funding_usd"]
        commissions = values["commission_cost_usd"]
        interest = values["borrow_interest_cost_usd"]
        unrealized = values["unrealized_pnl_change_usd"]
        stablecoin_fx = values["stablecoin_fx_movement_usd"]
        assert opening is not None
        assert deposits is not None
        assert withdrawals is not None
        assert realized is not None
        assert funding is not None
        assert commissions is not None
        assert interest is not None
        assert unrealized is not None
        assert stablecoin_fx is not None
        projected_close = (
            opening + deposits - withdrawals + realized + funding - commissions - interest + unrealized + stablecoin_fx
        )

    actual_close = values["closing_nav_usd"]
    tolerance = values["tolerance_usd"]
    difference = actual_close - projected_close if actual_close is not None and projected_close is not None else None
    if missing:
        status = PNL_INCOMPLETE
        blockers.append("daily_nav_components_unknown")
    elif actual_close is None:
        status = PROJECTED if not blockers else PNL_INCOMPLETE
    elif tolerance is None:
        status = PNL_INCOMPLETE
        blockers.append("daily_nav_tolerance_unknown")
    elif difference is None or abs(difference) > tolerance:
        status = PNL_INCOMPLETE
        blockers.append("daily_nav_equation_mismatch")
    elif blockers:
        status = PNL_INCOMPLETE
    else:
        status = FINALIZED

    missing_with_close = list(missing)
    if actual_close is None:
        missing_with_close.append("closing_nav_usd")
    if actual_close is not None and tolerance is None:
        missing_with_close.append("tolerance_usd")
    return DailyNavClose(
        status=status,
        opening_nav_usd=values["opening_nav_usd"],
        closing_nav_usd=actual_close,
        external_deposits_usd=values["external_deposits_usd"],
        external_withdrawals_usd=values["external_withdrawals_usd"],
        realized_price_pnl_usd=values["realized_price_pnl_usd"],
        actual_funding_usd=values["actual_funding_usd"],
        commission_cost_usd=values["commission_cost_usd"],
        borrow_interest_cost_usd=values["borrow_interest_cost_usd"],
        unrealized_pnl_change_usd=values["unrealized_pnl_change_usd"],
        stablecoin_fx_movement_usd=values["stablecoin_fx_movement_usd"],
        internal_transfers_usd=internal_transfers,
        projected_closing_nav_usd=projected_close,
        equation_difference_usd=difference,
        tolerance_usd=tolerance,
        missing_components=tuple(missing_with_close),
        blockers=tuple(blockers),
    )


@dataclass(frozen=True, slots=True)
class ReconciledDailyReport:
    start_time: str
    end_time: str
    reconciled: bool
    event_count: int
    fill_count: int
    funding_usd: Decimal | None
    commissions_usd: Decimal | None
    borrow_interest_usd: Decimal | None
    net_economic_effect_usd: Decimal | None
    gross_fill_notional_usd: Decimal
    unvalued_event_count: int
    capital_utilization: float | None
    open_incidents: int
    critical_incidents: int
    nav_close: DailyNavClose
    blockers: tuple[str, ...]
    statement_availability_gaps: tuple[StatementAvailabilityGap, ...] = ()
    funding_availability_gaps: tuple[LedgerAvailabilityGap, ...] = ()
    opportunity_funnel: dict[str, FunnelStageSummary] = field(default_factory=dict)
    tca: DailyTcaSummary = field(
        default_factory=lambda: DailyTcaSummary(0, 0, 0, 0, 0, {})
    )


def _exact_timedelta_seconds(start: datetime, end: datetime) -> Decimal:
    delta = end - start
    return (
        Decimal(delta.days * 86_400 + delta.seconds)
        + Decimal(delta.microseconds) / Decimal("1000000")
    )


def _statement_availability_gaps(
    conn: sqlite3.Connection,
    *,
    start_time: str,
    end_time: str,
    account_id: str | None,
    trading_mode: str | None,
) -> tuple[StatementAvailabilityGap, ...]:
    clauses = ["event_time >= ?", "event_time <= ?"]
    params: list[Any] = [start_time, end_time]
    if account_id is not None:
        clauses.append("account_id = ?")
        params.append(str(account_id))
    if trading_mode is not None:
        clauses.append("LOWER(trading_mode) = ?")
        params.append(str(trading_mode).lower())
    try:
        rows = conn.execute(
            "SELECT statement_key, statement_source, statement_type, symbol, "
            "event_time, recorded_at FROM exchange_statement_entries WHERE "
            + " AND ".join(clauses)
            + " ORDER BY event_time, statement_key",
            tuple(params),
        ).fetchall()
    except sqlite3.OperationalError:
        return ()
    result: list[StatementAvailabilityGap] = []
    for row in rows:
        item = dict(row)
        event_dt = datetime.fromisoformat(str(item["event_time"]))
        recorded_dt = datetime.fromisoformat(str(item["recorded_at"]))
        result.append(
            StatementAvailabilityGap(
                statement_key=str(item["statement_key"]),
                statement_source=str(item["statement_source"]),
                statement_type=str(item["statement_type"]),
                symbol=str(item.get("symbol") or ""),
                event_time=str(item["event_time"]),
                recorded_at=str(item["recorded_at"]),
                gap_seconds=_exact_timedelta_seconds(event_dt, recorded_dt),
            )
        )
    return tuple(result)


def _funnel_summary(
    conn: sqlite3.Connection,
    *,
    start_time: str,
    end_time: str,
) -> dict[str, FunnelStageSummary]:
    result = {
        stage: FunnelStageSummary(0, 0, 0, None) for stage in _FUNNEL_STAGES
    }
    try:
        rows = conn.execute(
            """
            SELECT stage, SUM(numerator_count) AS numerator,
                   SUM(denominator_count) AS denominator, COUNT(*) AS event_count
            FROM opportunity_funnel_events
            WHERE event_time >= ? AND event_time <= ?
            GROUP BY stage, stage_ordinal ORDER BY stage_ordinal
            """,
            (start_time, end_time),
        ).fetchall()
    except sqlite3.OperationalError:
        return result
    for row in rows:
        numerator = int(row["numerator"] or 0)
        denominator = int(row["denominator"] or 0)
        result[str(row["stage"])] = FunnelStageSummary(
            numerator=numerator,
            denominator=denominator,
            event_count=int(row["event_count"] or 0),
            conversion_rate=(
                Decimal(numerator) / Decimal(denominator)
                if denominator > 0
                else None
            ),
        )
    return result


def _daily_tca_summary(
    conn: sqlite3.Connection,
    *,
    start_time: str,
    end_time: str,
) -> DailyTcaSummary:
    try:
        intent_rows = conn.execute(
            """
            SELECT * FROM execution_tca_intents
            WHERE COALESCE(decision_time, queue_time, created_at) >= ?
              AND COALESCE(decision_time, queue_time, created_at) <= ?
            """,
            (start_time, end_time),
        ).fetchall()
        intent_ids = [str(row["intent_id"]) for row in intent_rows]
        if intent_ids:
            placeholders = ",".join("?" for _ in intent_ids)
            leg_rows = conn.execute(
                f"SELECT * FROM execution_tca_legs WHERE intent_id IN ({placeholders})",
                tuple(intent_ids),
            ).fetchall()
        else:
            leg_rows = []
    except sqlite3.OperationalError:
        return DailyTcaSummary(0, 0, 0, 0, 0, {})
    legs_by_intent: dict[str, list[sqlite3.Row]] = {}
    markout_counts = {horizon: 0 for horizon in _TCA_MARKOUT_HORIZONS}
    for leg in leg_rows:
        legs_by_intent.setdefault(str(leg["intent_id"]), []).append(leg)
        markouts = json.loads(str(leg["markouts_json"] or "{}"))
        for horizon in _TCA_MARKOUT_HORIZONS:
            measurement = markouts.get(horizon)
            if (
                isinstance(measurement, dict)
                and str(measurement.get("status") or "").startswith("MEASURED")
                and measurement.get("markout_bps") is not None
            ):
                markout_counts[horizon] += 1
    complete = 0
    for intent in intent_rows:
        legs = legs_by_intent.get(str(intent["intent_id"]), [])
        shared_complete = all(
            intent[name] is not None
            for name in (
                "decision_time",
                "queue_time",
                "send_time",
                "ack_time",
                "first_fill_time",
                "last_fill_time",
                "terminal_time",
                "requested_common_quantity",
                "submitted_common_quantity",
                "unhedged_notional_ms",
            )
        )
        leg_complete = len(legs) == 2 and all(
            all(
                leg[name] is not None
                for name in (
                    "decision_bid",
                    "decision_ask",
                    "decision_mid",
                    "send_bid",
                    "send_ask",
                    "send_mid",
                    "requested_quantity",
                    "submitted_quantity",
                    "gross_filled_quantity",
                    "net_filled_quantity",
                    "vwap",
                )
            )
            and str(leg["maker_status"] or "UNKNOWN") != "UNKNOWN"
            and all(
                isinstance(json.loads(str(leg["markouts_json"] or "{}")).get(horizon), dict)
                for horizon in _TCA_MARKOUT_HORIZONS
            )
            for leg in legs
        )
        complete += int(shared_complete and leg_complete)
    return DailyTcaSummary(
        intent_count=len(intent_rows),
        leg_count=len(leg_rows),
        terminal_intent_count=sum(row["terminal_time"] is not None for row in intent_rows),
        complete_intent_count=complete,
        unknown_unhedged_intent_count=sum(
            row["unhedged_notional_ms"] is None for row in intent_rows
        ),
        markout_complete_leg_count_by_horizon=markout_counts,
    )


def build_reconciled_daily_report(
    conn: sqlite3.Connection,
    *,
    start_time: datetime,
    end_time: datetime,
    reconciliation_matched: bool,
    reserved_capital_usd: DecimalInput | None = None,
    account_equity_usd: DecimalInput | None = None,
    open_incidents: int = 0,
    critical_incidents: int = 0,
    account_id: str | None = None,
    trading_mode: str | None = None,
    strategy_id: str | None = None,
    opening_nav_usd: DecimalInput | None = None,
    closing_nav_usd: DecimalInput | None = None,
    external_deposits_usd: DecimalInput | None = None,
    external_withdrawals_usd: DecimalInput | None = None,
    realized_price_pnl_usd: DecimalInput | None = None,
    actual_funding_usd: DecimalInput | None = None,
    commission_cost_usd: DecimalInput | None = None,
    borrow_interest_cost_usd: DecimalInput | None = None,
    unrealized_pnl_change_usd: DecimalInput | None = None,
    stablecoin_fx_movement_usd: DecimalInput | None = None,
    internal_transfers_usd: DecimalInput | None = None,
    nav_tolerance_usd: DecimalInput | None = None,
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
    start_iso = start_time.astimezone(timezone.utc).isoformat()
    end_iso = end_time.astimezone(timezone.utc).isoformat()
    projection: EconomicLedgerProjection = project_economic_ledger(
        conn,
        account_id=account_id,
        trading_mode=trading_mode,
        strategy_id=strategy_id,
        start_time=start_iso,
        end_time=end_iso,
    )
    values = projection.economic_effect_usd_by_type
    cashflows = projection.cashflow_usd_by_type
    equity = _optional_decimal(account_equity_usd, "account_equity_usd")
    reserved = _optional_decimal(reserved_capital_usd, "reserved_capital_usd")
    utilization = float(reserved / equity) if reserved is not None and equity is not None and equity > 0 else None

    def supplied_or_cashflow(
        supplied: DecimalInput | None,
        event_type: str,
        *,
        negate: bool = False,
    ) -> DecimalInput | None:
        if supplied is not None:
            return supplied
        value = cashflows.get(event_type)
        return -value if value is not None and negate else value

    nav_close = calculate_daily_nav_close(
        opening_nav_usd=opening_nav_usd,
        closing_nav_usd=closing_nav_usd,
        external_deposits_usd=supplied_or_cashflow(external_deposits_usd, DEPOSIT),
        external_withdrawals_usd=supplied_or_cashflow(external_withdrawals_usd, WITHDRAWAL, negate=True),
        realized_price_pnl_usd=supplied_or_cashflow(realized_price_pnl_usd, REALIZED_PNL),
        actual_funding_usd=supplied_or_cashflow(actual_funding_usd, FUNDING),
        commission_cost_usd=supplied_or_cashflow(commission_cost_usd, COMMISSION, negate=True),
        borrow_interest_cost_usd=supplied_or_cashflow(borrow_interest_cost_usd, BORROW_INTEREST, negate=True),
        unrealized_pnl_change_usd=unrealized_pnl_change_usd,
        stablecoin_fx_movement_usd=stablecoin_fx_movement_usd,
        internal_transfers_usd=supplied_or_cashflow(internal_transfers_usd, INTERNAL_TRANSFER),
        tolerance_usd=nav_tolerance_usd,
    )
    blockers: list[str] = []
    if not reconciliation_matched:
        blockers.append("economic_ledger_reconciliation_failed")
    if projection.unvalued_cashflow_event_count:
        blockers.append("unvalued_cashflow_events")
    if projection.incomplete_envelope_event_count:
        blockers.append("incomplete_ledger_envelopes")
    if critical_incidents:
        blockers.append("critical_incidents_open")
    if nav_close.status != FINALIZED:
        blockers.append("daily_nav_not_finalized")
    blockers.extend(nav_close.blockers)
    unique_blockers = tuple(dict.fromkeys(blockers))
    statement_gaps = _statement_availability_gaps(
        conn,
        start_time=start_iso,
        end_time=end_iso,
        account_id=account_id,
        trading_mode=trading_mode,
    )
    funding_gaps = read_economic_availability_gaps(
        conn,
        event_type=FUNDING,
        account_id=account_id,
        trading_mode=trading_mode,
        strategy_id=strategy_id,
        start_time=start_iso,
        end_time=end_iso,
    )
    return ReconciledDailyReport(
        start_time=start_iso,
        end_time=end_iso,
        reconciled=reconciliation_matched and not unique_blockers,
        event_count=projection.event_count,
        fill_count=projection.fill_count,
        funding_usd=values.get(FUNDING),
        commissions_usd=values.get(COMMISSION),
        borrow_interest_usd=values.get(BORROW_INTEREST),
        net_economic_effect_usd=(
            projection.total_economic_effect_usd
            if nav_close.status in {PROJECTED, FINALIZED} and not projection.unvalued_economic_event_count
            else None
        ),
        gross_fill_notional_usd=projection.gross_fill_notional_usd,
        unvalued_event_count=projection.unvalued_economic_event_count,
        capital_utilization=utilization,
        open_incidents=max(0, int(open_incidents)),
        critical_incidents=max(0, int(critical_incidents)),
        nav_close=nav_close,
        blockers=unique_blockers,
        statement_availability_gaps=statement_gaps,
        funding_availability_gaps=funding_gaps,
        opportunity_funnel=_funnel_summary(
            conn,
            start_time=start_iso,
            end_time=end_iso,
        ),
        tca=_daily_tca_summary(
            conn,
            start_time=start_iso,
            end_time=end_iso,
        ),
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
