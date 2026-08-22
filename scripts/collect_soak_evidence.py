"""Append one safe paper/testnet observation and emit immutable soak reports."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bongus.core.binance_endpoints import normalize_trading_mode
from bongus.core.config_manager import ConfigManager
from bongus.testing.soak_evidence import (
    append_observation,
    build_report_bundle,
    sha256_file,
    verify_journal,
)


def _resolve_runtime_data_root(value: str | None) -> Path:
    """Resolve mutable evidence inputs beneath the deployed runtime data root."""

    raw = str(value or "").strip()
    if not raw:
        return ROOT
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise ValueError("BONGUS_DATA_ROOT must be an absolute path")
    return candidate.resolve(strict=False)


def _build_parser(runtime_data_root: Path) -> argparse.ArgumentParser:
    def positive_finite_seconds(value: str) -> float:
        try:
            parsed = float(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("must be a number") from exc
        if not math.isfinite(parsed) or parsed <= 0.0:
            raise argparse.ArgumentTypeError("must be finite and greater than zero")
        return parsed

    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=runtime_data_root / "state.db")
    parser.add_argument(
        "--audit-db",
        type=Path,
        default=runtime_data_root / "audit.db",
        help="Read-only audit database containing operational health samples",
    )
    parser.add_argument(
        "--config", type=Path, default=runtime_data_root / "live_config.json"
    )
    parser.add_argument("--account-reconciliation", type=Path, required=True)
    parser.add_argument(
        "--journal-dir",
        type=Path,
        default=runtime_data_root / "verification_artifacts" / "soak_journal",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=runtime_data_root / "verification_artifacts" / "evidence",
    )
    parser.add_argument("--max-loop-age-seconds", type=float, default=180.0)
    parser.add_argument("--max-observation-gap-seconds", type=float, default=900.0)
    parser.add_argument(
        "--max-account-age-seconds",
        type=positive_finite_seconds,
        default=300.0,
    )
    return parser


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _read_state(
    db_path: Path,
    audit_db_path: Path | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Read hot risk state and audit-owned health evidence.

    ``audit_db_path`` remains optional for compatibility with legacy
    monolithic stores. Production split stores must pass their distinct audit
    database because ``health_samples`` is not present in ``state.db``.
    """

    state_uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    state_conn = sqlite3.connect(state_uri, uri=True, timeout=30)
    state_conn.row_factory = sqlite3.Row
    try:
        risk = {
            str(row["key"]): str(row["value"])
            for row in state_conn.execute("SELECT key, value FROM risk_state")
        }
    finally:
        state_conn.close()

    health_path = audit_db_path if audit_db_path is not None else db_path
    audit_uri = f"file:{health_path.resolve().as_posix()}?mode=ro"
    audit_conn = sqlite3.connect(audit_uri, uri=True, timeout=30)
    audit_conn.row_factory = sqlite3.Row
    try:
        latest_loop = audit_conn.execute(
            """SELECT sample_time, value, alert_level, runtime_mode, notes
               FROM health_samples WHERE metric = 'loop_alive'
               ORDER BY sample_time DESC LIMIT 1"""
        ).fetchone()
        critical_rows = audit_conn.execute(
            """SELECT sample_time, metric, alert_level, runtime_mode, notes
               FROM health_samples WHERE lower(alert_level) = 'critical'
               ORDER BY sample_time DESC LIMIT 100"""
        ).fetchall()
    finally:
        audit_conn.close()
    return risk, {
        "latest_loop": dict(latest_loop) if latest_loop is not None else None,
        "critical_health_rows": [dict(row) for row in critical_rows],
    }


def _bool(value: object) -> bool:
    return str(value).strip().lower() == "true"


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _validate_account_freshness(
    account: dict[str, Any],
    *,
    now: datetime,
    max_age_seconds: float,
) -> None:
    if not math.isfinite(max_age_seconds) or max_age_seconds <= 0.0:
        raise ValueError("max account age must be finite and greater than zero")
    snapshot = account.get("exchange_reconciliation_snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    for field_name, raw_value in (
        ("generated_at", account.get("generated_at")),
        ("exchange_reconciliation_snapshot.observed_at", snapshot.get("observed_at")),
    ):
        observed = _parse_time(raw_value)
        if observed is None:
            raise ValueError(f"account evidence {field_name} is missing or invalid")
        age_seconds = (now - observed).total_seconds()
        if age_seconds < 0.0:
            raise ValueError(f"account evidence {field_name} is in the future")
        if age_seconds > max_age_seconds:
            raise ValueError(
                f"account evidence {field_name} is stale "
                f"({age_seconds:.3f}s > {max_age_seconds:.3f}s)"
            )


def build_facts(
    *,
    now: datetime,
    environment: str,
    risk: dict[str, str],
    state: dict[str, Any],
    account: dict[str, Any],
    max_loop_age_seconds: float,
    db_path: Path = ROOT / "state.db",
) -> dict[str, Any]:
    latest_loop = state.get("latest_loop")
    latest_loop = latest_loop if isinstance(latest_loop, dict) else {}
    loop_time = _parse_time(latest_loop.get("sample_time"))
    loop_age = (now - loop_time).total_seconds() if loop_time else None
    checks = {
        "runtime_ready": _bool(risk.get("runtime_ready")),
        "telemetry_connected": _bool(risk.get("telemetry_connected")),
        "loop_heartbeat_fresh": (
            loop_age is not None and 0.0 <= loop_age <= max_loop_age_seconds
        ),
        "state_environment_matches": str(risk.get("trading_mode") or "").lower()
        == environment,
    }
    decision_ready = all(checks.values())
    reconciliation = account.get("reconciliation")
    reconciliation = reconciliation if isinstance(reconciliation, dict) else {}
    issues = reconciliation.get("issues")
    issues = issues if isinstance(issues, list) else []
    blocking_issues = [
        issue for issue in issues if isinstance(issue, dict) and issue.get("blocking") is True
    ]
    critical_issue_ids = sorted(
        {
            str(issue.get("incident_id") or issue.get("code") or "")
            for issue in blocking_issues
            if str(issue.get("incident_id") or issue.get("code") or "")
        }
    )
    alert_reasons = [f"account:{item}" for item in critical_issue_ids]
    alert_reasons.extend(
        f"decision:{key}" for key, passed in checks.items() if passed is not True
    )
    account_ready = reconciliation.get("ready") is True
    unresolved_alerts = len(set(alert_reasons))
    return {
        "unattended_eligible": bool(
            decision_ready and account_ready and unresolved_alerts == 0
        ),
        "decision_service": {
            "ready": decision_ready,
            "checks": checks,
            "latest_loop_sample_time": latest_loop.get("sample_time"),
            "latest_loop_age_seconds": loop_age,
            "max_loop_age_seconds": max_loop_age_seconds,
            "runtime_mode": risk.get("runtime_mode"),
            "blocked_reason": risk.get("blocked_reason"),
        },
        "reconciliation": {
            "ready": account_ready,
            "fingerprint": reconciliation.get("fingerprint"),
            "blocking_issue_count": len(blocking_issues),
            "critical_issue_ids": critical_issue_ids,
        },
        "unresolved_alerts": unresolved_alerts,
        "unresolved_alert_reasons": sorted(set(alert_reasons)),
        "fault_injection": {"gaps_injected": 0, "gaps_detected_replayed": 0},
        "routine_recovery": {"attempted": 0, "within_slo": 0},
        "source_state": {
            "database": str(db_path.resolve()),
            "risk_session_id": risk.get("session_id"),
            "risk_trading_mode": risk.get("trading_mode"),
            "critical_health_row_count": len(state.get("critical_health_rows") or []),
        },
    }


def main() -> int:
    load_dotenv(ROOT / ".env")
    try:
        runtime_data_root = _resolve_runtime_data_root(
            os.getenv("BONGUS_DATA_ROOT")
        )
    except ValueError as exc:
        parser = _build_parser(ROOT)
        parser.error(str(exc))
    parser = _build_parser(runtime_data_root)
    args = parser.parse_args()

    environment = normalize_trading_mode()
    if environment not in {"paper", "testnet"}:
        parser.error("soak collection is restricted to paper or testnet")
    config = ConfigManager(args.config)
    if not config.get_bool("pause_new_entries"):
        parser.error("pause_new_entries must remain true during initial evidence collection")
    if config.get_float("per_symbol_notional_cap_usd") > 2_500.0:
        parser.error("per-symbol cap exceeds the protected ceiling")
    if config.get_float("max_gross_exposure_usd") > 10_000.0:
        parser.error("gross cap exceeds the protected ceiling")

    account = _load_json(args.account_reconciliation)
    if account.get("evidence_kind") != "account_reconciliation":
        parser.error("account evidence has the wrong evidence_kind")
    if account.get("environment") != environment:
        parser.error("account evidence environment does not match TRADING_MODE")
    if account.get("machine_attestation", {}).get("attested") is not True:
        parser.error("account evidence is not machine-attested")

    now = datetime.now(timezone.utc)
    try:
        _validate_account_freshness(
            account,
            now=now,
            max_age_seconds=args.max_account_age_seconds,
        )
    except ValueError as exc:
        parser.error(str(exc))
    risk, state = _read_state(args.db, args.audit_db)
    facts = build_facts(
        now=now,
        environment=environment,
        risk=risk,
        state=state,
        account=account,
        max_loop_age_seconds=args.max_loop_age_seconds,
        db_path=args.db,
    )
    account_ref = {
        "kind": "account_reconciliation",
        "uri": str(args.account_reconciliation.resolve()),
        "sha256": sha256_file(args.account_reconciliation),
    }
    record, record_path = append_observation(
        args.journal_dir,
        observed_at=now,
        environment=environment,
        facts=facts,
        source_refs=[account_ref],
    )
    records = verify_journal(args.journal_dir)
    bundle, bundle_path = build_report_bundle(
        records,
        journal_directory=args.journal_dir,
        output_directory=args.output_dir,
        generated_at=now,
        max_observation_gap_seconds=args.max_observation_gap_seconds,
    )
    print(
        json.dumps(
            {
                "status": "observation_recorded",
                "record": str(record_path.resolve()),
                "record_sha256": record["record_sha256"],
                "bundle": str(bundle_path.resolve()),
                "bundle_sha256": sha256_file(bundle_path),
                "metrics": bundle["metrics"],
                "criteria_passed": bundle["machine_attestation"]["criteria_passed"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
