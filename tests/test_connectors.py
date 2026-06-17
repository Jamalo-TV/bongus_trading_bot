import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from dataclasses import dataclass
from typing import List, Optional
from models.market_data import OrderBook, PriceLevel
from connectors.base_connector import ExchangeConnector
from utils.rate_limiter import TokenBucketRateLimiter

@pytest.mark.asyncio
async def test_order_book_normalization():
    # Mock connector that implements normalization
    class MockConnector(ExchangeConnector):
        async def get_order_book(self, symbol: str) -> OrderBook:
            raw_data = {
                "bids": [["100.0", "1.0"], ["99.0", "2.0"]],
                "asks": [["101.0", "1.5"], ["102.0", "3.0"]],
                "timestamp": 123456789
            }
            return self._normalize_order_book(raw_data)

        def _normalize_order_book(self, data: dict) -> OrderBook:
            return OrderBook(
                symbol="BTCUSDT",
                bids=[PriceLevel(price=float(p), amount=float(a)) for p, a in data["bids"]],
                asks=[PriceLevel(price=float(p), amount=float(a)) for p, a in data["asks"]],
                timestamp=data["timestamp"]
            )

    connector = MockConnector()
    ob = await connector.get_order_book("BTCUSDT")
    
    assert ob.symbol == "BTCUSDT"
    assert len(ob.bids) == 2
    assert ob.bids[0].price == 100.0
    assert ob.bids[0].amount == 1.0
    assert ob.asks[0].price == 101.0
    assert ob.asks[0].amount == 1.5

@pytest.mark.asyncio
async def test_rate_limiter_blocks():
    limiter = TokenBucketRateLimiter(rate=1, capacity=1)
    
    # First request should pass
    async with limiter:
        pass
        
    # Second request should block until tokens are available
    start_time = asyncio.get_event_loop().time()
    async with limiter:
        pass
    end_time = asyncio.get_event_loop().time()
    
    # Since rate is 1 per second, it should take ~1 second
    assert end_time - start_time >= 0.9

@pytest.mark.asyncio
async def test_connector_retries_on_429():
    class MockConnector(ExchangeConnector):
        def __init__(self, rate_limiter):
            super().__init__(rate_limiter)
            self.attempts = 0

        async def get_order_book(self, symbol: str) -> OrderBook:
            return None  # type: ignore

        async def fetch_with_retry(self):
            self.attempts += 1
            if self.attempts == 1:
                return 429
            return 200

    limiter = TokenBucketRateLimiter(rate=10, capacity=10)
    connector = MockConnector(limiter)
    
    with patch.object(ExchangeConnector, 'fetch', new_callable=AsyncMock) as mock_fetch:
        mock_fetch.side_effect = [MagicMock(status=429), MagicMock(status=200)]
        
        # We need to implement fetch_with_retry in the base class
        status = await connector.fetch_with_retry_logic()
        assert status == 200
        assert mock_fetch.call_count == 2
