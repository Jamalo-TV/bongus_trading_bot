"""Time-penalized, restart-safe entry cooldowns for stressed conditions."""

from __future__ import annotations

import sqlite3
import threading
import time
from _thread import RLock as RLockType
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from bongus.core.config import COOLDOWN_ENABLED


def _iso_from_ts(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


@dataclass
class _CooldownEntry:
    until_ts: float
    reason: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS cooldown_entries (
    scope TEXT NOT NULL,
    symbol TEXT NOT NULL DEFAULT '',
    until_ts REAL NOT NULL,
    reason TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(scope, symbol),
    CHECK(scope IN ('GLOBAL', 'SYMBOL')),
    CHECK((scope = 'GLOBAL' AND symbol = '') OR (scope = 'SYMBOL' AND symbol <> ''))
);
"""


class CooldownManager:
    """Manage entry cooldowns and optionally persist them in SQLite.

    When ``connection`` or ``db_path`` is supplied, every activation and
    expiry is committed immediately.  Absolute UTC epoch expiries are used so
    a process restart cannot reset or extend a cooldown accidentally.
    """

    def __init__(
        self,
        config_get: Callable[[str], float | bool] | None = None,
        *,
        db_path: str | None = None,
        connection: sqlite3.Connection | None = None,
        lock: RLockType | None = None,
    ) -> None:
        if connection is not None and db_path is not None:
            raise ValueError("pass either connection or db_path, not both")
        self._config_get = config_get
        self._global: _CooldownEntry | None = None
        self._symbols: dict[str, _CooldownEntry] = {}
        self._lock = lock or threading.RLock()
        self._owns_connection = connection is None and db_path is not None
        self._conn = connection
        if self._conn is None and db_path is not None:
            self._conn = sqlite3.connect(
                db_path,
                timeout=30,
                check_same_thread=False,
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=30000")
        if self._conn is not None:
            self._conn.executescript(_SCHEMA)
            self._restore()

    def close(self) -> None:
        if self._owns_connection and self._conn is not None:
            self._conn.close()

    def _restore(self) -> None:
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT scope, symbol, until_ts, reason FROM cooldown_entries"
        ).fetchall()
        for scope, symbol, until_ts, reason in rows:
            entry = _CooldownEntry(until_ts=float(until_ts), reason=str(reason))
            if str(scope) == "GLOBAL":
                self._global = entry
            elif str(scope) == "SYMBOL" and str(symbol):
                self._symbols[str(symbol).upper()] = entry
        self._prune(self._now())

    def _persist_entry(self, scope: str, symbol: str, entry: _CooldownEntry) -> None:
        if self._conn is None:
            return
        self._conn.execute(
            """
            INSERT INTO cooldown_entries (scope, symbol, until_ts, reason, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(scope, symbol) DO UPDATE SET
                until_ts=excluded.until_ts,
                reason=excluded.reason,
                updated_at=excluded.updated_at
            """,
            (
                scope,
                symbol,
                entry.until_ts,
                entry.reason,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._conn.commit()

    def _persist_expiries(self, *, global_expired: bool, symbols: list[str]) -> None:
        if self._conn is None or (not global_expired and not symbols):
            return
        if global_expired:
            self._conn.execute(
                "DELETE FROM cooldown_entries WHERE scope = 'GLOBAL' AND symbol = ''"
            )
        if symbols:
            self._conn.executemany(
                "DELETE FROM cooldown_entries WHERE scope = 'SYMBOL' AND symbol = ?",
                [(symbol,) for symbol in symbols],
            )
        self._conn.commit()

    def _cfg(self, key: str, default):
        if self._config_get is None:
            return default
        value = self._config_get(key)
        return default if value is None else value

    def _enabled(self) -> bool:
        return bool(self._cfg("cooldown_enabled", COOLDOWN_ENABLED))

    def _now(self, now_ts: float | None = None) -> float:
        return time.time() if now_ts is None else float(now_ts)

    def _prune(self, now_ts: float) -> None:
        global_expired = False
        if self._global is not None and self._global.until_ts <= now_ts:
            self._global = None
            global_expired = True

        expired = [symbol for symbol, entry in self._symbols.items() if entry.until_ts <= now_ts]
        for symbol in expired:
            self._symbols.pop(symbol, None)
        self._persist_expiries(global_expired=global_expired, symbols=expired)

    def activate_global(self, duration_s: float, reason: str, now_ts: float | None = None) -> None:
        if not self._enabled() or duration_s <= 0.0:
            return
        with self._lock:
            now_ts = self._now(now_ts)
            self._prune(now_ts)
            until_ts = now_ts + float(duration_s)
            if self._global is None or until_ts > self._global.until_ts:
                self._global = _CooldownEntry(until_ts=until_ts, reason=str(reason))
                self._persist_entry("GLOBAL", "", self._global)

    def activate_symbol(
        self,
        symbol: str,
        duration_s: float,
        reason: str,
        now_ts: float | None = None,
    ) -> None:
        if not self._enabled() or duration_s <= 0.0:
            return
        symbol = str(symbol).upper()
        if not symbol:
            return
        with self._lock:
            now_ts = self._now(now_ts)
            self._prune(now_ts)
            until_ts = now_ts + float(duration_s)
            existing = self._symbols.get(symbol)
            if existing is None or until_ts > existing.until_ts:
                self._symbols[symbol] = _CooldownEntry(until_ts=until_ts, reason=str(reason))
                self._persist_entry("SYMBOL", symbol, self._symbols[symbol])

    def is_global_active(self, now_ts: float | None = None) -> bool:
        with self._lock:
            now_ts = self._now(now_ts)
            self._prune(now_ts)
            return self._global is not None

    def is_symbol_active(self, symbol: str, now_ts: float | None = None) -> bool:
        with self._lock:
            now_ts = self._now(now_ts)
            self._prune(now_ts)
            return str(symbol).upper() in self._symbols

    def allow_symbol(self, symbol: str, now_ts: float | None = None) -> tuple[bool, str]:
        with self._lock:
            now_ts = self._now(now_ts)
            self._prune(now_ts)
            if self._global is not None:
                return False, self._global.reason
            entry = self._symbols.get(str(symbol).upper())
            if entry is not None:
                return False, entry.reason
            return True, ""

    def blocked_symbols(self, symbols: list[str], now_ts: float | None = None) -> dict[str, str]:
        with self._lock:
            now_ts = self._now(now_ts)
            self._prune(now_ts)
            blocked: dict[str, str] = {}
            for symbol in symbols:
                allowed, reason = self.allow_symbol(symbol, now_ts=now_ts)
                if not allowed:
                    blocked[symbol] = reason
            return blocked

    def snapshot(self, now_ts: float | None = None) -> dict:
        with self._lock:
            now_ts = self._now(now_ts)
            self._prune(now_ts)

            global_active = self._global is not None
            global_reason = self._global.reason if self._global is not None else ""
            global_until = _iso_from_ts(self._global.until_ts) if self._global is not None else ""
            global_remaining_s = max(0.0, self._global.until_ts - now_ts) if self._global is not None else 0.0

            symbol_cooldowns = {
                symbol: {
                    "reason": entry.reason,
                    "until": _iso_from_ts(entry.until_ts),
                    "remaining_s": max(0.0, entry.until_ts - now_ts),
                }
                for symbol, entry in sorted(self._symbols.items())
            }

            return {
                "global_active": global_active,
                "global_reason": global_reason,
                "global_until": global_until,
                "global_remaining_s": global_remaining_s,
                "symbol_cooldowns": symbol_cooldowns,
            }
