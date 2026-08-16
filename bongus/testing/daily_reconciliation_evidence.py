"""Hash-chained daily exchange/ledger delta reconciliation evidence."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

from bongus.engine.economic_ledger import DecimalInput
from bongus.supervisor.daily_report import (
    FINALIZED,
    PNL_INCOMPLETE,
    DailyNavClose,
    calculate_daily_nav_close,
)
from bongus.testing.measurement_evidence import canonical_bytes, sha256_file

ZERO_HASH = "0" * 64
UNKNOWN = "UNKNOWN"


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


def _known_decimal(
    values: Mapping[str, object] | None,
    key: str,
    field: str,
) -> Decimal | None:
    if values is None or key not in values:
        return None
    value = values[key]
    if value is None or (isinstance(value, str) and value.strip().upper() == UNKNOWN):
        return None
    return _decimal(value, field)


def _nav_input(values: Mapping[str, object], key: str) -> DecimalInput | None:
    value = values.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, float, str)):
        raise DailyReconciliationError(f"daily NAV component {key} is invalid")
    return value


def _nav_payload(close: DailyNavClose) -> dict[str, Any]:
    def encoded(value: Decimal | None) -> str:
        return UNKNOWN if value is None else format(value, "f")

    return {
        "status": close.status,
        "opening_nav_usd": encoded(close.opening_nav_usd),
        "closing_nav_usd": encoded(close.closing_nav_usd),
        "external_deposits_usd": encoded(close.external_deposits_usd),
        "external_withdrawals_usd": encoded(close.external_withdrawals_usd),
        "realized_price_pnl_usd": encoded(close.realized_price_pnl_usd),
        "actual_funding_usd": encoded(close.actual_funding_usd),
        "commission_cost_usd": encoded(close.commission_cost_usd),
        "borrow_interest_cost_usd": encoded(close.borrow_interest_cost_usd),
        "unrealized_pnl_change_usd": encoded(close.unrealized_pnl_change_usd),
        "stablecoin_fx_movement_usd": encoded(close.stablecoin_fx_movement_usd),
        "internal_transfers_usd": encoded(close.internal_transfers_usd),
        "projected_closing_nav_usd": encoded(close.projected_closing_nav_usd),
        "equation_difference_usd": encoded(close.equation_difference_usd),
        "tolerance_usd": encoded(close.tolerance_usd),
        "missing_components": list(close.missing_components),
        "blockers": list(close.blockers),
    }


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
            "previous_record_sha256": (records[-1]["record_sha256"] if records else ZERO_HASH),
            "account_ref": dict(account_ref),
            "snapshot": dict(snapshot),
            "interval": dict(interval) if interval is not None else None,
        }
        record["record_sha256"] = _hash_record(record)
        destination = directory / (f"{record['sequence']:08d}_{record['record_sha256']}.json")
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
    daily_nav_components: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    previous_balances = previous_snapshot.get("combined_balances")
    current_balances = current_snapshot.get("combined_balances")
    previous_balances = previous_balances if isinstance(previous_balances, Mapping) else None
    current_balances = current_balances if isinstance(current_balances, Mapping) else None
    tolerances = current_snapshot.get("balance_tolerances")
    tolerances = tolerances if isinstance(tolerances, Mapping) else None
    prices = current_snapshot.get("asset_prices_usd")
    prices = prices if isinstance(prices, Mapping) else None

    balance_differences: dict[str, str] = {}
    unexplained_usd: list[Decimal] = []
    unvalued: list[str] = []
    unknown_components: list[str] = []
    within_precision = True
    for asset in sorted(
        set(map(str, previous_balances or {}))
        | set(map(str, current_balances or {}))
        | set(map(str, ledger_balance_deltas))
    ):
        previous_value = _known_decimal(previous_balances, asset, f"{asset} previous balance")
        current_value = _known_decimal(current_balances, asset, f"{asset} current balance")
        expected_delta = _known_decimal(ledger_balance_deltas, asset, f"{asset} projected balance delta")
        tolerance = _known_decimal(tolerances, asset, f"{asset} tolerance")
        missing = []
        if previous_value is None:
            missing.append("previous_balance")
        if current_value is None:
            missing.append("current_balance")
        if expected_delta is None:
            missing.append("projected_delta")
        if tolerance is None:
            missing.append("tolerance")
        if missing:
            balance_differences[asset] = UNKNOWN
            unknown_components.extend(f"balance:{asset}:{component}" for component in missing)
            within_precision = False
            continue
        assert previous_value is not None
        assert current_value is not None
        assert expected_delta is not None
        assert tolerance is not None
        actual_delta = current_value - previous_value
        difference = actual_delta - expected_delta
        tolerance = abs(tolerance)
        balance_differences[asset] = format(difference, "f")
        if abs(difference) > tolerance:
            within_precision = False
        if difference:
            price = _known_decimal(prices, asset, f"{asset} price")
            if price is None:
                unvalued.append(f"balance:{asset}")
            else:
                unexplained_usd.append(abs(difference) * price)

    previous_positions = previous_snapshot.get("perpetual_positions")
    current_positions = current_snapshot.get("perpetual_positions")
    previous_positions = previous_positions if isinstance(previous_positions, Mapping) else None
    current_positions = current_positions if isinstance(current_positions, Mapping) else None
    position_tolerance_raw = current_snapshot.get("position_tolerance")
    position_tolerance = (
        None
        if position_tolerance_raw is None
        or (isinstance(position_tolerance_raw, str) and position_tolerance_raw.strip().upper() == UNKNOWN)
        else abs(_decimal(position_tolerance_raw, "position tolerance"))
    )
    normalized_expected_positions = {
        (str(key) if ":" in str(key) else f"{key}:BOTH"): value for key, value in ledger_position_deltas.items()
    }
    position_differences: dict[str, str] = {}
    for key in sorted(
        set(map(str, previous_positions or {}))
        | set(map(str, current_positions or {}))
        | set(map(str, normalized_expected_positions))
    ):
        previous_value = _known_decimal(previous_positions, key, f"{key} previous position")
        current_value = _known_decimal(current_positions, key, f"{key} current position")
        expected_delta = _known_decimal(normalized_expected_positions, key, f"{key} projected position delta")
        missing = []
        if previous_value is None:
            missing.append("previous_position")
        if current_value is None:
            missing.append("current_position")
        if expected_delta is None:
            missing.append("projected_delta")
        if position_tolerance is None:
            missing.append("tolerance")
        if missing:
            position_differences[key] = UNKNOWN
            unknown_components.extend(f"position:{key}:{component}" for component in missing)
            within_precision = False
            continue
        assert previous_value is not None
        assert current_value is not None
        assert expected_delta is not None
        assert position_tolerance is not None
        actual_delta = current_value - previous_value
        difference = actual_delta - expected_delta
        position_differences[key] = format(difference, "f")
        if abs(difference) > position_tolerance:
            within_precision = False
        if difference:
            symbol = key.split(":", 1)[0]
            asset = symbol[:-4] if symbol.endswith("USDT") else ""
            price = _known_decimal(prices, asset, f"{asset} price") if asset else None
            if not asset or price is None:
                unvalued.append(f"position:{key}")
            else:
                unexplained_usd.append(abs(difference) * price)

    nav = daily_nav_components if isinstance(daily_nav_components, Mapping) else {}
    nav_close = calculate_daily_nav_close(
        opening_nav_usd=_nav_input(nav, "opening_nav_usd"),
        closing_nav_usd=_nav_input(nav, "closing_nav_usd"),
        external_deposits_usd=_nav_input(nav, "external_deposits_usd"),
        external_withdrawals_usd=_nav_input(nav, "external_withdrawals_usd"),
        realized_price_pnl_usd=_nav_input(nav, "realized_price_pnl_usd"),
        actual_funding_usd=_nav_input(nav, "actual_funding_usd"),
        commission_cost_usd=_nav_input(nav, "commission_cost_usd"),
        borrow_interest_cost_usd=_nav_input(nav, "borrow_interest_cost_usd"),
        unrealized_pnl_change_usd=_nav_input(nav, "unrealized_pnl_change_usd"),
        stablecoin_fx_movement_usd=_nav_input(nav, "stablecoin_fx_movement_usd"),
        internal_transfers_usd=_nav_input(nav, "internal_transfers_usd"),
        tolerance_usd=_nav_input(nav, "tolerance_usd"),
    )

    prerequisites = {
        "previous_snapshot_complete": previous_snapshot.get("snapshot_complete") is True,
        "current_snapshot_complete": current_snapshot.get("snapshot_complete") is True,
        "previous_account_identity_verified": previous_snapshot.get("account_identity_verified") is True,
        "current_account_identity_verified": current_snapshot.get("account_identity_verified") is True,
        "ledger_events_all_valued": ledger_unvalued_event_count == 0,
        "all_differences_valued": not unvalued,
        "all_components_known": not unknown_components,
        "daily_nav_finalized": nav_close.status == FINALIZED,
    }
    within_precision = within_precision and all(prerequisites.values())
    if within_precision:
        status = "RECONCILED"
    elif unknown_components or unvalued or ledger_unvalued_event_count:
        status = PNL_INCOMPLETE
    elif nav_close.status in {PNL_INCOMPLETE, "PROJECTED"}:
        status = nav_close.status
    else:
        status = "BLOCKED"
    return {
        "start_time": previous_snapshot.get("observed_at"),
        "end_time": current_snapshot.get("observed_at"),
        "status": status,
        "prerequisites": prerequisites,
        "ledger_event_count": max(0, int(ledger_event_count)),
        "ledger_unvalued_event_count": max(0, int(ledger_unvalued_event_count)),
        "ledger_rows_sha256": ledger_rows_sha256,
        "balance_differences": balance_differences,
        "position_differences": position_differences,
        "unknown_components": sorted(set(unknown_components)),
        "unvalued_differences": sorted(set(unvalued)),
        "unexplained_max_usd": (
            None if unvalued or unknown_components else float(max(unexplained_usd, default=Decimal("0")))
        ),
        "exchange_precision_usd": 0.01,
        "within_exchange_precision": within_precision,
        "daily_nav": _nav_payload(nav_close),
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
    days = [dict(record["interval"]) for record in records if isinstance(record.get("interval"), dict)]
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
