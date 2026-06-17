import os
import requests
import time
import hmac
import hashlib
from dotenv import load_dotenv

load_dotenv()
api_key = os.getenv('BINANCE_SPOT_API_KEY', '')
api_secret = os.getenv('BINANCE_SPOT_API_SECRET', '')

def close_position():
    base_url = "https://testnet.binancefuture.com"
    endpoint = "/fapi/v1/order"
    timestamp = int(time.time() * 1000)
    
    # We are short 30200, so we need to BUY 30200. No reduceOnly.
    query_string = f"symbol=PHBUSDT&side=BUY&type=MARKET&quantity=30200&timestamp={timestamp}"
    
    signature = hmac.new(
        (api_secret or "").encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    
    headers = {"X-MBX-APIKEY": api_key}
    url = f"{base_url}{endpoint}?{query_string}&signature={signature}"
    
    res = requests.post(url, headers=headers)
    print("STATUS:", res.status_code)
    print("RESPONSE:", res.json())

if __name__ == '__main__':
    close_position()
