"""Tests for FundingRanker — single REST call, filtered, sorted funding rates."""
import os
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'bongus', 'market_data')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'bongus', 'core')))

from funding_ranker import FundingRanker


_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

# Annualized funding = lastFundingRate * 3 * 365 = lastFundingRate * 1095
_MOCK_RESPONSE = [
    {"symbol": "BTCUSDT",  "lastFundingRate": "0.0001"},   # 0.0001 * 1095 = 0.1095
    {"symbol": "ETHUSDT",  "lastFundingRate": "0.0003"},   # 0.0003 * 1095 = 0.3285
    {"symbol": "SOLUSDT",  "lastFundingRate": "0.0002"},   # 0.0002 * 1095 = 0.219
    {"symbol": "XRPUSDT",  "lastFundingRate": "0.0005"},   # filtered out
]


def _mock_requests_get(response_data):
    mock_resp = MagicMock()
    mock_resp.json.return_value = response_data
    mock_resp.raise_for_status = MagicMock()
    return mock_resp


def test_initial_rates_are_zero():
    """Before refresh, all rates are 0.0."""
    ranker = FundingRanker(_SYMBOLS)
    assert ranker.get_rate("BTCUSDT") == 0.0
    assert ranker.get_rate("ETHUSDT") == 0.0


def test_unknown_symbol_returns_zero():
    """Symbols not in the ranker return 0.0, not KeyError."""
    ranker = FundingRanker(_SYMBOLS)
    assert ranker.get_rate("PEPEUSDT") == 0.0


def test_refresh_updates_rates():
    """After refresh, rates match annualized values from API."""
    ranker = FundingRanker(_SYMBOLS)
    with patch("requests.get", return_value=_mock_requests_get(_MOCK_RESPONSE)):
        import asyncio
        asyncio.run(ranker.refresh())

    assert abs(ranker.get_rate("BTCUSDT") - 0.1095) < 1e-9
    assert abs(ranker.get_rate("ETHUSDT") - 0.3285) < 1e-9
    assert abs(ranker.get_rate("SOLUSDT") - 0.2190) < 1e-9


def test_refresh_filters_unmonitored_symbols():
    """Symbols not in MONITORED_SYMBOLS are ignored even if in API response."""
    ranker = FundingRanker(_SYMBOLS)
    with patch("requests.get", return_value=_mock_requests_get(_MOCK_RESPONSE)):
        import asyncio
        asyncio.run(ranker.refresh())

    assert ranker.get_rate("XRPUSDT") == 0.0  # not in _SYMBOLS, not tracked


def test_get_ranked_returns_sorted_highest_first():
    """get_ranked returns symbols sorted by annualized rate, highest first."""
    ranker = FundingRanker(_SYMBOLS)
    with patch("requests.get", return_value=_mock_requests_get(_MOCK_RESPONSE)):
        import asyncio
        asyncio.run(ranker.refresh())

    ranked = ranker.get_ranked()
    rates = [r for _, r in ranked]
    assert rates == sorted(rates, reverse=True)
    assert ranked[0][0] == "ETHUSDT"   # highest: 0.3285
    assert ranked[-1][0] == "BTCUSDT"  # lowest: 0.1095


def test_get_ranked_returns_all_monitored_symbols():
    """get_ranked includes every symbol in MONITORED_SYMBOLS, even after refresh."""
    ranker = FundingRanker(_SYMBOLS)
    with patch("requests.get", return_value=_mock_requests_get(_MOCK_RESPONSE)):
        import asyncio
        asyncio.run(ranker.refresh())
    ranked = ranker.get_ranked()
    assert set(s for s, _ in ranked) == set(_SYMBOLS)


def test_refresh_makes_single_http_request():
    """Only one HTTP GET is made regardless of how many symbols are monitored."""
    ranker = FundingRanker(_SYMBOLS)
    with patch("requests.get", return_value=_mock_requests_get(_MOCK_RESPONSE)) as mock_get:
        import asyncio
        asyncio.run(ranker.refresh())
    assert mock_get.call_count == 1
