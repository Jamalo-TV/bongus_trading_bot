from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from bongus.portfolio.portfolio_allocator import AllocationDecision
from scripts.live_trader_v2 import LiveTraderV2


class _Config:
    def __init__(self, **values: object) -> None:
        self._values = values

    def get(self, key: str) -> object:
        return self._values.get(key)


class _StorageGuard:
    def __init__(self, *, optional: bool, entry: bool) -> None:
        self.optional = optional
        self.entry = entry

    def allows(self, action: object) -> bool:
        value = getattr(action, "value", str(action))
        if value == "optional_write":
            return self.optional
        if value == "entry":
            return self.entry
        return False


class _StateWriter:
    def __init__(self) -> None:
        self.snapshots: list[dict[str, object]] = []

    def set_risk_snapshot(self, snapshot: dict[str, object]) -> None:
        self.snapshots.append(snapshot)

    def flush(self) -> None:
        return None


def _trader(*, stage: str = "shadow") -> LiveTraderV2:
    trader = cast(Any, LiveTraderV2.__new__(LiveTraderV2))
    trader._trading_mode = "testnet"
    trader._config = _Config(
        decision_engine_stage=stage,
        trader_cycle_interval_seconds=15,
        research_evidence_min_interval_seconds=60,
    )
    trader._last_research_evidence_monotonic = 100.0
    return cast(LiveTraderV2, trader)


def test_research_sampling_does_not_slow_trading_decisions() -> None:
    trader = _trader()

    assert trader._decision_cycle_interval_seconds() == 15.0
    assert not trader._research_evidence_due(
        AllocationDecision(),
        now_monotonic=115.0,
    )
    assert trader._research_evidence_due(
        AllocationDecision(),
        now_monotonic=160.0,
    )


def test_actionable_and_canonical_cycles_are_never_sampled_out() -> None:
    trader = _trader()
    actionable = AllocationDecision(enter=[("BTCUSDT", 2_500.0)])

    assert trader._research_evidence_due(actionable, now_monotonic=101.0)

    canonical = _trader(stage="testnet_candidate")
    assert canonical._research_evidence_due(
        AllocationDecision(),
        now_monotonic=101.0,
    )


def test_warning_suppresses_idle_evidence_but_preserves_action_evidence() -> None:
    trader = _trader()
    raw_trader = cast(Any, trader)
    raw_trader._storage_guard = _StorageGuard(optional=True, entry=True)
    assert trader._research_evidence_write_policy(
        AllocationDecision(enter=[("BTCUSDT", 2_500.0)])
    ) == (True, True)

    raw_trader._storage_guard = _StorageGuard(optional=False, entry=True)

    assert trader._research_evidence_write_policy(AllocationDecision()) == (
        False,
        False,
    )
    assert trader._research_evidence_write_policy(
        AllocationDecision(enter=[("BTCUSDT", 2_500.0)])
    ) == (True, True)

    raw_trader._storage_guard = _StorageGuard(optional=False, entry=False)
    assert trader._research_evidence_write_policy(
        AllocationDecision(enter=[("BTCUSDT", 2_500.0)])
    ) == (False, False)


def test_retention_runs_hourly_and_recovers_from_bad_timestamp() -> None:
    now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)

    assert LiveTraderV2._retention_maintenance_due(now, "")
    assert LiveTraderV2._retention_maintenance_due(now, "not-a-time")
    assert not LiveTraderV2._retention_maintenance_due(
        now,
        (now - timedelta(minutes=59)).isoformat(),
    )
    assert LiveTraderV2._retention_maintenance_due(
        now,
        (now - timedelta(hours=1)).isoformat(),
    )
    assert LiveTraderV2._retention_maintenance_due(
        now,
        (now + timedelta(hours=1)).isoformat(),
    )


def test_retention_failure_is_reported_without_escaping_runtime() -> None:
    trader = _trader()
    raw_trader = cast(Any, trader)
    writer = _StateWriter()
    raw_trader.state_writer = writer
    raw_trader._last_retention_run_date = ""
    raw_trader._last_retention_run_at = ""
    reported: list[BaseException] = []

    def fail_retention() -> tuple[dict, dict]:
        raise RuntimeError("temporary SQLite lock")

    raw_trader._run_retention_maintenance_once = fail_retention
    raw_trader._report_storage_write_error = lambda exc: reported.append(exc)
    succeeded = asyncio.run(
        trader._run_retention_maintenance_safely(
            datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
        )
    )

    assert not succeeded
    assert len(reported) == 1
    assert writer.snapshots[-1]["last_retention_error"] == (
        "RuntimeError: temporary SQLite lock"
    )
