"""Deprecated import compatibility for the sole supervised trader runtime.

The executable implementation lives in :mod:`scripts.live_trader_v2`.  This
module deliberately contains no scanner, allocator, execution, or lifecycle
logic; retaining a second implementation made fixes and safety invariants
dependent on which entry point happened to be launched.
"""

from scripts.live_trader_v2 import LiveTraderV2, main, run_cli

# Preserve the historical import name without preserving a second runtime.
CanonicalMultiSymbolTrader = LiveTraderV2

__all__ = ["CanonicalMultiSymbolTrader", "LiveTraderV2", "main", "run_cli"]


if __name__ == "__main__":
    run_cli()
