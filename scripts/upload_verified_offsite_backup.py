"""Upload the newest verified operational backup to an encrypted Restic repository."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from bongus.engine.backup_set import (
    REQUIRED_DATABASES,
    VerifiedBackupSet,
    verify_backup_set,
)
from bongus.engine.database_backup import BackupError

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REPARSE_POINT_ATTRIBUTE = 0x0400


class OffsiteBackupError(RuntimeError):
    """The offsite upload contract could not be proven."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_file(path: Path, description: str) -> Path:
    candidate = path.absolute()
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise OffsiteBackupError(f"{description} is unavailable") from exc
    is_reparse = bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT_ATTRIBUTE)
    if candidate.is_symlink() or is_reparse or not stat.S_ISREG(metadata.st_mode):
        raise OffsiteBackupError(f"{description} must be a regular non-link file")
    return candidate.resolve(strict=True)


def _latest_verified_backup_set(backup_directory: Path) -> VerifiedBackupSet:
    if backup_directory.is_symlink():
        raise OffsiteBackupError("backup directory cannot be a symlink")
    try:
        directory = backup_directory.resolve(strict=True)
    except OSError as exc:
        raise OffsiteBackupError("backup directory is unavailable") from exc
    if not directory.is_dir() or directory.is_symlink():
        raise OffsiteBackupError("backup directory must be a regular non-link directory")
    verified: list[VerifiedBackupSet] = []
    failures: list[str] = []
    for manifest in sorted(directory.glob("backup-set.*/backup-set.*.json")):
        try:
            # The credential-bearing network process never invokes SQLite's
            # parser. The isolated backup identity already performed the deep
            # integrity pass; this process rehashes immutable payload bytes.
            candidate = verify_backup_set(manifest, deep=False)
            for backup in candidate.backups.values():
                if _sha256(backup.backup_path) != backup.manifest.sha256:
                    raise BackupError("backup payload hash changed after publication")
            rust_recovery = candidate.rust_recovery_generation
            if _sha256(rust_recovery.manifest_path) != rust_recovery.manifest_sha256:
                raise BackupError("Rust recovery manifest changed after publication")
            for member in rust_recovery.members.values():
                if _sha256(member.path) != member.sha256:
                    raise BackupError(f"Rust recovery member {member.key} changed after publication")
            verified.append(candidate)
        except (BackupError, OSError) as exc:
            failures.append(f"{manifest.name}: {exc}")
    if not verified:
        detail = "; ".join(failures[:3])
        raise OffsiteBackupError(f"a complete verified split-store backup set is missing; {detail}")
    return max(verified, key=lambda item: (item.completed_at, item.set_id))


def _require_remote_hostname(hostname: str | None) -> None:
    normalized = str(hostname or "").strip().casefold().rstrip(".")
    if (
        not normalized
        or normalized in {"localhost", "localhost.localdomain"}
        or normalized.endswith((".local", ".localhost", ".internal", ".nip.io", ".sslip.io"))
    ):
        raise OffsiteBackupError("offsite repository hostname is local or missing")
    try:
        address = ipaddress.ip_address(normalized.strip("[]"))
    except ValueError:
        return
    if (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_unspecified
        or address.is_reserved
    ):
        raise OffsiteBackupError("offsite repository IP is not independently remote")


def _restic_environment(environment: dict[str, str]) -> tuple[str, Path, str, str]:
    repository = environment.get("RESTIC_REPOSITORY", "").strip()
    if not repository or ":" not in repository:
        raise OffsiteBackupError("RESTIC_REPOSITORY must identify a remote backend")
    scheme = repository.split(":", 1)[0].casefold()
    if scheme in {"local", "file", "rclone"} or repository.startswith(("/", "\\")):
        raise OffsiteBackupError("local filesystems cannot satisfy encrypted offsite backup")
    if scheme in {"rest", "s3"}:
        endpoint = repository.split(":", 1)[1]
        parsed = urlparse(endpoint)
        if parsed.scheme.casefold() != "https":
            raise OffsiteBackupError(f"{scheme} offsite repositories require explicit HTTPS")
        _require_remote_hostname(parsed.hostname)
    else:
        raise OffsiteBackupError("RESTIC_REPOSITORY backend is not in the explicit-HTTPS remote allowlist")
    password_value = environment.get("RESTIC_PASSWORD", "")
    if password_value:
        raise OffsiteBackupError("inline RESTIC_PASSWORD is forbidden; use RESTIC_PASSWORD_FILE")
    if environment.get("RESTIC_PASSWORD_COMMAND", "").strip():
        raise OffsiteBackupError("RESTIC_PASSWORD_COMMAND is forbidden; use RESTIC_PASSWORD_FILE")
    raw_password_path = environment.get("RESTIC_PASSWORD_FILE", "").strip()
    if not raw_password_path:
        raise OffsiteBackupError("RESTIC_PASSWORD_FILE is required")
    password_path = _regular_file(Path(raw_password_path), "Restic password file")
    if os.name == "posix":
        password_metadata = password_path.stat()
        if password_metadata.st_mode & 0o037:
            raise OffsiteBackupError("Restic password file cannot be group-writable/executable or world-accessible")
        if password_metadata.st_uid not in {0, os.geteuid()}:
            raise OffsiteBackupError("Restic password file must be owned by root or the offsite identity")
    expected_repository_id = (
        environment.get(
            "BONGUS_EXPECTED_RESTIC_REPOSITORY_ID",
            "",
        )
        .strip()
        .casefold()
    )
    if _SHA256.fullmatch(expected_repository_id) is None:
        raise OffsiteBackupError("BONGUS_EXPECTED_RESTIC_REPOSITORY_ID must pin the reviewed Restic config ID")
    return repository, password_path, scheme, expected_repository_id


def _verify_restic_binary_identity(
    *,
    restic_binary: str,
    environment: dict[str, str],
    timeout_seconds: float,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> tuple[str, str]:
    binary = _regular_file(Path(restic_binary), "Restic binary")
    expected_hash = environment.get("BONGUS_EXPECTED_RESTIC_BINARY_SHA256", "").strip().casefold()
    if _SHA256.fullmatch(expected_hash) is None:
        raise OffsiteBackupError("BONGUS_EXPECTED_RESTIC_BINARY_SHA256 must pin the reviewed Restic executable")
    actual_hash = _sha256(binary)
    if actual_hash != expected_hash:
        raise OffsiteBackupError("Restic executable does not match the operator hash pin")
    expected_version = environment.get("BONGUS_EXPECTED_RESTIC_VERSION", "").strip()
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", expected_version) is None:
        raise OffsiteBackupError("BONGUS_EXPECTED_RESTIC_VERSION must pin an exact final Restic version")
    try:
        completed = runner(
            [str(binary), "version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OffsiteBackupError(f"Restic version check failed to execute: {exc}") from exc
    if completed.returncode != 0:
        raise OffsiteBackupError(f"Restic version check exited {completed.returncode}")
    version_line = (completed.stdout or "").splitlines()[0].strip()
    observed_match = re.fullmatch(
        r"restic ([0-9]+\.[0-9]+\.[0-9]+)(?: .*)?",
        version_line,
    )
    if observed_match is None or observed_match.group(1) != expected_version:
        raise OffsiteBackupError("Restic version does not match the operator pin")
    return actual_hash, expected_version


def _recovery_inputs(
    data_root: Path,
    backup_set: VerifiedBackupSet,
) -> tuple[Path, ...]:
    backups = backup_set.backups
    database_inputs = tuple(
        path
        for source_name in REQUIRED_DATABASES
        for path in (
            backups[source_name].backup_path,
            backups[source_name].manifest_path,
        )
    )
    candidates = (
        *database_inputs,
        backup_set.manifest_path,
        *backup_set.recovery_files.values(),
        backup_set.rust_recovery_generation.manifest_path,
        *(member.path for member in backup_set.rust_recovery_generation.members.values()),
    )
    inputs: list[Path] = []
    for candidate in candidates:
        if not candidate.exists():
            continue
        resolved = candidate.resolve(strict=True)
        try:
            resolved.relative_to(data_root)
        except ValueError as exc:
            raise OffsiteBackupError("recovery input escapes the data root") from exc
        if candidate.is_symlink():
            raise OffsiteBackupError("recovery inputs cannot be symlinks")
        if candidate.is_dir() and any(path.is_symlink() for path in candidate.rglob("*")):
            raise OffsiteBackupError("recovery directories cannot contain symlinks")
        inputs.append(resolved)
    return tuple(inputs)


def _snapshot_id(stdout: str) -> str:
    snapshot_ids: list[str] = []
    for raw_line in stdout.splitlines():
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or payload.get("message_type") != "summary":
            continue
        snapshot_id = str(payload.get("snapshot_id") or "").casefold()
        if _SHA256.fullmatch(snapshot_id):
            snapshot_ids.append(snapshot_id)
    if len(snapshot_ids) != 1:
        raise OffsiteBackupError("Restic output must contain exactly one valid snapshot ID")
    return snapshot_ids[0]


def _repository_config_id(stdout: str) -> str:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise OffsiteBackupError("Restic repository config is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise OffsiteBackupError("Restic repository config root must be an object")
    repository_id = str(payload.get("id") or "").casefold()
    if _SHA256.fullmatch(repository_id) is None:
        raise OffsiteBackupError("Restic repository config ID is malformed")
    return repository_id


def _write_receipt(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
        if os.name == "posix":
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_descriptor = os.open(path.parent, flags)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _read_repository_config_id(
    *,
    restic_binary: str,
    environment: dict[str, str],
    timeout_seconds: float,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> str:
    try:
        result = runner(
            [restic_binary, "--no-cache", "cat", "config"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OffsiteBackupError(f"Restic repository identity readback failed to execute: {exc}") from exc
    if result.returncode != 0:
        raise OffsiteBackupError(f"Restic repository identity readback exited {result.returncode}")
    return _repository_config_id(result.stdout)


def upload_latest_verified_backup(
    *,
    data_root: Path,
    backup_directory: Path,
    receipt_path: Path,
    restic_binary: str,
    environment: dict[str, str],
    timeout_seconds: float = 900.0,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    root = data_root.resolve(strict=True)
    if data_root.is_symlink() or not root.is_dir():
        raise OffsiteBackupError("data root must be a regular non-link directory")
    raw_receipt = receipt_path.absolute()
    try:
        relative_receipt = raw_receipt.relative_to(root)
    except ValueError as exc:
        raise OffsiteBackupError("offsite receipt must remain beneath the data root") from exc
    cursor = root
    for part in relative_receipt.parts:
        cursor /= part
        if cursor.exists() and cursor.is_symlink():
            raise OffsiteBackupError("offsite receipt path cannot contain symlinks")
    receipt = raw_receipt.resolve(strict=False)
    try:
        receipt.relative_to(root)
    except ValueError as exc:
        raise OffsiteBackupError("offsite receipt resolution escapes the data root") from exc
    if not (Path(restic_binary).is_absolute() or PurePosixPath(restic_binary).is_absolute()):
        raise OffsiteBackupError("Restic binary path must be absolute")
    (
        repository,
        _password_path,
        repository_backend,
        expected_repository_id,
    ) = _restic_environment(environment)
    restic_binary_sha256, restic_version = _verify_restic_binary_identity(
        restic_binary=restic_binary,
        environment=environment,
        timeout_seconds=timeout_seconds,
        runner=runner,
    )
    backup_set = _latest_verified_backup_set(backup_directory)
    backups = backup_set.backups
    inputs = _recovery_inputs(root, backup_set)
    mandatory_inputs = {path for backup in backups.values() for path in (backup.backup_path, backup.manifest_path)}
    mandatory_inputs.add(backup_set.manifest_path)
    mandatory_inputs.add(backup_set.rust_recovery_generation.manifest_path)
    mandatory_inputs.update(member.path for member in backup_set.rust_recovery_generation.members.values())
    if not mandatory_inputs.issubset(inputs):
        raise OffsiteBackupError("every verified database backup and manifest is mandatory")
    repository_config_id = _read_repository_config_id(
        restic_binary=restic_binary,
        environment=environment,
        timeout_seconds=timeout_seconds,
        runner=runner,
    )
    if repository_config_id != expected_repository_id:
        raise OffsiteBackupError("Restic repository config ID does not match the operator pin")
    command = [
        restic_binary,
        "--no-cache",
        "backup",
        "--json",
        "--tag",
        "bongus-operational",
        "--",
        *(str(path) for path in inputs),
    ]
    try:
        completed = runner(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OffsiteBackupError(f"Restic upload failed to execute: {exc}") from exc
    if completed.returncode != 0:
        raise OffsiteBackupError(f"Restic upload exited {completed.returncode}")
    snapshot_id = _snapshot_id(completed.stdout)
    post_upload_repository_id = _read_repository_config_id(
        restic_binary=restic_binary,
        environment=environment,
        timeout_seconds=timeout_seconds,
        runner=runner,
    )
    if post_upload_repository_id != repository_config_id:
        raise OffsiteBackupError("Restic repository identity changed during upload")
    source_backups = {
        source_name: {
            "source_created_at": backup.manifest.created_at,
            "source_manifest_sha256": _sha256(backup.manifest_path),
            "source_backup_sha256": backup.manifest.sha256,
        }
        for source_name, backup in sorted(backups.items())
    }
    payload: dict[str, Any] = {
        "schema_version": 1,
        "evidence_kind": "encrypted_offsite_backup_receipt",
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "encrypted": True,
        "offsite": True,
        "repository_id_sha256": repository_config_id,
        "repository_locator_sha256": hashlib.sha256(repository.encode()).hexdigest(),
        "repository_backend": repository_backend,
        "repository_pin_verified": True,
        "restic_binary_sha256": restic_binary_sha256,
        "restic_version": restic_version,
        "snapshot_id": snapshot_id,
        "backup_set_id": backup_set.set_id,
        "backup_set_completed_at": backup_set.completed_at.isoformat(),
        "backup_set_manifest_sha256": _sha256(backup_set.manifest_path),
        "backup_set_source_skew_seconds": backup_set.source_skew_seconds,
        "source_backups": source_backups,
        "recovery_files": {
            name: {"sha256": _sha256(path), "size_bytes": path.stat().st_size}
            for name, path in sorted(backup_set.recovery_files.items())
        },
        "recovery_input_count": len(inputs),
        "rust_recovery_generation": {
            "created_at_ms": backup_set.rust_recovery_generation.created_at_ms,
            "generation_id": backup_set.rust_recovery_generation.generation_id,
            "manifest_sha256": backup_set.rust_recovery_generation.manifest_sha256,
            "manifest_size_bytes": backup_set.rust_recovery_generation.manifest_size_bytes,
            "member_count": len(backup_set.rust_recovery_generation.members),
            "members": {
                key: {
                    "restore_relative_path": member.restore_relative_path,
                    "sha256": member.sha256,
                    "size_bytes": member.size_bytes,
                }
                for key, member in sorted(backup_set.rust_recovery_generation.members.items())
            },
            "restore_policy": "empty_runtime_then_signed_reconciliation",
            "total_size_bytes": backup_set.rust_recovery_generation.total_size_bytes,
        },
        "mutable_rust_runtime_included": True,
        "restart_requires_exchange_reconciliation": True,
    }
    _write_receipt(receipt, payload)
    return payload


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0.0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _parser() -> argparse.ArgumentParser:
    data_root = Path(os.getenv("BONGUS_DATA_ROOT", Path(__file__).resolve().parents[1]))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=data_root)
    parser.add_argument("--backup-directory", type=Path, default=data_root / "backups")
    parser.add_argument(
        "--receipt-path",
        type=Path,
        default=data_root / "offsite" / "upload" / "latest.json",
    )
    parser.add_argument("--restic-binary", default="/usr/bin/restic")
    parser.add_argument("--timeout-seconds", type=_positive_float, default=900.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload = upload_latest_verified_backup(
            data_root=args.data_root,
            backup_directory=args.backup_directory,
            receipt_path=args.receipt_path,
            restic_binary=args.restic_binary,
            environment=dict(os.environ),
            timeout_seconds=args.timeout_seconds,
        )
    except (BackupError, OffsiteBackupError, OSError, ValueError) as exc:
        print(
            json.dumps({"status": "failed", "error": str(exc)}, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "status": "uploaded",
                "snapshot_id": payload["snapshot_id"],
                "receipt_path": str(args.receipt_path.resolve()),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
