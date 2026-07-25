"""Inventory, archive, and bundle Bongus operational diagnostics.

The Rust JSONL files are durable recovery journals, not disposable logs.  They
are copied into startup archives and support bundles, but are deliberately left
in place so an engine restart can still recover idempotency and order state.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import IO


ARCHIVE_RELATIVE_DIR = Path("scripts/logs/archive")
DEFAULT_STARTUP_ARCHIVE_COUNT = 10
_ARCHIVE_DIR_NAME = re.compile(
    r"^\d{8}T\d{6}(?:\.\d{6})?Z(?:-\d+)?$"
)

# Files that belong to the just-finished process session and can safely start
# fresh.  Legacy names are retained because deployments may still have them.
_MOVABLE_PATTERNS = (
    "scripts/logs/live_trader.log*",
    "scripts/logs/king_watchdog.log*",
    "execution_engine/rust_debug_log.txt",
    "ssh_log.txt",
    "wsl_ssh_log.txt",
    "runtime_heartbeat.json",
    "watchdog.out",
)

# These files contain durable execution/recovery information.  Snapshot them,
# but never remove them during log housekeeping.
_DURABLE_PATTERNS = (
    "execution_engine/execution_state.jsonl",
    "execution_engine/execution_intents.jsonl",
    "execution_engine/data/private_stream_cursors/*.jsonl",
    ".watchdog_state.json",
)

# The user requested this file in the bundle.  It is application source, not a
# log, so it must never be archived away or reset.
_REFERENCE_PATTERNS = ("bongus/monitoring/web_dashboard_logs.html",)

_EXPECTED_CURRENT_PATHS = (
    "scripts/logs/live_trader.log",
    "execution_engine/execution_state.jsonl",
    "execution_engine/execution_intents.jsonl",
    "execution_engine/rust_debug_log.txt",
    "ssh_log.txt",
    "wsl_ssh_log.txt",
    "runtime_heartbeat.json",
    "bongus/monitoring/web_dashboard_logs.html",
)


@dataclass(frozen=True)
class StartupArchiveResult:
    archive_dir: Path | None
    moved: tuple[str, ...]
    copied: tuple[str, ...]
    errors: tuple[str, ...]
    removed_archives: tuple[str, ...]


def _matching_files(project_root: Path, patterns: tuple[str, ...]) -> list[Path]:
    matches: dict[str, Path] = {}
    for pattern in patterns:
        for path in project_root.glob(pattern):
            if not path.is_file():
                continue
            relative = path.relative_to(project_root).as_posix()
            matches[relative] = path
    return [matches[key] for key in sorted(matches)]


def current_artifacts(project_root: Path) -> list[Path]:
    """Return all current files that belong in a diagnostic download."""

    project_root = project_root.resolve()
    return _matching_files(
        project_root,
        _MOVABLE_PATTERNS + _DURABLE_PATTERNS + _REFERENCE_PATTERNS,
    )


def _unique_archive_dir(archive_root: Path, now: datetime) -> Path:
    stem = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    candidate = archive_root / stem
    suffix = 1
    while candidate.exists():
        candidate = archive_root / f"{stem}-{suffix}"
        suffix += 1
    return candidate


def _prune_archives(archive_root: Path, retention_count: int) -> tuple[str, ...]:
    retention_count = max(1, retention_count)
    archive_root = archive_root.resolve()
    directories = sorted(
        (
            path
            for path in archive_root.iterdir()
            if path.is_dir() and _ARCHIVE_DIR_NAME.fullmatch(path.name)
        ),
        key=lambda path: path.name,
        reverse=True,
    )
    removed: list[str] = []
    for old_dir in directories[retention_count:]:
        # Only direct timestamped children of the configured archive root are
        # eligible for recursive removal.
        resolved = old_dir.resolve()
        if resolved.parent != archive_root:
            continue
        shutil.rmtree(resolved)
        removed.append(old_dir.name)
    return tuple(removed)


def archive_startup_artifacts(
    project_root: Path,
    *,
    retention_count: int = DEFAULT_STARTUP_ARCHIVE_COUNT,
    now: datetime | None = None,
) -> StartupArchiveResult:
    """Move disposable session logs and snapshot durable journals at startup."""

    project_root = project_root.resolve()
    created_at = now or datetime.now(timezone.utc)
    movable = _matching_files(project_root, _MOVABLE_PATTERNS)
    durable = _matching_files(project_root, _DURABLE_PATTERNS)
    if not movable and not durable:
        return StartupArchiveResult(None, (), (), (), ())

    archive_root = (project_root / ARCHIVE_RELATIVE_DIR).resolve()
    archive_root.mkdir(parents=True, exist_ok=True)
    archive_dir = _unique_archive_dir(
        archive_root,
        created_at,
    )
    archive_dir.mkdir(parents=True)

    moved: list[str] = []
    copied: list[str] = []
    errors: list[str] = []

    for source, mode in (
        *((path, "move") for path in movable),
        *((path, "copy") for path in durable),
    ):
        relative = source.relative_to(project_root)
        destination = archive_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            if mode == "move":
                shutil.move(str(source), str(destination))
                moved.append(relative.as_posix())
            else:
                shutil.copy2(source, destination)
                copied.append(relative.as_posix())
        except OSError as exc:
            errors.append(f"{relative.as_posix()}: {exc}")

    manifest = {
        "schema_version": 1,
        "created_at": created_at.astimezone(timezone.utc).isoformat(),
        "moved_session_files": moved,
        "copied_durable_recovery_files": copied,
        "errors": errors,
        "note": (
            "Durable JSONL recovery journals are snapshots only; their live "
            "copies remain in place for safe execution recovery."
        ),
    }
    (archive_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    try:
        removed = _prune_archives(archive_root, retention_count)
    except OSError as exc:
        errors.append(f"archive retention: {exc}")
        removed = ()

    return StartupArchiveResult(
        archive_dir=archive_dir,
        moved=tuple(moved),
        copied=tuple(copied),
        errors=tuple(errors),
        removed_archives=removed,
    )


def _archive_files(project_root: Path) -> list[Path]:
    archive_root = project_root / ARCHIVE_RELATIVE_DIR
    if not archive_root.is_dir():
        return []
    return sorted(path for path in archive_root.rglob("*") if path.is_file())


def write_support_bundle(
    destination: IO[bytes],
    project_root: Path,
    *,
    include_startup_archives: bool = True,
    now: datetime | None = None,
) -> dict[str, object]:
    """Write a ZIP support bundle and return its generated manifest."""

    project_root = project_root.resolve()
    included: list[str] = []
    skipped: list[str] = []
    generated_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    with zipfile.ZipFile(
        destination,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    ) as bundle:
        candidates: list[tuple[Path, str]] = []
        for path in current_artifacts(project_root):
            relative = path.relative_to(project_root).as_posix()
            candidates.append((path, f"current/{relative}"))

        if include_startup_archives:
            archive_root = project_root / ARCHIVE_RELATIVE_DIR
            for path in _archive_files(project_root):
                relative = path.relative_to(archive_root).as_posix()
                candidates.append((path, f"startup_archives/{relative}"))

        for path, archive_name in candidates:
            try:
                bundle.write(path, archive_name)
                included.append(archive_name)
            except OSError as exc:
                skipped.append(f"{archive_name}: {exc}")

        present_current = {
            path.relative_to(project_root).as_posix()
            for path in current_artifacts(project_root)
        }
        manifest: dict[str, object] = {
            "schema_version": 1,
            "generated_at": generated_at.isoformat(),
            "included_startup_archives": include_startup_archives,
            "included_files": included,
            "missing_expected_files": sorted(
                path for path in _EXPECTED_CURRENT_PATHS if path not in present_current
            ),
            "skipped_files": skipped,
            "classifications": {
                "session_logs": list(_MOVABLE_PATTERNS),
                "durable_recovery_state": list(_DURABLE_PATTERNS),
                "reference_source": list(_REFERENCE_PATTERNS),
            },
            "note": (
                "execution_state.jsonl, execution_intents.jsonl, private-stream "
                "cursors, and .watchdog_state.json are recovery state, not "
                "disposable logs."
            ),
        }
        bundle.writestr(
            "manifest.json",
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        )

    return manifest


def startup_archive_retention_from_env() -> int:
    raw = os.getenv(
        "BONGUS_STARTUP_ARCHIVE_COUNT",
        str(DEFAULT_STARTUP_ARCHIVE_COUNT),
    )
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_STARTUP_ARCHIVE_COUNT
