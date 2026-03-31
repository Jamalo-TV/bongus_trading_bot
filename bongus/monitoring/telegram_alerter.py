"""Telegram alerter — bridges Rust IPC events and SQLite state changes to Telegram.

Two alert sources:
  1. Rust IPC (port 9000): order fills, WebSocket disconnections.
  2. StateReader polling (every 30s): position opens/closes, drawdown warnings,
     kill-switch activation, completed trade summaries.

Disconnect alerts use an escalating per-symbol throttle to prevent reconnect
spam during unstable WebSocket periods.
"""

import asyncio
import json
import logging
import os
import time as _time

import aiohttp
from dotenv import load_dotenv

from bongus.engine.state_store import StateReader

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_DOTENV_PATH = os.path.join(_PROJECT_ROOT, ".env")

load_dotenv(_DOTENV_PATH)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN_BONGUS")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID_BONGUS")

# Seconds between repeated alerts of the same type
_THROTTLE_S = 300  # 5 minutes
_DISCONNECT_THROTTLE_TIERS_S = (300, 900, 1800, 3600)

# alert_key -> monotonic timestamp of last send
_last_alert: dict[str, float] = {}
_escalation_level: dict[str, int] = {}
_disconnected_symbols: set[str] = set()


def _throttled(key: str, window: float = _THROTTLE_S) -> bool:
    """Return True (and do NOT update timestamp) if within throttle window.

    Returns False and records the send time when the alert may proceed.
    """
    now = _time.monotonic()
    if now - _last_alert.get(key, 0.0) < window:
        return True
    _last_alert[key] = now
    return False


def _normalized_symbol(symbol: str) -> str:
    return str(symbol or "UNKNOWN").upper()


def _disconnect_window(symbol: str) -> int:
    level = _escalation_level.get(symbol, 0)
    level = max(0, min(level, len(_DISCONNECT_THROTTLE_TIERS_S) - 1))
    return _DISCONNECT_THROTTLE_TIERS_S[level]


def _should_send_disconnect(symbol: str) -> bool:
    symbol = _normalized_symbol(symbol)
    _disconnected_symbols.add(symbol)
    if _throttled(f"disconnect_{symbol}", window=_disconnect_window(symbol)):
        return False
    _escalation_level[symbol] = min(
        _escalation_level.get(symbol, 0) + 1,
        len(_DISCONNECT_THROTTLE_TIERS_S) - 1,
    )
    return True


def _consume_reconnect(symbol: str) -> bool:
    symbol = _normalized_symbol(symbol)
    if symbol not in _disconnected_symbols:
        return False
    _disconnected_symbols.discard(symbol)
    _escalation_level.pop(symbol, None)
    _last_alert.pop(f"disconnect_{symbol}", None)
    _last_alert.pop(f"reconnect_{symbol}", None)
    return True


async def send_telegram(session: aiohttp.ClientSession, message: str) -> None:
    """Send a Markdown-formatted message to Telegram. Fire-and-forget."""
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.warning("Telegram credentials not set — skipping alert.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                logger.error("Telegram API error %s: %s", resp.status, await resp.text())
    except Exception as exc:
        logger.error("Failed to send Telegram alert: %s", exc)


async def poll_state_alerts(session: aiohttp.ClientSession) -> None:
    """Poll SQLite state every 30 s; send alerts for meaningful changes."""
    reader = StateReader()
    prev_symbols: set[str] = set()
    prev_trade_count: int = 0
    prev_kill_switch: bool = False

    # Prime initial state so we don't alert on startup
    try:
        prev_symbols = {p["symbol"] for p in reader.get_positions()}
        prev_trade_count = len(reader.get_trades(limit=500))
        risk = reader.get_risk()
        prev_kill_switch = str(risk.get("kill_switch", "false")).lower() in ("true", "1")
    except Exception as exc:
        logger.warning("State prime failed: %s", exc)

    while True:
        await asyncio.sleep(30)
        try:
            # ── Positions ──────────────────────────────────────────────
            positions = reader.get_positions()
            current_symbols = {p["symbol"] for p in positions}
            pos_map = {p["symbol"]: p for p in positions}

            for sym in current_symbols - prev_symbols:
                if not _throttled(f"open_{sym}"):
                    p = pos_map[sym]
                    direction = (
                        "🟢 Long Spot / Short Perp"
                        if p.get("direction") == "long"
                        else "🔴 Short Spot / Long Perp"
                    )
                    await send_telegram(
                        session,
                        f"📈 *POSITION OPENED*\n"
                        f"Symbol: `{sym}`\n"
                        f"Direction: {direction}\n"
                        f"Ann\\. Funding: `{p.get('ann_funding', 0) * 100:.2f}%`\n"
                        f"Qty: `{p.get('qty', 0):.4f}`",
                    )

            for sym in prev_symbols - current_symbols:
                if not _throttled(f"close_{sym}"):
                    recent_trades = reader.get_trades(limit=10)
                    t = next((x for x in recent_trades if x["symbol"] == sym), None)
                    pnl = t["net_pnl_usd"] if t else 0.0
                    funding = t["funding_collected"] if t else 0.0
                    sign = "+" if pnl >= 0 else ""
                    await send_telegram(
                        session,
                        f"📉 *POSITION CLOSED*\n"
                        f"Symbol: `{sym}`\n"
                        f"Net PnL: `{sign}${pnl:.2f}`\n"
                        f"Funding Collected: `+${funding:.4f}`",
                    )

            prev_symbols = current_symbols

            # ── Risk ───────────────────────────────────────────────────
            risk = reader.get_risk()
            dd = float(risk.get("drawdown_pct") or 0.0)
            ks = str(risk.get("kill_switch", "false")).lower() in ("true", "1")

            if ks and not prev_kill_switch and not _throttled("kill_switch", 60):
                await send_telegram(
                    session,
                    "🚨 *KILL SWITCH ACTIVATED*\n"
                    "All new positions are blocked\\. Check the dashboard immediately\\.",
                )
            elif dd > 0.08 and not _throttled("drawdown_critical"):
                await send_telegram(
                    session,
                    f"🔴 *CRITICAL DRAWDOWN*\n"
                    f"Current: `{dd * 100:.1f}%`  \\(threshold: 10%\\)\n"
                    f"Kill switch may trigger soon\\.",
                )
            elif dd > 0.04 and not _throttled("drawdown_soft"):
                await send_telegram(
                    session,
                    f"⚠️ *SOFT DRAWDOWN WARNING*\n"
                    f"Current: `{dd * 100:.1f}%`  \\(threshold: 4%\\)\n"
                    f"Position sizing scaled down\\.",
                )

            prev_kill_switch = ks

            # ── Completed trades ───────────────────────────────────────
            trades = reader.get_trades(limit=500)
            new_count = len(trades)
            if new_count > prev_trade_count:
                new_trades = trades[: new_count - prev_trade_count]
                for t in new_trades:
                    sym = t.get("symbol", "?")
                    uid = f"{sym}_{t.get('exit_time', '')}"
                    if not _throttled(f"trade_{uid}", window=10):
                        pnl = t.get("net_pnl_usd", 0.0)
                        funding = t.get("funding_collected", 0.0)
                        sign = "+" if pnl >= 0 else ""
                        await send_telegram(
                            session,
                            f"💰 *TRADE COMPLETED*\n"
                            f"Symbol: `{sym}`\n"
                            f"Net PnL: `{sign}${pnl:.2f}`\n"
                            f"Funding: `+${funding:.4f}`",
                        )
                prev_trade_count = new_count

        except Exception as exc:
            logger.error("State polling error: %s", exc)


async def listen_ipc_alerts(session: aiohttp.ClientSession) -> None:
    """Listen to Rust IPC TCP stream (port 9000) and forward events to Telegram."""
    while True:
        try:
            reader, _ = await asyncio.open_connection("127.0.0.1", 9000)
            logger.info("Connected to Rust Engine IPC.")
            if not _throttled("online", window=60):
                await send_telegram(
                    session,
                    "🟢 *Bongus Alerter Online*\nConnected to Rust Engine\\.",
                )

            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    data = json.loads(line.decode("utf-8").strip())
                except json.JSONDecodeError:
                    continue

                event = data.get("event")

                if event == "OrderUpdate" and data.get("status") == "FILLED":
                    sym = data.get("symbol", "?")
                    qty = data.get("filled_qty", 0)
                    if not _throttled(f"fill_{sym}"):
                        await send_telegram(
                            session,
                            f"⚡ *ORDER FILLED*\nSymbol: `{sym}`\nQty: `{qty}`",
                        )

                elif event == "Disconnected":
                    sym = _normalized_symbol(data.get("symbol", "UNKNOWN"))
                    if _should_send_disconnect(sym):
                        await send_telegram(
                            session,
                            f"⚠️ *WS DISCONNECTED*\n"
                            f"Binance WebSocket dropped for `{sym}`\\.",
                        )

                elif event == "Connected":
                    sym = _normalized_symbol(data.get("symbol", "UNKNOWN"))
                    if _consume_reconnect(sym) and not _throttled(f"reconnect_{sym}", window=10):
                        await send_telegram(
                            session,
                            f"✅ *WS RECONNECTED*\n"
                            f"Binance WebSocket is back for `{sym}`\\.",
                        )

        except Exception as exc:
            logger.error("IPC error: %s — retrying in 5 s", exc)
            await asyncio.sleep(5)


async def main() -> None:
    async with aiohttp.ClientSession() as session:
        await asyncio.gather(
            listen_ipc_alerts(session),
            poll_state_alerts(session),
        )


if __name__ == "__main__":
    asyncio.run(main())
