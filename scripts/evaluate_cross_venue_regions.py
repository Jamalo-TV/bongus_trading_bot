"""Verify region evidence and apply the fixed best-worst-venue-p99 rule."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from bongus.research.cross_venue.boundary import assert_default_research_boundary  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    boundary_hash = assert_default_research_boundary(__file__)
    args = _parser().parse_args(argv)

    from bongus.research.cross_venue.region_probe import (
        evaluate_region_evidence,
        verify_probe_log,
        write_region_selection_report,
    )

    report = evaluate_region_evidence(tuple(verify_probe_log(path) for path in args.evidence))
    output = write_region_selection_report(report, args.output)
    print(
        json.dumps(
            {
                "boundary_sha256": boundary_hash,
                "output": str(output),
                "status": report.status,
                "selected_region": report.selected_region,
                "selected_worst_venue_p99_ns": report.selected_worst_venue_p99_ns,
                "report_sha256": report.report_sha256,
                "grants_live_authority": report.grants_live_authority,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0 if report.selected_region is not None else 2


if __name__ == "__main__":
    raise SystemExit(main())
