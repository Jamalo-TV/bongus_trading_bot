"""Storage pressure detection and fail-closed recovery primitives.

The guard deliberately has no trading or process-management dependencies.  It
turns filesystem observations into an immutable health snapshot and exposes a
small operation gate which callers can enforce independently.  Disk and file
durability probes are injectable so failure campaigns never need to fill the
host filesystem.

Destructive cleanup is also kept separate from health sampling.  ``SafeCleanup``
only removes explicitly allowlisted, owned paths and refuses symbolic links,
Windows reparse points, and any path intersecting the protected set.
"""

from __future__ import annotations

import errno
import json
import os
import secrets
import shutil
import stat
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping, Protocol, Sequence

DECIMAL_MB = 1_000_000
DECIMAL_GB = 1_000_000_000
BINARY_MB = 1024 * 1024
DEFAULT_VOLUME_BUDGET_BYTES = 60 * DECIMAL_GB
DEFAULT_BASE_RUNTIME_RESERVATION_BYTES = 2 * DECIMAL_GB
DEFAULT_UNMANAGED_CONTINGENCY_BYTES = 1_150_000_000
DEFAULT_NORMAL_FREE_HEADROOM_BYTES = 20 * DECIMAL_GB
DEFAULT_RECOVERY_HYSTERESIS_BYTES = 512_000_000
DEFAULT_RESERVE_BYTES = 1 * DECIMAL_GB


class StorageState(str, Enum):
    """Ordered storage-health states."""

    HEALTHY = "healthy"
    WARNING = "warning"
    DEGRADED = "degraded"
    EMERGENCY = "emergency"
    CRITICAL = "critical"

    @property
    def severity(self) -> int:
        return _STATE_SEVERITY[self]


_STATE_SEVERITY = {
    StorageState.HEALTHY: 0,
    StorageState.WARNING: 1,
    StorageState.DEGRADED: 2,
    StorageState.EMERGENCY: 3,
    StorageState.CRITICAL: 4,
}


def _worst_state(*states: StorageState) -> StorageState:
    return max(states or (StorageState.HEALTHY,), key=lambda state: state.severity)


class StorageAction(str, Enum):
    """Classes of work which have different storage-pressure permissions."""

    ENTRY = "entry"
    ROTATION = "rotation"
    OPTIONAL_WRITE = "optional_write"
    RETENTION = "retention"
    BACKUP = "backup"
    CANCEL_ENTRY = "cancel_entry"
    HEDGE_REPAIR = "hedge_repair"
    EXIT = "exit"
    RECONCILIATION = "reconciliation"
    CRITICAL_WRITE = "critical_write"
    CLEANUP = "cleanup"


class StorageFault(str, Enum):
    SQLITE_FULL = "sqlite_full"
    ENOSPC = "enospc"
    MANDATORY_WRITE_FAILED = "mandatory_write_failed"
    MANDATORY_FSYNC_FAILED = "mandatory_fsync_failed"
    ATOMIC_RENAME_FAILED = "atomic_rename_failed"
    DATABASE_CORRUPT = "database_corrupt"
    PROBE_FAILED = "probe_failed"


class StorageComponent(str, Enum):
    BASE_RUNTIME = "base_runtime"
    UNMANAGED_CONTINGENCY = "unmanaged_contingency"
    APPLICATION = "application"
    PYTHON_RUNTIME = "python_runtime"
    HOT_STATE = "hot_state_sqlite"
    SQLITE_SCRATCH = "sqlite_wal_shm_scratch"
    AUDIT_ARCHIVE = "audit_archive"
    VERIFIED_BACKUP = "verified_backup"
    RESEARCH = "research_inputs"
    LOGS = "logs_support"
    RUST_JOURNALS = "rust_journals_cursors"
    MODELS_CACHES = "models_caches"
    OWNED_TEMP = "owned_temp_crash_test"
    EMERGENCY_RESERVE = "emergency_reserve"
    FREE_HEADROOM = "free_headroom"


# The component table is decimal by design: it is one exact whole-volume
# 60.00 GB deployment model.  It reserves 20.00 GB (33.33%) as normal free
# headroom while explicitly accounting for the OS/runtime, contingency,
# emergency reserve, every application-owned tier, the observed 5.13 GB legacy
# state image, and the publish peak of an old plus staging split-store backup
# generation.  The 20.50 GB backup cap keeps two 8 GB sets plus metadata and
# growth margin below the component's 80% warning point. The 600 MB Rust tier
# also covers live journals plus the old/new immutable-generation peak.
DEFAULT_COMPONENT_LIMITS: Mapping[StorageComponent, int] = MappingProxyType(
    {
        StorageComponent.BASE_RUNTIME: DEFAULT_BASE_RUNTIME_RESERVATION_BYTES,
        StorageComponent.UNMANAGED_CONTINGENCY: DEFAULT_UNMANAGED_CONTINGENCY_BYTES,
        StorageComponent.APPLICATION: 200_000_000,
        StorageComponent.PYTHON_RUNTIME: 600_000_000,
        StorageComponent.HOT_STATE: 6_500_000_000,
        StorageComponent.SQLITE_SCRATCH: 1_000_000_000,
        StorageComponent.AUDIT_ARCHIVE: 1_500_000_000,
        StorageComponent.VERIFIED_BACKUP: 20_500_000_000,
        StorageComponent.RESEARCH: 4_000_000_000,
        StorageComponent.LOGS: 500_000_000,
        StorageComponent.RUST_JOURNALS: 600_000_000,
        StorageComponent.MODELS_CACHES: 250_000_000,
        StorageComponent.OWNED_TEMP: 200_000_000,
        StorageComponent.EMERGENCY_RESERVE: DEFAULT_RESERVE_BYTES,
        StorageComponent.FREE_HEADROOM: DEFAULT_NORMAL_FREE_HEADROOM_BYTES,
    }
)

if sum(DEFAULT_COMPONENT_LIMITS.values()) != DEFAULT_VOLUME_BUDGET_BYTES:
    raise RuntimeError("default storage component limits must total exactly 60.00 GB")


@dataclass(frozen=True, slots=True)
class ComponentBudget:
    name: str | StorageComponent
    path: Path
    hard_limit_bytes: int
    warning_ratio: float = 0.80
    breach_state: StorageState = StorageState.DEGRADED
    additional_paths: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        normalized_name = self.name.value if isinstance(self.name, StorageComponent) else str(self.name).strip()
        if not normalized_name:
            raise ValueError("component name must not be empty")
        if self.hard_limit_bytes <= 0:
            raise ValueError("component hard_limit_bytes must be positive")
        if not 0 < self.warning_ratio < 1:
            raise ValueError("component warning_ratio must be between zero and one")
        if self.breach_state.severity < StorageState.DEGRADED.severity:
            raise ValueError("component breach_state must be degraded or worse")
        primary_path = Path(self.path)
        paths = tuple(
            dict.fromkeys(
                (primary_path, *(Path(path) for path in self.additional_paths))
            )
        )
        object.__setattr__(self, "name", normalized_name)
        object.__setattr__(self, "path", primary_path)
        object.__setattr__(self, "additional_paths", paths[1:])

    @property
    def paths(self) -> tuple[Path, ...]:
        return (self.path, *self.additional_paths)


@dataclass(frozen=True, slots=True)
class StoragePolicy:
    """Validated thresholds for a storage guard instance."""

    components: tuple[ComponentBudget, ...] = ()
    monitored_paths: tuple[Path, ...] = ()
    volume_budget_bytes: int = DEFAULT_VOLUME_BUDGET_BYTES
    base_runtime_reservation_bytes: int = DEFAULT_BASE_RUNTIME_RESERVATION_BYTES
    unmanaged_contingency_bytes: int = DEFAULT_UNMANAGED_CONTINGENCY_BYTES
    warning_free_bytes: int = DEFAULT_NORMAL_FREE_HEADROOM_BYTES
    degraded_free_bytes: int = 15 * DECIMAL_GB
    emergency_free_bytes: int = 10 * DECIMAL_GB
    critical_free_bytes: int = 5 * DECIMAL_GB
    warning_free_ratio: float = 1 / 3
    degraded_free_ratio: float = 0.25
    emergency_free_ratio: float = 1 / 6
    critical_free_ratio: float = 1 / 12
    warning_ttf_hours: float = 72.0
    degraded_ttf_hours: float = 24.0
    emergency_ttf_hours: float = 6.0
    critical_ttf_hours: float = 1.0
    minimum_rate_interval_seconds: float = 300.0
    volume_minimum_rate_interval_seconds: float = 300.0
    rate_intervals_required: int = 2
    ema_alpha: float = 0.30
    recovery_samples: int = 3
    recovery_hysteresis_bytes: int = DEFAULT_RECOVERY_HYSTERESIS_BYTES
    reserve_bytes: int = DEFAULT_RESERVE_BYTES

    def __post_init__(self) -> None:
        components = tuple(self.components)
        monitored_paths = tuple(Path(path) for path in self.monitored_paths)
        names = [str(component.name) for component in components]
        if len(names) != len(set(names)):
            raise ValueError("component names must be unique")
        if not components and not monitored_paths:
            raise ValueError("at least one component or monitored path is required")
        if self.volume_budget_bytes <= self.warning_free_bytes:
            raise ValueError(
                "volume_budget_bytes must exceed the normal free-space headroom"
            )
        if self.base_runtime_reservation_bytes < 0:
            raise ValueError("base_runtime_reservation_bytes must not be negative")
        if self.unmanaged_contingency_bytes < 0:
            raise ValueError("unmanaged_contingency_bytes must not be negative")
        byte_thresholds = (
            self.warning_free_bytes,
            self.degraded_free_bytes,
            self.emergency_free_bytes,
            self.critical_free_bytes,
        )
        if any(value <= 0 for value in byte_thresholds) or not all(
            left > right for left, right in zip(byte_thresholds, byte_thresholds[1:])
        ):
            raise ValueError("free-byte thresholds must be positive and strictly descending")
        ratio_thresholds = (
            self.warning_free_ratio,
            self.degraded_free_ratio,
            self.emergency_free_ratio,
            self.critical_free_ratio,
        )
        if not all(0 <= value <= 1 for value in ratio_thresholds) or not all(
            left > right for left, right in zip(ratio_thresholds, ratio_thresholds[1:])
        ):
            raise ValueError("free-ratio thresholds must be in [0, 1] and strictly descending")
        ttf_thresholds = (
            self.warning_ttf_hours,
            self.degraded_ttf_hours,
            self.emergency_ttf_hours,
            self.critical_ttf_hours,
        )
        if any(value <= 0 for value in ttf_thresholds) or not all(
            left > right for left, right in zip(ttf_thresholds, ttf_thresholds[1:])
        ):
            raise ValueError("TTF thresholds must be positive and strictly descending")
        if self.minimum_rate_interval_seconds <= 0:
            raise ValueError("minimum_rate_interval_seconds must be positive")
        if self.volume_minimum_rate_interval_seconds <= 0:
            raise ValueError("volume_minimum_rate_interval_seconds must be positive")
        if self.rate_intervals_required < 1:
            raise ValueError("rate_intervals_required must be positive")
        if not 0 < self.ema_alpha <= 1:
            raise ValueError("ema_alpha must be in (0, 1]")
        if self.recovery_samples < 1:
            raise ValueError("recovery_samples must be positive")
        if self.recovery_hysteresis_bytes < 0:
            raise ValueError("recovery_hysteresis_bytes must not be negative")
        if self.reserve_bytes <= 0:
            raise ValueError("reserve_bytes must be positive")
        object.__setattr__(self, "components", components)
        object.__setattr__(self, "monitored_paths", monitored_paths)

    @property
    def all_paths(self) -> tuple[Path, ...]:
        return tuple(
            dict.fromkeys(
                (
                    *self.monitored_paths,
                    *(
                        path
                        for component in self.components
                        for path in component.paths
                    ),
                )
            )
        )


@dataclass(frozen=True, slots=True)
class VolumeUsage:
    volume_id: str
    mount_path: Path
    total_bytes: int
    free_bytes: int
    observed_path: Path

    def __post_init__(self) -> None:
        if not self.volume_id:
            raise ValueError("volume_id must not be empty")
        if self.total_bytes <= 0:
            raise ValueError("total_bytes must be positive")
        if not 0 <= self.free_bytes <= self.total_bytes:
            raise ValueError("free_bytes must be within the volume capacity")
        object.__setattr__(self, "mount_path", Path(self.mount_path))
        object.__setattr__(self, "observed_path", Path(self.observed_path))

    @property
    def free_ratio(self) -> float:
        return self.free_bytes / self.total_bytes


class DiskProbe(Protocol):
    def inspect(self, path: Path) -> VolumeUsage:
        """Return capacity and stable volume identity for ``path``."""

        ...


class ComponentSizeProbe(Protocol):
    def size_bytes(self, path: Path) -> int:
        """Return bytes owned by one configured component."""

        ...


def _nearest_existing_path(path: Path) -> Path:
    candidate = path.expanduser().absolute()
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            raise FileNotFoundError(f"no existing ancestor for {path}")
        candidate = parent
    return candidate.resolve(strict=True)


def _windows_volume_root(path: Path) -> Path:
    if sys.platform != "win32":
        raise OSError("Windows volume lookup is unavailable on this platform")
    import ctypes

    buffer_length = 32768
    buffer = ctypes.create_unicode_buffer(buffer_length)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_volume_path = kernel32.GetVolumePathNameW
    get_volume_path.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong]
    get_volume_path.restype = ctypes.c_int
    if not get_volume_path(str(path), buffer, buffer_length):
        error = ctypes.get_last_error()
        raise OSError(error, os.strerror(error), str(path))
    return Path(buffer.value)


def _unix_mount_root(path: Path) -> Path:
    current = path if path.is_dir() else path.parent
    current = current.resolve(strict=True)
    while current.parent != current and not os.path.ismount(current):
        current = current.parent
    return current


def volume_root_for_path(path: str | os.PathLike[str]) -> Path:
    """Resolve a path to its actual Windows volume or Unix mount point."""

    existing = _nearest_existing_path(Path(path))
    if os.name == "nt":
        return _windows_volume_root(existing)
    return _unix_mount_root(existing)


class SystemDiskProbe:
    """Production disk probe with no hard-coded drive letters."""

    def inspect(self, path: Path) -> VolumeUsage:
        existing = _nearest_existing_path(path)
        mount_path = volume_root_for_path(existing)
        usage = shutil.disk_usage(mount_path)
        if os.name == "nt":
            volume_id = os.path.normcase(str(mount_path.resolve(strict=False)))
        else:
            device = os.stat(existing).st_dev
            volume_id = f"dev:{device}:{mount_path}"
        return VolumeUsage(
            volume_id=volume_id,
            mount_path=mount_path,
            total_bytes=int(usage.total),
            free_bytes=int(usage.free),
            observed_path=path,
        )


class RecursivePathSizeProbe:
    """Count owned bytes without following symlinks or reparse points."""

    def size_bytes(self, path: Path) -> int:
        try:
            path_stat = path.lstat()
        except FileNotFoundError:
            return 0
        if _is_link_or_reparse(path, path_stat):
            return int(path_stat.st_size)
        if stat.S_ISREG(path_stat.st_mode):
            return int(path_stat.st_size)
        if not stat.S_ISDIR(path_stat.st_mode):
            return 0

        total = 0
        stack = [path]
        while stack:
            directory = stack.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    entry_stat = entry.stat(follow_symlinks=False)
                    entry_path = Path(entry.path)
                    if _is_link_or_reparse(entry_path, entry_stat):
                        total += int(entry_stat.st_size)
                    elif stat.S_ISDIR(entry_stat.st_mode):
                        stack.append(entry_path)
                    elif stat.S_ISREG(entry_stat.st_mode):
                        total += int(entry_stat.st_size)
        return total


class DurabilityStage(str, Enum):
    WRITE = "write"
    FSYNC_FILE = "fsync_file"
    RENAME = "rename"
    FSYNC_DIRECTORY = "fsync_directory"
    VERIFY = "verify"
    CLEANUP = "cleanup"


class DurabilityError(OSError):
    def __init__(self, stage: DurabilityStage, cause: Exception):
        self.stage = stage
        self.cause = cause
        error_number = cause.errno if isinstance(cause, OSError) else None
        super().__init__(error_number or errno.EIO, f"{stage.value} failed: {cause}")


class DurableFileOperations(Protocol):
    """Injectable write/fsync/rename boundary used by failure tests."""

    def write_bytes(self, path: Path, payload: bytes, *, exclusive: bool = True) -> None: ...

    def fsync_file(self, path: Path) -> None: ...

    def replace(self, source: Path, destination: Path) -> None: ...

    def fsync_directory(self, path: Path) -> None: ...

    def unlink(self, path: Path) -> None: ...


class OSDurableFileOperations:
    def write_bytes(self, path: Path, payload: bytes, *, exclusive: bool = True) -> None:
        mode = "xb" if exclusive else "wb"
        with path.open(mode) as handle:
            handle.write(payload)
            handle.flush()

    def fsync_file(self, path: Path) -> None:
        descriptor = os.open(path, os.O_RDWR)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def replace(self, source: Path, destination: Path) -> None:
        os.replace(source, destination)

    def fsync_directory(self, path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            # Windows cannot normally open directories through os.open.  The
            # file itself was fsynced before the atomic rename.
            if os.name == "nt":
                return
            raise
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def unlink(self, path: Path) -> None:
        path.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class DurabilityProbeResult:
    directory: Path
    ok: bool
    latency_ms: float
    failed_stage: DurabilityStage | None = None
    error: str | None = None
    error_number: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "directory": str(self.directory),
            "ok": self.ok,
            "latency_ms": self.latency_ms,
            "failed_stage": self.failed_stage.value if self.failed_stage else None,
            "error": self.error,
            "error_number": self.error_number,
        }


class WriteFsyncRenameProbe:
    """Exercise the complete durable atomic-replacement path in a directory."""

    def __init__(self, operations: DurableFileOperations | None = None) -> None:
        self._operations = operations or OSDurableFileOperations()

    def inspect(self, directory: Path) -> DurabilityProbeResult:
        directory = Path(directory)
        token = uuid.uuid4().hex
        temporary = directory / f".bongus-storage-probe-{token}.tmp"
        destination = directory / f".bongus-storage-probe-{token}.ok"
        payload = f"bongus-storage-probe-v1:{token}\n".encode("ascii")
        started = time.monotonic()
        stage = DurabilityStage.WRITE
        error: Exception | None = None
        try:
            self._operations.write_bytes(temporary, payload, exclusive=True)
            stage = DurabilityStage.FSYNC_FILE
            self._operations.fsync_file(temporary)
            stage = DurabilityStage.RENAME
            self._operations.replace(temporary, destination)
            stage = DurabilityStage.FSYNC_DIRECTORY
            self._operations.fsync_directory(directory)
            stage = DurabilityStage.VERIFY
            if destination.read_bytes() != payload:
                raise OSError(errno.EIO, "durability probe content mismatch")
        except Exception as exc:  # returned as data so the guard can fail closed
            error = exc
        finally:
            cleanup_error: Exception | None = None
            for path in (temporary, destination):
                try:
                    self._operations.unlink(path)
                except Exception as exc:
                    cleanup_error = cleanup_error or exc
            try:
                self._operations.fsync_directory(directory)
            except Exception as exc:
                cleanup_error = cleanup_error or exc
            if error is None and cleanup_error is not None:
                error = cleanup_error
                stage = DurabilityStage.CLEANUP

        latency_ms = (time.monotonic() - started) * 1000.0
        if error is None:
            return DurabilityProbeResult(directory=directory, ok=True, latency_ms=latency_ms)
        return DurabilityProbeResult(
            directory=directory,
            ok=False,
            latency_ms=latency_ms,
            failed_stage=stage,
            error=f"{type(error).__name__}: {error}",
            error_number=error.errno if isinstance(error, OSError) else None,
        )


class DurabilityProbe(Protocol):
    def inspect(self, directory: Path) -> DurabilityProbeResult: ...


@dataclass(frozen=True, slots=True)
class ComponentHealth:
    name: str
    path: Path
    paths: tuple[Path, ...]
    used_bytes: int
    budget_bytes: int
    utilization: float
    growth_bytes_per_hour: float
    time_to_full_hours: float | None
    state: StorageState
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["path"] = str(self.path)
        payload["paths"] = [str(path) for path in self.paths]
        payload["state"] = self.state.value
        return payload


@dataclass(frozen=True, slots=True)
class VolumeHealth:
    volume_id: str
    mount_path: Path
    observed_paths: tuple[Path, ...]
    total_bytes: int
    free_bytes: int
    free_ratio: float
    consumption_bytes_per_hour: float
    time_to_full_hours: float | None
    state: StorageState
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["mount_path"] = str(self.mount_path)
        payload["observed_paths"] = [str(path) for path in self.observed_paths]
        payload["state"] = self.state.value
        return payload


@dataclass(frozen=True, slots=True)
class ReserveStatus:
    path: Path
    configured_bytes: int
    present: bool
    logical_bytes: int
    allocated_bytes: int | None
    staging_present: bool
    released: bool

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["path"] = str(self.path)
        return payload


class ReserveError(RuntimeError):
    pass


def _is_link_or_reparse(path: Path, path_stat: os.stat_result | None = None) -> bool:
    path_stat = path_stat or path.lstat()
    if stat.S_ISLNK(path_stat.st_mode):
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(path_stat, "st_file_attributes", 0) & reparse_flag)


def _allocated_bytes(path: Path, path_stat: os.stat_result | None = None) -> int | None:
    path_stat = path_stat or path.stat()
    blocks = getattr(path_stat, "st_blocks", None)
    if blocks is not None:
        return int(blocks) * 512
    if os.name != "nt":
        return None

    # GetCompressedFileSizeW returns allocated bytes for compressed and sparse
    # files too, allowing us to reject a nominally large but physically tiny
    # reserve on Windows.
    try:
        import ctypes

        high = ctypes.c_ulong(0)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        function = kernel32.GetCompressedFileSizeW
        function.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
        function.restype = ctypes.c_ulong
        low = function(str(path), ctypes.byref(high))
        if low == 0xFFFFFFFF:
            error = ctypes.get_last_error()
            if error:
                return None
        return (int(high.value) << 32) | int(low)
    except (AttributeError, OSError):
        return None


def _fsync_directory(path: Path) -> None:
    OSDurableFileOperations().fsync_directory(path)


class EmergencyReserve:
    """A crash-recoverable, fsynced and physically allocated reserve file."""

    def __init__(self, path: str | os.PathLike[str], size_bytes: int = DEFAULT_RESERVE_BYTES) -> None:
        if size_bytes <= 0:
            raise ValueError("reserve size_bytes must be positive")
        self.path = Path(path).absolute()
        self.size_bytes = int(size_bytes)
        self._staging_path = self.path.with_name(f".{self.path.name}.allocating")
        self._lock = threading.RLock()
        self._released = False
        self._last_released_bytes = 0

    @property
    def last_released_bytes(self) -> int:
        with self._lock:
            return self._last_released_bytes

    def _validate_file(self, path: Path, *, require_complete: bool) -> tuple[int, int | None]:
        path_stat = path.lstat()
        if _is_link_or_reparse(path, path_stat) or not stat.S_ISREG(path_stat.st_mode):
            raise ReserveError(f"reserve path is not a regular non-reparse file: {path}")
        logical = int(path_stat.st_size)
        if require_complete and logical != self.size_bytes:
            raise ReserveError(f"reserve has size {logical}, expected {self.size_bytes}: {path}")
        if logical > self.size_bytes:
            raise ReserveError(f"reserve staging file exceeds configured size: {path}")
        allocated = _allocated_bytes(path, path_stat)
        if require_complete and allocated is not None and allocated < self.size_bytes:
            raise ReserveError(
                f"reserve is sparse or compressed ({allocated} allocated for {self.size_bytes} bytes): {path}"
            )
        return logical, allocated

    def create(self) -> bool:
        """Create or resume the reserve; return ``True`` only for new work."""

        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            parent_stat = self.path.parent.lstat()
            if _is_link_or_reparse(self.path.parent, parent_stat):
                raise ReserveError(f"reserve parent must not be a symlink or reparse point: {self.path.parent}")

            if self.path.exists() or self.path.is_symlink():
                self._validate_file(self.path, require_complete=True)
                if self._staging_path.exists() or self._staging_path.is_symlink():
                    self._validate_file(self._staging_path, require_complete=False)
                    self._staging_path.unlink()
                    _fsync_directory(self.path.parent)
                self._released = False
                return False

            existed = self._staging_path.exists() or self._staging_path.is_symlink()
            if existed:
                current_size, _ = self._validate_file(self._staging_path, require_complete=False)
            else:
                descriptor = os.open(self._staging_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                os.close(descriptor)
                current_size = 0

            # One random MiB is reused.  It is incompressible within filesystem
            # compression blocks and avoids allocating hundreds of MiB of RAM.
            block_size = min(BINARY_MB, self.size_bytes)
            block = secrets.token_bytes(block_size)
            with self._staging_path.open("r+b", buffering=0) as handle:
                handle.seek(current_size)
                remaining = self.size_bytes - current_size
                while remaining:
                    chunk = block if remaining >= block_size else block[:remaining]
                    written = handle.write(chunk)
                    if written != len(chunk):
                        raise ReserveError(f"short reserve write: {written} of {len(chunk)} bytes")
                    remaining -= written
                handle.flush()
                os.fsync(handle.fileno())
            self._validate_file(self._staging_path, require_complete=True)
            os.replace(self._staging_path, self.path)
            _fsync_directory(self.path.parent)
            self._validate_file(self.path, require_complete=True)
            self._released = False
            self._last_released_bytes = 0
            return True

    def release(self) -> bool:
        """Release the final and crash-staging files exactly once."""

        with self._lock:
            released_bytes = 0
            changed = False
            for candidate in (self.path, self._staging_path):
                if not candidate.exists() and not candidate.is_symlink():
                    continue
                logical, _ = self._validate_file(candidate, require_complete=False)
                candidate.unlink()
                released_bytes += logical
                changed = True
            if changed:
                _fsync_directory(self.path.parent)
            self._released = True
            self._last_released_bytes = released_bytes
            return changed

    def status(self) -> ReserveStatus:
        with self._lock:
            present = self.path.exists() or self.path.is_symlink()
            staging_present = self._staging_path.exists() or self._staging_path.is_symlink()
            logical = 0
            allocated: int | None = 0
            if present:
                logical, allocated = self._validate_file(self.path, require_complete=True)
            return ReserveStatus(
                path=self.path,
                configured_bytes=self.size_bytes,
                present=present,
                logical_bytes=logical,
                allocated_bytes=allocated,
                staging_present=staging_present,
                released=self._released and not present and not staging_present,
            )


@dataclass(frozen=True, slots=True)
class StorageHealthSnapshot:
    generation: int
    observed_at: datetime
    state: StorageState
    instantaneous_state: StorageState
    reasons: tuple[str, ...]
    volumes: tuple[VolumeHealth, ...]
    components: tuple[ComponentHealth, ...]
    durability_probes: tuple[DurabilityProbeResult, ...]
    volume_budget_bytes: int
    budgeted_consumption_bytes: int
    budgeted_free_headroom_bytes: int
    budgeted_utilization: float
    worst_time_to_full_hours: float | None
    risk_increase_blocked: bool
    emergency_latched: bool
    healthy_recovery_samples: int
    recovery_samples_required: int
    recovery_ready_for_operator: bool
    integrity_ok: bool
    exchange_reconciled: bool
    active_faults: tuple[StorageFault, ...]
    reserve: ReserveStatus | None
    # A TTF-triggered risk latch needs fresh rate evidence before a missing
    # projection can count as recovery.  These additive fields keep older
    # snapshot readers compatible while making that proof observable and
    # durable across process restarts.
    ttf_recovery_required: bool = False
    ttf_recovery_sources: tuple[str, ...] = ()
    ttf_recovery_observed_intervals: int = 0
    ttf_recovery_intervals_required: int = 0
    ttf_recovery_observation_ready: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "generation": self.generation,
            "observed_at": self.observed_at.astimezone(timezone.utc).isoformat(),
            "state": self.state.value,
            "instantaneous_state": self.instantaneous_state.value,
            "reasons": list(self.reasons),
            "volumes": [volume.to_dict() for volume in self.volumes],
            "components": [component.to_dict() for component in self.components],
            "durability_probes": [result.to_dict() for result in self.durability_probes],
            "volume_budget_bytes": self.volume_budget_bytes,
            "budgeted_consumption_bytes": self.budgeted_consumption_bytes,
            "budgeted_free_headroom_bytes": self.budgeted_free_headroom_bytes,
            "budgeted_utilization": self.budgeted_utilization,
            "worst_time_to_full_hours": self.worst_time_to_full_hours,
            "risk_increase_blocked": self.risk_increase_blocked,
            "emergency_latched": self.emergency_latched,
            "healthy_recovery_samples": self.healthy_recovery_samples,
            "recovery_samples_required": self.recovery_samples_required,
            "recovery_ready_for_operator": self.recovery_ready_for_operator,
            "integrity_ok": self.integrity_ok,
            "exchange_reconciled": self.exchange_reconciled,
            "active_faults": [fault.value for fault in self.active_faults],
            "reserve": self.reserve.to_dict() if self.reserve else None,
            "ttf_recovery_required": self.ttf_recovery_required,
            "ttf_recovery_sources": list(self.ttf_recovery_sources),
            "ttf_recovery_observed_intervals": (
                self.ttf_recovery_observed_intervals
            ),
            "ttf_recovery_intervals_required": (
                self.ttf_recovery_intervals_required
            ),
            "ttf_recovery_observation_ready": (
                self.ttf_recovery_observation_ready
            ),
        }


class AtomicHealthSnapshotStore:
    """Persist one bounded JSON health snapshot using fsync + atomic replace."""

    def __init__(self, path: str | os.PathLike[str], operations: DurableFileOperations | None = None) -> None:
        self.path = Path(path).absolute()
        self._operations = operations or OSDurableFileOperations()
        self._lock = threading.Lock()

    def write(self, snapshot: StorageHealthSnapshot) -> None:
        payload = json.dumps(snapshot.to_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        ) + b"\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        stage = DurabilityStage.WRITE
        with self._lock:
            try:
                self._operations.write_bytes(temporary, payload, exclusive=True)
                stage = DurabilityStage.FSYNC_FILE
                self._operations.fsync_file(temporary)
                stage = DurabilityStage.RENAME
                self._operations.replace(temporary, self.path)
                stage = DurabilityStage.FSYNC_DIRECTORY
                self._operations.fsync_directory(self.path.parent)
            except Exception as exc:
                raise DurabilityError(stage, exc) from exc
            finally:
                try:
                    self._operations.unlink(temporary)
                except OSError:
                    pass

    def read(self) -> Mapping[str, object]:
        with self.path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError("storage snapshot must be a JSON object")
        return payload


@dataclass(slots=True)
class _RateTracker:
    value: int
    timestamp: float
    ema_bytes_per_second: float = 0.0
    has_rate: bool = False
    positive_intervals: int = 0
    non_growth_intervals: int = 0


def _ttf(remaining_bytes: int, rate_bytes_per_second: float, has_rate: bool) -> float | None:
    if not has_rate or rate_bytes_per_second <= 0:
        return None
    return max(0.0, remaining_bytes / rate_bytes_per_second / 3600.0)


_AGGREGATE_TTF_SOURCE = "aggregate:whole_volume_budget"
_UNKNOWN_TTF_SOURCE = "unknown"


class StorageGuard:
    """Thread-safe state machine for storage pressure and risk admission."""

    _SURVIVAL_ACTIONS = frozenset(
        {
            StorageAction.CANCEL_ENTRY,
            StorageAction.HEDGE_REPAIR,
            StorageAction.EXIT,
            StorageAction.RECONCILIATION,
            StorageAction.CRITICAL_WRITE,
        }
    )

    def __init__(
        self,
        policy: StoragePolicy,
        *,
        disk_probe: DiskProbe | None = None,
        size_probe: ComponentSizeProbe | None = None,
        durability_probe: DurabilityProbe | None = None,
        reserve: EmergencyReserve | None = None,
        snapshot_store: AtomicHealthSnapshotStore | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] | None = None,
    ) -> None:
        self.policy = policy
        self._disk_probe = disk_probe or SystemDiskProbe()
        self._size_probe = size_probe or RecursivePathSizeProbe()
        self._durability_probe = durability_probe or WriteFsyncRenameProbe()
        self._reserve = reserve
        self._snapshot_store = snapshot_store
        self._monotonic = monotonic
        self._utcnow = utcnow or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._volume_rates: dict[str, _RateTracker] = {}
        self._component_rates: dict[str, _RateTracker] = {}
        self._state = StorageState.HEALTHY
        self._risk_blocked = False
        self._emergency_latched = False
        self._healthy_samples = 0
        self._recovery_ready = False
        self._integrity_ok = False
        self._exchange_reconciled = False
        self._active_faults: set[StorageFault] = set()
        self._ttf_recovery_sources: set[str] = set()
        self._generation = 0
        self._snapshot: StorageHealthSnapshot | None = None
        self._restore_persisted_latches()

    def _restore_persisted_latches(self) -> None:
        """Restore sticky safety state before the first post-restart sample.

        A healthy disk sample is not operator acknowledgement.  In particular,
        cleanup after releasing the reserve must not let a supervisor restart
        silently re-enable entries.
        """

        if self._snapshot_store is None:
            return
        try:
            payload = self._snapshot_store.read()
        except FileNotFoundError:
            return
        except Exception:
            self._state = StorageState.CRITICAL
            self._risk_blocked = True
            self._emergency_latched = True
            self._active_faults.add(StorageFault.PROBE_FAILED)
            return

        try:
            persisted_state = StorageState(str(payload.get("state", "healthy")))
            self._generation = max(0, int(str(payload.get("generation", 0))))
            self._risk_blocked = bool(payload.get("risk_increase_blocked", False))
            self._emergency_latched = bool(payload.get("emergency_latched", False))
            self._integrity_ok = bool(payload.get("integrity_ok", False))
            self._exchange_reconciled = bool(payload.get("exchange_reconciled", False))
            raw_faults = payload.get("active_faults", ())
            if not isinstance(raw_faults, list):
                raise ValueError("active_faults must be a list")
            self._active_faults = {StorageFault(str(value)) for value in raw_faults}
            raw_ttf_sources = payload.get("ttf_recovery_sources", ())
            if not isinstance(raw_ttf_sources, (list, tuple)):
                raise ValueError("ttf_recovery_sources must be a list")
            restored_ttf_sources = {
                str(value).strip()
                for value in raw_ttf_sources
                if str(value).strip()
            }
            # Snapshots written before the additive recovery-proof fields can
            # still identify the rate source from their stable reason format.
            if self._risk_blocked and not restored_ttf_sources:
                raw_reasons = payload.get("reasons", ())
                if not isinstance(raw_reasons, (list, tuple)):
                    raise ValueError("reasons must be a list")
                restored_ttf_sources = {
                    source
                    for reason in raw_reasons
                    if isinstance(reason, str)
                    for source in (self._ttf_source_from_reason(reason),)
                    if source is not None
                }
            if (
                bool(payload.get("ttf_recovery_required", False))
                and not restored_ttf_sources
            ):
                # A required proof with no attributable source must fail
                # closed rather than becoming ready from an empty set.
                restored_ttf_sources.add(_UNKNOWN_TTF_SOURCE)
            self._ttf_recovery_sources = restored_ttf_sources
            self._state = persisted_state
            if self._emergency_latched:
                self._risk_blocked = True
            if self._active_faults:
                self._state = StorageState.CRITICAL
                self._risk_blocked = True
                self._emergency_latched = True
            # Recovery evidence is intentionally process-local and must be
            # re-established after restart before an operator can acknowledge.
            self._healthy_samples = 0
            self._recovery_ready = False
        except Exception:
            self._state = StorageState.CRITICAL
            self._risk_blocked = True
            self._emergency_latched = True
            self._active_faults = {StorageFault.PROBE_FAILED}

    @staticmethod
    def _ttf_source_from_reason(reason: str) -> str | None:
        marker = ":ttf_"
        if marker not in reason:
            return None
        prefix = reason.split(marker, 1)[0]
        if prefix == "aggregate":
            return _AGGREGATE_TTF_SOURCE
        if prefix.startswith("component:") or prefix.startswith("volume:"):
            return prefix
        return None

    def rebase_rate_baselines(self) -> None:
        """Discard pre-ready rate baselines and seed them on the next sample.

        Callers can invoke this after startup migrations, cache creation, or a
        controlled maintenance burst so those bounded writes do not dominate
        the first production TTF window.  TTF incident causes and safety
        latches are deliberately preserved; any in-progress recovery proof is
        restarted from zero complete observation intervals.
        """

        with self._lock:
            self._volume_rates.clear()
            self._component_rates.clear()
            if self._ttf_recovery_sources:
                self._healthy_samples = 0
                self._recovery_ready = False

    @property
    def risk_increase_blocked(self) -> bool:
        with self._lock:
            return self._risk_blocked

    @property
    def emergency_latched(self) -> bool:
        with self._lock:
            return self._emergency_latched

    def snapshot(self) -> StorageHealthSnapshot | None:
        """Return the last immutable snapshot as one atomic object reference."""

        with self._lock:
            return self._snapshot

    def report_fault(self, fault: StorageFault) -> None:
        with self._lock:
            self._active_faults.add(fault)
            self._state = StorageState.CRITICAL
            self._risk_blocked = True
            self._emergency_latched = True
            self._healthy_samples = 0
            self._recovery_ready = False
            if self._snapshot is not None:
                self._generation += 1
                reasons = (
                    *self._snapshot.reasons,
                    f"active_fault:{fault.value}",
                    "risk_increase_blocked_latched",
                    "storage_emergency_latched",
                )
                self._snapshot = replace(
                    self._snapshot,
                    generation=self._generation,
                    observed_at=self._utcnow(),
                    state=StorageState.CRITICAL,
                    instantaneous_state=StorageState.CRITICAL,
                    reasons=tuple(dict.fromkeys(reasons)),
                    risk_increase_blocked=True,
                    emergency_latched=True,
                    healthy_recovery_samples=0,
                    recovery_ready_for_operator=False,
                    active_faults=tuple(sorted(self._active_faults, key=lambda item: item.value)),
                )
                if self._snapshot_store is not None:
                    try:
                        self._snapshot_store.write(self._snapshot)
                    except DurabilityError as exc:
                        if isinstance(exc.cause, OSError) and exc.cause.errno == errno.ENOSPC:
                            persistence_fault = StorageFault.ENOSPC
                        elif exc.stage in {
                            DurabilityStage.FSYNC_FILE,
                            DurabilityStage.FSYNC_DIRECTORY,
                        }:
                            persistence_fault = StorageFault.MANDATORY_FSYNC_FAILED
                        elif exc.stage is DurabilityStage.RENAME:
                            persistence_fault = StorageFault.ATOMIC_RENAME_FAILED
                        else:
                            persistence_fault = StorageFault.MANDATORY_WRITE_FAILED
                        self._active_faults.add(persistence_fault)
                        self._generation += 1
                        self._snapshot = replace(
                            self._snapshot,
                            generation=self._generation,
                            reasons=tuple(
                                dict.fromkeys(
                                    (
                                        *self._snapshot.reasons,
                                        "fault_snapshot_write_failed:"
                                        f"{exc.stage.value}:{exc.cause}",
                                    )
                                )
                            ),
                            active_faults=tuple(
                                sorted(
                                    self._active_faults,
                                    key=lambda item: item.value,
                                )
                            ),
                        )

    def report_os_error(self, error: Exception, *, stage: DurabilityStage | None = None) -> StorageFault:
        error_number = error.errno if isinstance(error, OSError) else None
        if error_number == errno.ENOSPC:
            fault = StorageFault.ENOSPC
        elif stage is DurabilityStage.FSYNC_FILE or stage is DurabilityStage.FSYNC_DIRECTORY:
            fault = StorageFault.MANDATORY_FSYNC_FAILED
        elif stage is DurabilityStage.RENAME:
            fault = StorageFault.ATOMIC_RENAME_FAILED
        else:
            fault = StorageFault.MANDATORY_WRITE_FAILED
        self.report_fault(fault)
        return fault

    def resolve_fault(self, fault: StorageFault) -> bool:
        """Mark the underlying condition repaired; latches remain operator-sticky."""

        with self._lock:
            if fault not in self._active_faults:
                return False
            self._active_faults.remove(fault)
            return True

    def allows(self, action: StorageAction) -> bool:
        with self._lock:
            if action in self._SURVIVAL_ACTIONS:
                return True
            if action in {StorageAction.ENTRY, StorageAction.ROTATION}:
                return not self._risk_blocked and self._state.severity < StorageState.DEGRADED.severity
            if action is StorageAction.OPTIONAL_WRITE:
                # Warning is the point at which optional evidence must yield
                # to bounded retention.  A sticky risk latch must not restart
                # the same producer merely because instantaneous pressure fell.
                return self._state is StorageState.HEALTHY and not self._risk_blocked
            if action is StorageAction.RETENTION:
                # The caller is responsible for restricting this action to
                # bounded Tier-C deletion/checkpoint work.  It remains useful
                # through emergency pressure, but not after a critical fault
                # where even SQLite scratch allocation is no longer trusted.
                return (
                    self._state.severity <= StorageState.EMERGENCY.severity
                    and not self._active_faults
                )
            if action is StorageAction.BACKUP:
                return self._state is StorageState.HEALTHY and not self._risk_blocked
            if action is StorageAction.CLEANUP:
                reserve_released = self._reserve is None or not self._reserve.status().present
                return self._emergency_latched and self._risk_blocked and reserve_released
            return False

    def create_reserve(self) -> bool:
        if self._reserve is None:
            raise ReserveError("no emergency reserve configured")
        with self._lock:
            if self._emergency_latched:
                raise ReserveError("cannot replenish the reserve while storage emergency is latched")
            return self._reserve.create()

    def release_reserve(self) -> bool:
        if self._reserve is None:
            raise ReserveError("no emergency reserve configured")
        with self._lock:
            if not self._emergency_latched or not self._risk_blocked:
                raise ReserveError("reserve release requires the emergency and risk-block latches first")
            changed = self._reserve.release()
            if self._snapshot is not None:
                self._generation += 1
                self._snapshot = replace(
                    self._snapshot,
                    generation=self._generation,
                    reserve=self._reserve.status(),
                )
            return changed

    def acknowledge_recovery(self, *, operator_acknowledged: bool = False) -> bool:
        """Explicitly re-enable risk after proven recovery and operator review."""

        if not operator_acknowledged:
            raise PermissionError("storage recovery requires explicit operator acknowledgement")
        with self._lock:
            if (
                self._state is not StorageState.HEALTHY
                or not self._recovery_ready
                or self._active_faults
                or not self._integrity_ok
                or not self._exchange_reconciled
            ):
                return False
            if self._reserve is not None:
                try:
                    self._reserve.create()
                except Exception:
                    return False
            if self._snapshot is not None:
                next_generation = self._generation + 1
                latch_reasons = {"risk_increase_blocked_latched", "storage_emergency_latched"}
                recovered_snapshot = replace(
                    self._snapshot,
                    generation=next_generation,
                    observed_at=self._utcnow(),
                    reasons=tuple(
                        reason
                        for reason in self._snapshot.reasons
                        if reason not in latch_reasons
                        and not reason.startswith("ttf_recovery_observation_")
                    ),
                    risk_increase_blocked=False,
                    emergency_latched=False,
                    recovery_ready_for_operator=False,
                    reserve=self._reserve.status() if self._reserve else None,
                    ttf_recovery_required=False,
                    ttf_recovery_sources=(),
                    ttf_recovery_observed_intervals=0,
                    ttf_recovery_intervals_required=0,
                    ttf_recovery_observation_ready=False,
                )
                if self._snapshot_store is not None:
                    try:
                        self._snapshot_store.write(recovered_snapshot)
                    except DurabilityError as exc:
                        self.report_os_error(exc.cause, stage=exc.stage)
                        return False
                self._generation = next_generation
                self._snapshot = recovered_snapshot
            self._risk_blocked = False
            self._emergency_latched = False
            self._recovery_ready = False
            self._ttf_recovery_sources.clear()
            return True

    def _update_rate(
        self,
        trackers: dict[str, _RateTracker],
        key: str,
        value: int,
        timestamp: float,
        *,
        consumption: bool,
        minimum_interval_seconds: float | None = None,
    ) -> tuple[float, bool]:
        previous = trackers.get(key)
        if previous is None:
            trackers[key] = _RateTracker(value=value, timestamp=timestamp)
            return 0.0, False
        elapsed = timestamp - previous.timestamp
        if elapsed <= 0:
            return previous.ema_bytes_per_second, previous.has_rate
        minimum_interval = (
            self.policy.minimum_rate_interval_seconds
            if minimum_interval_seconds is None
            else minimum_interval_seconds
        )
        if elapsed < minimum_interval:
            # Preserve the baseline until a representative interval exists.
            # Startup opens SQLite WALs and initializes caches in a short,
            # bounded burst; extrapolating that burst immediately creates a
            # false critical TTF projection.
            return previous.ema_bytes_per_second, previous.has_rate
        delta = previous.value - value if consumption else value - previous.value
        instantaneous = delta / elapsed
        ema = instantaneous if previous.positive_intervals == 0 else (
            self.policy.ema_alpha * instantaneous + (1.0 - self.policy.ema_alpha) * previous.ema_bytes_per_second
        )
        previous.value = value
        previous.timestamp = timestamp
        if instantaneous <= 0:
            previous.ema_bytes_per_second = 0.0
            previous.has_rate = False
            previous.positive_intervals = 0
            previous.non_growth_intervals += 1
            return 0.0, False
        previous.ema_bytes_per_second = ema
        previous.positive_intervals += 1
        previous.non_growth_intervals = 0
        previous.has_rate = (
            previous.positive_intervals >= self.policy.rate_intervals_required
        )
        return ema, previous.has_rate

    def _state_for_ttf(self, hours: float | None) -> StorageState:
        if hours is None:
            return StorageState.HEALTHY
        if hours <= self.policy.critical_ttf_hours:
            return StorageState.CRITICAL
        if hours <= self.policy.emergency_ttf_hours:
            return StorageState.EMERGENCY
        if hours <= self.policy.degraded_ttf_hours:
            return StorageState.DEGRADED
        if hours <= self.policy.warning_ttf_hours:
            return StorageState.WARNING
        return StorageState.HEALTHY

    def _ttf_sources_at_or_above(
        self,
        volumes: Sequence[VolumeHealth],
        components: Sequence[ComponentHealth],
        aggregate_ttf: float | None,
        minimum_state: StorageState,
    ) -> set[str]:
        sources = {
            f"volume:{volume.volume_id}"
            for volume in volumes
            if self._state_for_ttf(volume.time_to_full_hours).severity
            >= minimum_state.severity
        }
        sources.update(
            f"component:{component.name}"
            for component in components
            if self._state_for_ttf(component.time_to_full_hours).severity
            >= minimum_state.severity
        )
        if self._state_for_ttf(aggregate_ttf).severity >= minimum_state.severity:
            sources.add(_AGGREGATE_TTF_SOURCE)
        return sources

    def _tracker_for_ttf_source(self, source: str) -> _RateTracker | None:
        if source == _AGGREGATE_TTF_SOURCE:
            return self._component_rates.get("__whole_volume_budget__")
        if source.startswith("component:"):
            return self._component_rates.get(source.removeprefix("component:"))
        if source.startswith("volume:"):
            return self._volume_rates.get(source.removeprefix("volume:"))
        return None

    def _restart_ttf_recovery_observation(self, source: str) -> None:
        """Turn the incident sample into the baseline for fresh proof windows."""

        tracker = self._tracker_for_ttf_source(source)
        if tracker is None:
            return
        tracker.ema_bytes_per_second = 0.0
        tracker.has_rate = False
        tracker.positive_intervals = 0
        tracker.non_growth_intervals = 0

    def _ttf_recovery_status(
        self,
        volumes: Sequence[VolumeHealth],
        components: Sequence[ComponentHealth],
        aggregate_ttf: float | None,
    ) -> tuple[bool, int]:
        """Return whether every latched TTF source has fresh rate evidence."""

        if not self._ttf_recovery_sources:
            return True, 0
        required = max(1, self.policy.rate_intervals_required)
        ttf_by_source = {
            **{
                f"volume:{volume.volume_id}": volume.time_to_full_hours
                for volume in volumes
            },
            **{
                f"component:{component.name}": component.time_to_full_hours
                for component in components
            },
            _AGGREGATE_TTF_SOURCE: aggregate_ttf,
        }
        observed_by_source: list[int] = []
        ready = True
        for source in sorted(self._ttf_recovery_sources):
            tracker = self._tracker_for_ttf_source(source)
            if tracker is None:
                observed_by_source.append(0)
                ready = False
                continue
            observed = max(
                tracker.positive_intervals,
                tracker.non_growth_intervals,
            )
            observed_by_source.append(min(required, observed))
            source_ttf = ttf_by_source.get(source)
            source_ready = (
                tracker.non_growth_intervals >= required
                or (
                    tracker.has_rate
                    and source_ttf is not None
                    and source_ttf > self.policy.warning_ttf_hours
                )
            )
            ready = ready and source_ready
        return ready, min(observed_by_source, default=0)

    def _volume_health(self, usage: VolumeUsage, paths: Sequence[Path], timestamp: float) -> VolumeHealth:
        rate, has_rate = self._update_rate(
            self._volume_rates,
            usage.volume_id,
            usage.free_bytes,
            timestamp,
            consumption=True,
            minimum_interval_seconds=self.policy.volume_minimum_rate_interval_seconds,
        )
        ttf_hours = _ttf(usage.free_bytes, rate, has_rate)
        state = StorageState.HEALTHY
        reasons: list[str] = []
        thresholds = (
            (StorageState.CRITICAL, self.policy.critical_free_bytes, self.policy.critical_free_ratio),
            (StorageState.EMERGENCY, self.policy.emergency_free_bytes, self.policy.emergency_free_ratio),
            (StorageState.DEGRADED, self.policy.degraded_free_bytes, self.policy.degraded_free_ratio),
            (StorageState.WARNING, self.policy.warning_free_bytes, self.policy.warning_free_ratio),
        )
        for candidate, byte_threshold, ratio_threshold in thresholds:
            if usage.free_bytes < byte_threshold or usage.free_ratio < ratio_threshold:
                state = candidate
                reasons.append(
                    f"volume:{usage.volume_id}:free_below_{candidate.value}:"
                    f"{usage.free_bytes}:{usage.free_ratio:.6f}"
                )
                break
        ttf_state = self._state_for_ttf(ttf_hours)
        if ttf_state is not StorageState.HEALTHY:
            state = _worst_state(state, ttf_state)
            reasons.append(f"volume:{usage.volume_id}:ttf_{ttf_state.value}:{ttf_hours:.6f}h")
        return VolumeHealth(
            volume_id=usage.volume_id,
            mount_path=usage.mount_path,
            observed_paths=tuple(sorted((Path(path) for path in paths), key=str)),
            total_bytes=usage.total_bytes,
            free_bytes=usage.free_bytes,
            free_ratio=usage.free_ratio,
            consumption_bytes_per_hour=rate * 3600.0,
            time_to_full_hours=ttf_hours,
            state=state,
            reasons=tuple(reasons),
        )

    @staticmethod
    def _component_path_key(path: Path) -> str:
        return os.path.normcase(str(Path(path).resolve(strict=False)))

    def _component_health(
        self,
        budget: ComponentBudget,
        timestamp: float,
        path_sizes: Mapping[str, int],
    ) -> ComponentHealth:
        used = sum(
            path_sizes[path_key]
            for path_key in dict.fromkeys(
                self._component_path_key(path) for path in budget.paths
            )
        )
        if used < 0:
            raise ValueError(f"negative size for component {budget.name}")
        rate, has_rate = self._update_rate(
            self._component_rates,
            str(budget.name),
            used,
            timestamp,
            consumption=False,
        )
        remaining = max(0, budget.hard_limit_bytes - used)
        ttf_hours = _ttf(remaining, rate, has_rate)
        utilization = used / budget.hard_limit_bytes
        state = StorageState.HEALTHY
        reasons: list[str] = []
        if utilization >= 1.0:
            state = budget.breach_state
            reasons.append(f"component:{budget.name}:budget_breached:{used}:{budget.hard_limit_bytes}")
        elif utilization >= budget.warning_ratio:
            state = StorageState.WARNING
            reasons.append(f"component:{budget.name}:warning_utilization:{utilization:.6f}")
        ttf_state = self._state_for_ttf(ttf_hours)
        if utilization < 1.0 and ttf_state is not StorageState.HEALTHY:
            state = _worst_state(state, ttf_state)
            reasons.append(f"component:{budget.name}:ttf_{ttf_state.value}:{ttf_hours:.6f}h")
        return ComponentHealth(
            name=str(budget.name),
            path=budget.path,
            paths=budget.paths,
            used_bytes=used,
            budget_bytes=budget.hard_limit_bytes,
            utilization=utilization,
            growth_bytes_per_hour=rate * 3600.0,
            time_to_full_hours=ttf_hours,
            state=state,
            reasons=tuple(reasons),
        )

    def _is_recovery_healthy(
        self,
        volumes: Sequence[VolumeHealth],
        components: Sequence[ComponentHealth],
        worst_ttf: float | None,
        aggregate_state: StorageState,
        ttf_recovery_observation_ready: bool,
    ) -> bool:
        return (
            bool(volumes)
            and aggregate_state is StorageState.HEALTHY
            and all(
                volume.free_bytes >= self.policy.warning_free_bytes + self.policy.recovery_hysteresis_bytes
                and volume.free_ratio >= self.policy.warning_free_ratio
                for volume in volumes
            )
            and all(
                component.utilization
                < next(
                    budget.warning_ratio
                    for budget in self.policy.components
                    if str(budget.name) == component.name
                )
                for component in components
            )
            and (worst_ttf is None or worst_ttf > self.policy.warning_ttf_hours)
            and self._integrity_ok
            and self._exchange_reconciled
            and not self._active_faults
            and ttf_recovery_observation_ready
        )

    @staticmethod
    def _probe_directory(path: Path) -> Path:
        existing = _nearest_existing_path(path)
        return existing if existing.is_dir() else existing.parent

    def sample(
        self,
        *,
        integrity_ok: bool | None = None,
        exchange_reconciled: bool | None = None,
        run_durability_probes: bool = False,
    ) -> StorageHealthSnapshot:
        """Observe every path, apply worst-filesystem policy, and publish atomically."""

        with self._lock:
            if integrity_ok is not None:
                self._integrity_ok = bool(integrity_ok)
            if exchange_reconciled is not None:
                self._exchange_reconciled = bool(exchange_reconciled)
            timestamp = self._monotonic()
            observed_at = self._utcnow()
            if observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=timezone.utc)

            reasons: list[str] = []
            volume_groups: dict[str, tuple[VolumeUsage, list[Path]]] = {}
            probe_failed = False
            for path in self.policy.all_paths:
                try:
                    usage = self._disk_probe.inspect(path)
                except Exception as exc:
                    probe_failed = True
                    reasons.append(f"volume_probe_failed:{path}:{type(exc).__name__}:{exc}")
                    continue
                grouped = volume_groups.get(usage.volume_id)
                if grouped is None:
                    volume_groups[usage.volume_id] = (usage, [path])
                else:
                    previous, paths = grouped
                    # A real filesystem should report identical values.  On a
                    # racing sample, choosing the least free observation fails
                    # conservatively.
                    conservative = usage if usage.free_bytes < previous.free_bytes else previous
                    paths.append(path)
                    volume_groups[usage.volume_id] = (conservative, paths)

            volumes = tuple(
                self._volume_health(usage, paths, timestamp)
                for _, (usage, paths) in sorted(volume_groups.items(), key=lambda item: item[0])
            )
            components_list: list[ComponentHealth] = []
            component_path_sizes: dict[str, int] = {}
            for budget in self.policy.components:
                try:
                    for path in budget.paths:
                        path_key = self._component_path_key(path)
                        if path_key in component_path_sizes:
                            continue
                        size = int(self._size_probe.size_bytes(path))
                        if size < 0:
                            raise ValueError(f"negative size for component path {path}")
                        component_path_sizes[path_key] = size
                    components_list.append(
                        self._component_health(
                            budget,
                            timestamp,
                            component_path_sizes,
                        )
                    )
                except Exception as exc:
                    probe_failed = True
                    reasons.append(f"component_probe_failed:{budget.name}:{type(exc).__name__}:{exc}")
            components = tuple(components_list)

            # Enforce the 60 GB deployment model independently of the host
            # filesystem size.  Base-runtime, unmanaged contingency, and the
            # physical emergency reserve consume the same whole-volume budget
            # as all measured application components; the warning free-space
            # threshold is the reserved normal headroom.
            budgeted_capacity = max(
                1,
                self.policy.volume_budget_bytes - self.policy.warning_free_bytes,
            )
            budgeted_consumption = (
                self.policy.base_runtime_reservation_bytes
                + self.policy.unmanaged_contingency_bytes
                + self.policy.reserve_bytes
                # Component caps may intentionally overlap.  For example,
                # research WAL/SHM files count toward both the research cap and
                # the shared SQLite-scratch cap, but they consume physical
                # whole-volume bytes only once.
                + sum(component_path_sizes.values())
            )
            budgeted_utilization = budgeted_consumption / budgeted_capacity
            aggregate_remaining = max(0, budgeted_capacity - budgeted_consumption)
            aggregate_rate, aggregate_has_rate = self._update_rate(
                self._component_rates,
                "__whole_volume_budget__",
                budgeted_consumption,
                timestamp,
                consumption=False,
            )
            aggregate_ttf = _ttf(
                aggregate_remaining,
                aggregate_rate,
                aggregate_has_rate,
            )
            aggregate_state = StorageState.HEALTHY
            aggregate_reasons: list[str] = []
            if budgeted_utilization >= 1.0:
                aggregate_state = StorageState.DEGRADED
                aggregate_reasons.append(
                    "aggregate:volume_budget_breached:"
                    f"{budgeted_consumption}:{budgeted_capacity}:"
                    f"{self.policy.volume_budget_bytes}"
                )
            elif budgeted_utilization >= 0.80:
                aggregate_state = StorageState.WARNING
                aggregate_reasons.append(
                    "aggregate:warning_utilization:"
                    f"{budgeted_utilization:.6f}:{budgeted_consumption}:"
                    f"{budgeted_capacity}"
                )
            aggregate_ttf_state = self._state_for_ttf(aggregate_ttf)
            if aggregate_ttf_state is not StorageState.HEALTHY:
                aggregate_state = _worst_state(
                    aggregate_state,
                    aggregate_ttf_state,
                )
                aggregate_reasons.append(
                    "aggregate:ttf_"
                    f"{aggregate_ttf_state.value}:{aggregate_ttf:.6f}h"
                )
            reasons.extend(aggregate_reasons)

            durability_results: list[DurabilityProbeResult] = []
            if run_durability_probes:
                directories: dict[str, Path] = {}
                for volume_id, (_, paths) in volume_groups.items():
                    try:
                        directories[volume_id] = self._probe_directory(paths[0])
                    except Exception as exc:
                        probe_failed = True
                        reasons.append(
                            f"durability_probe_directory_failed:{paths[0]}:{type(exc).__name__}:{exc}"
                        )
                for _, directory in sorted(directories.items()):
                    try:
                        result = self._durability_probe.inspect(directory)
                    except Exception as exc:
                        result = DurabilityProbeResult(
                            directory=directory,
                            ok=False,
                            latency_ms=0.0,
                            error=f"{type(exc).__name__}: {exc}",
                            error_number=exc.errno if isinstance(exc, OSError) else None,
                        )
                    durability_results.append(result)
                    if not result.ok:
                        if result.error_number == errno.ENOSPC:
                            self._active_faults.add(StorageFault.ENOSPC)
                        elif result.failed_stage in {DurabilityStage.FSYNC_FILE, DurabilityStage.FSYNC_DIRECTORY}:
                            self._active_faults.add(StorageFault.MANDATORY_FSYNC_FAILED)
                        elif result.failed_stage is DurabilityStage.RENAME:
                            self._active_faults.add(StorageFault.ATOMIC_RENAME_FAILED)
                        else:
                            self._active_faults.add(StorageFault.MANDATORY_WRITE_FAILED)
                        reasons.append(
                            f"durability_probe_failed:{directory}:"
                            f"{result.failed_stage.value if result.failed_stage else 'unknown'}:{result.error}"
                        )

            if probe_failed:
                self._active_faults.add(StorageFault.PROBE_FAILED)

            observed_states = [
                *(volume.state for volume in volumes),
                *(component.state for component in components),
                aggregate_state,
            ]
            instantaneous = _worst_state(*observed_states)
            if probe_failed or self._active_faults:
                instantaneous = StorageState.CRITICAL
            for volume in volumes:
                reasons.extend(volume.reasons)
            for component in components:
                reasons.extend(component.reasons)
            for fault in sorted(self._active_faults, key=lambda item: item.value):
                reasons.append(f"active_fault:{fault.value}")

            ttf_values = [
                value
                for value in (
                    *(volume.time_to_full_hours for volume in volumes),
                    *(component.time_to_full_hours for component in components),
                    aggregate_ttf,
                )
                if value is not None
            ]
            worst_ttf = min(ttf_values) if ttf_values else None

            # Capture the specific rate projections which caused a risk-level
            # incident.  The cause set remains sticky until operator
            # acknowledgement, so write suppression (which naturally changes
            # the next projection to ``None``) cannot itself prove recovery.
            ttf_incident_minimum = (
                StorageState.WARNING
                if instantaneous is StorageState.WARNING
                else StorageState.DEGRADED
            )
            ttf_incident_sources = (
                self._ttf_sources_at_or_above(
                    volumes,
                    components,
                    aggregate_ttf,
                    ttf_incident_minimum,
                )
                if instantaneous.severity >= StorageState.WARNING.severity
                else set()
            )
            if ttf_incident_sources:
                new_ttf_incident_sources = (
                    ttf_incident_sources - self._ttf_recovery_sources
                )
                self._ttf_recovery_sources.update(ttf_incident_sources)
                for source in new_ttf_incident_sources:
                    self._restart_ttf_recovery_observation(source)
            (
                ttf_recovery_observation_ready,
                ttf_recovery_observed_intervals,
            ) = self._ttf_recovery_status(volumes, components, aggregate_ttf)

            if instantaneous.severity > self._state.severity:
                self._state = instantaneous
                self._healthy_samples = 0
                self._recovery_ready = False
            elif instantaneous is self._state:
                if instantaneous is not StorageState.HEALTHY:
                    self._healthy_samples = 0
                    self._recovery_ready = False
            elif instantaneous is StorageState.HEALTHY and self._is_recovery_healthy(
                volumes,
                components,
                worst_ttf,
                aggregate_state,
                ttf_recovery_observation_ready,
            ):
                self._healthy_samples += 1
                if self._healthy_samples >= self.policy.recovery_samples:
                    self._healthy_samples = self.policy.recovery_samples
                    self._state = StorageState.HEALTHY
                    self._recovery_ready = self._risk_blocked or self._emergency_latched
            else:
                self._healthy_samples = 0

            if self._state.severity >= StorageState.DEGRADED.severity:
                self._risk_blocked = True
            if self._state.severity >= StorageState.EMERGENCY.severity:
                self._emergency_latched = True
            if (
                self._state is StorageState.HEALTHY
                and not self._risk_blocked
                and not self._emergency_latched
                and ttf_recovery_observation_ready
            ):
                # Warning-only TTF incidents do not require operator
                # acknowledgement.  Clear their completed proof as soon as
                # ordinary state hysteresis has also recovered.
                self._ttf_recovery_sources.clear()
            if self._risk_blocked:
                reasons.append("risk_increase_blocked_latched")
            if self._emergency_latched:
                reasons.append("storage_emergency_latched")
            if self._ttf_recovery_sources:
                proof_state = (
                    "complete"
                    if ttf_recovery_observation_ready
                    else "pending"
                )
                reasons.append(
                    f"ttf_recovery_observation_{proof_state}:"
                    f"{ttf_recovery_observed_intervals}:"
                    f"{self.policy.rate_intervals_required}"
                )
            if self._state is not instantaneous and self._state is not StorageState.HEALTHY:
                reasons.append(
                    f"recovery_hysteresis:{self._healthy_samples}:{self.policy.recovery_samples}:"
                    f"{self.policy.recovery_hysteresis_bytes}"
                )

            self._generation += 1
            snapshot = StorageHealthSnapshot(
                generation=self._generation,
                observed_at=observed_at,
                state=self._state,
                instantaneous_state=instantaneous,
                reasons=tuple(dict.fromkeys(reasons)),
                volumes=volumes,
                components=components,
                durability_probes=tuple(durability_results),
                volume_budget_bytes=self.policy.volume_budget_bytes,
                budgeted_consumption_bytes=budgeted_consumption,
                budgeted_free_headroom_bytes=self.policy.warning_free_bytes,
                budgeted_utilization=budgeted_utilization,
                worst_time_to_full_hours=worst_ttf,
                risk_increase_blocked=self._risk_blocked,
                emergency_latched=self._emergency_latched,
                healthy_recovery_samples=self._healthy_samples,
                recovery_samples_required=self.policy.recovery_samples,
                recovery_ready_for_operator=self._recovery_ready,
                integrity_ok=self._integrity_ok,
                exchange_reconciled=self._exchange_reconciled,
                active_faults=tuple(sorted(self._active_faults, key=lambda item: item.value)),
                reserve=self._reserve.status() if self._reserve else None,
                ttf_recovery_required=bool(self._ttf_recovery_sources),
                ttf_recovery_sources=tuple(sorted(self._ttf_recovery_sources)),
                ttf_recovery_observed_intervals=(
                    ttf_recovery_observed_intervals
                    if self._ttf_recovery_sources
                    else 0
                ),
                ttf_recovery_intervals_required=(
                    self.policy.rate_intervals_required
                    if self._ttf_recovery_sources
                    else 0
                ),
                ttf_recovery_observation_ready=(
                    bool(self._ttf_recovery_sources)
                    and ttf_recovery_observation_ready
                ),
            )
            self._snapshot = snapshot

            if self._snapshot_store is not None:
                try:
                    self._snapshot_store.write(snapshot)
                except DurabilityError as exc:
                    self.report_os_error(exc.cause, stage=exc.stage)
                    self._generation += 1
                    snapshot = replace(
                        snapshot,
                        generation=self._generation,
                        state=StorageState.CRITICAL,
                        instantaneous_state=StorageState.CRITICAL,
                        reasons=tuple(
                            dict.fromkeys(
                                (*snapshot.reasons, f"health_snapshot_write_failed:{exc.stage.value}:{exc.cause}")
                            )
                        ),
                        risk_increase_blocked=True,
                        emergency_latched=True,
                        healthy_recovery_samples=0,
                        recovery_ready_for_operator=False,
                        active_faults=tuple(sorted(self._active_faults, key=lambda item: item.value)),
                    )
                    self._snapshot = snapshot
            return snapshot


OWNERSHIP_MARKER = ".bongus-owned-cleanup-root"
OWNERSHIP_MARKER_CONTENT = "bongus-owned-cleanup-root-v1\n"


@dataclass(frozen=True, slots=True)
class CleanupRoot:
    path: Path
    require_ownership_marker: bool = True
    marker_name: str = OWNERSHIP_MARKER

    def __post_init__(self) -> None:
        if not self.marker_name or Path(self.marker_name).name != self.marker_name:
            raise ValueError("cleanup marker_name must be one plain filename")
        object.__setattr__(self, "path", Path(self.path).absolute())


@dataclass(frozen=True, slots=True)
class CleanupPolicy:
    allowlisted_roots: tuple[CleanupRoot, ...]
    protected_paths: tuple[Path, ...]

    def __post_init__(self) -> None:
        roots = tuple(self.allowlisted_roots)
        if not roots:
            raise ValueError("at least one cleanup root is required")
        object.__setattr__(self, "allowlisted_roots", roots)
        object.__setattr__(self, "protected_paths", tuple(Path(path).absolute() for path in self.protected_paths))


class UnsafeCleanupPath(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CleanupResult:
    target: Path
    deleted: bool
    files_deleted: int
    directories_deleted: int
    bytes_reclaimed: int


def _paths_intersect(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


class SafeCleanup:
    """Containment-checked deletion which never follows filesystem links."""

    _WINDOWS_RENAME_ATTEMPTS = 8
    _WINDOWS_RENAME_MAX_DELAY_SECONDS = 0.25

    def __init__(self, policy: CleanupPolicy) -> None:
        self.policy = policy
        self._lock = threading.RLock()

    @staticmethod
    def initialize_owned_root(root: CleanupRoot) -> Path:
        """Create and durably mark a cleanup root before it is used."""

        root.path.mkdir(parents=True, exist_ok=True)
        root_stat = root.path.lstat()
        if _is_link_or_reparse(root.path, root_stat) or not stat.S_ISDIR(root_stat.st_mode):
            raise UnsafeCleanupPath(f"cleanup root is not a regular directory: {root.path}")
        marker = root.path / root.marker_name
        if marker.exists() or marker.is_symlink():
            marker_stat = marker.lstat()
            if _is_link_or_reparse(marker, marker_stat) or not stat.S_ISREG(marker_stat.st_mode):
                raise UnsafeCleanupPath(f"invalid cleanup ownership marker: {marker}")
            if marker.read_text(encoding="utf-8") != OWNERSHIP_MARKER_CONTENT:
                raise UnsafeCleanupPath(f"cleanup ownership marker has invalid content: {marker}")
            return marker
        with marker.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(OWNERSHIP_MARKER_CONTENT)
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(root.path)
        return marker

    @staticmethod
    def _absolute_without_link_resolution(path: Path) -> Path:
        return Path(os.path.abspath(os.fspath(path)))

    def _matching_root(self, target: Path) -> CleanupRoot:
        matches: list[CleanupRoot] = []
        for root in self.policy.allowlisted_roots:
            root_path = self._absolute_without_link_resolution(root.path)
            try:
                target.relative_to(root_path)
            except ValueError:
                continue
            matches.append(root)
        if not matches:
            raise UnsafeCleanupPath(f"target is outside cleanup allowlist: {target}")
        return max(matches, key=lambda item: len(self._absolute_without_link_resolution(item.path).parts))

    def _validate_root_and_marker(self, root: CleanupRoot) -> Path:
        root_path = self._absolute_without_link_resolution(root.path)
        try:
            root_stat = root_path.lstat()
        except FileNotFoundError as exc:
            raise UnsafeCleanupPath(f"cleanup root does not exist: {root_path}") from exc
        if _is_link_or_reparse(root_path, root_stat) or not stat.S_ISDIR(root_stat.st_mode):
            raise UnsafeCleanupPath(f"cleanup root is a symlink, reparse point, or non-directory: {root_path}")
        resolved_root = root_path.resolve(strict=True)
        if os.path.normcase(str(resolved_root)) != os.path.normcase(str(root_path)):
            raise UnsafeCleanupPath(f"cleanup root contains a symlink or reparse point: {root_path}")
        if root.require_ownership_marker:
            marker = root_path / root.marker_name
            try:
                marker_stat = marker.lstat()
            except FileNotFoundError as exc:
                raise UnsafeCleanupPath(f"cleanup ownership marker missing: {marker}") from exc
            if _is_link_or_reparse(marker, marker_stat) or not stat.S_ISREG(marker_stat.st_mode):
                raise UnsafeCleanupPath(f"cleanup ownership marker is unsafe: {marker}")
            if marker.read_text(encoding="utf-8") != OWNERSHIP_MARKER_CONTENT:
                raise UnsafeCleanupPath(f"cleanup ownership marker has invalid content: {marker}")
        return root_path

    def _validate_path_chain(self, root: Path, target: Path) -> None:
        relative = target.relative_to(root)
        current = root
        for part in relative.parts:
            current = current / part
            try:
                current_stat = current.lstat()
            except FileNotFoundError:
                return
            if _is_link_or_reparse(current, current_stat):
                raise UnsafeCleanupPath(f"cleanup path contains a symlink or reparse point: {current}")

    def validate(self, target: str | os.PathLike[str]) -> tuple[Path, CleanupRoot]:
        target_path = self._absolute_without_link_resolution(Path(target))
        root = self._matching_root(target_path)
        root_path = self._validate_root_and_marker(root)
        if target_path == root_path:
            raise UnsafeCleanupPath("cleanup root itself cannot be deleted")
        self._validate_path_chain(root_path, target_path)
        for protected in self.policy.protected_paths:
            protected_path = self._absolute_without_link_resolution(protected)
            if _paths_intersect(target_path, protected_path):
                raise UnsafeCleanupPath(f"cleanup target intersects protected path: {protected_path}")
        return target_path, root

    def _deletion_plan(self, target: Path) -> tuple[list[tuple[Path, int]], list[Path]]:
        """Validate an entire tree before returning files and post-order dirs."""

        target_stat = target.lstat()
        if _is_link_or_reparse(target, target_stat):
            raise UnsafeCleanupPath(f"refusing linked cleanup target: {target}")
        if stat.S_ISREG(target_stat.st_mode):
            return [(target, int(target_stat.st_size))], []
        if not stat.S_ISDIR(target_stat.st_mode):
            raise UnsafeCleanupPath(f"unsupported cleanup target type: {target}")

        files: list[tuple[Path, int]] = []
        directories: list[Path] = []
        with os.scandir(target) as entries:
            children = [Path(entry.path) for entry in entries]
        for child in children:
            child_stat = child.lstat()
            if _is_link_or_reparse(child, child_stat):
                raise UnsafeCleanupPath(f"cleanup tree contains a symlink or reparse point: {child}")
            child_files, child_directories = self._deletion_plan(child)
            files.extend(child_files)
            directories.extend(child_directories)
        directories.append(target)
        return files, directories

    @classmethod
    def _atomic_replace_with_windows_retry(
        cls,
        source: Path,
        destination: Path,
        *,
        expected_stat: os.stat_result,
    ) -> None:
        """Retry a transient Windows directory-sharing failure without weakening containment.

        Antivirus and indexer handles can briefly make an otherwise valid directory
        rename fail with ``ERROR_ACCESS_DENIED``.  Every retry proves that the source
        still has the identity and type validated by the caller and that the unique
        destination has not appeared.  Other platforms and non-permission failures
        retain fail-fast behavior.
        """

        expected_identity = (expected_stat.st_dev, expected_stat.st_ino)
        expected_type = stat.S_IFMT(expected_stat.st_mode)
        attempts = cls._WINDOWS_RENAME_ATTEMPTS if os.name == "nt" else 1
        for attempt in range(attempts):
            try:
                os.replace(source, destination)
                return
            except PermissionError:
                if attempt + 1 >= attempts:
                    raise
                try:
                    current_stat = source.lstat()
                except FileNotFoundError:
                    try:
                        destination_stat = destination.lstat()
                    except FileNotFoundError as exc:
                        raise UnsafeCleanupPath(
                            "cleanup target disappeared during atomic quarantine"
                        ) from exc
                    if (
                        _is_link_or_reparse(destination, destination_stat)
                        or (destination_stat.st_dev, destination_stat.st_ino)
                        != expected_identity
                        or stat.S_IFMT(destination_stat.st_mode) != expected_type
                    ):
                        raise UnsafeCleanupPath(
                            "cleanup target identity changed during atomic quarantine"
                        )
                    return
                if (
                    _is_link_or_reparse(source, current_stat)
                    or (current_stat.st_dev, current_stat.st_ino) != expected_identity
                    or stat.S_IFMT(current_stat.st_mode) != expected_type
                ):
                    raise UnsafeCleanupPath(
                        "cleanup target identity changed before atomic quarantine"
                    )
                if destination.exists() or destination.is_symlink():
                    raise UnsafeCleanupPath(
                        "cleanup quarantine destination appeared before atomic rename"
                    )
                delay = min(
                    0.01 * (2**attempt),
                    cls._WINDOWS_RENAME_MAX_DELAY_SECONDS,
                )
                time.sleep(delay)

    def remove(self, target: str | os.PathLike[str]) -> CleanupResult:
        with self._lock:
            target_path, root = self.validate(target)
            if not target_path.exists() and not target_path.is_symlink():
                return CleanupResult(target_path, False, 0, 0, 0)
            root_path = self._absolute_without_link_resolution(root.path)
            initial_stat = target_path.lstat()
            # Reject a linked or otherwise unsafe descendant before mutating the
            # allowlisted tree.  The quarantined tree is scanned again below so a
            # race between this preflight and the atomic rename still fails closed.
            self._deletion_plan(target_path)
            quarantine = root_path / f".bongus-cleanup-{uuid.uuid4().hex}"
            self._atomic_replace_with_windows_retry(
                target_path,
                quarantine,
                expected_stat=initial_stat,
            )
            _fsync_directory(root_path)
            try:
                quarantined_stat = quarantine.lstat()
                initial_identity = (initial_stat.st_dev, initial_stat.st_ino)
                quarantined_identity = (quarantined_stat.st_dev, quarantined_stat.st_ino)
                if initial_identity != quarantined_identity:
                    raise UnsafeCleanupPath("cleanup target identity changed before quarantine")
                files, directories = self._deletion_plan(quarantine)
                reclaimed = sum(size for _, size in files)
                for path, _ in files:
                    path_stat = path.lstat()
                    if _is_link_or_reparse(path, path_stat) or not stat.S_ISREG(path_stat.st_mode):
                        raise UnsafeCleanupPath(f"cleanup file changed type after preflight: {path}")
                    path.unlink()
                for path in directories:
                    path_stat = path.lstat()
                    if _is_link_or_reparse(path, path_stat) or not stat.S_ISDIR(path_stat.st_mode):
                        raise UnsafeCleanupPath(f"cleanup directory changed type after preflight: {path}")
                    path.rmdir()
            except Exception:
                if (quarantine.exists() or quarantine.is_symlink()) and not (
                    target_path.exists() or target_path.is_symlink()
                ):
                    self._atomic_replace_with_windows_retry(
                        quarantine,
                        target_path,
                        expected_stat=quarantine.lstat(),
                    )
                    _fsync_directory(root_path)
                raise
            _fsync_directory(root_path)
            return CleanupResult(target_path, True, len(files), len(directories), reclaimed)


__all__ = [
    "AtomicHealthSnapshotStore",
    "BINARY_MB",
    "CleanupPolicy",
    "CleanupResult",
    "CleanupRoot",
    "ComponentBudget",
    "ComponentHealth",
    "ComponentSizeProbe",
    "DECIMAL_GB",
    "DECIMAL_MB",
    "DEFAULT_BASE_RUNTIME_RESERVATION_BYTES",
    "DEFAULT_COMPONENT_LIMITS",
    "DEFAULT_NORMAL_FREE_HEADROOM_BYTES",
    "DEFAULT_RECOVERY_HYSTERESIS_BYTES",
    "DEFAULT_RESERVE_BYTES",
    "DEFAULT_UNMANAGED_CONTINGENCY_BYTES",
    "DEFAULT_VOLUME_BUDGET_BYTES",
    "DiskProbe",
    "DurabilityError",
    "DurabilityProbe",
    "DurabilityProbeResult",
    "DurabilityStage",
    "DurableFileOperations",
    "EmergencyReserve",
    "OSDurableFileOperations",
    "OWNERSHIP_MARKER",
    "OWNERSHIP_MARKER_CONTENT",
    "RecursivePathSizeProbe",
    "ReserveError",
    "ReserveStatus",
    "SafeCleanup",
    "StorageAction",
    "StorageComponent",
    "StorageFault",
    "StorageGuard",
    "StorageHealthSnapshot",
    "StoragePolicy",
    "StorageState",
    "SystemDiskProbe",
    "UnsafeCleanupPath",
    "VolumeHealth",
    "VolumeUsage",
    "WriteFsyncRenameProbe",
    "volume_root_for_path",
]
