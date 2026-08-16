from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts.maintain_offsite_repository import maintain_repository
from scripts.upload_verified_offsite_backup import OffsiteBackupError


def _environment(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    password = tmp_path / "password"
    password.write_text("test-only\n", encoding="utf-8")
    password.chmod(0o600)
    restic_binary = (tmp_path / "restic").resolve()
    restic_binary.write_bytes(b"reviewed-test-restic-binary\n")
    return restic_binary, {
        "RESTIC_REPOSITORY": "s3:https://s3.example.test/bongus",
        "RESTIC_PASSWORD_FILE": str(password),
        "BONGUS_EXPECTED_RESTIC_REPOSITORY_ID": "a" * 64,
        "BONGUS_EXPECTED_RESTIC_BINARY_SHA256": hashlib.sha256(restic_binary.read_bytes()).hexdigest(),
        "BONGUS_EXPECTED_RESTIC_VERSION": "0.18.1",
    }


def test_retention_is_pinned_bounded_and_prunes_without_local_cache(
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []
    restic_binary, environment = _environment(tmp_path)

    def runner(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[-1] == "version":
            output = "restic 0.18.1 compiled with go1.24.4 on linux/amd64\n"
        elif command[-2:] == ["cat", "config"]:
            output = json.dumps({"id": "a" * 64, "version": 2})
        else:
            output = "[]"
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    receipt = tmp_path / "offsite" / "retention-latest.json"
    payload = maintain_repository(
        receipt_path=receipt,
        restic_binary=str(restic_binary),
        environment=environment,
        runner=runner,
    )

    forget = next(command for command in commands if "forget" in command)
    assert forget[:3] == [str(restic_binary), "--no-cache", "forget"]
    assert "--keep-within" in forget and "24h" in forget
    assert "--keep-daily" in forget and "30" in forget
    assert "--keep-weekly" in forget and "12" in forget
    assert "--keep-monthly" in forget and "12" in forget
    assert "--group-by" in forget and "tags" in forget
    assert "--prune" in forget
    assert payload["prune_completed"] is True
    assert payload["maintenance_identity_separated"] is True
    assert payload["maximum_duration_seconds"] == 240.0
    assert payload["restic_binary_sha256"] == environment["BONGUS_EXPECTED_RESTIC_BINARY_SHA256"]
    assert payload["restic_version"] == "0.18.1"
    assert json.loads(receipt.read_text(encoding="utf-8")) == payload


def test_wrong_repository_pin_prevents_retention_mutation(tmp_path: Path) -> None:
    commands: list[list[str]] = []
    restic_binary, environment = _environment(tmp_path)

    def runner(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[-1] == "version":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="restic 0.18.1 compiled with go1.24.4 on linux/amd64\n",
                stderr="",
            )
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"id": "b" * 64, "version": 2}),
            stderr="",
        )

    receipt = tmp_path / "offsite" / "retention-latest.json"
    with pytest.raises(OffsiteBackupError, match="does not match"):
        maintain_repository(
            receipt_path=receipt,
            restic_binary=str(restic_binary),
            environment=environment,
            runner=runner,
        )
    assert all("forget" not in command for command in commands)
    assert not receipt.exists()
