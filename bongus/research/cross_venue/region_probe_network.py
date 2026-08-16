"""Credential-free REST/WebSocket measurement for the region probe harness."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import socket
import ssl
import struct
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Final, Protocol, cast

from bongus.research.cross_venue.feeds import (
    BinancePublicFeeds,
    HyperliquidPublicFeeds,
    StdlibJsonTransport,
)
from bongus.research.cross_venue.region_probe import (
    NANOSECONDS_PER_HOUR,
    NANOSECONDS_PER_SECOND,
    PROBE_PROTOCOL_VERSION,
    AppendOnlyProbeLog,
    ProbeMetric,
    ProbeObservation,
    ProbeRegion,
)
from bongus.research.cross_venue.schema import CanonicalAsset, Venue
from bongus.research.cross_venue.storage import canonical_json_bytes

_BINANCE_WS_HOST: Final[str] = "fstream.binance.com"
_BINANCE_WS_PATH: Final[str] = "/ws/btcusdt@markPrice@1s"
_HYPERLIQUID_WS_HOST: Final[str] = "api.hyperliquid.xyz"
_HYPERLIQUID_WS_PATH: Final[str] = "/ws"
_WEBSOCKET_GUID: Final[str] = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_HANDSHAKE_BYTES: Final[int] = 16_384
_MAX_FRAME_BYTES: Final[int] = 2_000_000


class RegionProbeTransportError(RuntimeError):
    """A fixed public probe transport failed or violated its contract."""


class RegionProbeReadTimeout(RegionProbeTransportError):
    """No public WebSocket event arrived within the scheduled sample window."""


@dataclass(frozen=True, slots=True)
class RestProbeSample:
    capture_time_ns: int
    receive_time_ns: int
    rtt_ns: int
    connection_id: str


@dataclass(frozen=True, slots=True)
class WsProbeSample:
    source_event_time_ns: int
    receive_time_ns: int
    connection_id: str
    sequence_id: str
    quality_flags: tuple[str, ...] = ()


class RegionProbeTransport(Protocol):
    def rest_sample(self, venue: Venue) -> RestProbeSample: ...

    def ws_sample(self, venue: Venue, *, timeout_milliseconds: int) -> WsProbeSample: ...

    def reconnect(
        self,
        venue: Venue,
        *,
        timeout_milliseconds: int,
    ) -> tuple[int, WsProbeSample]: ...

    def close(self) -> None: ...


def _exact_json(payload: bytes) -> object:
    try:
        return json.loads(
            payload.decode("utf-8"),
            parse_float=Decimal,
            parse_int=int,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RegionProbeTransportError("public WebSocket returned invalid exact JSON") from exc


def _milliseconds(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise RegionProbeTransportError(f"{field_name} must be exact milliseconds")
    try:
        result = int(value)
    except ValueError as exc:
        raise RegionProbeTransportError(f"{field_name} must be exact milliseconds") from exc
    if result < 0 or (not isinstance(value, int) and value.strip() != str(result)):
        raise RegionProbeTransportError(f"{field_name} must be exact milliseconds")
    return result


class _FixedPublicWebSocket:
    """Minimal RFC6455 client restricted to the two fixed public market feeds."""

    def __init__(self, venue: Venue, connection_number: int, *, timeout_seconds: int = 10) -> None:
        if not isinstance(venue, Venue):
            raise TypeError("WebSocket venue must use the fixed enum")
        if isinstance(connection_number, bool) or not isinstance(connection_number, int) or connection_number <= 0:
            raise ValueError("connection_number must be positive")
        self.venue = venue
        self.connection_id = f"region-probe-{venue.value}-{connection_number}"
        self._host, self._path = (
            (_BINANCE_WS_HOST, _BINANCE_WS_PATH)
            if venue is Venue.BINANCE
            else (_HYPERLIQUID_WS_HOST, _HYPERLIQUID_WS_PATH)
        )
        self._buffer = bytearray()
        self._socket = self._connect(timeout_seconds)
        if venue is Venue.HYPERLIQUID:
            self._send_text(
                canonical_json_bytes(
                    {
                        "method": "subscribe",
                        "subscription": {"type": "l2Book", "coin": "BTC"},
                    }
                )
            )

    def _connect(self, timeout_seconds: int) -> ssl.SSLSocket:
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        raw = socket.create_connection((self._host, 443), timeout=timeout_seconds)
        wrapped: ssl.SSLSocket | None = None
        try:
            wrapped = ssl.create_default_context().wrap_socket(raw, server_hostname=self._host)
            key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
            request = (
                f"GET {self._path} HTTP/1.1\r\n"
                f"Host: {self._host}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "User-Agent: bongus-region-probe-public-v1\r\n\r\n"
            ).encode("ascii")
            wrapped.sendall(request)
            response = bytearray()
            while b"\r\n\r\n" not in response:
                chunk = wrapped.recv(4_096)
                if not chunk:
                    raise RegionProbeTransportError("public WebSocket closed during handshake")
                response.extend(chunk)
                if len(response) > _MAX_HANDSHAKE_BYTES:
                    raise RegionProbeTransportError("public WebSocket handshake exceeded its byte bound")
            header_bytes, remainder = bytes(response).split(b"\r\n\r\n", 1)
            try:
                lines = header_bytes.decode("ascii").split("\r\n")
            except UnicodeDecodeError as exc:
                raise RegionProbeTransportError("public WebSocket handshake is not ASCII") from exc
            if not lines or not lines[0].startswith(("HTTP/1.1 101 ", "HTTP/1.0 101 ")):
                raise RegionProbeTransportError("public WebSocket upgrade was rejected")
            headers: dict[str, str] = {}
            for line in lines[1:]:
                if ":" not in line:
                    raise RegionProbeTransportError("malformed public WebSocket response header")
                name, value = line.split(":", 1)
                normalized = name.strip().casefold()
                if normalized in headers:
                    raise RegionProbeTransportError("duplicate public WebSocket response header")
                headers[normalized] = value.strip()
            expected_accept = base64.b64encode(
                hashlib.sha1((key + _WEBSOCKET_GUID).encode("ascii"), usedforsecurity=False).digest()
            ).decode("ascii")
            if (
                headers.get("upgrade", "").casefold() != "websocket"
                or "upgrade" not in {item.strip().casefold() for item in headers.get("connection", "").split(",")}
                or headers.get("sec-websocket-accept") != expected_accept
            ):
                raise RegionProbeTransportError("public WebSocket handshake validation failed")
            self._buffer.extend(remainder)
            return wrapped
        except Exception:
            if wrapped is not None:
                wrapped.close()
            else:
                raw.close()
            raise

    def _read_exact(self, count: int) -> bytes:
        while len(self._buffer) < count:
            try:
                chunk = self._socket.recv(max(4_096, count - len(self._buffer)))
            except TimeoutError as exc:
                raise RegionProbeReadTimeout("public WebSocket sample timed out") from exc
            if not chunk:
                raise RegionProbeTransportError("public WebSocket closed")
            self._buffer.extend(chunk)
        result = bytes(self._buffer[:count])
        del self._buffer[:count]
        return result

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if len(payload) > _MAX_FRAME_BYTES:
            raise RegionProbeTransportError("outbound public WebSocket frame exceeds its bound")
        mask = secrets.token_bytes(4)
        length = len(payload)
        header = bytearray((0x80 | opcode,))
        if length < 126:
            header.append(0x80 | length)
        elif length <= 65_535:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        header.extend(mask)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self._socket.sendall(bytes(header) + masked)

    def _send_text(self, payload: bytes) -> None:
        self._send_frame(0x1, payload)

    def _read_message(self) -> bytes:
        fragments = bytearray()
        initial_opcode: int | None = None
        while True:
            first, second = self._read_exact(2)
            final = bool(first & 0x80)
            if first & 0x70:
                raise RegionProbeTransportError("unsupported public WebSocket extension bits")
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            if masked:
                raise RegionProbeTransportError("public server sent a masked WebSocket frame")
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            if length > _MAX_FRAME_BYTES:
                raise RegionProbeTransportError("public WebSocket frame exceeds its byte bound")
            payload = self._read_exact(length)
            if opcode in {0x8, 0x9, 0xA}:
                if not final or length > 125:
                    raise RegionProbeTransportError("invalid public WebSocket control frame")
                if opcode == 0x8:
                    raise RegionProbeTransportError("public WebSocket sent a close frame")
                if opcode == 0x9:
                    self._send_frame(0xA, payload)
                continue
            if opcode == 0x1:
                if initial_opcode is not None:
                    raise RegionProbeTransportError("nested public WebSocket message")
                initial_opcode = opcode
            elif opcode != 0x0 or initial_opcode is None:
                raise RegionProbeTransportError("unsupported public WebSocket data frame")
            fragments.extend(payload)
            if len(fragments) > _MAX_FRAME_BYTES:
                raise RegionProbeTransportError("fragmented public WebSocket message exceeds its bound")
            if final:
                return bytes(fragments)

    def sample(self, *, timeout_milliseconds: int) -> WsProbeSample:
        if (
            isinstance(timeout_milliseconds, bool)
            or not isinstance(timeout_milliseconds, int)
            or timeout_milliseconds <= 0
        ):
            raise ValueError("timeout_milliseconds must be positive")
        self._socket.settimeout(timeout_milliseconds / 1_000)
        while True:
            payload = _exact_json(self._read_message())
            received = time.time_ns()
            if not isinstance(payload, Mapping):
                continue
            if self.venue is Venue.BINANCE:
                source_value = payload.get("E")
            else:
                if payload.get("channel") != "l2Book" or not isinstance(payload.get("data"), Mapping):
                    continue
                source_value = cast(Mapping[str, object], payload["data"]).get("time")
            if source_value is None:
                continue
            source = _milliseconds(source_value, "exchange event time") * 1_000_000
            flags = ("source_clock_ahead",) if source > received else ()
            return WsProbeSample(
                source_event_time_ns=source,
                receive_time_ns=received,
                connection_id=self.connection_id,
                sequence_id=str(source_value),
                quality_flags=flags,
            )

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except (OSError, RegionProbeTransportError):
            pass
        self._socket.close()


class StdlibPublicRegionProbeTransport:
    """Only fixed public BTC market-data operations; no caller URL surface."""

    def __init__(self) -> None:
        http_transport = StdlibJsonTransport(max_response_bytes=2_000_000)
        self._binance = BinancePublicFeeds(http_transport)
        self._hyperliquid = HyperliquidPublicFeeds(http_transport)
        self._connections: dict[Venue, _FixedPublicWebSocket] = {}
        self._connection_counts = {venue: 0 for venue in Venue}

    def rest_sample(self, venue: Venue) -> RestProbeSample:
        capture = time.time_ns()
        start = time.perf_counter_ns()
        try:
            if venue is Venue.BINANCE:
                self._binance.premium_index(CanonicalAsset.BTC)
            elif venue is Venue.HYPERLIQUID:
                self._hyperliquid.l2_book(CanonicalAsset.BTC)
            else:
                raise TypeError("REST probe venue must use the fixed enum")
        except Exception as exc:
            raise RegionProbeTransportError(f"public {venue.value} REST probe failed") from exc
        rtt = time.perf_counter_ns() - start
        receive = time.time_ns()
        return RestProbeSample(capture, receive, max(1, rtt), f"public-rest-{venue.value}")

    def _connection(self, venue: Venue) -> _FixedPublicWebSocket:
        connection = self._connections.get(venue)
        if connection is None:
            self._connection_counts[venue] += 1
            connection = _FixedPublicWebSocket(venue, self._connection_counts[venue])
            self._connections[venue] = connection
        return connection

    def ws_sample(self, venue: Venue, *, timeout_milliseconds: int) -> WsProbeSample:
        return self._connection(venue).sample(timeout_milliseconds=timeout_milliseconds)

    def reconnect(
        self,
        venue: Venue,
        *,
        timeout_milliseconds: int,
    ) -> tuple[int, WsProbeSample]:
        start = time.perf_counter_ns()
        previous = self._connections.pop(venue, None)
        if previous is not None:
            previous.close()
        sample = self._connection(venue).sample(timeout_milliseconds=timeout_milliseconds)
        return max(0, time.perf_counter_ns() - start), sample

    def close(self) -> None:
        for connection in tuple(self._connections.values()):
            connection.close()
        self._connections.clear()


@dataclass(frozen=True, slots=True)
class ProbeRunnerConfig:
    duration_hours: int = 60
    sample_interval_seconds: int = 1
    rest_interval_seconds: int = 5
    forced_reconnect_interval_seconds: int = 3_600
    websocket_timeout_milliseconds: int = 750

    def __post_init__(self) -> None:
        for name in (
            "duration_hours",
            "sample_interval_seconds",
            "rest_interval_seconds",
            "forced_reconnect_interval_seconds",
            "websocket_timeout_milliseconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive exact integer")
        if not 48 <= self.duration_hours <= 72:
            raise ValueError("duration_hours must be in the inclusive range [48, 72]")
        if self.rest_interval_seconds < self.sample_interval_seconds:
            raise ValueError("REST interval cannot be shorter than the WS sample interval")
        if self.forced_reconnect_interval_seconds < self.rest_interval_seconds:
            raise ValueError("forced reconnect interval cannot be shorter than REST cadence")

    @property
    def configuration_sha256(self) -> str:
        return hashlib.sha256(
            canonical_json_bytes(
                {
                    "protocol_version": PROBE_PROTOCOL_VERSION,
                    "duration_hours": self.duration_hours,
                    "sample_interval_seconds": self.sample_interval_seconds,
                    "rest_interval_seconds": self.rest_interval_seconds,
                    "forced_reconnect_interval_seconds": self.forced_reconnect_interval_seconds,
                    "websocket_timeout_milliseconds": self.websocket_timeout_milliseconds,
                    "binance_rest_operation": "premium_index:BTCUSDT",
                    "hyperliquid_rest_operation": "l2Book:BTC",
                    "binance_ws_operation": "markPrice:BTCUSDT:1s",
                    "hyperliquid_ws_operation": "l2Book:BTC",
                }
            )
        ).hexdigest()


class PublicRegionProbeRunner:
    """Scheduled recorder for one explicitly tagged probe host and region."""

    def __init__(
        self,
        *,
        log: AppendOnlyProbeLog,
        run_id: str,
        region: ProbeRegion,
        probe_host_id: str,
        transport: RegionProbeTransport,
        config: ProbeRunnerConfig = ProbeRunnerConfig(),
        clock_ns: Callable[[], int] = time.time_ns,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not run_id.strip() or not probe_host_id.strip():
            raise ValueError("run_id and probe_host_id must be non-empty")
        self.log = log
        self.run_id = run_id.strip()
        self.region = region
        self.probe_host_id = probe_host_id.strip()
        self.transport = transport
        self.config = config
        self._clock_ns = clock_ns
        self._monotonic_ns = monotonic_ns
        self._sleeper = sleeper
        self._code_sha256 = hashlib.sha256(
            Path(__file__).read_bytes() + Path(__file__).with_name("region_probe.py").read_bytes()
        ).hexdigest()
        self._previous_ws_receive_ns: dict[Venue, int] = {}
        self._next_rest_ns: dict[Venue, int] = {}
        self._next_reconnect_ns: dict[Venue, int] = {}
        self._started = False
        self._finished = False

    def _observation(
        self,
        venue: Venue,
        metric: ProbeMetric,
        capture: int,
        receive: int,
        *,
        value_ns: int | None = None,
        source_event_time_ns: int | None = None,
        expected_messages: int = 0,
        received_messages: int = 0,
        reconnect_count: int = 0,
        gaps_detected: int = 0,
        gaps_recovered: int = 0,
        connection_id: str = "none",
        sequence_id: str = "none",
        quality_flags: tuple[str, ...] = (),
    ) -> ProbeObservation:
        return ProbeObservation.create(
            run_id=self.run_id,
            region=self.region,
            probe_host_id=self.probe_host_id,
            venue=venue,
            metric=metric,
            capture_time_ns=capture,
            receive_time_ns=receive,
            available_time_ns=receive,
            code_sha256=self._code_sha256,
            configuration_sha256=self.config.configuration_sha256,
            value_ns=value_ns,
            source_event_time_ns=source_event_time_ns,
            expected_messages=expected_messages,
            received_messages=received_messages,
            reconnect_count=reconnect_count,
            gaps_detected=gaps_detected,
            gaps_recovered=gaps_recovered,
            connection_id=connection_id,
            sequence_id=sequence_id,
            quality_flags=quality_flags,
        )

    def begin(self) -> int:
        if self._started:
            raise RegionProbeTransportError("probe run has already started")
        started = self._clock_ns()
        now_mono = self._monotonic_ns()
        for venue in Venue:
            self.log.append(self._observation(venue, ProbeMetric.RUN_START, started, started))
            self._next_rest_ns[venue] = now_mono
            self._next_reconnect_ns[venue] = (
                now_mono + self.config.forced_reconnect_interval_seconds * NANOSECONDS_PER_SECOND
            )
        self._started = True
        return started

    def _record_ws_sample(self, venue: Venue, sample: WsProbeSample) -> None:
        event_age = max(0, sample.receive_time_ns - sample.source_event_time_ns)
        self.log.append(
            self._observation(
                venue,
                ProbeMetric.WS_EVENT_AGE,
                sample.receive_time_ns,
                sample.receive_time_ns,
                value_ns=event_age,
                source_event_time_ns=sample.source_event_time_ns,
                connection_id=sample.connection_id,
                sequence_id=sample.sequence_id,
                quality_flags=sample.quality_flags,
            )
        )
        previous = self._previous_ws_receive_ns.get(venue)
        if previous is not None:
            expected = self.config.sample_interval_seconds * NANOSECONDS_PER_SECOND
            jitter = abs((sample.receive_time_ns - previous) - expected)
            self.log.append(
                self._observation(
                    venue,
                    ProbeMetric.WS_JITTER,
                    sample.receive_time_ns,
                    sample.receive_time_ns,
                    value_ns=jitter,
                    connection_id=sample.connection_id,
                    sequence_id=sample.sequence_id,
                )
            )
        self._previous_ws_receive_ns[venue] = sample.receive_time_ns

    def _record_reconnect(self, venue: Venue, capture: int) -> WsProbeSample | None:
        self.log.append(
            self._observation(
                venue,
                ProbeMetric.RECONNECT,
                capture,
                capture,
                reconnect_count=1,
            )
        )
        try:
            recovery_ns, sample = self.transport.reconnect(
                venue,
                timeout_milliseconds=self.config.websocket_timeout_milliseconds,
            )
        except RegionProbeTransportError:
            receive = max(capture, self._clock_ns())
            self.log.append(
                self._observation(
                    venue,
                    ProbeMetric.GAP_RECOVERY,
                    capture,
                    receive,
                    gaps_detected=1,
                    gaps_recovered=0,
                    quality_flags=("reconnect_failure",),
                )
            )
            return None
        self.log.append(
            self._observation(
                venue,
                ProbeMetric.GAP_RECOVERY,
                capture,
                max(capture, sample.receive_time_ns),
                value_ns=recovery_ns,
                gaps_detected=1,
                gaps_recovered=1,
                connection_id=sample.connection_id,
                sequence_id=sample.sequence_id,
            )
        )
        return sample

    def sample_cycle(self, *, force_rest: bool = False, force_reconnect: bool = False) -> None:
        if not self._started or self._finished:
            raise RegionProbeTransportError("probe cycle requires an active run")
        cycle_capture = self._clock_ns()
        now_mono = self._monotonic_ns()
        for venue in Venue:
            if force_rest or now_mono >= self._next_rest_ns[venue]:
                rest = self.transport.rest_sample(venue)
                self.log.append(
                    self._observation(
                        venue,
                        ProbeMetric.REST_RTT,
                        rest.capture_time_ns,
                        rest.receive_time_ns,
                        value_ns=rest.rtt_ns,
                        connection_id=rest.connection_id,
                    )
                )
                self._next_rest_ns[venue] = now_mono + self.config.rest_interval_seconds * NANOSECONDS_PER_SECOND
            sample: WsProbeSample | None
            if force_reconnect or now_mono >= self._next_reconnect_ns[venue]:
                sample = self._record_reconnect(
                    venue,
                    max(cycle_capture, self._clock_ns()),
                )
                self._next_reconnect_ns[venue] = (
                    now_mono + self.config.forced_reconnect_interval_seconds * NANOSECONDS_PER_SECOND
                )
            else:
                try:
                    sample = self.transport.ws_sample(
                        venue,
                        timeout_milliseconds=self.config.websocket_timeout_milliseconds,
                    )
                except RegionProbeReadTimeout:
                    sample = None
                except RegionProbeTransportError:
                    sample = self._record_reconnect(
                        venue,
                        max(cycle_capture, self._clock_ns()),
                    )
            receive = max(cycle_capture, sample.receive_time_ns if sample is not None else self._clock_ns())
            self.log.append(
                self._observation(
                    venue,
                    ProbeMetric.MESSAGE_WINDOW,
                    cycle_capture,
                    receive,
                    expected_messages=1,
                    received_messages=1 if sample is not None else 0,
                )
            )
            if sample is not None:
                self._record_ws_sample(venue, sample)

    def finish(self) -> int:
        if not self._started or self._finished:
            raise RegionProbeTransportError("probe finish requires one active run")
        finished = self._clock_ns()
        for venue in Venue:
            self.log.append(self._observation(venue, ProbeMetric.RUN_END, finished, finished))
        self._finished = True
        self.transport.close()
        return finished

    def run(self) -> None:
        self.begin()
        deadline = self._monotonic_ns() + self.config.duration_hours * NANOSECONDS_PER_HOUR
        try:
            while self._monotonic_ns() < deadline:
                cycle_start = self._monotonic_ns()
                self.sample_cycle()
                remaining = self.config.sample_interval_seconds * NANOSECONDS_PER_SECOND - (
                    self._monotonic_ns() - cycle_start
                )
                if remaining > 0:
                    self._sleeper(remaining / NANOSECONDS_PER_SECOND)
        finally:
            self.finish()


__all__ = [
    "ProbeRunnerConfig",
    "PublicRegionProbeRunner",
    "RegionProbeReadTimeout",
    "RegionProbeTransport",
    "RegionProbeTransportError",
    "RestProbeSample",
    "StdlibPublicRegionProbeTransport",
    "WsProbeSample",
]
