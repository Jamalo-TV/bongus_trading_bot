"""Create, verify, or clean-restore one coherent split-store backup set."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from bongus.engine.backup_set import (
    DEFAULT_BACKUP_TREE_BUDGET_BYTES,
    DEFAULT_SET_BUDGET_BYTES,
    VerifiedBackupSet,
    create_verified_backup_set,
    restore_backup_set_to_empty_directory,
    verify_backup_set,
)
from bongus.engine.database_backup import (
    DEFAULT_BACKUP_BUDGET_BYTES,
    DEFAULT_PEAK_HEADROOM_BYTES,
    BackupError,
)
from bongus.engine.rust_recovery import run_rust_recovery_offline_verifier


def _positive_integer(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return value


def _parser() -> argparse.ArgumentParser:
    data_root = Path(os.getenv("BONGUS_DATA_ROOT", str(Path(__file__).resolve().parents[1]))).resolve()
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="publish one complete verified set")
    create.add_argument("--data-root", type=Path, default=data_root)
    create.add_argument("--backup-directory", type=Path, default=data_root / "backups")
    create.add_argument(
        "--source-budget-bytes",
        type=_positive_integer,
        default=DEFAULT_BACKUP_BUDGET_BYTES,
    )
    create.add_argument(
        "--set-budget-bytes",
        type=_positive_integer,
        default=DEFAULT_SET_BUDGET_BYTES,
    )
    create.add_argument(
        "--required-headroom-bytes",
        type=_positive_integer,
        default=DEFAULT_PEAK_HEADROOM_BYTES,
    )
    create.add_argument(
        "--backup-tree-budget-bytes",
        type=_positive_integer,
        default=DEFAULT_BACKUP_TREE_BUDGET_BYTES,
    )
    create.add_argument("--retention-count", type=_positive_integer, default=1)
    create.add_argument("--rust-execution-binary", type=Path, required=True)
    create.add_argument("--rust-recovery-control-socket", type=Path, required=True)
    create.add_argument(
        "--rust-recovery-generations-directory",
        type=Path,
        required=True,
    )
    create.add_argument(
        "--rust-recovery-timeout-ms",
        type=_positive_integer,
        default=15_000,
    )

    verify = commands.add_parser("verify", help="deep-verify one set and every member")
    verify.add_argument("manifest", type=Path)
    verify.add_argument("--rust-execution-binary", type=Path, required=True)

    restore = commands.add_parser("restore-empty", help="restore all three databases into a new/empty directory")
    restore.add_argument("manifest", type=Path)
    restore.add_argument("--destination", type=Path, required=True)
    restore.add_argument("--rust-execution-binary", type=Path, required=True)
    return parser


def _payload(verified: VerifiedBackupSet) -> dict[str, object]:
    return {
        "status": "verified",
        "set_id": verified.set_id,
        "manifest_path": str(verified.manifest_path),
        "completed_at": verified.completed_at.isoformat(),
        "source_skew_seconds": verified.source_skew_seconds,
        "total_size_bytes": verified.total_size_bytes,
        "source_names": sorted(verified.backups),
        "rust_recovery_generation_id": verified.rust_recovery_generation.generation_id,
        "rust_recovery_manifest_sha256": verified.rust_recovery_generation.manifest_sha256,
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "create":
            payload = _payload(
                create_verified_backup_set(
                    args.data_root,
                    args.backup_directory,
                    rust_execution_binary=args.rust_execution_binary,
                    rust_recovery_control_socket=args.rust_recovery_control_socket,
                    rust_recovery_generations_directory=args.rust_recovery_generations_directory,
                    rust_recovery_timeout_ms=args.rust_recovery_timeout_ms,
                    source_budget_bytes=args.source_budget_bytes,
                    set_budget_bytes=args.set_budget_bytes,
                    required_headroom_bytes=args.required_headroom_bytes,
                    backup_tree_budget_bytes=args.backup_tree_budget_bytes,
                    retention_count=args.retention_count,
                )
            )
        elif args.command == "verify":
            verified = verify_backup_set(args.manifest)
            run_rust_recovery_offline_verifier(
                args.rust_execution_binary,
                verified.rust_recovery_generation.manifest_path,
            )
            payload = _payload(verified)
        else:
            restored = restore_backup_set_to_empty_directory(
                args.manifest,
                args.destination,
                rust_execution_binary=args.rust_execution_binary,
            )
            payload = {
                "status": "restored",
                "destination": str(args.destination.resolve()),
                "restored_paths": [str(path) for path in restored],
            }
    except (BackupError, OSError, ValueError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
