from __future__ import annotations

import errno
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bongus.engine.storage_guard import (
    AtomicHealthSnapshotStore,
    BINARY_MB,
    CleanupPolicy,
    CleanupRoot,
    ComponentBudget,
    DECIMAL_GB,
    DEFAULT_COMPONENT_LIMITS,
    DEFAULT_RECOVERY_HYSTERESIS_BYTES,
    DurabilityError,
    DurabilityProbeResult,
    DurabilityStage,
    EmergencyReserve,
    OSDurableFileOperations,
    SafeCleanup,
    StorageAction,
    StorageComponent,
    StorageFault,
    StorageGuard,
    StoragePolicy,
    StorageState,
    SystemDiskProbe,
    UnsafeCleanupPath,
    VolumeUsage,
    WriteFsyncRenameProbe,
    volume_root_for_path,
)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class MutableDiskProbe:
    def __init__(self, observations: dict[Path, tuple[str, int, int]]) -> None:
        self.observations = observations
        self.calls: list[Path] = []

    def inspect(self, path: Path) -> VolumeUsage:
        self.calls.append(path)
        volume_id, total, free = self.observations[path]
        return VolumeUsage(
            volume_id=volume_id,
            mount_path=Path(f"/{volume_id}"),
            total_bytes=total,
            free_bytes=free,
            observed_path=path,
        )


class MutableSizeProbe:
    def __init__(self, sizes: dict[Path, int]) -> None:
        self.sizes = sizes

    def size_bytes(self, path: Path) -> int:
        return self.sizes[path]


def policy_for(path: Path) -> StoragePolicy:
    return StoragePolicy(monitored_paths=(path,))


def guard_for_free_space(
    path: Path,
    *,
    total: int,
    free: int,
    clock: Clock | None = None,
    policy: StoragePolicy | None = None,
    reserve: EmergencyReserve | None = None,
    durability_probe: object | None = None,
    snapshot_store: AtomicHealthSnapshotStore | None = None,
) -> tuple[StorageGuard, MutableDiskProbe, Clock]:
    active_clock = clock or Clock()
    probe = MutableDiskProbe({path: ("volume-a", total, free)})
    guard = StorageGuard(
        policy or policy_for(path),
        disk_probe=probe,
        reserve=reserve,
        durability_probe=durability_probe,  # type: ignore[arg-type]
        snapshot_store=snapshot_store,
        monotonic=active_clock,
        utcnow=lambda: datetime(2026, 8, 9, tzinfo=timezone.utc),
    )
    return guard, probe, active_clock


def test_default_component_budget_is_exactly_sixteen_decimal_gigabytes() -> None:
    assert sum(DEFAULT_COMPONENT_LIMITS.values()) == 16 * DECIMAL_GB
    assert DEFAULT_COMPONENT_LIMITS[StorageComponent.HOT_STATE] == 1_250_000_000
    assert DEFAULT_COMPONENT_LIMITS[StorageComponent.FREE_HEADROOM] == 4_000_000_000


@pytest.mark.parametrize(
    ("free", "expected"),
    [
        (4_000_000_000, StorageState.HEALTHY),
        (3_999_999_999, StorageState.WARNING),
        (2_999_999_999, StorageState.DEGRADED),
        (1_999_999_999, StorageState.EMERGENCY),
        (999_999_999, StorageState.CRITICAL),
    ],
)
def test_absolute_free_space_thresholds(path: Path, free: int, expected: StorageState) -> None:
    guard, _, _ = guard_for_free_space(path, total=16 * DECIMAL_GB, free=free)

    snapshot = guard.sample()

    assert snapshot.state is expected
    assert snapshot.instantaneous_state is expected


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


@pytest.mark.parametrize(
    ("free", "expected"),
    [
        (25_000_000_000, StorageState.HEALTHY),
        (24_999_999_999, StorageState.WARNING),
        (18_749_999_999, StorageState.DEGRADED),
        (12_499_999_999, StorageState.EMERGENCY),
        (6_249_999_999, StorageState.CRITICAL),
    ],
)
def test_percentage_thresholds_apply_even_when_absolute_headroom_is_large(
    path: Path, free: int, expected: StorageState
) -> None:
    guard, _, _ = guard_for_free_space(path, total=100 * DECIMAL_GB, free=free)

    assert guard.sample().state is expected


def test_component_warning_and_hard_budget_states_are_explicit(tmp_path: Path) -> None:
    normal = tmp_path / "state.db"
    journal = tmp_path / "journal"
    disk = MutableDiskProbe(
        {
            normal: ("volume", 100 * DECIMAL_GB, 50 * DECIMAL_GB),
            journal: ("volume", 100 * DECIMAL_GB, 50 * DECIMAL_GB),
        }
    )
    sizes = MutableSizeProbe({normal: 800, journal: 1_000})
    policy = StoragePolicy(
        components=(
            ComponentBudget("state", normal, 1_000),
            ComponentBudget("journal", journal, 1_000, breach_state=StorageState.EMERGENCY),
        )
    )
    guard = StorageGuard(policy, disk_probe=disk, size_probe=sizes, monotonic=Clock())

    snapshot = guard.sample()

    by_name = {component.name: component for component in snapshot.components}
    assert by_name["state"].state is StorageState.WARNING
    assert by_name["journal"].state is StorageState.EMERGENCY
    assert snapshot.state is StorageState.EMERGENCY


def test_whole_volume_budget_blocks_risk_before_reserved_headroom_is_consumed(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    disk = MutableDiskProbe(
        {
            first: ("volume", 1_000, 800),
            second: ("volume", 1_000, 800),
        }
    )
    sizes = MutableSizeProbe({first: 180, second: 180})
    policy = StoragePolicy(
        components=(
            ComponentBudget("first", first, 1_000),
            ComponentBudget("second", second, 1_000),
        ),
        volume_budget_bytes=1_000,
        base_runtime_reservation_bytes=100,
        unmanaged_contingency_bytes=100,
        reserve_bytes=50,
        warning_free_bytes=400,
        degraded_free_bytes=300,
        emergency_free_bytes=200,
        critical_free_bytes=100,
    )
    guard = StorageGuard(policy, disk_probe=disk, size_probe=sizes)

    snapshot = guard.sample()

    assert snapshot.budgeted_consumption_bytes == 610
    assert snapshot.budgeted_free_headroom_bytes == 400
    assert snapshot.budgeted_utilization == pytest.approx(610 / 600)
    assert snapshot.state is StorageState.DEGRADED
    assert not guard.allows(StorageAction.ENTRY)
    assert any("aggregate:volume_budget_breached" in reason for reason in snapshot.reasons)


def test_overlapping_component_paths_count_per_cap_but_once_for_volume(
    tmp_path: Path,
) -> None:
    research = tmp_path / "research.db"
    archive = tmp_path / "research.archive.db"
    wal = tmp_path / "research.db-wal"
    shm = tmp_path / "research.db-shm"
    paths = (research, archive, wal, shm)
    disk = MutableDiskProbe(
        {
            path: ("volume", 10_000, 9_000)
            for path in paths
        }
    )
    sizes = MutableSizeProbe(
        {
            research: 100,
            archive: 200,
            wal: 30,
            shm: 20,
        }
    )
    policy = StoragePolicy(
        components=(
            ComponentBudget(
                "research",
                research,
                400,
                additional_paths=(archive, wal, shm),
            ),
            ComponentBudget(
                "sqlite_scratch",
                wal,
                100,
                additional_paths=(shm,),
            ),
        ),
        volume_budget_bytes=1_000,
        base_runtime_reservation_bytes=0,
        unmanaged_contingency_bytes=0,
        reserve_bytes=10,
        warning_free_bytes=400,
        degraded_free_bytes=300,
        emergency_free_bytes=200,
        critical_free_bytes=100,
    )
    snapshot = StorageGuard(
        policy,
        disk_probe=disk,
        size_probe=sizes,
    ).sample()

    by_name = {component.name: component for component in snapshot.components}
    assert by_name["research"].used_bytes == 350
    assert by_name["sqlite_scratch"].used_bytes == 50
    assert by_name["research"].paths == paths
    assert snapshot.budgeted_consumption_bytes == 360
    assert by_name["research"].to_dict()["paths"] == [
        str(path) for path in paths
    ]


def test_ema_projects_time_to_full_without_a_real_disk_fill(path: Path) -> None:
    clock = Clock()
    guard, probe, _ = guard_for_free_space(
        path,
        total=100 * DECIMAL_GB,
        free=50 * DECIMAL_GB,
        clock=clock,
    )
    assert guard.sample().worst_time_to_full_hours is None

    clock.advance(3600)
    probe.observations[path] = ("volume-a", 100 * DECIMAL_GB, 49 * DECIMAL_GB)
    snapshot = guard.sample()

    assert snapshot.state is StorageState.WARNING
    assert snapshot.volumes[0].consumption_bytes_per_hour == pytest.approx(DECIMAL_GB)
    assert snapshot.worst_time_to_full_hours == pytest.approx(49.0)
    assert any("ttf_warning" in reason for reason in snapshot.reasons)


def test_component_ema_can_escalate_before_budget_is_reached(tmp_path: Path) -> None:
    component_path = tmp_path / "research"
    clock = Clock()
    disk = MutableDiskProbe({component_path: ("volume", 100 * DECIMAL_GB, 50 * DECIMAL_GB)})
    sizes = MutableSizeProbe({component_path: 100})
    policy = StoragePolicy(components=(ComponentBudget("research", component_path, 10_000),), ema_alpha=1.0)
    guard = StorageGuard(policy, disk_probe=disk, size_probe=sizes, monotonic=clock)
    guard.sample()

    clock.advance(3600)
    sizes.sizes[component_path] = 9_900
    snapshot = guard.sample()

    assert snapshot.components[0].time_to_full_hours == pytest.approx(100 / 9_800)
    assert snapshot.components[0].state is StorageState.CRITICAL
    assert snapshot.state is StorageState.CRITICAL


def test_worst_state_wins_across_multiple_filesystems(tmp_path: Path) -> None:
    state_path = tmp_path / "state.db"
    journal_path = tmp_path / "journals"
    disk = MutableDiskProbe(
        {
            state_path: ("healthy-volume", 16 * DECIMAL_GB, 8 * DECIMAL_GB),
            journal_path: ("low-volume", 16 * DECIMAL_GB, 1_500_000_000),
        }
    )
    guard = StorageGuard(StoragePolicy(monitored_paths=(state_path, journal_path)), disk_probe=disk)

    snapshot = guard.sample()

    assert {volume.volume_id for volume in snapshot.volumes} == {"healthy-volume", "low-volume"}
    assert snapshot.state is StorageState.EMERGENCY
    assert snapshot.emergency_latched


def test_paths_on_same_filesystem_are_evaluated_once_as_one_volume(tmp_path: Path) -> None:
    first = tmp_path / "state.db"
    second = tmp_path / "logs"
    disk = MutableDiskProbe(
        {
            first: ("shared", 16 * DECIMAL_GB, 6 * DECIMAL_GB),
            second: ("shared", 16 * DECIMAL_GB, 5 * DECIMAL_GB),
        }
    )
    guard = StorageGuard(StoragePolicy(monitored_paths=(first, second)), disk_probe=disk)

    snapshot = guard.sample()

    assert len(snapshot.volumes) == 1
    assert snapshot.volumes[0].free_bytes == 5 * DECIMAL_GB
    assert snapshot.volumes[0].observed_paths == tuple(sorted((first, second), key=str))


def test_recovery_requires_hysteresis_three_samples_integrity_reconciliation_and_ack(path: Path) -> None:
    guard, probe, _ = guard_for_free_space(path, total=16 * DECIMAL_GB, free=2_500_000_000)
    first = guard.sample()
    assert first.state is StorageState.DEGRADED
    assert first.risk_increase_blocked
    assert not guard.allows(StorageAction.ENTRY)

    almost_recovered = 4 * DECIMAL_GB + DEFAULT_RECOVERY_HYSTERESIS_BYTES - 1
    probe.observations[path] = ("volume-a", 16 * DECIMAL_GB, almost_recovered)
    held = guard.sample(integrity_ok=True, exchange_reconciled=True)
    assert held.instantaneous_state is StorageState.HEALTHY
    assert held.state is StorageState.DEGRADED
    assert held.healthy_recovery_samples == 0

    probe.observations[path] = ("volume-a", 16 * DECIMAL_GB, almost_recovered + 2)
    for expected_count in (1, 2):
        held = guard.sample()
        assert held.state is StorageState.DEGRADED
        assert held.healthy_recovery_samples == expected_count

    recovered = guard.sample()
    assert recovered.state is StorageState.HEALTHY
    assert recovered.risk_increase_blocked
    assert recovered.recovery_ready_for_operator
    assert not guard.allows(StorageAction.ENTRY)
    with pytest.raises(PermissionError):
        guard.acknowledge_recovery()
    assert guard.acknowledge_recovery(operator_acknowledged=True)
    assert guard.allows(StorageAction.ENTRY)


def test_emergency_is_sticky_but_survival_actions_remain_available(path: Path) -> None:
    guard, probe, _ = guard_for_free_space(path, total=16 * DECIMAL_GB, free=1_500_000_000)
    emergency = guard.sample()

    assert emergency.state is StorageState.EMERGENCY
    assert emergency.emergency_latched
    assert not guard.allows(StorageAction.ENTRY)
    assert not guard.allows(StorageAction.ROTATION)
    assert guard.allows(StorageAction.CANCEL_ENTRY)
    assert guard.allows(StorageAction.HEDGE_REPAIR)
    assert guard.allows(StorageAction.EXIT)
    assert guard.allows(StorageAction.RECONCILIATION)
    assert guard.allows(StorageAction.CRITICAL_WRITE)

    probe.observations[path] = ("volume-a", 16 * DECIMAL_GB, 6 * DECIMAL_GB)
    recovered = guard.sample(integrity_ok=True, exchange_reconciled=True)
    for _ in range(2):
        recovered = guard.sample()
    assert recovered.state is StorageState.HEALTHY
    assert recovered.emergency_latched
    assert not guard.allows(StorageAction.ENTRY)


def test_faults_are_critical_and_require_resolution_before_recovery(path: Path) -> None:
    guard, _, _ = guard_for_free_space(path, total=16 * DECIMAL_GB, free=8 * DECIMAL_GB)
    healthy = guard.sample(integrity_ok=True, exchange_reconciled=True)
    guard.report_fault(StorageFault.SQLITE_FULL)
    latched = guard.snapshot()
    assert latched is not None
    assert latched.generation == healthy.generation + 1
    assert latched.state is StorageState.CRITICAL
    assert latched.risk_increase_blocked

    critical = guard.sample()
    assert critical.state is StorageState.CRITICAL
    assert critical.active_faults == (StorageFault.SQLITE_FULL,)
    assert guard.resolve_fault(StorageFault.SQLITE_FULL)

    recovered = guard.sample()
    for _ in range(2):
        recovered = guard.sample()
    assert recovered.state is StorageState.HEALTHY
    assert recovered.risk_increase_blocked
    assert guard.acknowledge_recovery(operator_acknowledged=True)
    acknowledged = guard.snapshot()
    assert acknowledged is not None
    assert not acknowledged.risk_increase_blocked
    assert not acknowledged.emergency_latched


def test_emergency_reserve_is_materialized_fsynced_and_release_is_idempotent(tmp_path: Path) -> None:
    reserve_path = tmp_path / "emergency.reserve"
    size = 2 * BINARY_MB
    reserve = EmergencyReserve(reserve_path, size_bytes=size)

    assert reserve.create()
    status = reserve.status()
    assert status.present
    assert status.logical_bytes == size
    if status.allocated_bytes is not None:
        assert status.allocated_bytes >= size
    assert not reserve.create()

    # A new object simulates process restart; file presence is the durable
    # source of truth for exactly-once release.
    restarted = EmergencyReserve(reserve_path, size_bytes=size)
    assert restarted.release()
    assert not restarted.release()
    assert restarted.status().released
    assert not reserve_path.exists()


def test_reserve_release_recovers_a_crash_staging_file(tmp_path: Path) -> None:
    reserve_path = tmp_path / "reserve.bin"
    staging = tmp_path / ".reserve.bin.allocating"
    staging.write_bytes(os.urandom(4096))
    reserve = EmergencyReserve(reserve_path, size_bytes=8192)

    assert reserve.release()
    assert not staging.exists()
    assert not reserve.release()


def test_guard_latches_before_reserve_release_and_cleanup_permission(tmp_path: Path) -> None:
    watched = tmp_path / "state.db"
    reserve = EmergencyReserve(tmp_path / "reserve.bin", size_bytes=BINARY_MB)
    reserve.create()
    guard, _, _ = guard_for_free_space(
        watched,
        total=16 * DECIMAL_GB,
        free=1_500_000_000,
        reserve=reserve,
    )

    with pytest.raises(Exception, match="requires the emergency"):
        guard.release_reserve()
    guard.sample()
    assert not guard.allows(StorageAction.CLEANUP)
    assert guard.release_reserve()
    assert guard.allows(StorageAction.CLEANUP)
    assert not guard.release_reserve()


class FailingOperations(OSDurableFileOperations):
    def __init__(self, failed_stage: DurabilityStage, error_number: int = errno.EIO) -> None:
        self.failed_stage = failed_stage
        self.error_number = error_number

    def _fail(self, stage: DurabilityStage) -> None:
        if self.failed_stage is stage:
            raise OSError(self.error_number, os.strerror(self.error_number))

    def write_bytes(self, path: Path, payload: bytes, *, exclusive: bool = True) -> None:
        self._fail(DurabilityStage.WRITE)
        super().write_bytes(path, payload, exclusive=exclusive)

    def fsync_file(self, path: Path) -> None:
        self._fail(DurabilityStage.FSYNC_FILE)
        super().fsync_file(path)

    def replace(self, source: Path, destination: Path) -> None:
        self._fail(DurabilityStage.RENAME)
        super().replace(source, destination)

    def fsync_directory(self, path: Path) -> None:
        self._fail(DurabilityStage.FSYNC_DIRECTORY)
        super().fsync_directory(path)


@pytest.mark.parametrize(
    "stage",
    [
        DurabilityStage.WRITE,
        DurabilityStage.FSYNC_FILE,
        DurabilityStage.RENAME,
        DurabilityStage.FSYNC_DIRECTORY,
    ],
)
def test_write_fsync_rename_probe_reports_exact_failure_stage(tmp_path: Path, stage: DurabilityStage) -> None:
    probe = WriteFsyncRenameProbe(FailingOperations(stage, errno.ENOSPC))

    result = probe.inspect(tmp_path)

    assert not result.ok
    assert result.failed_stage is stage
    assert result.error_number == errno.ENOSPC
    assert not list(tmp_path.glob(".bongus-storage-probe-*"))


class InjectedDurabilityProbe:
    def __init__(self, result: DurabilityProbeResult) -> None:
        self.result = result

    def inspect(self, directory: Path) -> DurabilityProbeResult:
        return self.result


class RaisingDurabilityProbe:
    def inspect(self, directory: Path) -> DurabilityProbeResult:
        raise OSError(errno.EIO, "injected probe crash")


def test_enospc_durability_probe_forces_sticky_critical_state(tmp_path: Path) -> None:
    watched = tmp_path / "state.db"
    result = DurabilityProbeResult(
        directory=tmp_path,
        ok=False,
        latency_ms=1.0,
        failed_stage=DurabilityStage.WRITE,
        error="OSError: no space",
        error_number=errno.ENOSPC,
    )
    guard, _, _ = guard_for_free_space(
        watched,
        total=16 * DECIMAL_GB,
        free=8 * DECIMAL_GB,
        durability_probe=InjectedDurabilityProbe(result),
    )

    snapshot = guard.sample(run_durability_probes=True)

    assert snapshot.state is StorageState.CRITICAL
    assert StorageFault.ENOSPC in snapshot.active_faults
    assert snapshot.risk_increase_blocked
    assert snapshot.emergency_latched


def test_raised_durability_probe_error_also_fails_closed(tmp_path: Path) -> None:
    watched = tmp_path / "state.db"
    guard, _, _ = guard_for_free_space(
        watched,
        total=16 * DECIMAL_GB,
        free=8 * DECIMAL_GB,
        durability_probe=RaisingDurabilityProbe(),
    )

    snapshot = guard.sample(run_durability_probes=True)

    assert snapshot.state is StorageState.CRITICAL
    assert StorageFault.MANDATORY_WRITE_FAILED in snapshot.active_faults
    assert snapshot.durability_probes[0].error == "OSError: [Errno 5] injected probe crash"


def test_atomic_health_snapshot_is_complete_and_bounded(tmp_path: Path) -> None:
    watched = tmp_path / "state.db"
    guard, _, _ = guard_for_free_space(watched, total=16 * DECIMAL_GB, free=8 * DECIMAL_GB)
    snapshot = guard.sample()
    target = tmp_path / "health" / "storage.json"
    store = AtomicHealthSnapshotStore(target)

    store.write(snapshot)
    payload = store.read()

    assert payload["generation"] == snapshot.generation
    assert payload["state"] == "healthy"
    assert json.loads(target.read_text(encoding="utf-8"))["volumes"][0]["volume_id"] == "volume-a"
    assert not list(target.parent.glob(".*.tmp"))


def test_emergency_latch_survives_guard_restart(tmp_path: Path) -> None:
    monitored = tmp_path / "state.db"
    store = AtomicHealthSnapshotStore(tmp_path / "storage-health.json")
    guard, probe, _ = guard_for_free_space(
        monitored,
        total=16 * DECIMAL_GB,
        free=1_500_000_000,
        snapshot_store=store,
    )
    assert guard.sample().emergency_latched

    probe.observations[monitored] = (
        "volume-a",
        16 * DECIMAL_GB,
        8 * DECIMAL_GB,
    )
    restarted, _, _ = guard_for_free_space(
        monitored,
        total=16 * DECIMAL_GB,
        free=8 * DECIMAL_GB,
        snapshot_store=store,
    )

    recovered_sample = restarted.sample(
        integrity_ok=True,
        exchange_reconciled=True,
    )
    assert recovered_sample.risk_increase_blocked
    assert recovered_sample.emergency_latched
    assert not restarted.allows(StorageAction.ENTRY)


@pytest.mark.parametrize(
    ("free_bytes", "expected_state", "entry_allowed", "emergency_latched"),
    [
        (8_000_000_000, StorageState.HEALTHY, True, False),
        (3_500_000_000, StorageState.WARNING, True, False),
        (2_500_000_000, StorageState.DEGRADED, False, False),
        (1_500_000_000, StorageState.EMERGENCY, False, True),
        (500_000_000, StorageState.CRITICAL, False, True),
    ],
)
def test_accelerated_restart_matrix_preserves_every_storage_state(
    tmp_path: Path,
    free_bytes: int,
    expected_state: StorageState,
    entry_allowed: bool,
    emergency_latched: bool,
) -> None:
    monitored = tmp_path / "state.db"
    store = AtomicHealthSnapshotStore(tmp_path / "storage-health.json")
    guard, _, _ = guard_for_free_space(
        monitored,
        total=16 * DECIMAL_GB,
        free=free_bytes,
        snapshot_store=store,
    )
    before_restart = guard.sample(
        integrity_ok=True,
        exchange_reconciled=True,
    )
    assert before_restart.state is expected_state

    restarted, _, _ = guard_for_free_space(
        monitored,
        total=16 * DECIMAL_GB,
        free=free_bytes,
        snapshot_store=store,
    )
    after_restart = restarted.sample(
        integrity_ok=True,
        exchange_reconciled=True,
    )

    assert after_restart.state is expected_state
    assert restarted.allows(StorageAction.ENTRY) is entry_allowed
    assert after_restart.emergency_latched is emergency_latched
    assert restarted.allows(StorageAction.CANCEL_ENTRY)
    assert restarted.allows(StorageAction.RECONCILIATION)
    assert restarted.allows(StorageAction.EXIT)


def test_reported_write_fault_is_durable_before_the_next_sample(tmp_path: Path) -> None:
    monitored = tmp_path / "state.db"
    store = AtomicHealthSnapshotStore(tmp_path / "storage-health.json")
    guard, _, _ = guard_for_free_space(
        monitored,
        total=16 * DECIMAL_GB,
        free=8 * DECIMAL_GB,
        snapshot_store=store,
    )
    guard.sample(integrity_ok=True, exchange_reconciled=True)

    guard.report_fault(StorageFault.SQLITE_FULL)

    restarted, _, _ = guard_for_free_space(
        monitored,
        total=16 * DECIMAL_GB,
        free=8 * DECIMAL_GB,
        snapshot_store=store,
    )
    assert restarted.risk_increase_blocked
    assert restarted.emergency_latched
    assert not restarted.allows(StorageAction.ENTRY)


def test_corrupt_persisted_snapshot_fails_closed(tmp_path: Path) -> None:
    monitored = tmp_path / "state.db"
    snapshot_path = tmp_path / "storage-health.json"
    snapshot_path.write_text("{not-json", encoding="utf-8")

    restarted, _, _ = guard_for_free_space(
        monitored,
        total=16 * DECIMAL_GB,
        free=8 * DECIMAL_GB,
        snapshot_store=AtomicHealthSnapshotStore(snapshot_path),
    )

    snapshot = restarted.sample(integrity_ok=True, exchange_reconciled=True)
    assert snapshot.state is StorageState.CRITICAL
    assert snapshot.risk_increase_blocked
    assert snapshot.emergency_latched
    assert StorageFault.PROBE_FAILED in snapshot.active_faults


def test_atomic_snapshot_rename_failure_preserves_old_snapshot(tmp_path: Path) -> None:
    target = tmp_path / "storage.json"
    target.write_text('{"generation":0}\n', encoding="utf-8")
    watched = tmp_path / "state.db"
    guard, _, _ = guard_for_free_space(watched, total=16 * DECIMAL_GB, free=8 * DECIMAL_GB)
    snapshot = guard.sample()
    store = AtomicHealthSnapshotStore(target, FailingOperations(DurabilityStage.RENAME))

    with pytest.raises(DurabilityError) as raised:
        store.write(snapshot)

    assert raised.value.stage is DurabilityStage.RENAME
    assert json.loads(target.read_text(encoding="utf-8"))["generation"] == 0


def test_snapshot_store_failure_is_reflected_in_in_memory_health(tmp_path: Path) -> None:
    watched = tmp_path / "state.db"
    store = AtomicHealthSnapshotStore(
        tmp_path / "storage.json",
        FailingOperations(DurabilityStage.FSYNC_FILE),
    )
    guard, _, _ = guard_for_free_space(
        watched,
        total=16 * DECIMAL_GB,
        free=8 * DECIMAL_GB,
        snapshot_store=store,
    )

    snapshot = guard.sample()

    assert snapshot.state is StorageState.CRITICAL
    assert StorageFault.MANDATORY_FSYNC_FAILED in snapshot.active_faults
    assert any("health_snapshot_write_failed:fsync_file" in reason for reason in snapshot.reasons)


def test_snapshot_reference_is_atomic_under_concurrent_samples(path: Path) -> None:
    guard, _, _ = guard_for_free_space(path, total=16 * DECIMAL_GB, free=8 * DECIMAL_GB)
    generations: list[int] = []

    def take_sample() -> None:
        generations.append(guard.sample().generation)

    threads = [threading.Thread(target=take_sample) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(generations) == list(range(1, 9))
    final = guard.snapshot()
    assert final is not None
    assert final.generation == 8
    assert len(final.volumes) == 1


def make_cleanup(tmp_path: Path, *, protected: tuple[Path, ...] = ()) -> tuple[SafeCleanup, CleanupRoot]:
    root = CleanupRoot(tmp_path / "owned")
    SafeCleanup.initialize_owned_root(root)
    return SafeCleanup(CleanupPolicy((root,), protected)), root


def test_allowlisted_owned_cleanup_removes_only_the_selected_tree(tmp_path: Path) -> None:
    cleanup, root = make_cleanup(tmp_path)
    target = root.path / "stale"
    target.mkdir()
    (target / "a.log").write_bytes(b"abc")
    nested = target / "nested"
    nested.mkdir()
    (nested / "b.tmp").write_bytes(b"12345")
    keep = root.path / "keep.log"
    keep.write_text("keep", encoding="utf-8")

    result = cleanup.remove(target)

    assert result.deleted
    assert result.files_deleted == 2
    assert result.directories_deleted == 2
    assert result.bytes_reclaimed == 8
    assert not target.exists()
    assert keep.exists()


def test_cleanup_refuses_outside_unowned_root_and_protected_intersections(tmp_path: Path) -> None:
    protected = tmp_path / "owned" / "tier-a" / "state.db"
    cleanup, root = make_cleanup(tmp_path, protected=(protected,))
    tier_a = protected.parent
    tier_a.mkdir()
    protected.write_bytes(b"critical")

    with pytest.raises(UnsafeCleanupPath, match="outside"):
        cleanup.remove(tmp_path / "outside")
    with pytest.raises(UnsafeCleanupPath, match="protected"):
        cleanup.remove(tier_a)
    with pytest.raises(UnsafeCleanupPath, match="root itself"):
        cleanup.remove(root.path)
    assert protected.read_bytes() == b"critical"

    unowned = CleanupRoot(tmp_path / "unowned")
    unowned.path.mkdir()
    candidate = unowned.path / "candidate"
    candidate.mkdir()
    with pytest.raises(UnsafeCleanupPath, match="marker missing"):
        SafeCleanup(CleanupPolicy((unowned,), ())).remove(candidate)


def test_cleanup_rejects_symlink_escape_before_deleting_any_sibling(tmp_path: Path) -> None:
    cleanup, root = make_cleanup(tmp_path)
    target = root.path / "stale"
    target.mkdir()
    ordinary = target / "ordinary.log"
    ordinary.write_text("must remain", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    linked = target / "escape"
    try:
        linked.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable on this Windows account: {exc}")

    with pytest.raises(UnsafeCleanupPath, match="symlink or reparse"):
        cleanup.remove(target)

    assert ordinary.read_text(encoding="utf-8") == "must remain"
    assert outside.read_text(encoding="utf-8") == "outside"
    assert linked.is_symlink()


def test_cleanup_refuses_symlink_in_path_to_target(tmp_path: Path) -> None:
    cleanup, root = make_cleanup(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim").mkdir()
    link = root.path / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable on this Windows account: {exc}")

    with pytest.raises(UnsafeCleanupPath, match="symlink or reparse"):
        cleanup.remove(link / "victim")
    assert (outside / "victim").exists()


def test_system_probe_discovers_actual_volume_for_nonexistent_child(tmp_path: Path) -> None:
    missing = tmp_path / "not-created" / "state.db"
    mount = volume_root_for_path(missing)
    usage = SystemDiskProbe().inspect(missing)

    assert mount.exists()
    assert usage.mount_path == mount
    assert usage.total_bytes > 0
    assert 0 <= usage.free_bytes <= usage.total_bytes
    assert usage.observed_path == missing
