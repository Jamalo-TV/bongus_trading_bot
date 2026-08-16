"""Produce the immutable preregistered B5 evidence report from local data."""

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
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    boundary_hash = assert_default_research_boundary(__file__)
    args = _parser().parse_args(argv)

    from bongus.research.cross_venue.evidence import (
        evaluate_research_evidence,
        load_evidence_fixture,
        write_evidence_report,
    )

    daily, outcomes, windows, evidence = load_evidence_fixture(args.fixture)
    report = evaluate_research_evidence(
        daily_observations=daily,
        outcomes=outcomes,
        windows=windows,
        evidence=evidence,
    )
    output = write_evidence_report(report, args.output)
    print(
        json.dumps(
            {
                "boundary_sha256": boundary_hash,
                "output": str(output),
                "protocol_sha256": report.protocol_sha256,
                "preregistration_sha256": report.preregistration_sha256,
                "dataset_sha256": report.dataset_sha256,
                "report_sha256": report.report_sha256,
                "verdict": report.verdict.status,
                "grants_live_authority": report.verdict.grants_live_authority,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
