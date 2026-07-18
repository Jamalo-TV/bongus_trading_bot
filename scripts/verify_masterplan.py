"""Truthful verifier for the autonomy/profit master plan.

This command deliberately separates three different claims:

* local validation: commands were actually executed and passed;
* implementation coverage: mapped behavioral contracts were executed and the
  manifest says the required matrix is complete;
* promotion evidence: dedicated runtime/research/canary artifacts satisfy the
  literal Section K gates.

File presence is inventory, not proof.  A skipped command or test is never a
pass, and unit tests are never treated as external trading evidence.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from typing import Any, Callable, Iterable, Sequence
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bongus.testing.execution_fault_campaign import (  # noqa: E402
    run_parallel_execution_fault_campaign,
)
from bongus.core import config as static_config  # noqa: E402


DEFAULT_CONTRACT_MANIFEST = ROOT / "masterplan_verification_contracts.json"
DEFAULT_EXTERNAL_EVIDENCE = (
    ROOT / "verification_artifacts" / "masterplan_external_evidence.json"
)

PROTECTED_CAPITAL_CEILINGS = {
    "notional_per_trade": 2_500.0,
    "per_symbol_notional_cap_usd": 2_500.0,
    "max_gross_exposure_usd": 10_000.0,
}

EXPECTED_IMPLEMENTATION_PHASES = tuple(
    f"phase_{number}_{name}"
    for number, name in (
        (0, "measurement"),
        (1, "integrity"),
        (2, "execution"),
        (3, "strategy"),
        (4, "recovery"),
        (5, "portfolio"),
        (6, "research"),
    )
)
EXPECTED_PHASE_GATES = (
    "phase_0_to_1",
    "phase_1_to_2",
    "phase_2_to_3",
    "phase_3_to_4",
    "phase_4_to_live_canary",
    "canary_to_more_capital",
)
EXPECTED_FAILURE_IDS = (
    "failure_01_transport_drop_duplicate_reorder",
    "failure_02_crash_every_transition",
    "failure_03_accepted_rest_timeout",
    "failure_04_stream_disconnect_and_cursor_replay",
    "failure_05_one_sided_overhedged_dust_exchange_only",
    "failure_06_exchange_metadata_change_active_cycle",
    "failure_07_api_pressure_maintenance_clock_hang",
    "failure_08_exhaust_capital_and_repair_reserves",
    "failure_09_corruption_backup_rebuild",
    "failure_10_config_concurrency_version_hash",
    "failure_11_heartbeat_false_green_and_port_collision",
    "failure_12_restart_safe_mode_cooldown_incident",
    "failure_13_funding_reversal_eligibility_interval",
    "failure_14_flash_withdrawal_partial_liquidation_emergency_exit",
    "failure_15_treasury_reserved_and_unrelated_assets",
)

PASS = "PASS"
FAIL = "FAIL"
NOT_VERIFIED = "NOT_VERIFIED"
BLOCKED_EVIDENCE = "BLOCKED_EVIDENCE"


@dataclass(slots=True)
class Check:
    check_id: str
    status: str
    summary: str
    proof_kind: str
    observed: Any = None
    required: Any = None


@dataclass(slots=True)
class CommandEvidence:
    check_id: str
    command: list[str]
    status: str
    return_code: int
    elapsed_seconds: float
    output_tail: str
    test_outcomes: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class VerificationReport:
    schema_version: int
    generated_at: str
    repository_root: str
    contract_manifest: dict[str, Any]
    local_validation_status: str
    implementation_status: str
    safety_status: str
    promotion_status: str
    required_gate_ids: list[str] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    command_evidence: list[CommandEvidence] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_behavior_tests(owner: str, value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{owner}.behavior_tests must be a non-empty list")
    tests: list[str] = []
    for nodeid in value:
        if (
            not isinstance(nodeid, str)
            or not nodeid.startswith("tests/")
            or "::test_" not in nodeid
        ):
            raise ValueError(f"{owner} has invalid pytest node id: {nodeid!r}")
        tests.append(nodeid)
    if len(tests) != len(set(tests)):
        raise ValueError(f"{owner}.behavior_tests contains duplicates")
    return tests


def _validate_rust_behavior_tests(owner: str, value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{owner}.rust_behavior_tests must be a list")
    tests: list[str] = []
    for test_name in value:
        if (
            not isinstance(test_name, str)
            or not ("::tests::" in test_name or test_name.startswith("tests::"))
            or any(character.isspace() for character in test_name)
        ):
            raise ValueError(f"{owner} has invalid Rust test name: {test_name!r}")
        tests.append(test_name)
    if len(tests) != len(set(tests)):
        raise ValueError(f"{owner}.rust_behavior_tests contains duplicates")
    return tests


def _validate_exact_ids(
    label: str,
    records: Any,
    expected: Sequence[str],
) -> list[dict[str, Any]]:
    if not isinstance(records, list):
        raise ValueError(f"{label} must be a list")
    if any(not isinstance(record, dict) for record in records):
        raise ValueError(f"{label} entries must be objects")
    typed = list(records)
    observed = tuple(str(record.get("id", "")) for record in typed)
    if observed != tuple(expected):
        raise ValueError(
            f"{label} ids/order must be exactly {list(expected)!r}; got {list(observed)!r}"
        )
    return typed


def _load_contract_manifest(path: Path) -> dict[str, Any]:
    manifest = _load_json(path)
    if manifest.get("schema_version") != 1:
        raise ValueError("contract manifest schema_version must be 1")
    source = manifest.get("source")
    if not isinstance(source, dict) or source.get("section") != "K. Verification plan":
        raise ValueError("contract manifest must identify Section K. Verification plan")

    phases = _validate_exact_ids(
        "implementation_phases",
        manifest.get("implementation_phases"),
        EXPECTED_IMPLEMENTATION_PHASES,
    )
    for record in phases:
        if record.get("coverage") not in {"complete", "partial"}:
            raise ValueError(f"{record['id']}.coverage must be complete or partial")
        _validate_behavior_tests(str(record["id"]), record.get("behavior_tests"))
        _validate_rust_behavior_tests(
            str(record["id"]), record.get("rust_behavior_tests")
        )

    gates = _validate_exact_ids(
        "phase_promotion_gates",
        manifest.get("phase_promotion_gates"),
        EXPECTED_PHASE_GATES,
    )
    selectors = tuple(str(record.get("selector", "")) for record in gates)
    if selectors != ("0", "1", "2", "3", "4", "canary"):
        raise ValueError("phase promotion selectors must be 0,1,2,3,4,canary")
    for record in gates:
        if not record.get("proof") or not record.get("evidence_kind"):
            raise ValueError(f"{record['id']} needs proof and evidence_kind")
        ref_kinds = record.get("required_evidence_ref_kinds")
        if (
            not isinstance(ref_kinds, list)
            or not ref_kinds
            or any(not isinstance(kind, str) or not kind for kind in ref_kinds)
            or len(ref_kinds) != len(set(ref_kinds))
        ):
            raise ValueError(
                f"{record['id']}.required_evidence_ref_kinds must be unique strings"
            )
        _validate_behavior_tests(str(record["id"]), record.get("behavior_tests"))
        _validate_rust_behavior_tests(
            str(record["id"]), record.get("rust_behavior_tests")
        )

    failures = _validate_exact_ids(
        "failure_injection_matrix",
        manifest.get("failure_injection_matrix"),
        EXPECTED_FAILURE_IDS,
    )
    ordinals = tuple(record.get("ordinal") for record in failures)
    if ordinals != tuple(range(1, 16)):
        raise ValueError("failure matrix ordinals must be exactly 1 through 15")
    for record in failures:
        if record.get("coverage") not in {"complete", "partial"}:
            raise ValueError(f"{record['id']}.coverage must be complete or partial")
        if not record.get("requirement"):
            raise ValueError(f"{record['id']} needs the Section K requirement text")
        _validate_behavior_tests(str(record["id"]), record.get("behavior_tests"))
        _validate_rust_behavior_tests(
            str(record["id"]), record.get("rust_behavior_tests")
        )
    return manifest


def _decode_state_value(value: str) -> Any:
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def _count(conn: sqlite3.Connection, table: str) -> int:
    if not _table_exists(conn, table):
        return 0
    return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def _timestamp_span_days(
    conn: sqlite3.Connection,
    table: str,
    column: str,
) -> float:
    if not _table_exists(conn, table):
        return 0.0
    row = conn.execute(
        f'SELECT MIN("{column}"), MAX("{column}") FROM "{table}"'
    ).fetchone()
    if not row or not row[0] or not row[1]:
        return 0.0
    try:
        start = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(row[1]).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return max(0.0, (end - start).total_seconds() / 86_400.0)


def _read_db_metrics(path: Path) -> dict[str, Any]:
    """Read operational context only; these observations never pass a gate."""

    if not path.exists():
        return {"available": False, "path": str(path)}
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        state = (
            {
                str(row["key"]): _decode_state_value(str(row["value"]))
                for row in conn.execute("SELECT key, value FROM risk_state")
            }
            if "risk_state" in tables
            else {}
        )
        funding_events = 0
        if "economic_ledger_events" in tables:
            row = conn.execute(
                "SELECT COUNT(*) FROM economic_ledger_events "
                "WHERE UPPER(event_type)='FUNDING'"
            ).fetchone()
            funding_events = int(row[0]) if row else 0
        return {
            "available": True,
            "path": str(path.resolve()),
            "economic_event_count": _count(conn, "economic_ledger_events"),
            "funding_event_count": funding_events,
            "execution_event_count": _count(conn, "execution_events"),
            "trade_count": _count(conn, "trade_history"),
            "decision_count": _count(conn, "candidate_snapshots"),
            "execution_quality_count": _count(conn, "execution_quality"),
            "health_sample_count": _count(conn, "health_samples"),
            "health_span_days": _timestamp_span_days(
                conn, "health_samples", "sample_time"
            ),
            "runtime_ready": bool(state.get("runtime_ready", False)),
            "ledger_reconciled": bool(
                state.get("economic_ledger_reconciled", False)
            ),
            "trading_mode": state.get("trading_mode"),
        }
    finally:
        conn.close()


def _run_command(
    check_id: str,
    command: list[str],
    cwd: Path,
) -> CommandEvidence:
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        output = completed.stdout or ""
        return_code = int(completed.returncode)
    except OSError as exc:
        output = f"unable to start command: {exc}"
        return_code = -1
    return CommandEvidence(
        check_id=check_id,
        command=command,
        status=PASS if return_code == 0 else FAIL,
        return_code=return_code,
        elapsed_seconds=round(time.perf_counter() - started, 3),
        output_tail=output[-4_000:],
        test_outcomes=(
            _rust_outcomes_from_cargo_output(output)
            if check_id == "local.command.cargo_test"
            else {}
        ),
    )


def _local_commands(pytest_junit: Path) -> list[tuple[str, list[str], Path]]:
    pyright = Path(sys.executable).with_name(
        "pyright.exe" if sys.platform == "win32" else "pyright"
    )
    return [
        (
            "local.command.pytest",
            [
                sys.executable,
                "-m",
                "pytest",
                "tests",
                "-q",
                f"--junitxml={pytest_junit}",
            ],
            ROOT,
        ),
        ("local.command.pyright", [str(pyright)], ROOT),
        (
            "local.command.compileall",
            [sys.executable, "-m", "compileall", "-q", "bongus", "scripts"],
            ROOT,
        ),
        (
            "local.command.cargo_fmt",
            ["cargo", "fmt", "--all", "--", "--check"],
            ROOT / "execution_engine",
        ),
        (
            "local.command.cargo_test",
            ["cargo", "test", "--locked"],
            ROOT / "execution_engine",
        ),
    ]


def _merge_test_status(current: str | None, incoming: str) -> str:
    priority = {PASS: 0, "SKIP": 1, FAIL: 2}
    if current is None or priority[incoming] > priority[current]:
        return incoming
    return current


_RUST_TEST_RESULT = re.compile(
    r"^test (?P<name>\S+) \.\.\. (?P<status>ok|FAILED|ignored)$", re.MULTILINE
)


def _rust_outcomes_from_cargo_output(output: str) -> dict[str, str]:
    status_map = {"ok": PASS, "FAILED": FAIL, "ignored": "SKIP"}
    outcomes: dict[str, str] = {}
    for match in _RUST_TEST_RESULT.finditer(output):
        name = match.group("name")
        incoming = status_map[match.group("status")]
        outcomes[name] = _merge_test_status(outcomes.get(name), incoming)
    return outcomes


def _pytest_outcomes_from_junit(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return {}
    outcomes: dict[str, str] = {}
    for testcase in root.iter("testcase"):
        classname = testcase.attrib.get("classname", "")
        name = testcase.attrib.get("name", "")
        if not classname or not name:
            continue
        parts = classname.split(".")
        module_index = next(
            (index for index, part in enumerate(parts) if part.startswith("test_")),
            None,
        )
        if module_index is None:
            continue
        module_path = "/".join(parts[: module_index + 1]) + ".py"
        class_parts = parts[module_index + 1 :]
        suffix = "::".join([*class_parts, name])
        nodeid = f"{module_path}::{suffix}"
        child_tags = {child.tag.rsplit("}", 1)[-1] for child in testcase}
        status = FAIL if child_tags.intersection({"failure", "error"}) else PASS
        if "skipped" in child_tags:
            status = "SKIP"
        keys = [nodeid]
        if "[" in name:
            base_name = name.split("[", 1)[0]
            base_suffix = "::".join([*class_parts, base_name])
            keys.append(f"{module_path}::{base_suffix}")
        for key in keys:
            outcomes[key] = _merge_test_status(outcomes.get(key), status)
    return outcomes


def _behavior_contract_check(
    prefix: str,
    record: dict[str, Any],
    outcomes: dict[str, str],
    *,
    local_checks_ran: bool,
    rust_outcomes: dict[str, str] | None = None,
) -> Check:
    nodeids = [str(value) for value in record["behavior_tests"]]
    rust_test_names = [str(value) for value in record.get("rust_behavior_tests", [])]
    observed_outcomes = {
        nodeid: outcomes.get(nodeid, "MISSING") for nodeid in nodeids
    }
    observed_outcomes.update(
        {
            test_name: (rust_outcomes or {}).get(test_name, "MISSING")
            for test_name in rust_test_names
        }
    )
    coverage = str(record.get("coverage", "partial"))
    if not local_checks_ran:
        status = NOT_VERIFIED
        summary = "mapped behavior tests were not executed"
    elif any(value == FAIL for value in observed_outcomes.values()):
        status = FAIL
        summary = "one or more mapped behavior tests failed"
    elif coverage != "complete":
        status = NOT_VERIFIED
        summary = "mapped tests passed only partial Section K coverage"
    elif any(value != PASS for value in observed_outcomes.values()):
        status = NOT_VERIFIED
        summary = "one or more mapped behavior tests were missing or skipped"
    else:
        status = PASS
        summary = "complete mapped behavior contract executed and passed"
    return Check(
        check_id=f"{prefix}.{record['id']}",
        status=status,
        summary=summary,
        proof_kind="local_behavior",
        observed={
            "coverage": coverage,
            "coverage_gap": record.get("coverage_gap"),
            "test_outcomes": observed_outcomes,
        },
        required={"coverage": "complete", "all_mapped_tests": PASS},
    )


def _implementation_checks(
    manifest: dict[str, Any],
    outcomes: dict[str, str],
    *,
    local_checks_ran: bool,
    rust_outcomes: dict[str, str] | None = None,
) -> list[Check]:
    checks = [
        _behavior_contract_check(
            "implementation",
            record,
            outcomes,
            local_checks_ran=local_checks_ran,
            rust_outcomes=rust_outcomes,
        )
        for record in manifest["implementation_phases"]
    ]
    checks.extend(
        _behavior_contract_check(
            "failure_matrix",
            record,
            outcomes,
            local_checks_ran=local_checks_ran,
            rust_outcomes=rust_outcomes,
        )
        for record in manifest["failure_injection_matrix"]
    )
    return checks


def _safety_checks(config: dict[str, Any]) -> list[Check]:
    def effective(key: str, static_name: str, fallback: Any = None) -> Any:
        if key in config:
            return config[key]
        return getattr(static_config, static_name, fallback)

    pause_new_entries = effective(
        "pause_new_entries", "PAUSE_NEW_ENTRIES", False
    )
    checks = [
        Check(
            check_id="safety.pause_new_entries",
            status=PASS if pause_new_entries is True else FAIL,
            summary="new entries remain operator-paused",
            proof_kind="configuration",
            observed=pause_new_entries,
            required=True,
        )
    ]
    for key, ceiling in PROTECTED_CAPITAL_CEILINGS.items():
        observed = effective(key, key.upper())
        valid = (
            isinstance(observed, (int, float))
            and not isinstance(observed, bool)
            and float(observed) <= ceiling
        )
        checks.append(
            Check(
                check_id=f"safety.capital_ceiling.{key}",
                status=PASS if valid else FAIL,
                summary="capital/risk ceiling has not increased",
                proof_kind="configuration",
                observed=observed,
                required={"maximum": ceiling},
            )
        )
    leverage_enabled = bool(
        effective("dynamic_leverage_enabled", "DYNAMIC_LEVERAGE_ENABLED", False)
    )
    checks.append(
        Check(
            check_id="safety.dynamic_leverage_disabled",
            status=PASS if not leverage_enabled else FAIL,
            summary="dynamic leverage remains disabled",
            proof_kind="configuration",
            observed=leverage_enabled,
            required=False,
        )
    )
    auto_compound = bool(
        effective("auto_compound_enabled", "AUTO_COMPOUND_ENABLED", False)
    )
    checks.append(
        Check(
            check_id="safety.auto_compound_disabled",
            status=PASS if not auto_compound else FAIL,
            summary="automatic capital increases remain disabled",
            proof_kind="configuration",
            observed=auto_compound,
            required=False,
        )
    )
    return checks


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _gate_metric_requirements(gate_id: str) -> list[tuple[str, Callable[[Any, dict[str, Any]], bool], str]]:
    true = lambda value, _metrics: value is True
    false = lambda value, _metrics: value is False
    minimum = lambda threshold: (
        lambda value, _metrics: _is_number(value) and float(value) >= threshold
    )
    exactly = lambda expected: (
        lambda value, _metrics: _is_number(value) and float(value) == expected
    )
    closed_range = lambda lower, upper: (
        lambda value, _metrics: _is_number(value)
        and lower <= float(value) <= upper
    )
    half_open_range = lambda lower, upper: (
        lambda value, _metrics: _is_number(value)
        and lower <= float(value) < upper
    )
    above = lambda threshold: (
        lambda value, _metrics: _is_number(value) and float(value) > threshold
    )

    requirements: dict[
        str, list[tuple[str, Callable[[Any, dict[str, Any]], bool], str]]
    ] = {
        "phase_0_to_1": [
            ("clean_ci_passed", true, "must be true"),
            ("decision_order_fill_lineage_pct", exactly(100.0), "must be exactly 100"),
            ("deterministic_causal_replay", true, "must be true"),
            ("exchange_fill_funding_mapping_pct", exactly(100.0), "must be exactly 100"),
            ("daily_unexplained_max_usd", closed_range(0.0, 0.01), "must be between 0 and 0.01"),
            ("within_exchange_precision", true, "must be true"),
        ],
        "phase_1_to_2": [
            ("duplicate_exchange_effects", exactly(0.0), "must be exactly 0"),
            ("state_invariant_failures", exactly(0.0), "must be exactly 0"),
            ("randomized_state_machine_traces", minimum(1_000_000), "must be >=1000000"),
            ("unclassified_open_orders_positions", exactly(0.0), "must be exactly 0"),
            ("ready_under_mismatch", false, "must be false"),
            ("backup_restore_demonstrated", true, "must be true"),
        ],
        "phase_2_to_3": [
            ("representative_completed_cycles", minimum(100.0), "must be >=100"),
            ("cost_model_holdout_median_bias_near_zero", true, "must be true under preregistered tolerance"),
            ("cost_model_p90_coverage_calibrated", true, "must be true"),
            ("cost_model_mape_pct", half_open_range(0.0, 15.0), "must be >=0 and <15"),
            ("calibrated_bucket_min_n", minimum(100.0), "must be >=100"),
            ("adaptive_route_noninferior_total_cost", true, "must be true"),
            ("hedge_risk_slo_worsened", false, "must be false"),
        ],
        "phase_3_to_4": [
            ("purged_oos_positive_incremental_net_value", true, "must be true"),
            ("live_shadow_or_paper_positive_incremental_net_value", true, "must be true"),
            ("multiple_testing_correction_passed", true, "must be true"),
            ("rotation_counterfactual_value_positive", true, "must be true"),
            ("forecast_stable_regime_count", minimum(3.0), "must be >=3"),
        ],
        "phase_4_to_live_canary": [
            ("consecutive_unattended_days", minimum(30.0), "must be >=30"),
            ("decision_service_readiness_pct", closed_range(99.5, 100.0), "must be between 99.5 and 100"),
            ("critical_reconciliation_invariant_incidents", exactly(0.0), "must be exactly 0"),
            ("injected_gaps_detected_replayed_pct", exactly(100.0), "must be exactly 100"),
            ("routine_auto_recovery_within_slo_pct", closed_range(95.0, 100.0), "must be between 95 and 100"),
            ("unresolved_alerts", exactly(0.0), "must be exactly 0"),
        ],
        "canary_to_more_capital": [
            ("closed_cycles", minimum(100.0), "must be >=100"),
            ("actual_funding_settlements", minimum(30.0), "must be >=30"),
            ("elapsed_days", minimum(30.0), "must be >=30"),
            ("daily_reconciliation_pct", exactly(100.0), "must be exactly 100"),
            ("duplicate_or_orphan_exposure_count", exactly(0.0), "must be exactly 0"),
            ("cluster_bootstrap_95_lcb_net_expectancy", above(0.0), "must be >0"),
            ("drawdown_within_preregistered_limit", true, "must be true"),
            ("cost_within_preregistered_limit", true, "must be true"),
            ("mismatch_within_preregistered_limit", true, "must be true"),
            ("uptime_within_preregistered_limit", true, "must be true"),
        ],
    }
    return requirements[gate_id]


def _external_gate_checks(
    manifest: dict[str, Any],
    external_evidence: dict[str, Any] | None,
) -> list[Check]:
    evidence_gates = (
        external_evidence.get("gates", {})
        if isinstance(external_evidence, dict)
        else {}
    )
    if not isinstance(evidence_gates, dict):
        evidence_gates = {}

    checks: list[Check] = []
    for gate in manifest["phase_promotion_gates"]:
        gate_id = str(gate["id"])
        record = evidence_gates.get(gate_id)
        unmet: list[str] = []
        metrics: dict[str, Any] = {}
        if not isinstance(record, dict):
            unmet.append("dedicated evidence record is missing")
            record = {}
        if record.get("evidence_kind") != gate["evidence_kind"]:
            unmet.append(
                f"evidence_kind must be {gate['evidence_kind']!r}; unit_test is never accepted"
            )
        if record.get("attested") is not True:
            unmet.append("attested must be true")
        refs = record.get("evidence_refs")
        valid_refs: list[dict[str, str]] = []
        if not isinstance(refs, list) or not refs:
            unmet.append("evidence_refs must be a non-empty list of immutable artifacts")
        else:
            for index, ref in enumerate(refs):
                if not isinstance(ref, dict):
                    unmet.append(f"evidence_refs[{index}] must be an object")
                    continue
                kind = ref.get("kind")
                uri = ref.get("uri")
                sha256 = ref.get("sha256")
                digest_valid = (
                    isinstance(sha256, str)
                    and len(sha256) == 64
                    and all(character in "0123456789abcdefABCDEF" for character in sha256)
                )
                if (
                    not isinstance(kind, str)
                    or not kind
                    or not isinstance(uri, str)
                    or not uri.strip()
                    or not digest_valid
                ):
                    unmet.append(
                        f"evidence_refs[{index}] needs non-empty kind/uri and a 64-hex sha256"
                    )
                    continue
                if kind in {"unit_test", "pytest", "junit"}:
                    unmet.append(
                        f"evidence_refs[{index}] kind {kind!r} is local-test evidence, not promotion evidence"
                    )
                    continue
                valid_refs.append(
                    {"kind": str(kind), "uri": str(uri), "sha256": str(sha256)}
                )
        observed_ref_kinds = {ref["kind"] for ref in valid_refs}
        required_ref_kinds = set(gate["required_evidence_ref_kinds"])
        missing_ref_kinds = sorted(required_ref_kinds - observed_ref_kinds)
        if missing_ref_kinds:
            unmet.append(
                f"missing immutable evidence artifact kinds: {missing_ref_kinds}"
            )
        raw_metrics = record.get("metrics")
        if isinstance(raw_metrics, dict):
            metrics = raw_metrics
        else:
            unmet.append("metrics must be an object")

        criteria: dict[str, str] = {}
        for key, predicate, description in _gate_metric_requirements(gate_id):
            criteria[key] = description
            value = metrics.get(key)
            if not predicate(value, metrics):
                unmet.append(f"{key} {description}; observed {value!r}")

        checks.append(
            Check(
                check_id=f"promotion.{gate_id}",
                status=PASS if not unmet else BLOCKED_EVIDENCE,
                summary=(
                    "dedicated Section K evidence satisfies this gate"
                    if not unmet
                    else "Section K promotion proof is incomplete"
                ),
                proof_kind="external_evidence",
                observed={
                    "evidence_kind": record.get("evidence_kind"),
                    "attested": record.get("attested"),
                    "evidence_refs": valid_refs,
                    "metrics": metrics,
                    "unmet": unmet,
                    "local_tests_are_not_evidence": True,
                },
                required={
                    "evidence_kind": gate["evidence_kind"],
                    "required_evidence_ref_kinds": gate[
                        "required_evidence_ref_kinds"
                    ],
                    "proof": gate["proof"],
                    "criteria": criteria,
                },
            )
        )
    return checks


def _status_from_checks(checks: Iterable[Check], *, not_run: bool = False) -> str:
    values = [check.status for check in checks]
    if not_run:
        return NOT_VERIFIED
    if any(value == FAIL for value in values):
        return FAIL
    if values and all(value == PASS for value in values):
        return PASS
    return NOT_VERIFIED


def _required_gate_ids(
    manifest: dict[str, Any],
    requested_phases: Sequence[str],
    *,
    require_canary: bool,
    require_capital_increase: bool,
) -> list[str]:
    gates = list(manifest["phase_promotion_gates"])
    aliases: dict[str, int] = {}
    for index, gate in enumerate(gates):
        aliases[str(gate["selector"])] = index
        aliases[str(gate["id"])] = index

    highest = -1
    for requested in requested_phases:
        if requested not in aliases:
            valid = ", ".join(sorted(aliases))
            raise ValueError(f"unknown --require-phase {requested!r}; choose from {valid}")
        highest = max(highest, aliases[requested])
    if require_canary:
        highest = max(highest, aliases["4"])
    if require_capital_increase:
        highest = len(gates) - 1
    return [str(gate["id"]) for gate in gates[: highest + 1]]


def _exit_code(
    *,
    safety_status: str,
    local_validation_status: str,
    implementation_status: str,
    promotion_checks: Sequence[Check],
    required_gate_ids: Sequence[str],
    require_implementation: bool,
    local_checks_requested: bool,
) -> int:
    if safety_status != PASS:
        return 1
    if local_checks_requested and local_validation_status != PASS:
        return 1
    implementation_is_required = require_implementation or bool(required_gate_ids)
    if implementation_is_required and implementation_status != PASS:
        return 1
    by_id = {
        check.check_id.removeprefix("promotion."): check.status
        for check in promotion_checks
    }
    if any(by_id.get(gate_id) != PASS for gate_id in required_gate_ids):
        return 1
    return 0


def _render_summary(report: VerificationReport) -> str:
    required = ", ".join(report.required_gate_ids) or "none (report-only)"
    promotion_lines = [
        f"  {check.check_id.removeprefix('promotion.')}: {check.status}"
        for check in report.checks
        if check.proof_kind == "external_evidence"
    ]
    return "\n".join(
        [
            f"Local validation: {report.local_validation_status}",
            f"Implementation coverage: {report.implementation_status}",
            f"Safety: {report.safety_status}",
            f"All promotion gates: {report.promotion_status}",
            f"Required gates: {required}",
            *promotion_lines,
        ]
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Report local implementation and Section K evidence without false-green inference."
    )
    parser.add_argument("--db", type=Path, default=ROOT / "state.db")
    parser.add_argument("--config", type=Path, default=ROOT / "live_config.json")
    parser.add_argument(
        "--manifest", type=Path, default=DEFAULT_CONTRACT_MANIFEST
    )
    parser.add_argument(
        "--external-evidence", type=Path, default=DEFAULT_EXTERNAL_EVIDENCE
    )
    parser.add_argument(
        "--fault-report",
        type=Path,
        default=ROOT / "verification_artifacts" / "phase1_fault_campaign.json",
    )
    parser.add_argument("--run-fault-traces", type=int, default=0)
    parser.add_argument("--fault-seed", type=int, default=20_260_718)
    parser.add_argument("--fault-workers", type=int, default=1)
    parser.add_argument(
        "--run-local-checks",
        action="store_true",
        help="actually run pytest, pyright, compileall, cargo fmt and cargo test",
    )
    parser.add_argument(
        "--require-implementation",
        action="store_true",
        help="exit nonzero unless complete mapped local contracts were run and passed",
    )
    parser.add_argument(
        "--require-phase",
        action="append",
        default=[],
        metavar="PHASE",
        help="require cumulative Section K gates through 0,1,2,3,4,canary (or a gate id)",
    )
    parser.add_argument(
        "--require-canary",
        action="store_true",
        help="require implementation and all gates through Phase 4 -> live canary",
    )
    parser.add_argument(
        "--require-capital-increase",
        action="store_true",
        help="require implementation, live-canary evidence, and canary -> more-capital gate",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "verification_artifacts" / "masterplan_verification.json",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        manifest = _load_contract_manifest(args.manifest)
        required_gate_ids = _required_gate_ids(
            manifest,
            args.require_phase,
            require_canary=args.require_canary,
            require_capital_increase=args.require_capital_increase,
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        parser.error(str(exc))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.run_fault_traces:
        campaign = run_parallel_execution_fault_campaign(
            traces=args.run_fault_traces,
            seed=args.fault_seed,
            workers=args.fault_workers,
        )
        args.fault_report.parent.mkdir(parents=True, exist_ok=True)
        args.fault_report.write_text(
            json.dumps(campaign.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    config = _load_json(args.config)
    db_metrics = _read_db_metrics(args.db)
    fault_report = _load_json(args.fault_report) if args.fault_report.exists() else None
    external_evidence = (
        _load_json(args.external_evidence)
        if args.external_evidence.exists()
        else None
    )

    pytest_junit = args.output.with_name(f"{args.output.stem}_pytest.xml")
    command_evidence: list[CommandEvidence] = []
    if args.run_local_checks:
        pytest_junit.unlink(missing_ok=True)
        for check_id, command, cwd in _local_commands(pytest_junit):
            command_evidence.append(_run_command(check_id, command, cwd))

    command_checks = [
        Check(
            check_id=evidence.check_id,
            status=evidence.status,
            summary="local validation command executed",
            proof_kind="local_command",
            observed={
                "return_code": evidence.return_code,
                "elapsed_seconds": evidence.elapsed_seconds,
            },
            required={"return_code": 0},
        )
        for evidence in command_evidence
    ]
    if not args.run_local_checks:
        command_checks.append(
            Check(
                check_id="local.commands",
                status=NOT_VERIFIED,
                summary="local validation commands were skipped",
                proof_kind="local_command",
                observed="not run",
                required="pass --run-local-checks",
            )
        )

    outcomes = (
        _pytest_outcomes_from_junit(pytest_junit) if args.run_local_checks else {}
    )
    rust_outcomes = next(
        (
            evidence.test_outcomes
            for evidence in command_evidence
            if evidence.check_id == "local.command.cargo_test"
        ),
        {},
    )
    implementation_checks = _implementation_checks(
        manifest,
        outcomes,
        local_checks_ran=args.run_local_checks,
        rust_outcomes=rust_outcomes,
    )
    safety_checks = _safety_checks(config)
    promotion_checks = _external_gate_checks(manifest, external_evidence)

    local_validation_status = _status_from_checks(
        command_checks,
        not_run=not args.run_local_checks,
    )
    implementation_status = _status_from_checks(
        [*command_checks, *implementation_checks],
        not_run=not args.run_local_checks,
    )
    safety_status = _status_from_checks(safety_checks)
    promotion_status = (
        PASS
        if promotion_checks and all(check.status == PASS for check in promotion_checks)
        else BLOCKED_EVIDENCE
    )

    report = VerificationReport(
        schema_version=2,
        generated_at=datetime.now(timezone.utc).isoformat(),
        repository_root=str(ROOT),
        contract_manifest={
            "path": str(args.manifest.resolve()),
            "sha256": _file_sha256(args.manifest),
            "schema_version": manifest["schema_version"],
            "implementation_phase_count": len(manifest["implementation_phases"]),
            "phase_gate_count": len(manifest["phase_promotion_gates"]),
            "failure_scenario_count": len(manifest["failure_injection_matrix"]),
        },
        local_validation_status=local_validation_status,
        implementation_status=implementation_status,
        safety_status=safety_status,
        promotion_status=promotion_status,
        required_gate_ids=required_gate_ids,
        checks=[
            *command_checks,
            *implementation_checks,
            *safety_checks,
            *promotion_checks,
        ],
        command_evidence=command_evidence,
        metrics={
            "database_observations_not_gate_evidence": db_metrics,
            "fault_campaign_observations_not_complete_gate_evidence": (
                fault_report or {"available": False}
            ),
            "external_evidence": {
                "available": external_evidence is not None,
                "path": str(args.external_evidence),
                "unit_tests_accepted_as_external_evidence": False,
            },
            "protected_capital_ceilings": PROTECTED_CAPITAL_CEILINGS,
        },
    )
    args.output.write_text(
        json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(_render_summary(report))
    print(f"Machine-readable report: {args.output.resolve()}")
    return _exit_code(
        safety_status=safety_status,
        local_validation_status=local_validation_status,
        implementation_status=implementation_status,
        promotion_checks=promotion_checks,
        required_gate_ids=required_gate_ids,
        require_implementation=args.require_implementation,
        local_checks_requested=args.run_local_checks,
    )


if __name__ == "__main__":
    raise SystemExit(main())
