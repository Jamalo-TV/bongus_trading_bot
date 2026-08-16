"""Record one public-only region probe or ingest an exact offline fixture."""

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
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--region", choices=("germany", "france"))
    parser.add_argument("--probe-host-id")
    parser.add_argument("--run-id")
    parser.add_argument("--duration-hours", type=int, default=60)
    parser.add_argument("--sample-interval-seconds", type=int, default=1)
    parser.add_argument("--rest-interval-seconds", type=int, default=5)
    parser.add_argument("--forced-reconnect-interval-seconds", type=int, default=3_600)
    parser.add_argument("--websocket-timeout-milliseconds", type=int, default=750)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    boundary_hash = assert_default_research_boundary(__file__)
    args = _parser().parse_args(argv)
    if (args.fixture is None) == (not args.allow_network):
        raise SystemExit("choose exactly one of offline fixture ingestion or explicit public network mode")

    from bongus.research.cross_venue.region_probe import (
        AppendOnlyProbeLog,
        ProbeRegion,
        load_probe_fixture,
    )

    log = AppendOnlyProbeLog(args.evidence)
    if args.fixture is not None:
        if any((args.region, args.probe_host_id, args.run_id)):
            raise SystemExit("fixture observations already contain their immutable region/run/host tags")
        inserted = log.append_many(load_probe_fixture(args.fixture))
        mode = "fixture"
    else:
        if not all((args.region, args.probe_host_id, args.run_id)):
            raise SystemExit("public network mode requires region, probe-host-id, and run-id")
        from bongus.research.cross_venue.region_probe_network import (
            ProbeRunnerConfig,
            PublicRegionProbeRunner,
            StdlibPublicRegionProbeTransport,
        )

        config = ProbeRunnerConfig(
            duration_hours=args.duration_hours,
            sample_interval_seconds=args.sample_interval_seconds,
            rest_interval_seconds=args.rest_interval_seconds,
            forced_reconnect_interval_seconds=args.forced_reconnect_interval_seconds,
            websocket_timeout_milliseconds=args.websocket_timeout_milliseconds,
        )
        runner = PublicRegionProbeRunner(
            log=log,
            run_id=args.run_id,
            region=ProbeRegion(args.region),
            probe_host_id=args.probe_host_id,
            transport=StdlibPublicRegionProbeTransport(),
            config=config,
        )
        runner.run()
        inserted = 0
        mode = "public_network"
    verification = log.verify()
    print(
        json.dumps(
            {
                "boundary_sha256": boundary_hash,
                "mode": mode,
                "inserted_events": inserted,
                "total_events": len(verification.events),
                "evidence_file_sha256": verification.file_sha256,
                "evidence_chain_sha256": verification.final_chain_sha256,
                "verification_sha256": verification.report_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
