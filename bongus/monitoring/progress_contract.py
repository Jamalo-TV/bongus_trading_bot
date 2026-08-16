"""Shared independent-progress heartbeat names and deadline arithmetic."""

from __future__ import annotations

import math
from typing import Mapping

REQUIRED_PROGRESS_LOOPS: tuple[str, ...] = (
    "liveness_loop",
    "maintenance_loop",
    "retention_loop",
    "execution_event_writer",
    "storage_monitor",
    "trading_loop",
)
REQUIRED_LOOP_MAX_AGE_SECONDS = 30.0
TRADING_LOOP_DEFAULT_MAX_AGE_SECONDS = 120.0
HEAVY_LOOP_MAX_AGE_SECONDS = 180.0


def normalize_trading_loop_max_age(raw_value: object) -> float:
    raw_text = str(raw_value or "").strip()
    try:
        value = float(raw_text) if raw_text else TRADING_LOOP_DEFAULT_MAX_AGE_SECONDS
    except (TypeError, ValueError):
        value = TRADING_LOOP_DEFAULT_MAX_AGE_SECONDS
    if not math.isfinite(value):
        value = TRADING_LOOP_DEFAULT_MAX_AGE_SECONDS
    return min(300.0, max(REQUIRED_LOOP_MAX_AGE_SECONDS, value))


def progress_loop_deadlines(trading_loop_max_age: object = None) -> dict[str, float]:
    trading_deadline = normalize_trading_loop_max_age(trading_loop_max_age)
    return {
        name: (
            HEAVY_LOOP_MAX_AGE_SECONDS
            if name in {"retention_loop", "storage_monitor"}
            else trading_deadline
            if name == "trading_loop"
            else REQUIRED_LOOP_MAX_AGE_SECONDS
        )
        for name in REQUIRED_PROGRESS_LOOPS
    }


def effective_reported_loop_ages(
    raw_ages: Mapping[str, object], *, report_staleness_seconds: float
) -> dict[str, float]:
    staleness = max(0.0, float(report_staleness_seconds))
    effective: dict[str, float] = {}
    for name, raw_age in raw_ages.items():
        try:
            age = float(str(raw_age))
        except (TypeError, ValueError):
            effective[str(name)] = math.inf
            continue
        effective[str(name)] = age + staleness if math.isfinite(age) and age >= 0.0 else math.inf
    return effective
