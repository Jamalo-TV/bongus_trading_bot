from __future__ import annotations

import json
from pathlib import Path

from scripts import verify_masterplan as verifier


def _safe_config() -> dict[str, object]:
    return {
        "pause_new_entries": True,
        "notional_per_trade": 2_500.0,
        "per_symbol_notional_cap_usd": 2_500.0,
        "max_gross_exposure_usd": 10_000.0,
        "dynamic_leverage_enabled": False,
        "auto_compound_enabled": False,
    }


def _phase_0_metrics() -> dict[str, object]:
    return {
        "clean_ci_passed": True,
        "decision_order_fill_lineage_pct": 100.0,
        "deterministic_causal_replay": True,
        "exchange_fill_funding_mapping_pct": 100.0,
        "daily_unexplained_max_usd": 0.01,
        "within_exchange_precision": True,
    }


def _phase_0_refs() -> list[dict[str, str]]:
    return [
        {
            "kind": kind,
            "uri": f"verification_artifacts/{kind}.json",
            "sha256": "a" * 64,
        }
        for kind in ("clean_ci", "runtime_reconciliation", "causal_replay")
    ]


def test_contract_manifest_encodes_every_section_k_gate_and_failure() -> None:
    manifest = verifier._load_contract_manifest(verifier.DEFAULT_CONTRACT_MANIFEST)

    assert tuple(item["id"] for item in manifest["implementation_phases"]) == (
        verifier.EXPECTED_IMPLEMENTATION_PHASES
    )
    assert tuple(item["id"] for item in manifest["phase_promotion_gates"]) == (
        verifier.EXPECTED_PHASE_GATES
    )
    failures = manifest["failure_injection_matrix"]
    assert tuple(item["id"] for item in failures) == verifier.EXPECTED_FAILURE_IDS
    assert tuple(item["ordinal"] for item in failures) == tuple(range(1, 16))
    assert all(item["requirement"] for item in failures)
    assert all(item["behavior_tests"] for item in failures)


def test_partial_contract_cannot_pass_even_when_every_mapped_test_passes() -> None:
    record = {
        "id": "phase_partial",
        "coverage": "partial",
        "coverage_gap": "one end-to-end permutation is absent",
        "behavior_tests": ["tests/test_example.py::test_behavior"],
    }

    check = verifier._behavior_contract_check(
        "implementation",
        record,
        {"tests/test_example.py::test_behavior": verifier.PASS},
        local_checks_ran=True,
    )

    assert check.status == verifier.NOT_VERIFIED
    assert check.observed["coverage_gap"]


def test_complete_contract_requires_mapped_rust_behavior_to_pass() -> None:
    record = {
        "id": "complete_cross_language_contract",
        "coverage": "complete",
        "behavior_tests": ["tests/test_example.py::test_behavior"],
        "rust_behavior_tests": [
            "order_manager::tests::fill_progresses_during_slow_rest"
        ],
    }

    missing = verifier._behavior_contract_check(
        "implementation",
        record,
        {"tests/test_example.py::test_behavior": verifier.PASS},
        local_checks_ran=True,
        rust_outcomes={},
    )
    passing = verifier._behavior_contract_check(
        "implementation",
        record,
        {"tests/test_example.py::test_behavior": verifier.PASS},
        local_checks_ran=True,
        rust_outcomes={
            "order_manager::tests::fill_progresses_during_slow_rest": verifier.PASS
        },
    )

    assert missing.status == verifier.NOT_VERIFIED
    assert passing.status == verifier.PASS


def test_cargo_output_retains_individual_rust_test_outcomes() -> None:
    outcomes = verifier._rust_outcomes_from_cargo_output(
        "\n".join(
            [
                "test order_manager::tests::fill_progresses ... ok",
                "test binance_rest::tests::timeout_recovery ... FAILED",
                "test tests::optional_external_fixture ... ignored",
            ]
        )
    )

    assert outcomes == {
        "order_manager::tests::fill_progresses": verifier.PASS,
        "binance_rest::tests::timeout_recovery": verifier.FAIL,
        "tests::optional_external_fixture": "SKIP",
    }


def test_skipped_or_missing_behavior_test_is_not_verified(tmp_path: Path) -> None:
    junit = tmp_path / "pytest.xml"
    junit.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite tests="1" skipped="1">
<testcase classname="tests.test_example" name="test_behavior">
<skipped type="pytest.skip" message="not available" />
</testcase></testsuite></testsuites>
""",
        encoding="utf-8",
    )
    outcomes = verifier._pytest_outcomes_from_junit(junit)
    record = {
        "id": "complete_contract",
        "coverage": "complete",
        "behavior_tests": [
            "tests/test_example.py::test_behavior",
            "tests/test_example.py::test_missing",
        ],
    }

    check = verifier._behavior_contract_check(
        "implementation",
        record,
        outcomes,
        local_checks_ran=True,
    )

    assert outcomes["tests/test_example.py::test_behavior"] == "SKIP"
    assert check.status == verifier.NOT_VERIFIED
    assert check.observed["test_outcomes"]["tests/test_example.py::test_missing"] == (
        "MISSING"
    )


def test_missing_live_safety_override_uses_fail_closed_static_capital_defaults() -> None:
    checks = {check.check_id: check for check in verifier._safety_checks({})}

    assert checks["safety.pause_new_entries"].status == verifier.FAIL
    assert checks["safety.capital_ceiling.max_gross_exposure_usd"].status == verifier.PASS
    assert checks["safety.capital_ceiling.per_symbol_notional_cap_usd"].status == (
        verifier.PASS
    )


def test_canonical_probe_uses_live_dispatch_and_real_messagepack_boundary(
    tmp_path: Path,
) -> None:
    config = tmp_path / "live_config.json"
    config.write_text(json.dumps(_safe_config()), encoding="utf-8")

    check = verifier._canonical_python_rust_path_check(config)

    assert check.status == verifier.PASS
    assert check.proof_kind == "isolated_runtime_probe"
    assert check.observed["paused"] is True
    assert check.observed["schema_version"] == 3
    assert check.observed["intent"] == "EXIT_LONG"
    assert check.observed["urgency"] == 1.0
    assert check.observed["outbox_state"] == "SENT"
    assert check.observed["outbox_matches_wire"] is True
    assert check.observed["command_hash_valid"] is True
    assert check.observed["send_attempts"] == 1
    assert check.observed["rust_accepted"] is True
    assert check.observed["rust_schema_version"] == 3
    assert check.observed["rust_command_hash_matches"] is True
    assert check.observed["rust_exact_exit_matches"] is True
    assert check.observed["wire_bytes"] > 0


def test_external_gate_rejects_unit_test_fixture_with_perfect_metrics() -> None:
    manifest = verifier._load_contract_manifest(verifier.DEFAULT_CONTRACT_MANIFEST)
    evidence = {
        "gates": {
            "phase_0_to_1": {
                "evidence_kind": "unit_test",
                "attested": True,
                "evidence_refs": ["pytest output"],
                "metrics": _phase_0_metrics(),
            }
        }
    }

    checks = verifier._external_gate_checks(manifest, evidence)
    phase_0 = checks[0]

    assert phase_0.status == verifier.BLOCKED_EVIDENCE
    assert any("unit_test is never accepted" in reason for reason in phase_0.observed["unmet"])


def test_external_gate_requires_dedicated_attested_artifact() -> None:
    manifest = verifier._load_contract_manifest(verifier.DEFAULT_CONTRACT_MANIFEST)
    evidence = {
        "gates": {
            "phase_0_to_1": {
                "evidence_kind": "ci_and_runtime",
                "attested": True,
                "evidence_refs": _phase_0_refs(),
                "metrics": _phase_0_metrics(),
            }
        }
    }

    checks = verifier._external_gate_checks(manifest, evidence)

    assert checks[0].status == verifier.PASS
    assert all(check.status == verifier.BLOCKED_EVIDENCE for check in checks[1:])


def test_require_implementation_fails_when_local_commands_are_skipped(
    tmp_path: Path,
) -> None:
    config = tmp_path / "live_config.json"
    config.write_text(json.dumps(_safe_config()), encoding="utf-8")
    output = tmp_path / "report.json"

    exit_code = verifier.main(
        [
            "--config",
            str(config),
            "--db",
            str(tmp_path / "missing.db"),
            "--external-evidence",
            str(tmp_path / "missing-evidence.json"),
            "--output",
            str(output),
            "--require-implementation",
        ]
    )
    report = json.loads(output.read_text(encoding="utf-8"))

    assert exit_code == 1
    assert report["local_validation_status"] == verifier.NOT_VERIFIED
    assert report["implementation_status"] == verifier.NOT_VERIFIED
    assert any(
        check["check_id"] == "local.commands"
        and check["status"] == verifier.NOT_VERIFIED
        for check in report["checks"]
    )


def test_require_phase_is_cumulative_and_missing_evidence_is_nonzero(
    tmp_path: Path,
) -> None:
    config = tmp_path / "live_config.json"
    config.write_text(json.dumps(_safe_config()), encoding="utf-8")
    output = tmp_path / "phase-report.json"

    exit_code = verifier.main(
        [
            "--config",
            str(config),
            "--db",
            str(tmp_path / "missing.db"),
            "--external-evidence",
            str(tmp_path / "missing-evidence.json"),
            "--output",
            str(output),
            "--require-phase",
            "2",
        ]
    )
    report = json.loads(output.read_text(encoding="utf-8"))

    assert exit_code == 1
    assert report["required_gate_ids"] == list(verifier.EXPECTED_PHASE_GATES[:3])
    required = {
        check["check_id"]: check["status"]
        for check in report["checks"]
        if check["check_id"].startswith("promotion.")
    }
    assert all(
        required[f"promotion.{gate_id}"] == verifier.BLOCKED_EVIDENCE
        for gate_id in verifier.EXPECTED_PHASE_GATES[:3]
    )


def test_require_canary_demands_every_precanary_gate() -> None:
    manifest = verifier._load_contract_manifest(verifier.DEFAULT_CONTRACT_MANIFEST)

    required = verifier._required_gate_ids(
        manifest,
        [],
        require_canary=True,
        require_capital_increase=False,
    )

    assert required == list(verifier.EXPECTED_PHASE_GATES[:5])


def test_default_mode_is_truthful_report_only(tmp_path: Path) -> None:
    config = tmp_path / "live_config.json"
    config.write_text(json.dumps(_safe_config()), encoding="utf-8")
    output = tmp_path / "report-only.json"

    exit_code = verifier.main(
        [
            "--config",
            str(config),
            "--db",
            str(tmp_path / "missing.db"),
            "--external-evidence",
            str(tmp_path / "missing-evidence.json"),
            "--output",
            str(output),
        ]
    )
    report = json.loads(output.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert report["implementation_status"] == verifier.NOT_VERIFIED
    assert report["promotion_status"] == verifier.BLOCKED_EVIDENCE
    assert report["contract_manifest"]["failure_scenario_count"] == 15
