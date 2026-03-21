import asyncio
import logging
import os

import aiohttp

from bongus.ipc import ExecutionClient

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
        connected = False
        async for data in ExecutionClient.listen_events(reconnect_delay=5.0):
            if not connected:
                await send_telegram_alert(session, "🟢 *Bongus Alerter Online* - Connected to Rust Engine.")
                connected = True

            event_type = data.get("event")

            if event_type == "OrderUpdate" and data.get("status") == "FILLED":
                await send_telegram_alert(
                    session,
                    f"💰 *TRADE FILLED*\nSymbol: {data.get('symbol')}\nQty: {data.get('filled_qty')}"
                )
            elif event_type == "Disconnected":
                symbol = data.get("symbol", "UNKNOWN")
                await send_telegram_alert(session, f"⚠️ *CRITICAL:* Rust engine disconnected from Binance for {symbol}!")

def main_sync():
    asyncio.run(listen_for_alerts())


if __name__ == "__main__":
    main_sync()
