"""Append one authenticated balance snapshot to the daily reconciliation chain."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
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
from bongus.engine.economic_ledger import project_economic_ledger, read_economic_events
from bongus.testing.daily_reconciliation_evidence import (
    append_record,
    build_bundle,
    build_interval,
    verify_journal,
)
from bongus.testing.measurement_evidence import canonical_bytes, sha256_file


def _resolve_runtime_data_root(value: str | None) -> Path:
    """Resolve mutable reconciliation inputs beneath the runtime data root."""

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
    parser.add_argument(
        "--audit-db",
        "--db",
        dest="audit_db",
        type=Path,
        default=runtime_data_root / "audit.db",
        help="Read-only audit database containing the economic ledger (--db is deprecated)",
    )
    parser.add_argument(
        "--config", type=Path, default=runtime_data_root / "live_config.json"
    )
    parser.add_argument("--account-reconciliation", type=Path, required=True)
    parser.add_argument(
        "--journal-dir",
        type=Path,
        default=(
            runtime_data_root
            / "verification_artifacts"
            / "daily_reconciliation_journal"
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=runtime_data_root / "verification_artifacts" / "evidence",
    )
    parser.add_argument(
        "--max-account-age-seconds",
        type=positive_finite_seconds,
        default=300.0,
    )
    return parser


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


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
    machine_attestation = account.get("machine_attestation")
    if (
        not isinstance(machine_attestation, dict)
        or machine_attestation.get("attested") is not True
    ):
        parser.error("account artifact is not machine-attested")
    snapshot = account.get("exchange_reconciliation_snapshot")
    if not isinstance(snapshot, dict):
        parser.error("account artifact predates balance reconciliation snapshots")
    now = datetime.now(timezone.utc)
    try:
        _validate_account_freshness(
            account,
            now=now,
            max_age_seconds=args.max_account_age_seconds,
        )
    except ValueError as exc:
        parser.error(str(exc))

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
        uri = f"file:{args.audit_db.resolve().as_posix()}?mode=ro"
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
