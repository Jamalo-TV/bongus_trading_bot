import asyncio
import os
from dotenv import load_dotenv
from bongus.core.binance_endpoints import resolve_binance_credentials, get_rest_base_urls
import aiohttp
import time
import hmac
import hashlib
from urllib.parse import urlencode

async def main():
    load_dotenv()
    creds = resolve_binance_credentials()
    f_base, s_base = get_rest_base_urls('testnet')
    
    timestamp = int(time.time() * 1000)
    params = {'timestamp': timestamp}
    query = urlencode(params)
    signature = hmac.new((creds.get('futures_api_secret') or '').encode('utf-8'), query.encode('utf-8'), hashlib.sha256).hexdigest()
    
    async with aiohttp.ClientSession() as session:
        url = f"{f_base}/fapi/v2/positionRisk?{query}&signature={signature}"
        headers = {'X-MBX-APIKEY': creds['futures_api_key']}
        async with session.get(url, headers=headers) as resp:
            data = await resp.json()
            if isinstance(data, dict) and 'code' in data:
                print("Error:", data)
                return
            for pos in data:
                if float(pos['positionAmt']) != 0:
                    print(pos['symbol'], pos['positionAmt'])

asyncio.run(main())
