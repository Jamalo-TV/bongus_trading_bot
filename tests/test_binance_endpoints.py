from bongus.core.binance_endpoints import (
    TESTNET_FUTURES_REST_BASE_URL,
    TESTNET_FUTURES_STREAM_WS_BASE_URL,
    TESTNET_SPOT_REST_BASE_URL,
    TESTNET_SPOT_STREAM_WS_BASE_URL,
    get_rest_base_urls,
    get_stream_ws_base_urls,
    resolve_binance_credentials,
)
from bongus.market_data.funding_ranker import FundingRanker
from bongus.market_data.rest_depth_fetcher import RestDepthFetcher


def test_testnet_components_use_demo_endpoints(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "testnet")
    monkeypatch.setenv("BINANCE_SPOT_API_KEY", "shared-key")
    monkeypatch.setenv("BINANCE_SPOT_API_SECRET", "shared-secret")
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)

    assert get_rest_base_urls() == (
        TESTNET_FUTURES_REST_BASE_URL,
        TESTNET_SPOT_REST_BASE_URL,
    )
    assert get_stream_ws_base_urls() == (
        TESTNET_FUTURES_STREAM_WS_BASE_URL,
        TESTNET_SPOT_STREAM_WS_BASE_URL,
    )

    creds = resolve_binance_credentials()
    assert creds["futures_api_key"] == "shared-key"
    assert creds["futures_api_secret"] == "shared-secret"
    assert creds["spot_api_key"] == "shared-key"
    assert creds["spot_api_secret"] == "shared-secret"

    ranker = FundingRanker(["BTCUSDT"])
    fetcher = RestDepthFetcher(["BTCUSDT"])

    assert ranker._endpoint == f"{TESTNET_FUTURES_REST_BASE_URL}/fapi/v1/premiumIndex"
    assert fetcher._futures_base_url == TESTNET_FUTURES_REST_BASE_URL
    assert fetcher._spot_base_url == TESTNET_SPOT_REST_BASE_URL
