from __future__ import annotations

from collections.abc import Mapping
from typing import cast

import pytest

from bongus.research.cross_venue.feeds import (
    ALLOWED_BINANCE_PATHS,
    ALLOWED_HYPERLIQUID_INFO_TYPES,
    BINANCE_PUBLIC_ORIGIN,
    HYPERLIQUID_INFO_URL,
    BinancePublicFeeds,
    HttpMethod,
    HyperliquidPublicFeeds,
    JsonHttpResponse,
    PublicFeedError,
    QueryValue,
    StdlibJsonTransport,
)
from bongus.research.cross_venue.schema import CanonicalAsset


class RecordingTransport:
    def __init__(
        self,
        payload: object | None = None,
        *,
        status_code: int = 200,
        response_url: str | None = None,
        response_headers: Mapping[str, str] | None = None,
    ) -> None:
        self.payload = {} if payload is None else payload
        self.status_code = status_code
        self.response_url = response_url
        self.response_headers = dict(response_headers or {})
        self.calls: list[dict[str, object]] = []

    def request(
        self,
        *,
        method: HttpMethod,
        url: str,
        query: Mapping[str, QueryValue] | None = None,
        body: Mapping[str, object] | None = None,
        timeout_seconds: int = 10,
    ) -> JsonHttpResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "query": dict(query or {}),
                "body": dict(body or {}),
                "timeout_seconds": timeout_seconds,
            }
        )
        return JsonHttpResponse(
            self.status_code,
            self.payload,
            self.response_url or url,
            self.response_headers,
        )


def test_binance_client_exposes_only_named_public_gets() -> None:
    transport = RecordingTransport([])
    client = BinancePublicFeeds(transport)
    client.exchange_info()
    client.funding_info()
    client.premium_index(CanonicalAsset.BTC)
    client.funding_history("BTC", start_time_ms=100, end_time_ms=200)
    client.book_ticker("BTC")
    client.depth("BTC", limit=20)

    assert len(transport.calls) == 6
    assert all(call["method"] == "GET" for call in transport.calls)
    assert all(call["body"] == {} for call in transport.calls)
    for call in transport.calls:
        url = str(call["url"])
        assert url.startswith(BINANCE_PUBLIC_ORIGIN)
        assert url.removeprefix(BINANCE_PUBLIC_ORIGIN) in ALLOWED_BINANCE_PATHS
    history = transport.calls[3]
    assert history["query"] == {
        "symbol": "BTCUSDT",
        "startTime": 100,
        "endTime": 200,
        "limit": 1000,
    }
    with pytest.raises(ValueError, match="precede"):
        client.funding_history("BTC", start_time_ms=200, end_time_ms=100)
    with pytest.raises(ValueError, match="depth limit"):
        client.depth("BTC", limit=100)
    with pytest.raises(ValueError, match="depth limit"):
        client.depth("BTC", limit=cast(int, 20.5))


def test_hyperliquid_client_posts_only_allowlisted_info_contracts() -> None:
    transport = RecordingTransport([])
    client = HyperliquidPublicFeeds(transport)
    client.meta_and_asset_contexts()
    client.funding_history("ETH", start_time_ms=100, end_time_ms=200)
    client.l2_book("SOL")
    client.all_mids()
    client.predicted_fundings()

    assert len(transport.calls) == 5
    assert all(call["method"] == "POST" for call in transport.calls)
    assert all(call["url"] == HYPERLIQUID_INFO_URL for call in transport.calls)
    assert all(call["query"] == {} for call in transport.calls)
    for call in transport.calls:
        body = call["body"]
        assert isinstance(body, dict)
        assert body["type"] in ALLOWED_HYPERLIQUID_INFO_TYPES
    assert transport.calls[1]["body"] == {
        "type": "fundingHistory",
        "coin": "ETH",
        "startTime": 100,
        "endTime": 200,
    }


@pytest.mark.parametrize(
    ("transport", "message"),
    [
        (RecordingTransport(status_code=302), "HTTP 302"),
        (RecordingTransport(response_headers={"location": "https://example.invalid"}), "redirect"),
        (RecordingTransport(response_url="https://example.invalid/info"), "unexpected URL"),
    ],
)
def test_public_clients_reject_status_redirect_and_url_confusion(
    transport: RecordingTransport,
    message: str,
) -> None:
    with pytest.raises(PublicFeedError, match=message):
        HyperliquidPublicFeeds(transport).all_mids()


def test_production_transport_cannot_be_repurposed_outside_public_allowlists() -> None:
    transport = StdlibJsonTransport()
    with pytest.raises(PublicFeedError, match="allowlisted Binance"):
        transport.request(method="GET", url="https://example.invalid/fapi/v1/depth")
    with pytest.raises(PublicFeedError, match="Hyperliquid public info"):
        transport.request(
            method="POST",
            url="https://api.hyperliquid.xyz/exchange",
            body={"type": "allMids"},
        )
    with pytest.raises(PublicFeedError, match="query violates"):
        transport.request(
            method="GET",
            url=f"{BINANCE_PUBLIC_ORIGIN}/fapi/v1/premiumIndex",
            query={"signature": "forbidden"},
        )
    with pytest.raises(PublicFeedError, match="fixed v1 universe"):
        transport.request(
            method="POST",
            url=HYPERLIQUID_INFO_URL,
            body={"type": "l2Book", "coin": "HYPE"},
        )
