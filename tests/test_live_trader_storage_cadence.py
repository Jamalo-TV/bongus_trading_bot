from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast

import scripts.live_trader_v2 as live_trader_module
from bongus.engine.storage_guard import StorageFault
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


def test_retention_failure_from_storage_worker_is_reported_without_escaping_runtime() -> None:
    trader = _trader()
    raw_trader = cast(Any, trader)
    writer = _StateWriter()
    raw_trader.state_writer = writer
    raw_trader._last_retention_run_date = ""
    raw_trader._last_retention_run_at = ""
    reported: list[BaseException] = []

    def fail_retention() -> tuple[dict, dict]:
        raise RuntimeError("temporary SQLite lock")

    storage_callbacks: list[object] = []

    async def run_storage_blocking(callback, /, *args, **kwargs):
        storage_callbacks.append(callback)
        return callback(*args, **kwargs)

    raw_trader._run_retention_maintenance_once = fail_retention
    raw_trader._run_storage_blocking = run_storage_blocking
    raw_trader._report_storage_write_error = lambda exc: reported.append(exc)
    succeeded = asyncio.run(
        trader._run_retention_maintenance_safely(
            datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
        )
    )

    assert not succeeded
    assert storage_callbacks == [fail_retention]
    assert len(reported) == 1
    assert writer.snapshots[-1]["last_retention_error"] == (
        "RuntimeError: temporary SQLite lock"
    )


def test_healthy_integrity_probe_is_paced_but_faults_bypass_cache(monkeypatch) -> None:
    clock = [100.0]
    probe_times: list[float] = []

    class _CadenceGuard:
        def __init__(self) -> None:
            self.active_faults: set[StorageFault] = set()

        def snapshot(self):
            return SimpleNamespace(active_faults=tuple(self.active_faults))

        def sample(self, **_kwargs):
            return self.snapshot()

        def resolve_fault(self, fault: StorageFault) -> None:
            self.active_faults.discard(fault)

        def report_fault(self, fault: StorageFault) -> None:
            self.active_faults.add(fault)

    guard = _CadenceGuard()
    trader = cast(Any, LiveTraderV2.__new__(LiveTraderV2))
    trader._storage_integrity_ok = False
    trader._last_storage_integrity_probe_monotonic = None
    trader._storage_guard = guard
    trader._account_reconciliation_ready = True
    trader._apply_storage_snapshot = lambda _snapshot: None

    def integrity_probe() -> bool:
        probe_times.append(clock[0])
        return True

    async def run_storage_blocking(callback, /, *args, **kwargs):
        return callback(*args, **kwargs)

    trader._database_integrity_probe = integrity_probe
    trader._run_storage_blocking = run_storage_blocking
    monkeypatch.setattr(
        live_trader_module,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0]),
    )

    async def scenario() -> None:
        await trader._sample_storage_health(run_durability_probes=False)
        clock[0] = 399.0
        await trader._sample_storage_health(run_durability_probes=False)
        clock[0] = 400.0
        await trader._sample_storage_health(run_durability_probes=False)
        guard.report_fault(StorageFault.DATABASE_CORRUPT)
        clock[0] = 401.0
        await trader._sample_storage_health(run_durability_probes=False)

    asyncio.run(scenario())

    assert probe_times == [100.0, 400.0, 401.0]


def test_retention_has_independent_completion_heartbeat(monkeypatch) -> None:
    class _ShutdownEvent:
        @staticmethod
        def is_set() -> bool:
            return False

    class _RetentionGuard:
        @staticmethod
        def allows(_action: object) -> bool:
            return True

    trader = cast(Any, LiveTraderV2.__new__(LiveTraderV2))
    trader._shutdown_event = _ShutdownEvent()
    trader._loop_heartbeats = {}
    trader._last_retention_run_at = ""
    trader._last_retention_attempt_monotonic = 0.0
    trader._storage_guard = _RetentionGuard()
    trader.state_reader = SimpleNamespace(get_risk=lambda: {})
    retention_runs: list[datetime] = []
    sleep_observations: list[tuple[float, float]] = []

    async def run_retention(now: datetime) -> bool:
        assert trader._loop_heartbeats["retention_loop"] == 10.0
        retention_runs.append(now)
        return True

    async def sleep_or_shutdown(interval: float) -> bool:
        sleep_observations.append(
            (interval, trader._loop_heartbeats["retention_loop"])
        )
        return True

    trader._run_retention_maintenance_safely = run_retention
    trader._sleep_or_shutdown = sleep_or_shutdown
    monotonic_values = iter((10.0, 20.0, 30.0))
    monkeypatch.setattr(
        live_trader_module,
        "time",
        SimpleNamespace(monotonic=lambda: next(monotonic_values)),
    )

    asyncio.run(trader._run_retention_loop())

    assert len(retention_runs) == 1
    assert trader._last_retention_attempt_monotonic == 20.0
    assert sleep_observations == [(60.0, 30.0)]


def test_retention_scheduler_failure_is_contained_and_keeps_cadence(monkeypatch) -> None:
    class _ShutdownEvent:
        @staticmethod
        def is_set() -> bool:
            return False

    def fail_get_risk() -> dict[str, object]:
        raise RuntimeError("temporary risk-state read failure")

    trader = cast(Any, LiveTraderV2.__new__(LiveTraderV2))
    trader._shutdown_event = _ShutdownEvent()
    trader._loop_heartbeats = {}
    trader._last_retention_run_at = ""
    trader._last_retention_attempt_monotonic = 0.0
    trader.state_reader = SimpleNamespace(get_risk=fail_get_risk)
    reported: list[BaseException] = []
    sleep_observations: list[tuple[float, float]] = []
    trader._report_storage_write_error = lambda exc: reported.append(exc)

    async def sleep_or_shutdown(interval: float) -> bool:
        sleep_observations.append(
            (interval, trader._loop_heartbeats["retention_loop"])
        )
        return True

    trader._sleep_or_shutdown = sleep_or_shutdown
    monotonic_values = iter((10.0, 20.0, 30.0))
    monkeypatch.setattr(
        live_trader_module,
        "time",
        SimpleNamespace(monotonic=lambda: next(monotonic_values)),
    )

    asyncio.run(trader._run_retention_loop())

    assert len(reported) == 1
    assert str(reported[0]) == "temporary risk-state read failure"
    assert sleep_observations == [(60.0, 30.0)]
