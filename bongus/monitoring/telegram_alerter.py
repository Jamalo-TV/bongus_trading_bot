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
import re
import time as _time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

from bongus.core.config_manager import validate_live_config
from bongus.engine.state_store import StateReader, StateWriter
from bongus.monitoring.performance_metrics import calculate_metrics

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
_LIVE_CONFIG_PATH = os.path.join(_PROJECT_ROOT, "live_config.json")
_APPROVAL_RE = re.compile(r"^(ja|nein)\s+([A-Za-z0-9._:-]+)\s*$", re.IGNORECASE)
_CONFIG_WHITELIST = {
    "entry_ann_funding_threshold",
    "exit_ann_funding_threshold",
    "max_gross_exposure_usd",
    "notional_per_trade",
    "max_notional_per_trade",
    "regime_filter_basis_zscore_max",
    "regime_filter_depth_ratio_min",
    "loss_streak_entry_multiplier",
    "loss_streak_notional_scale",
}

# alert_key -> monotonic timestamp of last send
_last_alert: dict[str, float] = {}
_escalation_level: dict[str, int] = {}
_disconnected_symbols: set[str] = set()


def _throttled(key: str, window: float = _THROTTLE_S) -> bool:
    """Return True (and do NOT update timestamp) if within throttle window.

    Returns False and records the send time when the alert may proceed.
    """
    now = _time.monotonic()
    if key in _last_alert and now - _last_alert[key] < window:
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


def _load_live_config() -> dict:
    if not os.path.exists(_LIVE_CONFIG_PATH):
        return {}
    try:
        with open(_LIVE_CONFIG_PATH, encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError):
        return {}


def _write_live_config(config: dict) -> None:
    Path(_LIVE_CONFIG_PATH).parent.mkdir(parents=True, exist_ok=True)
    with open(_LIVE_CONFIG_PATH, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _parse_approval_message(text: str) -> tuple[str, str] | None:
    match = _APPROVAL_RE.match((text or "").strip())
    if match is None:
        return None
    action = match.group(1).lower()
    proposal_id = match.group(2)
    return action, proposal_id


def _apply_proposal_to_config(proposal: dict) -> tuple[bool, str]:
    proposed_changes = proposal.get("proposed_changes") or {}
    if not isinstance(proposed_changes, dict):
        return False, "proposal changes are not a JSON object"

    filtered_changes = {
        key: value for key, value in proposed_changes.items() if key in _CONFIG_WHITELIST
    }
    if not filtered_changes:
        return False, "proposal has no whitelisted config keys"

    merged = _load_live_config()
    merged.update(filtered_changes)
    validate_live_config(merged)
    _write_live_config(merged)
    return True, ", ".join(sorted(filtered_changes))


def _format_daily_summary(reader: StateReader) -> str:
    metrics = calculate_metrics(reader)
    risk = reader.get_risk()
    runtime_mode = str(risk.get("runtime_mode") or "LIVE").upper()
    return (
        "📊 *DAILY PNL SUMMARY*\n"
        f"Runtime: `{runtime_mode}`\n"
        f"Net PnL: `{metrics['total_pnl']:+.2f} USD`\n"
        f"Monthly Return: `{metrics['monthly_return_pct'] * 100:.2f}%`\n"
        f"Win Rate: `{metrics['win_rate'] * 100:.1f}%`\n"
        f"Sharpe: `{metrics['sharpe_ratio_annualized']:.2f}`\n"
        f"Max Drawdown: `{metrics['max_drawdown_pct'] * 100:.2f}%`\n"
        f"Cost Error: `{metrics['cost_model_error_pct']:.2f}%`\n"
        f"Decision Gate: `{metrics['go_no_go']}`"
    )


async def poll_state_alerts(session: aiohttp.ClientSession) -> None:
    """Poll SQLite state every 30 s; send alerts for meaningful changes."""
    reader = StateReader()
    writer = StateWriter()
    prev_symbols: set[str] = set()
    prev_trade_count: int = 0
    prev_kill_switch: bool = False
    prev_runtime_mode: str = ""
    prev_preflight_status: str = ""
    prev_safe_mode_reason: str = ""
    prev_config_error: str = ""
    prev_heartbeat_status: str = ""
    prev_hedge_gap_symbols: tuple[str, ...] = ()
    last_daily_summary_date: str = ""

    # Prime initial state so we don't alert on startup
    try:
        prev_symbols = {p["symbol"] for p in reader.get_positions()}
        prev_trade_count = len(reader.get_trades(limit=500))
        risk = reader.get_risk()
        prev_kill_switch = str(risk.get("kill_switch", "false")).lower() in ("true", "1")
        prev_runtime_mode = str(risk.get("runtime_mode", "")).upper()
        prev_preflight_status = str(risk.get("preflight_status", ""))
        prev_safe_mode_reason = str(risk.get("safe_mode_reason", ""))
        prev_config_error = str(risk.get("config_last_error", ""))
        prev_heartbeat_status = str(risk.get("heartbeat_status", ""))
        prev_hedge_gap_symbols = tuple(risk.get("startup_reconciliation_spot_hedge_gaps", []))
        last_daily_summary_date = str(risk.get("last_daily_pnl_summary_at", ""))[:10]
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
            runtime_mode = str(risk.get("runtime_mode", "LIVE")).upper()
            preflight_status = str(risk.get("preflight_status", ""))
            safe_mode_reason = str(risk.get("safe_mode_reason", ""))
            config_last_error = str(risk.get("config_last_error", ""))
            heartbeat_status = str(risk.get("heartbeat_status", ""))
            hedge_gap_symbols = tuple(risk.get("startup_reconciliation_spot_hedge_gaps", []))

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

            if runtime_mode != prev_runtime_mode and runtime_mode:
                await send_telegram(
                    session,
                    "🧭 *RUNTIME MODE CHANGED*\n"
                    f"Mode: `{runtime_mode}`\n"
                    f"Reason: `{safe_mode_reason or 'n/a'}`",
                )

            if preflight_status != prev_preflight_status and preflight_status:
                await send_telegram(
                    session,
                    "🛫 *PREFLIGHT STATUS*\n"
                    f"Status: `{preflight_status}`",
                )

            if safe_mode_reason and safe_mode_reason != prev_safe_mode_reason and not _throttled("safe_mode_reason", 60):
                await send_telegram(
                    session,
                    "⚠️ *SAFE MODE ACTIVE*\n"
                    f"Reason: `{safe_mode_reason}`",
                )

            if heartbeat_status != prev_heartbeat_status and heartbeat_status:
                await send_telegram(
                    session,
                    "💓 *HEARTBEAT STATUS*\n"
                    f"State: `{heartbeat_status}`",
                )

            if hedge_gap_symbols and hedge_gap_symbols != prev_hedge_gap_symbols and not _throttled("hedge_gap", 60):
                await send_telegram(
                    session,
                    "🛑 *HEDGE GAP DETECTED*\n"
                    f"Symbols: `{', '.join(hedge_gap_symbols)}`",
                )

            if config_last_error and config_last_error != prev_config_error and not _throttled("config_error", 60):
                await send_telegram(
                    session,
                    "⚠️ *CONFIG RELOAD REJECTED*\n"
                    f"Error: `{config_last_error[:300]}`",
                )

            prev_kill_switch = ks
            prev_runtime_mode = runtime_mode
            prev_preflight_status = preflight_status
            prev_safe_mode_reason = safe_mode_reason
            prev_config_error = config_last_error
            prev_heartbeat_status = heartbeat_status
            prev_hedge_gap_symbols = hedge_gap_symbols

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

            # ── Daily summary ─────────────────────────────────────────
            now = datetime.now(timezone.utc)
            if (
                now.hour == 0
                and now.minute >= 5
                and last_daily_summary_date != now.date().isoformat()
                and not _throttled(f"daily_summary_{now.date().isoformat()}", window=300)
            ):
                await send_telegram(session, _format_daily_summary(reader))
                writer.set_risk("last_daily_pnl_summary_at", now.isoformat())
                last_daily_summary_date = now.date().isoformat()

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


async def poll_command_updates(session: aiohttp.ClientSession) -> None:
    """Poll Telegram bot updates and process proposal approvals."""
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return

    reader = StateReader()
    writer = StateWriter()
    risk = reader.get_risk()
    offset = int(risk.get("telegram_last_update_id", 0) or 0)

    if offset == 0:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
            async with session.get(url, params={"timeout": 1, "limit": 1}, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                payload = await resp.json()
            updates = payload.get("result") or []
            if updates:
                offset = int(updates[-1]["update_id"]) + 1
                writer.set_risk("telegram_last_update_id", str(offset))
        except Exception as exc:
            logger.warning("Failed to initialize Telegram update offset: %s", exc)

    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
            params = {"timeout": 30, "offset": offset}
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=35)) as resp:
                payload = await resp.json()
            for update in payload.get("result") or []:
                offset = max(offset, int(update["update_id"]) + 1)
                writer.set_risk("telegram_last_update_id", str(offset))

                message = update.get("message") or {}
                chat_id = str((message.get("chat") or {}).get("id", ""))
                if chat_id != str(CHAT_ID):
                    continue
                text = str(message.get("text") or "")
                parsed = _parse_approval_message(text)
                if parsed is None:
                    continue

                action, proposal_id = parsed
                proposal = reader.get_ai_report_proposal(proposal_id)
                if proposal is None:
                    await send_telegram(
                        session,
                        f"❓ *UNKNOWN PROPOSAL*\nNo proposal found for `{proposal_id}`.",
                    )
                    continue

                if action == "nein":
                    writer.update_ai_report_proposal(
                        proposal_id,
                        status="REJECTED",
                        decision_source="telegram",
                        applied=False,
                    )
                    await send_telegram(
                        session,
                        f"🛑 *PROPOSAL REJECTED*\nID: `{proposal_id}`",
                    )
                    continue

                try:
                    applied, changed_keys = _apply_proposal_to_config(proposal)
                except Exception as exc:
                    writer.update_ai_report_proposal(
                        proposal_id,
                        status="APPROVAL_FAILED",
                        decision_source="telegram",
                        applied=False,
                    )
                    await send_telegram(
                        session,
                        f"⚠️ *PROPOSAL APPLY FAILED*\nID: `{proposal_id}`\nError: `{str(exc)[:300]}`",
                    )
                    continue

                if applied:
                    writer.update_ai_report_proposal(
                        proposal_id,
                        status="APPLIED",
                        decision_source="telegram",
                        applied=True,
                    )
                    await send_telegram(
                        session,
                        f"✅ *PROPOSAL APPLIED*\nID: `{proposal_id}`\nKeys: `{changed_keys}`",
                    )
                else:
                    writer.update_ai_report_proposal(
                        proposal_id,
                        status="APPROVAL_SKIPPED",
                        decision_source="telegram",
                        applied=False,
                    )
                    await send_telegram(
                        session,
                        f"ℹ️ *PROPOSAL NOT APPLIED*\nID: `{proposal_id}`\nReason: `{changed_keys}`",
                    )

        except Exception as exc:
            logger.error("Telegram command polling error: %s", exc)
            await asyncio.sleep(5)


async def main() -> None:
    async with aiohttp.ClientSession() as session:
        await asyncio.gather(
            listen_ipc_alerts(session),
            poll_state_alerts(session),
            poll_command_updates(session),
        )


if __name__ == "__main__":
    asyncio.run(main())
