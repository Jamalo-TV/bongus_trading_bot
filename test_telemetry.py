import asyncio
from bongus.ipc.telemetry import TelemetryClient

async def main():
    client = TelemetryClient(host='127.0.0.1', port=9000)
    print("Connecting...")
    async for event in client.stream_events():
        print("Received:", event)

asyncio.run(main())
