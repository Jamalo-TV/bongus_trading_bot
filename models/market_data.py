from dataclasses import dataclass
from typing import List

@dataclass(frozen=True)
class PriceLevel:
    price: float
    amount: float

@dataclass(frozen=True)
class OrderBook:
    symbol: str
    bids: List[PriceLevel]
    asks: List[PriceLevel]
    timestamp: int
