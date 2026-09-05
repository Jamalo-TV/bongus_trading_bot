"""Evaluate offline carry evidence or a read-only economic-ledger snapshot.

See docs/carry_economics.md for the explicit input contract. This command does
not read environment credentials or send any exchange request. Output files
are created exclusively and can never overwrite an existing evidence file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bongus.research.carry_economics import (
    CarryPortfolioWindow,
    CarryWindow,
    FundingSettlement,
    OperatingCost,
    capital_scenarios,
    compare_carry_to_baseline,
    decimal,
    evidence_digest,
    json_value,
    ledger_cost_report,
    research_evidence_gate,
    utc,
)


def _costs(items: list[dict[str, Any]]) -> tuple[OperatingCost, ...]:
    return tuple(OperatingCost(**item) for item in items)


def _window(values: dict[str, Any]) -> CarryWindow | CarryPortfolioWindow:
    result = dict(values)
    for key in ("start", "end", "policy_frozen_at", "data_cutoff"):
        result[key] = utc(result[key])
    result["operating_costs"] = _costs(result.get("operating_costs", []))
    if "cycles" in result:
        cycles = tuple(_window(cycle) for cycle in result["cycles"])
        if any(not isinstance(cycle, CarryWindow) for cycle in cycles):
            raise ValueError("nested cycle portfolios are not supported")
        result["cycles"] = cycles
        return CarryPortfolioWindow(**result)
    result["settlements"] = tuple(
        FundingSettlement(**{**item, "settlement_time": utc(item["settlement_time"]),
                             "available_at": utc(item["available_at"])})
        for item in result.get("settlements", [])
    )
    return CarryWindow(**result)


def build_report(payload: dict[str, Any], *, ledger_db: Path | None = None) -> dict[str, Any]:
    mode = payload.get("mode")
    if mode == "unit_economics":
        if ledger_db is not None:
            raise ValueError("unit economics mode does not use an economic ledger")
        return capital_scenarios(
            capitals_usd=tuple(decimal(value, "capital") for value in payload["capitals_usd"]),
            reserved_fraction=decimal(payload["reserved_fraction"], "reserved fraction"),
            annual_net_edge_on_reserved_before_opex=decimal(
                payload["annual_net_edge_on_reserved_before_opex"], "annual net edge"),
            monthly_opex_usd=decimal(payload["monthly_opex_usd"], "monthly operating cost"),
            assumption_label=payload["assumption_label"],
        )
    if mode == "comparison":
        if ledger_db is not None:
            raise ValueError("comparison mode does not use an economic ledger")
        comparisons = tuple(compare_carry_to_baseline(_window(pair["candidate"]), _window(pair["baseline"]))
                            for pair in payload["comparisons"])
        return {
            "comparisons": comparisons,
            "evidence_gate": research_evidence_gate(comparisons, expected_digest=payload.get("expected_digest", "")),
        }
    if mode != "actual" or ledger_db is None:
        raise ValueError("use mode=unit_economics/comparison, or mode=actual with --ledger-db")
    if not ledger_db.is_file():
        raise ValueError("economic ledger database must already exist")
    # mode=ro plus a read transaction provides one consistent SQLite view,
    # including committed WAL data. Do not use immutable=1 on a live WAL file.
    with sqlite3.connect(ledger_db.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        return ledger_cost_report(
            conn, account_id=payload["account_id"], trading_mode=payload["trading_mode"],
            start_time=payload["start_time"], end_time=payload["end_time"],
            nav_inputs=payload.get("nav_inputs", {}),
            ledger_reconciled=payload.get("ledger_reconciled") is True,
            average_reserved_capital_usd=decimal(payload["average_reserved_capital_usd"], "reserved capital"),
            operating_costs=_costs(payload.get("operating_costs", [])),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Explicit offline JSON evidence and assumptions")
    parser.add_argument("--input-sha256", help="Reject the input if it differs from this pinned SHA-256")
    parser.add_argument("--ledger-db", type=Path, help="Existing economic ledger database, opened read-only")
    parser.add_argument("--output", type=Path, help="New immutable JSON artifact; omit for stdout")
    args = parser.parse_args(argv)
    try:
        raw = args.input.read_bytes()
        input_hash = hashlib.sha256(raw).hexdigest()
        if args.input_sha256 is not None and args.input_sha256 != input_hash:
            raise ValueError("input SHA-256 mismatch")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("input must be a JSON object")
        report = {
            "schema_version": 1, "source_input_sha256": input_hash,
            "result": build_report(payload, ledger_db=args.ledger_db),
            "live_activation_authorized": False, "capital_increase_authorized": False,
        }
        report["report_digest"] = evidence_digest(report)
        encoded = json.dumps(json_value(report), indent=2, sort_keys=True, allow_nan=False) + "\n"
        if args.output is None:
            print(encoded, end="")
        else:
            with args.output.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as exc:
        parser.exit(2, f"carry economics: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
