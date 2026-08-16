"""Coherent publication and verification for split-store SQLite backup sets."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from bongus.engine.database_backup import (
    DEFAULT_BACKUP_BUDGET_BYTES,
    DEFAULT_PEAK_HEADROOM_BYTES,
    MANIFEST_FORMAT,
    BackupError,
    BackupManifest,
    BackupResult,
    create_verified_backup,
    restore_verified_backup,
    verify_backup,
)
from bongus.engine.rust_recovery import (
    MAX_RUST_RECOVERY_GENERATION_BYTES,
    RUST_RECOVERY_DIRECTORY_NAME,
    CommandRunner,
    VerifiedRustRecoveryGeneration,
    copy_rust_recovery_generation,
    request_rust_recovery_generation,
    run_rust_recovery_offline_verifier,
    verify_rust_recovery_generation,
)

BACKUP_SET_FORMAT = "bongus-split-store-backup-set-v1"
REQUIRED_DATABASES = ("state.db", "audit.db", "research.db")
DEFAULT_SET_BUDGET_BYTES = 8_000_000_000
DEFAULT_BACKUP_TREE_BUDGET_BYTES = 20_500_000_000
DEFAULT_MAX_SOURCE_SKEW_SECONDS = 900.0
_REPARSE_POINT_ATTRIBUTE = 0x0400
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SET_ID = re.compile(r"^[0-9]{8}T[0-9]{6}\.[0-9]{6}Z-[0-9a-f]{32}$")
_GENERATION_MARKER = ".backup-set-generation-v1"
_STAGING_MEMBER = re.compile(
    r"^(?:\.backup-set-generation-v1|"
    r"\.(?:state|audit|research)\..+\.db\.tmp|"
    r"(?:state|audit|research)\..+\.db(?:\.manifest\.json)?|"
    r"backup-set\..+\.json|live_config\.json|migration-manifest\.json)$"
)
_RUST_GENERATION_DIRECTORY = re.compile(r"^generation-[A-Za-z0-9_-]{1,128}$")
_ENTRY_KEYS = frozenset(
    {
        "backup_filename",
        "backup_sha256",
        "created_at",
        "manifest_filename",
        "manifest_sha256",
        "size_bytes",
    }
)
_SET_KEYS = frozenset(
    {
        "schema_version",
        "evidence_kind",
        "format",
        "set_id",
        "started_at",
        "completed_at",
        "complete",
        "required_source_names",
        "source_backups",
        "source_count",
        "source_skew_seconds",
        "total_size_bytes",
        "recovery_files",
        "recovery_file_names",
        "recovery_file_count",
        "rust_recovery_generation",
    }
)
_RECOVERY_ENTRY_KEYS = frozenset({"filename", "sha256", "size_bytes"})
_RUST_ENTRY_KEYS = frozenset(
    {
        "created_at_ms",
        "generation_id",
        "manifest_relative_path",
        "manifest_sha256",
        "manifest_size_bytes",
        "member_count",
        "restore_policy",
        "total_size_bytes",
    }
)
_REQUIRED_RECOVERY_FILES = ("live_config.json",)
_OPTIONAL_RECOVERY_FILES = ("migration-manifest.json",)
MAX_RECOVERY_CONFIGURATION_BYTES = 2_000_000


def _assert_backup_budget_covers_writer_contract() -> None:
    from bongus.engine.split_state_store import (
        SPLIT_ROLE_DATABASE_MAX_BYTES,
        SPLIT_ROLE_WAL_MAX_BYTES,
    )

    maximum_bound_set_bytes = (
        sum(SPLIT_ROLE_DATABASE_MAX_BYTES.values())
        + len(REQUIRED_DATABASES) * SPLIT_ROLE_WAL_MAX_BYTES
        + MAX_RUST_RECOVERY_GENERATION_BYTES
        + MAX_RECOVERY_CONFIGURATION_BYTES
    )
    if maximum_bound_set_bytes > DEFAULT_SET_BUDGET_BYTES:
        raise RuntimeError("split-store writer/WAL/recovery maxima exceed the complete-set backup budget")


_assert_backup_budget_covers_writer_contract()


@dataclass(frozen=True, slots=True)
class VerifiedBackupSet:
    manifest_path: Path
    set_id: str
    started_at: datetime
    completed_at: datetime
    source_skew_seconds: float
    total_size_bytes: int
    backups: Mapping[str, BackupResult]
    recovery_files: Mapping[str, Path]
    rust_recovery_generation: VerifiedRustRecoveryGeneration


def _is_link_or_reparse(path: Path, metadata: os.stat_result | None = None) -> bool:
    observed = metadata if metadata is not None else path.lstat()
    return path.is_symlink() or bool(getattr(observed, "st_file_attributes", 0) & _REPARSE_POINT_ATTRIBUTE)


def _regular_directory(path: Path, *, create: bool = False) -> Path:
    candidate = path.absolute()
    if create:
        candidate.mkdir(parents=True, exist_ok=True)
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise BackupError(f"backup-set directory is unavailable: {candidate}") from exc
    if _is_link_or_reparse(candidate, metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise BackupError("backup-set directory must be a regular non-link directory")
    return candidate.resolve(strict=True)


def _regular_file(path: Path, *, description: str) -> Path:
    candidate = path.absolute()
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise BackupError(f"{description} is unavailable: {candidate}") from exc
    if _is_link_or_reparse(candidate, metadata) or not stat.S_ISREG(metadata.st_mode):
        raise BackupError(f"{description} must be a regular non-link file")
    return candidate.resolve(strict=True)


def _direct_child(directory: Path, raw_name: object, *, description: str) -> Path:
    if not isinstance(raw_name, str) or not raw_name:
        raise BackupError(f"{description} filename is missing")
    name = Path(raw_name)
    if name.is_absolute() or name.name != raw_name or raw_name in {".", ".."}:
        raise BackupError(f"{description} filename is unsafe")
    path = _regular_file(directory / raw_name, description=description)
    if path.parent != directory:
        raise BackupError(f"{description} escapes the backup-set directory")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc(value: object, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise BackupError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise BackupError(f"{field} must carry a UTC offset")
    return parsed.astimezone(timezone.utc)


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_file(path: Path) -> None:
    """Flush a completed copy through a write-capable file descriptor."""
    descriptor = os.open(path, os.O_RDWR)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _owned_tree_size(directory: Path) -> int:
    """Measure an exact regular-file tree without following links."""

    total = 0
    pending = [directory]
    while pending:
        current = pending.pop()
        for member in current.iterdir():
            metadata = member.lstat()
            if _is_link_or_reparse(member, metadata):
                raise BackupError("backup tree contains a link or reparse point")
            if stat.S_ISDIR(metadata.st_mode):
                pending.append(member)
            elif stat.S_ISREG(metadata.st_mode):
                total += metadata.st_size
            else:
                raise BackupError("backup tree contains an unsupported member")
    return total


def _owned_tree_files(directory: Path) -> set[Path]:
    """Return every regular file in a link-free owned tree."""

    files: set[Path] = set()
    pending = [directory]
    while pending:
        current = pending.pop()
        for member in current.iterdir():
            metadata = member.lstat()
            if _is_link_or_reparse(member, metadata):
                raise BackupError("backup tree contains a link or reparse point")
            if stat.S_ISDIR(metadata.st_mode):
                pending.append(member)
            elif stat.S_ISREG(metadata.st_mode):
                files.add(member.resolve(strict=True))
            else:
                raise BackupError("backup tree contains an unsupported member")
    return files


def _rust_cleanup_tree_is_owned(container: Path) -> bool:
    """Recognize only the exact (possibly partial) Rust-generation shape."""

    try:
        metadata = container.lstat()
    except OSError:
        return False
    if (
        container.name != RUST_RECOVERY_DIRECTORY_NAME
        or _is_link_or_reparse(container, metadata)
        or not stat.S_ISDIR(metadata.st_mode)
    ):
        return False
    allowed_files = {
        "manifest.json",
        "members/execution_state.jsonl",
        "members/execution_intents.jsonl",
        "members/execution_telemetry.jsonl",
        "members/execution_telemetry.jsonl.cursor.a",
        "members/execution_telemetry.jsonl.cursor.b",
        "members/private_stream_cursors/spot.jsonl",
        "members/private_stream_cursors/futures.jsonl",
    }
    generations: set[str] = set()
    pending = [container]
    while pending:
        current = pending.pop()
        for candidate in current.iterdir():
            child_metadata = candidate.lstat()
            if _is_link_or_reparse(candidate, child_metadata):
                return False
            relative = candidate.relative_to(container)
            if not relative.parts:
                return False
            generation_name = relative.parts[0]
            if _RUST_GENERATION_DIRECTORY.fullmatch(generation_name) is None:
                return False
            generations.add(generation_name)
            if len(generations) > 1:
                return False
            if stat.S_ISDIR(child_metadata.st_mode):
                allowed_directories = {
                    generation_name,
                    f"{generation_name}/members",
                    f"{generation_name}/members/private_stream_cursors",
                }
                if relative.as_posix() not in allowed_directories:
                    return False
                pending.append(candidate)
            elif stat.S_ISREG(child_metadata.st_mode):
                inner = Path(*relative.parts[1:]).as_posix()
                if inner not in allowed_files:
                    return False
            else:
                return False
    return True


def _remove_owned_rust_cleanup_tree(container: Path) -> tuple[Path, ...]:
    if not _rust_cleanup_tree_is_owned(container):
        raise BackupError("Rust recovery cleanup tree is not safely owned")
    removed: list[Path] = []
    descendants = sorted(
        container.rglob("*"),
        key=lambda path: (len(path.parts), path.as_posix()),
        reverse=True,
    )
    for candidate in descendants:
        metadata = candidate.lstat()
        if _is_link_or_reparse(candidate, metadata):
            raise BackupError("Rust recovery cleanup tree changed during deletion")
        if stat.S_ISREG(metadata.st_mode):
            candidate.unlink()
        elif stat.S_ISDIR(metadata.st_mode):
            candidate.rmdir()
        else:
            raise BackupError("Rust recovery cleanup tree contains an unsupported member")
        removed.append(candidate)
    container.rmdir()
    removed.append(container)
    return tuple(removed)


def _estimated_split_store_image_bytes(root: Path) -> int:
    total = 0
    for source_name in REQUIRED_DATABASES:
        source = _regular_file(root / source_name, description=source_name)
        total += source.stat().st_size
        wal = Path(f"{source}-wal")
        if wal.exists():
            total += _regular_file(wal, description=f"{source_name} WAL").stat().st_size
    return total


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def verify_backup_set(
    manifest_path: str | os.PathLike[str],
    *,
    max_source_skew_seconds: float = DEFAULT_MAX_SOURCE_SKEW_SECONDS,
    deep: bool = True,
    _allow_staging: bool = False,
) -> VerifiedBackupSet:
    manifest = _regular_file(Path(manifest_path), description="backup-set manifest")
    directory = _regular_directory(manifest.parent)
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BackupError(f"invalid backup-set manifest JSON: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != _SET_KEYS:
        raise BackupError("backup-set manifest fields do not match the exact schema")
    if (
        payload.get("schema_version") != 1
        or payload.get("evidence_kind") != "verified_split_store_backup_set"
        or payload.get("format") != BACKUP_SET_FORMAT
        or payload.get("complete") is not True
    ):
        raise BackupError("backup-set manifest identity/completion is invalid")
    set_id = str(payload.get("set_id") or "")
    if _SET_ID.fullmatch(set_id) is None:
        raise BackupError("backup-set identifier is invalid")
    if manifest.name != f"backup-set.{set_id}.json":
        raise BackupError("backup-set filename does not match its identifier")
    if not _allow_staging and directory.name != f"backup-set.{set_id}":
        raise BackupError("backup-set generation directory does not match its identifier")
    if payload.get("required_source_names") != list(REQUIRED_DATABASES):
        raise BackupError("backup-set required sources do not match policy")
    raw_sources = payload.get("source_backups")
    if not isinstance(raw_sources, dict) or set(raw_sources) != set(REQUIRED_DATABASES):
        raise BackupError("backup set is incomplete or has unknown sources")
    if payload.get("source_count") != len(REQUIRED_DATABASES):
        raise BackupError("backup-set source count is invalid")

    started_at = _utc(payload.get("started_at"), field="started_at")
    completed_at = _utc(payload.get("completed_at"), field="completed_at")
    if completed_at < started_at:
        raise BackupError("backup-set completion precedes its start")
    backups: dict[str, BackupResult] = {}
    expected_files: set[Path] = {
        manifest,
        _regular_file(
            directory / _GENERATION_MARKER,
            description="backup-set generation marker",
        ),
    }
    source_times: list[datetime] = []
    total_size = 0
    for source_name in REQUIRED_DATABASES:
        raw_entry = raw_sources[source_name]
        if not isinstance(raw_entry, dict) or set(raw_entry) != _ENTRY_KEYS:
            raise BackupError(f"backup-set entry is malformed for {source_name}")
        source_manifest = _direct_child(
            directory,
            raw_entry.get("manifest_filename"),
            description=f"{source_name} backup manifest",
        )
        expected_manifest_hash = raw_entry.get("manifest_sha256")
        if (
            not isinstance(expected_manifest_hash, str)
            or _SHA256.fullmatch(expected_manifest_hash) is None
            or _sha256(source_manifest) != expected_manifest_hash
        ):
            raise BackupError(f"{source_name} manifest hash mismatch")
        if deep:
            result = verify_backup(source_manifest)
        else:
            try:
                source_payload = json.loads(source_manifest.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise BackupError(f"invalid source manifest for {source_name}: {exc}") from exc
            if not isinstance(source_payload, dict):
                raise BackupError(f"source manifest root is invalid for {source_name}")
            source_record = BackupManifest.from_dict(source_payload)
            if (
                source_record.format != MANIFEST_FORMAT
                or source_record.integrity_check.casefold() != "ok"
                or _SHA256.fullmatch(source_record.sha256) is None
            ):
                raise BackupError(f"source manifest evidence is invalid for {source_name}")
            source_backup = _direct_child(
                directory,
                source_record.backup_filename,
                description=f"{source_name} backup image",
            )
            if source_backup.stat().st_size != source_record.size_bytes:
                raise BackupError(f"source backup size mismatch for {source_name}")
            result = BackupResult(
                backup_path=source_backup,
                manifest_path=source_manifest,
                manifest=source_record,
            )
        if result.manifest.source_name != source_name:
            raise BackupError(f"backup source identity mismatch for {source_name}")
        if raw_entry.get("backup_filename") != result.backup_path.name:
            raise BackupError(f"backup filename mismatch for {source_name}")
        if raw_entry.get("backup_sha256") != result.manifest.sha256:
            raise BackupError(f"backup hash mismatch for {source_name}")
        if raw_entry.get("size_bytes") != result.manifest.size_bytes:
            raise BackupError(f"backup size mismatch for {source_name}")
        if raw_entry.get("created_at") != result.manifest.created_at:
            raise BackupError(f"backup timestamp mismatch for {source_name}")
        source_time = _utc(result.manifest.created_at, field=f"{source_name}.created_at")
        if source_time < started_at or source_time > completed_at:
            raise BackupError(f"backup timestamp is outside the set window for {source_name}")
        backups[source_name] = result
        expected_files.update((result.manifest_path, result.backup_path))
        source_times.append(source_time)
        total_size += result.manifest.size_bytes

    raw_recovery_files = payload.get("recovery_files")
    if not isinstance(raw_recovery_files, dict):
        raise BackupError("backup-set recovery files are malformed")
    recovery_names = tuple(sorted(raw_recovery_files))
    allowed_recovery_names = set(_REQUIRED_RECOVERY_FILES + _OPTIONAL_RECOVERY_FILES)
    if (
        not set(_REQUIRED_RECOVERY_FILES) <= set(recovery_names)
        or not set(recovery_names) <= allowed_recovery_names
        or payload.get("recovery_file_names") != list(recovery_names)
        or payload.get("recovery_file_count") != len(recovery_names)
    ):
        raise BackupError("backup-set recovery-file inventory violates policy")
    recovery_files: dict[str, Path] = {}
    for recovery_name in recovery_names:
        raw_entry = raw_recovery_files[recovery_name]
        if not isinstance(raw_entry, dict) or set(raw_entry) != _RECOVERY_ENTRY_KEYS:
            raise BackupError(f"recovery-file entry is malformed for {recovery_name}")
        if raw_entry.get("filename") != recovery_name:
            raise BackupError(f"recovery-file name mismatch for {recovery_name}")
        recovery_file = _direct_child(
            directory,
            recovery_name,
            description=f"{recovery_name} recovery file",
        )
        expected_hash = raw_entry.get("sha256")
        if (
            not isinstance(expected_hash, str)
            or _SHA256.fullmatch(expected_hash) is None
            or _sha256(recovery_file) != expected_hash
        ):
            raise BackupError(f"recovery-file hash mismatch for {recovery_name}")
        if raw_entry.get("size_bytes") != recovery_file.stat().st_size:
            raise BackupError(f"recovery-file size mismatch for {recovery_name}")
        recovery_files[recovery_name] = recovery_file
        expected_files.add(recovery_file)
        total_size += recovery_file.stat().st_size
    if sum(path.stat().st_size for path in recovery_files.values()) > MAX_RECOVERY_CONFIGURATION_BYTES:
        raise BackupError("backup-set recovery configuration exceeds its aggregate size cap")

    raw_rust = payload.get("rust_recovery_generation")
    if not isinstance(raw_rust, dict) or set(raw_rust) != _RUST_ENTRY_KEYS:
        raise BackupError("backup-set Rust recovery entry is malformed")
    generation_id = raw_rust.get("generation_id")
    if not isinstance(generation_id, str):
        raise BackupError("backup-set Rust recovery generation ID is missing")
    expected_manifest_relative = f"{RUST_RECOVERY_DIRECTORY_NAME}/generation-{generation_id}/manifest.json"
    if raw_rust.get("manifest_relative_path") != expected_manifest_relative:
        raise BackupError("backup-set Rust recovery manifest path is invalid")
    rust_container = _regular_directory(directory / RUST_RECOVERY_DIRECTORY_NAME)
    rust_recovery = verify_rust_recovery_generation(
        directory.joinpath(*Path(expected_manifest_relative).parts),
        expected_generations_directory=rust_container,
        deep=deep,
    )
    if (
        rust_recovery.generation_id != generation_id
        or raw_rust.get("manifest_sha256") != rust_recovery.manifest_sha256
        or raw_rust.get("manifest_size_bytes") != rust_recovery.manifest_size_bytes
        or raw_rust.get("created_at_ms") != rust_recovery.created_at_ms
        or raw_rust.get("total_size_bytes") != rust_recovery.total_size_bytes
        or raw_rust.get("member_count") != len(rust_recovery.members)
        or raw_rust.get("restore_policy") != "empty_runtime_then_signed_reconciliation"
    ):
        raise BackupError("backup-set Rust recovery evidence does not match its immutable generation")
    expected_files.add(rust_recovery.manifest_path)
    expected_files.update(member.path for member in rust_recovery.members.values())
    total_size += rust_recovery.total_size_bytes

    source_skew = (max(source_times) - min(source_times)).total_seconds()
    recorded_skew = payload.get("source_skew_seconds")
    if not isinstance(recorded_skew, (int, float)) or float(recorded_skew) != source_skew:
        raise BackupError("backup-set source skew does not match source evidence")
    if source_skew > max_source_skew_seconds:
        raise BackupError("backup-set source skew exceeds the allowed window")
    if payload.get("total_size_bytes") != total_size:
        raise BackupError("backup-set total size does not match its members")
    actual_files = _owned_tree_files(directory)
    if actual_files != expected_files:
        raise BackupError("backup-set generation contains missing or unexpected members")
    return VerifiedBackupSet(
        manifest_path=manifest,
        set_id=set_id,
        started_at=started_at,
        completed_at=completed_at,
        source_skew_seconds=source_skew,
        total_size_bytes=total_size,
        backups=backups,
        recovery_files=recovery_files,
        rust_recovery_generation=rust_recovery,
    )


def _remove_verified_set(backup_set: VerifiedBackupSet) -> tuple[Path, ...]:
    verified = verify_backup_set(backup_set.manifest_path, deep=False)
    generation_directory = verified.manifest_path.parent
    gc_directory = generation_directory.parent / f".backup-set-gc.{verified.set_id}"
    if gc_directory.exists():
        raise BackupError("backup-set GC destination already exists")
    # Atomically unpublish the generation before deleting any member. If
    # deletion is interrupted, the exact owned directory remains resumable.
    os.replace(generation_directory, gc_directory)
    _fsync_directory(gc_directory.parent)
    return _remove_owned_gc_directory(gc_directory)


def _remove_owned_gc_directory(candidate: Path) -> tuple[Path, ...]:
    gc_directory = _regular_directory(candidate)
    prefix = ".backup-set-gc."
    if not gc_directory.name.startswith(prefix) or _SET_ID.fullmatch(gc_directory.name.removeprefix(prefix)) is None:
        raise BackupError("backup-set GC directory name is invalid")
    members = tuple(gc_directory.iterdir())
    if not members:
        gc_directory.rmdir()
        _fsync_directory(gc_directory.parent)
        return (gc_directory,)
    marker = gc_directory / _GENERATION_MARKER
    _regular_file(marker, description="backup-set GC generation marker")
    for member in members:
        metadata = member.lstat()
        if _is_link_or_reparse(member, metadata):
            raise BackupError("backup-set GC directory contains unsafe members")
        if stat.S_ISDIR(metadata.st_mode):
            if not _rust_cleanup_tree_is_owned(member):
                raise BackupError("backup-set GC directory contains an unsafe directory")
        elif not stat.S_ISREG(metadata.st_mode) or _STAGING_MEMBER.fullmatch(member.name) is None:
            raise BackupError("backup-set GC directory contains unsafe members")
    removed: list[Path] = []
    # Keep the ownership marker until every other member is gone. A crash or
    # unlink error therefore leaves a safely recognizable generation.
    for member in sorted(
        (item for item in members if item.name != _GENERATION_MARKER),
        key=lambda item: item.name,
    ):
        if member.is_dir():
            removed.extend(_remove_owned_rust_cleanup_tree(member))
        else:
            _regular_file(member, description="superseded backup-set member").unlink()
            removed.append(member)
    _regular_file(marker, description="backup-set GC generation marker").unlink()
    removed.append(marker)
    _fsync_directory(gc_directory)
    gc_directory.rmdir()
    removed.append(gc_directory)
    _fsync_directory(gc_directory.parent)
    return tuple(removed)


def cleanup_interrupted_gc(
    backup_directory: str | os.PathLike[str],
) -> tuple[Path, ...]:
    """Resume deletion only for exact, marker-owned unpublished generations."""

    directory = _regular_directory(Path(backup_directory))
    removed: list[Path] = []
    for candidate in sorted(directory.glob(".backup-set-gc.*")):
        try:
            removed.extend(_remove_owned_gc_directory(candidate))
        except (BackupError, OSError):
            continue
    return tuple(removed)


def prune_backup_sets(
    backup_directory: str | os.PathLike[str],
    *,
    retention_count: int = 1,
    protected_manifest: str | os.PathLike[str] | None = None,
) -> tuple[Path, ...]:
    directory = _regular_directory(Path(backup_directory))
    cleanup_interrupted_gc(directory)
    if any(directory.glob(".backup-set-gc.*")):
        raise BackupError("an unsafe interrupted backup-set GC generation exists")
    protected = (
        _regular_file(Path(protected_manifest), description="protected backup-set manifest")
        if protected_manifest is not None
        else None
    )
    verified: list[VerifiedBackupSet] = []
    for candidate in sorted(directory.glob("backup-set.*/backup-set.*.json")):
        try:
            verified.append(verify_backup_set(candidate, deep=False))
        except BackupError:
            continue
    verified.sort(key=lambda item: (item.completed_at, item.set_id), reverse=True)
    keep = max(1, int(retention_count))
    kept_manifests: set[Path] = set()
    if protected is not None:
        kept_manifests.add(protected)
    for item in verified:
        if len(kept_manifests) >= keep:
            break
        kept_manifests.add(item.manifest_path)
    removed: list[Path] = []
    for backup_set in reversed(verified):
        if backup_set.manifest_path in kept_manifests:
            continue
        removed.extend(_remove_verified_set(backup_set))
    return tuple(removed)


def cleanup_abandoned_staging(
    backup_directory: str | os.PathLike[str],
    *,
    now: datetime | None = None,
    minimum_age_seconds: float = 60.0,
) -> tuple[Path, ...]:
    """Remove only old, sentinel-owned incomplete generation directories."""

    directory = _regular_directory(Path(backup_directory))
    observed_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    removed: list[Path] = []
    for candidate in sorted(directory.glob(".backup-set-staging.*")):
        try:
            staging = _regular_directory(candidate)
            marker = _regular_file(
                staging / _GENERATION_MARKER,
                description="backup-set staging marker",
            )
            try:
                started_at = _utc(
                    marker.read_text(encoding="ascii").strip(),
                    field="staging start",
                )
            except (BackupError, UnicodeError):
                # A power loss can preserve the exact marker inode but not its
                # contents. Its mtime is a conservative fallback age; strict
                # directory/member allowlists still govern deletion.
                started_at = datetime.fromtimestamp(
                    marker.stat().st_mtime,
                    tz=timezone.utc,
                )
            age_seconds = (observed_now - started_at).total_seconds()
            members = tuple(staging.iterdir())
            if age_seconds < minimum_age_seconds or age_seconds < 0:
                continue
            safe_members = True
            for member in members:
                metadata = member.lstat()
                if _is_link_or_reparse(member, metadata):
                    safe_members = False
                    break
                if stat.S_ISDIR(metadata.st_mode):
                    if not _rust_cleanup_tree_is_owned(member):
                        safe_members = False
                        break
                elif not stat.S_ISREG(metadata.st_mode) or _STAGING_MEMBER.fullmatch(member.name) is None:
                    safe_members = False
                    break
            if not safe_members:
                continue
            # Incomplete staging has never been published. Remove manifests
            # and the sentinel first, then inert payload/temp files.
            ordered = sorted(
                members,
                key=lambda path: (
                    not (path.name.endswith(".json") or path.name == marker.name),
                    path.name,
                ),
            )
            for member in ordered:
                if member.is_dir():
                    removed.extend(_remove_owned_rust_cleanup_tree(member))
                else:
                    _regular_file(member, description="abandoned staging member").unlink()
                    removed.append(member)
            staging.rmdir()
            removed.append(staging)
        except (BackupError, OSError, UnicodeError):
            continue
    if removed:
        _fsync_directory(directory)
    return tuple(removed)


def create_verified_backup_set(
    data_root: str | os.PathLike[str],
    backup_directory: str | os.PathLike[str],
    *,
    rust_execution_binary: str | os.PathLike[str],
    rust_recovery_control_socket: str | os.PathLike[str],
    rust_recovery_generations_directory: str | os.PathLike[str],
    rust_recovery_timeout_ms: int = 15_000,
    rust_command_runner: CommandRunner = subprocess.run,
    source_budget_bytes: int = DEFAULT_BACKUP_BUDGET_BYTES,
    set_budget_bytes: int = DEFAULT_SET_BUDGET_BYTES,
    required_headroom_bytes: int = DEFAULT_PEAK_HEADROOM_BYTES,
    backup_tree_budget_bytes: int = DEFAULT_BACKUP_TREE_BUDGET_BYTES,
    retention_count: int = 1,
) -> VerifiedBackupSet:
    root = _regular_directory(Path(data_root))
    directory = _regular_directory(Path(backup_directory), create=True)
    cleanup_abandoned_staging(directory)
    cleanup_interrupted_gc(directory)
    if any(directory.glob(".backup-set-staging.*")):
        raise BackupError("a recent or unsafe incomplete backup-set staging generation exists")
    if any(directory.glob(".backup-set-gc.*")):
        raise BackupError("an unsafe interrupted backup-set GC generation exists")
    # A previous publication may have succeeded while best-effort GC failed.
    # Retry that bounded cleanup before calculating the next peak so retained
    # valid generations cannot permanently deadlock the backup timer.
    prune_backup_sets(directory, retention_count=retention_count)
    cleanup_interrupted_gc(directory)
    if any(directory.glob(".backup-set-gc.*")):
        raise BackupError("interrupted backup-set GC could not be completed")
    from bongus.engine.split_state_store import SplitStateReader

    split_reader = SplitStateReader(
        state_path=str(root / "state.db"),
        audit_path=str(root / "audit.db"),
        research_path=str(root / "research.db"),
    )
    split_reader.close()
    started_at = datetime.now(timezone.utc)
    current_backup_bytes = _owned_tree_size(directory)
    recovery_source_bytes = 0
    for recovery_name in _REQUIRED_RECOVERY_FILES + _OPTIONAL_RECOVERY_FILES:
        recovery_path = root / recovery_name
        if recovery_path.exists():
            recovery_source_bytes += (
                _regular_file(
                    recovery_path,
                    description=f"{recovery_name} recovery source",
                )
                .stat()
                .st_size
            )
    if recovery_source_bytes > MAX_RECOVERY_CONFIGURATION_BYTES:
        raise BackupError("recovery configuration exceeds its aggregate backup size cap")
    rust_source = request_rust_recovery_generation(
        rust_execution_binary,
        rust_recovery_control_socket,
        rust_recovery_generations_directory,
        timeout_ms=rust_recovery_timeout_ms,
        runner=rust_command_runner,
    )
    estimated_set_bytes = (
        _estimated_split_store_image_bytes(root) + recovery_source_bytes + rust_source.total_size_bytes
    )
    if backup_tree_budget_bytes <= 0 or current_backup_bytes + estimated_set_bytes > backup_tree_budget_bytes:
        raise BackupError(
            "backup publication peak exceeds the root backup-tree budget "
            f"({current_backup_bytes} + {estimated_set_bytes} > {backup_tree_budget_bytes})"
        )
    staging = directory / f".backup-set-staging.{uuid4().hex}"
    # Production's parent is setgid root:<service-group>.  Preserve group
    # traversal so the unprivileged trader can account bytes without gaining
    # write access to root-owned recovery evidence.
    staging.mkdir(mode=0o750)
    staging = _regular_directory(staging)
    marker = staging / _GENERATION_MARKER
    marker.write_text(started_at.isoformat() + "\n", encoding="ascii")
    _fsync_file(marker)
    _fsync_directory(staging)
    _fsync_directory(directory)
    created: dict[str, BackupResult] = {}
    recovery_copies: dict[str, Path] = {}
    rust_copy: VerifiedRustRecoveryGeneration | None = None
    set_manifest: Path | None = None
    published_directory: Path | None = None
    try:
        for source_name in REQUIRED_DATABASES:
            source = _regular_file(root / source_name, description=source_name)
            created[source_name] = create_verified_backup(
                source,
                staging,
                label=source_name.removesuffix(".db"),
                required_headroom_bytes=required_headroom_bytes,
                backup_budget_bytes=source_budget_bytes,
                retention_count=1_000_000,
                retention_max_total_bytes=0,
            )
        for recovery_name in _REQUIRED_RECOVERY_FILES + _OPTIONAL_RECOVERY_FILES:
            source_recovery = root / recovery_name
            if recovery_name in _OPTIONAL_RECOVERY_FILES and not source_recovery.exists():
                continue
            source_recovery = _regular_file(source_recovery, description=f"{recovery_name} recovery source")
            destination_recovery = staging / recovery_name
            shutil.copyfile(source_recovery, destination_recovery)
            _fsync_file(destination_recovery)
            recovery_copies[recovery_name] = destination_recovery
        rust_copy = copy_rust_recovery_generation(
            rust_source,
            staging / RUST_RECOVERY_DIRECTORY_NAME,
            execution_binary=rust_execution_binary,
            runner=rust_command_runner,
        )
        total_size = (
            sum(result.manifest.size_bytes for result in created.values())
            + sum(path.stat().st_size for path in recovery_copies.values())
            + rust_copy.total_size_bytes
        )
        if set_budget_bytes <= 0 or total_size > set_budget_bytes:
            raise BackupError(
                f"split-store backup set ({total_size} bytes) exceeds aggregate budget ({set_budget_bytes} bytes)"
            )
        completed_at = datetime.now(timezone.utc)
        rust_created_at = datetime.fromtimestamp(
            rust_copy.created_at_ms / 1_000.0,
            tz=timezone.utc,
        )
        if rust_created_at < started_at or rust_created_at > completed_at:
            raise BackupError("Rust recovery generation timestamp is outside the set window")
        source_times = [_utc(result.manifest.created_at, field="created_at") for result in created.values()]
        source_skew = (max(source_times) - min(source_times)).total_seconds()
        if source_skew > DEFAULT_MAX_SOURCE_SKEW_SECONDS:
            raise BackupError("split-store backup set exceeded maximum source skew")
        set_id = f"{completed_at.strftime('%Y%m%dT%H%M%S.%fZ')}-{uuid4().hex}"
        set_manifest = staging / f"backup-set.{set_id}.json"
        source_payload = {
            source_name: {
                "backup_filename": result.backup_path.name,
                "backup_sha256": result.manifest.sha256,
                "created_at": result.manifest.created_at,
                "manifest_filename": result.manifest_path.name,
                "manifest_sha256": _sha256(result.manifest_path),
                "size_bytes": result.manifest.size_bytes,
            }
            for source_name, result in created.items()
        }
        recovery_payload = {
            name: {
                "filename": name,
                "sha256": _sha256(path),
                "size_bytes": path.stat().st_size,
            }
            for name, path in sorted(recovery_copies.items())
        }
        rust_manifest_relative_path = rust_copy.manifest_path.relative_to(staging).as_posix()
        rust_payload = {
            "created_at_ms": rust_copy.created_at_ms,
            "generation_id": rust_copy.generation_id,
            "manifest_relative_path": rust_manifest_relative_path,
            "manifest_sha256": rust_copy.manifest_sha256,
            "manifest_size_bytes": rust_copy.manifest_size_bytes,
            "member_count": len(rust_copy.members),
            "restore_policy": "empty_runtime_then_signed_reconciliation",
            "total_size_bytes": rust_copy.total_size_bytes,
        }
        payload = {
            "schema_version": 1,
            "evidence_kind": "verified_split_store_backup_set",
            "format": BACKUP_SET_FORMAT,
            "set_id": set_id,
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "complete": True,
            "required_source_names": list(REQUIRED_DATABASES),
            "source_backups": source_payload,
            "source_count": len(source_payload),
            "source_skew_seconds": source_skew,
            "total_size_bytes": total_size,
            "recovery_files": recovery_payload,
            "recovery_file_names": sorted(recovery_payload),
            "recovery_file_count": len(recovery_payload),
            "rust_recovery_generation": rust_payload,
        }
        _atomic_json(set_manifest, payload)
        verify_backup_set(set_manifest, deep=False, _allow_staging=True)
        published_directory = directory / f"backup-set.{set_id}"
        if published_directory.exists():
            raise BackupError("backup-set generation directory already exists")
        os.replace(staging, published_directory)
        _fsync_directory(directory)
        set_manifest = published_directory / set_manifest.name
        verified = verify_backup_set(set_manifest, deep=False)
        try:
            prune_backup_sets(
                directory,
                retention_count=retention_count,
                protected_manifest=verified.manifest_path,
            )
        except (BackupError, OSError):
            # The complete set is already published.  Old-set GC is
            # best-effort and must not revoke the new recovery point.
            pass
        return verified
    except Exception:
        cleanup_directory = (
            published_directory if published_directory is not None and published_directory.exists() else staging
        )
        if cleanup_directory.exists() and cleanup_directory.is_dir():
            for candidate in tuple(cleanup_directory.iterdir()):
                if candidate.is_dir() and not candidate.is_symlink():
                    _remove_owned_rust_cleanup_tree(candidate)
                elif candidate.is_file() and not candidate.is_symlink():
                    candidate.unlink(missing_ok=True)
            cleanup_directory.rmdir()
        _fsync_directory(directory)
        raise


def restore_backup_set_to_empty_directory(
    manifest_path: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    rust_execution_binary: str | os.PathLike[str],
    rust_command_runner: CommandRunner = subprocess.run,
) -> tuple[Path, ...]:
    backup_set = verify_backup_set(manifest_path)
    run_rust_recovery_offline_verifier(
        rust_execution_binary,
        backup_set.rust_recovery_generation.manifest_path,
        runner=rust_command_runner,
    )
    target = Path(destination).absolute()
    if target.exists():
        if target.is_symlink() or not target.is_dir() or any(target.iterdir()):
            raise BackupError("backup-set restore destination must be empty and unlinked")
    else:
        target.mkdir(parents=True)
    target = _regular_directory(target)
    restored: list[Path] = []
    try:
        for source_name in REQUIRED_DATABASES:
            restored_result = restore_verified_backup(
                backup_set.backups[source_name].manifest_path,
                target / source_name,
            )
            restored.append(restored_result.restored_path)
        for recovery_name, source in backup_set.recovery_files.items():
            destination_file = target / recovery_name
            shutil.copyfile(source, destination_file)
            _fsync_file(destination_file)
            restored.append(destination_file)
        rust_runtime = target / "runtime" / "rust"
        rust_generations = rust_runtime / "recovery_generations"
        rust_runtime.mkdir(parents=True, mode=0o750)
        copied_generation = copy_rust_recovery_generation(
            backup_set.rust_recovery_generation,
            rust_generations,
            execution_binary=rust_execution_binary,
            runner=rust_command_runner,
        )
        restored.append(copied_generation.manifest_path)
        for member in copied_generation.members.values():
            destination_member = rust_runtime.joinpath(*Path(member.restore_relative_path).parts)
            destination_member.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
            shutil.copyfile(member.path, destination_member)
            os.chmod(destination_member, 0o640)
            _fsync_file(destination_member)
            restored.append(destination_member)
        from bongus.engine.split_state_store import SplitStateReader

        reader = SplitStateReader(
            state_path=str(target / "state.db"),
            audit_path=str(target / "audit.db"),
            research_path=str(target / "research.db"),
        )
        reader.close()
        _fsync_directory(target)
        return tuple(restored)
    except Exception:
        runtime = target / "runtime"
        if runtime.exists() and runtime.is_dir() and not runtime.is_symlink():
            shutil.rmtree(runtime)
        for path in restored:
            if path.exists() and path.is_file() and not path.is_symlink():
                path.unlink(missing_ok=True)
        _fsync_directory(target)
        raise
