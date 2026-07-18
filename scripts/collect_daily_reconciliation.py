"""Append one authenticated balance snapshot to the daily reconciliation chain."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
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
from bongus.engine.economic_ledger import project_economic_ledger, read_economic_events
from bongus.testing.daily_reconciliation_evidence import (
    append_record,
    build_bundle,
    build_interval,
    verify_journal,
)
from bongus.testing.measurement_evidence import canonical_bytes, sha256_file


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "state.db")
    parser.add_argument("--config", type=Path, default=ROOT / "live_config.json")
    parser.add_argument("--account-reconciliation", type=Path, required=True)
    parser.add_argument(
        "--journal-dir",
        type=Path,
        default=ROOT / "verification_artifacts" / "daily_reconciliation_journal",
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
        parser.error("daily reconciliation is restricted to paper or testnet")
    config = ConfigManager(args.config)
    if not config.get_bool("pause_new_entries"):
        parser.error("pause_new_entries must remain true during baseline collection")
    if config.get_float("per_symbol_notional_cap_usd") > 2_500.0:
        parser.error("per-symbol cap exceeds the protected ceiling")
    if config.get_float("max_gross_exposure_usd") > 10_000.0:
        parser.error("gross cap exceeds the protected ceiling")

    account = _load(args.account_reconciliation)
    if account.get("evidence_kind") != "account_reconciliation":
        parser.error("input is not an account reconciliation artifact")
    if account.get("environment") != environment:
        parser.error("account artifact environment does not match TRADING_MODE")
    if account.get("collection_policy", {}).get("read_only") is not True:
        parser.error("account artifact is not read-only")
    snapshot = account.get("exchange_reconciliation_snapshot")
    if not isinstance(snapshot, dict):
        parser.error("account artifact predates balance reconciliation snapshots")

    existing = verify_journal(args.journal_dir)
    interval = None
    if existing:
        previous = existing[-1].get("snapshot")
        if not isinstance(previous, dict):
            parser.error("journal head snapshot is invalid")
        previous_time = datetime.fromisoformat(
            str(previous["observed_at"]).replace("Z", "+00:00")
        ).astimezone(timezone.utc)
        current_time = datetime.fromisoformat(
            str(snapshot["observed_at"]).replace("Z", "+00:00")
        ).astimezone(timezone.utc)
        if current_time <= previous_time:
            parser.error("account snapshot must be newer than the journal head")
        exclusive_start = (previous_time + timedelta(microseconds=1)).isoformat()
        uri = f"file:{args.db.resolve().as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN")
            rows = read_economic_events(
                conn,
                account_id=str(account.get("account_id") or "binance-default"),
                trading_mode=environment,
                venue="BINANCE",
                start_time=exclusive_start,
                end_time=current_time.isoformat(),
                limit=None,
            )
            projection = project_economic_ledger(
                conn,
                account_id=str(account.get("account_id") or "binance-default"),
                trading_mode=environment,
                venue="BINANCE",
                start_time=exclusive_start,
                end_time=current_time.isoformat(),
            )
            conn.rollback()
        finally:
            conn.close()
        interval = build_interval(
            previous,
            snapshot,
            ledger_balance_deltas={
                key: str(value) for key, value in projection.balance_deltas.items()
            },
            ledger_position_deltas={
                key: str(value)
                for key, value in projection.perpetual_position_deltas.items()
            },
            ledger_event_count=projection.event_count,
            ledger_unvalued_event_count=projection.unvalued_economic_event_count,
            ledger_rows_sha256=hashlib.sha256(canonical_bytes(rows)).hexdigest(),
        )

    account_ref = {
        "kind": "account_reconciliation",
        "uri": str(args.account_reconciliation.resolve()),
        "sha256": sha256_file(args.account_reconciliation),
    }
    record, record_path = append_record(
        args.journal_dir,
        observed_at=str(snapshot["observed_at"]),
        environment=environment,
        account_ref=account_ref,
        snapshot=snapshot,
        interval=interval,
    )
    records = verify_journal(args.journal_dir)
    bundle, bundle_path = build_bundle(
        records,
        journal_directory=args.journal_dir,
        output_directory=args.output_dir,
        generated_at=datetime.now(timezone.utc),
    )
    print(
        json.dumps(
            {
                "status": "baseline_recorded" if interval is None else interval["status"].lower(),
                "record": str(record_path.resolve()),
                "record_sha256": record["record_sha256"],
                "bundle": str(bundle_path.resolve()),
                "bundle_sha256": sha256_file(bundle_path),
                "interval_count": bundle["journal"]["interval_count"],
                "all_intervals_reconciled": bundle["machine_attestation"][
                    "all_intervals_reconciled"
                ],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
