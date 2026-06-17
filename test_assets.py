import os
import requests
import time
import hmac
import hashlib
from dotenv import load_dotenv

load_dotenv()
api_key = os.getenv('BINANCE_SPOT_API_KEY')
api_secret = os.getenv('BINANCE_SPOT_API_SECRET')

def get_assets():
    base_url = "https://testnet.binancefuture.com"
    endpoint = "/fapi/v2/account"
    timestamp = int(time.time() * 1000)
    query_string = f"timestamp={timestamp}"
    signature = hmac.new(api_secret.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256).hexdigest()
    headers = {"X-MBX-APIKEY": api_key}
    url = f"{base_url}{endpoint}?{query_string}&signature={signature}"
    res = requests.get(url, headers=headers)
    data = res.json()
    assets = data.get('assets', [])
    for a in assets:
        if float(a.get('walletBalance', 0)) > 0 or float(a.get('marginBalance', 0)) > 0:
            print(a)
            
if __name__ == '__main__':
    get_assets()
