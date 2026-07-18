"""Retired compatibility entry point for the unsafe testnet dust sweeper.

The former implementation market-sold every eligible free spot balance. Free
balance is not proof that inventory is unrelated to an active hedge, repair
reserve, operator order, or another strategy. The masterplan therefore keeps
all treasury activity proposal-only until a separately reviewed adapter can
consume a complete account reconciliation and reservation snapshot.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping


ENABLE_ENV_VAR = "BONGUS_ENABLE_TESTNET_DUST_SWEEPER"
RETIRED_REASON = (
    "testnet dust sweeper is retired: account-wide liquidation cannot prove "
    "reservation or order ownership safety"
)


def dust_sweeper_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return the legacy flag value for diagnostics, never authorization."""

    source = os.environ if env is None else env
    return str(source.get(ENABLE_ENV_VAR, "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def run_sweeper() -> None:
    """Fail closed even when called directly by stale tooling."""

    raise RuntimeError(RETIRED_REASON)


def main() -> int:
    flag_note = " (legacy enable flag ignored)" if dust_sweeper_enabled() else ""
    print(f"[rebalancer] Disabled permanently{flag_note}: {RETIRED_REASON}.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
