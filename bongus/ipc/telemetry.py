import asyncio
import json
import logging
from typing import Any, AsyncGenerator, Dict, Optional

logger = logging.getLogger(__name__)

class TelemetryClient:
    """Async Client for consuming JSON-line telemetry from the Rust execution engine."""

    def __init__(self, host: str = '127.0.0.1', port: int = 9000):
        self.host = host
        self.port = port

    async def stream_events(self) -> AsyncGenerator[Optional[Dict[str, Any]], None]:
        """
        Connects to the Rust engine and yields parsed JSON events.
        Automatically reconnects if the connection drops.
        """
        while True:
            try:
                reader, _ = await asyncio.open_connection(self.host, self.port)
                logger.info(f"Connected to Rust Exection Engine IPC ({self.host}:{self.port})")

                while True:
                    line = await reader.readline()
                    if not line:
                        logger.warning("Telemetry stream closed by remote.")
                        break

                    # Decode JSON line
                    try:
                        event = json.loads(line.decode('utf-8'))
                        yield event
                    except json.JSONDecodeError as e:
                        logger.error(f"Failed to decode telemetry line: {e}")
                        continue

            except ConnectionRefusedError:
                logger.error(f"Cannot connect to Rust Engine at {self.host}:{self.port}. Retrying in 2s...")
                await asyncio.sleep(2)
            except Exception as e:
                logger.exception(f"Unexpected error in telemetry stream: {e}. Retrying in 2s...")
                await asyncio.sleep(2)
