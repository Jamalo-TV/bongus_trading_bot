"""Collect public Binance/Hyperliquid evidence into a dedicated research.db."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Sequence

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from bongus.research.cross_venue.boundary import assert_default_research_boundary  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="research.db")
    parser.add_argument(
        "--artifact-root",
        required=True,
        type=Path,
        help="required immutable Zstd Parquet dataset root",
    )
    parser.add_argument("--fixture", type=Path)
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help="explicitly permit one public-only collection cycle",
    )
    parser.add_argument("--no-books", action="store_true")
    parser.add_argument("--no-funding-history", action="store_true")
    parser.add_argument(
        "--continuous",
        action="store_true",
        help="run the frozen cadence scheduler; requires explicit public network mode",
    )
    parser.add_argument(
        "--startup-check",
        action="store_true",
        help="fail-closed backend/store/backlog check without public network access",
    )
    parser.add_argument(
        "--artifact-flush-seconds",
        type=int,
        default=30,
        help="batch immutable artifacts while SQLite remains the immediate journal",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    boundary_hash = assert_default_research_boundary(__file__)
    args = _parser().parse_args(argv)
    selected_modes = sum(
        (
            args.fixture is not None,
            args.allow_network,
            args.startup_check,
        )
    )
    if selected_modes != 1:
        raise SystemExit("choose exactly one of fixture, explicit public network, or startup check")
    if args.fixture is not None and args.allow_network:
        raise SystemExit("choose fixture ingestion or public network collection, not both")
    if args.continuous and (args.fixture is not None or not args.allow_network):
        raise SystemExit("continuous collection requires explicit public network mode")
    if args.startup_check and args.continuous:
        raise SystemExit("startup check cannot run the continuous scheduler")
    if args.artifact_flush_seconds <= 0:
        raise SystemExit("artifact flush cadence must be a positive integer")

    from bongus.research.cross_venue.artifacts import ParquetArtifactWriter
    from bongus.research.cross_venue.cadence import cadence_for_dataset
    from bongus.research.cross_venue.collector import PublicResearchCollector
    from bongus.research.cross_venue.publication import ResearchArtifactPublisher
    from bongus.research.cross_venue.replay import FixtureReplay, load_raw_snapshot_fixture
    from bongus.research.cross_venue.storage import ResearchStore

    publisher = ResearchArtifactPublisher(ParquetArtifactWriter(args.artifact_root))
    with ResearchStore(args.database) as store:
        backlog = publisher.publish_store(store)
        if args.startup_check:
            output = {
                "mode": "startup_check",
                "backlog_raw_rows": backlog.raw_rows,
                "backlog_opportunity_rows": backlog.opportunity_rows,
                "backlog_gap_rows": backlog.gap_rows,
                "publication_report_sha256": backlog.report_sha256,
            }
        elif args.fixture is not None:
            result = FixtureReplay().run(
                load_raw_snapshot_fixture(args.fixture),
                store=store,
            )
            publication = publisher.publish_store(store)
            output = {
                "mode": "fixture",
                "processed_events": result.processed_events,
                "exact_duplicates": result.exact_duplicates,
                "report_sha256": result.report_sha256,
                "artifact_root": str(args.artifact_root.resolve()),
                "published_artifacts": publication.published_artifacts,
                "publication_report_sha256": publication.report_sha256,
            }
        else:
            collector = PublicResearchCollector(store)
            if args.continuous:
                last_run_ns: dict[tuple[str, str, str, str], int] = {}
                pending_records = []
                last_publication_ns = time.monotonic_ns()
                while True:
                    now_ns = time.monotonic_ns()
                    due = []
                    for target in collector.targets(
                        include_books=not args.no_books,
                        include_funding_history=not args.no_funding_history,
                    ):
                        contract = cadence_for_dataset(target.dataset)
                        interval_ns = contract.normal_interval_ns or contract.maximum_lateness_ns
                        identity = (
                            target.dataset,
                            target.venue.value,
                            target.venue_symbol,
                            target.endpoint,
                        )
                        previous = last_run_ns.get(identity)
                        if previous is None or now_ns - previous >= interval_ns:
                            due.append(target)
                            last_run_ns[identity] = now_ns
                    if due:
                        result = collector.collect_targets(tuple(due))
                        pending_records.extend(result.records)
                        publication = None
                        if now_ns - last_publication_ns >= args.artifact_flush_seconds * 1_000_000_000:
                            publication = publisher.publish_records(raw_records=tuple(pending_records))
                            pending_records.clear()
                            last_publication_ns = now_ns
                        print(
                            json.dumps(
                                {
                                    "boundary_sha256": boundary_hash,
                                    "mode": "public_network_continuous",
                                    "inserted_snapshots": result.inserted_snapshots,
                                    "exact_duplicates": result.exact_duplicates,
                                    "failed_snapshots": result.failed_snapshots,
                                    "pending_artifact_rows": len(pending_records),
                                    "published_artifacts": (
                                        publication.published_artifacts if publication is not None else 0
                                    ),
                                },
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            flush=True,
                        )
                    time.sleep(0.2)
            result = collector.collect_once(
                include_books=not args.no_books,
                include_funding_history=not args.no_funding_history,
            )
            publication = publisher.publish_records(raw_records=result.records)
            output = {
                "mode": "public_network",
                "inserted_snapshots": result.inserted_snapshots,
                "exact_duplicates": result.exact_duplicates,
                "failed_snapshots": result.failed_snapshots,
                "event_ids": result.event_ids,
                "published_artifacts": publication.published_artifacts,
                "publication_report_sha256": publication.report_sha256,
            }
    output["boundary_sha256"] = boundary_hash
    print(json.dumps(output, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
