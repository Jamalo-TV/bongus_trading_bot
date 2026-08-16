"""Command-line entry point for verified state database backups and restores."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

from bongus.engine.database_backup import (
    BackupError,
    DEFAULT_BACKUP_BUDGET_BYTES,
    DEFAULT_PEAK_HEADROOM_BYTES,
    create_verified_backup,
    restore_verified_backup,
    run_restore_drill,
    verify_backup,
)


PROJECT_ROOT = Path(__file__).resolve().parent


def _data_root() -> Path:
    return Path(os.getenv("BONGUS_DATA_ROOT", str(PROJECT_ROOT))).resolve()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create, verify, restore, or drill a checksummed SQLite state backup."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    backup = commands.add_parser("backup", help="Create and verify an online backup")
    backup.add_argument("--source", type=Path, default=_data_root() / "state.db")
    backup.add_argument("--destination", type=Path, default=_data_root() / "backups")
    backup.add_argument("--label", default="state")
    backup.add_argument(
        "--backup-budget-bytes",
        type=int,
        default=DEFAULT_BACKUP_BUDGET_BYTES,
        help=(
            "maximum source image size; defaults to 8 GB so the current "
            "operational database remains eligible"
        ),
    )
    backup.add_argument(
        "--required-headroom-bytes",
        type=int,
        default=DEFAULT_PEAK_HEADROOM_BYTES,
        help="free space that must remain beyond the estimated online-copy peak",
    )
    backup.add_argument(
        "--retention-count",
        type=int,
        default=1,
        help="number of newest verified local generations to retain",
    )

    verify = commands.add_parser("verify", help="Verify a backup manifest and database")
    verify.add_argument("manifest", type=Path)

    restore = commands.add_parser("restore", help="Atomically restore a verified backup")
    restore.add_argument("manifest", type=Path)
    restore.add_argument("--target", type=Path, default=PROJECT_ROOT / "state.db")
    restore.add_argument("--replace", action="store_true")
    restore.add_argument(
        "--confirm-quiesced",
        action="store_true",
        help="Confirm all bot/database writer processes are stopped",
    )
    restore.add_argument(
        "--quarantine-corrupt-target",
        action="store_true",
        help=(
            "Preserve a proven-corrupt primary/WAL in corrupt_quarantine before "
            "restoring; never overrides an active-writer lock"
        ),
    )

    drill = commands.add_parser("drill", help="Restore into an isolated drill directory")
    drill.add_argument("manifest", type=Path)
    drill.add_argument(
        "--directory",
        type=Path,
        default=_data_root() / "backup_restore_drills",
    )
    drill.add_argument(
        "--evidence-output",
        type=Path,
        help="Atomically persist a hash-addressed restore-drill evidence record",
    )
    return parser


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "backup":
            if args.backup_budget_bytes <= 0:
                raise BackupError("backup budget must be positive")
            if args.required_headroom_bytes < 0:
                raise BackupError("required backup headroom must be non-negative")
            if args.retention_count < 1:
                raise BackupError("backup retention count must be at least one")
            result = create_verified_backup(
                args.source,
                args.destination,
                label=args.label,
                backup_budget_bytes=args.backup_budget_bytes,
                required_headroom_bytes=args.required_headroom_bytes,
                retention_count=args.retention_count,
            )
            payload = {
                "status": "verified",
                "backup_path": str(result.backup_path),
                "manifest_path": str(result.manifest_path),
                "sha256": result.manifest.sha256,
                "size_bytes": result.manifest.size_bytes,
                "backup_budget_bytes": args.backup_budget_bytes,
                "required_headroom_bytes": args.required_headroom_bytes,
                "retention_count": args.retention_count,
                "table_row_counts": dict(result.manifest.table_row_counts),
            }
        elif args.command == "verify":
            result = verify_backup(args.manifest)
            payload = {
                "status": "verified",
                "backup_path": str(result.backup_path),
                "manifest_path": str(result.manifest_path),
                "sha256": result.manifest.sha256,
                "table_row_counts": dict(result.manifest.table_row_counts),
            }
        elif args.command == "restore":
            result = restore_verified_backup(
                args.manifest,
                args.target,
                replace=args.replace,
                confirm_quiesced=args.confirm_quiesced,
                quarantine_corrupt_target=args.quarantine_corrupt_target,
            )
            payload = {
                "status": "restored",
                "restored_path": str(result.restored_path),
                "source_backup_path": str(result.source_backup_path),
                "pre_restore_backup_path": (
                    str(result.pre_restore_backup_path)
                    if result.pre_restore_backup_path is not None
                    else None
                ),
                "table_row_counts": dict(result.table_row_counts),
                "quarantined_corrupt_files": [
                    str(path) for path in result.quarantined_corrupt_files
                ],
            }
        else:
            result = run_restore_drill(args.manifest, args.directory)
            payload = {
                "status": "restore_drill_passed",
                "restored_path": str(result.restored_path),
                "source_backup_path": str(result.source_backup_path),
                "table_row_counts": dict(result.table_row_counts),
            }
            if args.evidence_output is not None:
                manifest_path = Path(args.manifest).resolve()
                evidence = {
                    "schema_version": 1,
                    "evidence_kind": "backup_restore",
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "machine_attestation": {
                        "attested": True,
                        "basis": "checksum-verified backup restored into an isolated database and integrity-checked",
                    },
                    "manifest_path": str(manifest_path),
                    "manifest_sha256": _sha256(manifest_path),
                    "source_backup_path": str(result.source_backup_path),
                    "source_backup_sha256": _sha256(result.source_backup_path),
                    "restored_path": str(result.restored_path),
                    "restored_sha256": _sha256(result.restored_path),
                    "table_row_counts": dict(result.table_row_counts),
                    "status": "restore_drill_passed",
                }
                _write_json_atomic(args.evidence_output, evidence)
                payload["evidence_output"] = str(args.evidence_output.resolve())
                payload["evidence_sha256"] = _sha256(args.evidence_output.resolve())
    except BackupError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2

    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
