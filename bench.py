import asyncio
import time
import os
from unittest.mock import patch
import requests

# Mock the environment to allow running the check
os.environ["BINANCE_API_KEY"] = "mock_key"
os.environ["USE_TESTNET"] = "false"
os.environ["BINANCE_API_SECRET"] = "mock_secret"

import scripts.live_trader as live_trader

def mock_get(*args, **kwargs):
    time.sleep(1) # simulate slow network I/O
    class MockResp:
        def json(self):
            return [{"symbol": "BTCUSDT", "positionAmt": "0.0"}]
    return MockResp()

async def concurrent_task():
    # Record current event loop time
    loop = asyncio.get_running_loop()
    start = loop.time()
    await asyncio.sleep(0.5)
    end = loop.time()
    return end - start

async def main():
    print("Running with blocking requests.get...")
    with patch('requests.get', side_effect=mock_get):
        loop = asyncio.get_running_loop()
        start_time = loop.time()

        # Start the concurrent task BEFORE calling check_initial_position
        # since check_initial_position blocks the loop
        t2 = asyncio.create_task(concurrent_task())

        # Yield control so t2 can start
        await asyncio.sleep(0.01)

        # Now run the blocking code
        await live_trader.check_initial_position()

        # Wait for t2 to finish
        await t2

        total_time = loop.time() - start_time
        print(f"Total time: {total_time:.2f}s")
        print(f"Concurrent task expected ~0.5s, actually took: {t2.result():.2f}s")

if __name__ == "__main__":
    asyncio.run(main())
