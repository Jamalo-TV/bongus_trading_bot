from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from bongus.engine.split_state_store import SplitStateWriter
from bongus.testing.soak_evidence import (
    SoakJournalError,
    append_observation,
    build_report_bundle,
    derive_metrics,
    verify_journal,
)
from scripts.collect_soak_evidence import (
    ROOT as PROJECT_ROOT,
    _build_parser,
    _read_state,
    _resolve_runtime_data_root,
    _validate_account_freshness,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _facts(*, ready: bool = True, unresolved: int = 0) -> dict:
    return {
        "unattended_eligible": ready and unresolved == 0,
        "decision_service": {"ready": ready},
        "reconciliation": {"critical_issue_ids": []},
        "unresolved_alerts": unresolved,
        "fault_injection": {"gaps_injected": 0, "gaps_detected_replayed": 0},
        "routine_recovery": {"attempted": 0, "within_slo": 0},
    }


def test_append_replay_and_hash_chain(tmp_path: Path) -> None:
    journal = tmp_path / "journal"
    first, _ = append_observation(
        journal, observed_at=NOW, environment="testnet", facts=_facts()
    )
    second, _ = append_observation(
        journal,
        observed_at=NOW + timedelta(minutes=5),
        environment="testnet",
        facts=_facts(),
    )
    records = verify_journal(journal)
    assert [record["sequence"] for record in records] == [1, 2]
    assert second["previous_record_sha256"] == first["record_sha256"]


def test_tamper_is_detected_before_another_append(tmp_path: Path) -> None:
    journal = tmp_path / "journal"
    _, path = append_observation(
        journal, observed_at=NOW, environment="paper", facts=_facts()
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["facts"]["decision_service"]["ready"] = False
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SoakJournalError, match="content hash mismatch"):
        verify_journal(journal)
    with pytest.raises(SoakJournalError, match="content hash mismatch"):
        append_observation(
            journal,
            observed_at=NOW + timedelta(minutes=1),
            environment="paper",
            facts=_facts(),
        )


def test_single_observation_cannot_mint_elapsed_days(tmp_path: Path) -> None:
    journal = tmp_path / "journal"
    append_observation(
        journal, observed_at=NOW, environment="testnet", facts=_facts()
    )
    metrics = derive_metrics(
        verify_journal(journal), max_observation_gap_seconds=900
    )
    assert metrics["consecutive_unattended_days"] == 0.0
    assert metrics["injected_gaps_detected_replayed_pct"] is None
    assert metrics["routine_auto_recovery_within_slo_pct"] is None


def test_failed_sample_or_gap_resets_current_unattended_suffix(tmp_path: Path) -> None:
    journal = tmp_path / "journal"
    for offset, facts in (
        (0, _facts()),
        (5, _facts(ready=False, unresolved=1)),
        (10, _facts()),
        (30, _facts()),
    ):
        append_observation(
            journal,
            observed_at=NOW + timedelta(minutes=offset),
            environment="testnet",
            facts=facts,
        )
    metrics = derive_metrics(
        verify_journal(journal), max_observation_gap_seconds=900
    )
    assert metrics["consecutive_unattended_days"] == 0.0
    assert metrics["decision_service_readiness_pct"] == 75.0


def test_bundle_has_all_required_immutable_ref_kinds(tmp_path: Path) -> None:
    journal = tmp_path / "journal"
    append_observation(
        journal, observed_at=NOW, environment="paper", facts=_facts()
    )
    bundle, path = build_report_bundle(
        verify_journal(journal),
        journal_directory=journal,
        output_directory=tmp_path / "evidence",
        generated_at=NOW,
        max_observation_gap_seconds=900,
    )
    assert path.exists()
    assert bundle["machine_attestation"]["attested"] is True
    assert bundle["machine_attestation"]["criteria_passed"] is False
    assert {ref["kind"] for ref in bundle["evidence_refs"]} == {
        "unattended_soak",
        "fault_injection",
        "incident_log",
        "readiness_report",
    }


def test_collector_defaults_follow_runtime_data_root(tmp_path: Path) -> None:
    runtime_root = tmp_path.resolve()
    args = _build_parser(runtime_root).parse_args(
        ["--account-reconciliation", "account.json"]
    )

    assert args.db == runtime_root / "state.db"
    assert args.audit_db == runtime_root / "audit.db"
    assert args.config == runtime_root / "live_config.json"
    assert args.journal_dir == (
        runtime_root / "verification_artifacts" / "soak_journal"
    )
    assert args.output_dir == runtime_root / "verification_artifacts" / "evidence"


def test_collector_reads_health_from_split_audit_store(tmp_path: Path) -> None:
    state_path = tmp_path / "state.db"
    audit_path = tmp_path / "audit.db"
    research_path = tmp_path / "research.db"
    writer = SplitStateWriter(
        state_path=str(state_path),
        audit_path=str(audit_path),
        research_path=str(research_path),
    )
    try:
        writer.set_risk_snapshot(
            {
                "runtime_ready": True,
                "telemetry_connected": True,
                "trading_mode": "testnet",
            }
        )
        writer.record_health_sample(
            "loop_alive",
            1.0,
            alert_level="ok",
            runtime_mode="SAFE_MODE",
            sample_time=NOW.isoformat(),
        )
        writer.record_health_sample(
            "storage",
            0.0,
            alert_level="critical",
            runtime_mode="SAFE_MODE",
            sample_time=(NOW + timedelta(seconds=1)).isoformat(),
        )
        writer.flush()
    finally:
        writer.close()

    risk, state = _read_state(state_path, audit_path)

    assert risk["runtime_ready"] == "true"
    assert risk["telemetry_connected"] == "true"
    assert state["latest_loop"]["sample_time"] == NOW.isoformat()
    assert [row["metric"] for row in state["critical_health_rows"]] == [
        "storage"
    ]


def test_runtime_data_root_falls_back_locally_and_rejects_relative_paths() -> None:
    assert _resolve_runtime_data_root(None) == PROJECT_ROOT
    assert _resolve_runtime_data_root("  ") == PROJECT_ROOT
    with pytest.raises(ValueError, match="must be an absolute path"):
        _resolve_runtime_data_root("relative/runtime")


def test_account_freshness_rejects_stale_or_future_signed_times() -> None:
    def account(observed_at: datetime) -> dict:
        encoded = observed_at.isoformat()
        return {
            "generated_at": encoded,
            "exchange_reconciliation_snapshot": {"observed_at": encoded},
        }

    _validate_account_freshness(
        account(NOW - timedelta(seconds=299)),
        now=NOW,
        max_age_seconds=300.0,
    )
    with pytest.raises(ValueError, match="stale"):
        _validate_account_freshness(
            account(NOW - timedelta(seconds=301)),
            now=NOW,
            max_age_seconds=300.0,
        )
    with pytest.raises(ValueError, match="in the future"):
        _validate_account_freshness(
            account(NOW + timedelta(seconds=1)),
            now=NOW,
            max_age_seconds=300.0,
        )


@pytest.mark.parametrize("value", ["0", "-1", "inf", "nan"])
def test_account_age_override_must_be_positive_and_finite(value: str) -> None:
    with pytest.raises(SystemExit):
        _build_parser(PROJECT_ROOT).parse_args(
            [
                "--account-reconciliation",
                "account.json",
                "--max-account-age-seconds",
                value,
            ]
        )
