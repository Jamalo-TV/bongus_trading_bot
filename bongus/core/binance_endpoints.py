"""Shared Binance endpoint and credential helpers.

Paper mode intentionally uses mainnet market data while suppressing real orders.
Testnet mode selects the official USD-M demo and Spot Testnet endpoints.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ENDPOINT_MATRIX_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "binance_endpoints_v1.json"
)


def _load_endpoint_matrix() -> dict[str, Any]:
    payload = json.loads(ENDPOINT_MATRIX_PATH.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise RuntimeError("unsupported Binance endpoint-matrix schema")
    max_age = payload.get("planned_connection_max_age_seconds")
    if not isinstance(max_age, int) or not 0 < max_age < 24 * 60 * 60:
        raise RuntimeError("Binance connection renewal must be planned before 24 hours")
    environments = payload.get("environments")
    if not isinstance(environments, dict) or set(environments) != {"mainnet", "testnet"}:
        raise RuntimeError("Binance endpoint matrix must define mainnet and testnet")
    for environment in ("mainnet", "testnet"):
        venues = environments.get(environment)
        if not isinstance(venues, dict) or set(venues) != {"futures", "spot"}:
            raise RuntimeError(f"invalid Binance venue matrix for {environment}")
        for venue in ("futures", "spot"):
            endpoints = venues.get(venue)
            required = {
                "rest_base_url",
                "public_stream_ws_base_url",
                "market_stream_ws_base_url",
                "private_ws_base_url",
            }
            if not isinstance(endpoints, dict) or set(endpoints) != required:
                raise RuntimeError(f"invalid Binance endpoint set for {environment}/{venue}")
            if not str(endpoints["rest_base_url"]).startswith("https://"):
                raise RuntimeError("Binance REST endpoint must use HTTPS")
            for field in (
                "public_stream_ws_base_url",
                "market_stream_ws_base_url",
                "private_ws_base_url",
            ):
                if not str(endpoints[field]).startswith("wss://"):
                    raise RuntimeError("Binance stream endpoint must use WSS")
    return payload


_ENDPOINT_MATRIX = _load_endpoint_matrix()
PLANNED_CONNECTION_MAX_AGE_SECONDS = int(
    _ENDPOINT_MATRIX["planned_connection_max_age_seconds"]
)


def _endpoint(environment: str, venue: str, field: str) -> str:
    return str(_ENDPOINT_MATRIX["environments"][environment][venue][field])


MAINNET_FUTURES_REST_BASE_URL = _endpoint(
    "mainnet", "futures", "rest_base_url"
)
TESTNET_FUTURES_REST_BASE_URL = _endpoint(
    "testnet", "futures", "rest_base_url"
)
MAINNET_SPOT_REST_BASE_URL = _endpoint("mainnet", "spot", "rest_base_url")
TESTNET_SPOT_REST_BASE_URL = _endpoint("testnet", "spot", "rest_base_url")

MAINNET_FUTURES_STREAM_WS_BASE_URL = _endpoint(
    "mainnet", "futures", "market_stream_ws_base_url"
)
TESTNET_FUTURES_STREAM_WS_BASE_URL = _endpoint(
    "testnet", "futures", "market_stream_ws_base_url"
)
MAINNET_SPOT_STREAM_WS_BASE_URL = _endpoint(
    "mainnet", "spot", "market_stream_ws_base_url"
)
TESTNET_SPOT_STREAM_WS_BASE_URL = _endpoint(
    "testnet", "spot", "market_stream_ws_base_url"
)
MAINNET_FUTURES_PUBLIC_STREAM_WS_BASE_URL = _endpoint(
    "mainnet", "futures", "public_stream_ws_base_url"
)
TESTNET_FUTURES_PUBLIC_STREAM_WS_BASE_URL = _endpoint(
    "testnet", "futures", "public_stream_ws_base_url"
)
MAINNET_SPOT_PUBLIC_STREAM_WS_BASE_URL = _endpoint(
    "mainnet", "spot", "public_stream_ws_base_url"
)
TESTNET_SPOT_PUBLIC_STREAM_WS_BASE_URL = _endpoint(
    "testnet", "spot", "public_stream_ws_base_url"
)
MAINNET_FUTURES_PRIVATE_WS_BASE_URL = _endpoint(
    "mainnet", "futures", "private_ws_base_url"
)
TESTNET_FUTURES_PRIVATE_WS_BASE_URL = _endpoint(
    "testnet", "futures", "private_ws_base_url"
)
MAINNET_SPOT_PRIVATE_WS_BASE_URL = _endpoint(
    "mainnet", "spot", "private_ws_base_url"
)
TESTNET_SPOT_PRIVATE_WS_BASE_URL = _endpoint(
    "testnet", "spot", "private_ws_base_url"
)


def normalize_trading_mode(trading_mode: str | None = None) -> str:
    normalized = (trading_mode or os.getenv("TRADING_MODE", "paper")).strip().lower()
    return normalized if normalized in {"paper", "testnet", "live"} else "paper"


def uses_testnet(trading_mode: str | None = None) -> bool:
    return normalize_trading_mode(trading_mode) == "testnet"


def get_rest_base_urls(trading_mode: str | None = None) -> tuple[str, str]:
    if uses_testnet(trading_mode):
        return TESTNET_FUTURES_REST_BASE_URL, TESTNET_SPOT_REST_BASE_URL
    return MAINNET_FUTURES_REST_BASE_URL, MAINNET_SPOT_REST_BASE_URL


def get_stream_ws_base_urls(trading_mode: str | None = None) -> tuple[str, str]:
    if uses_testnet(trading_mode):
        return TESTNET_FUTURES_STREAM_WS_BASE_URL, TESTNET_SPOT_STREAM_WS_BASE_URL
    return MAINNET_FUTURES_STREAM_WS_BASE_URL, MAINNET_SPOT_STREAM_WS_BASE_URL


def get_public_ws_base_urls(trading_mode: str | None = None) -> tuple[str, str]:
    if uses_testnet(trading_mode):
        return (
            TESTNET_FUTURES_PUBLIC_STREAM_WS_BASE_URL,
            TESTNET_SPOT_PUBLIC_STREAM_WS_BASE_URL,
        )
    return (
        MAINNET_FUTURES_PUBLIC_STREAM_WS_BASE_URL,
        MAINNET_SPOT_PUBLIC_STREAM_WS_BASE_URL,
    )


def get_private_ws_base_urls(trading_mode: str | None = None) -> tuple[str, str]:
    if uses_testnet(trading_mode):
        return TESTNET_FUTURES_PRIVATE_WS_BASE_URL, TESTNET_SPOT_PRIVATE_WS_BASE_URL
    return MAINNET_FUTURES_PRIVATE_WS_BASE_URL, MAINNET_SPOT_PRIVATE_WS_BASE_URL


def resolve_binance_credentials() -> dict[str, str]:
    futures_api_key = os.getenv("BINANCE_API_KEY", "").strip()
    futures_api_secret = os.getenv("BINANCE_API_SECRET", "").strip()
    spot_api_key = os.getenv("BINANCE_SPOT_API_KEY", "").strip()
    spot_api_secret = os.getenv("BINANCE_SPOT_API_SECRET", "").strip()

    shared_api_key = futures_api_key or spot_api_key
    shared_api_secret = futures_api_secret or spot_api_secret

    return {
        "futures_api_key": shared_api_key,
        "futures_api_secret": shared_api_secret,
        "spot_api_key": spot_api_key or shared_api_key,
        "spot_api_secret": spot_api_secret or shared_api_secret,
    }
