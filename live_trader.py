import asyncio
import json
import time
import requests
import zmq
import os
import msgpack
from datetime import datetime, timezone
from dotenv import load_dotenv

from config import (
    EXIT_ANN_FUNDING_THRESHOLD,
    ENTRY_ANN_FUNDING_THRESHOLD,
    NOTIONAL_PER_TRADE,
    MAX_NOTIONAL_PER_TRADE,
    MAX_GROSS_EXPOSURE_USD,
    ACCOUNT_EQUITY_USD,
)
from risk_engine import RiskEngine, RiskLimits, RiskState
from state_store import StateWriter

load_dotenv()

writer = StateWriter()
risk_engine = RiskEngine()


class LiveData:
    def __init__(self):
        self.ann_funding = 0.0
        self.sentiment_score = 0.0
        self.spot_price = 0.0
        self.perp_price = 0.0
        self.basis_pct = 0.0


# Global state
in_position = False
position_entry_time: str = ""
position_entry_price: float = 0.0
position_qty: float = 0.0
live_data = LiveData()

# Dynamic entry threshold (updated from optimal_params.json)
dynamic_entry = ENTRY_ANN_FUNDING_THRESHOLD


async def watch_params_file():
    """Constantly checks optimal_params.json for updates."""
    while True:
        try:
            if os.path.exists("optimal_params.json"):
                with open("optimal_params.json", "r") as f:
                    params = json.load(f)
                    global dynamic_entry
                    dynamic_entry = params.get("ENTRY_ANN_FUNDING_THRESHOLD", dynamic_entry)
        except Exception:
            pass
        await asyncio.sleep(60)


async def watch_sentiment_file():
    """Continuously reads current_sentiment.json."""
    while True:
        try:
            if os.path.exists("current_sentiment.json"):
                with open("current_sentiment.json", "r") as f:
                    data = json.load(f)
                    live_data.sentiment_score = data.get("sentiment_score", 0.0)
        except Exception:
            pass
        await asyncio.sleep(60)


async def check_initial_position():
    """
    Check Binance REST API if we are currently in position before entering the trading loop.
    Refuse to start if we cannot verify.
    """
    global in_position
    print("Checking initial position with Binance API...")

    try:
        api_key = os.getenv("BINANCE_API_KEY", "")
        use_testnet = os.getenv("USE_TESTNET", "true").lower() == "true"

        if api_key and not use_testnet:
            import hmac
            import hashlib
            timestamp = int(time.time() * 1000)
            query = f"timestamp={timestamp}"
            signature = hmac.new(
                os.getenv("BINANCE_API_SECRET", "").encode(),
                query.encode(), hashlib.sha256
            ).hexdigest()
            headers = {"X-MBX-APIKEY": api_key}
            resp = requests.get(
                f"https://fapi.binance.com/fapi/v2/positionRisk?{query}&signature={signature}",
                headers=headers, timeout=10
            )
            data = resp.json()
            for pos in data:
                if pos.get("symbol") == "BTCUSDT":
                    if float(pos.get("positionAmt", 0)) != 0.0:
                        in_position = True
                        print(f"Found existing BTCUSDT position: {pos['positionAmt']}")
                        break
        else:
            in_position = False

        print(f"Startup verification successful. in_position = {in_position}")
    except Exception as e:
        print(f"CRITICAL ERROR: Cannot reach Binance to verify position on startup. {e}")
        raise SystemExit("Refusing to start to prevent double-entries.")


async def trading_logic_loop():
    """Main fast trading loop with risk engine integration."""
    global in_position, position_entry_time, position_entry_price, position_qty

    await check_initial_position()

    context = zmq.Context()
    socket = context.socket(zmq.PUSH)
    socket.connect("tcp://127.0.0.1:5555")

    fetch_counter = 0
    stats_counter = 0
    trade_count = 0
    total_pnl = 0.0
    wins = 0

    # Initialize stats in state store
    writer.set_stat("account_equity", ACCOUNT_EQUITY_USD)
    writer.set_stat("max_gross_exposure", MAX_GROSS_EXPOSURE_USD)
    writer.set_stat("total_pnl", 0.0)
    writer.set_stat("trade_count", 0)
    writer.set_stat("gross_exposure", 0.0)

    print("Starting trading logic loop...")
    while True:
        fetch_counter += 1
        stats_counter += 1

        # ── Fetch real live data every 10 seconds ───────────────────────
        if fetch_counter >= 10:
            try:
                url = "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT"
                resp = await asyncio.to_thread(requests.get, url, timeout=5)
                data = resp.json()
                raw_8h_rate = float(data.get("lastFundingRate", 0.0))
                live_data.ann_funding = raw_8h_rate * 3 * 365
                live_data.spot_price = float(data.get("indexPrice", 0.0))
                live_data.perp_price = float(data.get("markPrice", 0.0))
                if live_data.spot_price > 0:
                    live_data.basis_pct = (live_data.perp_price - live_data.spot_price) / live_data.spot_price
            except Exception as e:
                print(f"[Warning] Could not fetch real funding rate: {e}")

            fetch_counter = 0
            print(f"[Loop] Funding: {live_data.ann_funding:.2%} | Spot: ${live_data.spot_price:.2f} | Basis: {live_data.basis_pct:.4%}")

        # ── Update state store every 10 seconds ─────────────────────────
        if stats_counter >= 10:
            writer.set_stat("ann_funding", live_data.ann_funding)
            writer.set_stat("sentiment_score", live_data.sentiment_score)
            writer.set_stat("spot_price", live_data.spot_price)
            writer.set_stat("perp_price", live_data.perp_price)

            if in_position and live_data.spot_price > 0:
                # Update live position prices
                writer.upsert_position(
                    symbol="BTCUSDT",
                    side="LONG_SPOT_SHORT_PERP",
                    spot_entry=position_entry_price,
                    perp_entry=position_entry_price,  # Simplified
                    qty=position_qty,
                    ann_funding=live_data.ann_funding,
                    basis_pct=live_data.basis_pct,
                    spot_live=live_data.spot_price,
                    perp_live=live_data.perp_price,
                    status="OPEN",
                )

            stats_counter = 0

        # ── Risk evaluation ─────────────────────────────────────────────
        gross_exposure = position_qty * live_data.spot_price * 2 if in_position else 0.0
        drawdown_pct = abs(min(0, total_pnl)) / ACCOUNT_EQUITY_USD if ACCOUNT_EQUITY_USD > 0 else 0.0

        risk_state = RiskState(
            gross_exposure_usd=gross_exposure,
            symbol_concentration=1.0 if in_position else 0.0,
            drawdown_pct=drawdown_pct,
            data_staleness_minutes=0,
            venue_latency_ms=0,
        )
        risk_decision = risk_engine.evaluate(risk_state)

        # Update risk in state store
        writer.set_risk("drawdown_pct", str(drawdown_pct))
        writer.set_risk("kill_switch", str(risk_decision.kill_switch))
        writer.set_risk("allow_new_risk", str(risk_decision.allow_new_risk))
        if risk_decision.reasons:
            writer.set_risk("reasons", json.dumps(risk_decision.reasons))
        else:
            writer.set_risk("reasons", "[]")
        writer.set_stat("gross_exposure", gross_exposure)

        # ── Kill switch ─────────────────────────────────────────────────
        if risk_decision.kill_switch and in_position:
            print("KILL SWITCH TRIGGERED! Emergency exit.")
            payload = {
                "symbol": "BTCUSDT",
                "intent": "EXIT_LONG",
                "quantity": position_qty,
                "urgency": 1.0,
                "max_slippage_bps": 20.0,
                "exposure_scale": 1.0,
            }
            socket.send(msgpack.packb(payload))
            writer.record_trade(
                symbol="BTCUSDT", side="LONG_SPOT_SHORT_PERP",
                entry_time=position_entry_time,
                exit_time=datetime.now(timezone.utc).isoformat(),
                entry_price=position_entry_price, exit_price=live_data.spot_price,
                qty=position_qty, net_pnl_usd=0.0,
            )
            writer.remove_position("BTCUSDT")
            in_position = False
            trade_count += 1
            writer.set_stat("trade_count", trade_count)
            continue

        # ── Position sizing with risk scale ─────────────────────────────
        position_scale = risk_decision.position_scale
        notional = min(NOTIONAL_PER_TRADE * position_scale, MAX_NOTIONAL_PER_TRADE)
        qty = notional / live_data.spot_price if live_data.spot_price > 0 else 0.01

        # ── Entry signal ────────────────────────────────────────────────
        if (
            live_data.ann_funding >= dynamic_entry
            and not in_position
            and live_data.sentiment_score > -0.5
            and risk_decision.allow_new_risk
            and live_data.spot_price > 0
        ):
            print(f"Trade Signal! Entering with qty={qty:.5f} (notional=${notional:.0f}, scale={position_scale:.2f})")
            payload = {
                "symbol": "BTCUSDT",
                "intent": "ENTER_LONG",
                "quantity": round(qty, 5),
                "urgency": 0.8,
                "max_slippage_bps": 5.0,
                "exposure_scale": position_scale,
            }
            socket.send(msgpack.packb(payload))
            in_position = True
            position_entry_time = datetime.now(timezone.utc).isoformat()
            position_entry_price = live_data.spot_price
            position_qty = round(qty, 5)

            writer.upsert_position(
                symbol="BTCUSDT",
                side="LONG_SPOT_SHORT_PERP",
                spot_entry=live_data.spot_price,
                perp_entry=live_data.perp_price,
                qty=position_qty,
                ann_funding=live_data.ann_funding,
                basis_pct=live_data.basis_pct,
                spot_live=live_data.spot_price,
                perp_live=live_data.perp_price,
                status="OPEN",
            )
            writer.set_stat("gross_exposure", position_qty * live_data.spot_price * 2)

        # ── Exit signal ─────────────────────────────────────────────────
        elif in_position and live_data.ann_funding < EXIT_ANN_FUNDING_THRESHOLD:
            print("Exit Signal! Funding dropped below threshold. Closing position.")
            payload = {
                "symbol": "BTCUSDT",
                "intent": "EXIT_LONG",
                "quantity": position_qty,
                "urgency": 0.8,
                "max_slippage_bps": 5.0,
                "exposure_scale": 1.0,
            }
            socket.send(msgpack.packb(payload))

            # Estimate PnL (simplified: basis convergence)
            est_pnl = (live_data.spot_price - position_entry_price) / position_entry_price * notional
            total_pnl += est_pnl
            trade_count += 1
            if est_pnl > 0:
                wins += 1

            writer.record_trade(
                symbol="BTCUSDT", side="LONG_SPOT_SHORT_PERP",
                entry_time=position_entry_time,
                exit_time=datetime.now(timezone.utc).isoformat(),
                entry_price=position_entry_price, exit_price=live_data.spot_price,
                qty=position_qty, net_pnl_usd=est_pnl,
            )
            writer.remove_position("BTCUSDT")
            writer.set_stat("total_pnl", total_pnl)
            writer.set_stat("trade_count", trade_count)
            writer.set_stat("win_rate", wins / trade_count if trade_count > 0 else 0.0)
            in_position = False

        await asyncio.sleep(1)


async def main():
    await asyncio.gather(
        watch_params_file(),
        watch_sentiment_file(),
        trading_logic_loop(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Live trader stopped.")
    finally:
        writer.close()
