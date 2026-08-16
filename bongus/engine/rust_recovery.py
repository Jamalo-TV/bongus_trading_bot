"""Strict capture, verification, and copying of Rust recovery generations."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bongus.engine.database_backup import BackupError

RUST_RECOVERY_SCHEMA_VERSION = 1
RUST_RECOVERY_EVIDENCE_KIND = "bongus_rust_recovery_generation"
RUST_RECOVERY_RESTORE_POLICY = "empty_runtime_then_signed_reconciliation"
RUST_RECOVERY_DIRECTORY_NAME = "rust-recovery"
RUST_RECOVERY_MEMBER_KEYS = frozenset(
    {
        "execution_state",
        "intent_journal",
        "telemetry_journal",
        "telemetry_ack_cursor",
        "private_cursor_spot",
        "private_cursor_futures",
    }
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_REPARSE_POINT_ATTRIBUTE = 0x0400
_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "evidence_kind",
        "complete",
        "restore_policy",
        "generation_id",
        "barrier_request_id",
        "created_at_ms",
        "terminal_sequence_watermark",
        "intent_producer_high_watermarks",
        "telemetry",
        "private_stream_cursors",
        "members",
    }
)
_MEMBER_KEYS = frozenset({"filename", "restore_relative_path", "sha256", "size_bytes"})
_TELEMETRY_KEYS = frozenset(
    {
        "published_high_water_sequence",
        "acknowledged_high_water_sequence",
        "cursor_generation",
    }
)
_PRIVATE_CURSOR_KEYS = frozenset({"through_ms"})
_CONTROL_RESPONSE_KEYS = frozenset(
    {
        "schema_version",
        "complete",
        "generation_id",
        "manifest_path",
        "manifest_sha256",
        "manifest_size_bytes",
        "pause_ms",
    }
)
_MEMBER_SIZE_CAPS = {
    "execution_state": 32_000_000,
    "intent_journal": 82_000_000,
    "telemetry_journal": 32_000_000,
    "telemetry_ack_cursor": 4_096,
    "private_cursor_spot": 17_000_000,
    "private_cursor_futures": 17_000_000,
}
MAX_RUST_RECOVERY_GENERATION_BYTES = (1 << 20) + sum(_MEMBER_SIZE_CAPS.values())
_FIXED_MEMBER_PATHS = {
    "execution_state": ("members/execution_state.jsonl", "execution_state.jsonl"),
    "intent_journal": ("members/execution_intents.jsonl", "execution_intents.jsonl"),
    "telemetry_journal": ("members/execution_telemetry.jsonl", "execution_telemetry.jsonl"),
    "private_cursor_spot": (
        "members/private_stream_cursors/spot.jsonl",
        "private_stream_cursors/spot.jsonl",
    ),
    "private_cursor_futures": (
        "members/private_stream_cursors/futures.jsonl",
        "private_stream_cursors/futures.jsonl",
    ),
}


@dataclass(frozen=True, slots=True)
class RustRecoveryMember:
    key: str
    path: Path
    filename: str
    restore_relative_path: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class VerifiedRustRecoveryGeneration:
    manifest_path: Path
    generation_directory: Path
    generation_id: str
    barrier_request_id: str
    created_at_ms: int
    terminal_sequence_watermark: int
    manifest_sha256: str
    manifest_size_bytes: int
    total_size_bytes: int
    members: Mapping[str, RustRecoveryMember]


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def _is_link_or_reparse(path: Path, metadata: os.stat_result | None = None) -> bool:
    observed = metadata if metadata is not None else path.lstat()
    return path.is_symlink() or bool(getattr(observed, "st_file_attributes", 0) & _REPARSE_POINT_ATTRIBUTE)


def _regular_file(path: Path, *, description: str) -> Path:
    candidate = path.absolute()
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise BackupError(f"{description} is unavailable: {candidate}") from exc
    if _is_link_or_reparse(candidate, metadata) or not stat.S_ISREG(metadata.st_mode):
        raise BackupError(f"{description} must be a regular non-link file")
    return candidate.resolve(strict=True)


def _regular_directory(path: Path, *, description: str) -> Path:
    candidate = path.absolute()
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise BackupError(f"{description} is unavailable: {candidate}") from exc
    if _is_link_or_reparse(candidate, metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise BackupError(f"{description} must be a regular non-link directory")
    return candidate.resolve(strict=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDWR)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _strict_nonnegative_integer(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BackupError(f"{field} must be a non-negative integer")
    return value


def _strict_positive_integer(value: object, *, field: str) -> int:
    parsed = _strict_nonnegative_integer(value, field=field)
    if parsed == 0:
        raise BackupError(f"{field} must be positive")
    return parsed


def _safe_relative_path(raw: object, *, field: str) -> str:
    if not isinstance(raw, str) or not raw or "\\" in raw:
        raise BackupError(f"{field} is not a safe relative path")
    path = Path(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise BackupError(f"{field} is not a safe relative path")
    return raw


def _read_exact_json(path: Path, *, description: str, max_bytes: int) -> dict[str, Any]:
    if path.stat().st_size > max_bytes:
        raise BackupError(f"{description} exceeds its size bound")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BackupError(f"{description} is invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise BackupError(f"{description} root must be an object")
    return payload


def _expected_member_paths(key: str, raw_filename: object) -> tuple[str, str]:
    if key != "telemetry_ack_cursor":
        return _FIXED_MEMBER_PATHS[key]
    filename = _safe_relative_path(raw_filename, field="telemetry_ack_cursor.filename")
    suffix = None
    for candidate in (".a", ".b"):
        if filename == f"members/execution_telemetry.jsonl.cursor{candidate}":
            suffix = candidate
            break
    if suffix is None:
        raise BackupError("telemetry ACK cursor member path is invalid")
    return filename, f"execution_telemetry.jsonl.cursor{suffix}"


def _collect_regular_files(root: Path) -> set[str]:
    files: set[str] = set()
    pending = [root]
    while pending:
        directory = pending.pop()
        for candidate in directory.iterdir():
            metadata = candidate.lstat()
            if _is_link_or_reparse(candidate, metadata):
                raise BackupError("Rust recovery generation contains a link or reparse point")
            if stat.S_ISDIR(metadata.st_mode):
                pending.append(candidate)
            elif stat.S_ISREG(metadata.st_mode):
                files.add(candidate.relative_to(root).as_posix())
            else:
                raise BackupError("Rust recovery generation contains an unsupported member")
    return files


def verify_rust_recovery_generation(
    manifest_path: str | os.PathLike[str],
    *,
    expected_generations_directory: str | os.PathLike[str] | None = None,
    deep: bool = True,
) -> VerifiedRustRecoveryGeneration:
    """Independently validate the strict Rust recovery-generation contract."""

    manifest = _regular_file(Path(manifest_path), description="Rust recovery manifest")
    generation_directory = _regular_directory(
        manifest.parent,
        description="Rust recovery generation directory",
    )
    if manifest.name != "manifest.json":
        raise BackupError("Rust recovery manifest must be named manifest.json")
    payload = _read_exact_json(
        manifest,
        description="Rust recovery manifest",
        max_bytes=1 << 20,
    )
    if set(payload) != _MANIFEST_KEYS:
        raise BackupError("Rust recovery manifest fields do not match the exact schema")
    if (
        payload.get("schema_version") != RUST_RECOVERY_SCHEMA_VERSION
        or payload.get("evidence_kind") != RUST_RECOVERY_EVIDENCE_KIND
        or payload.get("complete") is not True
        or payload.get("restore_policy") != RUST_RECOVERY_RESTORE_POLICY
    ):
        raise BackupError("Rust recovery manifest identity/completion is invalid")
    generation_id = payload.get("generation_id")
    barrier_request_id = payload.get("barrier_request_id")
    if not isinstance(generation_id, str) or _IDENTIFIER.fullmatch(generation_id) is None:
        raise BackupError("Rust recovery generation ID is invalid")
    if not isinstance(barrier_request_id, str) or _IDENTIFIER.fullmatch(barrier_request_id) is None:
        raise BackupError("Rust recovery barrier request ID is invalid")
    if generation_directory.name != f"generation-{generation_id}":
        raise BackupError("Rust recovery generation directory/ID mismatch")
    if expected_generations_directory is not None:
        expected_root = _regular_directory(
            Path(expected_generations_directory),
            description="Rust recovery generations root",
        )
        if generation_directory.parent != expected_root:
            raise BackupError("Rust recovery generation escaped its expected root")

    created_at_ms = _strict_positive_integer(payload.get("created_at_ms"), field="created_at_ms")
    terminal_sequence = _strict_nonnegative_integer(
        payload.get("terminal_sequence_watermark"),
        field="terminal_sequence_watermark",
    )
    intent_highwaters = payload.get("intent_producer_high_watermarks")
    if not isinstance(intent_highwaters, dict):
        raise BackupError("Rust intent producer high-watermarks must be an object")
    for producer, sequence in intent_highwaters.items():
        if not isinstance(producer, str) or not producer.strip():
            raise BackupError("Rust intent producer identity is invalid")
        _strict_positive_integer(sequence, field=f"intent producer {producer!r} high-water")

    telemetry = payload.get("telemetry")
    if not isinstance(telemetry, dict) or set(telemetry) != _TELEMETRY_KEYS:
        raise BackupError("Rust telemetry recovery watermarks are malformed")
    published = _strict_nonnegative_integer(
        telemetry.get("published_high_water_sequence"),
        field="published_high_water_sequence",
    )
    acknowledged = _strict_nonnegative_integer(
        telemetry.get("acknowledged_high_water_sequence"),
        field="acknowledged_high_water_sequence",
    )
    _strict_nonnegative_integer(telemetry.get("cursor_generation"), field="cursor_generation")
    if acknowledged > published:
        raise BackupError("Rust telemetry ACK exceeds the published high-water")

    private_cursors = payload.get("private_stream_cursors")
    if not isinstance(private_cursors, dict) or set(private_cursors) != {"spot", "futures"}:
        raise BackupError("Rust private cursor roles are not exact")
    for role, raw_cursor in private_cursors.items():
        if not isinstance(raw_cursor, dict) or set(raw_cursor) != _PRIVATE_CURSOR_KEYS:
            raise BackupError(f"Rust private cursor {role} is malformed")
        through_ms = raw_cursor.get("through_ms")
        if through_ms is not None:
            _strict_nonnegative_integer(through_ms, field=f"{role}.through_ms")

    raw_members = payload.get("members")
    if not isinstance(raw_members, dict) or set(raw_members) != RUST_RECOVERY_MEMBER_KEYS:
        raise BackupError("Rust recovery member set is not exact")
    members: dict[str, RustRecoveryMember] = {}
    expected_files = {"manifest.json"}
    restore_paths: set[str] = set()
    total_size = manifest.stat().st_size
    for key in sorted(RUST_RECOVERY_MEMBER_KEYS):
        raw_member = raw_members[key]
        if not isinstance(raw_member, dict) or set(raw_member) != _MEMBER_KEYS:
            raise BackupError(f"Rust recovery member {key} metadata is malformed")
        expected_filename, expected_restore = _expected_member_paths(key, raw_member.get("filename"))
        filename = _safe_relative_path(raw_member.get("filename"), field=f"{key}.filename")
        restore_relative_path = _safe_relative_path(
            raw_member.get("restore_relative_path"),
            field=f"{key}.restore_relative_path",
        )
        if filename != expected_filename or restore_relative_path != expected_restore:
            raise BackupError(f"Rust recovery member {key} paths violate policy")
        if restore_relative_path in restore_paths:
            raise BackupError("Rust recovery restore paths are not unique")
        restore_paths.add(restore_relative_path)
        expected_hash = raw_member.get("sha256")
        if not isinstance(expected_hash, str) or _SHA256.fullmatch(expected_hash) is None:
            raise BackupError(f"Rust recovery member {key} hash is invalid")
        expected_size = _strict_nonnegative_integer(
            raw_member.get("size_bytes"),
            field=f"{key}.size_bytes",
        )
        if expected_size > _MEMBER_SIZE_CAPS[key]:
            raise BackupError(f"Rust recovery member {key} exceeds its size cap")
        member_path = _regular_file(
            generation_directory.joinpath(*Path(filename).parts),
            description=f"Rust recovery member {key}",
        )
        if member_path.relative_to(generation_directory).as_posix() != filename:
            raise BackupError(f"Rust recovery member {key} escapes its generation")
        if member_path.stat().st_size != expected_size:
            raise BackupError(f"Rust recovery member {key} size mismatch")
        if deep and _sha256(member_path) != expected_hash:
            raise BackupError(f"Rust recovery member {key} hash mismatch")
        members[key] = RustRecoveryMember(
            key=key,
            path=member_path,
            filename=filename,
            restore_relative_path=restore_relative_path,
            sha256=expected_hash,
            size_bytes=expected_size,
        )
        expected_files.add(filename)
        total_size += expected_size
    if _collect_regular_files(generation_directory) != expected_files:
        raise BackupError("Rust recovery generation contains missing or unexpected files")
    return VerifiedRustRecoveryGeneration(
        manifest_path=manifest,
        generation_directory=generation_directory,
        generation_id=generation_id,
        barrier_request_id=barrier_request_id,
        created_at_ms=created_at_ms,
        terminal_sequence_watermark=terminal_sequence,
        manifest_sha256=_sha256(manifest),
        manifest_size_bytes=manifest.stat().st_size,
        total_size_bytes=total_size,
        members=members,
    )


def _parse_control_response(stdout: str, *, context: str) -> dict[str, Any]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise BackupError(f"{context} did not emit one JSON response") from exc
    if not isinstance(payload, dict) or set(payload) != _CONTROL_RESPONSE_KEYS:
        raise BackupError(f"{context} response fields do not match the exact schema")
    if payload.get("schema_version") != 1 or payload.get("complete") is not True:
        raise BackupError(f"{context} response is incomplete")
    generation_id = payload.get("generation_id")
    if not isinstance(generation_id, str) or _IDENTIFIER.fullmatch(generation_id) is None:
        raise BackupError(f"{context} generation ID is invalid")
    manifest_path = payload.get("manifest_path")
    if not isinstance(manifest_path, str) or not Path(manifest_path).is_absolute():
        raise BackupError(f"{context} manifest path must be absolute")
    manifest_sha256 = payload.get("manifest_sha256")
    if not isinstance(manifest_sha256, str) or _SHA256.fullmatch(manifest_sha256) is None:
        raise BackupError(f"{context} manifest hash is invalid")
    _strict_positive_integer(payload.get("manifest_size_bytes"), field="manifest_size_bytes")
    _strict_nonnegative_integer(payload.get("pause_ms"), field="pause_ms")
    return payload


def _run(
    command: list[str],
    *,
    timeout_seconds: float,
    runner: CommandRunner,
    context: str,
) -> dict[str, Any]:
    try:
        completed = runner(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BackupError(f"{context} could not execute: {exc}") from exc
    if completed.returncode != 0:
        detail = str(completed.stderr or "").strip()[:500]
        raise BackupError(f"{context} exited {completed.returncode}: {detail}")
    return _parse_control_response(completed.stdout, context=context)


def run_rust_recovery_offline_verifier(
    execution_binary: str | os.PathLike[str],
    manifest_path: str | os.PathLike[str],
    *,
    runner: CommandRunner = subprocess.run,
    timeout_seconds: float = 30.0,
) -> VerifiedRustRecoveryGeneration:
    binary = _regular_file(Path(execution_binary), description="Rust execution binary")
    manifest = _regular_file(Path(manifest_path), description="Rust recovery manifest")
    payload = _run(
        [str(binary), "--verify-recovery-generation", str(manifest)],
        timeout_seconds=timeout_seconds,
        runner=runner,
        context="Rust recovery offline verifier",
    )
    verified = verify_rust_recovery_generation(manifest)
    if (
        payload["generation_id"] != verified.generation_id
        or payload["manifest_path"] != str(verified.manifest_path)
        or payload["manifest_sha256"] != verified.manifest_sha256
        or payload["manifest_size_bytes"] != verified.manifest_size_bytes
        or payload["pause_ms"] != 0
    ):
        raise BackupError("Rust offline-verifier response does not match the immutable generation")
    return verified


def request_rust_recovery_generation(
    execution_binary: str | os.PathLike[str],
    control_socket: str | os.PathLike[str],
    generations_directory: str | os.PathLike[str],
    *,
    timeout_ms: int = 15_000,
    runner: CommandRunner = subprocess.run,
) -> VerifiedRustRecoveryGeneration:
    binary = _regular_file(Path(execution_binary), description="Rust execution binary")
    raw_socket = Path(control_socket)
    if not raw_socket.is_absolute():
        raise BackupError("Rust recovery control socket path must be absolute")
    socket = raw_socket.absolute()
    if not 1_000 <= timeout_ms <= 15_000:
        raise BackupError("Rust recovery barrier timeout must be 1000..15000 ms")
    raw_generations_root = Path(generations_directory)
    if not raw_generations_root.is_absolute():
        raise BackupError("Rust recovery generations root must be absolute")
    generations_root = raw_generations_root.absolute()
    if generations_root.exists():
        generations_root = _regular_directory(
            generations_root,
            description="Rust recovery generations root",
        )
    payload = _run(
        [
            str(binary),
            "--create-recovery-generation",
            "--socket",
            str(socket),
            "--timeout-ms",
            str(timeout_ms),
        ],
        timeout_seconds=(timeout_ms / 1_000.0) + 5.0,
        runner=runner,
        context="Rust recovery generation request",
    )
    manifest = _regular_file(
        Path(str(payload["manifest_path"])),
        description="Rust recovery manifest",
    )
    generations_root = _regular_directory(
        generations_root,
        description="Rust recovery generations root",
    )
    verified = verify_rust_recovery_generation(
        manifest,
        expected_generations_directory=generations_root,
    )
    if (
        payload["generation_id"] != verified.generation_id
        or payload["manifest_path"] != str(verified.manifest_path)
        or payload["manifest_sha256"] != verified.manifest_sha256
        or payload["manifest_size_bytes"] != verified.manifest_size_bytes
        or payload["pause_ms"] > timeout_ms
    ):
        raise BackupError("Rust recovery response does not match the immutable generation")
    return run_rust_recovery_offline_verifier(
        binary,
        verified.manifest_path,
        runner=runner,
    )


def copy_rust_recovery_generation(
    source: VerifiedRustRecoveryGeneration,
    destination_container: str | os.PathLike[str],
    *,
    execution_binary: str | os.PathLike[str],
    runner: CommandRunner = subprocess.run,
) -> VerifiedRustRecoveryGeneration:
    """Copy one verified immutable generation into an unpublished set."""

    verified_source = verify_rust_recovery_generation(source.manifest_path)
    if verified_source.manifest_sha256 != source.manifest_sha256:
        raise BackupError("Rust recovery source changed before backup copy")
    container = Path(destination_container).absolute()
    container.mkdir(mode=0o750)
    container = _regular_directory(container, description="Rust recovery backup container")
    target = container / f"generation-{source.generation_id}"
    target.mkdir(mode=0o750)
    members_root = target / "members"
    private_root = members_root / "private_stream_cursors"
    private_root.mkdir(parents=True, mode=0o750)
    try:
        for candidate in (target, members_root, private_root):
            os.chmod(candidate, 0o750)
        shutil.copyfile(source.manifest_path, target / "manifest.json")
        os.chmod(target / "manifest.json", 0o640)
        _fsync_file(target / "manifest.json")
        for member in source.members.values():
            destination = target.joinpath(*Path(member.filename).parts)
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
            shutil.copyfile(member.path, destination)
            os.chmod(destination, 0o640)
            _fsync_file(destination)
        _fsync_directory(private_root)
        _fsync_directory(members_root)
        _fsync_directory(target)
        _fsync_directory(container)
        copied = verify_rust_recovery_generation(
            target / "manifest.json",
            expected_generations_directory=container,
        )
        if copied.manifest_sha256 != source.manifest_sha256:
            raise BackupError("Rust recovery copy does not match the source manifest")
        return run_rust_recovery_offline_verifier(
            execution_binary,
            copied.manifest_path,
            runner=runner,
        )
    except Exception:
        if target.exists() and target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        if container.exists() and not any(container.iterdir()):
            container.rmdir()
        raise
