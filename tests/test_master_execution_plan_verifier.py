from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts import verify_master_execution_plan as verifier

ROOT = Path(__file__).resolve().parents[1]


def _complete_metrics() -> dict[str, dict]:
    return {
        "signed_testnet": {
            "authorized": True,
            "withdrawals_disabled": True,
            "current_account_snapshot": True,
            "passed_scenarios": [
                "zero_fill",
                "equal_partial",
                "unilateral_fill",
                "cancel_fill_race",
                "503_unknown",
                "429_418",
                "lifecycle_process_death",
                "dynamic_symbol_reconnect",
                "clock_skew",
            ],
            "orphan_orders": 0,
            "duplicate_positions": 0,
        },
        "safety_window": {
            "consecutive_unattended_days": 7,
            "consecutive_reconciled_utc_closes": 30,
            "unresolved_lifecycle_events": 0,
            "unexplained_restarts": 0,
            "alpha_changes": 0,
            "mainnet_public_paper_days": 7,
            "fault_terminal_unknown_states": 0,
            "untracked_orders_after_deadlines": 0,
            "storage_full_and_fsync_campaign_passed": True,
            "callback_rest_stall_campaign_passed": True,
            "independent_stream_failure_campaign_passed": True,
            "restart_full_replay_campaign_passed": True,
        },
        "operations": {
            "backup_rpo_minutes": 15,
            "restore_rto_minutes": 60,
            "blank_host_restore": True,
            "encrypted_offsite_backup": True,
            "independent_heartbeat_paging": True,
            "chrony_entry_block_test": True,
            "linux_systemd_verify": True,
            "free_disk_fraction": 0.31,
            "trading_vps_soak_days": 7,
            "total_rss_p99_fraction": 0.69,
            "cpu_p95_fraction": 0.59,
            "oom_or_restart_count": 0,
            "sqlite_write_latency_stable": True,
            "research_isolated_host_or_disk_quota": True,
            "research_daily_hashed_offsite_upload": True,
            "monthly_restore_drill": True,
            "quarterly_blank_host_recovery": True,
        },
        "region_probe": {
            "duration_hours": 48,
            "regions": ["germany", "france"],
            "selected_by_worst_venue_p99": True,
            "metric_families": [
                "rest_rtt",
                "ws_event_age",
                "p50_p95_p99",
                "jitter_packet_loss",
                "reconnect_gap_recovery",
            ],
            "artifact_sha256": "a" * 64,
        },
        "research_forward": {
            "collector_qa_days": 14,
            "complete_utc_days": 90,
            "decision_anchor_coverage": 0.99,
            "fresh_anchor_fraction": 0.99,
            "funding_reconciliation_fraction": 1.0,
            "future_data_joins": 0,
            "conflicting_duplicate_event_ids": 0,
            "report_hash_reproduced": True,
            "dataset_manifest_sha256": "b" * 64,
            "report_sha256": "c" * 64,
            "storage_sizing_pilot_hours": 48,
            "sealed_final_days": 30,
            "sealed_final_untouched": True,
            "deterministic_daily_weekly_bootstrap": True,
            "simple_annualized_estimate": 0.04,
            "one_sided_95_lcb": -0.01,
            "max_drawdown": 0.08,
            "verdict": "failed",
        },
    }


def _complete_evidence(root: Path) -> dict:
    artifacts: dict[str, dict[str, str]] = {}
    for evidence_kind, metrics in _complete_metrics().items():
        artifact_path = root / "evidence" / f"{evidence_kind}.json"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "evidence_kind": evidence_kind,
                    "metrics": metrics,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        artifacts[evidence_kind] = {
            "path": artifact_path.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
        }
    return {
        "schema_version": 2,
        "policy": {
            "live_entries_resumed": False,
            "local_tests_are_promotion_evidence": False,
        },
        "artifacts": artifacts,
    }


def _update_artifact(
    root: Path,
    evidence: dict,
    evidence_kind: str,
    **updates: object,
) -> None:
    reference = evidence["artifacts"][evidence_kind]
    artifact_path = root / reference["path"]
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    payload["metrics"].update(updates)
    artifact_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    reference["sha256"] = hashlib.sha256(artifact_path.read_bytes()).hexdigest()


def test_external_evidence_requires_real_elapsed_and_credentialed_gates() -> None:
    checks = verifier.evidence_checks(
        {
            "signed_testnet": {"passed_scenarios": []},
            "safety_window": {},
            "operations": {},
            "region_probe": {},
            "research_forward": {},
        }
    )

    assert checks
    assert {check.status for check in checks} == {verifier.BLOCKED}


def test_complete_hash_bound_external_evidence_passes_without_authorizing_live(
    tmp_path: Path,
) -> None:
    checks = verifier.evidence_checks(
        _complete_evidence(tmp_path),
        evidence_root=tmp_path,
    )

    assert checks
    assert all(check.status == verifier.PASS for check in checks)


def test_shape_only_metrics_cannot_manufacture_external_evidence() -> None:
    evidence = {
        "schema_version": 2,
        "policy": {
            "live_entries_resumed": False,
            "local_tests_are_promotion_evidence": False,
        },
        **_complete_metrics(),
    }

    checks = verifier.evidence_checks(evidence)

    assert checks[0].status == verifier.PASS
    assert {check.status for check in checks[1:]} == {verifier.BLOCKED}


def test_research_verdict_must_be_explicit_even_after_ninety_days(
    tmp_path: Path,
) -> None:
    evidence = _complete_evidence(tmp_path)
    _update_artifact(tmp_path, evidence, "research_forward", verdict="")

    checks = verifier.evidence_checks(evidence, evidence_root=tmp_path)
    research = next(check for check in checks if check.check_id == "evidence.preregistered_research_forward_verdict")
    assert research.status == verifier.BLOCKED


def test_viable_research_verdict_requires_every_preregistered_robustness_gate(
    tmp_path: Path,
) -> None:
    evidence = _complete_evidence(tmp_path)
    _update_artifact(
        tmp_path,
        evidence,
        "research_forward",
        verdict="viable",
        simple_annualized_estimate=0.08,
        one_sided_95_lcb=0.06,
        max_drawdown=0.09,
    )

    checks = verifier.evidence_checks(evidence, evidence_root=tmp_path)
    result = next(check for check in checks if check.check_id == "evidence.preregistered_research_forward_verdict")
    assert result.status == verifier.BLOCKED

    _update_artifact(
        tmp_path,
        evidence,
        "research_forward",
        positive_vs_no_trade=True,
        positive_vs_binance_only=True,
        positive_leave_one_symbol_out=True,
        positive_leave_one_month_out=True,
        top_five_profit_contribution=0.29,
        funding_minus_cost_positive_without_basis=True,
        minimum_depth_multiple=5,
        primary_2x_survives_without_liquidation=True,
        all_preregistered_stresses_present=True,
    )
    checks = verifier.evidence_checks(evidence, evidence_root=tmp_path)
    result = next(check for check in checks if check.check_id == "evidence.preregistered_research_forward_verdict")
    assert result.status == verifier.PASS


def test_research_verdict_and_hashes_must_match_the_recorded_economics(
    tmp_path: Path,
) -> None:
    evidence = _complete_evidence(tmp_path)
    _update_artifact(
        tmp_path,
        evidence,
        "research_forward",
        one_sided_95_lcb=0.01,
        simple_annualized_estimate=0.06,
        report_sha256="not-a-hash",
    )

    checks = verifier.evidence_checks(evidence, evidence_root=tmp_path)
    result = next(check for check in checks if check.check_id == "evidence.preregistered_research_forward_verdict")
    assert result.status == verifier.BLOCKED


def test_external_artifact_hash_mismatch_blocks_its_gate(tmp_path: Path) -> None:
    evidence = _complete_evidence(tmp_path)
    artifact = tmp_path / evidence["artifacts"]["operations"]["path"]
    artifact.write_text("{}", encoding="utf-8")

    checks = verifier.evidence_checks(evidence, evidence_root=tmp_path)
    result = next(check for check in checks if check.check_id == "evidence.operational_restore_clock_and_monitoring")

    assert result.status == verifier.BLOCKED
    assert result.observed["artifact"]["error"] == "artifact sha256 mismatch"


def test_external_artifact_parent_traversal_is_rejected(tmp_path: Path) -> None:
    evidence = _complete_evidence(tmp_path)
    evidence["artifacts"]["signed_testnet"]["path"] = "../signed.json"

    checks = verifier.evidence_checks(evidence, evidence_root=tmp_path)
    result = next(check for check in checks if check.check_id == "evidence.signed_testnet_fault_campaign")

    assert result.status == verifier.BLOCKED
    assert "parent traversal" in result.observed["artifact"]["error"]


def test_local_fault_campaign_requires_one_million_clean_traces(tmp_path) -> None:
    evidence_dir = tmp_path / "verification_artifacts" / "evidence"
    evidence_dir.mkdir(parents=True)
    artifact = evidence_dir / "execution_fault_campaign_test.json"
    artifact.write_text(
        json.dumps(
            {
                "passed": True,
                "traces_requested": 1_000_000,
                "traces_completed": 1_000_000,
                "invariant_failures": 0,
                "duplicate_exchange_effects": 0,
                "first_failure": "",
            }
        ),
        encoding="utf-8",
    )

    assert verifier._execution_fault_campaign_check(tmp_path).status == verifier.PASS

    artifact.write_text(
        json.dumps(
            {
                "passed": False,
                "traces_requested": 1_000_000,
                "traces_completed": 999_999,
                "invariant_failures": 1,
                "duplicate_exchange_effects": 0,
                "first_failure": "unsafe terminal state",
            }
        ),
        encoding="utf-8",
    )
    assert verifier._execution_fault_campaign_check(tmp_path).status == verifier.FAIL


def test_baseline_identity_artifact_is_self_consistent() -> None:
    assert verifier._baseline_identity_check(ROOT).status == verifier.PASS


def test_rust_lifecycle_gate_requires_retained_lineage_and_checked_persistence() -> None:
    safe_source = """
fn emit_cycle_order_update() {
    self.store_chase_state("terminal lifecycle durable before telemetry publication");
    self.dash_tx.send(vec);
}
async fn handle_ws_event() {
    self.persist_execution_state("order update and cumulative fill progress");
    self.dash_tx.send(encoded);
}
fn remove_chase_state() {
    if !self.persist_execution_state(context) { self.latch_symbol_persistence_failure(); }
}
struct Snapshot {
    terminal_tombstones: Vec<u8>,
    terminal_sequence_watermark: u64,
    reconciliation_status: String,
    retention_deadline_ms: i64,
    symbol_persistence_latches: Vec<String>,
}
fn latch_symbol_persistence_failure() {}
fn is_symbol_persistence_latched() {}
"""

    assert all(verifier._rust_lifecycle_observation(safe_source).values())

    unsafe_source = safe_source.replace(
        "if !self.persist_execution_state(context) { self.latch_symbol_persistence_failure(); }",
        "let _ = self.persist_execution_state(context);",
    ).replace("terminal_tombstones: Vec<u8>,", "")
    observed = verifier._rust_lifecycle_observation(unsafe_source)
    assert observed["order_update_state_before_publish"] is True
    assert observed["terminal_state_before_publish"] is True
    assert observed["durable_terminal_tombstones"] is False
    assert observed["chase_removal_checks_persistence"] is False


def test_economic_accounting_gate_requires_full_taxonomy_provenance_and_nav_equation() -> None:
    ledger = (ROOT / "bongus" / "engine" / "economic_ledger.py").read_text(encoding="utf-8")
    daily = (ROOT / "bongus" / "supervisor" / "daily_report.py").read_text(encoding="utf-8")
    evidence = (ROOT / "bongus" / "testing" / "daily_reconciliation_evidence.py").read_text(encoding="utf-8")

    observed = verifier._economic_accounting_observation(ledger, daily, evidence)
    assert observed["complete_event_taxonomy"] is True
    assert observed["complete_provenance_envelope"] is True
    assert observed["complete_daily_nav_equation"] is True
    assert observed["complete_daily_nav_statuses"] is True
    assert observed["exact_decimal_math"] is True
    assert observed["incomplete_envelope_blocks_finalization"] is True
    assert observed["internal_transfers_net_zero"] is True
    assert observed["unknown_is_explicit"] is True

    missing_cashflow = verifier._economic_accounting_observation(
        ledger.replace('STABLECOIN_CONVERSION = "STABLECOIN_CONVERSION"', ""),
        daily,
        evidence,
    )
    assert missing_cashflow["complete_event_taxonomy"] is False

    coerces_unknown = verifier._economic_accounting_observation(
        ledger,
        daily.replace("daily_nav_components_unknown", "missing_components_default_zero"),
        evidence,
    )
    assert coerces_unknown["unknown_is_explicit"] is False


def test_research_runtime_gate_executes_every_isolated_cli_and_boundary() -> None:
    observed = verifier._research_runtime_observation(ROOT)

    assert observed["all_boundary_controls"] is True
    assert observed["all_entrypoints_directly_executable"] is True
    assert all(result["returncode"] == 0 for result in observed["entrypoints"].values())
    assert all(result["usage"] is True for result in observed["entrypoints"].values())
