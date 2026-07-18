"""SQLite-backed observability, runtime, and governance state store."""

from __future__ import annotations

import json
import hashlib
import logging
import sqlite3
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, Mapping

from bongus.core.config import STATE_DB_PATH
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
from bongus.engine.economic_ledger import (
    project_economic_ledger as _project_economic_ledger,
)
from bongus.engine.economic_ledger import (
    reconcile_economic_ledger as _reconcile_economic_ledger,
)

try:
    import orjson as _orjson  # pyright: ignore[reportMissingImports]

    def _json_dump(value: Any) -> str:
        return _orjson.dumps(value).decode()
except ModuleNotFoundError:  # graceful fallback if orjson not installed
    def _json_dump(value: Any) -> str:  # type: ignore[misc]
        return json.dumps(value)

DB_PATH = STATE_DB_PATH
CURRENT_SCHEMA_VERSION = 12


class LifecycleRebuildError(RuntimeError):
    """Raised when immutable lifecycle evidence cannot prove a projection rebuild."""


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


def _connect(
    db_path: str = DB_PATH,
    *,
    readonly: bool = False,
    migrate: bool | None = None,
) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    # Reduce WAL checkpoint pressure, grow page cache to ~8 MB, keep temp
    # tables in memory so cycle writes don't hit the filesystem unnecessarily.
    conn.execute("PRAGMA wal_autocheckpoint=400")
    conn.execute("PRAGMA cache_size=-8000")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
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
            updated_at      TEXT NOT NULL
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_opportunity_scores_time ON opportunity_scores(score_time DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_feature_snapshots_trade ON feature_snapshots(trade_id, snapshot_time DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_execution_quality_symbol ON execution_quality(symbol, sample_time DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shadow_decisions_symbol ON model_shadow_decisions(symbol, decision_time DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_promotions_time ON parameter_promotions(promoted_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_health_samples_time ON health_samples(sample_time DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_market_samples_time ON market_samples(sample_minute DESC)")
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
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trade_history_scope ON trade_history(trading_mode, session_id, exit_time DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_execution_events_scope ON execution_events(trading_mode, session_id, event_time DESC)"
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
        "CREATE INDEX IF NOT EXISTS idx_lifecycle_events_symbol_time "
        "ON lifecycle_events(symbol, event_time, event_type)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_quality_sample_id "
        "ON execution_quality(sample_id) WHERE sample_id != ''"
    )
    conn.execute(
        """
        INSERT INTO schema_meta (key, value)
        VALUES ('schema_version', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (str(CURRENT_SCHEMA_VERSION),),
    )
    conn.commit()


class StateWriter:
    def __init__(self, db_path: str = DB_PATH, *, migrate: bool = True) -> None:
        self.conn = _connect(db_path, migrate=migrate)
        self._economic_ledger_lock = RLock()
        self._exchange_statement_lock = RLock()
        self._lifecycle_lock = RLock()
        self._command_outbox_lock = RLock()
        db_identifier = str(db_path)
        embedded_connection = (
            db_identifier == ":memory:" or db_identifier.startswith("file:")
        )
        # Keep command durability isolated from cycle/config writes performed
        # through ``self.conn`` on other threads.  A transport send must never
        # race an unrelated commit on the shared runtime connection.
        self._owns_command_connection = not embedded_connection
        self._command_conn = (
            _connect(db_path, migrate=False)
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
            _connect(db_path, migrate=False)
            if self._owns_statement_connection
            else self.conn
        )
        # Recovery guards commit immediately and may be activated from
        # telemetry/config callbacks.  Give each subsystem its own connection
        # so their transactions cannot commit or roll back an unrelated cycle
        # batch on ``self.conn`` (or one another).
        self._guard_lock = RLock()
        self._owns_guard_connections = not embedded_connection
        if self._owns_guard_connections:
            self._cooldown_conn = _connect(db_path, migrate=False)
            self._feed_recovery_conn = _connect(db_path, migrate=False)
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
        try:
            # Safety: archive to archive.db before clearing
            db_path = self.conn.execute("PRAGMA database_list").fetchone()[2]
            archive_db_path = str(Path(db_path).with_name("archive.db"))
            self.archive_old_data(archive_db_path=archive_db_path, retention_days=0)
        except Exception as e:
            logging.error(f"Failed to archive before clearing trade history: {e}")
        self.conn.execute("DELETE FROM trade_history")
        self.conn.commit()

    def clear_execution_events(self) -> None:
        self.conn.execute("DELETE FROM execution_events")
        self.conn.commit()

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
                 config_version_hash, event_name, asset, amount, reason,
                 trading_mode, runtime_mode, session_id, event_time, raw_payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    def record_execution_event(self, payload: dict[str, Any]) -> None:
        self._insert_execution_event(payload)
        self.conn.commit()

    def record_execution_and_economic_fill(
        self,
        payload: dict[str, Any],
        economic_fields: Mapping[str, Any],
    ) -> LedgerIngestionResult:
        """Atomically persist raw execution evidence and normalized economics.

        The append-only execution row is intentionally retained even when the
        normalized economic event is an exact replay.  A conflicting stable
        exchange identity rolls both writes back, so lifecycle code can never
        observe a telemetry fill without its economic counterpart.
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
    ) -> dict[str, int]:
        if archive_db_path is None:
            db_path = self.conn.execute("PRAGMA database_list").fetchone()[2]
            archive_db_path = str(Path(db_path).with_name("archive.db"))

        # Snapshots and features can be very high-volume; allow shorter retention for main DB
        # if not explicitly provided, default to retention_days.
        snap_days = snapshot_retention_days if snapshot_retention_days is not None else retention_days
        feat_days = feature_retention_days if feature_retention_days is not None else retention_days

        archive_conn = _connect(archive_db_path)
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
            ("model_shadow_decisions", "decision_time", retention_days, ""),
            ("validation_snapshots", "snapshot_time", retention_days, ""),
            (
                "pending_intents",
                "updated_at",
                retention_days,
                "AND status IN ('FILLED', 'REJECTED', 'CANCELED', 'FAILED')",
            ),
        ]

        results = {}
        for table_name, time_col, days, extra_where in configs:
            cutoff = _get_cutoff(days)
            where_clause = f"{time_col} < ? {extra_where}".strip()

            # Ensure table exists in archive - use the same migration logic
            # _connect(archive_db_path) already called _apply_migrations(archive_conn)

            rows = self.conn.execute(
                f"SELECT * FROM {table_name} WHERE {where_clause}",
                (cutoff,),
            ).fetchall()

            if rows:
                columns_info = self.conn.execute(f"PRAGMA table_info({table_name})").fetchall()
                col_names = [col["name"] for col in columns_info]
                col_sql = ", ".join(col_names)
                placeholders = ", ".join(["?"] * len(col_names))

                # Batch inserts into archive and deletes from main
                try:
                    archive_conn.executemany(
                        f"INSERT OR IGNORE INTO {table_name} ({col_sql}) VALUES ({placeholders})",
                        [tuple(row) for row in rows],
                    )
                    self.conn.execute(f"DELETE FROM {table_name} WHERE {where_clause}", (cutoff,))
                except sqlite3.OperationalError as e:
                    # Archive might be missing a column if schema changed
                    logging.error(f"Archival failed for {table_name}: {e}")
                    pass

            results[f"{table_name}_archived"] = len(rows)

        self.conn.commit()
        archive_conn.commit()
        archive_conn.close()
        return results

    def maintenance(self, run_vacuum: bool = False) -> None:
        """Perform database maintenance: WAL checkpoint, incremental vacuum, and optionally full VACUUM."""
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            # Reclaim some pages if auto_vacuum=INCREMENTAL
            self.conn.execute("PRAGMA incremental_vacuum(1000)")
            if run_vacuum:
                self.conn.execute("VACUUM")
        except sqlite3.Error:
            pass

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
            )
        now = _now()
        self.conn.execute(
            """
            INSERT INTO pending_intents
                (intent_id, symbol, intent_type, direction, status, quantity, notional_usd,
                 client_order_id, retry_count, last_error, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    def delete_pending_intent(self, intent_id: str, *, commit: bool = True) -> None:
        self.conn.execute("DELETE FROM pending_intents WHERE intent_id = ?", (intent_id,))
        if commit:
            self.conn.commit()

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

    def project_entry_lifecycle(
        self,
        *,
        event_key: str,
        intent_id: str,
        event_time: str,
        position_fields: Mapping[str, Any],
        evidence: Mapping[str, Any],
    ) -> bool:
        """Atomically claim an entry event, open its position and clear intent."""

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
                        self.delete_pending_intent(intent_id, commit=False)
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
        """Atomically claim an exit, append one trade, flatten and clear intent."""

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
                        self.delete_pending_intent(intent_id, commit=False)
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
                    Decimal(str(position.get("hedge_ratio", "0"))).normalize()
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
                    else:
                        self.record_trade(Trade(**dict(payload["trade"])), commit=False)
                        self.remove_position(str(payload["trade"]["symbol"]), commit=False)
                    if intent_id:
                        self.delete_pending_intent(intent_id, commit=False)
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
                f"WHERE ({time_column} >= ? AND (LOWER(COALESCE(trading_mode, '')) = ? OR COALESCE(trading_mode, '') = ''))",
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

    def get_pending_intents(
        self,
        status: str | None = None,
        statuses: list[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            rows = self.conn.execute(
                f"SELECT * FROM pending_intents WHERE status IN ({placeholders}) ORDER BY updated_at DESC LIMIT ?",
                (*statuses, limit),
            ).fetchall()
        elif status is not None:
            rows = self.conn.execute(
                "SELECT * FROM pending_intents WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM pending_intents ORDER BY updated_at DESC LIMIT ?",
                (limit,),
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
            "health_samples",
            "pending_intents",
            "execution_quality",
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
