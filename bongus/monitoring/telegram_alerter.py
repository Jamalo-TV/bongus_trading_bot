import asyncio
import json
import logging
import os

import aiohttp
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN_BONGUS")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID_BONGUS")

async def send_telegram_alert(session: aiohttp.ClientSession, message: str):
    """Sends a non-blocking asynchronous request to the Telegram API."""
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logging.warning("Telegram credentials missing in environment variables. Cannot send alert.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        async with session.post(url, json=payload, timeout=5) as resp:
            if resp.status != 200:
                logging.error(f"Telegram API error: {await resp.text()}")
    except Exception as e:
        logging.error(f"Failed to send Telegram alert: {e}")

async def listen_for_alerts():
    """Main event loop bridging the Rust IPC TCP stream to Telegram."""
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                reader, _ = await asyncio.open_connection('127.0.0.1', 9000)
                logging.info("Connected to Rust Engine IPC.")
                await send_telegram_alert(session, "🟢 *Bongus Alerter Online* - Connected to Rust Engine.")
                
                while True:
                    line = await reader.readline()
                    if not line: 
                        break
                    
                    try:
                        data = json.loads(line.decode('utf-8').strip())
                        event_type = data.get("event")
                        
                        if event_type == "OrderUpdate" and data.get("status") == "FILLED":
                            await send_telegram_alert(
                                session, 
                                f"💰 *TRADE FILLED*\nSymbol: {data.get('symbol')}\nQty: {data.get('filled_qty')}"
                            )
                        
                        # Corrected mapping matching WsEvent enum serialization
                        elif event_type == "Disconnected":
                            symbol = data.get("symbol", "UNKNOWN")
                            await send_telegram_alert(session, f"⚠️ *CRITICAL:* Rust engine disconnected from Binance for {symbol}!")
                            
                    except json.JSONDecodeError:
                        pass # Ignore standard string logs from the engine
                        
            except Exception as e:
                logging.error(f"IPC Connection error: {e}. Retrying in 5 seconds...")
                await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(listen_for_alerts())