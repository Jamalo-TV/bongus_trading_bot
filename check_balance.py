import asyncio
from bongus.engine.binance_rest import BinanceRest
from bongus.core.config import API_KEY, API_SECRET, TRADING_MODE
async def main():
    rest = BinanceRest(api_key=API_KEY, secret_key=API_SECRET, testnet=(TRADING_MODE=="testnet"))
    spot = await rest.get_spot_account()
    for b in spot.get("balances", []):
        if b["asset"] == "USDT":
            print(f"Spot USDT: {b}")
asyncio.run(main())
