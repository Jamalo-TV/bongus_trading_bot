"""Shared Binance endpoint and credential helpers.

Paper mode intentionally uses mainnet market data while suppressing real orders.
Testnet mode uses Binance's demo-mode endpoints for both futures and spot.
"""

from __future__ import annotations

import os

MAINNET_FUTURES_REST_BASE_URL = "https://fapi.binance.com"
TESTNET_FUTURES_REST_BASE_URL = "https://demo-fapi.binance.com"
MAINNET_SPOT_REST_BASE_URL = "https://api.binance.com"
TESTNET_SPOT_REST_BASE_URL = "https://demo-api.binance.com"

MAINNET_FUTURES_STREAM_WS_BASE_URL = "wss://fstream.binance.com"
TESTNET_FUTURES_STREAM_WS_BASE_URL = "wss://fstream.binancefuture.com"
MAINNET_SPOT_STREAM_WS_BASE_URL = "wss://stream.binance.com:9443"
TESTNET_SPOT_STREAM_WS_BASE_URL = "wss://demo-stream.binance.com/ws"


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
