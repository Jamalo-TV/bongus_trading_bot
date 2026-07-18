"""Assemble immutable promotion evidence without converting tests into runtime proof."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ref(kind: str, path: Path) -> dict[str, str]:
    resolved = path.resolve()
    return {"kind": kind, "uri": str(resolved), "sha256": _sha256(resolved)}


def _verified_bundle_refs(
    bundle: dict[str, Any], required_kinds: set[str]
) -> tuple[list[dict[str, str]], bool]:
    raw_refs = bundle.get("evidence_refs")
    raw_refs = raw_refs if isinstance(raw_refs, list) else []
    verified_refs: list[dict[str, str]] = []
    refs_valid = True
    for raw_ref in raw_refs:
        if not isinstance(raw_ref, dict):
            refs_valid = False
            continue
        kind = raw_ref.get("kind")
        uri = raw_ref.get("uri")
        digest = raw_ref.get("sha256")
        if not all(
            isinstance(value, str) and value for value in (kind, uri, digest)
        ):
            refs_valid = False
            continue
        artifact_path = Path(str(uri))
        if not artifact_path.is_file() or _sha256(artifact_path) != digest:
            refs_valid = False
            continue
        verified_refs.append(
            {
                "kind": str(kind),
                "uri": str(artifact_path.resolve()),
                "sha256": str(digest),
            }
        )
    observed_kinds = {ref["kind"] for ref in verified_refs}
    return verified_refs, refs_valid and observed_kinds == required_kinds


def build_manifest(
    *,
    fault_path: Path,
    account_path: Path,
    backup_path: Path,
    phase0_path: Path | None = None,
    soak_path: Path | None = None,
) -> dict[str, Any]:
    fault = _load(fault_path)
    account = _load(account_path)
    backup = _load(backup_path)

    fault_attested = (
        fault.get("passed") is True
        and int(fault.get("traces_completed") or 0)
        == int(fault.get("traces_requested") or -1)
    )
    account_attested = (
        account.get("evidence_kind") == "account_reconciliation"
        and account.get("environment") == "testnet"
        and account.get("collection_policy", {}).get("read_only") is True
        and account.get("machine_attestation", {}).get("attested") is True
    )
    backup_attested = (
        backup.get("evidence_kind") == "backup_restore"
        and backup.get("status") == "restore_drill_passed"
        and backup.get("machine_attestation", {}).get("attested") is True
        and backup.get("source_backup_sha256") == backup.get("restored_sha256")
    )
    account_metrics = account.get("gate_metrics") or {}
    metrics = {
        "duplicate_exchange_effects": fault.get("duplicate_exchange_effects"),
        "state_invariant_failures": fault.get("invariant_failures"),
        "randomized_state_machine_traces": fault.get("traces_completed"),
        "unclassified_open_orders_positions": account_metrics.get(
            "unclassified_open_orders_positions"
        ),
        "ready_under_mismatch": account_metrics.get("ready_under_mismatch"),
        "backup_restore_demonstrated": backup_attested,
    }
    gates: dict[str, Any] = {
        "phase_1_to_2": {
            "evidence_kind": "fault_campaign_and_exchange_reconciliation",
            "attested": fault_attested and account_attested and backup_attested,
            "attestation_components": {
                "fault_campaign": fault_attested,
                "testnet_account_readback": account_attested,
                "backup_restore_drill": backup_attested,
            },
            "evidence_refs": [
                _ref("randomized_fault_campaign", fault_path),
                _ref("account_reconciliation", account_path),
                _ref("backup_restore", backup_path),
            ],
            "metrics": metrics,
        }
    }
    if phase0_path is not None:
        phase0 = _load(phase0_path)
        phase0_refs, refs_valid = _verified_bundle_refs(
            phase0, {"clean_ci", "runtime_reconciliation", "causal_replay"}
        )
        phase0_attested = (
            phase0.get("evidence_kind") == "ci_and_runtime"
            and phase0.get("machine_attestation", {}).get("attested") is True
            and refs_valid
        )
        phase0_metrics = phase0.get("metrics")
        gates["phase_0_to_1"] = {
            "evidence_kind": "ci_and_runtime",
            "attested": phase0_attested,
            "attestation_components": {
                "artifact_hashes": refs_valid,
                "criteria_passed": phase0.get("machine_attestation", {}).get(
                    "criteria_passed"
                )
                is True,
            },
            "evidence_refs": phase0_refs,
            "metrics": phase0_metrics if isinstance(phase0_metrics, dict) else {},
        }
    if soak_path is not None:
        soak = _load(soak_path)
        required_kinds = {
            "unattended_soak",
            "fault_injection",
            "incident_log",
            "readiness_report",
        }
        verified_refs, refs_valid = _verified_bundle_refs(soak, required_kinds)
        soak_attested = (
            soak.get("evidence_kind") == "paper_testnet_soak"
            and soak.get("machine_attestation", {}).get("attested") is True
            and soak.get("journal", {}).get("chain_verified") is True
            and refs_valid
        )
        soak_metrics = soak.get("metrics")
        gates["phase_4_to_live_canary"] = {
            "evidence_kind": "paper_testnet_soak",
            "attested": soak_attested,
            "attestation_components": {
                "journal_chain": soak.get("journal", {}).get("chain_verified") is True,
                "artifact_hashes": refs_valid,
                "criteria_passed": soak.get("machine_attestation", {}).get(
                    "criteria_passed"
                )
                is True,
            },
            "evidence_refs": verified_refs,
            "metrics": soak_metrics if isinstance(soak_metrics, dict) else {},
        }

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "policy": {
            "unit_tests_are_promotion_evidence": False,
            "capital_increased": False,
            "live_entries_resumed": False,
        },
        "gates": gates,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fault-report",
        type=Path,
        default=ROOT / "verification_artifacts" / "phase1_fault_campaign.json",
    )
    parser.add_argument("--account-reconciliation", type=Path, required=True)
    parser.add_argument("--backup-restore", type=Path, required=True)
    parser.add_argument(
        "--phase0-evidence",
        type=Path,
        help="optional Phase 0 CI/runtime/causal-replay evidence bundle",
    )
    parser.add_argument(
        "--soak-evidence",
        type=Path,
        help="optional hash-chained paper/testnet soak bundle",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT
        / "verification_artifacts"
        / "masterplan_external_evidence.json",
    )
    args = parser.parse_args()
    manifest = build_manifest(
        fault_path=args.fault_report,
        account_path=args.account_reconciliation,
        backup_path=args.backup_restore,
        phase0_path=args.phase0_evidence,
        soak_path=args.soak_evidence,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(args.output)
    gate = manifest["gates"]["phase_1_to_2"]
    print(
        json.dumps(
            {
                "status": "assembled",
                "output": str(args.output.resolve()),
                "sha256": _sha256(args.output),
                "attested": gate["attested"],
                "metrics": gate["metrics"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
