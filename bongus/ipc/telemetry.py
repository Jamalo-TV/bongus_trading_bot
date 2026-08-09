"""Async MessagePack telemetry transport for the Rust execution engine.

Observer consumers use :meth:`TelemetryClient.stream_events` and never ACK.
The authoritative trader uses :meth:`TelemetryClient.stream_deliveries` and
ACKs a durable delivery only after its handler has committed the event.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

TELEMETRY_SCHEMA_VERSION = 1
DEFAULT_PRIMARY_CONSUMER_ID = "python-live-trader"
MAX_CONSUMER_ID_BYTES = 128
MAX_TELEMETRY_BUFFER_BYTES = 1024 * 1024

ConnectionStateHandler = Callable[[bool], Awaitable[None] | None]


class TelemetryProtocolError(RuntimeError):
    """The Rust telemetry stream violated the versioned wire contract."""


class TelemetryAcknowledgementError(RuntimeError):
    """A durable delivery could not be acknowledged safely."""


def _validate_consumer_id(consumer_id: str | None) -> str | None:
    if consumer_id is None:
        return None
    normalized = consumer_id.strip()
    encoded_length = len(normalized.encode("utf-8"))
    if not normalized or encoded_length > MAX_CONSUMER_ID_BYTES:
        raise ValueError(
            f"telemetry consumer_id must contain 1..{MAX_CONSUMER_ID_BYTES} UTF-8 bytes"
        )
    return normalized


def durable_telemetry_sequence(event: dict[str, Any]) -> int | None:
    """Return the durable sequence, or ``None`` for legacy/ephemeral events.

    Rust deliberately leaves high-rate market-data events byte-compatible and
    undecorated. Only an explicit boolean ``telemetry_ack_required=true`` opts
    an event into the durable protocol.
    """

    ack_required = event.get("telemetry_ack_required")
    if ack_required is None or ack_required is False:
        return None
    if ack_required is not True:
        raise TelemetryProtocolError("telemetry_ack_required must be a boolean")

    schema_version = event.get("telemetry_schema_version")
    sequence = event.get("telemetry_sequence")
    replay = event.get("telemetry_replay")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != TELEMETRY_SCHEMA_VERSION
    ):
        raise TelemetryProtocolError(
            f"unsupported durable telemetry schema: {schema_version!r}"
        )
    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence <= 0
        or sequence > (2**64 - 1)
    ):
        raise TelemetryProtocolError(f"invalid durable telemetry sequence: {sequence!r}")
    if not isinstance(replay, bool):
        raise TelemetryProtocolError("durable telemetry_replay must be a boolean")
    return sequence


@dataclass(slots=True)
class TelemetryDelivery:
    """One decoded event and an explicit, connection-bound ACK operation."""

    event: dict[str, Any]
    sequence: int | None
    _writer: Any = field(repr=False)
    _consumer_id: str | None = field(repr=False)
    _write_lock: asyncio.Lock = field(repr=False)
    _acknowledged: bool = field(default=False, init=False, repr=False)

    @property
    def ack_required(self) -> bool:
        return self.sequence is not None

    @property
    def acknowledged(self) -> bool:
        return self._acknowledged

    async def acknowledge(self) -> bool:
        """Write one newline-delimited cumulative ACK.

        Returns ``False`` for a repeated local call. Observer deliveries have
        no consumer identity and cannot accidentally advance Rust's cursor.
        """

        if self.sequence is None:
            return False
        if self._acknowledged:
            return False
        if self._consumer_id is None:
            raise TelemetryAcknowledgementError(
                "observer telemetry clients are not allowed to acknowledge durable events"
            )
        payload = {
            "event": "TelemetryAck",
            "schema_version": TELEMETRY_SCHEMA_VERSION,
            "consumer_id": self._consumer_id,
            "high_water_sequence": self.sequence,
        }
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"
        async with self._write_lock:
            try:
                self._writer.write(encoded)
                await self._writer.drain()
            except (ConnectionError, OSError, RuntimeError) as exc:
                raise TelemetryAcknowledgementError(
                    f"could not acknowledge telemetry sequence {self.sequence}: {exc}"
                ) from exc
        self._acknowledged = True
        return True


async def _notify_connection_state(
    handler: ConnectionStateHandler | None,
    connected: bool,
) -> None:
    if handler is None:
        return
    result = handler(connected)
    if inspect.isawaitable(result):
        await result


async def _close_writer(writer: Any) -> None:
    if writer is None:
        return
    try:
        writer.close()
    except Exception:
        return
    try:
        result = writer.wait_closed()
        if inspect.isawaitable(result):
            await result
    except Exception:
        pass


class TelemetryClient:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9000,
        *,
        reconnect_delay: float = 2.0,
        read_timeout: float | None = None,
        max_buffer_bytes: int = MAX_TELEMETRY_BUFFER_BYTES,
    ) -> None:
        if reconnect_delay < 0:
            raise ValueError("reconnect_delay cannot be negative")
        if read_timeout is not None and read_timeout <= 0:
            raise ValueError("read_timeout must be positive")
        if max_buffer_bytes <= 0:
            raise ValueError("max_buffer_bytes must be positive")
        self.host = host
        self.port = port
        self.reconnect_delay = reconnect_delay
        self.read_timeout = read_timeout
        self.max_buffer_bytes = max_buffer_bytes

    async def probe(self, timeout: float = 2.0) -> bool:
        writer = None
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=timeout
            )
            return True
        except Exception:
            return False
        finally:
            await _close_writer(writer)

    async def stream_deliveries(
        self,
        *,
        consumer_id: str | None = None,
        on_connection_state: ConnectionStateHandler | None = None,
    ) -> AsyncGenerator[TelemetryDelivery, None]:
        """Reconnect forever and yield explicit delivery/ACK handles.

        Merely iterating does not acknowledge anything. This is intentional:
        observers may inspect durable events, but only the primary subscriber
        supplies Rust's configured consumer ID and calls ``acknowledge()``.
        """

        import msgpack

        normalized_consumer_id = _validate_consumer_id(consumer_id)
        while True:
            writer = None
            connected = False
            sleep_before_retry = False
            try:
                reader, writer = await asyncio.open_connection(
                    self.host,
                    self.port,
                    limit=self.max_buffer_bytes,
                )
                connected = True
                await _notify_connection_state(on_connection_state, True)
                logger.info(
                    "Connected to Rust execution engine IPC (%s:%s)",
                    self.host,
                    self.port,
                )
                unpacker = msgpack.Unpacker(
                    raw=False,
                    strict_map_key=False,
                    max_buffer_size=self.max_buffer_bytes,
                )
                write_lock = asyncio.Lock()
                while True:
                    read = reader.read(65536)
                    chunk = (
                        await asyncio.wait_for(read, timeout=self.read_timeout)
                        if self.read_timeout is not None
                        else await read
                    )
                    if not chunk:
                        logger.warning("Telemetry stream closed by remote.")
                        break
                    unpacker.feed(chunk)
                    for raw_event in unpacker:
                        if not isinstance(raw_event, dict):
                            raise TelemetryProtocolError(
                                "telemetry MessagePack value must be a map"
                            )
                        event = dict(raw_event)
                        sequence = durable_telemetry_sequence(event)
                        yield TelemetryDelivery(
                            event=event,
                            sequence=sequence,
                            _writer=writer,
                            _consumer_id=normalized_consumer_id,
                            _write_lock=write_lock,
                        )
            except asyncio.CancelledError:
                raise
            except ConnectionRefusedError:
                sleep_before_retry = True
                logger.error(
                    "Cannot connect to Rust engine at %s:%s. Retrying in %.1fs...",
                    self.host,
                    self.port,
                    self.reconnect_delay,
                )
            except Exception as exc:
                sleep_before_retry = True
                logger.exception(
                    "Unexpected telemetry error: %s. Retrying in %.1fs...",
                    exc,
                    self.reconnect_delay,
                )
            finally:
                if connected:
                    try:
                        await _notify_connection_state(on_connection_state, False)
                    except Exception:
                        logger.exception("Telemetry connection-state callback failed")
                await _close_writer(writer)
            if sleep_before_retry and self.reconnect_delay:
                await asyncio.sleep(self.reconnect_delay)

    async def stream_events(self) -> AsyncGenerator[dict[str, Any] | None, None]:
        """Backward-compatible observer stream that intentionally never ACKs."""

        deliveries = self.stream_deliveries()
        try:
            async for delivery in deliveries:
                yield delivery.event
        finally:
            await deliveries.aclose()
