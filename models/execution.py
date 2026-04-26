from pydantic import BaseModel, Field, model_validator
from typing import Literal
from core.state import METRICS

class Order(BaseModel):
    symbol: str
    side: Literal["BUY", "SELL"]
    order_type: Literal["LIMIT", "MARKET"]
    price: float
    amount: float
    leverage: int = Field(gt=0)
    risk_percent: float = Field(gt=0)
    available_margin: float = Field(gt=0)
    estimated_fees: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_risk_and_margin(self) -> "Order":
        if self.risk_percent > 5.0:
            raise ValueError("Risk percent must be <= 5%")
        
        required_margin = (self.amount * self.price) / self.leverage + self.estimated_fees
        if required_margin > self.available_margin:
            raise ValueError(f"Insufficient margin: required {required_margin}, available {self.available_margin}")
        
        # Increment global validation count
        METRICS["total_orders_validated"] += 1
        
        return self
