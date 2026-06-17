import os
import requests
import time
import hmac
import hashlib
from dotenv import load_dotenv

load_dotenv()
api_key = os.getenv('BINANCE_SPOT_API_KEY')
api_secret = os.getenv('BINANCE_SPOT_API_SECRET')

def get_positions():
    base_url = "https://testnet.binancefuture.com"
    endpoint = "/fapi/v2/positionRisk"
    timestamp = int(time.time() * 1000)
    query_string = f"timestamp={timestamp}"
    
    signature = hmac.new(
        api_secret.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    
    headers = {"X-MBX-APIKEY": api_key}
    url = f"{base_url}{endpoint}?{query_string}&signature={signature}"
    
    res = requests.get(url, headers=headers)
    print("TESTNET STATUS:", res.status_code)
    print("TESTNET RESPONSE:", res.json())

if __name__ == '__main__':
    get_positions()
