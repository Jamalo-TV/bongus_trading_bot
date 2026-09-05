"""SQLite-backed observability, runtime, and governance state store."""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, Mapping
from urllib.parse import quote

from bongus.core.config import AUDIT_DB_PATH, STATE_DB_PATH
from bongus.engine.account_truth import NormalizedAccountTruth
from bongus.engine.economic_ledger import (
    BALANCE_ADJUSTMENT,
    BORROW_INTEREST,
    FUNDING,
    REALIZED_PNL,
    EconomicLedgerEvent,
    EconomicLedgerProjection,
    LedgerIngestionResult,
    LedgerReconciliation,
    apply_economic_ledger_migration,
    build_cashflow_event,
    build_commission_event,
    build_fill_events,
    ingest_economic_events,
    read_economic_events,
)
from bongus.engine.economic_ledger import (
    project_economic_ledger as _project_economic_ledger,
)
from bongus.engine.economic_ledger import (
    reconcile_economic_ledger as _reconcile_economic_ledger,
)
from bongus.engine.exchange_statements import (
    ExchangeStatementIngestionResult,
    NormalizedExchangeStatement,
    apply_exchange_statement_migration,
    ingest_exchange_statement,
    normalize_binance_futures_income,
    normalize_binance_margin_interest,
    read_exchange_statement_cursor,
    read_exchange_statement_entries,
)

try:
    import orjson as _orjson  # pyright: ignore[reportMissingImports]

    def _json_dump(value: Any) -> str:
        return _orjson.dumps(value).decode()
except ModuleNotFoundError:  # graceful fallback if orjson not installed
    def _json_dump(value: Any) -> str:  # type: ignore[misc]
        return json.dumps(value)

DB_PATH = STATE_DB_PATH
CURRENT_SCHEMA_VERSION = 18
# ASCII ``BONG``.  A non-zero application id prevents an unrelated SQLite
# database from being accepted as runtime state during backup/restore or
# operator error.
APPLICATION_ID = 0x424F4E47
DEFAULT_WAL_JOURNAL_LIMIT_BYTES = 256_000_000
DEFAULT_STATE_DB_MAX_BYTES = 1_250_000_000
DEFAULT_PENDING_INTENT_TOMBSTONE_RETENTION_DAYS = 30
ACCOUNT_TRUTH_RESTART_RETENTION_PER_SCOPE = 4
ACTIVE_PENDING_INTENT_STATE = "ACTIVE"
EXCHANGE_FLAT_AWAITING_TERMINAL = "EXCHANGE_FLAT_AWAITING_TERMINAL"
TERMINAL_RECONCILED = "TERMINAL_RECONCILED"
RETAINED_PRUNED = "RETAINED_PRUNED"
TELEMETRY_PUBLICATION_META_PREFIX = "telemetry_publication:v1:"

OPPORTUNITY_FUNNEL_STAGES = (
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


def _prepare_binance_futures_income_statement(
    conn: sqlite3.Connection,
    statement: NormalizedExchangeStatement,
    *,
    availability_time: str = "",
    code_hash: str = "",
    config_hash: str = "",
    schema_hash: str = "",
    cycle_id: str = "",
    intent_id: str = "",
) -> NormalizedExchangeStatement:
    """Add prospective funding lineage while preserving immutable replays."""

    normalized_cycle_id = str(cycle_id or "").strip()
    normalized_intent_id = str(intent_id or "").strip()
    if bool(normalized_cycle_id) != bool(normalized_intent_id):
        raise ValueError("funding attribution requires both cycle_id and intent_id")
    if normalized_cycle_id and statement.statement_type != "FUNDING_FEE":
        raise ValueError("cycle funding attribution is valid only for FUNDING_FEE")

    # Attribution is prospective. If immutable evidence already exists, replay
    # its originally recorded lineage (including blank historical values)
    # instead of attempting a forbidden ledger backfill.
    existing_lineage = conn.execute(
        """
        SELECT ledger.cycle_id, ledger.intent_id
        FROM exchange_statement_entries AS statement
        LEFT JOIN economic_ledger_events AS ledger
          ON ledger.event_key = statement.ledger_event_key
        WHERE statement.statement_key = ?
        """,
        (statement.statement_key,),
    ).fetchone()
    if existing_lineage is not None:
        normalized_cycle_id = str(existing_lineage["cycle_id"] or "")
        normalized_intent_id = str(existing_lineage["intent_id"] or "")
    if statement.economic_event is None:
        return statement
    return replace(
        statement,
        economic_event=replace(
            statement.economic_event,
            availability_time=availability_time,
            code_hash=code_hash,
            config_hash=config_hash,
            schema_hash=schema_hash,
            cycle_id=normalized_cycle_id,
            intent_id=normalized_intent_id,
        ),
    )
TCA_MARKOUT_HORIZONS = ("1s", "5s", "30s", "300s", "settlement")


class LifecycleRebuildError(RuntimeError):
    """Raised when immutable lifecycle evidence cannot prove a projection rebuild."""


class ArchiveVerificationError(RuntimeError):
    """Raised when an archive batch cannot be proven byte-for-byte equivalent."""


@dataclass(slots=True)
class Trade:
    symbol: str
    side: str
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    qty: float
    net_pnl_usd: float
    funding_collected: float = 0.0
    execution_cost_usd: float = 0.0
    basis_pnl_usd: float = 0.0
    borrow_cost_usd: float = 0.0
    trading_mode: str = ""
    runtime_mode: str = ""
    session_id: str = ""
    funding_source: str = ""
    # RECONCILED values are derived exclusively from exchange economic
    # evidence.  MODELED is paper/replay economics.  INCOMPLETE retains known
    # cash flows while making missing evidence impossible to mistake for
    # realized performance.
    economic_status: str = "RECONCILED"
    economic_notes: str = ""
    estimated_net_pnl_usd: float = 0.0
    estimated_funding_collected: float = 0.0
    estimated_execution_cost_usd: float = 0.0
    estimated_basis_pnl_usd: float = 0.0
    estimated_borrow_cost_usd: float = 0.0
    cycle_id: str = ""
    entry_intent_id: str = ""
    exit_intent_id: str = ""


@dataclass(slots=True)
class CandidateSnapshot:
    cycle_id: str
    symbol: str
    direction: str
    accepted: bool
    status: str
    cluster: str
    rejection_reasons: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    snapshot_time: str | None = None
    rank: int | None = None


@dataclass(slots=True)
class OpportunityScore:
    cycle_id: str
    symbol: str
    total_score: float
    predicted_net_edge_bps: float
    rank: int
    selected: bool
    component_scores: dict[str, float]
    expected_holding_hours: float
    score_time: str | None = None


@dataclass(slots=True)
class FeatureSnapshot:
    trade_id: str
    symbol: str
    features: dict[str, Any]
    snapshot_time: str | None = None
    target_incremental_value_usd: float | None = None
    label: str = ""


@dataclass(slots=True)
class ExecutionQualitySample:
    symbol: str
    client_order_id: str
    side: str
    order_type: str
    urgency: float
    expected_cost_bps: float
    realized_slippage_bps: float
    spread_bps: float
    depth_usd: float
    maker: bool
    quality_score: float
    sample_time: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    sample_id: str = ""


@dataclass(frozen=True, slots=True)
class ExecutionTcaIntent:
    """Normalized pair-level execution evidence.

    Decimal values are stored as canonical SQLite ``TEXT`` and absent values
    remain ``NULL``.  In particular, an unobserved hedge integral must never be
    confused with an observed zero integral.
    """

    intent_id: str
    cycle_id: str
    decision_id: str
    symbol: str
    operation: str
    decision_time: str | None = None
    queue_time: str | None = None
    send_time: str | None = None
    requested_common_quantity: Decimal | int | float | str | None = None
    submitted_common_quantity: Decimal | int | float | str | None = None
    reference_price: Decimal | int | float | str | None = None
    partial: bool | None = None
    emergency: bool | None = None
    status: str = "QUEUED"
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExecutionTcaLeg:
    """One independently executable leg in a normalized TCA record."""

    intent_id: str
    leg_id: str
    market: str
    side: str
    route: str = "UNKNOWN"
    decision_bid: Decimal | int | float | str | None = None
    decision_ask: Decimal | int | float | str | None = None
    decision_mid: Decimal | int | float | str | None = None
    decision_limit: Decimal | int | float | str | None = None
    send_bid: Decimal | int | float | str | None = None
    send_ask: Decimal | int | float | str | None = None
    send_mid: Decimal | int | float | str | None = None
    send_limit: Decimal | int | float | str | None = None
    requested_quantity: Decimal | int | float | str | None = None
    submitted_quantity: Decimal | int | float | str | None = None
    partial: bool | None = None
    emergency: bool | None = None
    status: str = "QUEUED"
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OpportunityFunnelEvent:
    """One durable stage measurement with its contemporaneous denominator."""

    cycle_id: str
    stage: str
    numerator_count: int
    denominator_count: int
    event_time: str
    scope: str = "CYCLE"
    symbol: str = "*"
    intent_id: str = ""
    reached: bool | None = None
    reason: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ShadowDecision:
    trade_id: str
    symbol: str
    action: str
    hold_score: float
    exit_score: float
    incremental_value_usd: float
    recommended: bool
    decision_time: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ParameterPromotion:
    status: str
    params: dict[str, Any]
    source: str = "walk_forward"
    validation_snapshot_time: str | None = None
    promoted_at: str | None = None
    rollback_reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ValidationSnapshot:
    phase: str
    validation_status: str
    go_no_go: str
    observation_days: float
    trade_count: int
    blockers: list[str]
    metrics: dict[str, Any]
    snapshot_time: str | None = None


@dataclass(slots=True)
class PendingIntent:
    intent_id: str
    symbol: str
    intent_type: str
    direction: str
    status: str
    quantity: float
    notional_usd: float
    client_order_id: str | None = None
    retry_count: int = 0
    last_error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None
    lifecycle_state: str = ACTIVE_PENDING_INTENT_STATE
    terminal_sequence_watermark: int | None = None
    reconciliation_status: str = "PENDING"
    retention_deadline: str | None = None
    tombstoned_at: str | None = None
    tombstone_reason: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _decimal_text(
    value: Decimal | int | float | str | None,
    field_name: str,
    *,
    non_negative: bool = False,
) -> str | None:
    """Return an exact, non-exponent decimal string without inventing zero."""

    if value is None or (isinstance(value, str) and value.strip().upper() == "UNKNOWN"):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a finite decimal or UNKNOWN")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite decimal or UNKNOWN") from exc
    if not parsed.is_finite() or (non_negative and parsed < 0):
        qualifier = "non-negative " if non_negative else ""
        raise ValueError(f"{field_name} must be a finite {qualifier}decimal or UNKNOWN")
    return format(parsed, "f")


def _canonical_iso(value: str | None, field_name: str, *, required: bool = False) -> str | None:
    if value is None or not str(value).strip():
        if required:
            raise ValueError(f"{field_name} is required")
        return None
    parsed = _parse_iso(value)
    if parsed is None:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp")
    return parsed.isoformat()


def _connect(
    db_path: str = DB_PATH,
    *,
    readonly: bool = False,
    migrate: bool | None = None,
    synchronous: str = "FULL",
    journal_size_limit_bytes: int = DEFAULT_WAL_JOURNAL_LIMIT_BYTES,
    max_database_bytes: int = DEFAULT_STATE_DB_MAX_BYTES,
) -> sqlite3.Connection:
    db_identifier = str(db_path)
    embedded = db_identifier == ":memory:" or db_identifier.startswith("file:")

    if readonly:
        if db_identifier == ":memory:":
            raise FileNotFoundError("a read-only StateReader cannot open :memory:")
        if db_identifier.startswith("file:"):
            separator = "&" if "?" in db_identifier else "?"
            uri = f"{db_identifier}{separator}mode=ro"
        else:
            resolved = Path(db_identifier).resolve()
            if not resolved.is_file():
                raise FileNotFoundError(f"state database does not exist: {resolved}")
            # Quote URI metacharacters while preserving the Windows drive
            # separator and path slashes.
            uri = f"file:{quote(resolved.as_posix(), safe='/:')}?mode=ro"
        conn = sqlite3.connect(
            uri,
            uri=True,
            check_same_thread=False,
            timeout=30,
        )
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA cache_size=-8000")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.row_factory = sqlite3.Row
        return conn

    if not embedded:
        Path(db_identifier).parent.mkdir(parents=True, exist_ok=True)
    is_new_file = not embedded and (
        not Path(db_identifier).exists() or Path(db_identifier).stat().st_size == 0
    )
    conn = sqlite3.connect(db_identifier, check_same_thread=False, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    if is_new_file:
        # This pragma only takes effect before the first schema is created.
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
    conn.execute("PRAGMA journal_mode=WAL")
    normalized_synchronous = str(synchronous).strip().upper()
    if normalized_synchronous not in {"FULL", "EXTRA", "NORMAL"}:
        conn.close()
        raise ValueError(f"unsupported SQLite synchronous mode: {synchronous!r}")
    conn.execute(f"PRAGMA synchronous={normalized_synchronous}")
    conn.execute("PRAGMA busy_timeout=30000")
    # Reduce WAL checkpoint pressure, grow page cache to ~8 MB, keep temp
    # tables in memory so cycle writes don't hit the filesystem unnecessarily.
    conn.execute("PRAGMA wal_autocheckpoint=400")
    conn.execute(
        f"PRAGMA journal_size_limit={max(0, int(journal_size_limit_bytes))}"
    )
    conn.execute("PRAGMA cache_size=-8000")
    conn.execute("PRAGMA temp_store=MEMORY")
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    if max_database_bytes > 0 and page_size > 0:
        max_pages = max(1, int(max_database_bytes) // page_size)
        conn.execute(f"PRAGMA max_page_count={max_pages}")
    conn.row_factory = sqlite3.Row
    should_migrate = (not readonly) if migrate is None else bool(migrate)
    if should_migrate:
        _apply_migrations(conn)
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _apply_migrations(conn: sqlite3.Connection) -> None:
    # Use individual execute() calls instead of executescript() so that each
    # DDL statement respects the busy_timeout.  executescript() bypasses the
    # SQLite busy-handler and fails immediately with "database is locked" when
    # another connection holds a write transaction — which crashes the dashboard
    # during module import whenever the trader is mid-cycle.
    current_application_id = int(conn.execute("PRAGMA application_id").fetchone()[0])
    if current_application_id not in {0, APPLICATION_ID}:
        raise sqlite3.DatabaseError(
            "refusing to migrate SQLite database with unexpected application_id "
            f"{current_application_id}"
        )
    conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS positions (
            symbol        TEXT PRIMARY KEY,
            side          TEXT NOT NULL,
            direction     TEXT DEFAULT '',
            spot_entry    REAL NOT NULL,
            perp_entry    REAL NOT NULL,
            spot_live     REAL DEFAULT 0.0,
            perp_live     REAL DEFAULT 0.0,
            qty           REAL NOT NULL,
            hedge_ratio   REAL DEFAULT 1.0,
            ann_funding   REAL DEFAULT 0.0,
            entry_ann_funding REAL DEFAULT 0.0,
            basis_pct     REAL DEFAULT 0.0,
            net_pnl_usd   REAL DEFAULT 0.0,
            exchange_pnl_usd REAL DEFAULT 0.0,
            recovery_state TEXT DEFAULT '',
            trading_mode  TEXT DEFAULT '',
            status        TEXT DEFAULT 'OPEN',
            updated_at    TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS portfolio_stats (
            key        TEXT PRIMARY KEY,
            value      REAL NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trade_history (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol             TEXT NOT NULL,
            side               TEXT NOT NULL,
            entry_time         TEXT NOT NULL,
            exit_time          TEXT NOT NULL,
            entry_price        REAL NOT NULL,
            exit_price         REAL NOT NULL,
            qty                REAL NOT NULL,
            net_pnl_usd        REAL NOT NULL,
            funding_collected  REAL DEFAULT 0.0,
            execution_cost_usd REAL DEFAULT 0.0,
            basis_pnl_usd      REAL DEFAULT 0.0,
            borrow_cost_usd    REAL DEFAULT 0.0,
            trading_mode       TEXT DEFAULT '',
            runtime_mode       TEXT DEFAULT '',
            session_id         TEXT DEFAULT '',
            funding_source     TEXT DEFAULT '',
            economic_status    TEXT NOT NULL DEFAULT 'LEGACY_UNVERIFIED',
            economic_notes     TEXT NOT NULL DEFAULT '',
            estimated_net_pnl_usd REAL NOT NULL DEFAULT 0.0,
            estimated_funding_collected REAL NOT NULL DEFAULT 0.0,
            estimated_execution_cost_usd REAL NOT NULL DEFAULT 0.0,
            estimated_basis_pnl_usd REAL NOT NULL DEFAULT 0.0,
            estimated_borrow_cost_usd REAL NOT NULL DEFAULT 0.0,
            cycle_id           TEXT NOT NULL DEFAULT '',
            entry_intent_id    TEXT NOT NULL DEFAULT '',
            exit_intent_id     TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS risk_state (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS candidate_snapshots (
            cycle_id          TEXT NOT NULL,
            symbol            TEXT NOT NULL,
            snapshot_time     TEXT NOT NULL,
            direction         TEXT NOT NULL,
            accepted          INTEGER NOT NULL,
            status            TEXT NOT NULL,
            cluster           TEXT NOT NULL,
            rank              INTEGER,
            rejection_reasons TEXT NOT NULL,
            metrics_json      TEXT NOT NULL,
            PRIMARY KEY (cycle_id, symbol)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS opportunity_scores (
            cycle_id               TEXT NOT NULL,
            symbol                 TEXT NOT NULL,
            score_time             TEXT NOT NULL,
            total_score            REAL NOT NULL,
            predicted_net_edge_bps REAL NOT NULL,
            rank                   INTEGER NOT NULL,
            selected               INTEGER NOT NULL,
            expected_holding_hours REAL NOT NULL,
            component_scores_json  TEXT NOT NULL,
            PRIMARY KEY (cycle_id, symbol)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feature_snapshots (
            id                           INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_time                TEXT NOT NULL,
            trade_id                     TEXT NOT NULL,
            symbol                       TEXT NOT NULL,
            label                        TEXT DEFAULT '',
            target_incremental_value_usd REAL,
            features_json                TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS execution_quality (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            sample_id             TEXT DEFAULT '',
            sample_time           TEXT NOT NULL,
            symbol                TEXT NOT NULL,
            client_order_id       TEXT NOT NULL,
            side                  TEXT NOT NULL,
            order_type            TEXT NOT NULL,
            urgency               REAL DEFAULT 0.0,
            expected_cost_bps     REAL DEFAULT 0.0,
            realized_slippage_bps REAL DEFAULT 0.0,
            spread_bps            REAL DEFAULT 0.0,
            depth_usd             REAL DEFAULT 0.0,
            maker                 INTEGER DEFAULT 0,
            quality_score         REAL DEFAULT 0.0,
            metadata_json         TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS execution_tca_intents (
            intent_id                    TEXT PRIMARY KEY,
            cycle_id                     TEXT NOT NULL,
            decision_id                  TEXT NOT NULL DEFAULT '',
            symbol                       TEXT NOT NULL,
            operation                    TEXT NOT NULL CHECK(operation IN ('ENTRY', 'EXIT')),
            decision_time                TEXT,
            queue_time                   TEXT,
            send_time                    TEXT,
            ack_time                     TEXT,
            first_fill_time              TEXT,
            last_fill_time               TEXT,
            cancel_time                  TEXT,
            terminal_time                TEXT,
            requested_common_quantity    TEXT,
            submitted_common_quantity    TEXT,
            unhedged_notional_ms          TEXT,
            last_hedge_observation_time   TEXT,
            last_spot_gross_quantity      TEXT,
            last_perp_gross_quantity      TEXT,
            last_reference_price          TEXT,
            partial                       INTEGER,
            emergency                     INTEGER,
            status                        TEXT NOT NULL,
            metadata_json                 TEXT NOT NULL DEFAULT '{}',
            created_at                    TEXT NOT NULL,
            updated_at                    TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS execution_tca_legs (
            intent_id             TEXT NOT NULL,
            leg_id                TEXT NOT NULL,
            market                TEXT NOT NULL CHECK(market IN ('spot', 'perp')),
            side                  TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
            route                 TEXT NOT NULL DEFAULT 'UNKNOWN',
            decision_time         TEXT,
            queue_time            TEXT,
            send_time             TEXT,
            ack_time              TEXT,
            first_fill_time       TEXT,
            last_fill_time        TEXT,
            cancel_time           TEXT,
            terminal_time         TEXT,
            decision_bid          TEXT,
            decision_ask          TEXT,
            decision_mid          TEXT,
            decision_limit        TEXT,
            send_bid              TEXT,
            send_ask              TEXT,
            send_mid              TEXT,
            send_limit            TEXT,
            requested_quantity    TEXT,
            submitted_quantity    TEXT,
            gross_filled_quantity TEXT,
            net_filled_quantity   TEXT,
            vwap                  TEXT,
            commissions_json      TEXT NOT NULL DEFAULT '{}',
            maker_status          TEXT NOT NULL DEFAULT 'UNKNOWN',
            partial               INTEGER,
            emergency             INTEGER,
            markouts_json         TEXT NOT NULL DEFAULT '{}',
            status                TEXT NOT NULL,
            metadata_json         TEXT NOT NULL DEFAULT '{}',
            created_at            TEXT NOT NULL,
            updated_at            TEXT NOT NULL,
            PRIMARY KEY(intent_id, leg_id),
            FOREIGN KEY(intent_id) REFERENCES execution_tca_intents(intent_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS opportunity_funnel_events (
            event_key          TEXT PRIMARY KEY,
            cycle_id           TEXT NOT NULL,
            scope              TEXT NOT NULL CHECK(scope IN ('CYCLE', 'INTENT')),
            symbol             TEXT NOT NULL,
            intent_id          TEXT NOT NULL DEFAULT '',
            stage              TEXT NOT NULL,
            stage_ordinal      INTEGER NOT NULL,
            reached            INTEGER,
            numerator_count    INTEGER NOT NULL CHECK(numerator_count >= 0),
            denominator_count  INTEGER NOT NULL CHECK(denominator_count >= 0),
            reason             TEXT NOT NULL DEFAULT '',
            event_time         TEXT NOT NULL,
            content_hash       TEXT NOT NULL,
            metadata_json      TEXT NOT NULL DEFAULT '{}',
            UNIQUE(cycle_id, scope, symbol, intent_id, stage)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS model_shadow_decisions (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_time         TEXT NOT NULL,
            trade_id              TEXT NOT NULL,
            symbol                TEXT NOT NULL,
            action                TEXT NOT NULL,
            hold_score            REAL DEFAULT 0.0,
            exit_score            REAL DEFAULT 0.0,
            incremental_value_usd REAL DEFAULT 0.0,
            recommended           INTEGER DEFAULT 0,
            metadata_json         TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS parameter_promotions (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            promoted_at              TEXT NOT NULL,
            status                   TEXT NOT NULL,
            source                   TEXT NOT NULL,
            validation_snapshot_time TEXT,
            rollback_reason          TEXT DEFAULT '',
            params_json              TEXT NOT NULL,
            metadata_json            TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS validation_snapshots (
            snapshot_time        TEXT PRIMARY KEY,
            phase                TEXT NOT NULL,
            validation_status    TEXT NOT NULL,
            go_no_go             TEXT NOT NULL,
            observation_days     REAL DEFAULT 0.0,
            trade_count          INTEGER DEFAULT 0,
            blockers             TEXT NOT NULL,
            metrics_json         TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS execution_events (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol               TEXT NOT NULL,
            client_order_id      TEXT NOT NULL,
            status               TEXT NOT NULL,
            filled_qty           REAL DEFAULT 0.0,
            cumulative_filled_qty REAL,
            avg_fill_price       REAL,
            last_fill_price      REAL,
            cumulative_quote_qty REAL,
            commission           REAL,
            commission_asset     TEXT,
            realized_pnl         REAL,
            maker                INTEGER,
            execution_type       TEXT,
            market               TEXT,
            side                 TEXT,
            order_id             TEXT,
            trade_id             TEXT,
            account_id           TEXT,
            environment          TEXT,
            strategy_id          TEXT,
            cycle_id             TEXT,
            intent_id            TEXT,
            leg_id               TEXT,
            config_version_hash  TEXT,
            telemetry_schema_version INTEGER,
            telemetry_sequence  INTEGER,
            event_name           TEXT DEFAULT 'OrderUpdate',
            asset                TEXT,
            amount               REAL,
            reason               TEXT DEFAULT '',
            trading_mode         TEXT DEFAULT '',
            runtime_mode         TEXT DEFAULT '',
            session_id           TEXT DEFAULT '',
            event_time           TEXT NOT NULL,
            raw_payload          TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS health_samples (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            sample_time    TEXT NOT NULL,
            symbol         TEXT,
            metric         TEXT NOT NULL,
            value          REAL DEFAULT 0.0,
            expected_value REAL DEFAULT 0.0,
            zscore         REAL,
            alert_level    TEXT DEFAULT '',
            runtime_mode   TEXT DEFAULT '',
            notes          TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_samples (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            sample_minute          TEXT NOT NULL,
            symbol                 TEXT NOT NULL,
            ann_funding            REAL DEFAULT 0.0,
            basis_pct              REAL DEFAULT 0.0,
            mark_price             REAL DEFAULT 0.0,
            minute_notional_volume REAL DEFAULT 0.0,
            UNIQUE(symbol, sample_minute)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_intents (
            intent_id       TEXT PRIMARY KEY,
            symbol          TEXT NOT NULL,
            intent_type     TEXT NOT NULL,
            direction       TEXT NOT NULL DEFAULT '',
            status          TEXT NOT NULL,
            quantity        REAL DEFAULT 0.0,
            notional_usd    REAL DEFAULT 0.0,
            client_order_id TEXT,
            retry_count     INTEGER DEFAULT 0,
            last_error      TEXT,
            metadata        TEXT,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            lifecycle_state TEXT NOT NULL DEFAULT 'ACTIVE',
            terminal_sequence_watermark INTEGER,
            reconciliation_status TEXT NOT NULL DEFAULT 'PENDING',
            retention_deadline TEXT,
            tombstoned_at    TEXT,
            tombstone_reason TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS account_truth_snapshots (
            snapshot_id            TEXT PRIMARY KEY,
            schema_version         INTEGER NOT NULL,
            account_id             TEXT NOT NULL,
            environment            TEXT NOT NULL,
            captured_at            TEXT,
            availability_time      TEXT,
            expires_at             TEXT,
            status                 TEXT NOT NULL CHECK(status IN ('COMPLETE', 'UNKNOWN', 'STALE')),
            standard_spot_status   TEXT NOT NULL CHECK(standard_spot_status IN ('COMPLETE', 'UNKNOWN', 'STALE')),
            usd_m_futures_status   TEXT NOT NULL CHECK(usd_m_futures_status IN ('COMPLETE', 'UNKNOWN', 'STALE')),
            missing_fields_json    TEXT NOT NULL,
            standard_spot_json     TEXT NOT NULL,
            usd_m_futures_json     TEXT NOT NULL,
            raw_snapshot_json      TEXT NOT NULL,
            content_hash           TEXT NOT NULL,
            created_at             TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_report_proposals (
            proposal_id          TEXT PRIMARY KEY,
            created_at           TEXT NOT NULL,
            report_period_start  TEXT,
            report_period_end    TEXT,
            summary              TEXT NOT NULL,
            proposed_changes     TEXT NOT NULL,
            status               TEXT NOT NULL,
            decision_time        TEXT,
            decision_source      TEXT,
            applied_at           TEXT,
            raw_response         TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_hourly_aggregates (
            bucket_hour                TEXT NOT NULL,
            symbol                     TEXT NOT NULL,
            sample_count               INTEGER NOT NULL CHECK(sample_count > 0),
            ann_funding_avg             REAL NOT NULL,
            ann_funding_min             REAL NOT NULL,
            ann_funding_max             REAL NOT NULL,
            basis_pct_avg               REAL NOT NULL,
            basis_pct_min               REAL NOT NULL,
            basis_pct_max               REAL NOT NULL,
            mark_price_avg              REAL NOT NULL,
            mark_price_min              REAL NOT NULL,
            mark_price_max              REAL NOT NULL,
            notional_volume_sum         REAL NOT NULL,
            source_first_minute         TEXT NOT NULL,
            source_last_minute          TEXT NOT NULL,
            refreshed_at                TEXT NOT NULL,
            PRIMARY KEY (bucket_hour, symbol)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS telemetry_receipts (
            telemetry_sequence INTEGER PRIMARY KEY CHECK(telemetry_sequence > 0),
            schema_version     INTEGER NOT NULL CHECK(schema_version > 0),
            event_hash         TEXT NOT NULL CHECK(length(event_hash) = 64),
            status             TEXT NOT NULL CHECK(status IN ('PROCESSING', 'PROCESSED')),
            first_seen_at      TEXT NOT NULL,
            processed_at       TEXT,
            raw_payload        TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS archive_batch_manifests (
            batch_id             TEXT PRIMARY KEY,
            table_name           TEXT NOT NULL,
            cutoff_time          TEXT NOT NULL,
            row_count            INTEGER NOT NULL,
            content_sha256       TEXT NOT NULL,
            state                TEXT NOT NULL,
            archive_db_path      TEXT NOT NULL,
            created_at           TEXT NOT NULL,
            verified_at          TEXT,
            completed_at         TEXT,
            error                TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS execution_decisions (
            decision_id         TEXT PRIMARY KEY,
            cycle_id            TEXT NOT NULL,
            symbol              TEXT NOT NULL,
            direction           TEXT NOT NULL,
            action              TEXT NOT NULL,
            accepted            INTEGER NOT NULL,
            config_version_hash TEXT NOT NULL,
            model_version       TEXT NOT NULL,
            decision_hash       TEXT NOT NULL,
            decision_payload    TEXT NOT NULL,
            created_at          TEXT NOT NULL,
            UNIQUE(cycle_id, symbol, action)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lifecycle_events (
            event_key    TEXT PRIMARY KEY,
            event_type   TEXT NOT NULL,
            symbol       TEXT NOT NULL,
            intent_id    TEXT NOT NULL DEFAULT '',
            event_time   TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS execution_command_sequences (
            producer_id   TEXT PRIMARY KEY,
            last_sequence INTEGER NOT NULL,
            updated_at    TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS execution_command_outbox (
            intent_id            TEXT PRIMARY KEY,
            schema_version       INTEGER NOT NULL,
            producer_id          TEXT NOT NULL,
            sequence             INTEGER NOT NULL,
            intent_type          TEXT NOT NULL,
            symbol               TEXT NOT NULL,
            command_hash         TEXT NOT NULL,
            envelope_json        TEXT NOT NULL,
            state                TEXT NOT NULL,
            ack_reason           TEXT NOT NULL DEFAULT '',
            send_attempts        INTEGER NOT NULL DEFAULT 0,
            created_at_ms        INTEGER NOT NULL,
            deadline_at_ms       INTEGER NOT NULL,
            first_sent_at        TEXT,
            last_sent_at         TEXT,
            last_ack_at          TEXT,
            updated_at           TEXT NOT NULL,
            UNIQUE(producer_id, sequence)
        )
        """
    )
    apply_economic_ledger_migration(conn)
    apply_exchange_statement_migration(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_candidate_snapshots_time ON candidate_snapshots(snapshot_time DESC)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_execution_decisions_cycle "
        "ON execution_decisions(cycle_id, symbol, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_opportunity_scores_time "
        "ON opportunity_scores(score_time DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_feature_snapshots_trade "
        "ON feature_snapshots(trade_id, snapshot_time DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_execution_quality_symbol "
        "ON execution_quality(symbol, sample_time DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_execution_tca_cycle "
        "ON execution_tca_intents(cycle_id, symbol, decision_time)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_execution_tca_leg_status "
        "ON execution_tca_legs(status, market, updated_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_opportunity_funnel_time "
        "ON opportunity_funnel_events(event_time, stage_ordinal, cycle_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_shadow_decisions_symbol "
        "ON model_shadow_decisions(symbol, decision_time DESC)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shadow_decisions_time ON model_shadow_decisions(decision_time DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_promotions_time ON parameter_promotions(promoted_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_health_samples_time ON health_samples(sample_time DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_market_samples_time ON market_samples(sample_minute DESC)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_hourly_time "
        "ON market_hourly_aggregates(bucket_hour DESC, symbol)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_command_outbox_state ON execution_command_outbox(state, updated_at)"
    )
    _ensure_column(conn, "positions", "direction", "TEXT DEFAULT ''")
    _ensure_column(conn, "positions", "hedge_ratio", "REAL DEFAULT 1.0")
    _ensure_column(conn, "positions", "entry_ann_funding", "REAL DEFAULT 0.0")
    _ensure_column(conn, "positions", "exchange_pnl_usd", "REAL DEFAULT 0.0")
    _ensure_column(conn, "positions", "recovery_state", "TEXT DEFAULT ''")
    _ensure_column(conn, "positions", "trading_mode", "TEXT DEFAULT ''")
    _ensure_column(conn, "trade_history", "execution_cost_usd", "REAL DEFAULT 0.0")
    _ensure_column(conn, "trade_history", "basis_pnl_usd", "REAL DEFAULT 0.0")
    _ensure_column(conn, "trade_history", "borrow_cost_usd", "REAL DEFAULT 0.0")
    _ensure_column(conn, "trade_history", "trading_mode", "TEXT DEFAULT ''")
    _ensure_column(conn, "trade_history", "runtime_mode", "TEXT DEFAULT ''")
    _ensure_column(conn, "trade_history", "session_id", "TEXT DEFAULT ''")
    _ensure_column(conn, "trade_history", "funding_source", "TEXT DEFAULT ''")
    _ensure_column(
        conn,
        "trade_history",
        "economic_status",
        "TEXT NOT NULL DEFAULT 'LEGACY_UNVERIFIED'",
    )
    _ensure_column(conn, "trade_history", "economic_notes", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(
        conn,
        "trade_history",
        "estimated_net_pnl_usd",
        "REAL NOT NULL DEFAULT 0.0",
    )
    _ensure_column(
        conn,
        "trade_history",
        "estimated_funding_collected",
        "REAL NOT NULL DEFAULT 0.0",
    )
    _ensure_column(
        conn,
        "trade_history",
        "estimated_execution_cost_usd",
        "REAL NOT NULL DEFAULT 0.0",
    )
    _ensure_column(
        conn,
        "trade_history",
        "estimated_basis_pnl_usd",
        "REAL NOT NULL DEFAULT 0.0",
    )
    _ensure_column(
        conn,
        "trade_history",
        "estimated_borrow_cost_usd",
        "REAL NOT NULL DEFAULT 0.0",
    )
    _ensure_column(conn, "trade_history", "cycle_id", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "trade_history", "entry_intent_id", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "trade_history", "exit_intent_id", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "execution_quality", "sample_id", "TEXT DEFAULT ''")
    _ensure_column(conn, "execution_events", "event_name", "TEXT DEFAULT 'OrderUpdate'")
    _ensure_column(conn, "execution_events", "asset", "TEXT")
    _ensure_column(conn, "execution_events", "amount", "REAL")
    _ensure_column(conn, "execution_events", "reason", "TEXT DEFAULT ''")
    _ensure_column(conn, "execution_events", "trading_mode", "TEXT DEFAULT ''")
    _ensure_column(conn, "execution_events", "runtime_mode", "TEXT DEFAULT ''")
    _ensure_column(conn, "execution_events", "session_id", "TEXT DEFAULT ''")
    _ensure_column(conn, "execution_events", "cumulative_filled_qty", "REAL")
    _ensure_column(conn, "execution_events", "market", "TEXT")
    _ensure_column(conn, "execution_events", "side", "TEXT")
    _ensure_column(conn, "execution_events", "order_id", "TEXT")
    _ensure_column(conn, "execution_events", "trade_id", "TEXT")
    _ensure_column(conn, "execution_events", "account_id", "TEXT")
    _ensure_column(conn, "execution_events", "environment", "TEXT")
    _ensure_column(conn, "execution_events", "strategy_id", "TEXT")
    _ensure_column(conn, "execution_events", "cycle_id", "TEXT")
    _ensure_column(conn, "execution_events", "intent_id", "TEXT")
    _ensure_column(conn, "execution_events", "leg_id", "TEXT")
    _ensure_column(conn, "execution_events", "config_version_hash", "TEXT")
    _ensure_column(conn, "execution_events", "telemetry_schema_version", "INTEGER")
    _ensure_column(conn, "execution_events", "telemetry_sequence", "INTEGER")
    _ensure_column(
        conn,
        "pending_intents",
        "lifecycle_state",
        "TEXT NOT NULL DEFAULT 'ACTIVE'",
    )
    _ensure_column(conn, "pending_intents", "terminal_sequence_watermark", "INTEGER")
    _ensure_column(
        conn,
        "pending_intents",
        "reconciliation_status",
        "TEXT NOT NULL DEFAULT 'PENDING'",
    )
    _ensure_column(conn, "pending_intents", "retention_deadline", "TEXT")
    _ensure_column(conn, "pending_intents", "tombstoned_at", "TEXT")
    _ensure_column(
        conn,
        "pending_intents",
        "tombstone_reason",
        "TEXT NOT NULL DEFAULT ''",
    )
    _ensure_column(
        conn,
        "telemetry_receipts",
        "raw_payload",
        "TEXT NOT NULL DEFAULT '{}'",
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trade_history_scope ON trade_history(trading_mode, session_id, exit_time DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_execution_events_scope "
        "ON execution_events(trading_mode, session_id, event_time DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_execution_events_exchange_identity "
        "ON execution_events(symbol, market, order_id, trade_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_execution_events_lineage "
        "ON execution_events(account_id, strategy_id, cycle_id, intent_id, leg_id)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_events_telemetry_sequence "
        "ON execution_events(telemetry_sequence) WHERE telemetry_sequence IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lifecycle_events_symbol_time "
        "ON lifecycle_events(symbol, event_time, event_type)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_quality_sample_id "
        "ON execution_quality(sample_id) WHERE sample_id != ''"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_intents_lifecycle_retention "
        "ON pending_intents(lifecycle_state, retention_deadline, symbol)"
    )
    conn.execute("DROP INDEX IF EXISTS idx_account_truth_latest")
    conn.execute(
        "CREATE INDEX idx_account_truth_latest "
        "ON account_truth_snapshots(account_id, environment, created_at DESC, availability_time DESC)"
    )
    conn.execute(
        """
        INSERT INTO schema_meta (key, value)
        VALUES ('schema_version', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (str(CURRENT_SCHEMA_VERSION),),
    )
    conn.execute(f"PRAGMA user_version={CURRENT_SCHEMA_VERSION}")
    conn.commit()


class StateWriter:
    def __init__(
        self,
        db_path: str = DB_PATH,
        *,
        migrate: bool = True,
        synchronous: str = "FULL",
        max_database_bytes: int = DEFAULT_STATE_DB_MAX_BYTES,
    ) -> None:
        self.conn = _connect(
            db_path,
            migrate=migrate,
            synchronous=synchronous,
            max_database_bytes=max_database_bytes,
        )
        self._economic_ledger_lock = RLock()
        self._exchange_statement_lock = RLock()
        self._lifecycle_lock = RLock()
        self._command_outbox_lock = RLock()
        self._telemetry_receipt_lock = RLock()
        db_identifier = str(db_path)
        embedded_connection = (
            db_identifier == ":memory:" or db_identifier.startswith("file:")
        )
        # Keep command durability isolated from cycle/config writes performed
        # through ``self.conn`` on other threads.  A transport send must never
        # race an unrelated commit on the shared runtime connection.
        self._owns_command_connection = not embedded_connection
        self._command_conn = (
            _connect(
                db_path,
                migrate=False,
                synchronous=synchronous,
                max_database_bytes=max_database_bytes,
            )
            if self._owns_command_connection
            else self.conn
        )
        # Statement ingestion is an immediate durability boundary.  Keep it
        # isolated from cycle-batched writes so its commit cannot accidentally
        # commit unrelated runtime state.  Embedded stores must share the one
        # authoritative connection because a second ``:memory:`` connection is
        # a different database.
        self._owns_statement_connection = not embedded_connection
        self._statement_conn = (
            _connect(
                db_path,
                migrate=False,
                synchronous=synchronous,
                max_database_bytes=max_database_bytes,
            )
            if self._owns_statement_connection
            else self.conn
        )
        # Critical telemetry receipt commits are the Python-side ACK boundary.
        # Keep them isolated from cycle-batched projection writes: committing a
        # raw receipt must neither commit unrelated state early nor wait behind
        # slow economic/lifecycle work on the runtime connection.
        self._owns_telemetry_connection = not embedded_connection
        self._telemetry_conn = (
            _connect(
                db_path,
                migrate=False,
                synchronous=synchronous,
                max_database_bytes=max_database_bytes,
            )
            if self._owns_telemetry_connection
            else self.conn
        )
        # Recovery guards commit immediately and may be activated from
        # telemetry/config callbacks.  Give each subsystem its own connection
        # so their transactions cannot commit or roll back an unrelated cycle
        # batch on ``self.conn`` (or one another).
        self._guard_lock = RLock()
        self._owns_guard_connections = not embedded_connection
        if self._owns_guard_connections:
            self._cooldown_conn = _connect(
                db_path,
                migrate=False,
                synchronous=synchronous,
                max_database_bytes=max_database_bytes,
            )
            self._feed_recovery_conn = _connect(
                db_path,
                migrate=False,
                synchronous=synchronous,
                max_database_bytes=max_database_bytes,
            )
        else:
            # Opening another ``:memory:`` connection creates another database;
            # URI lifetime/cache semantics are similarly easy to violate.  Keep
            # guard state on the authoritative connection and serialize it with
            # one shared re-entrant lock for these test/embedded stores.
            self._cooldown_conn = self.conn
            self._feed_recovery_conn = self.conn

    def flush(self) -> None:
        """Commit any pending writes accumulated during a cycle batch."""
        self.conn.commit()

    def _runtime_context(self) -> dict[str, str]:
        rows = self.conn.execute(
            """
            SELECT key, value
            FROM risk_state
            WHERE key IN ('trading_mode', 'runtime_mode', 'session_id')
            """
        ).fetchall()
        context = {"trading_mode": "", "runtime_mode": "", "session_id": ""}
        for row in rows:
            key = str(row["key"])
            value = str(row["value"] or "")
            if key == "trading_mode":
                context[key] = value.lower()
            else:
                context[key] = value
        return context

    def upsert_position(
        self,
        symbol: str,
        side: str,
        spot_entry: float,
        perp_entry: float,
        qty: float,
        direction: str = "",
        hedge_ratio: float = 1.0,
        ann_funding: float = 0.0,
        entry_ann_funding: float | None = None,
        basis_pct: float = 0.0,
        net_pnl_usd: float = 0.0,
        exchange_pnl_usd: float = 0.0,
        recovery_state: str = "",
        trading_mode: str | None = None,
        status: str = "OPEN",
        spot_live: float = 0.0,
        perp_live: float = 0.0,
        updated_at: str | None = None,
        commit: bool = True,
    ) -> None:
        effective_entry_ann_funding = ann_funding if entry_ann_funding is None else entry_ann_funding
        context = self._runtime_context()
        effective_trading_mode = context["trading_mode"] if trading_mode is None else str(trading_mode or "").lower()
        self.conn.execute(
            """
            INSERT INTO positions
                (symbol, side, direction, spot_entry, perp_entry, spot_live, perp_live, qty,
                 hedge_ratio, ann_funding, entry_ann_funding, basis_pct, net_pnl_usd,
                 exchange_pnl_usd, recovery_state, trading_mode, status, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                side=excluded.side,
                direction=excluded.direction,
                spot_entry=excluded.spot_entry,
                perp_entry=excluded.perp_entry,
                spot_live=excluded.spot_live,
                perp_live=excluded.perp_live,
                qty=excluded.qty,
                hedge_ratio=excluded.hedge_ratio,
                ann_funding=excluded.ann_funding,
                entry_ann_funding=excluded.entry_ann_funding,
                basis_pct=excluded.basis_pct,
                net_pnl_usd=excluded.net_pnl_usd,
                exchange_pnl_usd=excluded.exchange_pnl_usd,
                recovery_state=excluded.recovery_state,
                trading_mode=excluded.trading_mode,
                status=excluded.status,
                updated_at=excluded.updated_at
            """,
            (
                symbol,
                side,
                direction,
                spot_entry,
                perp_entry,
                spot_live,
                perp_live,
                qty,
                hedge_ratio,
                ann_funding,
                effective_entry_ann_funding,
                basis_pct,
                net_pnl_usd,
                exchange_pnl_usd,
                recovery_state,
                effective_trading_mode,
                status,
                updated_at or _now(),
            ),
        )
        if commit:
            self.conn.commit()

    def remove_position(self, symbol: str, *, commit: bool = True) -> None:
        self.conn.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))
        if commit:
            self.conn.commit()

    def clear_trade_history(self) -> None:
        """Retain immutable trade evidence when resetting operator metrics.

        Historical trades are Tier-A audit data.  A dashboard/statistics reset
        must not turn into an unbounded archive followed by a blanket delete:
        the bounded retention worker is the only path allowed to move these
        rows, and it verifies every batch before deleting its source copy.
        """

        logging.warning(
            "Trade-history reset requested; immutable audit rows were retained. "
            "Use verified bounded archival for retention."
        )

    def clear_execution_events(self) -> None:
        """Retain immutable execution lifecycle evidence across UI resets."""

        logging.warning(
            "Execution-event reset requested; immutable audit rows were retained. "
            "Use verified bounded archival for retention."
        )

    def record_trade(self, trade: Trade, *, commit: bool = True) -> None:
        context = self._runtime_context()
        effective_trading_mode = str(trade.trading_mode or context["trading_mode"] or "").lower()
        effective_runtime_mode = str(trade.runtime_mode or context["runtime_mode"] or "").upper()
        effective_session_id = str(trade.session_id or context["session_id"] or "")
        self.conn.execute(
            """
            INSERT INTO trade_history
                (symbol, side, entry_time, exit_time, entry_price, exit_price, qty,
                 net_pnl_usd, funding_collected, execution_cost_usd, basis_pnl_usd,
                 borrow_cost_usd, trading_mode, runtime_mode, session_id, funding_source,
                 economic_status, economic_notes, estimated_net_pnl_usd,
                 estimated_funding_collected, estimated_execution_cost_usd,
                 estimated_basis_pnl_usd, estimated_borrow_cost_usd, cycle_id,
                 entry_intent_id, exit_intent_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade.symbol,
                trade.side,
                trade.entry_time,
                trade.exit_time,
                trade.entry_price,
                trade.exit_price,
                trade.qty,
                trade.net_pnl_usd,
                trade.funding_collected,
                trade.execution_cost_usd,
                trade.basis_pnl_usd,
                trade.borrow_cost_usd,
                effective_trading_mode,
                effective_runtime_mode,
                effective_session_id,
                str(trade.funding_source or ""),
                str(trade.economic_status or "INCOMPLETE").upper(),
                str(trade.economic_notes or ""),
                trade.estimated_net_pnl_usd,
                trade.estimated_funding_collected,
                trade.estimated_execution_cost_usd,
                trade.estimated_basis_pnl_usd,
                trade.estimated_borrow_cost_usd,
                str(trade.cycle_id or ""),
                str(trade.entry_intent_id or ""),
                str(trade.exit_intent_id or ""),
            ),
        )
        if commit:
            self.conn.commit()

    def set_stat(self, key: str, value: Any) -> None:
        self.conn.execute(
            """
            INSERT INTO portfolio_stats (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, value, _now()),
        )
        self.conn.commit()

    def set_stats(self, stats: dict[str, Any]) -> None:
        now = _now()
        self.conn.executemany(
            """
            INSERT INTO portfolio_stats (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            [(key, value, now) for key, value in stats.items()],
        )
        # Caller (run_cycle) is responsible for the final flush().

    def set_risk(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO risk_state (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, value, _now()),
        )
        self.conn.commit()

    def set_risk_snapshot(self, snapshot: dict[str, Any]) -> None:
        now = _now()
        rows = [
            (key, value if isinstance(value, str) else _json_dump(value), now)
            for key, value in snapshot.items()
        ]
        self.conn.executemany(
            """
            INSERT INTO risk_state (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            rows,
        )
        self.conn.commit()
        # Caller (run_cycle) is responsible for the final flush().

    def record_candidate_snapshots(self, snapshots: Iterable[CandidateSnapshot]) -> None:
        rows = [
            (
                snapshot.cycle_id,
                snapshot.symbol,
                snapshot.snapshot_time or _now(),
                snapshot.direction,
                int(snapshot.accepted),
                snapshot.status,
                snapshot.cluster,
                snapshot.rank,
                _json_dump(snapshot.rejection_reasons),
                _json_dump(snapshot.metrics),
            )
            for snapshot in snapshots
        ]
        self.conn.executemany(
            """
            INSERT INTO candidate_snapshots
                (cycle_id, symbol, snapshot_time, direction, accepted, status,
                 cluster, rank, rejection_reasons, metrics_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cycle_id, symbol) DO UPDATE SET
                snapshot_time=excluded.snapshot_time,
                direction=excluded.direction,
                accepted=excluded.accepted,
                status=excluded.status,
                cluster=excluded.cluster,
                rank=excluded.rank,
                rejection_reasons=excluded.rejection_reasons,
                metrics_json=excluded.metrics_json
            """,
            rows,
        )
        # Caller (run_cycle) is responsible for the final flush().

    def record_opportunity_scores(self, scores: Iterable[OpportunityScore]) -> None:
        rows = [
            (
                score.cycle_id,
                score.symbol,
                score.score_time or _now(),
                score.total_score,
                score.predicted_net_edge_bps,
                score.rank,
                int(score.selected),
                score.expected_holding_hours,
                _json_dump(score.component_scores),
            )
            for score in scores
        ]
        self.conn.executemany(
            """
            INSERT INTO opportunity_scores
                (cycle_id, symbol, score_time, total_score, predicted_net_edge_bps,
                 rank, selected, expected_holding_hours, component_scores_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cycle_id, symbol) DO UPDATE SET
                score_time=excluded.score_time,
                total_score=excluded.total_score,
                predicted_net_edge_bps=excluded.predicted_net_edge_bps,
                rank=excluded.rank,
                selected=excluded.selected,
                expected_holding_hours=excluded.expected_holding_hours,
                component_scores_json=excluded.component_scores_json
            """,
            rows,
        )
        # Caller (run_cycle) is responsible for the final flush().

    def record_feature_snapshot(self, snapshot: FeatureSnapshot) -> None:
        self.record_feature_snapshots([snapshot])

    def record_feature_snapshots(self, snapshots: Iterable[FeatureSnapshot]) -> None:
        self.conn.executemany(
            """
            INSERT INTO feature_snapshots
                (snapshot_time, trade_id, symbol, label, target_incremental_value_usd, features_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    snapshot.snapshot_time or _now(),
                    snapshot.trade_id,
                    snapshot.symbol,
                    snapshot.label,
                    snapshot.target_incremental_value_usd,
                    _json_dump(snapshot.features),
                )
                for snapshot in snapshots
            ],
        )
        # Caller (run_cycle) is responsible for the final flush().

    def record_execution_quality(self, sample: ExecutionQualitySample) -> bool:
        sample_id = str(sample.sample_id or "").strip()
        metadata_json = _json_dump(sample.metadata)
        if sample_id:
            existing = self.conn.execute(
                """
                SELECT symbol, client_order_id, side, order_type, urgency,
                       expected_cost_bps, realized_slippage_bps, spread_bps,
                       depth_usd, maker, quality_score, metadata_json
                FROM execution_quality WHERE sample_id = ?
                """,
                (sample_id,),
            ).fetchone()
            if existing is not None:
                expected = (
                    sample.symbol,
                    sample.client_order_id,
                    sample.side,
                    sample.order_type,
                    sample.urgency,
                    sample.expected_cost_bps,
                    sample.realized_slippage_bps,
                    sample.spread_bps,
                    sample.depth_usd,
                    int(sample.maker),
                    sample.quality_score,
                    json.loads(metadata_json),
                )
                observed = tuple(existing[:-1]) + (json.loads(existing[-1]),)
                if observed == expected:
                    return False
                raise ValueError(f"execution quality sample_id collision: {sample_id}")
        self.conn.execute(
            """
            INSERT INTO execution_quality
                (sample_id, sample_time, symbol, client_order_id, side, order_type, urgency,
                 expected_cost_bps, realized_slippage_bps, spread_bps, depth_usd,
                 maker, quality_score, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sample_id,
                sample.sample_time or _now(),
                sample.symbol,
                sample.client_order_id,
                sample.side,
                sample.order_type,
                sample.urgency,
                sample.expected_cost_bps,
                sample.realized_slippage_bps,
                sample.spread_bps,
                sample.depth_usd,
                int(sample.maker),
                sample.quality_score,
                metadata_json,
            ),
        )
        # Caller (run_cycle) is responsible for the final flush().
        return True

    def record_execution_tca(
        self,
        intent: ExecutionTcaIntent,
        legs: Iterable[ExecutionTcaLeg],
    ) -> bool:
        """Create one pair and its legs without overwriting later observations."""

        intent_id = str(intent.intent_id or "").strip()
        cycle_id = str(intent.cycle_id or "").strip()
        symbol = str(intent.symbol or "").strip().upper()
        operation = str(intent.operation or "").strip().upper()
        if not intent_id or not cycle_id or not symbol:
            raise ValueError("TCA intent_id, cycle_id and symbol are required")
        if operation not in {"ENTRY", "EXIT"}:
            raise ValueError("TCA operation must be ENTRY or EXIT")
        decision_time = _canonical_iso(intent.decision_time, "decision_time")
        queue_time = _canonical_iso(intent.queue_time, "queue_time")
        send_time = _canonical_iso(intent.send_time, "send_time")
        ordered_times = [value for value in (decision_time, queue_time, send_time) if value]
        if ordered_times != sorted(ordered_times):
            raise ValueError("TCA decision/queue/send times must be causal")
        requested = _decimal_text(
            intent.requested_common_quantity,
            "requested_common_quantity",
            non_negative=True,
        )
        submitted = _decimal_text(
            intent.submitted_common_quantity,
            "submitted_common_quantity",
            non_negative=True,
        )
        reference = _decimal_text(
            intent.reference_price,
            "reference_price",
            non_negative=True,
        )
        status = str(intent.status or "QUEUED").strip().upper()
        metadata_json = json.dumps(
            dict(intent.metadata), sort_keys=True, separators=(",", ":"), default=str
        )
        now = _now()
        baseline_complete = bool(send_time and reference and Decimal(reference) > 0)
        immutable_identity = (
            cycle_id,
            str(intent.decision_id or "").strip(),
            symbol,
            operation,
        )
        existing = self.conn.execute(
            """
            SELECT cycle_id, decision_id, symbol, operation
            FROM execution_tca_intents WHERE intent_id = ?
            """,
            (intent_id,),
        ).fetchone()
        inserted = existing is None
        if existing is not None and tuple(existing) != immutable_identity:
            raise ValueError(f"TCA intent identity collision: {intent_id}")

        savepoint = "record_execution_tca"
        self.conn.execute(f"SAVEPOINT {savepoint}")
        try:
            if inserted:
                self.conn.execute(
                    """
                    INSERT INTO execution_tca_intents (
                        intent_id, cycle_id, decision_id, symbol, operation,
                        decision_time, queue_time, send_time,
                        requested_common_quantity, submitted_common_quantity,
                        unhedged_notional_ms, last_hedge_observation_time,
                        last_spot_gross_quantity, last_perp_gross_quantity,
                        last_reference_price, partial, emergency, status,
                        metadata_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        intent_id,
                        *immutable_identity,
                        decision_time,
                        queue_time,
                        send_time,
                        requested,
                        submitted,
                        "0" if baseline_complete else None,
                        send_time if baseline_complete else None,
                        "0" if baseline_complete else None,
                        "0" if baseline_complete else None,
                        reference if baseline_complete else None,
                        None if intent.partial is None else int(intent.partial),
                        None if intent.emergency is None else int(intent.emergency),
                        status,
                        metadata_json,
                        now,
                        now,
                    ),
                )

            normalized_legs = tuple(legs)
            if not normalized_legs:
                raise ValueError("TCA requires at least one leg")
            seen_leg_ids: set[str] = set()
            for leg in normalized_legs:
                if str(leg.intent_id).strip() != intent_id:
                    raise ValueError("TCA leg intent_id does not match pair intent_id")
                leg_id = str(leg.leg_id or "").strip()
                market = str(leg.market or "").strip().lower()
                side = str(leg.side or "").strip().upper()
                if not leg_id or leg_id in seen_leg_ids:
                    raise ValueError("TCA leg_id must be unique and non-empty")
                seen_leg_ids.add(leg_id)
                if market not in {"spot", "perp"} or side not in {"BUY", "SELL"}:
                    raise ValueError("TCA leg market/side is invalid")
                leg_identity = self.conn.execute(
                    """
                    SELECT market, side FROM execution_tca_legs
                    WHERE intent_id = ? AND leg_id = ?
                    """,
                    (intent_id, leg_id),
                ).fetchone()
                if leg_identity is not None:
                    if tuple(leg_identity) != (market, side):
                        raise ValueError(f"TCA leg identity collision: {intent_id}:{leg_id}")
                    continue
                decimal_values = {
                    name: _decimal_text(value, name, non_negative=True)
                    for name, value in {
                        "decision_bid": leg.decision_bid,
                        "decision_ask": leg.decision_ask,
                        "decision_mid": leg.decision_mid,
                        "decision_limit": leg.decision_limit,
                        "send_bid": leg.send_bid,
                        "send_ask": leg.send_ask,
                        "send_mid": leg.send_mid,
                        "send_limit": leg.send_limit,
                        "requested_quantity": leg.requested_quantity,
                        "submitted_quantity": leg.submitted_quantity,
                    }.items()
                }
                self.conn.execute(
                    """
                    INSERT INTO execution_tca_legs (
                        intent_id, leg_id, market, side, route,
                        decision_time, queue_time, send_time,
                        decision_bid, decision_ask, decision_mid, decision_limit,
                        send_bid, send_ask, send_mid, send_limit,
                        requested_quantity, submitted_quantity,
                        partial, emergency, status, metadata_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        intent_id,
                        leg_id,
                        market,
                        side,
                        str(leg.route or "UNKNOWN").strip() or "UNKNOWN",
                        decision_time,
                        queue_time,
                        send_time,
                        decimal_values["decision_bid"],
                        decimal_values["decision_ask"],
                        decimal_values["decision_mid"],
                        decimal_values["decision_limit"],
                        decimal_values["send_bid"],
                        decimal_values["send_ask"],
                        decimal_values["send_mid"],
                        decimal_values["send_limit"],
                        decimal_values["requested_quantity"],
                        decimal_values["submitted_quantity"],
                        None if leg.partial is None else int(leg.partial),
                        None if leg.emergency is None else int(leg.emergency),
                        str(leg.status or status).strip().upper(),
                        json.dumps(
                            dict(leg.metadata),
                            sort_keys=True,
                            separators=(",", ":"),
                            default=str,
                        ),
                        now,
                        now,
                    ),
                )
            self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            self.conn.commit()
            return inserted
        except Exception:
            self.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise

    def record_execution_tca_ack(
        self,
        intent_id: str,
        *,
        ack_time: str,
        status: str = "ACKNOWLEDGED",
    ) -> bool:
        """Stamp the pair and both legs from one durable Rust IntentAck."""

        normalized_time = _canonical_iso(ack_time, "ack_time", required=True)
        normalized_status = str(status or "ACKNOWLEDGED").strip().upper()
        row = self.conn.execute(
            "SELECT send_time, ack_time FROM execution_tca_intents WHERE intent_id = ?",
            (str(intent_id),),
        ).fetchone()
        if row is None:
            return False
        if row["send_time"] and normalized_time and normalized_time < str(row["send_time"]):
            raise ValueError("TCA ACK precedes send time")
        if row["ack_time"] and str(row["ack_time"]) != normalized_time:
            # Replayed ACKs must preserve the first exchange acknowledgement.
            return True
        now = _now()
        self.conn.execute(
            """
            UPDATE execution_tca_intents
            SET ack_time = COALESCE(ack_time, ?), status = ?, updated_at = ?
            WHERE intent_id = ?
            """,
            (normalized_time, normalized_status, now, str(intent_id)),
        )
        self.conn.execute(
            """
            UPDATE execution_tca_legs
            SET ack_time = COALESCE(ack_time, ?), status = ?, updated_at = ?
            WHERE intent_id = ? AND terminal_time IS NULL
            """,
            (normalized_time, normalized_status, now, str(intent_id)),
        )
        self.conn.commit()
        return True

    def record_execution_tca_fill(
        self,
        *,
        intent_id: str,
        leg_id: str = "",
        market: str,
        event_time: str,
        status: str,
        incremental_quantity: Decimal | int | float | str | None = None,
        cumulative_quantity: Decimal | int | float | str | None = None,
        net_quantity: Decimal | int | float | str | None = None,
        fill_price: Decimal | int | float | str | None = None,
        average_fill_price: Decimal | int | float | str | None = None,
        cumulative_quote_quantity: Decimal | int | float | str | None = None,
        commission: Decimal | int | float | str | None = None,
        commission_asset: str = "",
        maker: bool | None = None,
        reference_price: Decimal | int | float | str | None = None,
        emergency: bool | None = None,
    ) -> bool:
        """Fold one exchange fill into exact leg attribution and hedge exposure."""

        normalized_intent = str(intent_id or "").strip()
        normalized_market = str(market or "").strip().lower()
        normalized_leg = str(leg_id or "").strip()
        observed_at = _canonical_iso(event_time, "event_time", required=True)
        if normalized_market not in {"spot", "perp"}:
            return False
        row = None
        if normalized_leg:
            row = self.conn.execute(
                "SELECT * FROM execution_tca_legs WHERE intent_id = ? AND leg_id = ?",
                (normalized_intent, normalized_leg),
            ).fetchone()
        if row is None:
            row = self.conn.execute(
                "SELECT * FROM execution_tca_legs WHERE intent_id = ? AND market = ?",
                (normalized_intent, normalized_market),
            ).fetchone()
        if row is None:
            return False
        normalized_leg = str(row["leg_id"])
        incremental_text = _decimal_text(
            incremental_quantity, "incremental_quantity", non_negative=True
        )
        cumulative_text = _decimal_text(
            cumulative_quantity, "cumulative_quantity", non_negative=True
        )
        net_text = _decimal_text(net_quantity, "net_quantity", non_negative=True)
        fill_text = _decimal_text(fill_price, "fill_price", non_negative=True)
        average_text = _decimal_text(
            average_fill_price, "average_fill_price", non_negative=True
        )
        quote_text = _decimal_text(
            cumulative_quote_quantity,
            "cumulative_quote_quantity",
            non_negative=True,
        )
        reference_text = _decimal_text(
            reference_price, "reference_price", non_negative=True
        )
        prior_gross = (
            Decimal(str(row["gross_filled_quantity"]))
            if row["gross_filled_quantity"] is not None
            else None
        )
        if cumulative_text is not None:
            gross = Decimal(cumulative_text)
        elif incremental_text is not None:
            gross = (prior_gross or Decimal("0")) + Decimal(incremental_text)
        else:
            gross = prior_gross
        if prior_gross is not None and gross is not None and gross < prior_gross:
            raise ValueError("TCA cumulative fill quantity regressed")

        vwap: Decimal | None = None
        if gross is not None and gross > 0 and quote_text is not None:
            vwap = Decimal(quote_text) / gross
        elif average_text is not None and Decimal(average_text) > 0:
            vwap = Decimal(average_text)
        elif (
            gross is not None
            and gross > 0
            and fill_text is not None
            and incremental_text is not None
            and Decimal(incremental_text) > 0
        ):
            prior_vwap = (
                Decimal(str(row["vwap"])) if row["vwap"] is not None else Decimal(fill_text)
            )
            prior_quantity = prior_gross or Decimal("0")
            vwap = (
                prior_vwap * prior_quantity
                + Decimal(fill_text) * Decimal(incremental_text)
            ) / gross
        elif row["vwap"] is not None:
            vwap = Decimal(str(row["vwap"]))

        commissions = json.loads(str(row["commissions_json"] or "{}"))
        commission_text = _decimal_text(commission, "commission", non_negative=True)
        asset = str(commission_asset or "").strip().upper()
        if commission_text is not None and asset:
            commissions[asset] = format(
                Decimal(str(commissions.get(asset, "0"))) + Decimal(commission_text),
                "f",
            )
        maker_status = str(row["maker_status"] or "UNKNOWN").upper()
        if maker is not None:
            observed_maker = "MAKER" if maker else "TAKER"
            maker_status = (
                observed_maker
                if maker_status in {"", "UNKNOWN", observed_maker}
                else "MIXED"
            )

        normalized_status = str(status or "").strip().upper() or "UNKNOWN"
        submitted = (
            Decimal(str(row["submitted_quantity"]))
            if row["submitted_quantity"] is not None
            else None
        )
        partial = normalized_status == "PARTIALLY_FILLED"
        if gross is not None and submitted is not None:
            partial = partial or gross < submitted
        terminal_statuses = {
            "FILLED",
            "CANCELED",
            "CANCELLED",
            "EXPIRED",
            "REJECTED",
            "FAILED",
            "RECONCILIATION_REQUIRED",
        }
        is_cancel = normalized_status in {"CANCELED", "CANCELLED", "EXPIRED"}
        is_terminal = normalized_status in terminal_statuses

        pair = self.conn.execute(
            "SELECT * FROM execution_tca_intents WHERE intent_id = ?",
            (normalized_intent,),
        ).fetchone()
        if pair is None:
            return False
        prior_observation = _parse_iso(pair["last_hedge_observation_time"])
        current_observation = _parse_iso(observed_at)
        if prior_observation is not None and current_observation is not None:
            elapsed = current_observation - prior_observation
            elapsed_ms = (
                Decimal(elapsed.days * 86_400 + elapsed.seconds)
                * Decimal("1000")
                + Decimal(elapsed.microseconds) / Decimal("1000")
            )
            if elapsed_ms < 0:
                raise ValueError("TCA fill observation is not causal")
            prior_spot = (
                Decimal(str(pair["last_spot_gross_quantity"]))
                if pair["last_spot_gross_quantity"] is not None
                else None
            )
            prior_perp = (
                Decimal(str(pair["last_perp_gross_quantity"]))
                if pair["last_perp_gross_quantity"] is not None
                else None
            )
            prior_reference = (
                Decimal(str(pair["last_reference_price"]))
                if pair["last_reference_price"] is not None
                else None
            )
            prior_integral = (
                Decimal(str(pair["unhedged_notional_ms"]))
                if pair["unhedged_notional_ms"] is not None
                else None
            )
            if None not in (prior_spot, prior_perp, prior_reference, prior_integral):
                assert prior_spot is not None
                assert prior_perp is not None
                assert prior_reference is not None
                assert prior_integral is not None
                hedge_integral: Decimal | None = prior_integral + (
                    abs(prior_spot - prior_perp) * prior_reference * elapsed_ms
                )
            else:
                hedge_integral = None
        else:
            hedge_integral = None

        current_reference = (
            Decimal(reference_text)
            if reference_text is not None and Decimal(reference_text) > 0
            else Decimal(fill_text)
            if fill_text is not None and Decimal(fill_text) > 0
            else Decimal(str(pair["last_reference_price"]))
            if pair["last_reference_price"] is not None
            else None
        )
        spot_quantity = pair["last_spot_gross_quantity"]
        perp_quantity = pair["last_perp_gross_quantity"]
        if gross is not None:
            if normalized_market == "spot":
                spot_quantity = format(gross, "f")
            else:
                perp_quantity = format(gross, "f")

        first_fill = str(row["first_fill_time"] or "") or (
            observed_at if gross is not None and gross > 0 else None
        )
        last_fill = (
            observed_at if gross is not None and gross > 0 else row["last_fill_time"]
        )
        now = _now()
        savepoint = "record_execution_tca_fill"
        self.conn.execute(f"SAVEPOINT {savepoint}")
        try:
            self.conn.execute(
                """
                UPDATE execution_tca_legs SET
                    first_fill_time = COALESCE(first_fill_time, ?),
                    last_fill_time = ?,
                    cancel_time = CASE WHEN ? THEN COALESCE(cancel_time, ?) ELSE cancel_time END,
                    terminal_time = CASE WHEN ? THEN COALESCE(terminal_time, ?) ELSE terminal_time END,
                    gross_filled_quantity = ?,
                    net_filled_quantity = COALESCE(?, net_filled_quantity),
                    vwap = ?, commissions_json = ?, maker_status = ?,
                    partial = ?, emergency = COALESCE(?, emergency), status = ?, updated_at = ?
                WHERE intent_id = ? AND leg_id = ?
                """,
                (
                    first_fill,
                    last_fill,
                    int(is_cancel),
                    observed_at,
                    int(is_terminal),
                    observed_at,
                    None if gross is None else format(gross, "f"),
                    net_text,
                    None if vwap is None else format(vwap, "f"),
                    json.dumps(commissions, sort_keys=True, separators=(",", ":")),
                    maker_status,
                    int(partial),
                    None if emergency is None else int(emergency),
                    normalized_status,
                    now,
                    normalized_intent,
                    normalized_leg,
                ),
            )
            self.conn.execute(
                """
                UPDATE execution_tca_intents SET
                    first_fill_time = COALESCE(first_fill_time, ?),
                    last_fill_time = ?,
                    unhedged_notional_ms = ?,
                    last_hedge_observation_time = ?,
                    last_spot_gross_quantity = ?,
                    last_perp_gross_quantity = ?,
                    last_reference_price = ?,
                    partial = CASE WHEN COALESCE(partial, 0) = 1 OR ? = 1 THEN 1 ELSE partial END,
                    emergency = COALESCE(?, emergency),
                    updated_at = ?
                WHERE intent_id = ?
                """,
                (
                    first_fill,
                    last_fill,
                    None if hedge_integral is None else format(hedge_integral, "f"),
                    observed_at,
                    spot_quantity,
                    perp_quantity,
                    None if current_reference is None else format(current_reference, "f"),
                    int(partial),
                    None if emergency is None else int(emergency),
                    now,
                    normalized_intent,
                ),
            )
            self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            self.conn.commit()
            return True
        except Exception:
            self.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise

    def record_execution_tca_terminal(
        self,
        intent_id: str,
        *,
        terminal_time: str,
        status: str,
        cancel: bool = False,
        partial: bool | None = None,
        emergency: bool | None = None,
    ) -> bool:
        """Stamp the shared terminal state without fabricating absent fills."""

        normalized_intent = str(intent_id or "").strip()
        observed_at = _canonical_iso(terminal_time, "terminal_time", required=True)
        row = self.conn.execute(
            "SELECT * FROM execution_tca_intents WHERE intent_id = ?",
            (normalized_intent,),
        ).fetchone()
        if row is None:
            return False
        normalized_status = str(status or "UNKNOWN").strip().upper()
        hedge_integral = (
            Decimal(str(row["unhedged_notional_ms"]))
            if row["unhedged_notional_ms"] is not None
            else None
        )
        last_observation = _parse_iso(row["last_hedge_observation_time"])
        terminal_observation = _parse_iso(observed_at)
        spot_quantity = (
            Decimal(str(row["last_spot_gross_quantity"]))
            if row["last_spot_gross_quantity"] is not None
            else None
        )
        perp_quantity = (
            Decimal(str(row["last_perp_gross_quantity"]))
            if row["last_perp_gross_quantity"] is not None
            else None
        )
        reference_price = (
            Decimal(str(row["last_reference_price"]))
            if row["last_reference_price"] is not None
            else None
        )
        if last_observation is not None and terminal_observation is not None:
            elapsed = terminal_observation - last_observation
            elapsed_ms = (
                Decimal(elapsed.days * 86_400 + elapsed.seconds)
                * Decimal("1000")
                + Decimal(elapsed.microseconds) / Decimal("1000")
            )
            if elapsed_ms < 0:
                raise ValueError("TCA terminal observation is not causal")
            if None not in (
                hedge_integral,
                spot_quantity,
                perp_quantity,
                reference_price,
            ):
                assert hedge_integral is not None
                assert spot_quantity is not None
                assert perp_quantity is not None
                assert reference_price is not None
                hedge_integral += (
                    abs(spot_quantity - perp_quantity)
                    * reference_price
                    * elapsed_ms
                )
        now = _now()
        self.conn.execute(
            """
            UPDATE execution_tca_intents SET
                cancel_time = CASE WHEN ? THEN COALESCE(cancel_time, ?) ELSE cancel_time END,
                terminal_time = COALESCE(terminal_time, ?),
                unhedged_notional_ms = ?,
                last_hedge_observation_time = CASE
                    WHEN last_hedge_observation_time IS NULL THEN NULL ELSE ? END,
                partial = COALESCE(?, partial), emergency = COALESCE(?, emergency),
                status = ?, updated_at = ?
            WHERE intent_id = ?
            """,
            (
                int(cancel),
                observed_at,
                observed_at,
                None if hedge_integral is None else format(hedge_integral, "f"),
                observed_at,
                None if partial is None else int(partial),
                None if emergency is None else int(emergency),
                normalized_status,
                now,
                normalized_intent,
            ),
        )
        self.conn.execute(
            """
            UPDATE execution_tca_legs SET
                cancel_time = CASE WHEN ? THEN COALESCE(cancel_time, ?) ELSE cancel_time END,
                terminal_time = COALESCE(terminal_time, ?),
                partial = COALESCE(?, partial), emergency = COALESCE(?, emergency),
                status = CASE WHEN status = 'SKIPPED' THEN status ELSE ? END,
                updated_at = ?
            WHERE intent_id = ?
            """,
            (
                int(cancel),
                observed_at,
                observed_at,
                None if partial is None else int(partial),
                None if emergency is None else int(emergency),
                normalized_status,
                now,
                normalized_intent,
            ),
        )
        self.conn.commit()
        return True

    def record_execution_tca_markout(
        self,
        *,
        intent_id: str,
        leg_id: str,
        horizon: str,
        observed_at: str,
        reference_mid: Decimal | int | float | str | None,
        mark_mid: Decimal | int | float | str | None,
        markout_bps: Decimal | int | float | str | None,
        status: str = "MEASURED",
    ) -> bool:
        normalized_horizon = str(horizon or "").strip().lower()
        if normalized_horizon not in TCA_MARKOUT_HORIZONS:
            raise ValueError(f"unsupported TCA markout horizon {normalized_horizon!r}")
        row = self.conn.execute(
            """
            SELECT markouts_json FROM execution_tca_legs
            WHERE intent_id = ? AND leg_id = ?
            """,
            (str(intent_id), str(leg_id)),
        ).fetchone()
        if row is None:
            return False
        markouts = json.loads(str(row["markouts_json"] or "{}"))
        measurement = {
            "status": str(status or "UNKNOWN").strip().upper(),
            "observed_at": _canonical_iso(observed_at, "observed_at", required=True),
            "reference_mid": _decimal_text(reference_mid, "reference_mid", non_negative=True),
            "mark_mid": _decimal_text(mark_mid, "mark_mid", non_negative=True),
            "markout_bps": _decimal_text(markout_bps, "markout_bps"),
        }
        existing = markouts.get(normalized_horizon)
        if existing is not None and existing != measurement:
            raise ValueError(
                f"TCA markout collision: {intent_id}:{leg_id}:{normalized_horizon}"
            )
        markouts[normalized_horizon] = measurement
        self.conn.execute(
            """
            UPDATE execution_tca_legs
            SET markouts_json = ?, updated_at = ?
            WHERE intent_id = ? AND leg_id = ?
            """,
            (
                json.dumps(markouts, sort_keys=True, separators=(",", ":")),
                _now(),
                str(intent_id),
                str(leg_id),
            ),
        )
        self.conn.commit()
        return True

    def record_opportunity_funnel_event(self, event: OpportunityFunnelEvent) -> bool:
        """Append one idempotent funnel stage and preserve its denominator."""

        stage = str(event.stage or "").strip().lower()
        if stage not in OPPORTUNITY_FUNNEL_STAGES:
            raise ValueError(f"unsupported opportunity funnel stage {stage!r}")
        scope = str(event.scope or "CYCLE").strip().upper()
        if scope not in {"CYCLE", "INTENT"}:
            raise ValueError("opportunity funnel scope must be CYCLE or INTENT")
        cycle_id = str(event.cycle_id or "").strip()
        symbol = str(event.symbol or "*").strip().upper()
        intent_id = str(event.intent_id or "").strip()
        if not cycle_id or not symbol or (scope == "INTENT" and not intent_id):
            raise ValueError("opportunity funnel identity is incomplete")
        numerator = int(event.numerator_count)
        denominator = int(event.denominator_count)
        if numerator < 0 or denominator < 0 or numerator > denominator:
            raise ValueError("funnel counts require 0 <= numerator <= denominator")
        reached = (
            bool(event.reached)
            if event.reached is not None
            else numerator > 0
            if denominator > 0
            else None
        )
        event_time = _canonical_iso(event.event_time, "event_time", required=True)
        metadata_json = json.dumps(
            dict(event.metadata), sort_keys=True, separators=(",", ":"), default=str
        )
        identity = {
            "cycle_id": cycle_id,
            "scope": scope,
            "symbol": symbol,
            "intent_id": intent_id,
            "stage": stage,
        }
        event_key = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        content = {
            **identity,
            "stage_ordinal": OPPORTUNITY_FUNNEL_STAGES.index(stage),
            "reached": reached,
            "numerator_count": numerator,
            "denominator_count": denominator,
            "reason": str(event.reason or ""),
            "event_time": event_time,
            "metadata_json": metadata_json,
        }
        content_hash = hashlib.sha256(
            json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        existing = self.conn.execute(
            "SELECT content_hash FROM opportunity_funnel_events WHERE event_key = ?",
            (event_key,),
        ).fetchone()
        if existing is not None:
            if str(existing["content_hash"]) != content_hash:
                raise ValueError(f"opportunity funnel event collision: {event_key}")
            return False
        self.conn.execute(
            """
            INSERT INTO opportunity_funnel_events (
                event_key, cycle_id, scope, symbol, intent_id, stage,
                stage_ordinal, reached, numerator_count, denominator_count,
                reason, event_time, content_hash, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_key,
                cycle_id,
                scope,
                symbol,
                intent_id,
                stage,
                content["stage_ordinal"],
                None if reached is None else int(reached),
                numerator,
                denominator,
                content["reason"],
                event_time,
                content_hash,
                metadata_json,
            ),
        )
        self.conn.commit()
        return True

    def record_execution_decision(
        self,
        *,
        decision_id: str,
        cycle_id: str,
        symbol: str,
        direction: str,
        action: str,
        accepted: bool,
        config_version_hash: str,
        model_version: str,
        payload: Mapping[str, Any],
    ) -> bool:
        """Persist an immutable, versioned decision before execution dispatch."""

        normalized = {
            "decision_id": str(decision_id).strip(),
            "cycle_id": str(cycle_id).strip(),
            "symbol": str(symbol).strip().upper(),
            "direction": str(direction).strip().lower(),
            "action": str(action).strip().upper(),
            "accepted": bool(accepted),
            "config_version_hash": str(config_version_hash).strip(),
            "model_version": str(model_version).strip(),
            "payload": dict(payload),
        }
        required = (
            "decision_id",
            "cycle_id",
            "symbol",
            "direction",
            "action",
            "config_version_hash",
            "model_version",
        )
        if any(not normalized[key] for key in required):
            raise ValueError("execution decision identity/version fields are required")
        decision_payload = _json_dump(normalized)
        decision_hash = hashlib.sha256(decision_payload.encode("utf-8")).hexdigest()
        existing = self.conn.execute(
            "SELECT decision_hash FROM execution_decisions WHERE decision_id = ?",
            (normalized["decision_id"],),
        ).fetchone()
        if existing is not None:
            if str(existing["decision_hash"]) != decision_hash:
                raise ValueError(
                    f"execution decision identity collision: {normalized['decision_id']}"
                )
            return False
        self.conn.execute(
            """
            INSERT INTO execution_decisions
                (decision_id, cycle_id, symbol, direction, action, accepted,
                 config_version_hash, model_version, decision_hash,
                 decision_payload, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized["decision_id"],
                normalized["cycle_id"],
                normalized["symbol"],
                normalized["direction"],
                normalized["action"],
                int(normalized["accepted"]),
                normalized["config_version_hash"],
                normalized["model_version"],
                decision_hash,
                decision_payload,
                _now(),
            ),
        )
        self.conn.commit()
        return True

    def record_shadow_decision(self, decision: ShadowDecision) -> None:
        self.conn.execute(
            """
            INSERT INTO model_shadow_decisions
                (decision_time, trade_id, symbol, action, hold_score, exit_score,
                 incremental_value_usd, recommended, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision.decision_time or _now(),
                decision.trade_id,
                decision.symbol,
                decision.action,
                decision.hold_score,
                decision.exit_score,
                decision.incremental_value_usd,
                int(decision.recommended),
                _json_dump(decision.metadata),
            ),
        )
        # Caller (run_cycle) is responsible for the final flush().

    def record_parameter_promotion(self, promotion: ParameterPromotion) -> None:
        self.conn.execute(
            """
            INSERT INTO parameter_promotions
                (promoted_at, status, source, validation_snapshot_time, rollback_reason,
                 params_json, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                promotion.promoted_at or _now(),
                promotion.status,
                promotion.source,
                promotion.validation_snapshot_time,
                promotion.rollback_reason,
                _json_dump(promotion.params),
                _json_dump(promotion.metadata),
            ),
        )
        self.conn.commit()

    def record_validation_snapshot(self, snapshot: ValidationSnapshot | None = None, **kwargs: Any) -> None:
        if snapshot is None:
            snapshot = ValidationSnapshot(
                phase=str(kwargs.get("phase", "validation")),
                validation_status=str(kwargs.get("validation_status", "")),
                go_no_go=str(kwargs.get("go_no_go", "")),
                observation_days=float(kwargs.get("observation_days", 0.0)),
                trade_count=int(kwargs.get("trade_count", 0)),
                blockers=list(kwargs.get("blockers", [])),
                metrics=dict(kwargs.get("metrics", {})),
                snapshot_time=kwargs.get("snapshot_time"),
            )
        self.conn.execute(
            """
            INSERT INTO validation_snapshots
                (snapshot_time, phase, validation_status, go_no_go,
                 observation_days, trade_count, blockers, metrics_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_time) DO UPDATE SET
                phase=excluded.phase,
                validation_status=excluded.validation_status,
                go_no_go=excluded.go_no_go,
                observation_days=excluded.observation_days,
                trade_count=excluded.trade_count,
                blockers=excluded.blockers,
                metrics_json=excluded.metrics_json
            """,
            (
                snapshot.snapshot_time or _now(),
                snapshot.phase,
                snapshot.validation_status,
                snapshot.go_no_go,
                snapshot.observation_days,
                snapshot.trade_count,
                _json_dump(snapshot.blockers),
                _json_dump(snapshot.metrics),
            ),
        )
        self.conn.commit()

    def _insert_execution_event(self, payload: dict[str, Any]) -> None:
        context = self._runtime_context()
        event_time = payload.get("event_time")
        if not event_time and payload.get("event_time_ms") is not None:
            try:
                event_time = datetime.fromtimestamp(
                    int(float(payload["event_time_ms"])) / 1000.0,
                    tz=timezone.utc,
                ).isoformat()
            except (TypeError, ValueError):
                event_time = None
        self.conn.execute(
            """
            INSERT INTO execution_events
                (symbol, client_order_id, status, filled_qty, cumulative_filled_qty,
                 avg_fill_price, last_fill_price,
                 cumulative_quote_qty, commission, commission_asset, realized_pnl,
                 maker, execution_type, market, side, order_id, trade_id,
                 account_id, environment, strategy_id, cycle_id, intent_id, leg_id,
                 config_version_hash, telemetry_schema_version, telemetry_sequence,
                 event_name, asset, amount, reason,
                 trading_mode, runtime_mode, session_id, event_time, raw_payload)
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(telemetry_sequence) WHERE telemetry_sequence IS NOT NULL
            DO NOTHING
            """,
            (
                str(payload.get("symbol", "")),
                str(payload.get("client_order_id", payload.get("clientOrderId", ""))),
                str(payload.get("status", "")),
                float(payload.get("filled_qty", payload.get("filledQty", 0.0)) or 0.0),
                payload.get("cumulative_filled_qty"),
                payload.get("avg_fill_price"),
                payload.get("last_fill_price"),
                payload.get("cumulative_quote_qty"),
                payload.get("commission"),
                payload.get("commission_asset"),
                payload.get("realized_pnl"),
                payload.get("maker"),
                payload.get("execution_type"),
                payload.get("market"),
                payload.get("side"),
                None if payload.get("order_id") is None else str(payload.get("order_id")),
                None if payload.get("trade_id") is None else str(payload.get("trade_id")),
                payload.get("account_id"),
                payload.get("environment"),
                payload.get("strategy_id"),
                payload.get("cycle_id"),
                payload.get("intent_id"),
                payload.get("leg_id"),
                payload.get("config_version_hash"),
                payload.get("telemetry_schema_version"),
                payload.get("telemetry_sequence"),
                str(payload.get("event_name", "OrderUpdate")),
                payload.get("asset"),
                payload.get("amount"),
                str(payload.get("reason", "")),
                str(payload.get("trading_mode", context["trading_mode"]) or "").lower(),
                str(payload.get("runtime_mode", context["runtime_mode"]) or "").upper(),
                str(payload.get("session_id", context["session_id"]) or ""),
                str(event_time or _now()),
                _json_dump(payload),
            ),
        )

    @staticmethod
    def _durable_telemetry_event_hash(event: Mapping[str, Any]) -> str:
        canonical_event = dict(event)
        # Rust changes only this transport annotation when replaying an
        # otherwise identical durable record.  It is not part of event
        # identity and must not turn an ACK-loss replay into a conflict.
        canonical_event.pop("telemetry_replay", None)
        encoded = json.dumps(
            canonical_event,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def _durable_telemetry_publication_hash(cls, event: Mapping[str, Any]) -> str:
        return cls._durable_telemetry_event_hash({
            key: value for key, value in event.items()
            if key not in {
                "telemetry_sequence", "telemetry_schema_version",
                "telemetry_ack_required", "telemetry_replay",
                "terminal_sequence", "terminal_watermark",
            }
        })

    @staticmethod
    def _encode_telemetry_publication(event_hash: str, status: str) -> str:
        return json.dumps(
            {"event_hash": event_hash, "status": status},
            sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        )

    def _load_telemetry_publication(self, publication_id: str) -> tuple[str, str] | None:
        # Keep replay identities in the established restart-state metadata
        # table. A new physical table would invalidate existing split-store
        # activation identities and verified migration manifests.
        row = self._telemetry_conn.execute(
            "SELECT value FROM schema_meta WHERE key=?",
            (TELEMETRY_PUBLICATION_META_PREFIX + publication_id,),
        ).fetchone()
        if row is None:
            return None
        raw_value = str(row[0])
        try:
            value = json.loads(raw_value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid durable telemetry publication metadata: {publication_id}") from exc
        if not isinstance(value, dict) or set(value) != {"event_hash", "status"}:
            raise ValueError(f"invalid durable telemetry publication metadata: {publication_id}")
        event_hash, status = value["event_hash"], value["status"]
        if (
            not isinstance(event_hash, str)
            or len(event_hash) != 64
            or any(char not in "0123456789abcdef" for char in event_hash)
            or status not in ("PROCESSING", "PROCESSED")
            or raw_value != self._encode_telemetry_publication(event_hash, status)
        ):
            raise ValueError(f"invalid durable telemetry publication metadata: {publication_id}")
        return event_hash, status

    def begin_durable_telemetry(
        self,
        *,
        sequence: int,
        schema_version: int,
        event: Mapping[str, Any],
    ) -> bool:
        """Claim a replayable delivery, returning false once fully processed.

        A PROCESSING receipt is deliberately retried after a crash.  Only a
        PROCESSED receipt suppresses the business callback and permits a fresh
        ACK, closing the process-restart replay gap without treating receipt
        insertion itself as proof that lifecycle effects committed.
        """

        normalized_sequence = int(sequence)
        normalized_schema = int(schema_version)
        if normalized_sequence <= 0 or normalized_schema <= 0:
            raise ValueError("durable telemetry sequence/schema must be positive")
        stored_event = dict(event)
        stored_event.setdefault("telemetry_sequence", normalized_sequence)
        stored_event.setdefault("telemetry_schema_version", normalized_schema)
        stored_event.setdefault("telemetry_ack_required", True)
        event_hash = self._durable_telemetry_event_hash(stored_event)
        raw_payload = json.dumps(
            stored_event,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        with self._telemetry_receipt_lock, self._telemetry_conn:
            row = self._telemetry_conn.execute(
                "SELECT schema_version, event_hash, status, raw_payload "
                "FROM telemetry_receipts "
                "WHERE telemetry_sequence = ?",
                (normalized_sequence,),
            ).fetchone()
            if row is not None and (
                int(row[0]) != normalized_schema
                or not secrets.compare_digest(str(row[1]), event_hash)
            ):
                raise ValueError(
                    f"durable telemetry identity conflict at sequence {normalized_sequence}"
                )
            publication_id = str(stored_event.get("publication_id") or "").strip()
            publication_processed = False
            if publication_id:
                # An engine outbox can replay the same publication through a
                # fresh relay sequence after a crash across its fsync handoff.
                publication_hash = self._durable_telemetry_publication_hash(stored_event)
                publication = self._load_telemetry_publication(publication_id)
                if publication is not None:
                    if not secrets.compare_digest(str(publication[0]), publication_hash):
                        raise ValueError(f"durable telemetry publication conflict: {publication_id}")
                    publication_processed = str(publication[1]) == "PROCESSED"
                else:
                    self._telemetry_conn.execute(
                        "INSERT INTO schema_meta (key, value) VALUES (?, ?)",
                        (
                            TELEMETRY_PUBLICATION_META_PREFIX + publication_id,
                            self._encode_telemetry_publication(publication_hash, "PROCESSING"),
                        ),
                    )
            if row is None:
                self._telemetry_conn.execute(
                    "INSERT INTO telemetry_receipts "
                    "(telemetry_sequence, schema_version, event_hash, status, "
                    "first_seen_at, raw_payload) "
                    "VALUES (?, ?, ?, 'PROCESSING', ?, ?)",
                    (
                        normalized_sequence,
                        normalized_schema,
                        event_hash,
                        _now(),
                        raw_payload,
                    ),
                )
                if publication_processed:
                    self._telemetry_conn.execute(
                        "UPDATE telemetry_receipts SET status='PROCESSED', processed_at=? WHERE telemetry_sequence=?",
                        (_now(), normalized_sequence),
                    )
                self._telemetry_conn.commit()
                return not publication_processed
            if str(row[2]).upper() != "PROCESSED" and str(row[3] or "") in {
                "",
                "{}",
            }:
                # Upgrade an old PROCESSING receipt in place.  A completed old
                # receipt never needs its raw payload for projection recovery.
                self._telemetry_conn.execute(
                    "UPDATE telemetry_receipts SET raw_payload=? "
                    "WHERE telemetry_sequence=? AND status='PROCESSING'",
                    (raw_payload, normalized_sequence),
                )
                self._telemetry_conn.commit()
            if publication_processed and str(row[2]).upper() != "PROCESSED":
                self._telemetry_conn.execute(
                    "UPDATE telemetry_receipts SET status='PROCESSED', processed_at=? WHERE telemetry_sequence=?",
                    (_now(), normalized_sequence),
                )
            self._telemetry_conn.commit()
            return not publication_processed and str(row[2]).upper() != "PROCESSED"

    def append_durable_telemetry_receipt(
        self,
        event: Mapping[str, Any],
    ) -> bool:
        """Commit one raw critical event before its transport ACK.

        The return value indicates whether the idempotent projection still has
        to run.  A fully processed replay is retained as identity evidence but
        does not repeat business effects.
        """

        sequence = event.get("telemetry_sequence")
        schema_version = event.get("telemetry_schema_version")
        if sequence is None or schema_version is None:
            raise ValueError("durable telemetry event lacks sequence/schema")
        return self.begin_durable_telemetry(
            sequence=int(sequence),
            schema_version=int(schema_version),
            event=event,
        )

    def pending_durable_telemetry_events(
        self,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Load committed raw receipts whose projection is not checkpointed."""

        parameters: tuple[int, ...] = ()
        limit_sql = ""
        if limit is not None:
            bounded_limit = max(1, min(int(limit), 100_000))
            limit_sql = " LIMIT ?"
            parameters = (bounded_limit,)
        with self._telemetry_receipt_lock:
            rows = self._telemetry_conn.execute(
                "SELECT telemetry_sequence, schema_version, raw_payload "
                "FROM telemetry_receipts WHERE status='PROCESSING' "
                "ORDER BY telemetry_sequence ASC" + limit_sql,
                parameters,
            ).fetchall()
        pending: list[dict[str, Any]] = []
        for row in rows:
            try:
                event = json.loads(str(row[2] or ""))
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError(
                    "durable telemetry receipt "
                    f"{int(row[0])} has invalid raw payload"
                ) from exc
            if not isinstance(event, dict) or not str(event.get("event") or ""):
                raise ValueError(
                    "durable telemetry receipt "
                    f"{int(row[0])} lacks a dispatchable raw event"
                )
            event["telemetry_sequence"] = int(row[0])
            event["telemetry_schema_version"] = int(row[1])
            event["telemetry_ack_required"] = True
            event["telemetry_replay"] = True
            pending.append(event)
        return pending

    def complete_durable_telemetry(self, sequence: int) -> None:
        normalized_sequence = int(sequence)
        with self._telemetry_receipt_lock, self._telemetry_conn:
            raw_row = self._telemetry_conn.execute(
                "SELECT raw_payload FROM telemetry_receipts WHERE telemetry_sequence=?",
                (normalized_sequence,),
            ).fetchone()
            publication_id = ""
            publication = None
            if raw_row is not None:
                raw_event = json.loads(str(raw_row[0] or "{}"))
                publication_id = str(raw_event.get("publication_id") or "").strip()
                if publication_id:
                    # Validate before either checkpoint is changed. Corrupt or
                    # missing identity evidence must never mark a receipt done.
                    publication = self._load_telemetry_publication(publication_id)
                    if publication is None:
                        raise ValueError(f"durable telemetry publication unavailable: {publication_id}")
                    if not secrets.compare_digest(
                        publication[0], self._durable_telemetry_publication_hash(raw_event)
                    ):
                        raise ValueError(f"durable telemetry publication conflict: {publication_id}")
            cursor = self._telemetry_conn.execute(
                "UPDATE telemetry_receipts SET status='PROCESSED', processed_at=? "
                "WHERE telemetry_sequence=? AND status='PROCESSING'",
                (_now(), normalized_sequence),
            )
            if cursor.rowcount != 1:
                row = self._telemetry_conn.execute(
                    "SELECT status FROM telemetry_receipts WHERE telemetry_sequence=?",
                    (normalized_sequence,),
                ).fetchone()
                if row is None or str(row[0]).upper() != "PROCESSED":
                    raise ValueError(
                        f"durable telemetry receipt {normalized_sequence} is unavailable"
                    )
            if publication is not None:
                self._telemetry_conn.execute(
                    "UPDATE schema_meta SET value=? WHERE key=?",
                    (
                        self._encode_telemetry_publication(publication[0], "PROCESSED"),
                        TELEMETRY_PUBLICATION_META_PREFIX + publication_id,
                    ),
                )
            self._telemetry_conn.commit()

    def record_execution_event(self, payload: dict[str, Any]) -> None:
        self._insert_execution_event(payload)
        self.conn.commit()

    def record_execution_and_economic_fill(
        self,
        payload: dict[str, Any],
        economic_fields: Mapping[str, Any],
    ) -> LedgerIngestionResult:
        """Atomically persist raw execution evidence and normalized economics.

        The raw execution row is retained for each distinct durable telemetry
        sequence; a replay of the same sequence is suppressed by its unique
        receipt.  A conflicting stable exchange identity rolls both writes
        back, so lifecycle code can never observe a telemetry fill without its
        economic counterpart.
        """

        return self.record_execution_and_economic_events(
            payload,
            build_fill_events(**dict(economic_fields)),
        )

    def record_execution_and_economic_funding(
        self,
        payload: dict[str, Any],
        economic_fields: Mapping[str, Any],
    ) -> LedgerIngestionResult:
        return self.record_execution_and_economic_events(
            payload,
            (
                build_cashflow_event(
                    event_type=FUNDING,
                    **dict(economic_fields),
                ),
            ),
        )

    def record_execution_and_economic_events(
        self,
        payload: dict[str, Any],
        events: Iterable[EconomicLedgerEvent],
    ) -> LedgerIngestionResult:
        savepoint = "execution_economic_dual_write"
        with self._economic_ledger_lock:
            self.conn.execute(f"SAVEPOINT {savepoint}")
            try:
                result = ingest_economic_events(self.conn, events)
                self._insert_execution_event(payload)
                self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                self.conn.commit()
                return result
            except Exception:
                self.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise

    def record_economic_events(
        self,
        events: Iterable[EconomicLedgerEvent],
    ) -> LedgerIngestionResult:
        """Append a normalized economic-event batch under one savepoint.

        Exact replay is idempotent.  Reusing a stable source identity with
        different normalized economics raises ``LedgerIdempotencyConflict``
        and rolls the complete batch back.  This method intentionally does not
        commit: callers can compose it with other cycle-batched writes and use
        :meth:`flush` for the durability boundary.  Dual-write and statement
        APIs that require immediate durability commit explicitly.
        """

        with self._economic_ledger_lock:
            return ingest_economic_events(self.conn, events)

    def record_economic_fill(self, **event_fields: Any) -> LedgerIngestionResult:
        """Append a fill and its optional commission in one transaction."""

        return self.record_economic_events(build_fill_events(**event_fields))

    def record_economic_commission(self, **event_fields: Any) -> LedgerIngestionResult:
        """Append a commission received separately from its fill payload."""

        return self.record_economic_events((build_commission_event(**event_fields),))

    def record_economic_funding(self, **event_fields: Any) -> LedgerIngestionResult:
        """Append one signed exchange funding cashflow."""

        event = build_cashflow_event(event_type=FUNDING, **event_fields)
        return self.record_economic_events((event,))

    def record_economic_realized_pnl(self, **event_fields: Any) -> LedgerIngestionResult:
        """Append one signed exchange-reported perpetual realized-PnL cashflow."""

        event = build_cashflow_event(event_type=REALIZED_PNL, **event_fields)
        return self.record_economic_events((event,))

    def record_economic_borrow_interest(self, **event_fields: Any) -> LedgerIngestionResult:
        """Append one borrow/interest charge (positive input, negative ledger effect)."""

        event = build_cashflow_event(event_type=BORROW_INTEREST, **event_fields)
        return self.record_economic_events((event,))

    def record_economic_balance_adjustment(self, **event_fields: Any) -> LedgerIngestionResult:
        """Append one signed deposit, withdrawal, transfer or correction."""

        event = build_cashflow_event(event_type=BALANCE_ADJUSTMENT, **event_fields)
        return self.record_economic_events((event,))

    def record_exchange_statement(
        self,
        statement: NormalizedExchangeStatement,
    ) -> ExchangeStatementIngestionResult:
        """Durably append statement evidence and its optional ledger cashflow.

        The journal row, normalized economic event, and monotonic source cursor
        share one SQLite transaction.  A content collision rolls all three
        back; an exact replay is a no-op apart from repairing a missing cursor.
        """

        with self._exchange_statement_lock:
            result = ingest_exchange_statement(self._statement_conn, statement)
            self._statement_conn.commit()
            return result

    def record_binance_futures_income_statement(
        self,
        payload: Mapping[str, Any],
        *,
        account_id: str,
        trading_mode: str,
        strategy_id: str,
        venue: str = "BINANCE",
        runtime_mode: str = "",
        session_id: str = "",
        availability_time: str = "",
        code_hash: str = "",
        config_hash: str = "",
        schema_hash: str = "",
        cycle_id: str = "",
        intent_id: str = "",
    ) -> ExchangeStatementIngestionResult:
        """Normalize and durably record one Binance futures-income row."""

        statement = normalize_binance_futures_income(
            payload,
            account_id=account_id,
            trading_mode=trading_mode,
            strategy_id=strategy_id,
            venue=venue,
            runtime_mode=runtime_mode,
            session_id=session_id,
        )
        with self._exchange_statement_lock:
            statement = _prepare_binance_futures_income_statement(
                self._statement_conn,
                statement,
                availability_time=availability_time,
                code_hash=code_hash,
                config_hash=config_hash,
                schema_hash=schema_hash,
                cycle_id=cycle_id,
                intent_id=intent_id,
            )
            return self.record_exchange_statement(statement)

    def record_binance_margin_interest_statement(
        self,
        payload: Mapping[str, Any],
        *,
        account_id: str,
        trading_mode: str,
        strategy_id: str,
        venue: str = "BINANCE",
        runtime_mode: str = "",
        session_id: str = "",
        availability_time: str = "",
        code_hash: str = "",
        config_hash: str = "",
        schema_hash: str = "",
    ) -> ExchangeStatementIngestionResult:
        """Normalize and durably record one Binance margin-interest row."""

        statement = normalize_binance_margin_interest(
            payload,
            account_id=account_id,
            trading_mode=trading_mode,
            strategy_id=strategy_id,
            venue=venue,
            runtime_mode=runtime_mode,
            session_id=session_id,
        )
        if statement.economic_event is not None:
            statement = replace(
                statement,
                economic_event=replace(
                    statement.economic_event,
                    availability_time=availability_time,
                    code_hash=code_hash,
                    config_hash=config_hash,
                    schema_hash=schema_hash,
                ),
            )
        return self.record_exchange_statement(statement)

    def record_health_sample(
        self,
        metric: str,
        value: float,
        expected_value: float = 0.0,
        symbol: str | None = None,
        zscore: float | None = None,
        alert_level: str = "",
        runtime_mode: str = "",
        notes: str = "",
        sample_time: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO health_samples
                (sample_time, symbol, metric, value, expected_value, zscore,
                 alert_level, runtime_mode, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (sample_time or _now(), symbol, metric, value, expected_value, zscore, alert_level, runtime_mode, notes),
        )
        self.conn.commit()
        # Caller (run_cycle) is responsible for the final flush().

    def upsert_market_sample(
        self,
        sample_minute: str,
        symbol: str,
        ann_funding: float,
        basis_pct: float,
        mark_price: float,
        minute_notional_volume: float = 0.0,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO market_samples
                (sample_minute, symbol, ann_funding, basis_pct, mark_price, minute_notional_volume)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, sample_minute) DO UPDATE SET
                ann_funding=excluded.ann_funding,
                basis_pct=excluded.basis_pct,
                mark_price=excluded.mark_price,
                minute_notional_volume=excluded.minute_notional_volume
            """,
            (sample_minute, symbol, ann_funding, basis_pct, mark_price, minute_notional_volume),
        )
        self.conn.commit()
        # Caller (run_cycle) is responsible for the final flush().

    def archive_old_data(
        self,
        *,
        archive_db_path: str | None = None,
        retention_days: int = 90,
        market_retention_days: int = 21,
        health_retention_days: int = 21,
        snapshot_retention_days: int | None = None,
        feature_retention_days: int | None = None,
        batch_size: int = 2_000,
        max_batches_per_table: int = 32,
        archive_max_bytes: int = 1_100_000_000,
    ) -> dict[str, int]:
        """Move expired rows in bounded, exactly verified batches.

        Copying and deleting are intentionally separate durability boundaries:
        a crash can leave a duplicate in the source, but can never lose the
        only copy.  Primary-key collisions are accepted only when every source
        column is identical.  Any schema drift, conflicting row, short delete,
        or archive-cap breach raises and leaves source rows intact.
        """

        if archive_db_path is None:
            db_path = self.conn.execute("PRAGMA database_list").fetchone()[2]
            archive_db_path = (
                AUDIT_DB_PATH
                if Path(db_path).resolve() == Path(DB_PATH).resolve()
                else str(Path(db_path).with_name("archive.db"))
            )

        batch_size = min(10_000, max(1_000, int(batch_size)))
        max_batches_per_table = max(1, int(max_batches_per_table))
        archive_path = Path(archive_db_path).resolve()

        # Snapshots and features can be very high-volume; allow shorter retention for main DB
        # if not explicitly provided, default to retention_days.
        snap_days = snapshot_retention_days if snapshot_retention_days is not None else retention_days
        feat_days = feature_retention_days if feature_retention_days is not None else retention_days

        archive_conn = _connect(
            str(archive_path),
            max_database_bytes=max(0, int(archive_max_bytes)),
        )
        now_ts = datetime.now(timezone.utc).timestamp()

        def _get_cutoff(days: int) -> str:
            return datetime.fromtimestamp(now_ts - (days * 86400), tz=timezone.utc).isoformat()

        # Table configuration: (table_name, time_column, days_to_keep, extra_where)
        configs = [
            ("trade_history", "exit_time", retention_days, ""),
            ("execution_events", "event_time", retention_days, ""),
            ("market_samples", "sample_minute", market_retention_days, ""),
            ("health_samples", "sample_time", health_retention_days, ""),
            ("candidate_snapshots", "snapshot_time", snap_days, ""),
            ("opportunity_scores", "score_time", snap_days, ""),
            ("feature_snapshots", "snapshot_time", feat_days, ""),
            ("execution_quality", "sample_time", retention_days, ""),
            (
                "execution_tca_legs",
                "updated_at",
                retention_days,
                "AND terminal_time IS NOT NULL",
            ),
            (
                "execution_tca_intents",
                "updated_at",
                retention_days,
                "AND terminal_time IS NOT NULL",
            ),
            ("opportunity_funnel_events", "event_time", retention_days, ""),
            ("model_shadow_decisions", "decision_time", retention_days, ""),
            ("validation_snapshots", "snapshot_time", retention_days, ""),
        ]

        def _table_layout(
            connection: sqlite3.Connection,
            table_name: str,
        ) -> tuple[list[str], list[str]]:
            info = connection.execute(
                f'PRAGMA table_info("{table_name}")'
            ).fetchall()
            columns = [str(row["name"]) for row in info]
            primary_keys = [
                str(row["name"])
                for row in sorted(info, key=lambda item: int(item["pk"]) or 1_000_000)
                if int(row["pk"]) > 0
            ]
            if not columns or not primary_keys:
                raise ArchiveVerificationError(
                    f"{table_name} must have a declared primary key for verified archival"
                )
            return columns, primary_keys

        def _encoded_value(value: Any) -> bytes:
            if value is None:
                return b"n:"
            if isinstance(value, bytes):
                return b"b:" + value
            if isinstance(value, float):
                return b"f:" + value.hex().encode("ascii")
            if isinstance(value, int):
                return b"i:" + str(value).encode("ascii")
            return b"s:" + str(value).encode("utf-8", errors="surrogatepass")

        def _batch_digest(columns: list[str], values: list[tuple[Any, ...]]) -> str:
            digest = hashlib.sha256()
            digest.update("\x1f".join(columns).encode("utf-8"))
            for row_values in values:
                digest.update(b"\x1e")
                for value in row_values:
                    encoded = _encoded_value(value)
                    digest.update(len(encoded).to_bytes(8, "big"))
                    digest.update(encoded)
            return digest.hexdigest()

        def _archive_bytes() -> int:
            page_count = int(archive_conn.execute("PRAGMA page_count").fetchone()[0])
            page_size = int(archive_conn.execute("PRAGMA page_size").fetchone()[0])
            return page_count * page_size

        def _destination_row(
            table_name: str,
            columns: list[str],
            primary_keys: list[str],
            values_by_column: dict[str, Any],
        ) -> tuple[Any, ...] | None:
            predicates = " AND ".join(f'"{key}" = ?' for key in primary_keys)
            column_sql = ", ".join(f'"{name}"' for name in columns)
            row = archive_conn.execute(
                f'SELECT {column_sql} FROM "{table_name}" WHERE {predicates}',
                tuple(values_by_column[key] for key in primary_keys),
            ).fetchone()
            return tuple(row) if row is not None else None

        results: dict[str, int] = {}
        try:
            for table_name, time_col, days, extra_where in configs:
                cutoff = _get_cutoff(days)
                where_clause = f'"{time_col}" < ? {extra_where}'.strip()
                source_columns, source_primary_keys = _table_layout(
                    self.conn, table_name
                )
                archive_columns, archive_primary_keys = _table_layout(
                    archive_conn, table_name
                )
                if (
                    source_columns != archive_columns
                    or source_primary_keys != archive_primary_keys
                ):
                    raise ArchiveVerificationError(
                        f"archive schema mismatch for {table_name}; source rows retained"
                    )
                column_sql = ", ".join(f'"{name}"' for name in source_columns)
                placeholders = ", ".join("?" for _ in source_columns)
                archived = 0

                for _batch_number in range(max_batches_per_table):
                    rows = self.conn.execute(
                        f'SELECT rowid AS "__source_rowid__", {column_sql} '
                        f'FROM "{table_name}" WHERE {where_clause} '
                        f'ORDER BY "{time_col}", rowid LIMIT ?',
                        (cutoff, batch_size),
                    ).fetchall()
                    if not rows:
                        break
                    rowids = [int(row["__source_rowid__"]) for row in rows]
                    values = [
                        tuple(row[column] for column in source_columns)
                        for row in rows
                    ]
                    content_sha256 = _batch_digest(source_columns, values)
                    batch_id = f"archive-{uuid.uuid4().hex}"
                    created_at = _now()

                    if archive_max_bytes > 0 and _archive_bytes() >= archive_max_bytes:
                        raise ArchiveVerificationError(
                            f"archive budget exhausted at {_archive_bytes()} bytes; "
                            f"source rows in {table_name} retained"
                        )

                    # Record intent in the source before making the independent
                    # archive transaction durable.
                    self.conn.execute(
                        """
                        INSERT INTO archive_batch_manifests
                            (batch_id, table_name, cutoff_time, row_count,
                             content_sha256, state, archive_db_path, created_at)
                        VALUES (?, ?, ?, ?, ?, 'COPYING', ?, ?)
                        """,
                        (
                            batch_id,
                            table_name,
                            cutoff,
                            len(values),
                            content_sha256,
                            str(archive_path),
                            created_at,
                        ),
                    )
                    self.conn.commit()

                    try:
                        archive_conn.execute("BEGIN IMMEDIATE")
                        archive_conn.execute(
                            """
                            INSERT INTO archive_batch_manifests
                                (batch_id, table_name, cutoff_time, row_count,
                                 content_sha256, state, archive_db_path, created_at)
                            VALUES (?, ?, ?, ?, ?, 'COPYING', ?, ?)
                            """,
                            (
                                batch_id,
                                table_name,
                                cutoff,
                                len(values),
                                content_sha256,
                                str(archive_path),
                                created_at,
                            ),
                        )
                        for row_values in values:
                            values_by_column = dict(zip(source_columns, row_values))
                            try:
                                archive_conn.execute(
                                    f'INSERT INTO "{table_name}" ({column_sql}) '
                                    f"VALUES ({placeholders})",
                                    row_values,
                                )
                            except sqlite3.IntegrityError as exc:
                                existing = _destination_row(
                                    table_name,
                                    source_columns,
                                    source_primary_keys,
                                    values_by_column,
                                )
                                if existing != row_values:
                                    raise ArchiveVerificationError(
                                        f"conflicting archive identity in {table_name}; "
                                        "source rows retained"
                                    ) from exc

                        verified_values: list[tuple[Any, ...]] = []
                        for row_values in values:
                            values_by_column = dict(zip(source_columns, row_values))
                            existing = _destination_row(
                                table_name,
                                source_columns,
                                source_primary_keys,
                                values_by_column,
                            )
                            if existing is None:
                                raise ArchiveVerificationError(
                                    f"archive verification missed a {table_name} row"
                                )
                            verified_values.append(existing)
                        if _batch_digest(source_columns, verified_values) != content_sha256:
                            raise ArchiveVerificationError(
                                f"archive content hash mismatch for {table_name}"
                            )
                        verified_at = _now()
                        archive_conn.execute(
                            """
                            UPDATE archive_batch_manifests
                            SET state='VERIFIED', verified_at=?
                            WHERE batch_id=?
                            """,
                            (verified_at, batch_id),
                        )
                        archive_conn.commit()
                    except Exception:
                        archive_conn.rollback()
                        self.conn.execute(
                            """
                            UPDATE archive_batch_manifests
                            SET state='FAILED', error=? WHERE batch_id=?
                            """,
                            ("archive copy or verification failed", batch_id),
                        )
                        self.conn.commit()
                        raise

                    # Re-read the exact selected rowids while holding the write
                    # lock.  If a source row changed after the copy, retain the
                    # entire batch for a later operator-reviewed retry.
                    self.conn.execute("BEGIN IMMEDIATE")
                    try:
                        current_values: dict[int, tuple[Any, ...]] = {}
                        for offset in range(0, len(rowids), 900):
                            chunk = rowids[offset : offset + 900]
                            marks = ",".join("?" for _ in chunk)
                            current_rows = self.conn.execute(
                                f'SELECT rowid AS "__source_rowid__", {column_sql} '
                                f'FROM "{table_name}" WHERE rowid IN ({marks})',
                                tuple(chunk),
                            ).fetchall()
                            current_values.update(
                                {
                                    int(row["__source_rowid__"]): tuple(
                                        row[column] for column in source_columns
                                    )
                                    for row in current_rows
                                }
                            )
                        expected_by_rowid = dict(zip(rowids, values))
                        if current_values != expected_by_rowid:
                            raise ArchiveVerificationError(
                                f"source changed during archival of {table_name}; rows retained"
                            )
                        deleted = 0
                        for offset in range(0, len(rowids), 900):
                            chunk = rowids[offset : offset + 900]
                            marks = ",".join("?" for _ in chunk)
                            cursor = self.conn.execute(
                                f'DELETE FROM "{table_name}" WHERE rowid IN ({marks})',
                                tuple(chunk),
                            )
                            deleted += max(0, int(cursor.rowcount))
                        if deleted != len(rowids):
                            raise ArchiveVerificationError(
                                f"short source delete for {table_name}: "
                                f"expected {len(rowids)}, deleted {deleted}"
                            )
                        completed_at = _now()
                        self.conn.execute(
                            """
                            UPDATE archive_batch_manifests
                            SET state='COMPLETE', verified_at=?, completed_at=?
                            WHERE batch_id=?
                            """,
                            (verified_at, completed_at, batch_id),
                        )
                        self.conn.commit()
                        archive_conn.execute(
                            """
                            UPDATE archive_batch_manifests
                            SET state='COMPLETE', completed_at=? WHERE batch_id=?
                            """,
                            (completed_at, batch_id),
                        )
                        archive_conn.commit()
                    except Exception:
                        self.conn.rollback()
                        raise

                    archived += len(rows)
                    if len(rows) < batch_size:
                        break

                results[f"{table_name}_archived"] = archived
            return results
        finally:
            archive_conn.close()

    def maintenance(
        self,
        run_vacuum: bool = False,
        *,
        quiescent: bool = False,
        incremental_pages: int = 1_000,
    ) -> dict[str, Any]:
        """Run bounded online maintenance without allocating a second DB image.

        Active processes only use a PASSIVE checkpoint.  A full ``VACUUM`` is
        deliberately unsupported here; offline rebuilds belong in the verified
        backup/restore workflow where peak free space is preflighted.
        """

        if run_vacuum:
            raise ValueError(
                "full VACUUM is disabled; use a verified offline rebuild while flat"
            )
        checkpoint_mode = "TRUNCATE" if quiescent else "PASSIVE"
        checkpoint_row = self.conn.execute(
            f"PRAGMA wal_checkpoint({checkpoint_mode})"
        ).fetchone()
        auto_vacuum = int(self.conn.execute("PRAGMA auto_vacuum").fetchone()[0])
        if auto_vacuum == 2 and incremental_pages > 0:
            self.conn.execute(
                f"PRAGMA incremental_vacuum({max(1, int(incremental_pages))})"
            )
        self.conn.commit()
        return {
            "checkpoint_mode": checkpoint_mode,
            "checkpoint_busy": int(checkpoint_row[0]) if checkpoint_row else 0,
            "checkpoint_log_pages": int(checkpoint_row[1]) if checkpoint_row else 0,
            "checkpointed_pages": int(checkpoint_row[2]) if checkpoint_row else 0,
            "auto_vacuum": auto_vacuum,
        }

    def update_position_metrics(self, symbol: str, **fields: Any) -> None:
        if not fields:
            return
        updates: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            updates.append(f"{key} = ?")
            params.append(value)
        updates.append("updated_at = ?")
        params.append(_now())
        params.append(symbol)
        self.conn.execute(
            f"UPDATE positions SET {', '.join(updates)} WHERE symbol = ?",
            tuple(params),
        )
        self.conn.commit()

    def record_market_sample(
        self,
        symbol: str,
        sample_minute: str,
        ann_funding: float,
        basis_pct: float,
        mark_price: float,
        minute_notional_volume: float = 0.0,
    ) -> None:
        self.upsert_market_sample(
            sample_minute=sample_minute,
            symbol=symbol,
            ann_funding=ann_funding,
            basis_pct=basis_pct,
            mark_price=mark_price,
            minute_notional_volume=minute_notional_volume,
        )

    def upsert_pending_intent(self, intent: PendingIntent | None = None, **kwargs: Any) -> None:
        if intent is None:
            intent = PendingIntent(
                intent_id=str(kwargs.get("intent_id", "")),
                symbol=str(kwargs.get("symbol", "")),
                intent_type=str(kwargs.get("intent_type", "")),
                direction=str(kwargs.get("direction", "")),
                status=str(kwargs.get("status", "")),
                quantity=float(kwargs.get("quantity", 0.0)),
                notional_usd=float(kwargs.get("notional_usd", 0.0)),
                client_order_id=kwargs.get("client_order_id"),
                retry_count=int(kwargs.get("retry_count", 0)),
                last_error=str(kwargs.get("last_error", "")),
                metadata=dict(kwargs.get("metadata", {})),
                created_at=kwargs.get("created_at"),
                updated_at=kwargs.get("updated_at"),
                lifecycle_state=str(
                    kwargs.get("lifecycle_state", ACTIVE_PENDING_INTENT_STATE)
                ),
                terminal_sequence_watermark=kwargs.get(
                    "terminal_sequence_watermark"
                ),
                reconciliation_status=str(
                    kwargs.get("reconciliation_status", "PENDING")
                ),
                retention_deadline=kwargs.get("retention_deadline"),
                tombstoned_at=kwargs.get("tombstoned_at"),
                tombstone_reason=str(kwargs.get("tombstone_reason", "")),
            )
        now = _now()
        self.conn.execute(
            """
            INSERT INTO pending_intents
                (intent_id, symbol, intent_type, direction, status, quantity, notional_usd,
                 client_order_id, retry_count, last_error, metadata, created_at, updated_at,
                 lifecycle_state, terminal_sequence_watermark, reconciliation_status,
                 retention_deadline, tombstoned_at, tombstone_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(intent_id) DO UPDATE SET
                status=excluded.status,
                quantity=excluded.quantity,
                notional_usd=excluded.notional_usd,
                client_order_id=excluded.client_order_id,
                retry_count=excluded.retry_count,
                last_error=excluded.last_error,
                metadata=excluded.metadata,
                updated_at=excluded.updated_at
            """,
            (
                intent.intent_id,
                intent.symbol,
                intent.intent_type,
                intent.direction,
                intent.status,
                intent.quantity,
                intent.notional_usd,
                intent.client_order_id,
                intent.retry_count,
                intent.last_error,
                _json_dump(intent.metadata),
                intent.created_at or now,
                intent.updated_at or now,
                intent.lifecycle_state,
                intent.terminal_sequence_watermark,
                intent.reconciliation_status,
                intent.retention_deadline,
                intent.tombstoned_at,
                intent.tombstone_reason,
            ),
        )
        self.conn.commit()

    def update_pending_intent(self, intent_id: str, **fields: Any) -> None:
        if not fields:
            return
        updates: list[str] = []
        params: list[Any] = []
        if "metadata" in fields:
            fields["metadata"] = _json_dump(fields["metadata"] or {})
        for key, value in fields.items():
            updates.append(f"{key} = ?")
            params.append(value)
        updates.append("updated_at = ?")
        params.append(_now())
        params.append(intent_id)
        self.conn.execute(
            f"UPDATE pending_intents SET {', '.join(updates)} WHERE intent_id = ?",
            tuple(params),
        )
        self.conn.commit()

    def record_account_truth_snapshot(
        self,
        truth: NormalizedAccountTruth,
        *,
        commit: bool = True,
    ) -> bool:
        """Append an exact normalized account snapshot and its untouched raw JSON."""

        payload = truth.to_dict(include_raw=True)
        standard_spot_json = json.dumps(
            payload["standard_spot"],
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        usd_m_futures_json = json.dumps(
            payload["usd_m_futures"],
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        raw_snapshot_json = json.dumps(
            payload["raw_snapshot"],
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        missing_fields_json = json.dumps(
            payload["missing_fields"],
            sort_keys=True,
            separators=(",", ":"),
        )
        cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO account_truth_snapshots
                (snapshot_id, schema_version, account_id, environment,
                 captured_at, availability_time, expires_at, status,
                 standard_spot_status, usd_m_futures_status,
                 missing_fields_json, standard_spot_json, usd_m_futures_json,
                 raw_snapshot_json, content_hash, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                truth.snapshot_id,
                truth.schema_version,
                truth.account_id,
                truth.environment,
                truth.captured_at,
                truth.availability_time,
                truth.expires_at,
                truth.status,
                truth.standard_spot_status,
                truth.usd_m_futures_status,
                missing_fields_json,
                standard_spot_json,
                usd_m_futures_json,
                raw_snapshot_json,
                truth.content_hash,
                _now(),
            ),
        )
        inserted = cursor.rowcount == 1
        if not inserted:
            existing = self.conn.execute(
                "SELECT content_hash FROM account_truth_snapshots WHERE snapshot_id = ?",
                (truth.snapshot_id,),
            ).fetchone()
            if existing is None or str(existing["content_hash"]) != truth.content_hash:
                raise ValueError(
                    f"account truth snapshot identity collision: {truth.snapshot_id}"
                )
        # This is a restart projection, not the long-horizon economic ledger.
        # A signed snapshot can contain thousands of raw trade rows, so retain
        # only a bounded recovery window per account/environment. Immutable
        # fills and statements remain in their dedicated audit ledgers.
        self.conn.execute(
            """
            DELETE FROM account_truth_snapshots
            WHERE account_id = ?
              AND environment = ?
              AND snapshot_id NOT IN (
                  SELECT snapshot_id
                  FROM account_truth_snapshots
                  WHERE account_id = ? AND environment = ?
                  -- `created_at` can have identical values on coarse clocks.
                  -- A later UNKNOWN snapshot must still supersede an older
                  -- COMPLETE snapshot, so insertion order is the tie-breaker.
                  ORDER BY created_at DESC, rowid DESC, availability_time DESC
                  LIMIT ?
              )
            """,
            (
                truth.account_id,
                truth.environment,
                truth.account_id,
                truth.environment,
                ACCOUNT_TRUTH_RESTART_RETENTION_PER_SCOPE,
            ),
        )
        if commit:
            self.conn.commit()
        return inserted

    def tombstone_pending_intent(
        self,
        intent_id: str,
        *,
        lifecycle_state: str = TERMINAL_RECONCILED,
        terminal_sequence: int | None = None,
        reconciliation_status: str = "TERMINAL_CONFIRMED",
        retention_deadline: str | None = None,
        tombstoned_at: str | None = None,
        reason: str = "lifecycle_resolved",
        commit: bool = True,
    ) -> bool:
        """Retain resolved intent lineage as a monotonic durable tombstone."""

        target = str(intent_id or "").strip()
        if not target:
            return False
        normalized_state = str(lifecycle_state or TERMINAL_RECONCILED).strip().upper()
        if normalized_state == ACTIVE_PENDING_INTENT_STATE:
            raise ValueError("a tombstone cannot use ACTIVE lifecycle state")
        normalized_reconciliation = str(
            reconciliation_status or "PENDING_TERMINAL"
        ).strip().upper()
        observed_at = _parse_iso(tombstoned_at) or datetime.now(timezone.utc)
        observed_at_text = observed_at.isoformat()
        deadline = _parse_iso(retention_deadline)
        if deadline is None:
            deadline = observed_at + timedelta(
                days=DEFAULT_PENDING_INTENT_TOMBSTONE_RETENTION_DAYS
            )
        sequence = None if terminal_sequence is None else int(terminal_sequence)
        if sequence is not None and sequence < 0:
            raise ValueError("terminal_sequence must be non-negative")

        cursor = self.conn.execute(
            """
            UPDATE pending_intents
            SET lifecycle_state = ?,
                terminal_sequence_watermark = CASE
                    WHEN ? IS NULL THEN terminal_sequence_watermark
                    WHEN terminal_sequence_watermark IS NULL THEN ?
                    WHEN ? > terminal_sequence_watermark THEN ?
                    ELSE terminal_sequence_watermark
                END,
                reconciliation_status = ?,
                retention_deadline = COALESCE(retention_deadline, ?),
                tombstoned_at = COALESCE(tombstoned_at, ?),
                tombstone_reason = CASE
                    WHEN ? != '' THEN ?
                    ELSE tombstone_reason
                END,
                updated_at = ?
            WHERE intent_id = ?
            """,
            (
                normalized_state,
                sequence,
                sequence,
                sequence,
                sequence,
                normalized_reconciliation,
                deadline.isoformat(),
                observed_at_text,
                str(reason or ""),
                str(reason or ""),
                observed_at_text,
                target,
            ),
        )
        if commit:
            self.conn.commit()
        return cursor.rowcount == 1

    def delete_pending_intent(self, intent_id: str, *, commit: bool = True) -> None:
        """Compatibility alias retained for callers; no row is destructively deleted."""

        self.tombstone_pending_intent(intent_id, commit=commit)

    def prune_pending_intent_tombstones(
        self,
        *,
        now: str | None = None,
        commit: bool = True,
    ) -> int:
        """Compact expired reconciled tombstones without destroying lineage."""

        cutoff = (_parse_iso(now) or datetime.now(timezone.utc)).isoformat()
        cursor = self.conn.execute(
            """
            UPDATE pending_intents
            SET lifecycle_state = ?,
                reconciliation_status = 'RETENTION_EXPIRED',
                updated_at = ?
            WHERE lifecycle_state != ?
              AND lifecycle_state != ?
              AND retention_deadline IS NOT NULL
              AND retention_deadline <= ?
            """,
            (
                RETAINED_PRUNED,
                cutoff,
                ACTIVE_PENDING_INTENT_STATE,
                RETAINED_PRUNED,
                cutoff,
            ),
        )
        if commit:
            self.conn.commit()
        return max(0, int(cursor.rowcount))

    def _claim_lifecycle_event(
        self,
        *,
        event_key: str,
        event_type: str,
        symbol: str,
        intent_id: str,
        event_time: str,
        payload: Mapping[str, Any],
    ) -> bool:
        if not event_key.strip():
            raise ValueError("lifecycle event_key is required")
        payload_json = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        content_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO lifecycle_events
                (event_key, event_type, symbol, intent_id, event_time,
                 content_hash, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_key,
                event_type.upper(),
                symbol.upper(),
                intent_id,
                event_time,
                content_hash,
                payload_json,
            ),
        )
        if cursor.rowcount == 1:
            return True
        existing = self.conn.execute(
            "SELECT content_hash FROM lifecycle_events WHERE event_key = ?",
            (event_key,),
        ).fetchone()
        if existing is None or str(existing["content_hash"]) != content_hash:
            raise ValueError(f"lifecycle event identity collision: {event_key}")
        return False

    @staticmethod
    def _lifecycle_tombstone_fields(
        evidence: Mapping[str, Any],
    ) -> tuple[str, str, int | None, str]:
        execution_type = str(evidence.get("execution_type") or "").upper()
        exchange_flat = bool(evidence.get("exchange_flat_awaiting_terminal")) or (
            execution_type == "RECONCILED_FLAT"
        )
        state = str(evidence.get("tombstone_lifecycle_state") or "").upper()
        if not state:
            state = EXCHANGE_FLAT_AWAITING_TERMINAL if exchange_flat else TERMINAL_RECONCILED
        reconciliation = str(evidence.get("reconciliation_status") or "").upper()
        if not reconciliation:
            reconciliation = "EXCHANGE_FLAT" if exchange_flat else "TERMINAL_CONFIRMED"
        raw_sequence = evidence.get("telemetry_sequence")
        try:
            sequence = None if raw_sequence is None else int(raw_sequence)
        except (TypeError, ValueError):
            sequence = None
        reason = str(evidence.get("tombstone_reason") or execution_type or "lifecycle_projected")
        return state, reconciliation, sequence, reason

    def project_entry_lifecycle(
        self,
        *,
        event_key: str,
        intent_id: str,
        event_time: str,
        position_fields: Mapping[str, Any],
        evidence: Mapping[str, Any],
    ) -> bool:
        """Atomically claim an entry event, open its position and retain its tombstone."""

        symbol = str(position_fields.get("symbol") or "").upper()
        canonical = {
            "position": dict(position_fields),
            "evidence": dict(evidence),
        }
        savepoint = "entry_lifecycle_projection"
        with self._lifecycle_lock:
            self.conn.execute(f"SAVEPOINT {savepoint}")
            try:
                inserted = self._claim_lifecycle_event(
                    event_key=event_key,
                    event_type="ENTRY_FILLED",
                    symbol=symbol,
                    intent_id=intent_id,
                    event_time=event_time,
                    payload=canonical,
                )
                if inserted:
                    self.upsert_position(**dict(position_fields), commit=False)
                    if intent_id:
                        state, reconciliation, sequence, reason = (
                            self._lifecycle_tombstone_fields(evidence)
                        )
                        self.tombstone_pending_intent(
                            intent_id,
                            lifecycle_state=state,
                            terminal_sequence=sequence,
                            reconciliation_status=reconciliation,
                            reason=reason,
                            tombstoned_at=event_time,
                            commit=False,
                        )
                self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                self.conn.commit()
                return inserted
            except Exception:
                self.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise

    def project_exit_lifecycle(
        self,
        *,
        event_key: str,
        intent_id: str,
        event_time: str,
        trade: Trade,
        evidence: Mapping[str, Any],
    ) -> bool:
        """Atomically claim an exit, append one trade, flatten and retain lineage."""

        canonical = {"trade": asdict(trade), "evidence": dict(evidence)}
        savepoint = "exit_lifecycle_projection"
        with self._lifecycle_lock:
            self.conn.execute(f"SAVEPOINT {savepoint}")
            try:
                inserted = self._claim_lifecycle_event(
                    event_key=event_key,
                    event_type="EXIT_FILLED",
                    symbol=trade.symbol,
                    intent_id=intent_id,
                    event_time=event_time,
                    payload=canonical,
                )
                if inserted:
                    self.record_trade(trade, commit=False)
                    self.remove_position(trade.symbol, commit=False)
                    if intent_id:
                        state, reconciliation, sequence, reason = (
                            self._lifecycle_tombstone_fields(evidence)
                        )
                        self.tombstone_pending_intent(
                            intent_id,
                            lifecycle_state=state,
                            terminal_sequence=sequence,
                            reconciliation_status=reconciliation,
                            reason=reason,
                            tombstoned_at=event_time,
                            commit=False,
                        )
                self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                self.conn.commit()
                return inserted
            except Exception:
                self.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise

    def project_partial_exit_lifecycle(
        self,
        *,
        event_key: str,
        intent_id: str,
        event_time: str,
        remaining_position_fields: Mapping[str, Any],
        evidence: Mapping[str, Any],
    ) -> bool:
        """Atomically journal a neutral partial exit and project its residual.

        Partial exits do not create a realized trade row.  Their immutable fill
        evidence is folded into the one final trade when the residual position
        is eventually closed, preventing overlapping funding/commission
        windows from being counted more than once.
        """

        remaining = dict(remaining_position_fields)
        symbol = str(remaining.get("symbol") or "").upper()
        try:
            remaining_qty = Decimal(str(remaining.get("qty", "0")))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("partial exit residual quantity is not decimal-safe") from exc
        if not symbol or remaining_qty <= 0:
            raise ValueError("partial exit requires a positive residual position")
        canonical = {
            "remaining_position": remaining,
            "evidence": dict(evidence),
        }
        savepoint = "partial_exit_lifecycle_projection"
        with self._lifecycle_lock:
            self.conn.execute(f"SAVEPOINT {savepoint}")
            try:
                inserted = self._claim_lifecycle_event(
                    event_key=event_key,
                    event_type="PARTIAL_EXIT_FILLED",
                    symbol=symbol,
                    intent_id=intent_id,
                    event_time=event_time,
                    payload=canonical,
                )
                if inserted:
                    self.upsert_position(**remaining, commit=False)
                    if intent_id:
                        state, reconciliation, sequence, reason = (
                            self._lifecycle_tombstone_fields(evidence)
                        )
                        self.tombstone_pending_intent(
                            intent_id,
                            lifecycle_state=state,
                            terminal_sequence=sequence,
                            reconciliation_status=reconciliation,
                            reason=reason,
                            tombstoned_at=event_time,
                            commit=False,
                        )
                self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                self.conn.commit()
                return inserted
            except Exception:
                self.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise

    def rebuild_lifecycle_projections(
        self,
        *,
        authoritative_positions: Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Rebuild positions/trades from the immutable lifecycle journal.

        The replay is prepared and hash-checked before projections are touched.
        Its final open-position identity must match the caller's authoritative
        exchange snapshot; disagreement rolls back instead of adopting or
        deleting exposure.  Exchange history should first be ingested as
        lifecycle evidence when the journal is behind.
        """

        rows = self.conn.execute(
            """SELECT event_key, event_type, symbol, intent_id, event_time,
                      content_hash, payload_json
               FROM lifecycle_events
               ORDER BY event_time, event_key"""
        ).fetchall()
        replay: list[tuple[str, dict[str, Any], str]] = []
        projected_positions: dict[str, dict[str, Any]] = {}
        trade_count = 0
        for row in rows:
            payload_json = str(row["payload_json"])
            actual_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
            if actual_hash != str(row["content_hash"]):
                raise LifecycleRebuildError(
                    f"lifecycle content hash mismatch: {row['event_key']}"
                )
            try:
                payload = json.loads(payload_json)
            except json.JSONDecodeError as exc:
                raise LifecycleRebuildError(
                    f"invalid lifecycle payload: {row['event_key']}"
                ) from exc
            if not isinstance(payload, dict):
                raise LifecycleRebuildError(
                    f"lifecycle payload is not an object: {row['event_key']}"
                )
            event_type = str(row["event_type"]).upper()
            symbol = str(row["symbol"]).upper()
            if event_type == "ENTRY_FILLED":
                position = payload.get("position")
                if not isinstance(position, dict):
                    raise LifecycleRebuildError(
                        f"entry lifecycle lacks position: {row['event_key']}"
                    )
                position = dict(position)
                if str(position.get("symbol") or "").upper() != symbol:
                    raise LifecycleRebuildError(
                        f"entry lifecycle symbol mismatch: {row['event_key']}"
                    )
                projected_positions[symbol] = position
            elif event_type == "PARTIAL_EXIT_FILLED":
                remaining = payload.get("remaining_position")
                if not isinstance(remaining, dict):
                    raise LifecycleRebuildError(
                        f"partial exit lifecycle lacks residual: {row['event_key']}"
                    )
                remaining = dict(remaining)
                if str(remaining.get("symbol") or "").upper() != symbol:
                    raise LifecycleRebuildError(
                        f"partial exit lifecycle symbol mismatch: {row['event_key']}"
                    )
                if symbol not in projected_positions:
                    raise LifecycleRebuildError(
                        f"partial exit lifecycle lacks prior position: {row['event_key']}"
                    )
                try:
                    before_qty = Decimal(
                        str(projected_positions[symbol].get("qty", "0"))
                    )
                    after_qty = Decimal(str(remaining.get("qty", "0")))
                except (InvalidOperation, TypeError, ValueError) as exc:
                    raise LifecycleRebuildError(
                        f"partial exit quantity is not decimal-safe: {row['event_key']}"
                    ) from exc
                if after_qty <= 0 or after_qty >= before_qty:
                    raise LifecycleRebuildError(
                        f"partial exit residual is not strictly smaller: {row['event_key']}"
                    )
                projected_positions[symbol] = remaining
            elif event_type == "EXIT_FILLED":
                trade = payload.get("trade")
                if not isinstance(trade, dict):
                    raise LifecycleRebuildError(
                        f"exit lifecycle lacks trade: {row['event_key']}"
                    )
                if str(trade.get("symbol") or "").upper() != symbol:
                    raise LifecycleRebuildError(
                        f"exit lifecycle symbol mismatch: {row['event_key']}"
                    )
                projected_positions.pop(symbol, None)
                trade_count += 1
            else:
                raise LifecycleRebuildError(
                    f"unsupported lifecycle event type: {event_type}"
                )
            replay.append((event_type, payload, str(row["intent_id"])))

        def position_identity(position: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
            try:
                qty = str(Decimal(str(position.get("qty", "0"))).normalize())
                hedge_ratio = str(
                    Decimal(str(position.get("hedge_ratio", "1"))).normalize()
                )
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise LifecycleRebuildError("position identity is not decimal-safe") from exc
            return (
                str(position.get("symbol") or "").upper(),
                str(position.get("side") or "").upper(),
                str(position.get("direction") or "").lower(),
                qty,
                hedge_ratio,
            )

        exchange_positions: dict[str, Mapping[str, Any]] = {}
        for position in authoritative_positions:
            symbol = str(position.get("symbol") or "").upper()
            if not symbol or symbol in exchange_positions:
                raise LifecycleRebuildError(
                    "authoritative exchange positions require unique symbols"
                )
            exchange_positions[symbol] = position
        journal_identities = sorted(
            position_identity(position) for position in projected_positions.values()
        )
        exchange_identities = sorted(
            position_identity(position) for position in exchange_positions.values()
        )
        if journal_identities != exchange_identities:
            raise LifecycleRebuildError(
                "lifecycle replay does not match authoritative exchange positions"
            )

        proof_payload = {
            "events": len(replay),
            "positions": journal_identities,
            "trades": trade_count,
        }
        proof_hash = hashlib.sha256(
            json.dumps(proof_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        savepoint = "lifecycle_projection_rebuild"
        with self._lifecycle_lock:
            self.conn.execute(f"SAVEPOINT {savepoint}")
            try:
                self.conn.execute("DELETE FROM positions")
                self.conn.execute("DELETE FROM trade_history")
                for event_type, payload, intent_id in replay:
                    if event_type == "ENTRY_FILLED":
                        self.upsert_position(**dict(payload["position"]), commit=False)
                    elif event_type == "PARTIAL_EXIT_FILLED":
                        self.upsert_position(
                            **dict(payload["remaining_position"]), commit=False
                        )
                    else:
                        self.record_trade(Trade(**dict(payload["trade"])), commit=False)
                        self.remove_position(str(payload["trade"]["symbol"]), commit=False)
                    if intent_id:
                        evidence = payload.get("evidence")
                        evidence = evidence if isinstance(evidence, dict) else {}
                        state, reconciliation, sequence, reason = (
                            self._lifecycle_tombstone_fields(evidence)
                        )
                        self.tombstone_pending_intent(
                            intent_id,
                            lifecycle_state=state,
                            terminal_sequence=sequence,
                            reconciliation_status=reconciliation,
                            reason=f"lifecycle_rebuild:{reason}",
                            commit=False,
                        )
                self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                self.conn.commit()
            except Exception:
                self.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
        return {
            "event_count": len(replay),
            "position_count": len(projected_positions),
            "trade_count": trade_count,
            "proof_hash": proof_hash,
            "exchange_positions_matched": True,
        }

    def reserve_execution_command(
        self,
        payload: dict[str, Any],
        *,
        producer_id: str,
        ttl_ms: int,
        created_at_ms: int | None = None,
    ) -> dict[str, Any]:
        """Durably reserve a versioned command before it is sent.

        Re-reserving an identical ``intent_id`` returns its original immutable
        envelope.  Reusing an ID for different command economics raises rather
        than overwriting the original outbox record.
        """

        from bongus.ipc.protocol import build_command_envelope, command_hash

        intent_id = str(payload.get("intent_id") or "").strip()
        if not intent_id:
            raise ValueError("execution command requires intent_id")
        requested_hash = command_hash(payload)
        now = _now()

        with self._command_outbox_lock:
            self._command_conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._command_conn.execute(
                    """
                    SELECT command_hash, envelope_json
                    FROM execution_command_outbox
                    WHERE intent_id = ?
                    """,
                    (intent_id,),
                ).fetchone()
                if existing is not None:
                    if str(existing["command_hash"]) != requested_hash:
                        raise ValueError(
                            f"intent_id {intent_id!r} conflicts with its durable command"
                        )
                    envelope = json.loads(str(existing["envelope_json"]))
                    self._command_conn.commit()
                    return envelope

                row = self._command_conn.execute(
                    """
                    INSERT INTO execution_command_sequences
                        (producer_id, last_sequence, updated_at)
                    VALUES (?, 1, ?)
                    ON CONFLICT(producer_id) DO UPDATE SET
                        last_sequence=execution_command_sequences.last_sequence + 1,
                        updated_at=excluded.updated_at
                    RETURNING last_sequence
                    """,
                    (producer_id, now),
                ).fetchone()
                sequence = int(row["last_sequence"])
                envelope = build_command_envelope(
                    payload,
                    producer_id=producer_id,
                    sequence=sequence,
                    ttl_ms=ttl_ms,
                    created_at_ms=created_at_ms,
                )
                self._command_conn.execute(
                    """
                    INSERT INTO execution_command_outbox
                        (intent_id, schema_version, producer_id, sequence,
                         intent_type, symbol, command_hash, envelope_json, state,
                         created_at_ms, deadline_at_ms, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'READY', ?, ?, ?)
                    """,
                    (
                        intent_id,
                        int(envelope["schema_version"]),
                        producer_id,
                        sequence,
                        str(envelope["intent"]),
                        str(envelope.get("symbol") or "").upper(),
                        str(envelope["command_hash"]),
                        _json_dump(envelope),
                        int(envelope["created_at_ms"]),
                        int(envelope["deadline_at_ms"]),
                        now,
                    ),
                )
                self._command_conn.commit()
                return envelope
            except Exception:
                self._command_conn.rollback()
                raise

    def next_execution_intent_id(
        self,
        *,
        producer_id: str,
        symbol: str,
        intent_type: str,
    ) -> str:
        """Allocate a restart-safe, deterministic logical intent identifier."""

        now = _now()
        with self._command_outbox_lock:
            row = self._command_conn.execute(
                """
                INSERT INTO execution_command_sequences
                    (producer_id, last_sequence, updated_at)
                VALUES (?, 1, ?)
                ON CONFLICT(producer_id) DO UPDATE SET
                    last_sequence=execution_command_sequences.last_sequence + 1,
                    updated_at=excluded.updated_at
                RETURNING last_sequence
                """,
                (f"{producer_id}:intent-ids", now),
            ).fetchone()
            self._command_conn.commit()
        sequence = int(row["last_sequence"])
        return (
            f"{producer_id}:{sequence}:{intent_type.lower()}:{symbol.lower()}"
        )

    def mark_execution_command_sent(self, intent_id: str) -> None:
        now = _now()
        with self._command_outbox_lock:
            self._command_conn.execute(
                """
                UPDATE execution_command_outbox
                SET state=CASE
                        WHEN state IN ('READY', 'SEND_FAILED') THEN 'SENT'
                        ELSE state
                    END,
                    send_attempts=send_attempts + 1,
                    first_sent_at=COALESCE(first_sent_at, ?),
                    last_sent_at=?,
                    updated_at=?
                WHERE intent_id=?
                """,
                (now, now, now, intent_id),
            )
            self._command_conn.commit()

    def mark_execution_command_send_failed(self, intent_id: str, reason: str) -> None:
        now = _now()
        with self._command_outbox_lock:
            self._command_conn.execute(
                """
                UPDATE execution_command_outbox
                SET state=CASE
                        WHEN state IN ('READY', 'SENT', 'SEND_FAILED') THEN 'SEND_FAILED'
                        ELSE state
                    END,
                    ack_reason=?, send_attempts=send_attempts + 1,
                    last_sent_at=?, updated_at=?
                WHERE intent_id=?
                """,
                (reason, now, now, intent_id),
            )
            self._command_conn.commit()

    def apply_execution_command_ack(self, event: dict[str, Any]) -> bool:
        """Apply an idempotent monotonic ACK to the durable command outbox."""

        from bongus.ipc.protocol import validate_ack

        intent_id, ack_status = validate_ack(event)
        ack_hash = str(event.get("command_hash") or "")
        reason = str(event.get("reason") or "")
        ranks = {
            "READY": 0,
            "SEND_FAILED": 0,
            "SENT": 1,
            "RECEIVED": 2,
            "VALIDATED": 3,
            "SUBMITTED": 4,
            "TERMINAL": 5,
            "REJECTED": 5,
        }
        now = _now()
        with self._command_outbox_lock:
            row = self._command_conn.execute(
                "SELECT state, command_hash FROM execution_command_outbox WHERE intent_id=?",
                (intent_id,),
            ).fetchone()
            if row is None:
                return False
            if ack_hash and ack_hash != str(row["command_hash"]):
                raise ValueError(f"ACK hash conflict for intent_id {intent_id!r}")
            current = str(row["state"]).upper()
            if current in {"TERMINAL", "REJECTED"}:
                if ack_status in {"TERMINAL", "REJECTED"} and current != ack_status:
                    raise ValueError(
                        f"terminal ACK conflict for {intent_id!r}: {current} -> {ack_status}"
                    )
                return True
            if ranks[ack_status] < ranks.get(current, -1):
                return True
            self._command_conn.execute(
                """
                UPDATE execution_command_outbox
                SET state=?, ack_reason=?, last_ack_at=?, updated_at=?
                WHERE intent_id=?
                """,
                (ack_status, reason, now, now, intent_id),
            )
            self._command_conn.commit()
            return True

    def get_replayable_execution_commands(self) -> list[dict[str, Any]]:
        """Return non-terminal envelopes in producer sequence order."""

        rows = self._command_conn.execute(
            """
            SELECT * FROM execution_command_outbox
            WHERE state NOT IN ('TERMINAL', 'REJECTED')
            ORDER BY producer_id, sequence
            """
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["envelope"] = json.loads(str(item.pop("envelope_json")))
            result.append(item)
        return result

    def record_ai_report_proposal(
        self,
        *,
        proposal_id: str,
        summary: str,
        proposed_changes: dict[str, Any],
        status: str = "PENDING",
        report_period_start: str = "",
        report_period_end: str = "",
        raw_response: str = "",
    ) -> None:
        now = _now()
        self.conn.execute(
            """
            INSERT INTO ai_report_proposals
                (proposal_id, created_at, report_period_start, report_period_end,
                 summary, proposed_changes, status, raw_response)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(proposal_id) DO UPDATE SET
                summary=excluded.summary,
                proposed_changes=excluded.proposed_changes,
                status=excluded.status,
                raw_response=excluded.raw_response
            """,
            (
                proposal_id,
                now,
                report_period_start,
                report_period_end,
                summary,
                _json_dump(proposed_changes),
                status,
                raw_response,
            ),
        )
        self.conn.commit()

    def update_ai_report_proposal(
        self,
        proposal_id: str,
        *,
        status: str,
        decision_source: str = "",
        applied: bool = False,
    ) -> None:
        now = _now()
        self.conn.execute(
            """
            UPDATE ai_report_proposals
            SET status = ?, decision_time = ?, decision_source = ?,
                applied_at = CASE WHEN ? THEN ? ELSE applied_at END
            WHERE proposal_id = ?
            """,
            (status, now, decision_source, int(applied), now, proposal_id),
        )
        self.conn.commit()

    def close(self) -> None:
        if self._owns_guard_connections:
            self._feed_recovery_conn.close()
            self._cooldown_conn.close()
        if self._owns_statement_connection:
            self._statement_conn.close()
        if self._owns_telemetry_connection:
            self._telemetry_conn.close()
        if self._owns_command_connection:
            self._command_conn.close()
        self.conn.close()


class StateReader:
    def __init__(self, db_path: str = DB_PATH) -> None:
        # readonly=True skips DDL migrations; the StateWriter always starts first
        # and owns schema creation. Skipping migrations avoids "database is locked"
        # races when the dashboard process opens a second connection mid-write-cycle.
        self.conn = _connect(db_path, readonly=True)

    def _current_scope(self) -> dict[str, str]:
        risk = self.get_risk()
        return {
            "trading_mode": str(risk.get("trading_mode") or "").strip().lower(),
            "runtime_mode": str(risk.get("runtime_mode") or "").strip().upper(),
            "session_id": str(risk.get("session_id") or "").strip(),
            "session_start": str(risk.get("bot_started_at") or "").strip(),
        }

    def _scoped_where_clause(
        self,
        *,
        time_column: str,
        scope_current: bool,
        session_scoped: bool = True,
    ) -> tuple[str, list[Any]]:
        if not scope_current:
            return "", []
        scope = self._current_scope()
        trading_mode = scope["trading_mode"]
        session_id = scope["session_id"]
        session_start = scope["session_start"]
        if not session_scoped:
            if trading_mode:
                return (
                    "WHERE (LOWER(COALESCE(trading_mode, '')) = ? OR COALESCE(trading_mode, '') = '')",
                    [trading_mode],
                )
            return "", []
        if trading_mode and session_id and session_start:
            return (
                (
                    "WHERE ("
                    "(LOWER(COALESCE(trading_mode, '')) = ? AND COALESCE(session_id, '') = ?)"
                    f" OR (COALESCE(session_id, '') = '' AND {time_column} >= ? "
                    "AND (LOWER(COALESCE(trading_mode, '')) = ? OR COALESCE(trading_mode, '') = ''))"
                    ")"
                ),
                [trading_mode, session_id, session_start, trading_mode],
            )
        if trading_mode and session_start:
            return (
                f"WHERE ({time_column} >= ? AND "
                "(LOWER(COALESCE(trading_mode, '')) = ? "
                "OR COALESCE(trading_mode, '') = ''))",
                [session_start, trading_mode],
            )
        if trading_mode:
            return (
                "WHERE (LOWER(COALESCE(trading_mode, '')) = ? OR COALESCE(trading_mode, '') = '')",
                [trading_mode],
            )
        return "", []

    def _latest_market_price(self, symbol: str) -> float:
        row = self.conn.execute(
            """
            SELECT mark_price
            FROM market_samples
            WHERE symbol = ?
            ORDER BY sample_minute DESC
            LIMIT 1
            """,
            (symbol,),
        ).fetchone()
        if row is None:
            return 0.0
        try:
            return float(row["mark_price"] or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _rows_to_dicts(self, rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
        return [dict(row) for row in rows]

    def get_positions(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM positions WHERE status != 'CLOSED' ORDER BY symbol"
        ).fetchall()
        return self._rows_to_dicts(rows)

    def get_positions_for_current_mode(self) -> list[dict[str, Any]]:
        positions = self.get_positions()
        scope = self._current_scope()
        trading_mode = scope["trading_mode"]
        session_start = _parse_iso(scope["session_start"])
        if not trading_mode:
            return positions

        current_mode_positions = [
            position
            for position in positions
            if str(position.get("trading_mode") or "").strip().lower() == trading_mode
        ]
        if current_mode_positions:
            return current_mode_positions

        legacy_positions: list[dict[str, Any]] = []
        for position in positions:
            if str(position.get("trading_mode") or "").strip():
                continue
            updated_at = _parse_iso(position.get("updated_at"))
            if session_start is None or (updated_at is not None and updated_at >= session_start):
                legacy_positions.append(position)
        return legacy_positions

    def get_stats(self) -> dict[str, float]:
        rows = self.conn.execute("SELECT key, value FROM portfolio_stats").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def get_trades(
        self,
        limit: int = 50,
        *,
        scope_current: bool = True,
        session_scoped: bool = True,
        economic_statuses: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        where_sql, params = self._scoped_where_clause(
            time_column="exit_time",
            scope_current=scope_current,
            session_scoped=session_scoped,
        )
        normalized_statuses = tuple(
            str(status).strip().upper()
            for status in (economic_statuses or ())
            if str(status).strip()
        )
        if normalized_statuses:
            placeholders = ", ".join("?" for _ in normalized_statuses)
            where_sql += (
                " AND " if where_sql else "WHERE "
            ) + f"UPPER(COALESCE(economic_status, '')) IN ({placeholders})"
            params.extend(normalized_statuses)
        rows = self.conn.execute(
            f"SELECT * FROM trade_history {where_sql} ORDER BY id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return self._rows_to_dicts(rows)

    def get_pnl_attribution(
        self,
        *,
        scope_current: bool = True,
        session_scoped: bool = True,
    ) -> dict[str, Any]:
        where_sql, params = self._scoped_where_clause(
            time_column="exit_time",
            scope_current=scope_current,
            session_scoped=session_scoped,
        )
        row = self.conn.execute(
            f"""
            SELECT
                COALESCE(SUM(CASE WHEN UPPER(economic_status) = 'RECONCILED'
                    THEN funding_collected ELSE 0.0 END), 0.0) AS total_funding,
                COALESCE(SUM(CASE WHEN UPPER(economic_status) = 'RECONCILED'
                    THEN basis_pnl_usd ELSE 0.0 END), 0.0) AS total_basis_pnl,
                COALESCE(SUM(CASE WHEN UPPER(economic_status) = 'RECONCILED'
                    THEN borrow_cost_usd ELSE 0.0 END), 0.0) AS total_borrow_cost,
                COALESCE(SUM(CASE WHEN UPPER(economic_status) = 'RECONCILED'
                    THEN execution_cost_usd ELSE 0.0 END), 0.0) AS total_execution_cost,
                COALESCE(SUM(CASE WHEN UPPER(economic_status) = 'RECONCILED'
                    THEN net_pnl_usd ELSE 0.0 END), 0.0) AS total_net_pnl,
                SUM(CASE WHEN UPPER(economic_status) = 'RECONCILED' THEN 1 ELSE 0 END)
                    AS trade_count,
                SUM(CASE WHEN UPPER(economic_status) = 'MODELED' THEN 1 ELSE 0 END)
                    AS modeled_trade_count,
                SUM(CASE WHEN UPPER(economic_status) = 'INCOMPLETE' THEN 1 ELSE 0 END)
                    AS incomplete_trade_count,
                COALESCE(SUM(CASE WHEN UPPER(economic_status) = 'MODELED'
                    THEN net_pnl_usd ELSE 0.0 END), 0.0) AS modeled_net_pnl,
                COALESCE(SUM(CASE WHEN UPPER(economic_status) = 'INCOMPLETE'
                    THEN net_pnl_usd ELSE 0.0 END), 0.0) AS known_incomplete_net_pnl
            FROM trade_history
            {where_sql}
            """,
            tuple(params),
        ).fetchone()
        return dict(row) if row else {}

    def get_open_pnl_summary(self) -> dict[str, float]:
        positions = self.get_positions_for_current_mode()
        total_unrealized_pnl = 0.0
        total_exchange_unrealized_pnl = 0.0
        manual_review_count = 0

        for position in positions:
            try:
                total_unrealized_pnl += float(position.get("net_pnl_usd") or 0.0)
            except (TypeError, ValueError):
                pass
            try:
                total_exchange_unrealized_pnl += float(position.get("exchange_pnl_usd") or 0.0)
            except (TypeError, ValueError):
                pass
            if str(position.get("recovery_state") or "").strip().lower() == "manual_review":
                manual_review_count += 1

        return {
            "open_position_count": float(len(positions)),
            "manual_review_position_count": float(manual_review_count),
            "managed_open_position_count": float(max(0, len(positions) - manual_review_count)),
            "current_unrealized_pnl": total_unrealized_pnl,
            "current_exchange_unrealized_pnl": total_exchange_unrealized_pnl,
        }

    def get_risk(self) -> dict[str, Any]:
        rows = self.conn.execute("SELECT key, value FROM risk_state").fetchall()
        result: dict[str, Any] = {}
        float_keys = {
            "drawdown_pct",
            "spread_toxicity",
            "venue_latency",
            "gross_exposure",
            "telemetry_staleness_seconds",
        }
        bool_keys = {
            "kill_switch",
            "allow_new_risk",
            "is_kill_switch",
            "telemetry_connected",
            "runtime_ready",
        }
        json_list_keys = {"reasons", "preflight_checks"}
        for row in rows:
            key = row["key"]
            value = row["value"]
            if key in float_keys:
                try:
                    result[key] = float(value)
                except (TypeError, ValueError):
                    continue
                continue
            if key in bool_keys:
                result[key] = str(value).lower() == "true"
                continue
            if key in json_list_keys:
                try:
                    parsed = json.loads(value)
                except (TypeError, json.JSONDecodeError):
                    parsed = []
                result[key] = [str(item) for item in parsed] if isinstance(parsed, list) else []
                continue
            try:
                result[key] = json.loads(value)
            except (TypeError, json.JSONDecodeError):
                result[key] = value
        return result

    def get_candidate_snapshots(self, cycle_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if cycle_id is None:
            row = self.conn.execute(
                "SELECT cycle_id FROM candidate_snapshots ORDER BY snapshot_time DESC LIMIT 1"
            ).fetchone()
            cycle_id = row["cycle_id"] if row else None
        if cycle_id is None:
            return []
        rows = self.conn.execute(
            """
            SELECT * FROM candidate_snapshots
            WHERE cycle_id = ?
            ORDER BY accepted DESC, rank ASC, symbol ASC
            LIMIT ?
            """,
            (cycle_id, limit),
        ).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            data["accepted"] = bool(data["accepted"])
            data["rejection_reasons"] = json.loads(data["rejection_reasons"])
            data["metrics"] = json.loads(data["metrics_json"])
            data.pop("metrics_json", None)
            result.append(data)
        return result

    def get_opportunity_scores(self, cycle_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if cycle_id is None:
            row = self.conn.execute(
                "SELECT cycle_id FROM opportunity_scores ORDER BY score_time DESC LIMIT 1"
            ).fetchone()
            cycle_id = row["cycle_id"] if row else None
        if cycle_id is None:
            return []
        rows = self.conn.execute(
            """
            SELECT * FROM opportunity_scores
            WHERE cycle_id = ?
            ORDER BY rank ASC, symbol ASC
            LIMIT ?
            """,
            (cycle_id, limit),
        ).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            data["selected"] = bool(data["selected"])
            data["component_scores"] = json.loads(data["component_scores_json"])
            data.pop("component_scores_json", None)
            result.append(data)
        return result

    def get_feature_snapshots(self, trade_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if trade_id is None:
            rows = self.conn.execute(
                "SELECT * FROM feature_snapshots ORDER BY snapshot_time DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM feature_snapshots WHERE trade_id = ? ORDER BY snapshot_time DESC LIMIT ?",
                (trade_id, limit),
            ).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            data["features"] = json.loads(data["features_json"])
            data.pop("features_json", None)
            result.append(data)
        return result

    def get_execution_quality(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM execution_quality ORDER BY sample_time DESC LIMIT ?",
            (limit,),
        ).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            data["maker"] = bool(data["maker"])
            data["metadata"] = json.loads(data["metadata_json"])
            data.pop("metadata_json", None)
            result.append(data)
        return result

    @staticmethod
    def _decode_tca_intent(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        for field_name in (
            "requested_common_quantity",
            "submitted_common_quantity",
            "unhedged_notional_ms",
            "last_spot_gross_quantity",
            "last_perp_gross_quantity",
            "last_reference_price",
        ):
            value = data.get(field_name)
            data[field_name] = Decimal(str(value)) if value is not None else None
        for field_name in ("partial", "emergency"):
            value = data.get(field_name)
            data[field_name] = bool(value) if value is not None else None
        data["metadata"] = json.loads(str(data.pop("metadata_json") or "{}"))
        return data

    @staticmethod
    def _decode_tca_leg(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        for field_name in (
            "decision_bid",
            "decision_ask",
            "decision_mid",
            "decision_limit",
            "send_bid",
            "send_ask",
            "send_mid",
            "send_limit",
            "requested_quantity",
            "submitted_quantity",
            "gross_filled_quantity",
            "net_filled_quantity",
            "vwap",
        ):
            value = data.get(field_name)
            data[field_name] = Decimal(str(value)) if value is not None else None
        for field_name in ("partial", "emergency"):
            value = data.get(field_name)
            data[field_name] = bool(value) if value is not None else None
        data["commissions"] = {
            str(asset): Decimal(str(value))
            for asset, value in json.loads(str(data.pop("commissions_json") or "{}")).items()
        }
        data["markouts"] = json.loads(str(data.pop("markouts_json") or "{}"))
        data["metadata"] = json.loads(str(data.pop("metadata_json") or "{}"))
        return data

    def get_execution_tca(
        self,
        *,
        intent_id: str | None = None,
        cycle_id: str | None = None,
        symbol: str | None = None,
        operation: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value, transform in (
            ("intent_id", intent_id, str),
            ("cycle_id", cycle_id, str),
            ("symbol", symbol, lambda item: str(item).upper()),
            ("operation", operation, lambda item: str(item).upper()),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(transform(value))
        if start_time is not None:
            clauses.append("COALESCE(decision_time, queue_time, created_at) >= ?")
            params.append(start_time)
        if end_time is not None:
            clauses.append("COALESCE(decision_time, queue_time, created_at) <= ?")
            params.append(end_time)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"""
            SELECT * FROM execution_tca_intents {where}
            ORDER BY COALESCE(decision_time, queue_time, created_at) DESC, intent_id
            LIMIT ?
            """,
            (*params, max(0, int(limit))),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = self._decode_tca_intent(row)
            leg_rows = self.conn.execute(
                """
                SELECT * FROM execution_tca_legs
                WHERE intent_id = ? ORDER BY market, leg_id
                """,
                (item["intent_id"],),
            ).fetchall()
            item["legs"] = [self._decode_tca_leg(leg) for leg in leg_rows]
            result.append(item)
        return result

    def get_latest_execution_tca_intent(
        self,
        symbol: str,
        *,
        operation: str | None = None,
    ) -> dict[str, Any] | None:
        rows = self.get_execution_tca(
            symbol=symbol,
            operation=operation,
            limit=1,
        )
        return rows[0] if rows else None

    def get_opportunity_funnel_events(
        self,
        *,
        cycle_id: str | None = None,
        intent_id: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        limit: int = 10_000,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (("cycle_id", cycle_id), ("intent_id", intent_id)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(str(value))
        if start_time is not None:
            clauses.append("event_time >= ?")
            params.append(start_time)
        if end_time is not None:
            clauses.append("event_time <= ?")
            params.append(end_time)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"""
            SELECT * FROM opportunity_funnel_events {where}
            ORDER BY event_time, stage_ordinal, cycle_id, intent_id
            LIMIT ?
            """,
            (*params, max(0, int(limit))),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            reached = item.get("reached")
            item["reached"] = bool(reached) if reached is not None else None
            item["metadata"] = json.loads(str(item.pop("metadata_json") or "{}"))
            result.append(item)
        return result

    def summarize_opportunity_funnel(
        self,
        *,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> dict[str, dict[str, int | float | None]]:
        clauses: list[str] = []
        params: list[Any] = []
        if start_time is not None:
            clauses.append("event_time >= ?")
            params.append(start_time)
        if end_time is not None:
            clauses.append("event_time <= ?")
            params.append(end_time)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"""
            SELECT stage, stage_ordinal,
                   SUM(numerator_count) AS numerator,
                   SUM(denominator_count) AS denominator,
                   COUNT(*) AS event_count
            FROM opportunity_funnel_events {where}
            GROUP BY stage, stage_ordinal ORDER BY stage_ordinal
            """,
            tuple(params),
        ).fetchall()
        summary: dict[str, dict[str, int | float | None]] = {
            stage: {
                "numerator": 0,
                "denominator": 0,
                "event_count": 0,
                "conversion_rate": None,
            }
            for stage in OPPORTUNITY_FUNNEL_STAGES
        }
        for row in rows:
            numerator = int(row["numerator"] or 0)
            denominator = int(row["denominator"] or 0)
            summary[str(row["stage"])] = {
                "numerator": numerator,
                "denominator": denominator,
                "event_count": int(row["event_count"] or 0),
                "conversion_rate": (
                    numerator / denominator if denominator > 0 else None
                ),
            }
        return summary

    def get_shadow_decisions(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM model_shadow_decisions ORDER BY decision_time DESC LIMIT ?",
            (limit,),
        ).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            data["recommended"] = bool(data["recommended"])
            data["metadata"] = json.loads(data["metadata_json"])
            data.pop("metadata_json", None)
            result.append(data)
        return result

    def get_parameter_promotions(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM parameter_promotions ORDER BY promoted_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            data["params"] = json.loads(data["params_json"])
            data["metadata"] = json.loads(data["metadata_json"])
            data.pop("params_json", None)
            data.pop("metadata_json", None)
            result.append(data)
        return result

    def get_validation_snapshots(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM validation_snapshots ORDER BY snapshot_time DESC LIMIT ?",
            (limit,),
        ).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            data["blockers"] = json.loads(data["blockers"])
            data["metrics"] = json.loads(data["metrics_json"])
            data.pop("metrics_json", None)
            result.append(data)
        return result

    def get_execution_events(
        self,
        limit: int = 100,
        *,
        scope_current: bool = True,
        session_scoped: bool = True,
    ) -> list[dict[str, Any]]:
        where_sql, params = self._scoped_where_clause(
            time_column="event_time",
            scope_current=scope_current,
            session_scoped=session_scoped,
        )
        rows = self.conn.execute(
            f"SELECT * FROM execution_events {where_sql} ORDER BY event_time DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            try:
                data["raw_payload"] = json.loads(data["raw_payload"]) if data["raw_payload"] else None
            except json.JSONDecodeError:
                pass
            result.append(data)
        return result

    def get_candidate_snapshot(self, cycle_id: str, symbol: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT * FROM candidate_snapshots
            WHERE cycle_id = ? AND symbol = ?
            """,
            (str(cycle_id), str(symbol).upper()),
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["accepted"] = bool(data["accepted"])
        data["rejection_reasons"] = json.loads(data["rejection_reasons"])
        data["metrics"] = json.loads(data["metrics_json"])
        data.pop("metrics_json", None)
        return data

    def get_execution_decision(self, decision_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM execution_decisions WHERE decision_id = ?",
            (str(decision_id),),
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["accepted"] = bool(data["accepted"])
        data["decision_payload"] = json.loads(data["decision_payload"])
        return data

    def has_execution_quality_sample(self, sample_id: str) -> bool:
        normalized = str(sample_id or "").strip()
        if not normalized:
            return False
        row = self.conn.execute(
            "SELECT 1 FROM execution_quality WHERE sample_id = ? LIMIT 1",
            (normalized,),
        ).fetchone()
        return row is not None

    def get_economic_ledger_events(self, **filters: Any) -> list[dict[str, Any]]:
        """Read normalized events in deterministic replay order."""

        return read_economic_events(self.conn, **filters)

    def project_economic_ledger(self, **filters: Any) -> EconomicLedgerProjection:
        """Replay normalized events into exact balance/inventory/PnL deltas."""

        return _project_economic_ledger(self.conn, **filters)

    def reconcile_economic_ledger(self, **reconciliation_fields: Any) -> LedgerReconciliation:
        """Compare replayed balance changes with an exchange balance snapshot."""

        return _reconcile_economic_ledger(self.conn, **reconciliation_fields)

    def get_exchange_statement_entries(self, **filters: Any) -> list[dict[str, Any]]:
        """Read immutable exchange statement evidence in replay order."""

        return read_exchange_statement_entries(self.conn, **filters)

    def get_exchange_statement_cursor(
        self,
        *,
        venue: str,
        account_id: str,
        statement_source: str,
    ) -> dict[str, Any] | None:
        """Read the monotonic high-water mark for one statement source."""

        return read_exchange_statement_cursor(
            self.conn,
            venue=venue,
            account_id=account_id,
            statement_source=statement_source,
        )

    def get_execution_events_since(
        self,
        start_time: str,
        end_time: str | None = None,
        limit: int = 100,
        *,
        scope_current: bool = True,
        session_scoped: bool = True,
    ) -> list[dict[str, Any]]:
        where_sql, params = self._scoped_where_clause(
            time_column="event_time",
            scope_current=scope_current,
            session_scoped=session_scoped,
        )
        if end_time is None:
            rows = self.conn.execute(
                f"""
                SELECT * FROM execution_events
                {where_sql if where_sql else 'WHERE'}
                {' AND ' if where_sql else ' '}event_time >= ?
                ORDER BY event_time DESC LIMIT ?
                """,
                (*params, start_time, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                f"""
                SELECT * FROM execution_events
                {where_sql if where_sql else 'WHERE'}
                {' AND ' if where_sql else ' '}event_time >= ? AND event_time <= ?
                ORDER BY event_time DESC LIMIT ?
                """,
                (*params, start_time, end_time, limit),
            ).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            try:
                data["raw_payload"] = json.loads(data["raw_payload"]) if data["raw_payload"] else None
            except json.JSONDecodeError:
                pass
            result.append(data)
        return result

    def get_execution_command_outbox(
        self,
        *,
        intent_id: str | None = None,
        state: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if intent_id is not None:
            clauses.append("intent_id = ?")
            params.append(intent_id)
        if state is not None:
            clauses.append("state = ?")
            params.append(state.upper())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"""
            SELECT * FROM execution_command_outbox
            {where}
            ORDER BY producer_id, sequence
            """,
            tuple(params),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["envelope"] = json.loads(str(item.pop("envelope_json")))
            result.append(item)
        return result

    def get_health_samples(
        self,
        metric: str | None = None,
        symbol: str | None = None,
        since: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if metric is not None:
            conditions.append("metric = ?")
            params.append(metric)
        if symbol is not None:
            conditions.append("symbol = ?")
            params.append(symbol)
        if since is not None:
            conditions.append("sample_time >= ?")
            params.append(since)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self.conn.execute(
            f"SELECT * FROM health_samples {where} ORDER BY sample_time DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return self._rows_to_dicts(rows)

    def get_market_samples(
        self,
        symbol: str | None = None,
        since: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if symbol is not None:
            conditions.append("symbol = ?")
            params.append(symbol)
        if since is not None:
            conditions.append("sample_minute >= ?")
            params.append(since)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self.conn.execute(
            f"SELECT * FROM market_samples {where} ORDER BY sample_minute DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return self._rows_to_dicts(rows)

    def get_market_hourly_aggregates(
        self,
        symbol: str | None = None,
        since: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if symbol is not None:
            conditions.append("symbol = ?")
            params.append(symbol)
        if since is not None:
            conditions.append("bucket_hour >= ?")
            params.append(since)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self.conn.execute(
            "SELECT * FROM market_hourly_aggregates "
            f"{where} ORDER BY bucket_hour DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return self._rows_to_dicts(rows)

    def get_pending_intents(
        self,
        status: str | None = None,
        statuses: list[str] | None = None,
        limit: int = 100,
        *,
        include_tombstones: bool = False,
        lifecycle_states: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if lifecycle_states:
            lifecycle_placeholders = ", ".join("?" for _ in lifecycle_states)
            conditions.append(f"lifecycle_state IN ({lifecycle_placeholders})")
            params.extend(str(item).upper() for item in lifecycle_states)
        elif not include_tombstones:
            conditions.append("lifecycle_state = ?")
            params.append(ACTIVE_PENDING_INTENT_STATE)
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            conditions.append(f"status IN ({placeholders})")
            params.extend(statuses)
        elif status is not None:
            conditions.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self.conn.execute(
            f"SELECT * FROM pending_intents {where} "
            "ORDER BY updated_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            try:
                data["metadata"] = json.loads(data["metadata"]) if data["metadata"] else {}
            except json.JSONDecodeError:
                data["metadata"] = {}
            result.append(data)
        return result

    def get_latest_account_truth(
        self,
        *,
        account_id: str | None = None,
        environment: str | None = None,
        now: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the latest venue-separated truth with freshness re-evaluated."""

        conditions: list[str] = []
        params: list[Any] = []
        if account_id is not None:
            conditions.append("account_id = ?")
            params.append(str(account_id))
        if environment is not None:
            conditions.append("environment = ?")
            params.append(str(environment))
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        row = self.conn.execute(
            "SELECT * FROM account_truth_snapshots "
            f"{where} ORDER BY created_at DESC, rowid DESC, "
            "availability_time DESC LIMIT 1",
            tuple(params),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        decode_failed = False
        for source, target, fallback in (
            ("missing_fields_json", "missing_fields", []),
            ("standard_spot_json", "standard_spot", {}),
            ("usd_m_futures_json", "usd_m_futures", {}),
            ("raw_snapshot_json", "raw_snapshot", {}),
        ):
            try:
                result[target] = json.loads(str(result.pop(source)))
            except (json.JSONDecodeError, TypeError):
                result.pop(source, None)
                result[target] = fallback
                decode_failed = True

        hash_payload = {
            "schema_version": int(result.get("schema_version") or 0),
            "account_id": str(result.get("account_id") or ""),
            "environment": str(result.get("environment") or ""),
            "captured_at": result.get("captured_at"),
            "availability_time": result.get("availability_time"),
            "expires_at": result.get("expires_at"),
            "status": str(result.get("status") or "UNKNOWN"),
            "standard_spot_status": str(
                result.get("standard_spot_status") or "UNKNOWN"
            ),
            "usd_m_futures_status": str(
                result.get("usd_m_futures_status") or "UNKNOWN"
            ),
            "missing_fields": result.get("missing_fields", []),
            "standard_spot": result.get("standard_spot", {}),
            "usd_m_futures": result.get("usd_m_futures", {}),
            "raw_snapshot": result.get("raw_snapshot", {}),
        }
        observed_hash = hashlib.sha256(
            json.dumps(
                hash_payload,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        integrity_valid = not decode_failed and observed_hash == str(
            result.get("content_hash") or ""
        )

        stored_status = str(result.get("status") or "UNKNOWN").upper()
        effective_status = stored_status if integrity_valid else "UNKNOWN"
        observed = _parse_iso(now) or datetime.now(timezone.utc)
        expiry = _parse_iso(result.get("expires_at"))
        if stored_status == "COMPLETE" and (expiry is None or observed > expiry):
            effective_status = "STALE" if expiry is not None else "UNKNOWN"
        result["stored_status"] = stored_status
        result["integrity_valid"] = integrity_valid
        result["status"] = effective_status
        for key in ("standard_spot_status", "usd_m_futures_status"):
            stored_venue_status = str(result.get(key) or "UNKNOWN").upper()
            result[f"stored_{key}"] = stored_venue_status
            if stored_venue_status == "COMPLETE" and effective_status in {
                "STALE",
                "UNKNOWN",
            }:
                result[key] = effective_status
        result["ready"] = effective_status == "COMPLETE"
        return result

    def get_pending_intent(
        self,
        intent_id: str,
        *,
        include_tombstone: bool = True,
    ) -> dict[str, Any] | None:
        conditions = ["intent_id = ?"]
        params: list[Any] = [str(intent_id)]
        if not include_tombstone:
            conditions.append("lifecycle_state = ?")
            params.append(ACTIVE_PENDING_INTENT_STATE)
        row = self.conn.execute(
            "SELECT * FROM pending_intents WHERE " + " AND ".join(conditions),
            tuple(params),
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        try:
            data["metadata"] = json.loads(data["metadata"]) if data["metadata"] else {}
        except json.JSONDecodeError:
            data["metadata"] = {}
        return data

    def find_pending_intent_tombstone(
        self,
        *,
        symbol: str,
        intent_id: str | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any] | None:
        conditions = ["symbol = ?", "lifecycle_state != ?"]
        params: list[Any] = [str(symbol).upper(), ACTIVE_PENDING_INTENT_STATE]
        identity_conditions: list[str] = []
        if str(intent_id or "").strip():
            identity_conditions.append("intent_id = ?")
            params.append(str(intent_id).strip())
        if str(client_order_id or "").strip():
            identity_conditions.append("client_order_id = ?")
            params.append(str(client_order_id).strip())
        if not identity_conditions:
            return None
        conditions.append("(" + " OR ".join(identity_conditions) + ")")
        row = self.conn.execute(
            "SELECT * FROM pending_intents WHERE "
            + " AND ".join(conditions)
            + " ORDER BY updated_at DESC LIMIT 1",
            tuple(params),
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        try:
            data["metadata"] = json.loads(data["metadata"]) if data["metadata"] else {}
        except json.JSONDecodeError:
            data["metadata"] = {}
        return data

    def get_partial_exit_lifecycle_events(
        self,
        symbol: str,
        *,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return hash-verified partial-exit evidence in causal order."""

        clauses = ["event_type = 'PARTIAL_EXIT_FILLED'", "symbol = ?"]
        params: list[Any] = [symbol.upper()]
        if start_time is not None:
            clauses.append("event_time >= ?")
            params.append(start_time)
        if end_time is not None:
            clauses.append("event_time <= ?")
            params.append(end_time)
        rows = self.conn.execute(
            "SELECT event_key, intent_id, event_time, content_hash, payload_json "
            "FROM lifecycle_events WHERE "
            + " AND ".join(clauses)
            + " ORDER BY event_time, event_key",
            tuple(params),
        ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            payload_json = str(row["payload_json"])
            if hashlib.sha256(payload_json.encode("utf-8")).hexdigest() != str(
                row["content_hash"]
            ):
                raise LifecycleRebuildError(
                    f"lifecycle content hash mismatch: {row['event_key']}"
                )
            try:
                payload = json.loads(payload_json)
            except json.JSONDecodeError as exc:
                raise LifecycleRebuildError(
                    f"invalid lifecycle payload: {row['event_key']}"
                ) from exc
            if not isinstance(payload, dict):
                raise LifecycleRebuildError(
                    f"lifecycle payload is not an object: {row['event_key']}"
                )
            remaining = payload.get("remaining_position")
            evidence = payload.get("evidence")
            if not isinstance(remaining, dict) or not isinstance(evidence, dict):
                raise LifecycleRebuildError(
                    f"partial exit lifecycle payload is incomplete: {row['event_key']}"
                )
            events.append(
                {
                    "event_key": str(row["event_key"]),
                    "intent_id": str(row["intent_id"]),
                    "event_time": str(row["event_time"]),
                    "remaining_position": dict(remaining),
                    "evidence": dict(evidence),
                }
            )
        return events

    def get_latest_validation_snapshot(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM validation_snapshots ORDER BY snapshot_time DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["blockers"] = json.loads(item["blockers"])
        item["metrics_json"] = json.loads(item["metrics_json"])
        return item

    def get_ai_report_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM ai_report_proposals WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        try:
            item["proposed_changes"] = json.loads(item.get("proposed_changes") or "{}")
        except (json.JSONDecodeError, TypeError):
            item["proposed_changes"] = {}
        return item

    def get_ai_report_proposals(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        params: list[Any] = []
        sql = "SELECT * FROM ai_report_proposals"
        if status is not None:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, tuple(params)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["proposed_changes"] = json.loads(item.get("proposed_changes") or "{}")
            except (json.JSONDecodeError, TypeError):
                item["proposed_changes"] = {}
            result.append(item)
        return result

    def estimate_trade_execution_cost(self, symbol: str, start_time: str, end_time: str) -> float:
        """Return exchange-reported commissions for all fills in a trade window.

        Binance's ``commission`` field is incremental for each TRADE execution,
        even when another quantity field in the same update is cumulative.  A
        completed order can therefore have commission-bearing
        ``PARTIALLY_FILLED`` rows followed by one final commission-bearing
        ``FILLED`` row; restricting this query to the terminal row loses the
        earlier fees.

        ``FILLED_CYCLE``/``PAPER_FILL``/``RECONCILED_FLAT`` events are local
        lifecycle summaries rather than additional exchange fills.  Excluding
        them prevents a future summary carrying cumulative commission from
        double-counting the underlying leg executions.
        """
        rows = self.conn.execute(
            """
            SELECT commission, commission_asset, avg_fill_price, last_fill_price
            FROM execution_events
            WHERE symbol = ?
              AND status IN ('PARTIALLY_FILLED', 'FILLED')
              AND event_name = 'OrderUpdate'
              AND COALESCE(commission, 0.0) > 0.0
              AND UPPER(COALESCE(execution_type, '')) NOT IN
                  ('FILLED_CYCLE', 'PAPER_FILL', 'RECONCILED_FLAT')
              AND event_time >= ?
              AND event_time <= ?
            """,
            (symbol, start_time, end_time),
        ).fetchall()
        total = 0.0
        base_asset = symbol.replace("USDT", "")
        stable_quote_assets = {"USDT", "USDC", "FDUSD", "BUSD"}
        for row in rows:
            commission = float(row["commission"] or 0.0)
            asset = str(row["commission_asset"] or "").upper()
            fill_price = float(row["avg_fill_price"] or row["last_fill_price"] or 0.0)
            if commission <= 0.0:
                continue
            if asset in stable_quote_assets or asset == "":
                total += commission
            elif asset == base_asset and fill_price > 0.0:
                total += commission * fill_price
            elif asset:
                quote_price = self._latest_market_price(f"{asset}USDT")
                if quote_price > 0.0:
                    total += commission * quote_price
        return total

    def get_trade_execution_cost_evidence(
        self,
        symbol: str,
        start_time: str,
        end_time: str,
    ) -> dict[str, Any]:
        """Return commission completeness as well as its converted USD cost.

        A numeric zero is valid only when every incremental exchange TRADE row
        explicitly reported a commission.  Missing commission fields and fees
        in an asset without a USD conversion remain incomplete evidence.
        """

        rows = self.conn.execute(
            """
            SELECT commission, commission_asset, avg_fill_price, last_fill_price
            FROM execution_events
            WHERE symbol = ?
              AND status IN ('PARTIALLY_FILLED', 'FILLED')
              AND event_name = 'OrderUpdate'
              AND UPPER(COALESCE(execution_type, '')) = 'TRADE'
              AND event_time >= ?
              AND event_time <= ?
            """,
            (symbol, start_time, end_time),
        ).fetchall()
        total = 0.0
        reported = 0
        unvalued = 0
        base_asset = symbol.replace("USDT", "")
        stable_quote_assets = {"USDT", "USDC", "FDUSD", "BUSD", "USD"}
        for row in rows:
            if row["commission"] is None:
                continue
            reported += 1
            try:
                commission = float(row["commission"] or 0.0)
            except (TypeError, ValueError):
                unvalued += 1
                continue
            if commission < 0.0:
                unvalued += 1
                continue
            asset = str(row["commission_asset"] or "").upper()
            fill_price = float(row["avg_fill_price"] or row["last_fill_price"] or 0.0)
            if commission == 0.0:
                continue
            if asset in stable_quote_assets or asset == "":
                total += commission
            elif asset == base_asset and fill_price > 0.0:
                total += commission * fill_price
            elif asset:
                quote_price = self._latest_market_price(f"{asset}USDT")
                if quote_price > 0.0:
                    total += commission * quote_price
                else:
                    unvalued += 1
            else:
                unvalued += 1
        return {
            "exchange_fill_event_count": len(rows),
            "commission_report_count": reported,
            "unvalued_commission_count": unvalued,
            "execution_cost_usd": total,
            "complete": bool(rows) and reported == len(rows) and unvalued == 0,
        }

    def get_trade_economic_cashflows(
        self,
        symbol: str,
        start_time: str,
        end_time: str,
    ) -> dict[str, Any]:
        rows = self.conn.execute(
            """
            SELECT event_type, amount, amount_asset, amount_usd
            FROM economic_ledger_events
            WHERE symbol = ? AND event_time >= ? AND event_time <= ?
            ORDER BY event_time, id
            """,
            (symbol.upper(), start_time, end_time),
        ).fetchall()
        totals: dict[str, float] = {}
        counts: dict[str, int] = {}
        unvalued: dict[str, int] = {}
        for row in rows:
            event_type = str(row["event_type"] or "").upper()
            counts[event_type] = counts.get(event_type, 0) + 1
            amount_usd = row["amount_usd"]
            if amount_usd is None:
                asset = str(row["amount_asset"] or "").upper()
                if asset in {"USDT", "USDC", "FDUSD", "BUSD", "USD"}:
                    amount_usd = row["amount"]
            if amount_usd is None:
                unvalued[event_type] = unvalued.get(event_type, 0) + 1
                continue
            totals[event_type] = totals.get(event_type, 0.0) + float(amount_usd)
        return {"totals_usd": totals, "counts": counts, "unvalued": unvalued}

    def get_trade_funding_cashflows(
        self,
        symbol: str,
        start_time: str,
        end_time: str,
        *,
        scope_current: bool = False,
    ) -> list[dict[str, Any]]:
        where_sql, params = self._scoped_where_clause(
            time_column="event_time",
            scope_current=scope_current,
        )
        ledger_rows = self.conn.execute(
            f"""
            SELECT *
            FROM economic_ledger_events
            {where_sql if where_sql else 'WHERE'}
            {' AND ' if where_sql else ' '}symbol = ?
              AND event_type = 'FUNDING'
              AND event_time >= ?
              AND event_time <= ?
            ORDER BY event_time ASC, id ASC
            """,
            (*params, symbol, start_time, end_time),
        ).fetchall()
        if ledger_rows:
            return self._rows_to_dicts(ledger_rows)

        # Compatibility for pre-ledger databases.  New statement/fill paths
        # must use the immutable economic ledger so funding cannot disappear
        # merely because the optional execution projection was absent.
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM execution_events
            {where_sql if where_sql else 'WHERE'}
            {' AND ' if where_sql else ' '}symbol = ?
              AND event_name = 'FundingFee'
              AND event_time >= ?
              AND event_time <= ?
            ORDER BY event_time ASC
            """,
            (*params, symbol, start_time, end_time),
        ).fetchall()
        return self._rows_to_dicts(rows)

    def get_account_equity(self) -> float | None:
        stats = self.get_stats()
        risk = self.get_risk()
        for value in (risk.get("account_equity"), stats.get("account_equity")):
            try:
                if value is not None:
                    return float(value)
            except (TypeError, ValueError):
                continue
        return None

    def get_db_stats(self) -> dict[str, Any]:
        """Return row counts per table and approximate database file size."""
        tables = [
            "positions",
            "trade_history",
            "execution_events",
            "economic_ledger_events",
            "exchange_statement_entries",
            "exchange_statement_cursors",
            "candidate_snapshots",
            "opportunity_scores",
            "feature_snapshots",
            "market_samples",
            "market_hourly_aggregates",
            "health_samples",
            "pending_intents",
            "account_truth_snapshots",
            "execution_quality",
            "execution_tca_intents",
            "execution_tca_legs",
            "opportunity_funnel_events",
            "model_shadow_decisions",
            "validation_snapshots",
        ]
        stats: dict[str, Any] = {}
        for table in tables:
            try:
                row = self.conn.execute(f"SELECT COUNT(*) as count FROM {table}").fetchone()
                stats[f"{table}_count"] = row["count"] if row else 0
            except sqlite3.Error:
                stats[f"{table}_count"] = -1

        try:
            db_path = self.conn.execute("PRAGMA database_list").fetchone()[2]
            if db_path:
                stats["db_size_bytes"] = Path(db_path).stat().st_size
        except (sqlite3.Error, IndexError, OSError):
            stats["db_size_bytes"] = -1

        return stats

    def close(self) -> None:
        self.conn.close()
