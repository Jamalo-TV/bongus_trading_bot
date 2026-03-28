"""Asyncio TCP client that subscribes to the Rust engine's port 9000 broadcast.

The Rust engine emits newline-delimited JSON. We use StreamReader.readline()
to handle TCP packet fragmentation automatically — never .read().

Expected event shapes:
  {"event": "L2Depth", "symbol": "BTCUSDT", "market": "spot"|"perp",
   "bids": [[price, qty], ...], "asks": [[price, qty], ...]}

  {"event": "OrderUpdate", "symbol": "BTCUSDT", "status": "FILLED",
   "filled_qty": 0.1, "client_order_id": "abc123"}

  {"event": "MarkPrice", "symbol": "BTCUSDT",
   "mark_price": 65000.0, "next_funding_rate": 0.0001}
"""

import asyncio
import json
import logging
import os
from typing import Callable, Any

logger = logging.getLogger(__name__)


class RustDataSubscriber:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9000,
        on_depth: Callable[..., None] | None = None,
        on_order_update: Callable[..., None] | None = None,
        on_mark_price: Callable[..., None] | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._on_depth = on_depth
        self._on_order_update = on_order_update
        self._on_mark_price = on_mark_price
        self._reconnect_delay = 1.0
        self._trading_mode = os.getenv("TRADING_MODE", "paper").lower()
        logger.info("RustDataSubscriber initialized (TRADING_MODE=%s)", self._trading_mode)

    async def run(self) -> None:
        """Connect to Rust engine and process events indefinitely with reconnect."""
        while True:
            writer = None
            try:
                reader, writer = await asyncio.open_connection(self._host, self._port)
                self._reconnect_delay = 1.0
                logger.info("Connected to Rust engine at %s:%d", self._host, self._port)
                await self._read_loop(reader)
            except (ConnectionRefusedError, OSError) as exc:
                logger.warning(
                    "Cannot connect to Rust engine (%s). Retrying in %.1fs",
                    exc, self._reconnect_delay,
                )
            except Exception as exc:
                logger.error("Unexpected error in RustDataSubscriber: %s", exc)
            finally:
                if writer is not None:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except Exception:
                        pass

            await asyncio.sleep(self._reconnect_delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, 30.0)

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        """Read newline-delimited JSON lines and dispatch to callbacks."""
        while True:
            line = await reader.readline()
            if not line:
                logger.warning("Rust engine closed connection — reconnecting")
                return

            try:
                event = json.loads(line.decode())
            except json.JSONDecodeError as exc:
                logger.warning("Failed to parse event from Rust: %s | raw: %r", exc, line[:200])
                continue

            self._dispatch(event)

    def _dispatch(self, event: dict[str, Any]) -> None:
        event_type = event.get("event")

        if event_type == "L2Depth" and self._on_depth is not None:
            self._on_depth(
                symbol=event.get("symbol", "").upper(),
                market=event.get("market", ""),
                bids=event.get("bids", []),
                asks=event.get("asks", []),
            )
        elif event_type == "OrderUpdate" and self._on_order_update is not None:
            self._on_order_update(
                # Normalize to uppercase — Binance symbols are always uppercase
                # and _exit_events keys are stored as uppercase from config.
                symbol=event.get("symbol", "").upper(),
                status=event.get("status", ""),
                filled_qty=event.get("filled_qty", 0.0),
                client_order_id=event.get("client_order_id", ""),
            )
        elif event_type == "MarkPrice" and self._on_mark_price is not None:
            self._on_mark_price(
                symbol=event.get("symbol", "").upper(),
                mark_price=event.get("mark_price", 0.0),
                next_funding_rate=event.get("next_funding_rate", 0.0),
            )
