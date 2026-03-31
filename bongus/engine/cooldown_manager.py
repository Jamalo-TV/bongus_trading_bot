"""Time-penalized trading cooldowns for stressed conditions."""

import time
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


class CooldownManager:
    def __init__(self, config_get: Callable[[str], float | bool] | None = None) -> None:
        self._config_get = config_get
        self._global: _CooldownEntry | None = None
        self._symbols: dict[str, _CooldownEntry] = {}

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
        if self._global is not None and self._global.until_ts <= now_ts:
            self._global = None

        expired = [symbol for symbol, entry in self._symbols.items() if entry.until_ts <= now_ts]
        for symbol in expired:
            self._symbols.pop(symbol, None)

    def activate_global(self, duration_s: float, reason: str, now_ts: float | None = None) -> None:
        if not self._enabled() or duration_s <= 0.0:
            return
        now_ts = self._now(now_ts)
        self._prune(now_ts)
        until_ts = now_ts + float(duration_s)
        if self._global is None or until_ts > self._global.until_ts:
            self._global = _CooldownEntry(until_ts=until_ts, reason=str(reason))

    def activate_symbol(
        self,
        symbol: str,
        duration_s: float,
        reason: str,
        now_ts: float | None = None,
    ) -> None:
        if not self._enabled() or duration_s <= 0.0:
            return
        now_ts = self._now(now_ts)
        self._prune(now_ts)
        until_ts = now_ts + float(duration_s)
        existing = self._symbols.get(symbol)
        if existing is None or until_ts > existing.until_ts:
            self._symbols[symbol] = _CooldownEntry(until_ts=until_ts, reason=str(reason))

    def is_global_active(self, now_ts: float | None = None) -> bool:
        now_ts = self._now(now_ts)
        self._prune(now_ts)
        return self._global is not None

    def is_symbol_active(self, symbol: str, now_ts: float | None = None) -> bool:
        now_ts = self._now(now_ts)
        self._prune(now_ts)
        return symbol in self._symbols

    def allow_symbol(self, symbol: str, now_ts: float | None = None) -> tuple[bool, str]:
        now_ts = self._now(now_ts)
        self._prune(now_ts)
        if self._global is not None:
            return False, self._global.reason
        entry = self._symbols.get(symbol)
        if entry is not None:
            return False, entry.reason
        return True, ""

    def blocked_symbols(self, symbols: list[str], now_ts: float | None = None) -> dict[str, str]:
        now_ts = self._now(now_ts)
        self._prune(now_ts)
        blocked: dict[str, str] = {}
        for symbol in symbols:
            allowed, reason = self.allow_symbol(symbol, now_ts=now_ts)
            if not allowed:
                blocked[symbol] = reason
        return blocked

    def snapshot(self, now_ts: float | None = None) -> dict:
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
