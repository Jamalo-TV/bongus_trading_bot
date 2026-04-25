import pytest
from pydantic import ValidationError
from models.execution import Order

def test_order_risk_too_high():
    # Assuming risk is a percentage and we want to fail if it's > 5%
    with pytest.raises(ValidationError) as excinfo:
        Order(
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            price=50000.0,
            amount=1.0,
            leverage=20,
            risk_percent=6.0,  # > 5%
            available_margin=10000.0,
            estimated_fees=10.0
        )
    assert "Risk percent must be <= 5%" in str(excinfo.value)

def test_order_margin_insufficient():
    # (Order Size * Entry Price) / Leverage + Estimated Fees <= Available Margin
    # (1.0 * 50000.0) / 10 + 10 = 5010 > 5000
    with pytest.raises(ValidationError) as excinfo:
        Order(
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            price=50000.0,
            amount=1.0,
            leverage=10,
            risk_percent=1.0,
            available_margin=5000.0,
            estimated_fees=10.0
        )
    assert "Insufficient margin" in str(excinfo.value)

def test_valid_order():
    # (0.1 * 50000.0) / 10 + 10 = 510 <= 1000
    order = Order(
        symbol="BTCUSDT",
        side="BUY",
        order_type="LIMIT",
        price=50000.0,
        amount=0.1,
        leverage=10,
        risk_percent=1.0,
        available_margin=1000.0,
        estimated_fees=10.0
    )
    assert order.symbol == "BTCUSDT"
