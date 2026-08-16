"""Replay exact local cross-venue fixtures in availability order."""

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
    parser.add_argument("--database", help="optional dedicated research.db output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    boundary_hash = assert_default_research_boundary(__file__)
    args = _parser().parse_args(argv)

    from bongus.research.cross_venue.replay import FixtureReplay, load_raw_snapshot_fixture
    from bongus.research.cross_venue.storage import ResearchStore

    records = load_raw_snapshot_fixture(args.fixture)
    if args.database:
        with ResearchStore(args.database) as store:
            result = FixtureReplay().run(records, store=store)
    else:
        result = FixtureReplay().run(records)
    print(
        json.dumps(
            {
                "boundary_sha256": boundary_hash,
                "processed_events": result.processed_events,
                "exact_duplicates": result.exact_duplicates,
                "input_reordered": result.input_reordered,
                "first_available_time_ns": result.first_available_time_ns,
                "last_available_time_ns": result.last_available_time_ns,
                "report_sha256": result.report_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
