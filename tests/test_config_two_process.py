from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import time
from typing import Any, cast

import msgpack

from bongus.core.config_manager import ConfigManager
from bongus.ipc.protocol import (
    build_config_sync_envelope,
    validate_ack,
)


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "execution_engine" / "Cargo.toml"


def _raw_config_command_hash(payload: dict[str, Any]) -> str:
    """Hash even a deliberately self-inconsistent config test envelope."""

    encoded = bytearray(b"bongus-config-sync-command-v3\n")
    fields = (
        ("schema_version", "int"),
        ("account_id", "string"),
        ("environment", "string"),
        ("strategy_id", "string"),
        ("cycle_id", "string"),
        ("config_version_hash", "string"),
        ("intent", "string"),
        ("intent_id", "string"),
        ("config_canonical_json", "string"),
    )
    for name, kind in fields:
        encoded.extend(f"{name}=".encode("ascii"))
        if kind == "int":
            encoded.extend(f"i{int(payload[name])}".encode("ascii"))
        else:
            value = str(payload[name]).encode("utf-8")
            encoded.extend(b"s")
            encoded.extend(str(len(value)).encode("ascii"))
            encoded.extend(b":")
            encoded.extend(value)
        encoded.extend(b"\n")
    return hashlib.sha256(encoded).hexdigest()


def _run_rust(envelope: dict[str, Any]) -> dict[str, Any]:
    packed = cast(bytes, msgpack.packb(envelope, use_bin_type=True))
    completed = subprocess.run(
        [
            "cargo",
            "run",
            "--quiet",
            "--locked",
            "--manifest-path",
            str(MANIFEST),
            "--",
            "--config-consensus-harness",
        ],
        cwd=ROOT,
        input=packed.hex() + "\n",
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    payload = json.loads(completed.stdout)
    assert isinstance(payload, dict)
    return payload


def _valid_envelope() -> tuple[dict[str, Any], dict[str, Any]]:
    # Exercise the exact checked-in operator document, including the durable
    # pause that must survive cross-language consensus.
    snapshot = ConfigManager(ROOT / "live_config.json").canonical_snapshot()
    now_ms = int(time.time() * 1_000)
    envelope = build_config_sync_envelope(
        {
            "intent": "CONFIG_SYNC",
            "intent_id": "two-process-config",
            "account_id": "account-a",
            "environment": "paper",
            "strategy_id": "funding-v2",
            "cycle_id": "two-process-cycle",
            "config_version_hash": snapshot.sha256,
            "config_canonical_json": snapshot.canonical_json,
        },
        producer_id="python-two-process-test",
        sequence=1,
        ttl_ms=60_000,
        created_at_ms=now_ms,
    )
    return envelope, dict(snapshot.values)


def _replace_snapshot(
    envelope: dict[str, Any],
    values: dict[str, Any],
    *,
    declared_hash: str | None = None,
) -> dict[str, Any]:
    canonical = json.dumps(
        values,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    modified = {
        **envelope,
        "config_canonical_json": canonical,
        "config_version_hash": declared_hash
        or hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }
    modified["command_hash"] = _raw_config_command_hash(modified)
    return modified


def test_real_python_rust_restart_stale_hash_and_invalid_config_campaign(
) -> None:
    envelope, values = _valid_envelope()

    first_process = _run_rust(envelope)
    assert first_process["before_entry_block"] == "config_consensus_unavailable"
    assert first_process["applied"] is True
    assert first_process["active_hash"] == envelope["config_version_hash"]
    # Exact consensus preserves the configured operator pause state; it must
    # neither bypass a pause nor manufacture one when entries are enabled.
    expected_same_hash_block = (
        "config_pause_new_entries" if values["pause_new_entries"] else ""
    )
    assert first_process["same_hash_entry_block"] == expected_same_hash_block
    assert (
        first_process["mismatched_hash_entry_block"]
        == "config_consensus_hash_mismatch"
    )
    assert validate_ack(dict(first_process["ack"])) == (
        "two-process-config",
        "TERMINAL",
    )

    # A new OS process starts without inherited consensus and must apply the
    # exact document again before it can even reach the operator pause check.
    restarted_process = _run_rust(envelope)
    assert restarted_process["before_entry_block"] == (
        "config_consensus_unavailable"
    )
    assert restarted_process["active_hash"] == first_process["active_hash"]

    hash_disagreement = _replace_snapshot(
        envelope,
        values,
        declared_hash="f" * 64,
    )
    disagreed = _run_rust(hash_disagreement)
    assert disagreed["applied"] is False
    assert disagreed["active_hash"] == ""
    assert dict(disagreed["ack"])["ack_status"] == "REJECTED"
    assert dict(disagreed["ack"])["reason"] == "config_hash_mismatch"

    invalid_values = dict(values)
    invalid_values["per_symbol_notional_cap_usd"] = 2_500.0
    invalid_values["max_gross_exposure_usd"] = 1_000.0
    invalid_cross_field = _replace_snapshot(envelope, invalid_values)
    invalid = _run_rust(invalid_cross_field)
    assert invalid["applied"] is False
    assert invalid["active_hash"] == ""
    assert dict(invalid["ack"])["ack_status"] == "REJECTED"
    assert dict(invalid["ack"])["reason"] == "inconsistent_risk_limits"
