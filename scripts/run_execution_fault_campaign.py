"""Run the Phase 1 execution fault gate and persist machine-readable evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bongus.testing.execution_fault_campaign import (  # noqa: E402
    run_parallel_execution_fault_campaign,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--traces", type=int, default=1_000_000)
    parser.add_argument("--seed", type=int, default=20_260_718)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run_parallel_execution_fault_campaign(
        traces=args.traces,
        seed=args.seed,
        workers=args.workers,
    )
    payload = result.to_dict()
    rendered = json.dumps(payload, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
