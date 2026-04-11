"""Async client for JSON-line telemetry from the Rust execution engine."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncGenerator

logger = logging.getLogger(__name__)


class TelemetryClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 9000):
        self.host = host
        self.port = port

    async def probe(self, timeout: float = 2.0) -> bool:
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), timeout=timeout)
            writer.close()
            await writer.wait_closed()
            return True
        except Exception:
            return False

    async def stream_events(self) -> AsyncGenerator[dict[str, Any] | None, None]:
        while True:
            try:
                # 256 KB buffer comfortably absorbs bursts of many small JSON
                # events (e.g. 100 symbols × multiple events/tick) without
                # stalling the reader on a short asyncio.readline() call.
                reader, _ = await asyncio.open_connection(
                    self.host, self.port, limit=256 * 1024
                )
                logger.info("Connected to Rust execution engine IPC (%s:%s)", self.host, self.port)

                async for line in reader:
                    if not line:
                        logger.warning("Telemetry stream closed by remote.")
                        break
                    try:
                        yield json.loads(line.decode("utf-8"))
                    except json.JSONDecodeError as exc:
                        logger.error("Failed to decode telemetry line: %s", exc)
                        continue
            except ConnectionRefusedError:
                logger.error("Cannot connect to Rust engine at %s:%s. Retrying in 2s...", self.host, self.port)
                await asyncio.sleep(2)
            except Exception as exc:
                logger.exception("Unexpected telemetry error: %s. Retrying in 2s...", exc)
                await asyncio.sleep(2)
