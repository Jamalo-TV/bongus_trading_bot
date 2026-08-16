from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

import bongus.engine.backup_set as backup_set_module
from bongus.engine.backup_set import create_verified_backup_set
from bongus.engine.split_state_store import SplitStateWriter
from scripts.upload_verified_offsite_backup import (
    OffsiteBackupError,
    upload_latest_verified_backup,
)
from tests.rust_recovery_support import FakeRustRecoveryHarness, rust_create_kwargs


def _prepared_data_root(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, str]]:
    data_root = tmp_path / "data"
    data_root.mkdir()
    backups = data_root / "backups"
    writer = SplitStateWriter(
        state_path=str(data_root / "state.db"),
        audit_path=str(data_root / "audit.db"),
        research_path=str(data_root / "research.db"),
    )
    writer.close()
    (data_root / "live_config.json").write_text('{"pause_new_entries":true}\n', encoding="utf-8")
    rust_harness = FakeRustRecoveryHarness(data_root / "test-rust-recovery")
    create_verified_backup_set(
        data_root,
        backups,
        **rust_create_kwargs(rust_harness),
    )
    password_file = tmp_path / "restic-password"
    password_file.write_text("test-only-secret\n", encoding="utf-8")
    password_file.chmod(0o600)
    restic_binary = (tmp_path / "restic").resolve()
    restic_binary.write_bytes(b"reviewed-test-restic-binary\n")
    environment = {
        "RESTIC_REPOSITORY": "s3:https://s3.example.test/bongus",
        "RESTIC_PASSWORD_FILE": str(password_file),
        "BONGUS_EXPECTED_RESTIC_REPOSITORY_ID": "f" * 64,
        "BONGUS_EXPECTED_RESTIC_BINARY_SHA256": hashlib.sha256(restic_binary.read_bytes()).hexdigest(),
        "BONGUS_EXPECTED_RESTIC_VERSION": "0.18.1",
    }
    return data_root, backups, restic_binary, environment


def test_verified_backup_upload_writes_hash_bound_remote_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root, backups, restic_binary, environment = _prepared_data_root(tmp_path)
    receipt = data_root / "offsite" / "latest.json"
    observed_commands: list[list[str]] = []

    def fake_runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed_commands.append(command)
        assert kwargs["check"] is False
        assert kwargs["env"] == environment
        if command[-1] == "version":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="restic 0.18.1 compiled with go1.24.4 on windows/amd64\n",
                stderr="",
            )
        if command[-2:] == ["cat", "config"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps({"id": "f" * 64, "version": 2}),
                stderr="",
            )
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"message_type": "summary", "snapshot_id": "e" * 64}),
            stderr="",
        )

    def forbidden_sqlite_parse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("credential-bearing uploader invoked SQLite verification")

    monkeypatch.setattr(
        backup_set_module,
        "verify_backup",
        forbidden_sqlite_parse,
    )

    payload = upload_latest_verified_backup(
        data_root=data_root,
        backup_directory=backups,
        receipt_path=receipt,
        restic_binary=str(restic_binary),
        environment=environment,
        runner=fake_runner,
    )

    backup_command = next(command for command in observed_commands if "backup" in command)
    assert backup_command[:7] == [
        str(restic_binary),
        "--no-cache",
        "backup",
        "--json",
        "--tag",
        "bongus-operational",
        "--",
    ]
    assert any(value.endswith("live_config.json") for value in backup_command)
    assert any(value.endswith("execution_state.jsonl") for value in backup_command)
    assert any(Path(value).as_posix().endswith("private_stream_cursors/spot.jsonl") for value in backup_command)
    assert any("backup-set." in value and value.endswith(".json") for value in backup_command)
    assert sum(command[-2:] == ["cat", "config"] for command in observed_commands) == 2
    assert payload["encrypted"] is True
    assert payload["offsite"] is True
    assert payload["snapshot_id"] == "e" * 64
    assert payload["repository_id_sha256"] == "f" * 64
    assert payload["repository_pin_verified"] is True
    assert payload["restic_binary_sha256"] == environment["BONGUS_EXPECTED_RESTIC_BINARY_SHA256"]
    assert payload["restic_version"] == "0.18.1"
    assert payload["mutable_rust_runtime_included"] is True
    assert payload["restart_requires_exchange_reconciliation"] is True
    assert payload["rust_recovery_generation"]["member_count"] == 6
    assert set(payload["source_backups"]) == {"state.db", "audit.db", "research.db"}
    assert json.loads(receipt.read_text(encoding="utf-8")) == payload


@pytest.mark.parametrize(
    "environment",
    [
        {"RESTIC_REPOSITORY": "/srv/not-offsite", "RESTIC_PASSWORD_FILE": "missing"},
        {
            "RESTIC_REPOSITORY": "local:/srv/not-offsite",
            "RESTIC_PASSWORD_FILE": "missing",
        },
        {
            "RESTIC_REPOSITORY": "s3:https://s3.example.test/bongus",
            "RESTIC_PASSWORD": "inline-secret",
            "RESTIC_PASSWORD_FILE": "missing",
        },
        {
            "RESTIC_REPOSITORY": "rest:http://127.0.0.1:8000/repo",
            "RESTIC_PASSWORD_FILE": "missing",
        },
        {
            "RESTIC_REPOSITORY": "rest:https://127.0.0.1:8000/repo",
            "RESTIC_PASSWORD_FILE": "missing",
        },
        {
            "RESTIC_REPOSITORY": "rest:https://foo.localhost/repo",
            "RESTIC_PASSWORD_FILE": "missing",
        },
        {
            "RESTIC_REPOSITORY": "s3:https://foo.localhost/bucket",
            "RESTIC_PASSWORD_FILE": "missing",
        },
        {
            "RESTIC_REPOSITORY": "sftp:user@127.0.0.1.nip.io:/repo",
            "RESTIC_PASSWORD_FILE": "missing",
        },
        {
            "RESTIC_REPOSITORY": "sftp:user@[::1]:/repo",
            "RESTIC_PASSWORD_FILE": "missing",
        },
        {
            "RESTIC_REPOSITORY": "swift:container:/repo",
            "OS_AUTH_URL": "http://127.0.0.1:8080/v3",
            "RESTIC_PASSWORD_FILE": "missing",
        },
    ],
)
def test_upload_rejects_local_repositories_and_inline_passwords(
    tmp_path: Path,
    environment: dict[str, str],
) -> None:
    data_root, backups, restic_binary, _valid = _prepared_data_root(tmp_path)

    with pytest.raises(OffsiteBackupError):
        upload_latest_verified_backup(
            data_root=data_root,
            backup_directory=backups,
            receipt_path=data_root / "offsite" / "latest.json",
            restic_binary=str(restic_binary),
            environment=environment,
        )


def test_failed_upload_never_advances_the_receipt(tmp_path: Path) -> None:
    data_root, backups, restic_binary, environment = _prepared_data_root(tmp_path)
    receipt = data_root / "offsite" / "latest.json"

    def failed_runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 3, stdout="", stderr="remote unavailable")

    with pytest.raises(OffsiteBackupError, match="exited 3"):
        upload_latest_verified_backup(
            data_root=data_root,
            backup_directory=backups,
            receipt_path=receipt,
            restic_binary=str(restic_binary),
            environment=environment,
            runner=failed_runner,
        )

    assert not receipt.exists()


def test_upload_rejects_changed_restic_binary_before_any_command(tmp_path: Path) -> None:
    data_root, backups, restic_binary, environment = _prepared_data_root(tmp_path)
    restic_binary.write_bytes(b"unreviewed replacement\n")

    def forbidden_runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("a hash-mismatched Restic binary was executed")

    with pytest.raises(OffsiteBackupError, match="hash pin"):
        upload_latest_verified_backup(
            data_root=data_root,
            backup_directory=backups,
            receipt_path=data_root / "offsite" / "latest.json",
            restic_binary=str(restic_binary),
            environment=environment,
            runner=forbidden_runner,
        )


def test_upload_rejects_restic_version_mismatch_before_repository_access(
    tmp_path: Path,
) -> None:
    data_root, backups, restic_binary, environment = _prepared_data_root(tmp_path)
    observed_commands: list[list[str]] = []

    def wrong_version_runner(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        observed_commands.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="restic 0.18.2 compiled with go1.24.5 on linux/amd64\n",
            stderr="",
        )

    with pytest.raises(OffsiteBackupError, match="version does not match"):
        upload_latest_verified_backup(
            data_root=data_root,
            backup_directory=backups,
            receipt_path=data_root / "offsite" / "latest.json",
            restic_binary=str(restic_binary),
            environment=environment,
            runner=wrong_version_runner,
        )
    assert observed_commands == [[str(restic_binary), "version"]]


def test_upload_requires_actual_repository_config_id_to_match_pin(
    tmp_path: Path,
) -> None:
    data_root, backups, restic_binary, environment = _prepared_data_root(tmp_path)
    receipt = data_root / "offsite" / "latest.json"

    def mismatched_runner(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        if command[-1] == "version":
            stdout = "restic 0.18.1 compiled with go1.24.4 on windows/amd64\n"
        elif command[-2:] == ["cat", "config"]:
            stdout = json.dumps({"id": "0" * 64, "version": 2})
        else:
            stdout = json.dumps({"message_type": "summary", "snapshot_id": "e" * 64})
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    with pytest.raises(OffsiteBackupError, match="does not match"):
        upload_latest_verified_backup(
            data_root=data_root,
            backup_directory=backups,
            receipt_path=receipt,
            restic_binary=str(restic_binary),
            environment=environment,
            runner=mismatched_runner,
        )
    assert not receipt.exists()


def test_upload_refuses_an_incomplete_split_store_backup_set(tmp_path: Path) -> None:
    data_root, backups, restic_binary, environment = _prepared_data_root(tmp_path)
    next(backups.glob("backup-set.*/audit.*.db")).unlink()

    def version_runner(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="restic 0.18.1 compiled with go1.24.4 on windows/amd64\n",
            stderr="",
        )

    with pytest.raises(OffsiteBackupError, match="complete verified"):
        upload_latest_verified_backup(
            data_root=data_root,
            backup_directory=backups,
            receipt_path=data_root / "offsite" / "latest.json",
            restic_binary=str(restic_binary),
            environment=environment,
            runner=version_runner,
        )
