"""Run the immutable offline B0 Binance-Hyperliquid futility screen."""

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
    parser.add_argument("artifact", type=Path, help="canonical sealed finalized-funding JSON artifact")
    parser.add_argument("--output", required=True, type=Path, help="new immutable B0 report path")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    boundary_hash = assert_default_research_boundary(__file__)
    args = _parser().parse_args(argv)

    from bongus.research.cross_venue.historical import (
        evaluate_historical_feasibility,
        load_historical_artifact,
        load_historical_screen_policy,
        write_historical_report,
    )

    artifact = load_historical_artifact(args.artifact)
    policy, preregistration_sha256 = load_historical_screen_policy()
    report = evaluate_historical_feasibility(
        artifact,
        policy=policy,
        preregistration_sha256=preregistration_sha256,
    )
    output = write_historical_report(report, args.output)
    print(
        json.dumps(
            {
                "boundary_sha256": boundary_hash,
                "input_content_sha256": report["input_content_sha256"],
                "output": str(output),
                "report_sha256": report["report_sha256"],
                "verdict": report["verdict"],
                "grants_live_authority": report["grants_live_authority"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
