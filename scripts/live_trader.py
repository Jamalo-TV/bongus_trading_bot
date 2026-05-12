"""Canonical script entrypoint for the Bongus live trader."""

from __future__ import annotations

import asyncio

from bongus.runtime.live_trader import main


async def check_initial_position() -> bool:
    """Compatibility shim for local benchmarks from the old runtime."""
    return False


if __name__ == "__main__":
    print("Starting live trader script...")
    try:
        print("Running main()...")
        asyncio.run(main())
    except KeyboardInterrupt:
        print("KeyboardInterrupt")
    except Exception as e:
        print(f"Exception in main: {e}")
        import traceback
        traceback.print_exc()
