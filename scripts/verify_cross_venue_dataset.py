"""Deep-verify immutable cross-venue Parquet evidence and retention metadata."""

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
    parser.add_argument("dataset_root", type=Path)
    parser.add_argument("--as-of-time-ns", required=True, type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    boundary_hash = assert_default_research_boundary(__file__)
    args = _parser().parse_args(argv)

    from bongus.research.cross_venue.artifacts import (
        audit_retention,
        load_artifact_manifest,
        verify_dataset,
    )

    report = verify_dataset(args.dataset_root)
    manifests = tuple(
        load_artifact_manifest(path)
        for path in sorted(args.dataset_root.resolve().rglob("*.parquet.manifest.json"), key=str)
    )
    retention = audit_retention(manifests, as_of_time_ns=args.as_of_time_ns)
    print(
        json.dumps(
            {
                "boundary_sha256": boundary_hash,
                "dataset_report_sha256": report.report_sha256,
                "retention_report_sha256": retention.report_sha256,
                "valid": report.valid,
                "manifest_count": report.manifest_count,
                "row_count": report.row_count,
                "byte_count": report.byte_count,
                "parquet_backend": report.parquet_backend,
                "conflicting_event_ids": report.conflicting_event_ids,
                "future_data_event_ids": report.future_data_event_ids,
                "orphan_parquet_paths": report.orphan_parquet_paths,
                "temporary_paths": report.temporary_paths,
                "wall_clock_gates": {
                    "decision_anchor_coverage": "evidence_required",
                    "fresh_anchor_fraction": "evidence_required",
                    "finalized_funding_reconciliation": "evidence_required",
                    "storage_sizing_pilot_48h": "evidence_required",
                    "settlement_window_top20_burst": "evidence_required",
                    "daily_offload_and_retention_operation": "evidence_required",
                    "isolated_systemd_host_activation": "evidence_required",
                    "forward_oos_90_to_180_days": "evidence_required",
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0 if report.valid else 2


if __name__ == "__main__":
    raise SystemExit(main())
