from __future__ import annotations

import hashlib
import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict
from uuid import uuid4

_MEMBER_PATHS = {
    "execution_state": ("members/execution_state.jsonl", "execution_state.jsonl"),
    "intent_journal": ("members/execution_intents.jsonl", "execution_intents.jsonl"),
    "telemetry_journal": ("members/execution_telemetry.jsonl", "execution_telemetry.jsonl"),
    "telemetry_ack_cursor": (
        "members/execution_telemetry.jsonl.cursor.a",
        "execution_telemetry.jsonl.cursor.a",
    ),
    "private_cursor_spot": (
        "members/private_stream_cursors/spot.jsonl",
        "private_stream_cursors/spot.jsonl",
    ),
    "private_cursor_futures": (
        "members/private_stream_cursors/futures.jsonl",
        "private_stream_cursors/futures.jsonl",
    ),
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _response(manifest: Path, *, pause_ms: int) -> dict[str, object]:
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    return {
        "schema_version": 1,
        "complete": True,
        "generation_id": payload["generation_id"],
        "manifest_path": str(manifest.resolve()),
        "manifest_sha256": _sha256(manifest),
        "manifest_size_bytes": manifest.stat().st_size,
        "pause_ms": pause_ms,
    }


def write_fake_rust_recovery_generation(generations_root: Path) -> Path:
    generations_root.mkdir(parents=True, exist_ok=True)
    generation_id = f"test-{uuid4().hex}"
    generation = generations_root / f"generation-{generation_id}"
    generation.mkdir()
    members: dict[str, dict[str, object]] = {}
    for key, (filename, restore_path) in _MEMBER_PATHS.items():
        member_path = generation.joinpath(*Path(filename).parts)
        member_path.parent.mkdir(parents=True, exist_ok=True)
        if key == "execution_state":
            content = b'{"schema_version":1,"terminal_sequence_watermark":0}\n'
        else:
            content = b""
        member_path.write_bytes(content)
        members[key] = {
            "filename": filename,
            "restore_relative_path": restore_path,
            "sha256": _sha256(member_path),
            "size_bytes": len(content),
        }
    manifest = generation / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "evidence_kind": "bongus_rust_recovery_generation",
                "complete": True,
                "restore_policy": "empty_runtime_then_signed_reconciliation",
                "generation_id": generation_id,
                "barrier_request_id": f"backup-{uuid4().hex}",
                "created_at_ms": max(1, time.time_ns() // 1_000_000),
                "terminal_sequence_watermark": 0,
                "intent_producer_high_watermarks": {},
                "telemetry": {
                    "published_high_water_sequence": 0,
                    "acknowledged_high_water_sequence": 0,
                    "cursor_generation": 0,
                },
                "private_stream_cursors": {
                    "spot": {"through_ms": None},
                    "futures": {"through_ms": None},
                },
                "members": members,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest


@dataclass(slots=True)
class FakeRustRecoveryHarness:
    root: Path
    observed_commands: list[list[str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.binary.write_bytes(b"test execution binary\n")
        self.generations.mkdir(parents=True, exist_ok=True)

    @property
    def binary(self) -> Path:
        return self.root / "execution_engine"

    @property
    def socket(self) -> Path:
        return self.root / "recovery-control.sock"

    @property
    def generations(self) -> Path:
        return self.root / "recovery_generations"

    def runner(
        self,
        command: list[str],
        **_kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        self.observed_commands.append(command)
        if "--create-recovery-generation" in command:
            manifest = write_fake_rust_recovery_generation(self.generations)
            payload = _response(manifest, pause_ms=1)
        elif "--verify-recovery-generation" in command:
            manifest = Path(command[-1])
            payload = _response(manifest, pause_ms=0)
        else:
            return subprocess.CompletedProcess(command, 2, stdout="", stderr="unexpected command")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(payload),
            stderr="",
        )


class RustCreateKwargs(TypedDict):
    rust_execution_binary: Path
    rust_recovery_control_socket: Path
    rust_recovery_generations_directory: Path
    rust_command_runner: Any


def rust_create_kwargs(harness: FakeRustRecoveryHarness) -> RustCreateKwargs:
    return {
        "rust_execution_binary": harness.binary,
        "rust_recovery_control_socket": harness.socket,
        "rust_recovery_generations_directory": harness.generations,
        "rust_command_runner": harness.runner,
    }
