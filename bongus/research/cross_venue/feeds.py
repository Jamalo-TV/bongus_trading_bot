"""Narrow public-data clients for Binance USD-M and Hyperliquid info data.

The clients expose named operations rather than a generic URL/method surface.
Their production transport does not follow redirects, decodes JSON numbers as
Decimal, and sends no caller-provided headers.
"""

from __future__ import annotations

import http.client
import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal, Protocol
from urllib.parse import urlencode, urlsplit

from bongus.research.cross_venue.normalization import mapping_for_asset
from bongus.research.cross_venue.schema import CanonicalAsset

BINANCE_PUBLIC_ORIGIN: Final[str] = "https://fapi.binance.com"
HYPERLIQUID_INFO_URL: Final[str] = "https://api.hyperliquid.xyz/info"
ALLOWED_BINANCE_PATHS: Final[frozenset[str]] = frozenset(
    {
        "/fapi/v1/depth",
        "/fapi/v1/exchangeInfo",
        "/fapi/v1/fundingInfo",
        "/fapi/v1/fundingRate",
        "/fapi/v1/premiumIndex",
        "/fapi/v1/ticker/bookTicker",
    }
)
ALLOWED_HYPERLIQUID_INFO_TYPES: Final[frozenset[str]] = frozenset(
    {
        "allMids",
        "fundingHistory",
        "l2Book",
        "metaAndAssetCtxs",
        "predictedFundings",
    }
)
_APPROVED_BINANCE_SYMBOLS: Final[frozenset[str]] = frozenset({"BTCUSDT", "DOGEUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"})
_APPROVED_HYPERLIQUID_COINS: Final[frozenset[str]] = frozenset({"BTC", "DOGE", "ETH", "SOL", "XRP"})
_BINANCE_QUERY_KEYS: Final[Mapping[str, frozenset[str]]] = {
    "/fapi/v1/depth": frozenset({"symbol", "limit"}),
    "/fapi/v1/exchangeInfo": frozenset(),
    "/fapi/v1/fundingInfo": frozenset(),
    "/fapi/v1/fundingRate": frozenset({"symbol", "startTime", "endTime", "limit"}),
    "/fapi/v1/premiumIndex": frozenset({"symbol"}),
    "/fapi/v1/ticker/bookTicker": frozenset({"symbol"}),
}
_HYPERLIQUID_BODY_KEYS: Final[Mapping[str, frozenset[str]]] = {
    "allMids": frozenset({"type"}),
    "fundingHistory": frozenset({"type", "coin", "startTime", "endTime"}),
    "l2Book": frozenset({"type", "coin"}),
    "metaAndAssetCtxs": frozenset({"type"}),
    "predictedFundings": frozenset({"type"}),
}

HttpMethod = Literal["GET", "POST"]
QueryValue = str | int


class PublicFeedError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class JsonHttpResponse:
    status_code: int
    payload: object
    url: str
    headers: Mapping[str, str]
    raw_body: bytes | None = None


class JsonTransport(Protocol):
    def request(
        self,
        *,
        method: HttpMethod,
        url: str,
        query: Mapping[str, QueryValue] | None = None,
        body: Mapping[str, object] | None = None,
        timeout_seconds: int = 10,
    ) -> JsonHttpResponse: ...


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


class StdlibJsonTransport:
    """Small synchronous HTTPS transport with bounded, exact JSON decoding."""

    def __init__(self, *, max_response_bytes: int = 16_000_000) -> None:
        if isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int) or max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        self._max_response_bytes = max_response_bytes

    def request(
        self,
        *,
        method: HttpMethod,
        url: str,
        query: Mapping[str, QueryValue] | None = None,
        body: Mapping[str, object] | None = None,
        timeout_seconds: int = 10,
    ) -> JsonHttpResponse:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed.query
            or parsed.port not in (None, 443)
        ):
            raise PublicFeedError("public feed URL must be credential-free HTTPS on the standard port")
        if method == "GET":
            if (
                parsed.hostname != "fapi.binance.com"
                or parsed.path not in ALLOWED_BINANCE_PATHS
                or url != f"{BINANCE_PUBLIC_ORIGIN}{parsed.path}"
            ):
                raise PublicFeedError("GET transport is restricted to allowlisted Binance public paths")
            if body is not None:
                raise PublicFeedError("public GET requests cannot contain a body")
            request_query = query or {}
            if not set(request_query).issubset(_BINANCE_QUERY_KEYS[parsed.path]):
                raise PublicFeedError("Binance query violates the fixed public contract")
            for key, value in request_query.items():
                if isinstance(value, bool) or not isinstance(value, (str, int)):
                    raise PublicFeedError(f"Binance query {key} must be an exact string or integer")
            symbol = request_query.get("symbol")
            if symbol is not None and symbol not in _APPROVED_BINANCE_SYMBOLS:
                raise PublicFeedError("Binance symbol is outside the fixed v1 universe")
            for key in ("startTime", "endTime", "limit"):
                if key in request_query:
                    _exact_nonnegative_integer(request_query[key], key)
        elif method == "POST":
            if url != HYPERLIQUID_INFO_URL:
                raise PublicFeedError("POST transport is restricted to Hyperliquid public info")
            if query:
                raise PublicFeedError("public info requests cannot contain query parameters")
            if body is None:
                raise PublicFeedError("Hyperliquid public info requires an allowlisted body")
            info_type = body.get("type")
            if (
                not isinstance(info_type, str)
                or info_type not in ALLOWED_HYPERLIQUID_INFO_TYPES
                or not set(body).issubset(_HYPERLIQUID_BODY_KEYS[info_type])
            ):
                raise PublicFeedError("Hyperliquid info body violates its fixed contract")
            coin = body.get("coin")
            if coin is not None and (not isinstance(coin, str) or coin not in _APPROVED_HYPERLIQUID_COINS):
                raise PublicFeedError("Hyperliquid coin is outside the fixed v1 universe")
            for key in ("startTime", "endTime"):
                if key in body:
                    value = body[key]
                    if not isinstance(value, (str, int)) or isinstance(value, bool):
                        raise PublicFeedError(f"Hyperliquid {key} must be an exact integer")
                    _exact_nonnegative_integer(value, key)
        else:
            raise PublicFeedError("public transport permits only GET and POST")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        target = parsed.path or "/"
        if query:
            target = f"{target}?{urlencode(sorted(query.items()))}"
        encoded_body: bytes | None = None
        headers = {"Accept": "application/json", "User-Agent": "bongus-research-public-v1"}
        if body is not None:
            encoded_body = json.dumps(
                dict(body),
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPSConnection(
            parsed.hostname,
            parsed.port or 443,
            timeout=timeout_seconds,
        )
        try:
            connection.request(method, target, body=encoded_body, headers=headers)
            response = connection.getresponse()
            raw = response.read(self._max_response_bytes + 1)
            response_headers = {name.lower(): value for name, value in response.getheaders()}
            status = int(response.status)
        finally:
            connection.close()
        if len(raw) > self._max_response_bytes:
            raise PublicFeedError("public response exceeds the configured byte bound")
        try:
            payload = json.loads(
                raw.decode("utf-8"),
                parse_float=Decimal,
                parse_int=int,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise PublicFeedError("public endpoint returned invalid exact JSON") from exc
        return JsonHttpResponse(status, payload, url, response_headers, raw_body=raw)


def _checked_payload(response: JsonHttpResponse, expected_url: str) -> object:
    if response.url != expected_url:
        raise PublicFeedError("public transport returned an unexpected URL")
    if not 200 <= response.status_code < 300:
        raise PublicFeedError(f"public endpoint returned HTTP {response.status_code}")
    if any(name.lower() == "location" for name in response.headers):
        raise PublicFeedError("redirect responses are forbidden")
    return response.payload


def _exact_nonnegative_integer(value: int | str, field_name: str) -> int:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{field_name} must be an exact integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an exact integer") from exc
    if result < 0 or (not isinstance(value, int) and str(value).strip() != str(result)):
        raise ValueError(f"{field_name} must be a non-negative integer")
    return result


class BinancePublicFeeds:
    """Named, public GET operations for the frozen Binance research surface."""

    def __init__(self, transport: JsonTransport | None = None) -> None:
        self._transport = transport or StdlibJsonTransport()

    def _get(self, path: str, query: Mapping[str, QueryValue] | None = None) -> object:
        if path not in ALLOWED_BINANCE_PATHS:
            raise PublicFeedError("Binance path is outside the public allowlist")
        url = f"{BINANCE_PUBLIC_ORIGIN}{path}"
        response = self._transport.request(method="GET", url=url, query=query)
        return _checked_payload(response, url)

    def exchange_info(self) -> object:
        return self._get("/fapi/v1/exchangeInfo")

    def funding_info(self) -> object:
        return self._get("/fapi/v1/fundingInfo")

    def premium_index(self, asset: CanonicalAsset | str | None = None) -> object:
        query = None
        if asset is not None:
            query = {"symbol": mapping_for_asset(asset).binance_symbol}
        return self._get("/fapi/v1/premiumIndex", query)

    def funding_history(
        self,
        asset: CanonicalAsset | str,
        *,
        start_time_ms: int | str,
        end_time_ms: int | str,
        limit: int = 1_000,
    ) -> object:
        start = _exact_nonnegative_integer(start_time_ms, "start_time_ms")
        end = _exact_nonnegative_integer(end_time_ms, "end_time_ms")
        if end < start:
            raise ValueError("end_time_ms must not precede start_time_ms")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1_000:
            raise ValueError("limit must be in [1, 1000]")
        mapping = mapping_for_asset(asset)
        return self._get(
            "/fapi/v1/fundingRate",
            {
                "symbol": mapping.binance_symbol,
                "startTime": start,
                "endTime": end,
                "limit": limit,
            },
        )

    def book_ticker(self, asset: CanonicalAsset | str) -> object:
        return self._get(
            "/fapi/v1/ticker/bookTicker",
            {"symbol": mapping_for_asset(asset).binance_symbol},
        )

    def depth(self, asset: CanonicalAsset | str, *, limit: int = 20) -> object:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit not in {5, 10, 20}:
            raise ValueError("v1 depth limit must be 5, 10, or 20")
        return self._get(
            "/fapi/v1/depth",
            {"symbol": mapping_for_asset(asset).binance_symbol, "limit": limit},
        )


class HyperliquidPublicFeeds:
    """Named operations for explicitly approved Hyperliquid info request types."""

    def __init__(self, transport: JsonTransport | None = None) -> None:
        self._transport = transport or StdlibJsonTransport()

    def _info(self, info_type: str, body: Mapping[str, object]) -> object:
        if info_type not in ALLOWED_HYPERLIQUID_INFO_TYPES:
            raise PublicFeedError("Hyperliquid info type is outside the allowlist")
        if body.get("type") != info_type or not set(body).issubset(_HYPERLIQUID_BODY_KEYS[info_type]):
            raise PublicFeedError("Hyperliquid info body violates its fixed contract")
        response = self._transport.request(
            method="POST",
            url=HYPERLIQUID_INFO_URL,
            body=body,
        )
        return _checked_payload(response, HYPERLIQUID_INFO_URL)

    def meta_and_asset_contexts(self) -> object:
        return self._info("metaAndAssetCtxs", {"type": "metaAndAssetCtxs"})

    def funding_history(
        self,
        asset: CanonicalAsset | str,
        *,
        start_time_ms: int | str,
        end_time_ms: int | str | None = None,
    ) -> object:
        start = _exact_nonnegative_integer(start_time_ms, "start_time_ms")
        body: dict[str, object] = {
            "type": "fundingHistory",
            "coin": mapping_for_asset(asset).hyperliquid_coin,
            "startTime": start,
        }
        if end_time_ms is not None:
            end = _exact_nonnegative_integer(end_time_ms, "end_time_ms")
            if end < start:
                raise ValueError("end_time_ms must not precede start_time_ms")
            body["endTime"] = end
        return self._info("fundingHistory", body)

    def l2_book(self, asset: CanonicalAsset | str) -> object:
        return self._info(
            "l2Book",
            {"type": "l2Book", "coin": mapping_for_asset(asset).hyperliquid_coin},
        )

    def all_mids(self) -> object:
        return self._info("allMids", {"type": "allMids"})

    def predicted_fundings(self) -> object:
        return self._info("predictedFundings", {"type": "predictedFundings"})


__all__ = [
    "ALLOWED_BINANCE_PATHS",
    "ALLOWED_HYPERLIQUID_INFO_TYPES",
    "BINANCE_PUBLIC_ORIGIN",
    "BinancePublicFeeds",
    "HYPERLIQUID_INFO_URL",
    "HyperliquidPublicFeeds",
    "JsonHttpResponse",
    "JsonTransport",
    "PublicFeedError",
    "StdlibJsonTransport",
]
