"""Fail-closed verifier for the 2026 Bongus master execution plan.

This verifier deliberately separates locally provable implementation controls
from credentialed and elapsed-time evidence.  A green unit suite can never
manufacture a signed exchange snapshot, a 30-day NAV close, a blank-host
restore, or a 90-day forward research experiment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Sequence

PASS: Final = "PASS"
FAIL: Final = "FAIL"
BLOCKED: Final = "BLOCKED_EVIDENCE"


@dataclass(frozen=True, slots=True)
class Check:
    check_id: str
    status: str
    summary: str
    observed: Any
    required: Any


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key is forbidden: {key}")
        result[key] = value
    return result


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_reject_duplicate_json_keys,
        parse_constant=_reject_nonfinite_json,
    )
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _check(
    check_id: str,
    passed: bool,
    summary: str,
    observed: Any,
    required: Any,
    *,
    blocked: bool = False,
) -> Check:
    return Check(
        check_id=check_id,
        status=PASS if passed else (BLOCKED if blocked else FAIL),
        summary=summary,
        observed=observed,
        required=required,
    )


def _nested(value: dict[str, Any], *keys: str, default: Any = None) -> Any:
    current: Any = value
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def _float_or(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int_or(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _is_sha256(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


def _load_bound_evidence_artifact(
    evidence: dict[str, Any],
    evidence_kind: str,
    evidence_root: Path,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """Load one immutable, directory-confined external evidence artifact.

    External promotion evidence must be portable with its manifest.  Absolute
    paths, parent traversal, symlinks, missing files, hash mismatches, and
    generic JSON blobs all fail closed.  The returned observation is safe to
    include in the verifier report and deliberately excludes artifact content.
    """

    raw_ref = _nested(evidence, "artifacts", evidence_kind, default={})
    observed: dict[str, Any] = {
        "evidence_kind": evidence_kind,
        "reference": raw_ref,
        "reference_valid": False,
    }
    if not isinstance(raw_ref, dict) or set(raw_ref) != {"path", "sha256"}:
        observed["error"] = "reference must contain exactly path and sha256"
        return {}, observed, False
    relative_text = raw_ref.get("path")
    expected_sha256 = raw_ref.get("sha256")
    if not isinstance(relative_text, str) or not relative_text.strip():
        observed["error"] = "artifact path must be a non-empty relative path"
        return {}, observed, False
    if not _is_sha256(expected_sha256):
        observed["error"] = "artifact sha256 is malformed"
        return {}, observed, False

    relative_path = Path(relative_text)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        observed["error"] = "absolute paths and parent traversal are forbidden"
        return {}, observed, False

    try:
        root = evidence_root.resolve(strict=True)
        unresolved = root / relative_path
        cursor = root
        for part in relative_path.parts:
            cursor /= part
            if cursor.is_symlink():
                raise ValueError("symlinked evidence paths are forbidden")
        artifact_path = unresolved.resolve(strict=True)
        artifact_path.relative_to(root)
        if not artifact_path.is_file() or artifact_path.suffix.casefold() != ".json":
            raise ValueError("evidence artifact must be a JSON file")
        actual_sha256 = _sha256(artifact_path)
        if actual_sha256 != str(expected_sha256).casefold():
            raise ValueError("artifact sha256 mismatch")
        payload = _load_json(artifact_path)
        if payload.get("schema_version") != 1:
            raise ValueError("artifact schema_version must be 1")
        if payload.get("evidence_kind") != evidence_kind:
            raise ValueError("artifact evidence_kind does not match its manifest slot")
        metrics = payload.get("metrics")
        if not isinstance(metrics, dict):
            raise ValueError("artifact metrics must be a JSON object")
    except (OSError, RuntimeError, ValueError) as exc:
        observed["error"] = str(exc)
        return {}, observed, False

    observed.update(
        {
            "reference_valid": True,
            "resolved_relative_path": artifact_path.relative_to(root).as_posix(),
            "verified_sha256": actual_sha256,
            "artifact_schema_version": payload.get("schema_version"),
        }
    )
    return payload, observed, True


def _rust_function_body(source: str, signature: str) -> str:
    """Return one Rust function body for fail-closed source-order checks.

    The verifier is intentionally independent of the Rust compiler.  A small
    brace scanner is sufficient here because the selected functions are
    ordinary implementations and balanced braces inside strings do not change
    the final nesting depth.
    """

    signature_offset = source.find(signature)
    if signature_offset < 0:
        return ""
    body_offset = source.find("{", signature_offset)
    if body_offset < 0:
        return ""
    depth = 0
    for offset in range(body_offset, len(source)):
        character = source[offset]
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return source[body_offset : offset + 1]
    return ""


def _rust_lifecycle_observation(order_manager_source: str) -> dict[str, bool]:
    order_handler = _rust_function_body(order_manager_source, "async fn handle_ws_event")
    terminal_emitter = _rust_function_body(order_manager_source, "fn emit_cycle_order_update")
    chase_removal = _rust_function_body(order_manager_source, "fn remove_chase_state")

    order_persist = order_handler.find('persist_execution_state("order update and cumulative fill progress")')
    order_publish = order_handler.find("self.dash_tx.send(encoded)", order_persist + 1)
    terminal_persist = terminal_emitter.find('"terminal lifecycle durable before telemetry publication"')
    terminal_publish = terminal_emitter.find("self.dash_tx.send(vec)", terminal_persist + 1)
    required_tombstone_terms = {
        "terminal_tombstones",
        "terminal_sequence_watermark",
        "reconciliation_status",
        "retention_deadline_ms",
    }
    return {
        "order_update_state_before_publish": order_persist >= 0 and order_publish > order_persist,
        "terminal_state_before_publish": terminal_persist >= 0 and terminal_publish > terminal_persist,
        "durable_terminal_tombstones": all(term in order_manager_source for term in required_tombstone_terms),
        "symbol_scoped_persistence_latch": all(
            term in order_manager_source
            for term in (
                "symbol_persistence_latches",
                "latch_symbol_persistence_failure",
                "is_symbol_persistence_latched",
            )
        ),
        "chase_removal_checks_persistence": bool(chase_removal)
        and "let _ = self.persist_execution_state(context)" not in chase_removal,
    }


def _economic_accounting_observation(
    ledger_source: str,
    daily_report_source: str,
    reconciliation_evidence_source: str,
) -> dict[str, Any]:
    """Describe the locally provable A5.1/A5.2 accounting contract.

    This is intentionally stricter than checking for one ledger table or one
    report class.  The master plan requires an explicit economic taxonomy,
    lineage/provenance that can remain incomplete without being fabricated,
    the complete consolidated NAV equation, and distinct projected/finalized
    states.  Elapsed 30-day reconciliation evidence remains a separate
    ``BLOCKED_EVIDENCE`` gate.
    """

    required_event_types = {
        "FILL",
        "COMMISSION",
        "REALIZED_PNL",
        "FUNDING",
        "BORROW_INTEREST",
        "DEPOSIT",
        "WITHDRAWAL",
        "INTERNAL_TRANSFER",
        "STABLECOIN_CONVERSION",
        "RECONCILIATION_ADJUSTMENT",
    }
    required_envelope_fields = {
        "exchange_event_id",
        "cycle_id",
        "intent_id",
        "order_id",
        "exchange_fill_id",
        "venue",
        "account_id",
        "symbol",
        "quantity_asset",
        "amount_asset",
        "event_time",
        "availability_time",
        "code_hash",
        "config_hash",
        "schema_hash",
    }
    required_nav_components = {
        "opening_nav_usd",
        "closing_nav_usd",
        "external_deposits_usd",
        "external_withdrawals_usd",
        "realized_price_pnl_usd",
        "actual_funding_usd",
        "commission_cost_usd",
        "borrow_interest_cost_usd",
        "unrealized_pnl_change_usd",
        "stablecoin_fx_movement_usd",
        "internal_transfers_usd",
    }
    required_statuses = {"PNL_INCOMPLETE", "PROJECTED", "FINALIZED"}
    event_types = {name for name in required_event_types if f'{name} = "{name}"' in ledger_source}
    envelope_fields = {name for name in required_envelope_fields if name in ledger_source}
    nav_components = {name for name in required_nav_components if name in daily_report_source}
    statuses = {name for name in required_statuses if f'{name} = "{name}"' in daily_report_source}

    return {
        "economic_event_types": sorted(event_types),
        "provenance_envelope_fields": sorted(envelope_fields),
        "daily_nav_components": sorted(nav_components),
        "daily_nav_statuses": sorted(statuses),
        "exact_decimal_math": "Decimal(" in ledger_source and "Decimal(" in daily_report_source,
        "incomplete_envelope_blocks_finalization": (
            "incomplete_envelope_event_count" in ledger_source and "incomplete_ledger_envelopes" in daily_report_source
        ),
        "internal_transfers_net_zero": "internal_transfers_not_net_zero" in daily_report_source,
        "unknown_is_explicit": (
            'UNKNOWN = "UNKNOWN"' in reconciliation_evidence_source
            and "daily_nav_components_unknown" in daily_report_source
        ),
        "complete_event_taxonomy": event_types == required_event_types,
        "complete_provenance_envelope": envelope_fields == required_envelope_fields,
        "complete_daily_nav_equation": nav_components == required_nav_components,
        "complete_daily_nav_statuses": statuses == required_statuses,
    }


def _research_runtime_observation(root: Path) -> dict[str, Any]:
    """Exercise every isolated research CLI without network or credentials."""

    entrypoint_names = (
        "collect_binance_hyperliquid_shadow.py",
        "replay_binance_hyperliquid.py",
        "backtest_binance_hyperliquid.py",
        "report_binance_hyperliquid.py",
        "verify_cross_venue_dataset.py",
        "evaluate_binance_hyperliquid.py",
        "probe_cross_venue_region.py",
        "evaluate_cross_venue_regions.py",
        "screen_binance_hyperliquid_history.py",
    )
    boundary_path = root / "bongus" / "research" / "cross_venue" / "boundary.py"
    boundary_source = boundary_path.read_text(encoding="utf-8") if boundary_path.is_file() else ""
    # Sensitive names are deliberately split in boundary.py; compare against a
    # whitespace/case-normalized representation as well as the requirements
    # file so this check does not reward merely writing a forbidden literal.
    compact_boundary = "".join(boundary_source.casefold().split())
    requirements_path = root / "requirements-cross-venue.txt"
    requirements_text = requirements_path.read_text(encoding="utf-8") if requirements_path.is_file() else ""
    boundary_controls = {
        "credential_environment": '"binance_"+"api_key"' in compact_boundary,
        "signing_dependency": "eth_account" in boundary_source,
        "live_database": '"state"+".db"' in compact_boundary,
        "live_configuration": '"live_"+"config.json"' in compact_boundary,
        "trading_ipc_5555": 'int("555"+"5")' in compact_boundary,
        "telemetry_ipc_9000": 'int("900"+"0")' in compact_boundary,
        "mutation_endpoint": 'endswith("/"+"exchange")' in compact_boundary,
        "fixed_runtime_dependency": "pyarrow==23.0.1" in requirements_text,
    }

    scrubbed_environment = dict(os.environ)
    for key in (
        "BINANCE_API_KEY",
        "BINANCE_API_SECRET",
        "HYPERLIQUID_PRIVATE_KEY",
        "HYPERLIQUID_WALLET_PRIVATE_KEY",
        "TRADING_MODE",
    ):
        scrubbed_environment.pop(key, None)
    cli_results: dict[str, dict[str, Any]] = {}
    for name in entrypoint_names:
        path = root / "scripts" / name
        if not path.is_file():
            cli_results[name] = {"returncode": None, "usage": False, "error": "missing"}
            continue
        try:
            result = subprocess.run(
                [sys.executable, str(path), "--help"],
                cwd=root,
                env=scrubbed_environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            cli_results[name] = {
                "returncode": result.returncode,
                "usage": "usage:" in result.stdout.casefold(),
                "error": result.stderr.strip()[:500],
            }
        except (OSError, subprocess.SubprocessError) as exc:
            cli_results[name] = {
                "returncode": None,
                "usage": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

    return {
        "boundary_controls": boundary_controls,
        "entrypoints": cli_results,
        "all_boundary_controls": all(boundary_controls.values()),
        "all_entrypoints_directly_executable": all(
            result.get("returncode") == 0 and result.get("usage") is True for result in cli_results.values()
        ),
    }


def _execution_fault_campaign_check(root: Path) -> Check:
    evidence_dir = root / "verification_artifacts" / "evidence"
    candidates = sorted(evidence_dir.glob("execution_fault_campaign_*.json"))
    artifact = candidates[-1] if candidates else None
    try:
        evidence = _load_json(artifact) if artifact is not None else {}
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        evidence = {"load_error": f"{type(exc).__name__}: {exc}"}
    passed = (
        artifact is not None
        and evidence.get("passed") is True
        and _int_or(evidence.get("traces_requested"), 0) >= 1_000_000
        and _int_or(evidence.get("traces_completed"), 0) == _int_or(evidence.get("traces_requested"), -1)
        and _int_or(evidence.get("invariant_failures"), -1) == 0
        and _int_or(evidence.get("duplicate_exchange_effects"), -1) == 0
        and not str(evidence.get("first_failure") or "")
    )
    observed = {
        "artifact": str(artifact.relative_to(root)) if artifact is not None else None,
        "sha256": _sha256(artifact) if artifact is not None and artifact.is_file() else None,
        "passed": evidence.get("passed"),
        "traces_requested": evidence.get("traces_requested"),
        "traces_completed": evidence.get("traces_completed"),
        "invariant_failures": evidence.get("invariant_failures"),
        "duplicate_exchange_effects": evidence.get("duplicate_exchange_effects"),
        "first_failure": evidence.get("first_failure"),
        "load_error": evidence.get("load_error"),
    }
    return _check(
        "safety.local_million_trace_fault_campaign",
        passed,
        "deterministic crash/replay model completes at least one million traces without an invariant failure",
        observed,
        {
            "traces_min": 1_000_000,
            "completed_equals_requested": True,
            "invariant_failures": 0,
            "duplicate_exchange_effects": 0,
            "first_failure": "",
        },
    )


def _baseline_identity_check(root: Path) -> Check:
    baseline_path = root / "verification_artifacts" / "baseline_20260815_masterplan.json"
    try:
        baseline = _load_json(baseline_path)
        identity = baseline.get("baseline_identity") or {}
        source = identity.get("release_source_archive") or {}
        source_path = root / str(source.get("path") or "__missing__")
        manifests = sorted(
            (root / "verification_artifacts" / "evidence" / "baseline_20260815" / "backups").glob("*.manifest.json")
        )
        manifest_path = manifests[0] if len(manifests) == 1 else None
        manifest = _load_json(manifest_path) if manifest_path is not None else {}
        backup_path = (
            manifest_path.parent / str(manifest.get("backup_filename") or "__missing__")
            if manifest_path is not None
            else None
        )
        recorded_commands = {
            str(item.get("check_id") or "")
            for item in (baseline.get("command_evidence") or [])
            if isinstance(item, dict)
        }
        required_commands = {
            "local.command.pytest",
            "local.command.pyright",
            "local.command.cargo_fmt",
            "local.command.cargo_test",
            "local.command.cargo_clippy",
        }
        observed = {
            "git_commit": identity.get("git_commit"),
            "git_tree": identity.get("git_tree"),
            "entries_paused": identity.get("effective_entries_paused"),
            "source_archive_present": source_path.is_file(),
            "source_archive_sha256_matches": source_path.is_file() and _sha256(source_path) == source.get("sha256"),
            "database_backup_present": backup_path is not None and backup_path.is_file(),
            "database_size_matches_manifest": backup_path is not None
            and backup_path.is_file()
            and backup_path.stat().st_size == manifest.get("size_bytes"),
            "database_hash_recorded_consistently": manifest.get("sha256")
            == (identity.get("database") or {}).get("sha256"),
            "database_integrity": manifest.get("integrity_check"),
            "manifest_sha256_matches": manifest_path is not None
            and _sha256(manifest_path) == (identity.get("database") or {}).get("manifest_sha256"),
            "signed_snapshot_state_recorded": isinstance(identity.get("signed_exchange_snapshot"), dict),
            "missing_mandated_commands": sorted(required_commands - recorded_commands),
        }
        passed = (
            observed["git_commit"] == "7ee71fadbfdfbb946aff8bfe15bbe95bdf86f7ef"
            and observed["git_tree"] == "28f3934ae96fbfdc5ef52f7b4450534b9fd34312"
            and observed["entries_paused"] is True
            and observed["source_archive_present"] is True
            and observed["source_archive_sha256_matches"] is True
            and observed["database_backup_present"] is True
            and observed["database_size_matches_manifest"] is True
            and observed["database_hash_recorded_consistently"] is True
            and observed["database_integrity"] == "ok"
            and observed["manifest_sha256_matches"] is True
            and observed["signed_snapshot_state_recorded"] is True
            and not observed["missing_mandated_commands"]
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        observed = {"error_type": type(exc).__name__, "error": str(exc)}
        passed = False
    return _check(
        "baseline.reproducible_identity_backup_and_commands",
        passed,
        "baseline commit, release source, configuration, verified database manifest, "
        "exchange-snapshot status, and mandated commands are explicit",
        observed,
        {
            "git_commit": "7ee71fadbfdfbb946aff8bfe15bbe95bdf86f7ef",
            "entries_paused": True,
            "source_archive_sha256_matches": True,
            "database_backup_and_manifest_match": True,
            "database_integrity": "ok",
            "signed_snapshot_state_recorded": True,
            "missing_mandated_commands": [],
        },
    )


def implementation_checks(root: Path) -> list[Check]:
    live_config_path = root / "live_config.json"
    live_config = _load_json(live_config_path)
    trading_mode = os.environ.get("TRADING_MODE", "paper").strip().lower()
    checks = [
        _check(
            "lockdown.entries_paused",
            live_config.get("pause_new_entries") is True
            and live_config.get("live_approval_required") is True
            and not str(live_config.get("live_approval_artifact_path") or "").strip()
            and trading_mode != "live",
            "administrative entry lockdown remains active",
            {
                "pause_new_entries": live_config.get("pause_new_entries"),
                "live_approval_required": live_config.get("live_approval_required"),
                "approval_artifact_present": bool(str(live_config.get("live_approval_artifact_path") or "").strip()),
                "trading_mode": trading_mode,
            },
            {
                "pause_new_entries": True,
                "live_approval_required": True,
                "approval_artifact_present": False,
                "trading_mode": "paper_or_testnet",
            },
        ),
        _baseline_identity_check(root),
        _check(
            "freeze.manifest_and_pr_classification",
            (root / "ALPHA_FREEZE.md").is_file()
            and (root / "scripts" / "verify_pr_classification.py").is_file()
            and (root / ".github" / "pull_request_template.md").is_file(),
            "alpha freeze and classified-change controls exist",
            {
                "alpha_freeze": (root / "ALPHA_FREEZE.md").is_file(),
                "classification_verifier": (root / "scripts" / "verify_pr_classification.py").is_file(),
                "pull_request_template": (root / ".github" / "pull_request_template.md").is_file(),
            },
            "all controls present",
        ),
    ]

    protocol_source = (root / "bongus" / "ipc" / "protocol.py").read_text(encoding="utf-8")
    rust_protocol_source = (root / "execution_engine" / "src" / "ipc.rs").read_text(encoding="utf-8")
    exact_quantity_terms = {
        "requested_quantity_decimal",
        "actual_spot_inventory_decimal",
        "actual_futures_inventory_decimal",
        "exit_spot_quantity_decimal",
        "exit_futures_quantity_decimal",
    }
    route_active_python = (
        'ACTIVE_ROUTE_POLICIES = frozenset({"legacy_dual_maker", "emergency_reduce_only"})' in protocol_source
    )
    route_active_rust = (
        'route_policy != "legacy_dual_maker" && route_policy != "emergency_reduce_only"' in rust_protocol_source
        or '"legacy_dual_maker" | "emergency_reduce_only"' in rust_protocol_source
    )
    exact_contract = all(term in protocol_source and term in rust_protocol_source for term in exact_quantity_terms)
    checks.append(
        _check(
            "execution.protocol_exact_exit_only_emergency",
            route_active_python and route_active_rust and exact_contract,
            "cross-language protocol carries exact exposure semantics and an exit-only emergency route",
            {
                "python_emergency_active": route_active_python,
                "rust_emergency_active": route_active_rust,
                "exact_quantity_fields": sorted(
                    term for term in exact_quantity_terms if term in protocol_source and term in rust_protocol_source
                ),
            },
            {
                "python_emergency_active": True,
                "rust_emergency_active": True,
                "exact_quantity_fields": sorted(exact_quantity_terms),
            },
        )
    )

    state_store_source = (root / "bongus" / "engine" / "state_store.py").read_text(encoding="utf-8")
    subscriber_source = (root / "bongus" / "market_data" / "rust_data_subscriber.py").read_text(encoding="utf-8")
    checks.append(
        _check(
            "persistence.raw_before_ack_and_no_destructive_lifecycle_delete",
            "append_durable_telemetry_receipt" in state_store_source
            and "enqueue_projection" in subscriber_source
            and "DELETE FROM pending_intents" not in state_store_source
            and "EXCHANGE_FLAT_AWAITING_TERMINAL" in state_store_source,
            "critical telemetry is raw-durable before ACK and lifecycle completion uses tombstones",
            {
                "durable_receipt": "append_durable_telemetry_receipt" in state_store_source,
                "ordered_projection_enqueue": "enqueue_projection" in subscriber_source,
                "destructive_pending_delete": "DELETE FROM pending_intents" in state_store_source,
                "terminal_tombstone": "EXCHANGE_FLAT_AWAITING_TERMINAL" in state_store_source,
            },
            {
                "durable_receipt": True,
                "ordered_projection_enqueue": True,
                "destructive_pending_delete": False,
                "terminal_tombstone": True,
            },
        )
    )

    trader_source = (root / "scripts" / "live_trader_v2.py").read_text(encoding="utf-8")
    decay_disabled = (
        float(live_config.get("hwm_auto_decay_after_hours") or 0.0) == 0.0
        and float(live_config.get("hwm_auto_decay_fraction") or 0.0) == 0.0
        and "_maybe_auto_decay_equity_high_watermark" not in trader_source
    )
    checks.append(
        _check(
            "pnl.no_passive_high_watermark_decay",
            decay_disabled,
            "high-water marks cannot decay merely because time passes",
            {
                "after_hours": live_config.get("hwm_auto_decay_after_hours"),
                "fraction": live_config.get("hwm_auto_decay_fraction"),
                "auto_decay_code_present": "_maybe_auto_decay_equity_high_watermark" in trader_source,
            },
            {"after_hours": 0.0, "fraction": 0.0, "auto_decay_code_present": False},
        )
    )

    endpoint_path = root / "config" / "binance_endpoints_v1.json"
    endpoint_matrix = _load_json(endpoint_path)
    environments = endpoint_matrix.get("environments") or {}
    endpoint_ok = (
        endpoint_matrix.get("schema_version") == 1
        and 0 < int(endpoint_matrix.get("planned_connection_max_age_seconds") or 0) < 86_400
        and set(environments) == {"mainnet", "testnet"}
        and all(
            str(environments[environment]["futures"][field]).endswith(suffix)
            for environment in ("mainnet", "testnet")
            for field, suffix in (
                ("public_stream_ws_base_url", "/public"),
                ("market_stream_ws_base_url", "/market"),
                ("private_ws_base_url", "/private"),
            )
        )
    )
    checks.append(
        _check(
            "connectivity.shared_routed_endpoint_matrix",
            endpoint_ok,
            "shared Binance matrix uses routed futures feeds and planned renewal",
            {
                "sha256": _sha256(endpoint_path),
                "schema_version": endpoint_matrix.get("schema_version"),
                "planned_connection_max_age_seconds": endpoint_matrix.get("planned_connection_max_age_seconds"),
                "environments": sorted(environments),
            },
            {"environments": ["mainnet", "testnet"], "routed": True, "max_age_lt_24h": True},
        )
    )

    order_manager_source = (root / "execution_engine" / "src" / "order_manager.rs").read_text(encoding="utf-8")
    rest_source = (root / "execution_engine" / "src" / "binance_rest.rs").read_text(encoding="utf-8")
    rust_lifecycle = _rust_lifecycle_observation(order_manager_source)
    checks.append(
        _check(
            "persistence.rust_authoritative_lifecycle_and_symbol_latches",
            all(rust_lifecycle.values()),
            "Rust persists private and terminal lifecycle state before publication, retains terminal lineage, "
            "and fail-closes persistence errors per symbol",
            rust_lifecycle,
            {name: True for name in rust_lifecycle},
        )
    )
    required_risk_states = {
        "NORMAL",
        "ENTRY_FROZEN",
        "CANCELING_ENTRIES",
        "RECONCILING",
        "DERISKING",
        "MANUAL_REVIEW",
    }
    observed_risk_states = {state for state in required_risk_states if f'"{state}"' in order_manager_source}
    quota_and_clock_terms = {
        "request_weight": "reserved_request_weight",
        "order_count": "reserved_order_count",
        "shed_at_70pct": "NONESSENTIAL_SHED_UTILIZATION_BPS: u64 = 7_000",
        "block_at_85pct": "ENTRY_BLOCK_UTILIZATION_BPS: u64 = 8_500",
        "clock_warn_100ms": "CLOCK_WARN_OFFSET_MS: i64 = 100",
        "clock_block_250ms": "CLOCK_BLOCK_OFFSET_MS: i64 = 250",
        "ambiguous_503": "AmbiguousServerResult",
        "rate_limit_429": "RateLimited",
        "ip_ban_418": "IpBanned",
    }
    observed_quota_clock = {name: term in rest_source for name, term in quota_and_clock_terms.items()}
    active_risk_ok = (
        observed_risk_states == required_risk_states
        and "reevaluate_continuous_risk" in order_manager_source
        and "activate_continuous_risk" in order_manager_source
        and "continuous risk retained canceled entry" in order_manager_source
        and "continuous_risk_state" in order_manager_source
        and all(observed_quota_clock.values())
    )
    checks.append(
        _check(
            "risk.continuous_actor_quota_reserve_and_clock_gate",
            active_risk_ok,
            "Rust continuously persists and enforces entry cancellation, API "
            "reserves, and clock gates while preserving exits",
            {
                "risk_states": sorted(observed_risk_states),
                "reevaluates": "reevaluate_continuous_risk" in order_manager_source,
                "cancels_entry_chases": "continuous risk retained canceled entry" in order_manager_source,
                "persists_state": "continuous_risk_state" in order_manager_source,
                **observed_quota_clock,
            },
            {
                "risk_states": sorted(required_risk_states),
                "reevaluates": True,
                "cancels_entry_chases": True,
                "persists_state": True,
                **{name: True for name in quota_and_clock_terms},
            },
        )
    )

    user_stream_source = (root / "execution_engine" / "src" / "user_data_ws.rs").read_text(encoding="utf-8")
    required_account_truth_terms = {
        "wallet_balance",
        "available_balance",
        "positions",
        "leverage",
        "maintenance_margin",
        "margin_ratio",
        "liquidation_price",
        "position_mode",
        "open_orders",
        "borrow_state",
    }
    account_truth_sources = user_stream_source + order_manager_source
    observed_account_truth_terms = {term for term in required_account_truth_terms if term in account_truth_sources}
    python_account_truth_source = (root / "bongus" / "engine" / "account_truth.py").read_text(encoding="utf-8")
    live_trader_source = (root / "scripts" / "live_trader_v2.py").read_text(encoding="utf-8")
    python_account_controls = {
        "venue_separated": (
            "standard_spot" in python_account_truth_source and "usd_m_futures" in python_account_truth_source
        ),
        "float_evidence_rejected": "isinstance(value, (bool, float))" in python_account_truth_source,
        "unknown_and_stale_explicit": (
            '"UNKNOWN"' in python_account_truth_source and '"STALE"' in python_account_truth_source
        ),
        "content_hash_persisted": (
            "CREATE TABLE IF NOT EXISTS account_truth_snapshots" in state_store_source
            and "raw_snapshot_json" in state_store_source
            and "content_hash" in state_store_source
        ),
        "fresh_entry_gate": (
            "_fresh_account_truth_ready" in live_trader_source and 'self._trading_mode != "paper"' in live_trader_source
        ),
    }
    account_topology_ok = (
        all(python_account_controls.values())
        and observed_account_truth_terms == required_account_truth_terms
        and "/papi" not in rest_source
        and "directional_risk =" not in order_manager_source
    )
    checks.append(
        _check(
            "risk.standard_spot_and_usdm_account_truth",
            account_topology_ok,
            "runtime account truth separates Standard Spot and USD-M margin risk and quarantines Portfolio Margin",
            {
                "account_truth_fields": sorted(observed_account_truth_terms),
                "portfolio_margin_paths_present": "/papi" in rest_source,
                "spot_perp_netting_formula_present": "directional_risk =" in order_manager_source,
                "python_account_controls": python_account_controls,
            },
            {
                "account_truth_fields": sorted(required_account_truth_terms),
                "portfolio_margin_paths_present": False,
                "spot_perp_netting_formula_present": False,
                "python_account_controls": {name: True for name in python_account_controls},
            },
        )
    )

    daily_report_source = (root / "bongus" / "supervisor" / "daily_report.py").read_text(encoding="utf-8")
    economic_ledger_source = (root / "bongus" / "engine" / "economic_ledger.py").read_text(encoding="utf-8")
    reconciliation_evidence_source = (root / "bongus" / "testing" / "daily_reconciliation_evidence.py").read_text(
        encoding="utf-8"
    )
    accounting = _economic_accounting_observation(
        economic_ledger_source,
        daily_report_source,
        reconciliation_evidence_source,
    )
    accounting_boolean_requirements = {
        "exact_decimal_math",
        "incomplete_envelope_blocks_finalization",
        "internal_transfers_net_zero",
        "unknown_is_explicit",
        "complete_event_taxonomy",
        "complete_provenance_envelope",
        "complete_daily_nav_equation",
        "complete_daily_nav_statuses",
    }
    checks.append(
        _check(
            "pnl.economic_ledger_and_daily_nav_contract",
            all(accounting[name] is True for name in accounting_boolean_requirements),
            "economic lineage, exact cashflows, UNKNOWN handling, and the complete daily NAV equation are explicit",
            accounting,
            {
                "economic_event_types": sorted(
                    {
                        "FILL",
                        "COMMISSION",
                        "REALIZED_PNL",
                        "FUNDING",
                        "BORROW_INTEREST",
                        "DEPOSIT",
                        "WITHDRAWAL",
                        "INTERNAL_TRANSFER",
                        "STABLECOIN_CONVERSION",
                        "RECONCILIATION_ADJUSTMENT",
                    }
                ),
                "provenance_envelope_complete": True,
                "daily_nav_equation_complete": True,
                "daily_nav_statuses": ["FINALIZED", "PNL_INCOMPLETE", "PROJECTED"],
                "all_boolean_controls": True,
            },
        )
    )
    required_markouts = {'"1s"', '"5s"', '"30s"', '"300s"', '"settlement"'}
    required_funnel_stages = {
        "observed": '"observed"',
        "data_complete": '"data_complete"',
        "common_quantity_possible": '"common_qty"',
        "sufficient_depth": '"depth"',
        "positive_after_costs": '"positive_cost"',
        "risk_approved": '"risk"',
        "sent": '"sent"',
        "acknowledged": '"ack"',
        "filled": '"filled"',
        "funded": '"funded"',
        "closed": '"closed"',
        "reconciled": '"reconciled"',
    }
    tca_ok = (
        "CREATE TABLE IF NOT EXISTS execution_tca_intents" in state_store_source
        and "CREATE TABLE IF NOT EXISTS execution_tca_legs" in state_store_source
        and "CREATE TABLE IF NOT EXISTS opportunity_funnel_events" in state_store_source
        and all(horizon in daily_report_source for horizon in required_markouts)
        and all(term in state_store_source for term in required_funnel_stages.values())
        and "PNL_INCOMPLETE" in daily_report_source
    )
    checks.append(
        _check(
            "pnl.normalized_tca_complete_funnel_and_unknown_accounting",
            tca_ok,
            "exact per-leg TCA, every required funnel stage, all markout horizons, "
            "and incomplete-PnL semantics are present",
            {
                "tca_intents": "execution_tca_intents" in state_store_source,
                "tca_legs": "execution_tca_legs" in state_store_source,
                "opportunity_funnel": "opportunity_funnel_events" in state_store_source,
                "markout_horizons": sorted(
                    horizon.strip('"') for horizon in required_markouts if horizon in daily_report_source
                ),
                "funnel_stages": sorted(
                    stage for stage, term in required_funnel_stages.items() if term in state_store_source
                ),
                "pnl_incomplete": "PNL_INCOMPLETE" in daily_report_source,
            },
            {
                "tca_intents": True,
                "tca_legs": True,
                "opportunity_funnel": True,
                "markout_horizons": ["1s", "5s", "30s", "300s", "settlement"],
                "funnel_stages": sorted(required_funnel_stages),
                "pnl_incomplete": True,
            },
        )
    )

    prereg_path = root / "research" / "experiments" / "binance_hyperliquid_v1.json"
    prereg = _load_json(prereg_path)
    required_stresses = {
        "fees_plus_5bp",
        "fees_plus_10bp",
        "slippage_x1_5",
        "slippage_x2",
        "second_leg_delay_300ms",
        "second_leg_delay_1s",
        "second_leg_delay_5s",
        "funding_haircut_25pct",
        "funding_haircut_50pct",
        "funding_sign_reversal",
        "missed_funding",
        "exit_depth_reduced_50pct",
        "exit_depth_reduced_90pct",
        "usdc_usdt_deviation_0_5pct",
        "usdc_usdt_deviation_1pct",
        "usdc_usdt_deviation_5pct",
        "binance_outage_1h",
        "binance_outage_8h",
        "binance_outage_24h",
        "hyperliquid_outage_1h",
        "hyperliquid_outage_8h",
        "hyperliquid_outage_24h",
        "underlying_move_plus_30pct",
        "underlying_move_minus_30pct",
        "cross_venue_basis_widening",
        "delisting",
        "open_interest_cap",
        "adl",
        "liquidation",
        "worse_leg_execution_order",
    }
    stresses = set(prereg.get("sensitivity_grid") or [])
    research_files = [
        root / "scripts" / "verify_cross_venue_dataset.py",
        root / "scripts" / "evaluate_binance_hyperliquid.py",
        root / "deployment" / "bongus-research.service.in",
    ]
    collector_source = (root / "scripts" / "collect_binance_hyperliquid_shadow.py").read_text(encoding="utf-8")
    research_service_source = research_files[-1].read_text(encoding="utf-8")
    cross_venue_requirements = (root / "requirements-cross-venue.txt").read_text(encoding="utf-8")
    pyarrow_declared = any(
        line.strip().casefold().startswith("pyarrow")
        for line in cross_venue_requirements.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    parquet_runtime_wired = (
        "ParquetArtifactWriter" in collector_source
        and ("--artifact-root" in collector_source or "--dataset-root" in collector_source)
        and ("--artifact-root" in research_service_source or "--dataset-root" in research_service_source)
        and pyarrow_declared
    )
    standalone_service_invocation = (
        "sys.path.insert" in collector_source
        or " -m scripts.collect_binance_hyperliquid_shadow" in research_service_source
    )
    research_runtime = _research_runtime_observation(root)
    research_ok = (
        prereg.get("status") == "frozen_before_forward_oos"
        and set(prereg.get("universe") or []) == {"BTC", "ETH", "SOL", "XRP", "DOGE"}
        and required_stresses.issubset(stresses)
        and all(path.is_file() for path in research_files)
        and parquet_runtime_wired
        and standalone_service_invocation
        and research_runtime["all_boundary_controls"] is True
        and research_runtime["all_entrypoints_directly_executable"] is True
    )
    checks.append(
        _check(
            "research.preregistered_isolated_evidence_stack",
            research_ok,
            "fixed-universe read-only experiment has the complete preregistered evidence surface",
            {
                "preregistration_sha256": _sha256(prereg_path),
                "universe": prereg.get("universe"),
                "missing_stresses": sorted(required_stresses - stresses),
                "missing_files": [str(path.relative_to(root)) for path in research_files if not path.is_file()],
                "parquet_runtime_wired": parquet_runtime_wired,
                "standalone_service_invocation": standalone_service_invocation,
                "boundary_controls": research_runtime["boundary_controls"],
                "entrypoints": research_runtime["entrypoints"],
                "all_boundary_controls": research_runtime["all_boundary_controls"],
                "all_entrypoints_directly_executable": research_runtime["all_entrypoints_directly_executable"],
            },
            {
                "universe": ["BTC", "ETH", "SOL", "XRP", "DOGE"],
                "missing_stresses": [],
                "missing_files": [],
                "parquet_runtime_wired": True,
                "standalone_service_invocation": True,
                "all_boundary_controls": True,
                "all_entrypoints_directly_executable": True,
            },
        )
    )

    service_path = root / "deployment" / "bongus.service.in"
    service_text = service_path.read_text(encoding="utf-8")
    slice_path = root / "deployment" / "bongus.slice.in"
    slice_text = slice_path.read_text(encoding="utf-8") if slice_path.is_file() else ""
    ops_files = [
        root / "deployment" / "bongus-ops-health.service.in",
        root / "deployment" / "bongus-ops-health.timer.in",
        root / "deployment" / "bongus-backup.service.in",
        root / "deployment" / "bongus-backup.timer.in",
        root / "deployment" / "bongus-offsite-backup.service.in",
        root / "scripts" / "check_operational_health.py",
        root / "scripts" / "upload_verified_offsite_backup.py",
    ]
    health_service_text = ops_files[0].read_text(encoding="utf-8") if ops_files[0].is_file() else ""
    health_timer_text = ops_files[1].read_text(encoding="utf-8") if ops_files[1].is_file() else ""
    backup_service_text = ops_files[2].read_text(encoding="utf-8") if ops_files[2].is_file() else ""
    backup_timer_text = ops_files[3].read_text(encoding="utf-8") if ops_files[3].is_file() else ""
    offsite_service_text = ops_files[4].read_text(encoding="utf-8") if ops_files[4].is_file() else ""
    offsite_uploader_text = ops_files[6].read_text(encoding="utf-8") if ops_files[6].is_file() else ""
    release_manifest_text = (root / "scripts" / "release_manifest.py").read_text(encoding="utf-8")
    backup_source = (root / "bongus" / "engine" / "database_backup.py").read_text(encoding="utf-8")
    runbook_source = (root / "RUNBOOK.md").read_text(encoding="utf-8")
    ops_controls = {
        "systemd_authoritative": "systemd unit is the sole authoritative production entry point" in runbook_source,
        "memory_high_3gb": "MemoryHigh=3000000000" in service_text,
        "memory_max_3_5gb": "MemoryMax=3500000000" in service_text,
        "release_memory_contract_3_5gb": ("TOTAL_RUNTIME_MEMORY_MAX_BYTES = 3_500_000_000" in release_manifest_text),
        "aggregate_slice_memory_contract": (
            "MemoryHigh=3200000000" in slice_text
            and "MemoryMax=3500000000" in slice_text
            and "MemorySwapMax=0" in slice_text
            and all(
                "Slice=@SERVICE_NAME@.slice" in unit_text
                for unit_text in (
                    service_text,
                    health_service_text,
                    backup_service_text,
                    offsite_service_text,
                )
            )
        ),
        "swap_disabled": "MemorySwapMax=0" in service_text,
        "health_every_60s": "OnUnitActiveSec=60s" in health_timer_text,
        "verified_backup_every_10m": (
            "OnCalendar=*-*-* *:00/10:00" in backup_timer_text
            and "-m scripts.create_verified_backup_set create" in backup_service_text
            and "Persistent=true" in backup_timer_text
        ),
        "backup_age_checked": "--max-backup-age-seconds 900" in health_service_text,
        "offsite_age_checked": "--max-offsite-age-seconds 900" in health_service_text,
        "encrypted_offsite_handoff": (
            "EnvironmentFile=/etc/bongus/offsite-backup.env" in offsite_service_text
            and "RESTIC_PASSWORD_FILE" in offsite_uploader_text
            and "inline RESTIC_PASSWORD is forbidden" in offsite_uploader_text
            and "local filesystems cannot satisfy encrypted offsite backup" in offsite_uploader_text
        ),
        "heartbeat_two_miss_window": "--max-heartbeat-age-seconds 125" in health_service_text,
        "clock_warn_100ms": "--clock-warning-offset-ms 100" in health_service_text,
        "clock_block_250ms": "--clock-critical-offset-ms 250" in health_service_text,
        "backup_budget_8gb": "DEFAULT_BACKUP_BUDGET_BYTES = 8_000_000_000" in backup_source,
        "backup_headroom_20gb": "--required-headroom-bytes 20000000000" in backup_service_text,
        "backup_tree_peak_20_5gb": "--backup-tree-budget-bytes 20500000000" in backup_service_text,
        "passive_hwm_decay_forbidden": (
            '"hwm_auto_decay_after_hours": 0.0' in runbook_source and '"hwm_auto_decay_fraction": 0.0' in runbook_source
        ),
    }
    ops_ok = all(ops_controls.values()) and all(path.is_file() for path in ops_files) and slice_path.is_file()
    checks.append(
        _check(
            "operations.systemd_resource_and_health_controls",
            ops_ok,
            "systemd owns production with bounded memory and independent health probes",
            {
                "service_sha256": _sha256(service_path),
                "missing_files": [str(path.relative_to(root)) for path in ops_files if not path.is_file()],
                **ops_controls,
            },
            {"all_operational_controls": True, "missing_files": []},
        )
    )
    checks.append(_execution_fault_campaign_check(root))
    return checks


def evidence_checks(
    evidence: dict[str, Any],
    *,
    evidence_root: Path | None = None,
) -> list[Check]:
    artifact_root = (evidence_root or Path.cwd()).resolve()
    artifact_payloads: dict[str, dict[str, Any]] = {}
    artifact_observations: dict[str, dict[str, Any]] = {}
    artifact_validity: dict[str, bool] = {}
    for evidence_kind in (
        "signed_testnet",
        "safety_window",
        "operations",
        "region_probe",
        "research_forward",
    ):
        payload, observation, valid = _load_bound_evidence_artifact(
            evidence,
            evidence_kind,
            artifact_root,
        )
        artifact_payloads[evidence_kind] = payload
        artifact_observations[evidence_kind] = observation
        artifact_validity[evidence_kind] = valid

    measured_evidence = dict(evidence)
    for evidence_kind, payload in artifact_payloads.items():
        metrics = payload.get("metrics")
        measured_evidence[evidence_kind] = metrics if isinstance(metrics, dict) else {}
    evidence = measured_evidence
    policy_ok = (
        evidence.get("schema_version") == 2
        and _nested(evidence, "policy", "live_entries_resumed") is False
        and _nested(evidence, "policy", "local_tests_are_promotion_evidence") is False
    )
    checks = [
        _check(
            "evidence.policy_never_infers_live_authority",
            policy_ok,
            "evidence uses the current schema and cannot infer live permission from local tests",
            {
                "schema_version": evidence.get("schema_version"),
                "live_entries_resumed": _nested(evidence, "policy", "live_entries_resumed"),
                "local_tests_are_promotion_evidence": _nested(evidence, "policy", "local_tests_are_promotion_evidence"),
            },
            {
                "schema_version": 2,
                "live_entries_resumed": False,
                "local_tests_are_promotion_evidence": False,
            },
            blocked=True,
        )
    ]
    signed_scenarios = set(_nested(evidence, "signed_testnet", "passed_scenarios", default=[]) or [])
    required_signed = {
        "zero_fill",
        "equal_partial",
        "unilateral_fill",
        "cancel_fill_race",
        "503_unknown",
        "429_418",
        "lifecycle_process_death",
        "dynamic_symbol_reconnect",
        "clock_skew",
    }
    signed_ok = (
        artifact_validity["signed_testnet"]
        and _nested(evidence, "signed_testnet", "authorized") is True
        and _nested(evidence, "signed_testnet", "withdrawals_disabled") is True
        and _nested(evidence, "signed_testnet", "current_account_snapshot") is True
        and required_signed.issubset(signed_scenarios)
        and _int_or(_nested(evidence, "signed_testnet", "orphan_orders"), -1) == 0
        and _int_or(_nested(evidence, "signed_testnet", "duplicate_positions"), -1) == 0
    )
    checks.append(
        _check(
            "evidence.signed_testnet_fault_campaign",
            signed_ok,
            "credentialed demo/testnet fault campaign proves exchange behavior",
            {
                "authorized": _nested(evidence, "signed_testnet", "authorized"),
                "withdrawals_disabled": _nested(evidence, "signed_testnet", "withdrawals_disabled"),
                "current_account_snapshot": _nested(evidence, "signed_testnet", "current_account_snapshot"),
                "missing_scenarios": sorted(required_signed - signed_scenarios),
                "orphan_orders": _nested(evidence, "signed_testnet", "orphan_orders"),
                "duplicate_positions": _nested(evidence, "signed_testnet", "duplicate_positions"),
                "artifact": artifact_observations["signed_testnet"],
            },
            {
                "authorized": True,
                "withdrawals_disabled": True,
                "missing_scenarios": [],
                "orphan_orders": 0,
                "duplicate_positions": 0,
            },
            blocked=True,
        )
    )

    safety_ok = (
        artifact_validity["safety_window"]
        and _float_or(_nested(evidence, "safety_window", "consecutive_unattended_days"), 0.0) >= 7
        and _int_or(
            _nested(evidence, "safety_window", "consecutive_reconciled_utc_closes"),
            0,
        )
        >= 30
        and _int_or(_nested(evidence, "safety_window", "unresolved_lifecycle_events"), -1) == 0
        and _int_or(_nested(evidence, "safety_window", "unexplained_restarts"), -1) == 0
        and _int_or(_nested(evidence, "safety_window", "alpha_changes"), -1) == 0
        and _float_or(_nested(evidence, "safety_window", "mainnet_public_paper_days"), 0.0) >= 7
        and _int_or(_nested(evidence, "safety_window", "fault_terminal_unknown_states"), -1) == 0
        and _int_or(_nested(evidence, "safety_window", "untracked_orders_after_deadlines"), -1) == 0
        and _nested(evidence, "safety_window", "storage_full_and_fsync_campaign_passed") is True
        and _nested(evidence, "safety_window", "callback_rest_stall_campaign_passed") is True
        and _nested(evidence, "safety_window", "independent_stream_failure_campaign_passed") is True
        and _nested(evidence, "safety_window", "restart_full_replay_campaign_passed") is True
    )
    checks.append(
        _check(
            "evidence.seven_and_thirty_day_safety_window",
            safety_ok,
            "unattended operation and daily NAV evidence satisfy elapsed-time gates",
            {
                "artifact": artifact_observations["safety_window"],
                "metrics": evidence.get("safety_window", {}),
            },
            {
                "consecutive_unattended_days": 7,
                "consecutive_reconciled_utc_closes": 30,
                "mainnet_public_paper_days": 7,
                "all_incident_counts": 0,
                "all_fault_campaigns": True,
            },
            blocked=True,
        )
    )

    ops_ok = (
        artifact_validity["operations"]
        and _float_or(_nested(evidence, "operations", "backup_rpo_minutes"), 1e9) <= 15
        and _float_or(_nested(evidence, "operations", "restore_rto_minutes"), 1e9) <= 60
        and _nested(evidence, "operations", "blank_host_restore") is True
        and _nested(evidence, "operations", "encrypted_offsite_backup") is True
        and _nested(evidence, "operations", "independent_heartbeat_paging") is True
        and _nested(evidence, "operations", "chrony_entry_block_test") is True
        and _nested(evidence, "operations", "linux_systemd_verify") is True
        and _float_or(_nested(evidence, "operations", "free_disk_fraction"), 0.0) > 0.30
        and _float_or(_nested(evidence, "operations", "trading_vps_soak_days"), 0.0) >= 7
        and _float_or(_nested(evidence, "operations", "total_rss_p99_fraction"), 1.0) < 0.70
        and _float_or(_nested(evidence, "operations", "cpu_p95_fraction"), 1.0) < 0.60
        and _int_or(_nested(evidence, "operations", "oom_or_restart_count"), -1) == 0
        and _nested(evidence, "operations", "sqlite_write_latency_stable") is True
        and _nested(evidence, "operations", "research_isolated_host_or_disk_quota") is True
        and _nested(evidence, "operations", "research_daily_hashed_offsite_upload") is True
        and _nested(evidence, "operations", "monthly_restore_drill") is True
        and _nested(evidence, "operations", "quarterly_blank_host_recovery") is True
    )
    checks.append(
        _check(
            "evidence.operational_restore_clock_and_monitoring",
            ops_ok,
            "offsite backup, clean-host restore, clock, paging, systemd, and disk gates pass",
            {
                "artifact": artifact_observations["operations"],
                "metrics": evidence.get("operations", {}),
            },
            {
                "backup_rpo_minutes_max": 15,
                "restore_rto_minutes_max": 60,
                "free_disk_fraction_min_exclusive": 0.30,
                "trading_vps_soak_days_min": 7,
                "total_rss_p99_fraction_max_exclusive": 0.70,
                "cpu_p95_fraction_max_exclusive": 0.60,
                "oom_or_restart_count": 0,
                "all_boolean_gates": True,
            },
            blocked=True,
        )
    )

    region_ok = (
        artifact_validity["region_probe"]
        and _float_or(_nested(evidence, "region_probe", "duration_hours"), 0.0) >= 48
        and set(_nested(evidence, "region_probe", "regions", default=[]) or []) >= {"germany", "france"}
        and bool(_nested(evidence, "region_probe", "selected_by_worst_venue_p99"))
        and set(_nested(evidence, "region_probe", "metric_families", default=[]) or [])
        >= {
            "rest_rtt",
            "ws_event_age",
            "p50_p95_p99",
            "jitter_packet_loss",
            "reconnect_gap_recovery",
        }
        and _is_sha256(_nested(evidence, "region_probe", "artifact_sha256"))
    )
    checks.append(
        _check(
            "evidence.region_probe",
            region_ok,
            "Germany and France were compared by worst-venue p99",
            {
                "artifact": artifact_observations["region_probe"],
                "metrics": evidence.get("region_probe", {}),
            },
            {
                "duration_hours_min": 48,
                "regions": ["germany", "france"],
                "selected_by_worst_venue_p99": True,
                "all_metric_families": True,
                "artifact_sha256": "64 lowercase hexadecimal characters",
            },
            blocked=True,
        )
    )

    verdict = str(_nested(evidence, "research_forward", "verdict", default=""))
    estimate = _float_or(_nested(evidence, "research_forward", "simple_annualized_estimate"), float("nan"))
    lower_bound = _float_or(_nested(evidence, "research_forward", "one_sided_95_lcb"), float("nan"))
    max_drawdown = _float_or(_nested(evidence, "research_forward", "max_drawdown"), float("nan"))
    viable_robustness = (
        _nested(evidence, "research_forward", "positive_vs_no_trade") is True
        and _nested(evidence, "research_forward", "positive_vs_binance_only") is True
        and _nested(evidence, "research_forward", "positive_leave_one_symbol_out") is True
        and _nested(evidence, "research_forward", "positive_leave_one_month_out") is True
        and _float_or(
            _nested(evidence, "research_forward", "top_five_profit_contribution"),
            1.0,
        )
        < 0.30
        and _nested(evidence, "research_forward", "funding_minus_cost_positive_without_basis") is True
        and _float_or(_nested(evidence, "research_forward", "minimum_depth_multiple"), 0.0) >= 5.0
        and _nested(evidence, "research_forward", "primary_2x_survives_without_liquidation") is True
        and _nested(evidence, "research_forward", "all_preregistered_stresses_present") is True
    )
    verdict_consistent = (
        (verdict == "failed" and (lower_bound <= 0.0 or estimate < 0.05))
        or (
            verdict == "inconclusive"
            and estimate >= 0.05
            and lower_bound < 0.05
            and _int_or(_nested(evidence, "research_forward", "complete_utc_days"), 0) < 180
        )
        or (verdict == "viable" and lower_bound >= 0.05 and max_drawdown <= 0.10 and viable_robustness)
    )
    research_ok = (
        artifact_validity["research_forward"]
        and _int_or(_nested(evidence, "research_forward", "collector_qa_days"), 0) >= 14
        and _int_or(_nested(evidence, "research_forward", "complete_utc_days"), 0) >= 90
        and _float_or(_nested(evidence, "research_forward", "decision_anchor_coverage"), 0.0) >= 0.99
        and _float_or(_nested(evidence, "research_forward", "fresh_anchor_fraction"), 0.0) >= 0.99
        and _float_or(
            _nested(evidence, "research_forward", "funding_reconciliation_fraction"),
            0.0,
        )
        == 1.0
        and _int_or(_nested(evidence, "research_forward", "future_data_joins"), -1) == 0
        and _int_or(
            _nested(
                evidence,
                "research_forward",
                "conflicting_duplicate_event_ids",
            ),
            -1,
        )
        == 0
        and _nested(evidence, "research_forward", "report_hash_reproduced") is True
        and _is_sha256(_nested(evidence, "research_forward", "dataset_manifest_sha256"))
        and _is_sha256(_nested(evidence, "research_forward", "report_sha256"))
        and _float_or(_nested(evidence, "research_forward", "storage_sizing_pilot_hours"), 0.0) >= 48
        and _int_or(_nested(evidence, "research_forward", "sealed_final_days"), 0) == 30
        and _nested(evidence, "research_forward", "sealed_final_untouched") is True
        and _nested(evidence, "research_forward", "deterministic_daily_weekly_bootstrap") is True
        and verdict in {"failed", "inconclusive", "viable"}
        and verdict_consistent
    )
    checks.append(
        _check(
            "evidence.preregistered_research_forward_verdict",
            research_ok,
            "forward research data and report reach an explicit reproducible verdict",
            {
                "artifact": artifact_observations["research_forward"],
                "metrics": evidence.get("research_forward", {}),
            },
            {
                "collector_qa_days_min": 14,
                "complete_utc_days_min": 90,
                "coverage_min": 0.99,
                "funding_reconciliation": 1.0,
                "future_data_joins": 0,
                "conflicting_duplicates": 0,
                "storage_sizing_pilot_hours_min": 48,
                "sealed_final_days": 30,
                "deterministic_daily_weekly_bootstrap": True,
                "dataset_and_report_sha256": True,
                "verdict_thresholds_consistent": True,
                "verdict": ["failed", "inconclusive", "viable"],
            },
            blocked=True,
        )
    )
    return checks


def _parser(root: Path) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence",
        type=Path,
        default=root / "verification_artifacts" / "master_execution_plan_external_evidence.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=root / "verification_artifacts" / "master_execution_plan_current.json",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[1]
    args = _parser(root).parse_args(argv)
    try:
        evidence = _load_json(args.evidence)
        checks = [
            *implementation_checks(root),
            *evidence_checks(evidence, evidence_root=args.evidence.parent),
        ]
    except Exception as exc:
        checks = [
            Check(
                check_id="verifier.input_error",
                status=FAIL,
                summary="master execution plan verifier could not evaluate its inputs",
                observed={"error_type": type(exc).__name__, "error": str(exc)},
                required="valid repository and evidence artifacts",
            )
        ]
    structural_failures = [check.check_id for check in checks if check.status == FAIL]
    evidence_blockers = [check.check_id for check in checks if check.status == BLOCKED]
    overall = FAIL if structural_failures else BLOCKED if evidence_blockers else "COMPLETE_NOT_LIVE_AUTHORIZED"
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repository_root": str(root),
        "status": overall,
        "live_authorized": False,
        "structural_failures": structural_failures,
        "evidence_blockers": evidence_blockers,
        "checks": [asdict(check) for check in checks],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0 if overall == "COMPLETE_NOT_LIVE_AUTHORIZED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
