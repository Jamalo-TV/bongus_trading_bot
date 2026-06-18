import os
import sys
import time
import hmac
import hashlib
import requests
from urllib.parse import urlencode
from dotenv import load_dotenv

# Load env
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

_DOTENV_PATH = os.path.join(_PROJECT_ROOT, ".env")
load_dotenv(_DOTENV_PATH)

API_KEY = os.getenv("BINANCE_SPOT_API_KEY")
API_SECRET = os.getenv("BINANCE_SPOT_API_SECRET")

# Explicitly use Spot Testnet
BASE_URL = "https://demo-api.binance.com/api"

def sign_request(params: dict) -> str:
    query_string = urlencode(params)
    signature = hmac.new(
        API_SECRET.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return f"{query_string}&signature={signature}"

def get_account_info():
    endpoint = f"{BASE_URL}/v3/account"
    params = {"timestamp": int(time.time() * 1000)}
    query = sign_request(params)
    headers = {"X-MBX-APIKEY": API_KEY}
    resp = requests.get(f"{endpoint}?{query}", headers=headers)
    resp.raise_for_status()
    return resp.json()

def get_exchange_info():
    endpoint = f"{BASE_URL}/v3/exchangeInfo"
    resp = requests.get(endpoint)
    resp.raise_for_status()
    return resp.json()

def market_sell(symbol: str, quantity: float):
    endpoint = f"{BASE_URL}/v3/order"
    params = {
        "symbol": symbol,
        "side": "SELL",
        "type": "MARKET",
        "quantity": quantity,
        "timestamp": int(time.time() * 1000)
    }
    query = sign_request(params)
    headers = {"X-MBX-APIKEY": API_KEY}
    resp = requests.post(f"{endpoint}?{query}", headers=headers)
    resp.raise_for_status()
    return resp.json()

def run_sweeper():
    print("[rebalancer] Autonomous Dust Sweeper started.")
    print(f"[rebalancer] Target API: {BASE_URL}")

    while True:
        try:
            exchange_info = get_exchange_info()
            symbols_info = {s["symbol"]: s for s in exchange_info["symbols"]}

            account = get_account_info()
            balances = {b["asset"]: float(b["free"]) for b in account["balances"] if float(b["free"]) > 0}

            for asset, free_balance in balances.items():
                if asset in ["USDT", "ETHW", "BETH"]: # Ignore USDT and un-tradeable forks
                    continue
                
                symbol = f"{asset}USDT"
                if symbol not in symbols_info:
                    continue
                
                symbol_info = symbols_info[symbol]
                lot_size_filter = next((f for f in symbol_info["filters"] if f["filterType"] == "LOT_SIZE"), None)

                if lot_size_filter:
                    step_size = float(lot_size_filter["stepSize"])
                    # Calculate quantity conforming to step_size
                    precision = len(str(step_size).split(".")[1]) if "." in str(step_size) else 0
                    quantity = int(free_balance / step_size) * step_size
                    quantity = round(quantity, precision)

                    if quantity > 0:
                        try:
                            print(f"[rebalancer] Sweeping {quantity} {asset} to USDT...")
                            res = market_sell(symbol, quantity)
                            print(f"[rebalancer] Successfully sold {quantity} {asset}: {res['orderId']}")
                        except requests.exceptions.HTTPError as e:
                            # 400 Client Error: Bad Request usually means MIN_NOTIONAL not met
                            error_msg = e.response.json().get("msg", "")
                            if "MIN_NOTIONAL" in error_msg:
                                print(f"[rebalancer] Skipping {asset}: MIN_NOTIONAL not met")
                            else:
                                print(f"[rebalancer] Failed to sell {asset}: {e} - {e.response.text}")
        
        except requests.exceptions.HTTPError as e:
            print(f"[rebalancer] API Error: {e.response.text}")
        except Exception as e:
            print(f"[rebalancer] Unexpected error: {e}")
        
        time.sleep(60)

if __name__ == "__main__":
    if not API_KEY or not API_SECRET:
        print("[rebalancer] Error: Missing BINANCE_SPOT_API_KEY or BINANCE_SPOT_API_SECRET in .env")
        sys.exit(1)
    run_sweeper()
