"""Verify and summarize a deterministic cross-venue evaluation report."""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Mapping, Sequence

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from bongus.research.cross_venue.boundary import assert_default_research_boundary  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    return parser


def _baseline_totals(payload: Mapping[str, object]) -> tuple[Decimal, Decimal, int]:
    total_net = Decimal("0")
    total_capital_days = Decimal("0")
    outcomes = 0
    windows = payload.get("windows")
    if not isinstance(windows, list):
        raise ValueError("evaluation report windows must be an array")
    for window in windows:
        if not isinstance(window, Mapping):
            raise ValueError("evaluation report window must be an object")
        scenarios = window.get("scenario_metrics")
        if not isinstance(scenarios, list):
            raise ValueError("scenario_metrics must be an array")
        baseline = next(
            (value for value in scenarios if isinstance(value, Mapping) and value.get("scenario_name") == "baseline"),
            None,
        )
        if baseline is None:
            raise ValueError("evaluation report lacks baseline metrics")
        total_net += Decimal(str(baseline["total_net_pnl_usd"]))
        total_capital_days += Decimal(str(baseline["total_reserved_capital_days"]))
        outcomes += int(baseline["outcomes"])
    return total_net, total_capital_days, outcomes


def main(argv: Sequence[str] | None = None) -> int:
    boundary_hash = assert_default_research_boundary(__file__)
    args = _parser().parse_args(argv)

    from bongus.research.cross_venue.evaluation import verify_evaluation_report

    payload = verify_evaluation_report(args.report)
    total_net, total_capital_days, outcomes = _baseline_totals(payload)
    annualized = total_net / total_capital_days * Decimal("365") if total_capital_days > 0 else Decimal("0")
    print(
        json.dumps(
            {
                "boundary_sha256": boundary_hash,
                "protocol_id": payload["protocol_id"],
                "preregistration_sha256": payload["preregistration_sha256"],
                "report_sha256": payload["report_sha256"],
                "baseline_outcomes": outcomes,
                "baseline_total_net_pnl_usd": format(total_net, "f"),
                "baseline_simple_annualized_return": format(annualized, "f"),
                "authority": "research_evidence_only",
                "statistical_verdict": "not_computed_by_descriptive_report_cli",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
