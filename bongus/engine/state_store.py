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
"""


def _connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateWriter:
    def __init__(self, db_path: str = DB_PATH) -> None:
        self.conn = _connect(db_path)
        self.conn.executescript(_SCHEMA)
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
    ) -> None:
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
                 qty, ann_funding, basis_pct, net_pnl_usd, status, _now()),
            )
            self.conn.commit()

    def remove_position(self, symbol: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))
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

    def close(self) -> None:
        self.conn.close()


class StateReader:
    def __init__(self, db_path: str = DB_PATH) -> None:
        self.conn = _connect(db_path)
        self.conn.executescript(_SCHEMA)

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
