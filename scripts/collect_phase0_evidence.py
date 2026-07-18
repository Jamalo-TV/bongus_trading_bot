"""Collect immutable Phase 0 CI, causal replay, and runtime reconciliation proof."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bongus.core.binance_endpoints import normalize_trading_mode
from bongus.core.config_manager import ConfigManager
from bongus.engine.analytics import compute_trade_summary
from bongus.market_data.data_loader import load_data
from bongus.strategies.strategy import run_strategy
from bongus.testing.measurement_evidence import (
    build_phase0_metrics,
    canonical_bytes,
    derive_runtime_measurement,
    sha256_file,
)


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _write_new(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _frame_fingerprint(frame: Any) -> str:
    buffer = io.BytesIO()
    frame.write_ipc(buffer, compression="uncompressed")
    return hashlib.sha256(buffer.getvalue()).hexdigest()


def _run_replay(spot: Path, perp: Path, funding: Path) -> dict[str, Any]:
    market = load_data(str(spot), str(perp), str(funding))
    replay = run_strategy(market)
    trades = compute_trade_summary(replay)
    replay_view = replay.select(
        [
            "timestamp",
            "annualized_funding",
            "basis_premium_pct",
            "funding_velocity",
            "raw_entry",
            "raw_exit",
            "inverse_signal",
            "in_position",
            "trade_id",
            "entry_filled",
            "exit_filled",
            "forced_exit",
            "funding_eligible",
            "basis_stop_triggered",
            "cumulative_yield",
        ]
    )
    return {
        "market_rows": market.height,
        "replay_rows": replay.height,
        "trade_count": trades.height,
        "replay_fingerprint": _frame_fingerprint(replay_view),
        "trade_fingerprint": _frame_fingerprint(trades),
    }


def _clean_ci(verifier: Mapping[str, Any]) -> tuple[bool, dict[str, Any]]:
    commands = verifier.get("command_evidence")
    commands = commands if isinstance(commands, list) else []
    command_rows = [row for row in commands if isinstance(row, dict)]
    passed = (
        verifier.get("local_validation_status") == "PASS"
        and len(command_rows) >= 5
        and all(row.get("return_code") == 0 for row in command_rows)
    )
    return passed, {
        "local_validation_status": verifier.get("local_validation_status"),
        "command_count": len(command_rows),
        "commands": [
            {
                "name": row.get("name"),
                "return_code": row.get("return_code"),
                "elapsed_seconds": row.get("elapsed_seconds"),
            }
            for row in command_rows
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "state.db")
    parser.add_argument("--config", type=Path, default=ROOT / "live_config.json")
    parser.add_argument("--clean-verifier", type=Path, required=True)
    parser.add_argument("--account-reconciliation", type=Path, required=True)
    parser.add_argument("--daily-reconciliation", type=Path)
    parser.add_argument("--spot", type=Path, default=ROOT / "data" / "spot_1m.parquet")
    parser.add_argument("--perp", type=Path, default=ROOT / "data" / "perp_1m.parquet")
    parser.add_argument(
        "--funding", type=Path, default=ROOT / "data" / "funding_rates.parquet"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "verification_artifacts" / "evidence",
    )
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    environment = normalize_trading_mode()
    if environment not in {"paper", "testnet"}:
        parser.error("Phase 0 collection is restricted to paper or testnet")
    config = ConfigManager(args.config)
    if not config.get_bool("pause_new_entries"):
        parser.error("pause_new_entries must remain true during evidence collection")
    if config.get_float("per_symbol_notional_cap_usd") > 2_500.0:
        parser.error("per-symbol cap exceeds the protected ceiling")
    if config.get_float("max_gross_exposure_usd") > 10_000.0:
        parser.error("gross cap exceeds the protected ceiling")

    verifier = _load(args.clean_verifier)
    account = _load(args.account_reconciliation)
    if account.get("environment") != environment:
        parser.error("account reconciliation environment does not match TRADING_MODE")
    daily = _load(args.daily_reconciliation) if args.daily_reconciliation else None
    clean_passed, clean_details = _clean_ci(verifier)

    first_replay = _run_replay(args.spot, args.perp, args.funding)
    second_replay = _run_replay(args.spot, args.perp, args.funding)
    replay_deterministic = first_replay == second_replay

    uri = f"file:{args.db.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    try:
        conn.execute("BEGIN")
        runtime = derive_runtime_measurement(
            conn,
            account_artifact=account,
            daily_reconciliation_artifact=daily,
        )
        conn.rollback()
    finally:
        conn.close()

    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%dT%H%M%SZ")
    source_refs = {
        "clean_verifier": {
            "uri": str(args.clean_verifier.resolve()),
            "sha256": sha256_file(args.clean_verifier),
        },
        "account_reconciliation": {
            "uri": str(args.account_reconciliation.resolve()),
            "sha256": sha256_file(args.account_reconciliation),
        },
        "market_data": [
            {"uri": str(path.resolve()), "sha256": sha256_file(path)}
            for path in (args.spot, args.perp, args.funding)
        ],
    }
    if args.daily_reconciliation:
        source_refs["daily_reconciliation"] = {
            "uri": str(args.daily_reconciliation.resolve()),
            "sha256": sha256_file(args.daily_reconciliation),
        }
    common = {
        "schema_version": 1,
        "evidence_kind": "ci_and_runtime",
        "generated_at": now.isoformat(),
        "environment": environment,
    }
    clean_artifact = {
        **common,
        "view": "clean_ci",
        "source": source_refs["clean_verifier"],
        "clean_ci_passed": clean_passed,
        "details": clean_details,
    }
    replay_artifact = {
        **common,
        "view": "causal_replay",
        "market_data": source_refs["market_data"],
        "first_run": first_replay,
        "second_run": second_replay,
        "deterministic": replay_deterministic,
    }
    runtime_artifact = {
        **common,
        "view": "runtime_reconciliation",
        "source_refs": source_refs,
        "measurement": runtime,
    }
    artifacts = {
        "clean_ci": clean_artifact,
        "causal_replay": replay_artifact,
        "runtime_reconciliation": runtime_artifact,
    }
    refs: list[dict[str, str]] = []
    for kind, artifact in artifacts.items():
        content_hash = hashlib.sha256(canonical_bytes(artifact)).hexdigest()
        path = args.output_dir / f"phase0_{kind}_{timestamp}_{content_hash[:12]}.json"
        _write_new(path, artifact)
        refs.append(
            {"kind": kind, "uri": str(path.resolve()), "sha256": sha256_file(path)}
        )
    metrics = build_phase0_metrics(
        clean_ci_passed=clean_passed,
        deterministic_causal_replay=replay_deterministic,
        runtime_measurement=runtime,
    )
    bundle = {
        **common,
        "metrics": metrics,
        "evidence_refs": refs,
        "machine_attestation": {
            "attested": True,
            "basis": "full local verifier, two independent causal replays, authenticated account evidence, and a read-only SQLite snapshot",
            "criteria_passed": (
                metrics["clean_ci_passed"] is True
                and metrics["decision_order_fill_lineage_pct"] == 100.0
                and metrics["deterministic_causal_replay"] is True
                and metrics["exchange_fill_funding_mapping_pct"] == 100.0
                and isinstance(metrics["daily_unexplained_max_usd"], (int, float))
                and metrics["daily_unexplained_max_usd"] <= 0.01
                and metrics["within_exchange_precision"] is True
            ),
        },
    }
    bundle_hash = hashlib.sha256(canonical_bytes(bundle)).hexdigest()
    bundle_path = args.output_dir / f"phase0_bundle_{timestamp}_{bundle_hash[:12]}.json"
    _write_new(bundle_path, bundle)
    print(
        json.dumps(
            {
                "status": "assembled",
                "bundle": str(bundle_path.resolve()),
                "bundle_sha256": sha256_file(bundle_path),
                "metrics": metrics,
                "criteria_passed": bundle["machine_attestation"]["criteria_passed"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
