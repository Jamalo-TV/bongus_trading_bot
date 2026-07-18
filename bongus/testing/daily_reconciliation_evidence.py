"""Hash-chained daily exchange/ledger delta reconciliation evidence."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from bongus.testing.measurement_evidence import canonical_bytes, sha256_file


ZERO_HASH = "0" * 64


class DailyReconciliationError(ValueError):
    pass


def _decimal(value: object, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DailyReconciliationError(f"{field} must be a finite decimal") from exc
    if not parsed.is_finite():
        raise DailyReconciliationError(f"{field} must be a finite decimal")
    return parsed


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise DailyReconciliationError("observed_at must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DailyReconciliationError("observed_at is invalid") from exc
    if parsed.tzinfo is None:
        raise DailyReconciliationError("observed_at must include a timezone")
    return parsed.astimezone(timezone.utc)


def _hash_record(record: Mapping[str, Any]) -> str:
    unsigned = dict(record)
    unsigned.pop("record_sha256", None)
    return hashlib.sha256(canonical_bytes(unsigned)).hexdigest()


def verify_journal(directory: Path) -> list[dict[str, Any]]:
    if not directory.exists():
        return []
    records: list[dict[str, Any]] = []
    previous_hash = ZERO_HASH
    previous_time: datetime | None = None
    for sequence, path in enumerate(sorted(directory.glob("*.json")), start=1):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DailyReconciliationError(f"cannot decode {path.name}") from exc
        if not isinstance(record, dict) or record.get("schema_version") != 1:
            raise DailyReconciliationError(f"invalid journal schema in {path.name}")
        calculated = _hash_record(record)
        if record.get("sequence") != sequence:
            raise DailyReconciliationError("journal sequence is discontinuous")
        if record.get("record_sha256") != calculated:
            raise DailyReconciliationError(f"record hash mismatch in {path.name}")
        if path.name != f"{sequence:08d}_{calculated}.json":
            raise DailyReconciliationError("journal filename is not hash-bound")
        if record.get("previous_record_sha256") != previous_hash:
            raise DailyReconciliationError("journal previous-record link is broken")
        observed = _time(record.get("observed_at"))
        if previous_time is not None and observed <= previous_time:
            raise DailyReconciliationError("journal timestamps must increase")
        previous_time = observed
        previous_hash = calculated
        records.append(record)
    return records


def append_record(
    directory: Path,
    *,
    observed_at: str,
    environment: str,
    account_ref: Mapping[str, str],
    snapshot: Mapping[str, Any],
    interval: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], Path]:
    normalized_environment = environment.strip().lower()
    if normalized_environment not in {"paper", "testnet"}:
        raise DailyReconciliationError("environment must be paper or testnet")
    observed = _time(observed_at)
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / ".append.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise DailyReconciliationError("daily journal append lock exists") from exc
    try:
        os.close(descriptor)
        records = verify_journal(directory)
        if records and observed <= _time(records[-1]["observed_at"]):
            raise DailyReconciliationError("observation does not follow journal head")
        record: dict[str, Any] = {
            "schema_version": 1,
            "sequence": len(records) + 1,
            "event_type": "daily_reconciliation_observation",
            "observed_at": observed.isoformat(),
            "environment": normalized_environment,
            "previous_record_sha256": (
                records[-1]["record_sha256"] if records else ZERO_HASH
            ),
            "account_ref": dict(account_ref),
            "snapshot": dict(snapshot),
            "interval": dict(interval) if interval is not None else None,
        }
        record["record_sha256"] = _hash_record(record)
        destination = directory / (
            f"{record['sequence']:08d}_{record['record_sha256']}.json"
        )
        temporary = directory / f".{destination.name}.tmp"
        with temporary.open("xb") as handle:
            handle.write(canonical_bytes(record) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(destination)
        return record, destination
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


def build_interval(
    previous_snapshot: Mapping[str, Any],
    current_snapshot: Mapping[str, Any],
    *,
    ledger_balance_deltas: Mapping[str, object],
    ledger_position_deltas: Mapping[str, object],
    ledger_event_count: int,
    ledger_unvalued_event_count: int,
    ledger_rows_sha256: str,
) -> dict[str, Any]:
    previous_balances = previous_snapshot.get("combined_balances")
    current_balances = current_snapshot.get("combined_balances")
    previous_balances = previous_balances if isinstance(previous_balances, Mapping) else {}
    current_balances = current_balances if isinstance(current_balances, Mapping) else {}
    tolerances = current_snapshot.get("balance_tolerances")
    tolerances = tolerances if isinstance(tolerances, Mapping) else {}
    prices = current_snapshot.get("asset_prices_usd")
    prices = prices if isinstance(prices, Mapping) else {}

    balance_differences: dict[str, str] = {}
    unexplained_usd: list[Decimal] = []
    unvalued: list[str] = []
    within_precision = True
    for asset in sorted(
        set(map(str, previous_balances))
        | set(map(str, current_balances))
        | set(map(str, ledger_balance_deltas))
    ):
        actual_delta = _decimal(current_balances.get(asset, "0"), asset) - _decimal(
            previous_balances.get(asset, "0"), asset
        )
        expected_delta = _decimal(ledger_balance_deltas.get(asset, "0"), asset)
        difference = actual_delta - expected_delta
        tolerance = abs(_decimal(tolerances.get(asset, "0"), f"{asset} tolerance"))
        balance_differences[asset] = format(difference, "f")
        if abs(difference) > tolerance:
            within_precision = False
        if difference:
            price = prices.get(asset)
            if price is None or str(price) == "":
                unvalued.append(f"balance:{asset}")
            else:
                unexplained_usd.append(abs(difference) * _decimal(price, f"{asset} price"))

    previous_positions = previous_snapshot.get("perpetual_positions")
    current_positions = current_snapshot.get("perpetual_positions")
    previous_positions = previous_positions if isinstance(previous_positions, Mapping) else {}
    current_positions = current_positions if isinstance(current_positions, Mapping) else {}
    position_tolerance = abs(
        _decimal(current_snapshot.get("position_tolerance", "0"), "position tolerance")
    )
    normalized_expected_positions = {
        (key if ":" in str(key) else f"{key}:BOTH"): value
        for key, value in ledger_position_deltas.items()
    }
    position_differences: dict[str, str] = {}
    for key in sorted(
        set(map(str, previous_positions))
        | set(map(str, current_positions))
        | set(map(str, normalized_expected_positions))
    ):
        actual_delta = _decimal(current_positions.get(key, "0"), key) - _decimal(
            previous_positions.get(key, "0"), key
        )
        expected_delta = _decimal(normalized_expected_positions.get(key, "0"), key)
        difference = actual_delta - expected_delta
        position_differences[key] = format(difference, "f")
        if abs(difference) > position_tolerance:
            within_precision = False
        if difference:
            symbol = key.split(":", 1)[0]
            asset = symbol[:-4] if symbol.endswith("USDT") else ""
            price = prices.get(asset)
            if not asset or price is None or str(price) == "":
                unvalued.append(f"position:{key}")
            else:
                unexplained_usd.append(abs(difference) * _decimal(price, f"{asset} price"))

    prerequisites = {
        "previous_snapshot_complete": previous_snapshot.get("snapshot_complete") is True,
        "current_snapshot_complete": current_snapshot.get("snapshot_complete") is True,
        "previous_account_identity_verified": previous_snapshot.get(
            "account_identity_verified"
        )
        is True,
        "current_account_identity_verified": current_snapshot.get(
            "account_identity_verified"
        )
        is True,
        "ledger_events_all_valued": ledger_unvalued_event_count == 0,
        "all_differences_valued": not unvalued,
    }
    within_precision = within_precision and all(prerequisites.values())
    return {
        "start_time": previous_snapshot.get("observed_at"),
        "end_time": current_snapshot.get("observed_at"),
        "status": "RECONCILED" if within_precision else "BLOCKED",
        "prerequisites": prerequisites,
        "ledger_event_count": max(0, int(ledger_event_count)),
        "ledger_unvalued_event_count": max(0, int(ledger_unvalued_event_count)),
        "ledger_rows_sha256": ledger_rows_sha256,
        "balance_differences": balance_differences,
        "position_differences": position_differences,
        "unvalued_differences": sorted(set(unvalued)),
        "unexplained_max_usd": (
            None if unvalued else float(max(unexplained_usd, default=Decimal("0")))
        ),
        "exchange_precision_usd": 0.01,
        "within_exchange_precision": within_precision,
    }


def build_bundle(
    records: Sequence[Mapping[str, Any]],
    *,
    journal_directory: Path,
    output_directory: Path,
    generated_at: datetime,
) -> tuple[dict[str, Any], Path]:
    if not records:
        raise DailyReconciliationError("at least one journal record is required")
    days = [
        dict(record["interval"])
        for record in records
        if isinstance(record.get("interval"), dict)
    ]
    head = str(records[-1]["record_sha256"])
    payload = {
        "schema_version": 1,
        "evidence_kind": "runtime_daily_reconciliation",
        "generated_at": generated_at.astimezone(timezone.utc).isoformat(),
        "journal": {
            "directory": str(journal_directory.resolve()),
            "record_count": len(records),
            "interval_count": len(days),
            "head_sha256": head,
            "chain_verified": True,
        },
        "days": days,
        "machine_attestation": {
            "attested": True,
            "basis": "authenticated read-only account snapshots and a verified append-only hash chain",
            "baseline_only": not days,
            "all_intervals_reconciled": bool(days)
            and all(day.get("within_exchange_precision") is True for day in days),
        },
    }
    output_directory.mkdir(parents=True, exist_ok=True)
    stamp = generated_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = output_directory / f"daily_reconciliation_{stamp}_{head[:12]}.json"
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    return payload, path


def artifact_ref(path: Path) -> dict[str, str]:
    return {
        "kind": "daily_reconciliation",
        "uri": str(path.resolve()),
        "sha256": sha256_file(path),
    }
