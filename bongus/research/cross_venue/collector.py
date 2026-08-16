"""Public-only snapshot collector writing exclusively to research.db."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from bongus.research.cross_venue.feeds import (
    BINANCE_PUBLIC_ORIGIN,
    HYPERLIQUID_INFO_URL,
    BinancePublicFeeds,
    HttpMethod,
    HyperliquidPublicFeeds,
    JsonHttpResponse,
    JsonTransport,
    QueryValue,
    StdlibJsonTransport,
)
from bongus.research.cross_venue.normalization import mapping_for_asset
from bongus.research.cross_venue.schema import (
    CanonicalAsset,
    Venue,
    deterministic_event_id,
)
from bongus.research.cross_venue.storage import (
    AggregateAsset,
    RawSnapshotRecord,
    ResearchStore,
    canonical_json_bytes,
)


class _CapturingTransport:
    def __init__(self, wrapped: JsonTransport) -> None:
        self._wrapped = wrapped
        self.last_response: JsonHttpResponse | None = None

    def request(
        self,
        *,
        method: HttpMethod,
        url: str,
        query: Mapping[str, QueryValue] | None = None,
        body: Mapping[str, object] | None = None,
        timeout_seconds: int = 10,
    ) -> JsonHttpResponse:
        response = self._wrapped.request(
            method=method,
            url=url,
            query=query,
            body=body,
            timeout_seconds=timeout_seconds,
        )
        self.last_response = response
        return response

    def take_response(self) -> JsonHttpResponse:
        response = self.last_response
        self.last_response = None
        if response is None:
            raise RuntimeError("public client completed without a captured HTTP response")
        return response


class RawSnapshotPublisher(Protocol):
    """Narrow sink used to keep the collector independent of Parquet imports."""

    def publish_raw_snapshot(self, record: RawSnapshotRecord) -> Sequence[object]: ...


@dataclass(frozen=True, slots=True)
class CollectionTarget:
    dataset: str
    venue: Venue
    canonical_asset: CanonicalAsset | AggregateAsset
    venue_symbol: str
    contract_id: str
    endpoint: str
    request_method: HttpMethod
    fetch: Callable[[], object]


@dataclass(frozen=True, slots=True)
class CollectionResult:
    inserted_snapshots: int
    exact_duplicates: int
    failed_snapshots: int
    event_ids: tuple[str, ...]
    published_artifacts: int = 0
    records: tuple[RawSnapshotRecord, ...] = ()


def _extract_source_time_ns(payload: object, fallback_ns: int) -> tuple[int, bool]:
    candidates: list[int] = []

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key in {"time", "E", "T"} and isinstance(item, (int, str)):
                    try:
                        raw = int(item)
                    except ValueError:
                        continue
                    if raw >= 0 and str(item).strip() == str(raw):
                        candidates.append(raw * 1_000_000 if raw < 10**16 else raw)
                elif isinstance(item, (Mapping, list, tuple)):
                    visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)

    visit(payload)
    return (max(candidates), False) if candidates else (fallback_ns, True)


class PublicResearchCollector:
    """Explicit public operations; construction itself performs no network I/O."""

    def __init__(
        self,
        store: ResearchStore,
        *,
        transport: JsonTransport | None = None,
        clock_ns: Callable[[], int] = time.time_ns,
        artifact_publisher: RawSnapshotPublisher | None = None,
    ) -> None:
        self.store = store
        self._clock_ns = clock_ns
        self._transport = _CapturingTransport(transport or StdlibJsonTransport())
        self._binance = BinancePublicFeeds(self._transport)
        self._hyperliquid = HyperliquidPublicFeeds(self._transport)
        self._code_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        self._artifact_publisher = artifact_publisher

    def _binance_funding_history(self, asset: CanonicalAsset) -> object:
        end_time_ms = self._clock_ns() // 1_000_000
        start_time_ms = max(0, end_time_ms - 48 * 60 * 60 * 1_000)
        return self._binance.funding_history(
            asset,
            start_time_ms=start_time_ms,
            end_time_ms=end_time_ms,
        )

    def _hyperliquid_funding_history(self, asset: CanonicalAsset) -> object:
        end_time_ms = self._clock_ns() // 1_000_000
        start_time_ms = max(0, end_time_ms - 48 * 60 * 60 * 1_000)
        return self._hyperliquid.funding_history(
            asset,
            start_time_ms=start_time_ms,
            end_time_ms=end_time_ms,
        )

    def targets(
        self,
        *,
        include_books: bool = True,
        include_funding_history: bool = True,
    ) -> tuple[CollectionTarget, ...]:
        aggregate: list[CollectionTarget] = [
            CollectionTarget(
                "contract_metadata",
                Venue.BINANCE,
                "UNIVERSE",
                "ALL",
                "binance-usdt-linear-perpetuals",
                "/fapi/v1/exchangeInfo",
                "GET",
                self._binance.exchange_info,
            ),
            CollectionTarget(
                "funding_intervals",
                Venue.BINANCE,
                "UNIVERSE",
                "ALL",
                "binance-usdt-linear-perpetuals",
                "/fapi/v1/fundingInfo",
                "GET",
                self._binance.funding_info,
            ),
            CollectionTarget(
                "funding_quotes",
                Venue.BINANCE,
                "UNIVERSE",
                "ALL",
                "binance-usdt-linear-perpetuals",
                "/fapi/v1/premiumIndex",
                "GET",
                self._binance.premium_index,
            ),
            CollectionTarget(
                "reference_context",
                Venue.HYPERLIQUID,
                "UNIVERSE",
                "ALL",
                "hyperliquid-core-linear-perpetuals",
                "/info",
                "POST",
                self._hyperliquid.meta_and_asset_contexts,
            ),
            CollectionTarget(
                "funding_quotes",
                Venue.HYPERLIQUID,
                "UNIVERSE",
                "ALL",
                "hyperliquid-core-linear-perpetuals",
                "/info",
                "POST",
                self._hyperliquid.predicted_fundings,
            ),
            CollectionTarget(
                "mark_index_oracle_prices",
                Venue.HYPERLIQUID,
                "UNIVERSE",
                "ALL",
                "hyperliquid-core-linear-perpetuals",
                "/info",
                "POST",
                self._hyperliquid.all_mids,
            ),
        ]
        histories: list[CollectionTarget] = []
        if include_funding_history:
            for asset in CanonicalAsset:
                mapping = mapping_for_asset(asset)
                histories.extend(
                    (
                        CollectionTarget(
                            "final_funding_settlements",
                            Venue.BINANCE,
                            asset,
                            mapping.binance_symbol,
                            mapping.binance_contract_id,
                            "/fapi/v1/fundingRate",
                            "GET",
                            lambda asset=asset: self._binance_funding_history(asset),
                        ),
                        CollectionTarget(
                            "final_funding_settlements",
                            Venue.HYPERLIQUID,
                            asset,
                            mapping.hyperliquid_coin,
                            mapping.hyperliquid_contract_id,
                            "/info",
                            "POST",
                            lambda asset=asset: self._hyperliquid_funding_history(asset),
                        ),
                    )
                )
        if not include_books:
            return tuple(aggregate + histories)
        books: list[CollectionTarget] = []
        for asset in CanonicalAsset:
            mapping = mapping_for_asset(asset)
            books.extend(
                (
                    CollectionTarget(
                        "bbo",
                        Venue.BINANCE,
                        asset,
                        mapping.binance_symbol,
                        mapping.binance_contract_id,
                        "/fapi/v1/ticker/bookTicker",
                        "GET",
                        lambda asset=asset: self._binance.book_ticker(asset),
                    ),
                    CollectionTarget(
                        "top20_book",
                        Venue.BINANCE,
                        asset,
                        mapping.binance_symbol,
                        mapping.binance_contract_id,
                        "/fapi/v1/depth",
                        "GET",
                        lambda asset=asset: self._binance.depth(asset, limit=20),
                    ),
                    CollectionTarget(
                        "top20_book",
                        Venue.HYPERLIQUID,
                        asset,
                        mapping.hyperliquid_coin,
                        mapping.hyperliquid_contract_id,
                        "/info",
                        "POST",
                        lambda asset=asset: self._hyperliquid.l2_book(asset),
                    ),
                )
            )
        return tuple(aggregate + histories + books)

    def collect_targets(self, targets: Sequence[CollectionTarget]) -> CollectionResult:
        inserted = 0
        duplicates = 0
        failed = 0
        published_artifacts = 0
        event_ids: list[str] = []
        records: list[RawSnapshotRecord] = []
        targets = tuple(targets)
        configuration_sha256 = hashlib.sha256(
            canonical_json_bytes(
                tuple(
                    {
                        "dataset": target.dataset,
                        "venue": target.venue,
                        "canonical_asset": target.canonical_asset,
                        "venue_symbol": target.venue_symbol,
                        "contract_id": target.contract_id,
                        "endpoint": target.endpoint,
                        "request_method": target.request_method,
                    }
                    for target in targets
                )
            )
        ).hexdigest()
        for target in targets:
            capture = self._clock_ns()
            self._transport.last_response = None
            failure_flag: str | None = None
            try:
                payload = target.fetch()
                response = self._transport.take_response()
            except Exception as exc:
                failed += 1
                captured = self._transport.last_response
                self._transport.last_response = None
                if captured is None:
                    failure_flag = "transport_failure"
                    payload = {"error_type": type(exc).__name__}
                    response = JsonHttpResponse(
                        status_code=599,
                        payload=payload,
                        url=(
                            f"{BINANCE_PUBLIC_ORIGIN}{target.endpoint}"
                            if target.venue is Venue.BINANCE
                            else HYPERLIQUID_INFO_URL
                        ),
                        headers={},
                    )
                else:
                    failure_flag = "public_response_rejected"
                    response = captured
                    payload = captured.payload
            receive = self._clock_ns()
            available = receive
            persistence = max(self._clock_ns(), available)
            source, inferred = _extract_source_time_ns(payload, capture)
            payload_bytes = response.raw_body
            if payload_bytes is None:
                payload_bytes = canonical_json_bytes(response.payload)
            content_hash = hashlib.sha256(payload_bytes).hexdigest()
            event_id = deterministic_event_id(
                target.venue.value,
                target.dataset,
                target.venue_symbol,
                str(capture),
                content_hash,
            )
            record = RawSnapshotRecord(
                event_id=event_id,
                dataset=target.dataset,
                venue=target.venue,
                canonical_asset=target.canonical_asset,
                venue_symbol=target.venue_symbol,
                contract_id=target.contract_id,
                endpoint=target.endpoint,
                request_method=target.request_method,
                source_time_ns=source,
                capture_time_ns=capture,
                receive_time_ns=receive,
                available_time_ns=available,
                persistence_time_ns=persistence,
                http_status=response.status_code,
                response_headers=response.headers,
                payload_bytes=payload_bytes,
                code_sha256=self._code_sha256,
                configuration_sha256=configuration_sha256,
                connection_id=(BINANCE_PUBLIC_ORIGIN if target.venue is Venue.BINANCE else HYPERLIQUID_INFO_URL),
                quality_flags=tuple(
                    flag
                    for flag in (
                        "source_time_inferred" if inferred else None,
                        failure_flag,
                    )
                    if flag is not None
                ),
            )
            if self.store.append_raw_snapshot(record):
                inserted += 1
            else:
                duplicates += 1
            if self._artifact_publisher is not None:
                published_artifacts += len(self._artifact_publisher.publish_raw_snapshot(record))
            event_ids.append(event_id)
            records.append(record)
        return CollectionResult(
            inserted,
            duplicates,
            failed,
            tuple(event_ids),
            published_artifacts,
            tuple(records),
        )

    def collect_once(
        self,
        *,
        include_books: bool = True,
        include_funding_history: bool = True,
    ) -> CollectionResult:
        return self.collect_targets(
            self.targets(
                include_books=include_books,
                include_funding_history=include_funding_history,
            )
        )


__all__ = [
    "CollectionResult",
    "CollectionTarget",
    "PublicResearchCollector",
    "RawSnapshotPublisher",
]
