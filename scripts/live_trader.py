import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Dict, Optional
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

from bongus.core.config import (
    ACCOUNT_EQUITY_USD,
    ENTRY_ANN_FUNDING_THRESHOLD_ALT,
    ENTRY_ANN_FUNDING_THRESHOLD_BTC,
    EXIT_ANN_FUNDING_THRESHOLD,
    FUNDING_CAPTURE_DELAY_MIN,
    HOLD_THROUGH_FUNDING,
    MAKER_ORDER_PATIENCE_SEC,
    MAX_CONCURRENT_POSITIONS,
    MAX_GROSS_EXPOSURE_USD,
    MAX_NOTIONAL_PER_TRADE,
    MONITORED_SYMBOLS,
    NOTIONAL_PER_TRADE,
)
from bongus.engine.risk_engine import RiskEngine, RiskState
from bongus.engine.state_store import StateWriter, Trade
from bongus.ipc.execution import ExecutionClient

load_dotenv()

writer = StateWriter()
risk_engine = RiskEngine()

# ── Persistent File Logging ──────────────────────────────────────────────
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs")
LOG_FILE = os.path.join(LOG_DIR, "live_trader.log")
os.makedirs(LOG_DIR, exist_ok=True)

# Create logger for persistent file
file_logger = logging.getLogger("bongus_persistent")
file_logger.setLevel(logging.INFO)
file_logger.propagate = False

# Rotating file handler: max 5MB per file, keep 5 files
file_handler = RotatingFileHandler(
    LOG_FILE, maxBytes=5*1024*1024, backupCount=5, encoding="utf-8"
)
file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
file_logger.addHandler(file_handler)

def log(msg: str, level: str = "INFO"):
    """Print to console AND write to persistent log file."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    print(f"{timestamp} {level} {msg}")
    if level == "ERROR":
        file_logger.error(msg)
    elif level == "WARNING":
        file_logger.warning(msg)
    else:
        file_logger.info(msg)


@dataclass
class SymbolData:
    """Per-symbol trading data."""
    symbol: str
    ann_funding: float = 0.0
    spot_price: float = 0.0
    perp_price: float = 0.0
    basis_pct: float = 0.0
    last_funding_time: Optional[datetime] = None
    funding_just_paid: bool = False
    in_position: bool = False
    entry_time: str = ""
    entry_price: float = 0.0
    perp_entry_price: float = 0.0
    qty: float = 0.0
    entry_funding: float = 0.0
    # Maker order state
    maker_order_placed: bool = False
    maker_order_time: float = 0.0


# Global state
positions: Dict[str, SymbolData] = {symbol: SymbolData(symbol=symbol) for symbol in MONITORED_SYMBOLS}
live_data = {symbol: SymbolData(symbol=symbol) for symbol in MONITORED_SYMBOLS}

# Dynamic thresholds (updated from optimal_params.json)
dynamic_entry_thresholds: Dict[str, float] = {symbol: ENTRY_ANN_FUNDING_THRESHOLD_BTC for symbol in MONITORED_SYMBOLS}

# Track when funding was last paid
last_funding_check = {symbol: 0.0 for symbol in MONITORED_SYMBOLS}


def get_entry_threshold(symbol: str) -> float:
    """Get symbol-specific entry threshold."""
    if symbol == "BTCUSDT":
        return dynamic_entry_thresholds.get(symbol, ENTRY_ANN_FUNDING_THRESHOLD_BTC)
    return dynamic_entry_thresholds.get(symbol, ENTRY_ANN_FUNDING_THRESHOLD_ALT)


async def watch_params_file():
    """Continuously checks optimal_params.json for threshold updates."""
    while True:
        try:
            if os.path.exists("optimal_params.json"):
                with open("optimal_params.json", "r") as f:
                    params = json.load(f)
                    for symbol in MONITORED_SYMBOLS:
                        key = f"ENTRY_ANN_FUNDING_THRESHOLD_{symbol.replace('USDT', '')}"
                        if key in params:
                            dynamic_entry_thresholds[symbol] = params[key]
                        elif "ENTRY_ANN_FUNDING_THRESHOLD" in params:
                            dynamic_entry_thresholds[symbol] = params["ENTRY_ANN_FUNDING_THRESHOLD"]
        except Exception:
            pass
        await asyncio.sleep(60)


async def fetch_all_symbols_funding():
    """Fetch funding data for all monitored symbols."""
    tasks = []
    for symbol in MONITORED_SYMBOLS:
        tasks.append(fetch_symbol_funding(symbol))
    await asyncio.gather(*tasks, return_exceptions=True)


async def fetch_symbol_funding(symbol: str):
    """Fetch funding data for a single symbol from Binance."""
    try:
        url = f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}"
        resp = await asyncio.to_thread(requests.get, url, timeout=5)
        data = resp.json()

        raw_8h_rate = float(data.get("lastFundingRate", 0.0))
        spot_price = float(data.get("indexPrice", 0.0))
        perp_price = float(data.get("markPrice", 0.0))
        next_funding_time = int(data.get("nextFundingTime", 0))

        live_data[symbol].ann_funding = raw_8h_rate * 3 * 365
        live_data[symbol].spot_price = spot_price
        live_data[symbol].perp_price = perp_price

        if spot_price > 0:
            live_data[symbol].basis_pct = (perp_price - spot_price) / spot_price

        # Check if funding was just paid (within last 5 minutes)
        current_time = time.time()
        if next_funding_time > 0:
            time_since_funding = (current_time * 1000 - next_funding_time) / 1000 / 60  # minutes ago
            if -FUNDING_CAPTURE_DELAY_MIN < time_since_funding < 5:
                live_data[symbol].funding_just_paid = True
            else:
                live_data[symbol].funding_just_paid = False

    except Exception as e:
        log(f"[Warning] Could not fetch {symbol} funding rate: {e}")


async def check_initial_positions():
    """Check Binance REST API for existing positions before starting."""
    log("Checking initial positions with Binance API...")

    global positions
    try:
        api_key = os.getenv("BINANCE_API_KEY", "")
        api_secret = os.getenv("BINANCE_API_SECRET", "")
        use_testnet = os.getenv("USE_TESTNET", "true").lower() == "true"

        if api_key and api_secret and not use_testnet:
            headers = {"X-MBX-APIKEY": api_key}

            # Check positions for each symbol
            for symbol in MONITORED_SYMBOLS:
                timestamp = int(time.time() * 1000)
                query_string = urlencode({
                    "symbol": symbol,
                    "timestamp": timestamp,
                })
                signature = hmac.new(
                    api_secret.encode("utf-8"),
                    query_string.encode("utf-8"),
                    hashlib.sha256,
                ).hexdigest()
                resp = await asyncio.to_thread(
                    requests.get,
                    f"https://fapi.binance.com/fapi/v2/positionRisk?{query_string}&signature={signature}",
                    headers=headers,
                    timeout=10
                )
                data = resp.json()
                for pos in data:
                    if float(pos.get("positionAmt", 0)) != 0:
                        # Position exists - sync state
                        live_data[symbol].in_position = True
                        positions[symbol].in_position = True
                        positions[symbol].qty = abs(float(pos.get("positionAmt", 0)))
                        positions[symbol].entry_price = float(pos.get("entryPrice", 0))
                        log(f"Found existing {symbol} position: {pos['positionAmt']}")
        else:
            # Testnet - assume no positions
            for symbol in MONITORED_SYMBOLS:
                live_data[symbol].in_position = False
                positions[symbol].in_position = False

        log("Startup verification complete.")
    except Exception as e:
        log(f"CRITICAL ERROR: Cannot reach Binance to verify positions on startup. {e}", "ERROR")
        raise SystemExit("Refusing to start to prevent double-entries.")


def rank_symbols_by_funding() -> list:
    """Rank all symbols by annualized funding rate, return sorted list."""
    symbol_rates = []
    for symbol in MONITORED_SYMBOLS:
        if live_data[symbol].spot_price > 0:
            symbol_rates.append((symbol, live_data[symbol].ann_funding))
    # Sort by funding rate descending
    symbol_rates.sort(key=lambda x: x[1], reverse=True)
    return symbol_rates


def count_open_positions() -> int:
    """Count current open positions."""
    return sum(1 for s in positions.values() if s.in_position)


async def trading_logic_loop():
    """Main trading loop with proper multi-symbol and dual-leg execution."""
    global positions, live_data

    await check_initial_positions()

    execution_client = ExecutionClient(endpoint="tcp://127.0.0.1:5555")

    stats_counter = 0
    trade_count = 0
    total_pnl = 0.0
    wins = 0

    # Initialize state store
    writer.set_stat("account_equity", ACCOUNT_EQUITY_USD)
    writer.set_stat("max_gross_exposure", MAX_GROSS_EXPOSURE_USD)
    writer.set_stat("total_pnl", 0.0)
    writer.set_stat("trade_count", 0)
    writer.set_stat("gross_exposure", 0.0)

    log(f"Starting multi-symbol trading loop. Monitoring: {MONITORED_SYMBOLS}")
    log(f"Entry threshold: {ENTRY_ANN_FUNDING_THRESHOLD_BTC*100:.0f}% for BTC, {ENTRY_ANN_FUNDING_THRESHOLD_ALT*100:.0f}% for alts")

    while True:
        # ── Fetch data for all symbols every 10 seconds ─────────────────
        await fetch_all_symbols_funding()

        stats_counter += 1
        open_pos = count_open_positions()

        # ── Risk evaluation (aggregate) ────────────────────────────────
        gross_exposure = sum(
            p.qty * live_data[p.symbol].spot_price * 2
            for p in positions.values() if p.in_position
        )
        drawdown_pct = abs(min(0, total_pnl)) / ACCOUNT_EQUITY_USD if ACCOUNT_EQUITY_USD > 0 else 0.0

        risk_state = RiskState(
            gross_exposure_usd=gross_exposure,
            symbol_concentration=1.0 / len(MONITORED_SYMBOLS) if open_pos > 0 else 0.0,
            drawdown_pct=drawdown_pct,
            data_staleness_minutes=0,
            venue_latency_ms=0,
        )
        risk_decision = risk_engine.evaluate(risk_state)

        # Update state store
        writer.set_stat("open_positions", open_pos)
        writer.set_stat("gross_exposure", gross_exposure)
        writer.set_risk("drawdown_pct", str(drawdown_pct))
        writer.set_risk("kill_switch", str(risk_decision.kill_switch))
        writer.set_risk("allow_new_risk", str(risk_decision.allow_new_risk))

        # ── KILL SWITCH ────────────────────────────────────────────────
        if risk_decision.kill_switch:
            log("KILL SWITCH TRIGGERED! Emergency exit all positions.", "ERROR")
            for p in positions.values():
                if p.in_position:
                    await exit_position(execution_client, p, live_data[p.symbol], writer)
                    trade_count += 1
            writer.set_stat("trade_count", trade_count)
            await asyncio.sleep(10)
            continue

        # ── PROCESS EXISTING POSITIONS ─────────────────────────────────
        for p in positions.values():
            if not p.in_position or live_data[p.symbol].spot_price == 0:
                continue

            symbol = p.symbol
            data = live_data[symbol]

            # Check exit conditions
            should_exit = False
            exit_reason = ""

            # Exit 1: Funding dropped below threshold
            if data.ann_funding < EXIT_ANN_FUNDING_THRESHOLD:
                should_exit = True
                exit_reason = "funding_dropped"

            # Exit 2: Basis inversion (perp trading at discount)
            elif data.basis_pct < -0.0003:
                should_exit = True
                exit_reason = "basis_inversion"

            # Exit 3: Basis deviation stop
            elif data.spot_price > 0:
                entry_basis = (p.perp_entry_price - p.entry_price) / p.entry_price
                current_basis = data.basis_pct
                if abs(current_basis - entry_basis) > 0.003:
                    should_exit = True
                    exit_reason = "basis_deviation"

            # Exit 4: HOLD THROUGH FUNDING - Suppress soft exits (funding_dropped)
            # during the post-funding window so the position captures the payment.
            # Safety exits (basis_inversion, basis_deviation) are NEVER suppressed.
            if HOLD_THROUGH_FUNDING and data.funding_just_paid:
                if should_exit and exit_reason == "funding_dropped":
                    log(f"[{symbol}] Funding just paid — suppressing soft exit for {FUNDING_CAPTURE_DELAY_MIN} min to capture yield...")
                    should_exit = False

            if should_exit:
                log(f"[{symbol}] Exit signal ({exit_reason}). Funding: {data.ann_funding:.2%}, Basis: {data.basis_pct:.4%}")
                await exit_position(execution_client, p, data, writer)
                if p.qty > 0:  # Position was closed
                    trade_count += 1
                    # Estimate PnL
                    est_pnl = (data.spot_price - p.entry_price) / p.entry_price * p.qty * p.entry_price
                    total_pnl += est_pnl
                    if est_pnl > 0:
                        wins += 1
                    writer.set_stat("total_pnl", total_pnl)
                    writer.set_stat("trade_count", trade_count)
                    writer.set_stat("win_rate", wins / trade_count if trade_count > 0 else 0.0)

        # ── LOOK FOR NEW ENTRY OPPORTUNITIES ───────────────────────────
        if risk_decision.allow_new_risk and open_pos < MAX_CONCURRENT_POSITIONS:

            # Rank symbols by funding rate
            ranked = rank_symbols_by_funding()

            for symbol, funding_rate in ranked:
                if positions[symbol].in_position:
                    continue
                if count_open_positions() >= MAX_CONCURRENT_POSITIONS:
                    break

                data = live_data[symbol]
                entry_threshold = get_entry_threshold(symbol)

                # Check entry conditions (NO sentiment filter for delta-neutral arb!)
                if (
                    data.ann_funding >= entry_threshold
                    and data.spot_price > 0
                    and data.basis_pct > 0.0003  # Premium required
                ):
                    # Calculate position size
                    position_scale = risk_decision.position_scale
                    notional = min(NOTIONAL_PER_TRADE * position_scale, MAX_NOTIONAL_PER_TRADE)
                    qty = notional / data.spot_price

                    log(f"[{symbol}] ENTRY SIGNAL! Funding: {data.ann_funding:.2%} | Basis: {data.basis_pct:.4%} | Qty: {qty:.5f}")

                    # OPEN DUAL-LEG POSITION: Long spot + Short perp
                    success = await enter_position(execution_client, symbol, qty, data, positions[symbol], writer)

                    if success:
                        trade_count += 1
                        open_pos = count_open_positions()
                        writer.set_stat("trade_count", trade_count)

                        # Log to state store
                        for sym, p in positions.items():
                            if p.in_position:
                                writer.upsert_position(
                                    symbol=sym,
                                    side="LONG_SPOT_SHORT_PERP",
                                    spot_entry=p.entry_price,
                                    perp_entry=p.perp_entry_price,
                                    qty=p.qty,
                                    ann_funding=live_data[sym].ann_funding,
                                    basis_pct=live_data[sym].basis_pct,
                                    spot_live=live_data[sym].spot_price,
                                    perp_live=live_data[sym].perp_price,
                                    status="OPEN",
                                )

        # ── Update stats every cycle ───────────────────────────────────
        if stats_counter >= 10:
            for symbol, data in live_data.items():
                writer.set_stat(f"{symbol}_funding", data.ann_funding)
                writer.set_stat(f"{symbol}_price", data.spot_price)
            stats_counter = 0

        await asyncio.sleep(1)


async def enter_position(
    execution_client: ExecutionClient,
    symbol: str,
    qty: float,
    data: SymbolData,
    position: SymbolData,
    writer: StateWriter
) -> bool:
    """
    Enter delta-neutral position: Long spot + Short perpetual.

    Returns True if both legs filled successfully.
    """
    position_scale = 1.0  # TODO: Could use maker orders first

    # Round quantity to exchange precision (5 decimals for most pairs)
    qty = round(qty, 5)

    # ── LEG 1: Buy Spot (Long) ────────────────────────────────────────
    spot_payload = {
        "symbol": symbol,
        "intent": "BUY_SPOT",  # Long spot
        "quantity": qty,
        "urgency": 0.5,  # Lower urgency = try maker first
        "max_slippage_bps": 5.0,
        "exposure_scale": position_scale,
        "use_maker_first": True,
        "maker_patience_sec": MAKER_ORDER_PATIENCE_SEC,
    }

    # ── LEG 2: Sell Perpetual (Short) ────────────────────────────────
    perp_payload = {
        "symbol": symbol,
        "intent": "SELL_PERP",  # Short perp
        "quantity": qty,
        "urgency": 0.5,
        "max_slippage_bps": 5.0,
        "exposure_scale": position_scale,
        "use_maker_first": True,
        "maker_patience_sec": MAKER_ORDER_PATIENCE_SEC,
    }

    log(f"[{symbol}] Sending dual-leg order: BUY {qty} SPOT + SELL {qty} PERP")

    # Send both legs
    execution_client.send_order_intent(spot_payload)
    execution_client.send_order_intent(perp_payload)

    # Update position state
    position.in_position = True
    position.entry_time = datetime.now(timezone.utc).isoformat()
    position.entry_price = data.spot_price
    position.perp_entry_price = data.perp_price
    position.qty = qty
    position.entry_funding = data.ann_funding

    return True


async def exit_position(
    execution_client: ExecutionClient,
    position: SymbolData,
    data: SymbolData,
    writer: StateWriter
) -> bool:
    """
    Exit delta-neutral position: Sell spot + Buy back perpetual.

    Returns True if position was successfully closed.
    """
    symbol = position.symbol
    qty = position.qty

    if qty <= 0:
        return False

    # ── LEG 1: Sell Spot (Close Long) ───────────────────────────────
    spot_payload = {
        "symbol": symbol,
        "intent": "SELL_SPOT",  # Close spot long
        "quantity": qty,
        "urgency": 0.8,
        "max_slippage_bps": 5.0,
        "exposure_scale": 1.0,
    }

    # ── LEG 2: Buy Perpetual (Close Short) ──────────────────────────
    perp_payload = {
        "symbol": symbol,
        "intent": "BUY_PERP",  # Close perp short
        "quantity": qty,
        "urgency": 0.8,
        "max_slippage_bps": 5.0,
        "exposure_scale": 1.0,
    }

    log(f"[{symbol}] Closing dual-leg position: SELL {qty} SPOT + BUY {qty} PERP")

    # Send both legs
    execution_client.send_order_intent(spot_payload)
    execution_client.send_order_intent(perp_payload)

    # Calculate estimated PnL
    basis_pnl = (data.spot_price - position.entry_price) / position.entry_price
    notional = position.qty * position.entry_price
    est_pnl = basis_pnl * notional

    # Record trade
    trade = Trade(
        symbol=symbol,
        side="LONG_SPOT_SHORT_PERP",
        entry_time=position.entry_time,
        exit_time=datetime.now(timezone.utc).isoformat(),
        entry_price=position.entry_price,
        exit_price=data.spot_price,
        qty=position.qty,
        net_pnl_usd=est_pnl,
    )
    writer.record_trade(trade)
    writer.remove_position(symbol)

    # Reset position state
    position.in_position = False
    position.qty = 0.0
    position.entry_price = 0.0
    position.perp_entry_price = 0.0

    return True


async def main():
    await asyncio.gather(
        watch_params_file(),
        trading_logic_loop(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("Live trader stopped.")
    finally:
        writer.close()
