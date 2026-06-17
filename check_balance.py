import os
import time
import hmac
import hashlib
import requests
from bongus.core.binance_endpoints import resolve_binance_credentials, get_rest_base_urls

def main():
    trading_mode = os.getenv("TRADING_MODE", "paper")
    creds = resolve_binance_credentials()
    _, spot_base = get_rest_base_urls(trading_mode)
    if trading_mode == "paper":
        spot_base = "https://testnet.binance.vision"

    timestamp = int(time.time() * 1000)
    query = f"timestamp={timestamp}"
    signature = hmac.new((creds.get('spot_api_secret') or '').encode('utf-8'), query.encode('utf-8'), hashlib.sha256).hexdigest()
    
    headers = {"X-MBX-APIKEY": creds.get('spot_api_key') or ''}
    res = requests.get(f"{spot_base}/api/v3/account?{query}&signature={signature}", headers=headers)
    
    if res.status_code == 200:
        for b in res.json().get("balances", []):
            if b["asset"] == "USDT":
                print(f"Spot USDT: {b}")
    else:
        print(f"Failed to fetch balance: {res.text}")

if __name__ == '__main__':
    main()
