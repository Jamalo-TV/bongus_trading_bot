"""Operational reliability helpers: monitoring, reconciliation, failover, secrets."""

import os
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class ServiceHealth:
    name: str
    last_heartbeat_utc: datetime
    consecutive_failures: int

    @property
    def healthy(self) -> bool:
        return self.consecutive_failures == 0


@dataclass
class ReconciliationResult:
    matched: bool
    position_diff: float
    cash_diff: float


@dataclass
class Incident:
    incident_id: str
    severity: str
    message: str
    created_at_utc: datetime


def load_secret_env(var_name: str) -> str:
    value = os.getenv(var_name, "")
    if not value:
        raise RuntimeError(f"Missing required secret env var: {var_name}")
    return value


def reconcile_state(
    expected_position: float,
    actual_position: float,
    expected_cash: float,
    actual_cash: float,
    tolerance: float = 1e-6,
) -> ReconciliationResult:
    pos_diff = actual_position - expected_position
    cash_diff = actual_cash - expected_cash
    matched = abs(pos_diff) <= tolerance and abs(cash_diff) <= tolerance
    return ReconciliationResult(matched=matched, position_diff=pos_diff, cash_diff=cash_diff)


def choose_failover_target(primary_ok: bool, backup_ok: bool) -> str:
    if primary_ok:
        return "primary"
    if backup_ok:
        return "backup"
    return "halt"


def open_incident(severity: str, message: str) -> Incident:
    now = datetime.now(timezone.utc)
    incident_id = f"INC-{now.strftime('%Y%m%d-%H%M%S')}"
    return Incident(
        incident_id=incident_id,
        severity=severity,
        message=message,
        created_at_utc=now,
    )
