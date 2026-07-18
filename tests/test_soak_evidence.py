from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from bongus.testing.soak_evidence import (
    SoakJournalError,
    append_observation,
    build_report_bundle,
    derive_metrics,
    verify_journal,
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
