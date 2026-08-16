"""Run the preregistered purged walk-forward evaluation on a local fixture."""

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
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    boundary_hash = assert_default_research_boundary(__file__)
    args = _parser().parse_args(argv)

    from bongus.research.cross_venue.evaluation import (
        PurgedWalkForwardEvaluator,
        load_evaluation_fixture,
        write_evaluation_report,
    )

    outcomes, windows = load_evaluation_fixture(args.fixture)
    report = PurgedWalkForwardEvaluator().evaluate(outcomes, windows)
    output = write_evaluation_report(report, args.output)
    print(
        json.dumps(
            {
                "boundary_sha256": boundary_hash,
                "output": str(output),
                "preregistration_sha256": report.preregistration_sha256,
                "protocol_sha256": report.protocol_sha256,
                "report_sha256": report.report_sha256,
                "windows": len(report.windows),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
