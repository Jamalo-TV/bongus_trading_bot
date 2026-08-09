"""Verified SQLite online backup, restore, and restoration-drill tooling."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
import time
from typing import Any, Callable, Mapping


MANIFEST_FORMAT = "bongus-sqlite-backup-v1"
DEFAULT_BACKUP_BUDGET_BYTES = 1_500_000_000
DEFAULT_PEAK_HEADROOM_BYTES = 512_000_000
_REPARSE_POINT_ATTRIBUTE = 0x0400


class BackupError(RuntimeError):
    """Raised when a backup or restore cannot prove its invariants."""


class CorruptDatabaseError(BackupError):
    """Raised when the primary or a SQLite sidecar is structurally corrupt."""


@dataclass(frozen=True, slots=True)
class BackupManifest:
    format: str
    created_at: str
    source_name: str
    backup_filename: str
    sha256: str
    size_bytes: int
    sqlite_version: str
    schema_user_version: int
    application_id: int
    integrity_check: str
    table_row_counts: Mapping[str, int]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["table_row_counts"] = dict(sorted(self.table_row_counts.items()))
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "BackupManifest":
        try:
            return cls(
                format=str(payload["format"]),
                created_at=str(payload["created_at"]),
                source_name=str(payload["source_name"]),
                backup_filename=str(payload["backup_filename"]),
                sha256=str(payload["sha256"]),
                size_bytes=int(payload["size_bytes"]),
                sqlite_version=str(payload["sqlite_version"]),
                schema_user_version=int(payload["schema_user_version"]),
                application_id=int(payload["application_id"]),
                integrity_check=str(payload["integrity_check"]),
                table_row_counts={
                    str(name): int(count)
                    for name, count in dict(payload["table_row_counts"]).items()
                },
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BackupError(f"Invalid backup manifest: {exc}") from exc


@dataclass(frozen=True, slots=True)
class BackupResult:
    backup_path: Path
    manifest_path: Path
    manifest: BackupManifest


@dataclass(frozen=True, slots=True)
class RestoreResult:
    restored_path: Path
    source_backup_path: Path
    manifest_path: Path
    pre_restore_backup_path: Path | None
    restored_at: str
    table_row_counts: Mapping[str, int]
    quarantined_corrupt_files: tuple[Path, ...] = ()


DiskUsageProbe = Callable[[Path], Any]


def _is_link_or_reparse(path: Path, metadata: os.stat_result | None = None) -> bool:
    """Return whether *path* is a filesystem link or Windows reparse point."""

    path_metadata = metadata if metadata is not None else path.lstat()
    return path.is_symlink() or bool(
        getattr(path_metadata, "st_file_attributes", 0) & _REPARSE_POINT_ATTRIBUTE
    )


def _safe_backup_directory(path: Path) -> Path:
    """Resolve one real backup directory without accepting a linked root."""

    candidate = path.absolute()
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise BackupError(f"Backup directory is unavailable: {candidate}") from exc
    if _is_link_or_reparse(candidate, metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise BackupError(
            f"Backup directory must be a regular non-link/reparse directory: {candidate}"
        )
    return candidate.resolve(strict=True)


def _safe_contained_backup_file(
    path: Path,
    directory: Path,
    *,
    description: str,
) -> Path:
    """Return a direct, regular child of *directory* without following links."""

    safe_directory = _safe_backup_directory(directory)
    candidate = path.absolute()
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise BackupError(f"{description} does not exist: {candidate}") from exc
    if _is_link_or_reparse(candidate, metadata) or not stat.S_ISREG(metadata.st_mode):
        raise BackupError(
            f"{description} must be a regular non-link/reparse file: {candidate}"
        )
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise BackupError(f"Could not resolve {description}: {candidate}") from exc
    if resolved.parent != safe_directory:
        raise BackupError(
            f"{description} escapes the backup directory: {candidate}"
        )
    return resolved


def _preflight_peak_space(
    directory: Path,
    *,
    operation_bytes: int,
    required_headroom_bytes: int,
    disk_usage_probe: DiskUsageProbe,
    operation: str,
) -> None:
    """Prove an operation can finish without consuming recovery headroom."""

    try:
        usage = disk_usage_probe(directory)
        free_bytes = int(usage.free)
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise BackupError(
            f"cannot determine free space for {operation} at {directory}: {exc}"
        ) from exc
    required = max(0, int(operation_bytes)) + max(0, int(required_headroom_bytes))
    if free_bytes < required:
        raise BackupError(
            f"insufficient peak space for {operation}: free={free_bytes}, "
            f"required={required} (operation={operation_bytes}, "
            f"headroom={required_headroom_bytes})"
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDWR)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    # Windows does not permit opening directories this way; the atomic replace
    # still holds, while POSIX gets the stronger directory-entry durability.
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _readonly_connection(
    path: Path,
    *,
    immutable: bool = False,
) -> sqlite3.Connection:
    """Open a read-only database, optionally as a standalone immutable image.

    ``immutable=1`` deliberately ignores SQLite sidecars and locking.  It is
    therefore reserved for quiescent backup/restore images whose main file is
    the checksummed authority.  Live source databases must use the default so
    the online-backup API sees every committed WAL frame.
    """

    uri_path = path.resolve().as_posix()
    immutable_query = "&immutable=1" if immutable else ""
    return sqlite3.connect(
        f"file:{uri_path}?mode=ro{immutable_query}",
        uri=True,
        timeout=30,
    )


def _integrity_check(connection: sqlite3.Connection) -> str:
    rows = connection.execute("PRAGMA integrity_check").fetchall()
    result = "; ".join(str(row[0]) for row in rows)
    if result.lower() != "ok":
        raise BackupError(f"SQLite integrity_check failed: {result[:500]}")
    return result


def _table_row_counts(connection: sqlite3.Connection) -> dict[str, int]:
    names = [
        str(row[0])
        for row in connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
    ]
    counts: dict[str, int] = {}
    for name in names:
        quoted = name.replace('"', '""')
        counts[name] = int(connection.execute(f'SELECT COUNT(*) FROM "{quoted}"').fetchone()[0])
    return counts


def _database_metadata(
    path: Path,
    *,
    immutable: bool = False,
) -> tuple[str, int, int, dict[str, int]]:
    with closing(_readonly_connection(path, immutable=immutable)) as connection:
        integrity = _integrity_check(connection)
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        counts = _table_row_counts(connection)
    return integrity, user_version, application_id, counts


def _atomic_json_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    finally:
        temp_path.unlink(missing_ok=True)


def create_verified_backup(
    source_db_path: str | os.PathLike[str],
    backup_directory: str | os.PathLike[str],
    *,
    label: str = "state",
    required_headroom_bytes: int = DEFAULT_PEAK_HEADROOM_BYTES,
    backup_budget_bytes: int = DEFAULT_BACKUP_BUDGET_BYTES,
    retention_count: int = 1,
    disk_usage_probe: DiskUsageProbe = shutil.disk_usage,
) -> BackupResult:
    """Take a transactionally coherent SQLite online backup and verify it."""

    source = Path(source_db_path).resolve()
    destination_dir = Path(backup_directory).resolve()
    if not source.is_file():
        raise BackupError(f"Source database does not exist: {source}")
    destination_dir.mkdir(parents=True, exist_ok=True)
    if not destination_dir.is_dir():
        raise BackupError(f"Backup destination is not a directory: {destination_dir}")

    normalized_label = "".join(character for character in label if character.isalnum() or character in "-_")
    normalized_label = normalized_label.strip("-_") or "state"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    final_path = destination_dir / f"{normalized_label}.{timestamp}.db"
    manifest_path = destination_dir / f"{final_path.name}.manifest.json"
    if final_path == source:
        raise BackupError("Backup destination must differ from the source database")

    wal_path = Path(f"{source}-wal")
    estimated_image_bytes = source.stat().st_size + (
        wal_path.stat().st_size if wal_path.is_file() else 0
    )
    # The SQLite online-backup image and its temporary predecessor never need
    # to coexist after the atomic rename, but reserve a bounded metadata/page
    # margin for growth during the online copy.
    estimated_operation_bytes = estimated_image_bytes + max(
        16_000_000,
        estimated_image_bytes // 20,
    )
    if backup_budget_bytes > 0 and estimated_image_bytes > backup_budget_bytes:
        raise BackupError(
            f"source image ({estimated_image_bytes} bytes) exceeds backup budget "
            f"({backup_budget_bytes} bytes)"
        )
    _preflight_peak_space(
        destination_dir,
        operation_bytes=estimated_operation_bytes,
        required_headroom_bytes=required_headroom_bytes,
        disk_usage_probe=disk_usage_probe,
        operation="verified backup",
    )

    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{normalized_label}.",
        suffix=".db.tmp",
        dir=destination_dir,
    )
    os.close(descriptor)
    temp_path = Path(temp_name)
    try:
        with closing(_readonly_connection(source)) as source_connection:
            _integrity_check(source_connection)
            with closing(sqlite3.connect(temp_path, timeout=30)) as destination_connection:
                source_connection.backup(destination_connection, pages=1024)
                destination_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                destination_connection.commit()
                _integrity_check(destination_connection)

        _fsync_file(temp_path)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, final_path)
        _fsync_directory(destination_dir)

        integrity, user_version, application_id, counts = _database_metadata(
            final_path,
            immutable=True,
        )
        manifest = BackupManifest(
            format=MANIFEST_FORMAT,
            created_at=datetime.now(timezone.utc).isoformat(),
            source_name=source.name,
            backup_filename=final_path.name,
            sha256=_sha256(final_path),
            size_bytes=final_path.stat().st_size,
            sqlite_version=sqlite3.sqlite_version,
            schema_user_version=user_version,
            application_id=application_id,
            integrity_check=integrity,
            table_row_counts=counts,
        )
        _atomic_json_write(manifest_path, manifest.to_dict())
        verified = verify_backup(manifest_path)
        prune_verified_backups(
            destination_dir,
            retention_count=retention_count,
            max_total_bytes=backup_budget_bytes,
            source_name=source.name,
            protected_manifest=verified.manifest_path,
        )
        return verified
    except Exception:
        final_path.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
        raise
    finally:
        temp_path.unlink(missing_ok=True)


def _load_manifest(manifest_path: Path) -> BackupManifest:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError(f"Could not read backup manifest {manifest_path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise BackupError("Backup manifest root must be a JSON object")
    manifest = BackupManifest.from_dict(payload)
    if manifest.format != MANIFEST_FORMAT:
        raise BackupError(f"Unsupported backup manifest format: {manifest.format}")
    if Path(manifest.backup_filename).name != manifest.backup_filename:
        raise BackupError("Backup manifest contains an unsafe backup filename")
    return manifest


def verify_backup(manifest_path: str | os.PathLike[str]) -> BackupResult:
    """Verify manifest, checksum, file size, SQLite integrity and table counts."""

    manifest_candidate = Path(manifest_path).absolute()
    manifest_directory = _safe_backup_directory(manifest_candidate.parent)
    resolved_manifest = _safe_contained_backup_file(
        manifest_candidate,
        manifest_directory,
        description="Backup manifest",
    )
    manifest = _load_manifest(resolved_manifest)
    backup_path = _safe_contained_backup_file(
        manifest_directory / manifest.backup_filename,
        manifest_directory,
        description="Backup database",
    )
    actual_size = backup_path.stat().st_size
    if actual_size != manifest.size_bytes:
        raise BackupError(
            f"Backup size mismatch: manifest={manifest.size_bytes}, actual={actual_size}"
        )
    actual_hash = _sha256(backup_path)
    if actual_hash != manifest.sha256:
        raise BackupError(
            f"Backup checksum mismatch: manifest={manifest.sha256}, actual={actual_hash}"
        )
    integrity, user_version, application_id, counts = _database_metadata(
        backup_path,
        immutable=True,
    )
    if integrity != manifest.integrity_check:
        raise BackupError("Backup integrity result differs from the manifest")
    if user_version != manifest.schema_user_version:
        raise BackupError("Backup schema user_version differs from the manifest")
    if application_id != manifest.application_id:
        raise BackupError("Backup application_id differs from the manifest")
    if counts != dict(manifest.table_row_counts):
        raise BackupError("Backup table row counts differ from the manifest")
    return BackupResult(
        backup_path=backup_path,
        manifest_path=resolved_manifest,
        manifest=manifest,
    )


def prune_verified_backups(
    backup_directory: str | os.PathLike[str],
    *,
    retention_count: int = 1,
    max_total_bytes: int = DEFAULT_BACKUP_BUDGET_BYTES,
    source_name: str | None = None,
    protected_manifest: str | os.PathLike[str] | None = None,
) -> tuple[Path, ...]:
    """Remove only superseded, independently verified backup generations.

    Invalid or incomplete files are never treated as deletion authority.  At
    least one verified generation is retained even when it alone exceeds the
    byte budget, and the just-created protected generation is never removed.
    """

    directory_candidate = Path(backup_directory).absolute()
    if not directory_candidate.is_dir():
        return ()
    directory = _safe_backup_directory(directory_candidate)
    protected = (
        _safe_contained_backup_file(
            Path(protected_manifest),
            directory,
            description="Protected backup manifest",
        )
        if protected_manifest is not None
        else None
    )
    verified: list[BackupResult] = []
    for candidate in sorted(directory.glob("*.db.manifest.json")):
        try:
            _safe_contained_backup_file(
                candidate,
                directory,
                description="Backup manifest",
            )
            result = verify_backup(candidate)
            _safe_contained_backup_file(
                result.manifest_path,
                directory,
                description="Backup manifest",
            )
            _safe_contained_backup_file(
                result.backup_path,
                directory,
                description="Backup database",
            )
        except BackupError:
            continue
        if source_name is not None and result.manifest.source_name != source_name:
            continue
        verified.append(result)
    verified.sort(
        key=lambda item: (item.manifest.created_at, item.manifest_path.name),
        reverse=True,
    )
    if len(verified) <= 1:
        return ()

    keep_count = max(1, int(retention_count))
    kept: list[BackupResult] = []
    removal_candidates: list[BackupResult] = []
    for index, item in enumerate(verified):
        if index < keep_count or item.manifest_path == protected:
            kept.append(item)
        else:
            removal_candidates.append(item)

    total_bytes = sum(item.manifest.size_bytes for item in verified)
    projected_removed = {
        item.manifest_path: item for item in removal_candidates
    }
    projected_total = total_bytes - sum(
        item.manifest.size_bytes for item in projected_removed.values()
    )
    # Count retention is authoritative; byte retention can remove additional
    # old generations but never the newest/sole or protected generation.
    if max_total_bytes > 0 and projected_total > max_total_bytes:
        for item in reversed(kept[1:]):
            if item.manifest_path == protected:
                continue
            projected_removed[item.manifest_path] = item
            projected_total -= item.manifest.size_bytes
            if projected_total <= max_total_bytes:
                break
    removal_candidates = list(projected_removed.values())

    removed: list[Path] = []
    seen: set[Path] = set()
    # Oldest first minimizes the chance that a useful recent generation is
    # removed if the cleanup itself is interrupted.
    removed_generations = 0
    for item in sorted(
        removal_candidates,
        key=lambda candidate: (
            candidate.manifest.created_at,
            candidate.manifest_path.name,
        ),
    ):
        if item.manifest_path in seen or item.manifest_path == protected:
            continue
        if len(verified) - removed_generations <= 1:
            break
        # Revalidate immediately before each destructive operation.  This
        # catches a directory junction or file swapped after verification and
        # ensures pruning never unlinks a target outside the configured root.
        backup_path = _safe_contained_backup_file(
            item.backup_path,
            directory,
            description="Backup database",
        )
        manifest_path = _safe_contained_backup_file(
            item.manifest_path,
            directory,
            description="Backup manifest",
        )
        seen.add(item.manifest_path)
        backup_path.unlink()
        manifest_path = _safe_contained_backup_file(
            manifest_path,
            directory,
            description="Backup manifest",
        )
        manifest_path.unlink()
        removed_generations += 1
        removed.extend((item.backup_path, item.manifest_path))
    if removed:
        _fsync_directory(directory)
    return tuple(removed)


def _prove_target_quiesced(target: Path) -> None:
    try:
        with closing(sqlite3.connect(target, timeout=0.25, isolation_level=None)) as connection:
            connection.execute("PRAGMA busy_timeout=250")
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("BEGIN EXCLUSIVE")
            connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
            connection.execute("COMMIT")
    except sqlite3.Error as exc:
        raise BackupError(
            "Target database is not quiesced; stop writers before restore"
        ) from exc


def _validate_sqlite_sidecars(target: Path) -> None:
    """Reject a non-empty WAL that SQLite might otherwise silently ignore."""

    wal = Path(f"{target}-wal")
    if not wal.exists() or wal.stat().st_size == 0:
        return
    try:
        with wal.open("rb") as handle:
            header = handle.read(32)
    except OSError as exc:
        raise BackupError(f"Could not read SQLite WAL sidecar: {wal}") from exc
    if len(header) != 32 or int.from_bytes(header[:4], "big") not in {
        0x377F0682,
        0x377F0683,
    }:
        raise CorruptDatabaseError(f"SQLite WAL header is corrupt: {wal}")


def _replace_database_file(temp_path: Path, target: Path) -> None:
    """Replace a database while retaining a same-directory rollback on Windows.

    `os.replace(source, existing_target)` is atomic on POSIX, but Windows can
    reject that exact form for a recently checkpointed SQLite file even after
    every Python handle is closed.  The fallback first renames the proven,
    pre-backed-up target to a private rollback name, installs the new file, and
    restores the old file if installation fails.  There is never an
    unrecoverable overwrite.
    """

    def replace_with_bounded_retry(source: Path, destination: Path) -> None:
        last_error: PermissionError | None = None
        for attempt in range(6):
            try:
                os.replace(source, destination)
                return
            except PermissionError as exc:
                last_error = exc
                if attempt == 5:
                    break
                time.sleep(min(0.02 * (attempt + 1), 0.25))
        assert last_error is not None
        raise last_error

    try:
        replace_with_bounded_retry(temp_path, target)
        return
    except PermissionError:
        if os.name != "nt" or not target.exists():
            raise

    rollback = target.parent / f".{target.name}.{os.getpid()}.restore-rollback"
    if rollback.parent != target.parent or rollback.exists():
        raise BackupError("Could not allocate a safe restore rollback path")
    try:
        replace_with_bounded_retry(target, rollback)
    except PermissionError:
        # Some Windows scanners/open-file monitors do not grant delete sharing
        # even after SQLite itself proves an EXCLUSIVE transaction.  In that
        # case use SQLite's transactional online-backup API to replace the
        # contents of the already-quiesced target.  restore_verified_backup has
        # created and verified a pre-restore backup before reaching this path.
        with closing(
            _readonly_connection(temp_path, immutable=True)
        ) as source_connection:
            with closing(sqlite3.connect(target, timeout=30)) as destination_connection:
                destination_connection.execute("PRAGMA busy_timeout=30000")
                source_connection.backup(destination_connection, pages=1024)
                destination_connection.commit()
                _integrity_check(destination_connection)
        _fsync_file(target)
        temp_path.unlink(missing_ok=True)
        return
    installed = False
    try:
        replace_with_bounded_retry(temp_path, target)
        installed = True
    finally:
        if not installed:
            replace_with_bounded_retry(rollback, target)
        else:
            rollback.unlink(missing_ok=True)


def restore_verified_backup(
    manifest_path: str | os.PathLike[str],
    target_db_path: str | os.PathLike[str],
    *,
    replace: bool = False,
    confirm_quiesced: bool = False,
    quarantine_corrupt_target: bool = False,
    required_headroom_bytes: int = DEFAULT_PEAK_HEADROOM_BYTES,
    disk_usage_probe: DiskUsageProbe = shutil.disk_usage,
) -> RestoreResult:
    """Atomically restore a verified backup.

    Replacing an existing database requires both ``replace`` and the explicit
    ``confirm_quiesced`` acknowledgement.  A verified pre-restore backup is
    created before the swap, so the previous primary remains recoverable.
    """

    verified = verify_backup(manifest_path)
    target = Path(target_db_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target == verified.backup_path:
        raise BackupError("Restore target must differ from the backup file")
    if target.exists() and not replace:
        raise BackupError("Restore target exists; pass replace=True explicitly")
    if target.exists() and not confirm_quiesced:
        raise BackupError("Replacing a database requires confirm_quiesced=True")

    existing_image_bytes = 0
    if target.exists():
        existing_image_bytes = target.stat().st_size
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{target}{suffix}")
            if sidecar.is_file():
                existing_image_bytes += sidecar.stat().st_size
    # During replacement the verified pre-restore generation and the temporary
    # restored image coexist.  Prove that peak demand before touching the
    # target or its sidecars.
    _preflight_peak_space(
        target.parent,
        operation_bytes=(
            verified.manifest.size_bytes
            + existing_image_bytes
            + max(16_000_000, verified.manifest.size_bytes // 20)
        ),
        required_headroom_bytes=required_headroom_bytes,
        disk_usage_probe=disk_usage_probe,
        operation="verified restore",
    )

    pre_restore_backup: BackupResult | None = None
    quarantined_corrupt_files: tuple[Path, ...] = ()
    overwrite_corrupt_target = False
    if target.exists():
        try:
            _validate_sqlite_sidecars(target)
            _prove_target_quiesced(target)
            pre_restore_backup = create_verified_backup(
                target,
                target.parent / "pre_restore_backups",
                label=f"{target.stem}-pre-restore",
                required_headroom_bytes=(
                    required_headroom_bytes + verified.manifest.size_bytes
                ),
                disk_usage_probe=disk_usage_probe,
            )
        except BackupError as restore_error:
            if not quarantine_corrupt_target:
                raise
            # Do not use the corruption override for an otherwise readable
            # database (for example, a live writer lock).  It is reserved for
            # targets whose own SQLite metadata/integrity cannot be read.
            if not isinstance(restore_error, CorruptDatabaseError):
                try:
                    _database_metadata(target)
                except (BackupError, sqlite3.DatabaseError):
                    pass
                else:
                    raise
            quarantine = (
                target.parent
                / "corrupt_quarantine"
                / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            )
            quarantine.mkdir(parents=True, exist_ok=False)
            moved: list[Path] = []
            for source in (target, Path(f"{target}-wal"), Path(f"{target}-shm")):
                if not source.exists():
                    continue
                destination = quarantine / source.name
                last_error: PermissionError | None = None
                for attempt in range(12):
                    try:
                        os.replace(source, destination)
                        last_error = None
                        break
                    except PermissionError as exc:
                        last_error = exc
                        time.sleep(min(0.05 * (attempt + 1), 0.5))
                if last_error is not None:
                    # Windows may retain a non-delete-sharing handle briefly
                    # after SQLite discovers corruption.  Preserve an exact
                    # forensic copy, then overwrite only the already-proven
                    # corrupt primary after the replacement image is verified.
                    try:
                        shutil.copy2(source, destination)
                        _fsync_file(destination)
                    except OSError as exc:
                        raise BackupError(
                            f"Could not quarantine corrupt database file: {source}"
                        ) from exc
                    if source == target:
                        overwrite_corrupt_target = True
                    else:
                        try:
                            source.unlink()
                        except PermissionError:
                            pass
                moved.append(destination)
            if not moved or (target.exists() and not overwrite_corrupt_target):
                raise BackupError("Failed to quarantine corrupt database target")
            quarantined_corrupt_files = tuple(moved)
        # A successful verified pre-backup or explicit corruption quarantine
        # proves sidecars no longer contain state that can be silently reused.
        for suffix in ("-wal", "-shm"):
            Path(f"{target}{suffix}").unlink(missing_ok=True)

    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".restore.tmp",
        dir=target.parent,
    )
    os.close(descriptor)
    temp_path = Path(temp_name)
    try:
        with closing(
            _readonly_connection(verified.backup_path, immutable=True)
        ) as source_connection:
            with closing(sqlite3.connect(temp_path, timeout=30)) as destination_connection:
                source_connection.backup(destination_connection, pages=1024)
                destination_connection.commit()
                _integrity_check(destination_connection)
        _, user_version, application_id, counts = _database_metadata(
            temp_path,
            immutable=True,
        )
        if user_version != verified.manifest.schema_user_version:
            raise BackupError("Restored schema version differs from the verified backup")
        if application_id != verified.manifest.application_id:
            raise BackupError("Restored application ID differs from the verified backup")
        if counts != dict(verified.manifest.table_row_counts):
            raise BackupError("Restored table counts differ from the verified backup")
        _fsync_file(temp_path)
        os.chmod(temp_path, 0o600)
        if overwrite_corrupt_target:
            # Atomic rename is unavailable while Windows retains the corrupt
            # file handle.  The verified backup and forensic quarantine make a
            # bounded in-place replacement recoverable after interruption.
            with temp_path.open("rb") as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination, length=1024 * 1024)
                destination.flush()
                os.fsync(destination.fileno())
            temp_path.unlink(missing_ok=True)
        else:
            _replace_database_file(temp_path, target)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{target}{suffix}")
            if sidecar.parent == target.parent:
                sidecar.unlink(missing_ok=True)
        _fsync_directory(target.parent)
        _database_metadata(target)
        return RestoreResult(
            restored_path=target,
            source_backup_path=verified.backup_path,
            manifest_path=verified.manifest_path,
            pre_restore_backup_path=(
                pre_restore_backup.backup_path if pre_restore_backup is not None else None
            ),
            restored_at=datetime.now(timezone.utc).isoformat(),
            table_row_counts=counts,
            quarantined_corrupt_files=quarantined_corrupt_files,
        )
    finally:
        temp_path.unlink(missing_ok=True)


def run_restore_drill(
    manifest_path: str | os.PathLike[str],
    drill_directory: str | os.PathLike[str],
) -> RestoreResult:
    """Restore into an isolated path and re-verify all database invariants."""

    directory = Path(drill_directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"restore-drill-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}.db"
    return restore_verified_backup(manifest_path, target)
