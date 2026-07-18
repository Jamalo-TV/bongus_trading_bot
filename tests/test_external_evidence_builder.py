from __future__ import annotations

import json
from pathlib import Path

from scripts.build_masterplan_external_evidence import build_manifest


def _write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_phase1_manifest_is_attested_but_preserves_real_account_blocker(
    tmp_path: Path,
) -> None:
    fault = _write(
        tmp_path / "fault.json",
        {
            "passed": True,
            "traces_requested": 1_000_000,
            "traces_completed": 1_000_000,
            "duplicate_exchange_effects": 0,
            "invariant_failures": 0,
        },
    )
    account = _write(
        tmp_path / "account.json",
        {
            "evidence_kind": "account_reconciliation",
            "environment": "testnet",
            "collection_policy": {"read_only": True},
            "machine_attestation": {"attested": True},
            "gate_metrics": {
                "unclassified_open_orders_positions": 1,
                "ready_under_mismatch": False,
            },
        },
    )
    backup = _write(
        tmp_path / "backup.json",
        {
            "evidence_kind": "backup_restore",
            "status": "restore_drill_passed",
            "machine_attestation": {"attested": True},
            "source_backup_sha256": "a" * 64,
            "restored_sha256": "a" * 64,
        },
    )

    manifest = build_manifest(
        fault_path=fault,
        account_path=account,
        backup_path=backup,
    )
    gate = manifest["gates"]["phase_1_to_2"]

    assert gate["attested"] is True
    assert gate["metrics"]["duplicate_exchange_effects"] == 0
    assert gate["metrics"]["unclassified_open_orders_positions"] == 1
    assert {ref["kind"] for ref in gate["evidence_refs"]} == {
        "randomized_fault_campaign",
        "account_reconciliation",
        "backup_restore",
    }
    assert manifest["policy"]["capital_increased"] is False


def test_soak_bundle_is_attested_without_converting_missing_runtime_proof_to_success(
    tmp_path: Path,
) -> None:
    fault = _write(
        tmp_path / "fault.json",
        {
            "passed": True,
            "traces_requested": 1_000_000,
            "traces_completed": 1_000_000,
            "duplicate_exchange_effects": 0,
            "invariant_failures": 0,
        },
    )
    account = _write(
        tmp_path / "account.json",
        {
            "evidence_kind": "account_reconciliation",
            "environment": "testnet",
            "collection_policy": {"read_only": True},
            "machine_attestation": {"attested": True},
            "gate_metrics": {},
        },
    )
    backup = _write(
        tmp_path / "backup.json",
        {
            "evidence_kind": "backup_restore",
            "status": "restore_drill_passed",
            "machine_attestation": {"attested": True},
            "source_backup_sha256": "b" * 64,
            "restored_sha256": "b" * 64,
        },
    )
    refs = []
    import hashlib

    for kind in (
        "unattended_soak",
        "fault_injection",
        "incident_log",
        "readiness_report",
    ):
        artifact = _write(tmp_path / f"{kind}.json", {"kind": kind})
        refs.append(
            {
                "kind": kind,
                "uri": str(artifact),
                "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            }
        )
    soak = _write(
        tmp_path / "soak.json",
        {
            "evidence_kind": "paper_testnet_soak",
            "journal": {"chain_verified": True},
            "machine_attestation": {"attested": True, "criteria_passed": False},
            "evidence_refs": refs,
            "metrics": {
                "consecutive_unattended_days": 0.0,
                "decision_service_readiness_pct": 0.0,
                "critical_reconciliation_invariant_incidents": 1,
                "injected_gaps_detected_replayed_pct": None,
                "routine_auto_recovery_within_slo_pct": None,
                "unresolved_alerts": 1,
            },
        },
    )

    manifest = build_manifest(
        fault_path=fault,
        account_path=account,
        backup_path=backup,
        soak_path=soak,
    )
    gate = manifest["gates"]["phase_4_to_live_canary"]
    assert gate["attested"] is True
    assert gate["attestation_components"]["criteria_passed"] is False
    assert gate["metrics"]["consecutive_unattended_days"] == 0.0


def test_phase0_bundle_preserves_measured_zero_lineage(tmp_path: Path) -> None:
    import hashlib

    fault = _write(
        tmp_path / "fault.json",
        {
            "passed": True,
            "traces_requested": 1,
            "traces_completed": 1,
            "duplicate_exchange_effects": 0,
            "invariant_failures": 0,
        },
    )
    account = _write(
        tmp_path / "account.json",
        {
            "evidence_kind": "account_reconciliation",
            "environment": "testnet",
            "collection_policy": {"read_only": True},
            "machine_attestation": {"attested": True},
        },
    )
    backup = _write(
        tmp_path / "backup.json",
        {
            "evidence_kind": "backup_restore",
            "status": "restore_drill_passed",
            "machine_attestation": {"attested": True},
            "source_backup_sha256": "c" * 64,
            "restored_sha256": "c" * 64,
        },
    )
    refs = []
    for kind in ("clean_ci", "runtime_reconciliation", "causal_replay"):
        artifact = _write(tmp_path / f"phase0_{kind}.json", {"kind": kind})
        refs.append(
            {
                "kind": kind,
                "uri": str(artifact),
                "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            }
        )
    phase0 = _write(
        tmp_path / "phase0.json",
        {
            "evidence_kind": "ci_and_runtime",
            "machine_attestation": {"attested": True, "criteria_passed": False},
            "evidence_refs": refs,
            "metrics": {
                "clean_ci_passed": True,
                "decision_order_fill_lineage_pct": 0.0,
                "deterministic_causal_replay": True,
                "exchange_fill_funding_mapping_pct": 0.0,
                "daily_unexplained_max_usd": None,
                "within_exchange_precision": False,
            },
        },
    )
    manifest = build_manifest(
        fault_path=fault,
        account_path=account,
        backup_path=backup,
        phase0_path=phase0,
    )
    gate = manifest["gates"]["phase_0_to_1"]
    assert gate["attested"] is True
    assert gate["metrics"]["decision_order_fill_lineage_pct"] == 0.0
    assert gate["metrics"]["daily_unexplained_max_usd"] is None
