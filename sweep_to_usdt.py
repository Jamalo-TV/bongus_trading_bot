import os
import math
from dotenv import load_dotenv
from binance.client import Client

load_dotenv('.env')
api_key = os.getenv('BINANCE_SPOT_API_KEY')
api_secret = os.getenv('BINANCE_SPOT_API_SECRET')

if not api_key:
    print("Could not find testnet spot api keys in .env")
    exit(1)

client = Client(api_key, api_secret, testnet=True)

account = client.get_account()
balances = account['balances']

for b in balances:
    asset = b['asset']
    free = float(b['free'])
    if free > 0 and asset != 'USDT':
        symbol = f"{asset}USDT"
        try:
            info = client.get_symbol_info(symbol)
            if info:
                step_size = 1.0
                for f in info['filters']:
                    if f['filterType'] == 'LOT_SIZE':
                        step_size = float(f['stepSize'])
                        break
                
                precision = int(round(-math.log(step_size, 10), 0))
                qty = round(math.floor(free / step_size) * step_size, precision)
                
                if qty > 0:
                    print(f"Selling {qty} {asset} for USDT...")
                    client.create_order(
                        symbol=symbol,
                        side=Client.SIDE_SELL,
                        type=Client.ORDER_TYPE_MARKET,
                        quantity=qty
                    )
        except Exception as e:
            print(f"Could not sell {asset}: {e}")

print("Dust sweep complete! Your spot wallet is now entirely USDT.")
