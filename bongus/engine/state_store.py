"""
SQLite-backed shared state store for cross-process communication.

StateWriter: used by live_trader.py to persist positions, trades, stats, risk.
StateReader: used by web_dashboard.py to serve REST API endpoints.

Uses WAL journal mode for concurrent readers + single writer.
"""

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone


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
"""


def _connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


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
    conn.commit()


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
        basis_pct: float = 0.0,
        net_pnl_usd: float = 0.0,
        status: str = "OPEN",
        spot_live: float = 0.0,
        perp_live: float = 0.0,
        direction: str = "long",
        updated_at: str | None = None,
    ) -> None:
        timestamp = updated_at or _now()
        with self._lock:
            self.conn.execute(
                """INSERT INTO positions
                   (symbol, side, direction, spot_entry, perp_entry, spot_live, perp_live,
                    qty, ann_funding, basis_pct, net_pnl_usd, status, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(symbol) DO UPDATE SET
                     side=excluded.side, direction=excluded.direction,
                     spot_entry=excluded.spot_entry,
                     perp_entry=excluded.perp_entry, spot_live=excluded.spot_live,
                     perp_live=excluded.perp_live, qty=excluded.qty,
                     ann_funding=excluded.ann_funding, basis_pct=excluded.basis_pct,
                     net_pnl_usd=excluded.net_pnl_usd, status=excluded.status,
                     updated_at=excluded.updated_at""",
                (symbol, side, direction, spot_entry, perp_entry, spot_live, perp_live,
                 qty, ann_funding, basis_pct, net_pnl_usd, status, timestamp),
            )
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
