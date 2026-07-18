"""Fail-closed Phase 0 measurement and lineage evidence derivation."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping, Sequence


LINEAGE_FIELDS = (
    "account_id",
    "environment",
    "strategy_id",
    "cycle_id",
    "intent_id",
    "leg_id",
    "config_version_hash",
    "market",
    "side",
    "order_id",
    "trade_id",
)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
        default=str,
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_rows(rows: Iterable[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        encoded = canonical_bytes(dict(row))
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _rows(conn: sqlite3.Connection, query: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(query, tuple(params)).fetchall()]


def _percent(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(100.0 * numerator / denominator, 9)


def derive_runtime_measurement(
    conn: sqlite3.Connection,
    *,
    account_artifact: Mapping[str, Any],
    daily_reconciliation_artifact: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Measure real lineage/mapping denominators from one SQLite snapshot.

    Empty samples yield ``None``, not 100%. Legacy exchange rows with missing
    stable IDs remain in the denominator because omitting them would hide the
    exact migration gap the gate is intended to detect.
    """

    conn.row_factory = sqlite3.Row
    fills = _rows(
        conn,
        """SELECT * FROM execution_events
           WHERE upper(COALESCE(execution_type, '')) = 'TRADE'
             AND ABS(COALESCE(filled_qty, 0.0)) > 0
           ORDER BY id""",
    )
    decisions = _rows(conn, "SELECT * FROM execution_decisions ORDER BY decision_id")
    outbox = _rows(conn, "SELECT * FROM execution_command_outbox ORDER BY intent_id")
    pending = _rows(conn, "SELECT * FROM pending_intents ORDER BY intent_id")
    ledger = _rows(conn, "SELECT * FROM economic_ledger_events ORDER BY id")
    statements = _rows(conn, "SELECT * FROM exchange_statement_entries ORDER BY id")

    decision_cycles = {str(row.get("cycle_id") or "") for row in decisions}
    durable_intents = {
        str(row.get("intent_id") or "") for row in (*outbox, *pending)
    }
    ledger_fill_ids = {
        str(row.get("exchange_fill_id") or "")
        for row in ledger
        if str(row.get("event_type") or "").upper() == "FILL"
        and str(row.get("exchange_fill_id") or "")
    }
    ledger_event_keys = {str(row.get("event_key") or "") for row in ledger}

    complete_fill_ids: list[int] = []
    mapped_fill_ids: list[int] = []
    missing_lineage_counts = {field: 0 for field in LINEAGE_FIELDS}
    missing_lineage_counts.update(
        {"execution_decision": 0, "durable_intent": 0}
    )
    for fill in fills:
        missing = [field for field in LINEAGE_FIELDS if not str(fill.get(field) or "")]
        for field in missing:
            missing_lineage_counts[field] += 1
        cycle_id = str(fill.get("cycle_id") or "")
        intent_id = str(fill.get("intent_id") or "")
        if not cycle_id or cycle_id not in decision_cycles:
            missing_lineage_counts["execution_decision"] += 1
            missing.append("execution_decision")
        if not intent_id or intent_id not in durable_intents:
            missing_lineage_counts["durable_intent"] += 1
            missing.append("durable_intent")
        if not missing:
            complete_fill_ids.append(int(fill["id"]))
        trade_id = str(fill.get("trade_id") or "")
        if trade_id and trade_id in ledger_fill_ids:
            mapped_fill_ids.append(int(fill["id"]))

    account_funding = account_artifact.get("exchange_facts")
    account_funding = account_funding if isinstance(account_funding, dict) else {}
    funding_rows = account_funding.get("funding_statements")
    funding_rows = funding_rows if isinstance(funding_rows, list) else []
    local_statements_by_hash = {
        str(row.get("content_hash") or ""): row
        for row in statements
        if str(row.get("content_hash") or "")
    }
    mapped_funding_hashes: list[str] = []
    for funding in funding_rows:
        if not isinstance(funding, dict):
            continue
        content_hash = str(funding.get("content_hash") or "")
        local = local_statements_by_hash.get(content_hash)
        if local is None:
            continue
        ledger_key = str(local.get("ledger_event_key") or "")
        if ledger_key and ledger_key in ledger_event_keys:
            mapped_funding_hashes.append(content_hash)

    daily_rows: list[Mapping[str, Any]] = []
    daily_attested = False
    if isinstance(daily_reconciliation_artifact, Mapping):
        daily_attested = (
            daily_reconciliation_artifact.get("evidence_kind")
            == "runtime_daily_reconciliation"
            and isinstance(daily_reconciliation_artifact.get("machine_attestation"), Mapping)
            and daily_reconciliation_artifact["machine_attestation"].get("attested")
            is True
        )
        raw_daily = daily_reconciliation_artifact.get("days")
        if daily_attested and isinstance(raw_daily, list):
            daily_rows = [row for row in raw_daily if isinstance(row, Mapping)]
    unexplained_values: list[float] = []
    all_within_precision = bool(daily_rows)
    for row in daily_rows:
        value = row.get("unexplained_max_usd")
        precision = row.get("exchange_precision_usd")
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not isinstance(precision, (int, float))
            or isinstance(precision, bool)
            or float(value) < 0
            or float(precision) < 0
        ):
            all_within_precision = False
            continue
        unexplained_values.append(float(value))
        if float(value) > max(float(precision), 0.01):
            all_within_precision = False

    mapped_effects = len(mapped_fill_ids) + len(mapped_funding_hashes)
    sampled_effects = len(fills) + len(funding_rows)
    return {
        "lineage": {
            "sampled_exchange_trade_updates": len(fills),
            "complete_decision_order_fill_rows": len(complete_fill_ids),
            "complete_execution_event_ids": complete_fill_ids,
            "missing_field_counts": missing_lineage_counts,
            "decision_order_fill_lineage_pct": _percent(
                len(complete_fill_ids), len(fills)
            ),
        },
        "exchange_mapping": {
            "sampled_exchange_trade_updates": len(fills),
            "mapped_exchange_trade_updates": len(mapped_fill_ids),
            "sampled_authenticated_funding_statements": len(funding_rows),
            "mapped_authenticated_funding_statements": len(mapped_funding_hashes),
            "sampled_exchange_effects": sampled_effects,
            "mapped_exchange_effects": mapped_effects,
            "exchange_fill_funding_mapping_pct": _percent(
                mapped_effects, sampled_effects
            ),
        },
        "daily_reconciliation": {
            "artifact_attested": daily_attested,
            "sampled_days": len(daily_rows),
            "daily_unexplained_max_usd": (
                max(unexplained_values) if unexplained_values else None
            ),
            "within_exchange_precision": all_within_precision,
        },
        "database_snapshot": {
            "table_counts": {
                "execution_events": len(_rows(conn, "SELECT id FROM execution_events")),
                "execution_decisions": len(decisions),
                "execution_command_outbox": len(outbox),
                "pending_intents": len(pending),
                "economic_ledger_events": len(ledger),
                "exchange_statement_entries": len(statements),
            },
            "table_row_hashes": {
                "execution_events": _hash_rows(fills),
                "execution_decisions": _hash_rows(decisions),
                "execution_command_outbox": _hash_rows(outbox),
                "pending_intents": _hash_rows(pending),
                "economic_ledger_events": _hash_rows(ledger),
                "exchange_statement_entries": _hash_rows(statements),
            },
        },
    }


def build_phase0_metrics(
    *,
    clean_ci_passed: bool,
    deterministic_causal_replay: bool,
    runtime_measurement: Mapping[str, Any],
) -> dict[str, Any]:
    lineage = runtime_measurement.get("lineage")
    lineage = lineage if isinstance(lineage, Mapping) else {}
    mapping = runtime_measurement.get("exchange_mapping")
    mapping = mapping if isinstance(mapping, Mapping) else {}
    daily = runtime_measurement.get("daily_reconciliation")
    daily = daily if isinstance(daily, Mapping) else {}
    return {
        "clean_ci_passed": clean_ci_passed,
        "decision_order_fill_lineage_pct": lineage.get(
            "decision_order_fill_lineage_pct"
        ),
        "deterministic_causal_replay": deterministic_causal_replay,
        "exchange_fill_funding_mapping_pct": mapping.get(
            "exchange_fill_funding_mapping_pct"
        ),
        "daily_unexplained_max_usd": daily.get("daily_unexplained_max_usd"),
        "within_exchange_precision": daily.get("within_exchange_precision") is True,
    }


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
