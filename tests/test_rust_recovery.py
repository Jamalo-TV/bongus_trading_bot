from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bongus.engine.database_backup import BackupError
from bongus.engine.rust_recovery import (
    copy_rust_recovery_generation,
    request_rust_recovery_generation,
    verify_rust_recovery_generation,
)
from tests.rust_recovery_support import (
    FakeRustRecoveryHarness,
    write_fake_rust_recovery_generation,
)


def test_capture_and_copy_require_both_independent_and_rust_verification(
    tmp_path: Path,
) -> None:
    harness = FakeRustRecoveryHarness(tmp_path / "rust")

    captured = request_rust_recovery_generation(
        harness.binary,
        harness.socket,
        harness.generations,
        runner=harness.runner,
    )
    (tmp_path / "backup").mkdir()
    copied = copy_rust_recovery_generation(
        captured,
        tmp_path / "backup" / "rust-recovery",
        execution_binary=harness.binary,
        runner=harness.runner,
    )

    assert copied.manifest_sha256 == captured.manifest_sha256
    assert set(copied.members) == {
        "execution_state",
        "intent_journal",
        "telemetry_journal",
        "telemetry_ack_cursor",
        "private_cursor_spot",
        "private_cursor_futures",
    }
    assert sum("--create-recovery-generation" in command for command in harness.observed_commands) == 1
    assert sum("--verify-recovery-generation" in command for command in harness.observed_commands) == 2


def test_python_verifier_rejects_unknown_fields_and_member_tampering(
    tmp_path: Path,
) -> None:
    manifest = write_fake_rust_recovery_generation(tmp_path / "generations")
    verified = verify_rust_recovery_generation(manifest)
    member = verified.members["execution_state"].path
    member.write_bytes(member.read_bytes() + b"tamper")
    with pytest.raises(BackupError, match="size mismatch"):
        verify_rust_recovery_generation(manifest)

    manifest = write_fake_rust_recovery_generation(tmp_path / "generations")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["unexpected"] = True
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BackupError, match="exact schema"):
        verify_rust_recovery_generation(manifest)


def test_capture_rejects_a_valid_generation_outside_the_expected_root(
    tmp_path: Path,
) -> None:
    harness = FakeRustRecoveryHarness(tmp_path / "rust")
    escaped_manifest = write_fake_rust_recovery_generation(tmp_path / "escaped")

    def escaped_runner(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        payload = json.loads(
            harness.runner([str(harness.binary), "--verify-recovery-generation", str(escaped_manifest)]).stdout
        )
        payload["pause_ms"] = 1
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(payload),
            stderr="",
        )

    with pytest.raises(BackupError, match="escaped its expected root"):
        request_rust_recovery_generation(
            harness.binary,
            harness.socket,
            harness.generations,
            runner=escaped_runner,
        )


def test_copy_failure_never_leaves_a_consumable_partial_generation(
    tmp_path: Path,
) -> None:
    harness = FakeRustRecoveryHarness(tmp_path / "rust")
    captured = request_rust_recovery_generation(
        harness.binary,
        harness.socket,
        harness.generations,
        runner=harness.runner,
    )

    def failed_verifier(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 2, stdout="", stderr="injected failure")

    (tmp_path / "backup").mkdir()
    destination = tmp_path / "backup" / "rust-recovery"
    with pytest.raises(BackupError, match="exited 2"):
        copy_rust_recovery_generation(
            captured,
            destination,
            execution_binary=harness.binary,
            runner=failed_verifier,
        )
    assert not destination.exists()
