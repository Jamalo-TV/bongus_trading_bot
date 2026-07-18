"""Deprecated CLI delegate for :mod:`scripts.live_trader_v2`.

New process definitions must use ``python -m scripts.live_trader_v2``.
"""

from scripts.live_trader_v2 import LiveTraderV2, main, run_cli

CanonicalMultiSymbolTrader = LiveTraderV2


async def check_initial_position() -> bool:
    """Compatibility shim retained for old local probes."""

    return False


if __name__ == "__main__":
    run_cli()
