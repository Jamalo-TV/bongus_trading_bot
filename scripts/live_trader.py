"""Canonical script entrypoint for the Bongus live trader."""

from __future__ import annotations

import asyncio

from bongus.runtime.live_trader import main


async def check_initial_position() -> bool:
    """Compatibility shim for local benchmarks from the old runtime."""
    return False


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
