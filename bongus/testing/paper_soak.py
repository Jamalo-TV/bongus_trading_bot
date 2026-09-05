"""Operational paper-soak checks; never economic or live-approval evidence."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from bongus.monitoring.progress_contract import progress_loop_deadlines


def isolated_environment(parent: Mapping[str, str], data_root: Path) -> dict[str, str]:
    """Allow OS runtime essentials only; never inherit credentials or overrides."""
    permitted = {
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE",
        "TEMP", "TMP", "TMPDIR", "HOME", "USER", "USERPROFILE", "APPDATA",
        "LOCALAPPDATA", "LANG", "LC_ALL", "LC_CTYPE",
    }
    result = {key: value for key, value in parent.items() if key.upper() in permitted}
    result.update({
        "TRADING_MODE": "paper",
        "PYTHON_DOTENV_DISABLED": "1",
        "BONGUS_DISABLE_DOTENV": "1",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "BONGUS_DATA_ROOT": str(data_root.resolve()),
        "MONITORED_SYMBOLS": "BTCUSDT,ETHUSDT",
        "TZ": "UTC",
    })
    return result


def timestamp_age(value: object, now: datetime) -> float:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return math.inf
        age = (now - stamp.astimezone(timezone.utc)).total_seconds()
        return age if math.isfinite(age) and age >= -2.0 else math.inf
    except (ValueError, TypeError):
        return math.inf


def health_errors(
    heartbeat: Mapping[str, Any], risk: Mapping[str, Any], *, now: datetime,
) -> list[str]:
    errors: list[str] = []
    age = timestamp_age(heartbeat.get("updated_at"), now)
    if age > 20.0:
        errors.append("heartbeat_missing_or_stale")
    ages = heartbeat.get("loop_heartbeat_ages")
    if not isinstance(ages, dict):
        ages = {}
    for name, deadline in progress_loop_deadlines().items():
        raw = ages.get(name)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            errors.append(f"loop_missing:{name}")
        elif not math.isfinite(raw) or raw < 0 or raw + max(0.0, age) > deadline:
            errors.append(f"loop_stale:{name}")
    if risk.get("trading_mode") != "paper":
        errors.append("mode_is_not_paper")
    if risk.get("preflight_status") != "passed":
        errors.append("preflight_not_passed")
    session_id = heartbeat.get("session_id")
    if not isinstance(session_id, str) or not session_id or session_id != risk.get("session_id"):
        errors.append("runtime_session_mismatch")
    for key in (
        "runtime_ready", "execution_bridge_healthy", "telemetry_connected",
        "critical_telemetry_receipt_healthy", "rust_execution_ready",
    ):
        if risk.get(key) is not True:
            errors.append(f"not_ready:{key}")
    if timestamp_age(risk.get("loop_last_alive_at"), now) > 20.0:
        errors.append("risk_snapshot_stale")
    fresh_count = risk.get("funding_fresh_symbol_count")
    if (isinstance(fresh_count, bool) or not isinstance(fresh_count, (float, int))
            or not math.isfinite(fresh_count) or fresh_count < 1):
        errors.append("no_fresh_funding")
    for key in ("risk_kill_switch", "kill_switch"):
        if risk.get(key) is True:
            errors.append(f"risk_halt:{key}")
    return errors


def shutdown_log_errors(console: str) -> list[str]:
    return [marker for marker in ("sending SIGKILL", "Error while stopping") if marker in console]


class ProjectionDrain:
    """Permit async projection bursts, but require a complete drain within 30 s.

    This observer does not change the trader's stricter entry block while any
    critical projection remains pending. Missing/malformed samples fail closed.
    """

    def __init__(self) -> None:
        self.pending_since: float | None = None

    def observe(self, monotonic: float, backlog: object) -> list[str]:
        if isinstance(backlog, bool) or not isinstance(backlog, int) or backlog < 0:
            return ["critical_projection_backlog_invalid"]
        if backlog == 0:
            self.pending_since = None
            return []
        if self.pending_since is None:
            self.pending_since = monotonic
        if backlog > 100 or monotonic - self.pending_since >= 30:
            return ["critical_projection_drain_failed"]
        return []


class ContinuousWindow:
    """Fail a started window on any gap; startup is excluded from the duration."""

    def __init__(self, required_seconds: float, max_sample_gap: float = 20.0):
        if not math.isfinite(required_seconds) or required_seconds < 1800:
            raise ValueError("a qualifying paper soak requires at least 1800 seconds")
        self.required_seconds = required_seconds
        self.max_sample_gap = max_sample_gap
        self.started: float | None = None
        self.previous: float | None = None
        self.identity: object = None

    def observe(self, monotonic: float, identity: object, errors: list[str]) -> float:
        if self.started is None:
            if errors:
                return 0.0
            self.started = monotonic
            self.identity = identity
        elif errors:
            raise RuntimeError("soak health failure: " + ", ".join(errors))
        if identity != self.identity:
            raise RuntimeError("process/session identity changed during soak")
        if self.previous is not None and not 0 <= monotonic - self.previous <= self.max_sample_gap:
            raise RuntimeError("soak observer stalled or monotonic time regressed")
        self.previous = monotonic
        return monotonic - self.started
