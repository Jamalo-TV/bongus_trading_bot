import asyncio
import time

class TokenBucketRateLimiter:
    def __init__(self, rate: float, capacity: float):
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity
        self.last_refill = asyncio.get_event_loop().time()
        self.lock = asyncio.Lock()

    async def _refill(self):
        now = asyncio.get_event_loop().time()
        elapsed = now - self.last_refill
        new_tokens = elapsed * self.rate
        if new_tokens > 0:
            self.tokens = min(self.capacity, self.tokens + new_tokens)
            self.last_refill = now

    async def acquire(self):
        async with self.lock:
            while True:
                await self._refill()
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                
                # Calculate sleep time
                wait_time = (1 - self.tokens) / self.rate
                await asyncio.sleep(wait_time)

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass
