import asyncio
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
from models.market_data import OrderBook
from utils.rate_limiter import TokenBucketRateLimiter

class ExchangeConnector(ABC):
    def __init__(self, rate_limiter: Optional[TokenBucketRateLimiter] = None):
        self.rate_limiter = rate_limiter or TokenBucketRateLimiter(rate=10, capacity=10)

    @abstractmethod
    async def get_order_book(self, symbol: str) -> OrderBook:
        pass

    async def fetch(self, url: str) -> Any:
        # This would normally use aiohttp. It's mocked in tests.
        pass

    async def fetch_with_retry_logic(self) -> int:
        max_retries = 3
        for attempt in range(max_retries):
            async with self.rate_limiter:
                response = await self.fetch("mock://url")
                if response.status == 429:
                    # Exponential backoff or just wait for rate limiter
                    await asyncio.sleep(2 ** attempt)
                    continue
                return response.status
        return 429
