"""Apply the bounded encrypted-offsite snapshot retention policy."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.upload_verified_offsite_backup import (
    OffsiteBackupError,
    _read_repository_config_id,
    _restic_environment,
    _verify_restic_binary_identity,
    _write_receipt,
)

RETENTION_POLICY = {
    "keep_within": "24h",
    "keep_daily": 30,
    "keep_weekly": 12,
    "keep_monthly": 12,
}
MAXIMUM_MAINTENANCE_SECONDS = 240.0


def maintain_repository(
    *,
    receipt_path: Path,
    restic_binary: str,
    environment: dict[str, str],
    timeout_seconds: float = MAXIMUM_MAINTENANCE_SECONDS,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    if not 0.0 < timeout_seconds <= MAXIMUM_MAINTENANCE_SECONDS:
        raise OffsiteBackupError("Restic retention timeout must be within the four-minute RPO maintenance bound")
    (
        _repository,
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
    before_id = _read_repository_config_id(
        restic_binary=restic_binary,
        environment=environment,
        timeout_seconds=timeout_seconds,
        runner=runner,
    )
    if before_id != expected_repository_id:
        raise OffsiteBackupError("Restic repository config ID does not match the operator pin")
    command = [
        restic_binary,
        "--no-cache",
        "forget",
        "--json",
        "--tag",
        "bongus-operational",
        "--group-by",
        "tags",
        "--keep-within",
        str(RETENTION_POLICY["keep_within"]),
        "--keep-daily",
        str(RETENTION_POLICY["keep_daily"]),
        "--keep-weekly",
        str(RETENTION_POLICY["keep_weekly"]),
        "--keep-monthly",
        str(RETENTION_POLICY["keep_monthly"]),
        "--prune",
    ]
    try:
        result = runner(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OffsiteBackupError(f"Restic retention maintenance failed to execute: {exc}") from exc
    if result.returncode != 0:
        raise OffsiteBackupError(f"Restic retention maintenance exited {result.returncode}")
    after_id = _read_repository_config_id(
        restic_binary=restic_binary,
        environment=environment,
        timeout_seconds=timeout_seconds,
        runner=runner,
    )
    if after_id != before_id:
        raise OffsiteBackupError("Restic repository identity changed during retention maintenance")
    payload: dict[str, Any] = {
        "schema_version": 1,
        "evidence_kind": "encrypted_offsite_retention_receipt",
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "repository_id_sha256": before_id,
        "repository_backend": repository_backend,
        "repository_pin_verified": True,
        "restic_binary_sha256": restic_binary_sha256,
        "restic_version": restic_version,
        "policy": dict(RETENTION_POLICY),
        "stable_grouping": "tags",
        "maintenance_identity_separated": True,
        "maximum_duration_seconds": MAXIMUM_MAINTENANCE_SECONDS,
        "prune_completed": True,
    }
    _write_receipt(receipt_path, payload)
    return payload


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0.0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _parser() -> argparse.ArgumentParser:
    data_root = Path(os.getenv("BONGUS_DATA_ROOT", Path(__file__).resolve().parents[1]))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--receipt-path",
        type=Path,
        default=data_root / "offsite" / "maintenance" / "latest.json",
    )
    parser.add_argument("--restic-binary", default="/usr/bin/restic")
    parser.add_argument(
        "--timeout-seconds",
        type=_positive_float,
        default=MAXIMUM_MAINTENANCE_SECONDS,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload = maintain_repository(
            receipt_path=args.receipt_path,
            restic_binary=args.restic_binary,
            environment=dict(os.environ),
            timeout_seconds=args.timeout_seconds,
        )
    except (OffsiteBackupError, OSError, ValueError) as exc:
        print(f"offsite retention failed: {exc}", file=sys.stderr)
        return 2
    print(payload["completed_at"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
