"""
SQLite-backed shared state store for cross-process communication.

StateWriter: used by live_trader.py to persist positions, trades, stats, risk.
StateReader: used by web_dashboard.py to serve REST API endpoints.

Uses WAL journal mode for concurrent readers + single writer.
"""

import sqlite3
import json
from datetime import datetime, timezone


DB_PATH = "state.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    symbol        TEXT PRIMARY KEY,
    side          TEXT NOT NULL,
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
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol           TEXT NOT NULL,
    side             TEXT NOT NULL,
    entry_time       TEXT NOT NULL,
    exit_time        TEXT NOT NULL,
    entry_price      REAL NOT NULL,
    exit_price       REAL NOT NULL,
    qty              REAL NOT NULL,
    net_pnl_usd      REAL NOT NULL,
    funding_collected REAL DEFAULT 0.0
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
    ) -> None:
        self.conn.execute(
            """INSERT INTO positions
               (symbol, side, spot_entry, perp_entry, spot_live, perp_live,
                qty, ann_funding, basis_pct, net_pnl_usd, status, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(symbol) DO UPDATE SET
                 side=excluded.side, spot_entry=excluded.spot_entry,
                 perp_entry=excluded.perp_entry, spot_live=excluded.spot_live,
                 perp_live=excluded.perp_live, qty=excluded.qty,
                 ann_funding=excluded.ann_funding, basis_pct=excluded.basis_pct,
                 net_pnl_usd=excluded.net_pnl_usd, status=excluded.status,
                 updated_at=excluded.updated_at""",
            (symbol, side, spot_entry, perp_entry, spot_live, perp_live,
             qty, ann_funding, basis_pct, net_pnl_usd, status, _now()),
        )
        self.conn.commit()

    def remove_position(self, symbol: str) -> None:
        self.conn.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))
        self.conn.commit()

    def record_trade(
        self,
        symbol: str,
        side: str,
        entry_time: str,
        exit_time: str,
        entry_price: float,
        exit_price: float,
        qty: float,
        net_pnl_usd: float,
        funding_collected: float = 0.0,
    ) -> None:
        self.conn.execute(
            """INSERT INTO trade_history
               (symbol, side, entry_time, exit_time, entry_price, exit_price,
                qty, net_pnl_usd, funding_collected)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (symbol, side, entry_time, exit_time, entry_price, exit_price,
             qty, net_pnl_usd, funding_collected),
        )
        self.conn.commit()

    def set_stat(self, key: str, value: float) -> None:
        self.conn.execute(
            """INSERT INTO portfolio_stats (key, value, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (key, value, _now()),
        )
        self.conn.commit()

    def set_risk(self, key: str, value: str) -> None:
        self.conn.execute(
            """INSERT INTO risk_state (key, value, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (key, value, _now()),
        )
        self.conn.commit()

    def set_risk_snapshot(self, snapshot: dict) -> None:
        """Write all risk fields at once."""
        for key, value in snapshot.items():
            self.set_risk(key, json.dumps(value) if not isinstance(value, str) else value)

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

    def get_risk(self) -> dict:
        rows = self.conn.execute("SELECT key, value FROM risk_state").fetchall()
        result = {}
        for r in rows:
            try:
                result[r["key"]] = json.loads(r["value"])
            except (json.JSONDecodeError, TypeError):
                result[r["key"]] = r["value"]
        return result

    def close(self) -> None:
        self.conn.close()
