"""
SQLite-backed shared state store for cross-process communication.

StateWriter: used by live_trader.py to persist positions, trades, stats, risk.
StateReader: used by web_dashboard.py to serve REST API endpoints.

Uses WAL journal mode for concurrent readers + single writer.
"""

import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass
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

DB_PATH = "state.db"
DEFAULT_ARCHIVE_DB_PATH = os.path.join("data", "archive", "archive.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    symbol        TEXT PRIMARY KEY,
    side          TEXT NOT NULL,
    direction     TEXT NOT NULL DEFAULT 'long',
    spot_entry    REAL NOT NULL,
    perp_entry    REAL NOT NULL,
    spot_live     REAL DEFAULT 0.0,
    perp_live     REAL DEFAULT 0.0,
    qty           REAL NOT NULL,
    ann_funding   REAL DEFAULT 0.0,
    entry_ann_funding REAL DEFAULT 0.0,
    basis_pct     REAL DEFAULT 0.0,
    net_pnl_usd   REAL DEFAULT 0.0,
    status        TEXT DEFAULT 'OPEN',
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS portfolio_stats (
    key        TEXT PRIMARY KEY,
    value      REAL NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trade_history (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol            TEXT NOT NULL,
    side              TEXT NOT NULL,
    entry_time        TEXT NOT NULL,
    exit_time         TEXT NOT NULL,
    entry_price       REAL NOT NULL,
    exit_price        REAL NOT NULL,
    qty               REAL NOT NULL,
    net_pnl_usd       REAL NOT NULL,
    funding_collected REAL DEFAULT 0.0,
    execution_cost_usd REAL DEFAULT 0.0,
    basis_pnl_usd     REAL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS risk_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS execution_events (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol               TEXT NOT NULL,
    client_order_id      TEXT NOT NULL,
    status               TEXT NOT NULL,
    filled_qty           REAL DEFAULT 0.0,
    avg_fill_price       REAL,
    last_fill_price      REAL,
    cumulative_quote_qty REAL,
    commission           REAL,
    commission_asset     TEXT,
    realized_pnl         REAL,
    maker                INTEGER,
    execution_type       TEXT,
    event_time           TEXT NOT NULL,
    raw_payload          TEXT
);

CREATE INDEX IF NOT EXISTS idx_execution_events_symbol_time
    ON execution_events(symbol, event_time DESC);

CREATE INDEX IF NOT EXISTS idx_execution_events_client_order
    ON execution_events(client_order_id, event_time DESC);

CREATE TABLE IF NOT EXISTS pending_intents (
    intent_id          TEXT PRIMARY KEY,
    symbol             TEXT NOT NULL,
    intent_type        TEXT NOT NULL,
    direction          TEXT NOT NULL DEFAULT '',
    status             TEXT NOT NULL,
    quantity           REAL DEFAULT 0.0,
    notional_usd       REAL DEFAULT 0.0,
    client_order_id    TEXT,
    retry_count        INTEGER DEFAULT 0,
    last_error         TEXT,
    metadata           TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pending_intents_symbol_status
    ON pending_intents(symbol, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS market_samples (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_minute        TEXT NOT NULL,
    symbol               TEXT NOT NULL,
    ann_funding          REAL DEFAULT 0.0,
    basis_pct            REAL DEFAULT 0.0,
    mark_price           REAL DEFAULT 0.0,
    minute_notional_volume REAL DEFAULT 0.0,
    UNIQUE(symbol, sample_minute)
);

CREATE INDEX IF NOT EXISTS idx_market_samples_symbol_time
    ON market_samples(symbol, sample_minute DESC);

CREATE TABLE IF NOT EXISTS health_samples (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_time          TEXT NOT NULL,
    symbol               TEXT,
    metric               TEXT NOT NULL,
    value                REAL DEFAULT 0.0,
    expected_value       REAL DEFAULT 0.0,
    zscore               REAL,
    alert_level          TEXT DEFAULT '',
    runtime_mode         TEXT DEFAULT '',
    notes                TEXT
);

CREATE INDEX IF NOT EXISTS idx_health_samples_metric_time
    ON health_samples(metric, sample_time DESC);

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
);

CREATE TABLE IF NOT EXISTS validation_snapshots (
    snapshot_time        TEXT PRIMARY KEY,
    phase                TEXT NOT NULL,
    validation_status    TEXT NOT NULL,
    go_no_go             TEXT NOT NULL,
    observation_days     REAL DEFAULT 0.0,
    trade_count          INTEGER DEFAULT 0,
    blockers             TEXT NOT NULL,
    metrics_json         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_validation_snapshots_time
    ON validation_snapshots(snapshot_time DESC);
"""


def _connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_parent_dir(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_base_asset(symbol: str) -> str:
    for suffix in ("USDT", "USDC", "FDUSD", "BUSD", "BTC", "ETH", "BNB", "TRY", "EUR"):
        if symbol.endswith(suffix) and len(symbol) > len(suffix):
            return symbol[: -len(suffix)]
    return symbol


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row["name"]) for row in rows}


def _ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    column_sql: str,
) -> None:
    if column in _column_names(conn, table):
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_sql}")


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Apply additive schema migrations for older on-disk databases."""
    _ensure_column(
        conn,
        "positions",
        "direction",
        "direction TEXT NOT NULL DEFAULT 'long'",
    )
    _ensure_column(
        conn,
        "trade_history",
        "execution_cost_usd",
        "execution_cost_usd REAL DEFAULT 0.0",
    )
    _ensure_column(
        conn,
        "trade_history",
        "basis_pnl_usd",
        "basis_pnl_usd REAL DEFAULT 0.0",
    )
    _ensure_column(
        conn,
        "positions",
        "entry_ann_funding",
        "entry_ann_funding REAL DEFAULT 0.0",
    )
    conn.commit()


def _ensure_archive_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS trade_history (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol            TEXT NOT NULL,
            side              TEXT NOT NULL,
            entry_time        TEXT NOT NULL,
            exit_time         TEXT NOT NULL,
            entry_price       REAL NOT NULL,
            exit_price        REAL NOT NULL,
            qty               REAL NOT NULL,
            net_pnl_usd       REAL NOT NULL,
            funding_collected REAL DEFAULT 0.0,
            execution_cost_usd REAL DEFAULT 0.0,
            basis_pnl_usd     REAL DEFAULT 0.0
        );

        CREATE TABLE IF NOT EXISTS execution_events (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol               TEXT NOT NULL,
            client_order_id      TEXT NOT NULL,
            status               TEXT NOT NULL,
            filled_qty           REAL DEFAULT 0.0,
            avg_fill_price       REAL,
            last_fill_price      REAL,
            cumulative_quote_qty REAL,
            commission           REAL,
            commission_asset     TEXT,
            realized_pnl         REAL,
            maker                INTEGER,
            execution_type       TEXT,
            event_time           TEXT NOT NULL,
            raw_payload          TEXT
        );
        """
    )
    conn.commit()


def _is_missing_column_error(exc: sqlite3.OperationalError, column: str) -> bool:
    return f"no column named {column}".lower() in str(exc).lower()


class StateWriter:
    def __init__(self, db_path: str = DB_PATH) -> None:
        self.conn = _connect(db_path)
        self.conn.executescript(_SCHEMA)
        _migrate_schema(self.conn)
        # sqlite3 connections are not thread-safe even with check_same_thread=False.
        # This lock serializes all writes so a future refactor that calls StateWriter
        # from multiple threads or asyncio tasks won't silently corrupt the DB.
        self._lock = threading.RLock()

    def upsert_position(
        self,
        symbol: str,
        side: str,
        spot_entry: float,
        perp_entry: float,
        qty: float,
        ann_funding: float = 0.0,
        entry_ann_funding: float | None = None,
        basis_pct: float = 0.0,
        net_pnl_usd: float = 0.0,
        status: str = "OPEN",
        spot_live: float = 0.0,
        perp_live: float = 0.0,
        direction: str = "long",
        updated_at: str | None = None,
    ) -> None:
        timestamp = updated_at or _now()
        entry_funding = ann_funding if entry_ann_funding is None else entry_ann_funding
        with self._lock:
            params = (
                symbol,
                side,
                direction,
                spot_entry,
                perp_entry,
                spot_live,
                perp_live,
                qty,
                ann_funding,
                entry_funding,
                basis_pct,
                net_pnl_usd,
                status,
                timestamp,
            )
            sql = """INSERT INTO positions
                     (symbol, side, direction, spot_entry, perp_entry, spot_live, perp_live,
                      qty, ann_funding, entry_ann_funding, basis_pct, net_pnl_usd, status, updated_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                     ON CONFLICT(symbol) DO UPDATE SET
                       side=excluded.side, direction=excluded.direction,
                       spot_entry=excluded.spot_entry,
                       perp_entry=excluded.perp_entry, spot_live=excluded.spot_live,
                       perp_live=excluded.perp_live, qty=excluded.qty,
                       ann_funding=excluded.ann_funding,
                       entry_ann_funding=COALESCE(positions.entry_ann_funding, excluded.entry_ann_funding),
                       basis_pct=excluded.basis_pct,
                       net_pnl_usd=excluded.net_pnl_usd, status=excluded.status,
                       updated_at=excluded.updated_at"""
            try:
                self.conn.execute(sql, params)
            except sqlite3.OperationalError as exc:
                if not (
                    _is_missing_column_error(exc, "direction")
                    or _is_missing_column_error(exc, "entry_ann_funding")
                ):
                    raise
                _migrate_schema(self.conn)
                self.conn.execute(sql, params)
            self.conn.commit()

    def remove_position(self, symbol: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))
            self.conn.commit()

    def update_position_metrics(
        self,
        symbol: str,
        *,
        ann_funding: float | None = None,
        spot_live: float | None = None,
        perp_live: float | None = None,
        net_pnl_usd: float | None = None,
        updated_at: str | None = None,
    ) -> None:
        updates: list[str] = []
        params: list[float | str] = []

        if ann_funding is not None:
            updates.append("ann_funding = ?")
            params.append(ann_funding)
        if spot_live is not None:
            updates.append("spot_live = ?")
            params.append(spot_live)
        if perp_live is not None:
            updates.append("perp_live = ?")
            params.append(perp_live)
        if net_pnl_usd is not None:
            updates.append("net_pnl_usd = ?")
            params.append(net_pnl_usd)

        if not updates:
            return

        if updated_at is not None:
            updates.append("updated_at = ?")
            params.append(updated_at)
        params.append(symbol)

        with self._lock:
            self.conn.execute(
                f"UPDATE positions SET {', '.join(updates)} WHERE symbol = ?",
                tuple(params),
            )
            self.conn.commit()

    def record_trade(self, trade: Trade) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO trade_history
                   (symbol, side, entry_time, exit_time, entry_price, exit_price,
                    qty, net_pnl_usd, funding_collected, execution_cost_usd, basis_pnl_usd)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                ),
            )
            self.conn.commit()

    def set_stat(self, key: str, value: float) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO portfolio_stats (key, value, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (key, value, _now()),
            )
            self.conn.commit()

    def set_risk(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO risk_state (key, value, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (key, value, _now()),
            )
            self.conn.commit()

    def set_risk_snapshot(self, snapshot: dict) -> None:
        """Write all risk fields at once."""
        now = _now()
        data = [
            (key, json.dumps(value) if not isinstance(value, str) else value, now)
            for key, value in snapshot.items()
        ]
        with self._lock:
            self.conn.executemany(
                """INSERT INTO risk_state (key, value, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                data,
            )
            self.conn.commit()

    def record_execution_event(self, event: dict) -> None:
        """Persist a raw order/execution event for later reconciliation and replay."""
        payload = dict(event)
        event_time = str(payload.pop("event_time", _now()))
        raw_payload = json.dumps(payload)

        def _float_or_none(value):
            if value is None:
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        maker = payload.get("maker")
        maker_int = None if maker is None else int(bool(maker))

        with self._lock:
            self.conn.execute(
                """INSERT INTO execution_events
                   (symbol, client_order_id, status, filled_qty, avg_fill_price,
                    last_fill_price, cumulative_quote_qty, commission, commission_asset,
                    realized_pnl, maker, execution_type, event_time, raw_payload)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(payload.get("symbol", "")),
                    str(payload.get("client_order_id", "")),
                    str(payload.get("status", "")),
                    _float_or_none(payload.get("filled_qty")) or 0.0,
                    _float_or_none(payload.get("avg_fill_price")),
                    _float_or_none(payload.get("last_fill_price")),
                    _float_or_none(payload.get("cumulative_quote_qty")),
                    _float_or_none(payload.get("commission")),
                    payload.get("commission_asset"),
                    _float_or_none(payload.get("realized_pnl")),
                    maker_int,
                    payload.get("execution_type"),
                    event_time,
                    raw_payload,
                ),
            )
            self.conn.commit()

    def upsert_pending_intent(
        self,
        *,
        intent_id: str,
        symbol: str,
        intent_type: str,
        status: str,
        direction: str = "",
        quantity: float = 0.0,
        notional_usd: float = 0.0,
        client_order_id: str | None = None,
        retry_count: int = 0,
        last_error: str | None = None,
        metadata: dict | None = None,
        created_at: str | None = None,
        updated_at: str | None = None,
    ) -> None:
        created = created_at or _now()
        updated = updated_at or created
        metadata_json = json.dumps(metadata or {})
        with self._lock:
            self.conn.execute(
                """INSERT INTO pending_intents
                   (intent_id, symbol, intent_type, direction, status, quantity,
                    notional_usd, client_order_id, retry_count, last_error, metadata,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(intent_id) DO UPDATE SET
                     symbol=excluded.symbol,
                     intent_type=excluded.intent_type,
                     direction=excluded.direction,
                     status=excluded.status,
                     quantity=excluded.quantity,
                     notional_usd=excluded.notional_usd,
                     client_order_id=excluded.client_order_id,
                     retry_count=excluded.retry_count,
                     last_error=excluded.last_error,
                     metadata=excluded.metadata,
                     updated_at=excluded.updated_at""",
                (
                    intent_id,
                    symbol,
                    intent_type,
                    direction,
                    status,
                    quantity,
                    notional_usd,
                    client_order_id,
                    retry_count,
                    last_error,
                    metadata_json,
                    created,
                    updated,
                ),
            )
            self.conn.commit()

    def update_pending_intent(self, intent_id: str, **fields) -> None:
        if not fields:
            return
        updates: list[str] = []
        params: list[object] = []
        if "metadata" in fields:
            fields["metadata"] = json.dumps(fields["metadata"] or {})
        for key, value in fields.items():
            updates.append(f"{key} = ?")
            params.append(value)
        updates.append("updated_at = ?")
        params.append(_now())
        params.append(intent_id)
        with self._lock:
            self.conn.execute(
                f"UPDATE pending_intents SET {', '.join(updates)} WHERE intent_id = ?",
                tuple(params),
            )
            self.conn.commit()

    def delete_pending_intent(self, intent_id: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM pending_intents WHERE intent_id = ?", (intent_id,))
            self.conn.commit()

    def record_market_sample(
        self,
        *,
        symbol: str,
        sample_minute: str,
        ann_funding: float,
        basis_pct: float,
        mark_price: float,
        minute_notional_volume: float,
    ) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO market_samples
                   (sample_minute, symbol, ann_funding, basis_pct, mark_price, minute_notional_volume)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(symbol, sample_minute) DO UPDATE SET
                     ann_funding=excluded.ann_funding,
                     basis_pct=excluded.basis_pct,
                     mark_price=excluded.mark_price,
                     minute_notional_volume=excluded.minute_notional_volume""",
                (
                    sample_minute,
                    symbol,
                    ann_funding,
                    basis_pct,
                    mark_price,
                    minute_notional_volume,
                ),
            )
            self.conn.commit()

    def record_health_sample(
        self,
        *,
        metric: str,
        value: float,
        symbol: str | None = None,
        expected_value: float = 0.0,
        zscore: float | None = None,
        alert_level: str = "",
        runtime_mode: str = "",
        notes: str = "",
        sample_time: str | None = None,
    ) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO health_samples
                   (sample_time, symbol, metric, value, expected_value, zscore, alert_level, runtime_mode, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    sample_time or _now(),
                    symbol,
                    metric,
                    value,
                    expected_value,
                    zscore,
                    alert_level,
                    runtime_mode,
                    notes,
                ),
            )
            self.conn.commit()

    def record_ai_report_proposal(
        self,
        *,
        proposal_id: str,
        summary: str,
        proposed_changes: dict,
        status: str = "PENDING",
        report_period_start: str = "",
        report_period_end: str = "",
        raw_response: str = "",
    ) -> None:
        now = _now()
        with self._lock:
            self.conn.execute(
                """INSERT INTO ai_report_proposals
                   (proposal_id, created_at, report_period_start, report_period_end,
                    summary, proposed_changes, status, raw_response)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(proposal_id) DO UPDATE SET
                     summary=excluded.summary,
                     proposed_changes=excluded.proposed_changes,
                     status=excluded.status,
                     raw_response=excluded.raw_response""",
                (
                    proposal_id,
                    now,
                    report_period_start,
                    report_period_end,
                    summary,
                    json.dumps(proposed_changes),
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
        with self._lock:
            self.conn.execute(
                """UPDATE ai_report_proposals
                   SET status = ?, decision_time = ?, decision_source = ?,
                       applied_at = CASE WHEN ? THEN ? ELSE applied_at END
                   WHERE proposal_id = ?""",
                (
                    status,
                    now,
                    decision_source,
                    int(applied),
                    now,
                    proposal_id,
                ),
            )
            self.conn.commit()

    def record_validation_snapshot(
        self,
        *,
        snapshot_time: str,
        validation_status: str,
        go_no_go: str,
        observation_days: float,
        trade_count: int,
        blockers: list[str],
        metrics: dict,
        phase: str = "PHASE_4",
    ) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO validation_snapshots
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
                     metrics_json=excluded.metrics_json""",
                (
                    snapshot_time,
                    phase,
                    validation_status,
                    go_no_go,
                    observation_days,
                    trade_count,
                    json.dumps(blockers or []),
                    json.dumps(metrics or {}, ensure_ascii=True),
                ),
            )
            self.conn.commit()

    def archive_old_data(
        self,
        *,
        archive_db_path: str = DEFAULT_ARCHIVE_DB_PATH,
        retention_days: int = 90,
        market_retention_days: int = 21,
        health_retention_days: int = 21,
    ) -> dict[str, int]:
        trade_cutoff = (
            datetime.now(timezone.utc) - timedelta(days=max(retention_days, 0))
        ).isoformat()
        market_cutoff = (
            datetime.now(timezone.utc) - timedelta(days=max(market_retention_days, 0))
        ).isoformat()
        health_cutoff = (
            datetime.now(timezone.utc) - timedelta(days=max(health_retention_days, 0))
        ).isoformat()

        _ensure_parent_dir(archive_db_path)
        archive_conn = _connect(archive_db_path)
        _ensure_archive_schema(archive_conn)
        counts = {
            "trade_history_archived": 0,
            "execution_events_archived": 0,
            "market_samples_deleted": 0,
            "health_samples_deleted": 0,
        }
        try:
            with self._lock:
                old_trades = self.conn.execute(
                    "SELECT * FROM trade_history WHERE exit_time < ? ORDER BY id",
                    (trade_cutoff,),
                ).fetchall()
                if old_trades:
                    archive_conn.executemany(
                        """INSERT OR IGNORE INTO trade_history
                           (id, symbol, side, entry_time, exit_time, entry_price, exit_price,
                            qty, net_pnl_usd, funding_collected, execution_cost_usd, basis_pnl_usd)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        [
                            (
                                row["id"],
                                row["symbol"],
                                row["side"],
                                row["entry_time"],
                                row["exit_time"],
                                row["entry_price"],
                                row["exit_price"],
                                row["qty"],
                                row["net_pnl_usd"],
                                row["funding_collected"],
                                row["execution_cost_usd"],
                                row["basis_pnl_usd"],
                            )
                            for row in old_trades
                        ],
                    )
                    self.conn.execute("DELETE FROM trade_history WHERE exit_time < ?", (trade_cutoff,))
                    counts["trade_history_archived"] = len(old_trades)

                old_events = self.conn.execute(
                    "SELECT * FROM execution_events WHERE event_time < ? ORDER BY id",
                    (trade_cutoff,),
                ).fetchall()
                if old_events:
                    archive_conn.executemany(
                        """INSERT OR IGNORE INTO execution_events
                           (id, symbol, client_order_id, status, filled_qty, avg_fill_price,
                            last_fill_price, cumulative_quote_qty, commission, commission_asset,
                            realized_pnl, maker, execution_type, event_time, raw_payload)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        [
                            (
                                row["id"],
                                row["symbol"],
                                row["client_order_id"],
                                row["status"],
                                row["filled_qty"],
                                row["avg_fill_price"],
                                row["last_fill_price"],
                                row["cumulative_quote_qty"],
                                row["commission"],
                                row["commission_asset"],
                                row["realized_pnl"],
                                row["maker"],
                                row["execution_type"],
                                row["event_time"],
                                row["raw_payload"],
                            )
                            for row in old_events
                        ],
                    )
                    self.conn.execute("DELETE FROM execution_events WHERE event_time < ?", (trade_cutoff,))
                    counts["execution_events_archived"] = len(old_events)

                market_deleted = self.conn.execute(
                    "DELETE FROM market_samples WHERE sample_minute < ?",
                    (market_cutoff,),
                ).rowcount
                health_deleted = self.conn.execute(
                    "DELETE FROM health_samples WHERE sample_time < ?",
                    (health_cutoff,),
                ).rowcount
                counts["market_samples_deleted"] = max(0, market_deleted)
                counts["health_samples_deleted"] = max(0, health_deleted)
                self.conn.commit()
                archive_conn.commit()
        finally:
            archive_conn.close()
        return counts

    def close(self) -> None:
        self.conn.close()


class StateReader:
    def __init__(self, db_path: str = DB_PATH) -> None:
        self.conn = _connect(db_path)
        self.conn.executescript(_SCHEMA)
        _migrate_schema(self.conn)

    def get_positions(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM positions WHERE status != 'CLOSED' ORDER BY symbol"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_stats(self) -> dict:
        rows = self.conn.execute("SELECT key, value FROM portfolio_stats").fetchall()
        return {r["key"]: r["value"] for r in rows}

    def get_trades(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM trade_history ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_pnl_attribution(self) -> dict:
        """Aggregate PnL broken down by source: funding, basis, and execution costs."""
        row = self.conn.execute(
            """SELECT
                   COALESCE(SUM(funding_collected), 0.0)    AS total_funding,
                   COALESCE(SUM(basis_pnl_usd), 0.0)       AS total_basis_pnl,
                   COALESCE(SUM(execution_cost_usd), 0.0)   AS total_execution_cost,
                   COALESCE(SUM(net_pnl_usd), 0.0)          AS total_net_pnl,
                   COUNT(*)                                  AS trade_count
               FROM trade_history"""
        ).fetchone()
        return dict(row) if row else {}

    def get_risk(self) -> dict:
        rows = self.conn.execute("SELECT key, value FROM risk_state").fetchall()
        result = {}
        for r in rows:
            k = r["key"]
            v = r["value"]

            if k in ("drawdown_pct", "spread_toxicity", "venue_latency"):
                try:
                    result[k] = float(v)
                except ValueError:
                    pass # Ignore if it cannot be cast to float
            elif k in ("kill_switch", "allow_new_risk", "is_kill_switch"):
                result[k] = str(v).lower() == "true"
            elif k == "reasons":
                try:
                    parsed = json.loads(v)
                    if isinstance(parsed, list):
                        result[k] = [str(item) for item in parsed]
                    else:
                        result[k] = []
                except (json.JSONDecodeError, TypeError):
                    result[k] = []
            else:
                # Try JSON deserialization for values stored via set_risk_snapshot
                try:
                    result[k] = json.loads(v)
                except (json.JSONDecodeError, TypeError):
                    result[k] = str(v)
        return result

    def get_execution_events(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            """SELECT symbol, client_order_id, status, filled_qty, avg_fill_price,
                      last_fill_price, cumulative_quote_qty, commission, commission_asset,
                      realized_pnl, maker, execution_type, event_time
               FROM execution_events
               ORDER BY id DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_execution_events_since(self, start_time: str, end_time: str | None = None, limit: int = 500) -> list[dict]:
        params: list[object] = [start_time]
        where = "event_time >= ?"
        if end_time is not None:
            where += " AND event_time <= ?"
            params.append(end_time)
        params.append(limit)
        rows = self.conn.execute(
            f"""SELECT symbol, client_order_id, status, filled_qty, avg_fill_price,
                       last_fill_price, cumulative_quote_qty, commission, commission_asset,
                       realized_pnl, maker, execution_type, event_time
                FROM execution_events
                WHERE {where}
                ORDER BY id DESC
                LIMIT ?""",
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_pending_intents(self, statuses: list[str] | None = None) -> list[dict]:
        params: list[object] = []
        sql = "SELECT * FROM pending_intents"
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            sql += f" WHERE status IN ({placeholders})"
            params.extend(statuses)
        sql += " ORDER BY updated_at DESC"
        rows = self.conn.execute(sql, tuple(params)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["metadata"] = json.loads(item.get("metadata") or "{}")
            except (json.JSONDecodeError, TypeError):
                item["metadata"] = {}
            result.append(item)
        return result

    def get_market_samples(
        self,
        *,
        symbol: str | None = None,
        since: str | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[object] = []
        if symbol is not None:
            clauses.append("symbol = ?")
            params.append(symbol)
        if since is not None:
            clauses.append("sample_minute >= ?")
            params.append(since)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self.conn.execute(
            f"""SELECT * FROM market_samples
                {where}
                ORDER BY sample_minute DESC
                LIMIT ?""",
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_health_samples(
        self,
        *,
        metric: str | None = None,
        symbol: str | None = None,
        since: str | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[object] = []
        if metric is not None:
            clauses.append("metric = ?")
            params.append(metric)
        if symbol is not None:
            clauses.append("symbol = ?")
            params.append(symbol)
        if since is not None:
            clauses.append("sample_time >= ?")
            params.append(since)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self.conn.execute(
            f"""SELECT * FROM health_samples
                {where}
                ORDER BY sample_time DESC
                LIMIT ?""",
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_ai_report_proposal(self, proposal_id: str) -> dict | None:
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

    def get_ai_report_proposals(self, status: str | None = None, limit: int = 100) -> list[dict]:
        params: list[object] = []
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

    def get_latest_validation_snapshot(self) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM validation_snapshots ORDER BY snapshot_time DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        try:
            item["blockers"] = json.loads(item.get("blockers") or "[]")
        except (json.JSONDecodeError, TypeError):
            item["blockers"] = []
        try:
            item["metrics_json"] = json.loads(item.get("metrics_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            item["metrics_json"] = {}
        return item

    def get_validation_snapshots(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            """SELECT * FROM validation_snapshots
               ORDER BY snapshot_time DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        result: list[dict] = []
        for row in rows:
            item = dict(row)
            try:
                item["blockers"] = json.loads(item.get("blockers") or "[]")
            except (json.JSONDecodeError, TypeError):
                item["blockers"] = []
            try:
                item["metrics_json"] = json.loads(item.get("metrics_json") or "{}")
            except (json.JSONDecodeError, TypeError):
                item["metrics_json"] = {}
            result.append(item)
        return result

    def estimate_trade_execution_cost(
        self,
        symbol: str,
        start_time: str,
        end_time: str,
    ) -> float:
        """Approximate execution cost in USD for one completed trade window.

        Sums FILLED commissions between *start_time* and *end_time*.
        Quote-asset commissions are taken at face value; base-asset commissions
        are converted using the recorded fill price when available.
        """
        rows = self.conn.execute(
            """SELECT commission, commission_asset, avg_fill_price, last_fill_price
               FROM execution_events
               WHERE symbol = ?
                 AND status = 'FILLED'
                 AND event_time >= ?
                 AND event_time <= ?""",
            (symbol, start_time, end_time),
        ).fetchall()

        total_cost_usd = 0.0
        base_asset = _extract_base_asset(symbol)
        for row in rows:
            commission = row["commission"]
            asset = str(row["commission_asset"] or "").upper()
            if commission is None or not asset:
                continue

            try:
                commission_value = float(commission)
            except (TypeError, ValueError):
                continue

            if asset in {"USDT", "USDC", "FDUSD", "BUSD"}:
                total_cost_usd += commission_value
                continue

            if asset == base_asset:
                fill_price = row["avg_fill_price"] or row["last_fill_price"]
                try:
                    if fill_price is not None:
                        total_cost_usd += commission_value * float(fill_price)
                except (TypeError, ValueError):
                    continue

        return total_cost_usd

    def get_account_equity(self) -> float | None:
        """Return the last recorded account equity in USD, or None if unavailable."""
        row = self.conn.execute(
            "SELECT value FROM risk_state WHERE key = 'account_equity'"
        ).fetchone()
        if row is None:
            return None
        try:
            return float(row["value"])
        except (ValueError, TypeError):
            return None

    def close(self) -> None:
        self.conn.close()
