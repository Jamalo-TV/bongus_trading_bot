import os
import requests
import time
import hmac
import hashlib
from dotenv import load_dotenv

load_dotenv()
api_key = os.getenv('BINANCE_SPOT_API_KEY')
api_secret = os.getenv('BINANCE_SPOT_API_SECRET')

def get_spot_account():
    base_url = "https://testnet.binance.vision" # Wait, spot testnet? Let's check futures account assets
    endpoint = "/fapi/v2/account"
    timestamp = int(time.time() * 1000)
    query_string = f"timestamp={timestamp}"
    signature = hmac.new(api_secret.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256).hexdigest()
    headers = {"X-MBX-APIKEY": api_key}
    url = f"https://testnet.binancefuture.com{endpoint}?{query_string}&signature={signature}"
    res = requests.get(url, headers=headers)
    data = res.json()
    print("Futures assets:")
    for asset in data.get('assets', []):
        if float(asset.get('walletBalance', 0)) > 0:
            print(f"  {asset['asset']}: {asset['walletBalance']}")
            
if __name__ == '__main__':
    get_spot_account()
