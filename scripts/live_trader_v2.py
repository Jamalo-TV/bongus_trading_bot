"""Multi-symbol live trader orchestrator.

Wires together:
  - RustDataSubscriber (depth + fill confirmations from Rust port 9000)
  - FundingRanker (single REST call every 60s)
  - CorrelationBreaker (portfolio-level circuit breaker)
  - PortfolioAllocator (sizing, liquidity filter, rotation)
  - ExecutionClient (ZMQ PUSH to Rust)
  - StateWriter/StateReader (SQLite shared state)

Execution invariant: exits are dispatched first; ENTER for a rotation target
only fires after FILLED confirmation from Rust (or timeout fallback).

The original live_trader.py is preserved as a single-symbol fallback.
"""

import asyncio
import logging
import math
import os
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

from bongus.core.config import (
    MONITORED_SYMBOLS,
    CAPITAL_PER_SLOT_USD,
    TARGET_LEVERAGE,
    ROTATION_CONFIRM_TIMEOUT_S,
    ENTRY_ANN_FUNDING_THRESHOLD,
    EXIT_ANN_FUNDING_THRESHOLD,
    FUNDING_SNAPSHOT_HOURS,
    DYNAMIC_SYMBOL_MODE,
    INVERSE_FUNDING_ENABLED,
    MAX_CONCURRENT_POSITIONS,
)
from bongus.engine.state_store import StateWriter, StateReader
from bongus.ipc.execution import ExecutionClient
from bongus.market_data.bybit_monitor import BybitFundingMonitor
from bongus.market_data.depth_tracker import DepthTracker
from bongus.market_data.funding_predictor import FundingPredictor
from bongus.market_data.funding_ranker import FundingRanker
from bongus.market_data.rust_data_subscriber import RustDataSubscriber
from bongus.portfolio.correlation_breaker import CorrelationBreaker
from bongus.portfolio.portfolio_allocator import OpenPosition, PortfolioAllocator

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("live_trader_v2")


class LiveTraderV2:
    def __init__(self) -> None:
        self._trading_mode = os.getenv("TRADING_MODE", "paper").lower()
        logger.info("TRADING_MODE = %s", self._trading_mode)

        self.depth_tracker = DepthTracker()
        self.funding_ranker = FundingRanker(None if DYNAMIC_SYMBOL_MODE else MONITORED_SYMBOLS)
        self.breaker = CorrelationBreaker()
        self.allocator = PortfolioAllocator(self.depth_tracker, self.funding_ranker)
        self.predictor = FundingPredictor()
        self.bybit_monitor = BybitFundingMonitor()
        self._last_compound_check: float = 0.0
        self._last_xval_check: float = 0.0
        self.execution = ExecutionClient(endpoint="tcp://127.0.0.1:5555")
        self.state_writer = StateWriter()
        self.state_reader = StateReader()

        # Write trading mode to state DB so dashboard can display it
        self.state_writer.set_risk_snapshot({"trading_mode": self._trading_mode})

        # Pending exit tracking: symbol → asyncio.Event (set when FILLED received from Rust).
        # Note: spec described this as set[str]; dict[str, Event] enables per-symbol await
        # without a global polling loop — deliberate improvement over the spec.
        self._exit_events: dict[str, asyncio.Event] = {}

        # Mark price cache: populated from top-of-book perp bids in depth events.
        # Used by _dispatch_enter to compute base-asset qty from notional.
        self._mark_prices: dict[str, float] = {}

        # LOT_SIZE step sizes per symbol fetched from Binance at startup.
        # Keyed by symbol (e.g. "BTCUSDT" → 0.001). Falls back to 1e-5 if absent.
        self._lot_step: dict[str, float] = {}

        # Direction cache: populated from state DB each loop iteration.
        # "long" = long spot + short perp; "short" = short spot + long perp (inverse funding).
        self._position_directions: dict[str, str] = {}

        self.subscriber = RustDataSubscriber(
            on_depth=self._on_depth_update,
            on_order_update=self._on_order_update,
            on_mark_price=self._on_mark_price,
        )

    async def _fetch_lot_step_sizes(self) -> None:
        """Fetch futures LOT_SIZE stepSize for all monitored symbols at startup.

        Rounds quantities to the exchange-mandated step size, preventing -1111
        (invalid quantity precision) order rejections on symbols like DOGEUSDT
        where stepSize=1.0 and PEPEUSDT where stepSize=1000.0.
        """
        try:
            resp = await asyncio.to_thread(
                requests.get,
                "https://fapi.binance.com/fapi/v1/exchangeInfo",
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("Could not fetch exchange info for lot sizes: %s", exc)
            return

        for sym_info in data.get("symbols", []):
            symbol = sym_info.get("symbol", "")
            if not symbol:
                continue
            for f in sym_info.get("filters", []):
                if f.get("filterType") == "LOT_SIZE":
                    try:
                        self._lot_step[symbol] = float(f["stepSize"])
                    except (KeyError, ValueError):
                        pass
                    break

        logger.info("Lot step sizes loaded for %d symbols: %s", len(self._lot_step), self._lot_step)

    def _round_to_step(self, qty: float, step: float) -> float:
        """Round qty down to the nearest valid lot step size.

        Uses log10 to derive the correct number of decimal places:
          step=0.001 → 3 dp, step=1.0 → 0 dp, step=1000.0 → 0 dp.
        """
        if step <= 0:
            return qty
        rounded = (qty // step) * step
        decimals = max(0, -int(math.floor(math.log10(step))))
        return round(rounded, decimals)

    def _minutes_since_last_snapshot(self) -> float:
        """Return minutes elapsed since the most recent funding snapshot (0/8/16 UTC)."""
        now = datetime.now(timezone.utc)
        current_minutes = now.hour * 60 + now.minute
        snapshot_minutes = sorted(h * 60 for h in FUNDING_SNAPSHOT_HOURS)
        # Find the most recent snapshot that has already passed today
        elapsed = None
        for snap in reversed(snapshot_minutes):
            if current_minutes >= snap:
                elapsed = current_minutes - snap
                break
        if elapsed is None:
            # Past midnight but before first snapshot: measure from last snapshot of previous day
            elapsed = current_minutes + (24 * 60 - snapshot_minutes[-1])
        return float(elapsed)

    def _on_depth_update(self, symbol: str, market: str, bids: list, asks: list) -> None:
        """Update depth cache; capture top perp bid as mark price proxy."""
        self.depth_tracker.on_l2depth(symbol, market, bids, asks)
        if market == "perp" and bids:
            # bids is list of [price, qty] — top bid is bids[0]
            self._mark_prices[symbol] = float(bids[0][0])

    def _on_mark_price(self, symbol: str, mark_price: float, next_funding_rate: float) -> None:
        """Update FundingRanker with live WS funding rate (~1s cadence).

        This provides sub-minute rate resolution compared to the 60s REST fallback,
        enabling the post-snapshot decay exit and rotation logic to react immediately
        when funding collapses at settlement rather than waiting for the next REST poll.
        """
        self.funding_ranker.update_rate(symbol, next_funding_rate)
        self.predictor.push_sample(symbol, next_funding_rate * 1095)
        # Also keep mark price cache fresh for ENTER quantity calculations.
        if mark_price > 0.0:
            self._mark_prices[symbol] = mark_price

    def _on_order_update(self, symbol: str, status: str, **_kwargs) -> None:
        if status == "FILLED" and symbol in self._exit_events:
            logger.info("Exit FILLED confirmed for %s — releasing capital slot", symbol)
            self._exit_events[symbol].set()

    def _get_open_positions(self) -> list[OpenPosition]:
        rows = self.state_reader.get_positions()
        positions = []
        for r in rows:
            spot_price = r.get("spot_live", 0.0)
            # If spot_live is populated (price > $1), use actual qty × price.
            # Otherwise fall back to configured slot size (e.g., cold start with stale cache).
            if spot_price > 1.0:
                notional_usd = r["qty"] * spot_price
            else:
                notional_usd = CAPITAL_PER_SLOT_USD * TARGET_LEVERAGE
            positions.append(OpenPosition(
                symbol=r["symbol"],
                notional_usd=notional_usd,
                ann_funding=self.funding_ranker.get_rate(r["symbol"]),
            ))
            # Cache direction for use by exit dispatches
            self._position_directions[r["symbol"]] = r.get("direction", "long")
        return positions

    def _dispatch_exit(self, symbol: str, urgency: float = 0.8, direction: str = "long") -> asyncio.Event:
        """Send EXIT instruction and return an Event that fires when FILLED.

        If the ZMQ send fails (Rust engine down), the event is registered but
        will never be set — callers rely on ROTATION_CONFIRM_TIMEOUT_S to unblock.
        The CRITICAL log from ExecutionClient is the alert signal.
        """
        event = asyncio.Event()
        self._exit_events[symbol] = event
        intent = "EXIT_SHORT" if direction == "short" else "EXIT_LONG"
        sent = self.execution.send_order_intent({
            "symbol": symbol,
            "intent": intent,
            "quantity": 0.0,      # Rust reads from tracked position
            "urgency": urgency,
            "max_slippage_bps": 20.0 if urgency >= 1.0 else 5.0,
            "exposure_scale": 1.0,
        })
        if sent:
            logger.info("EXIT dispatched for %s (urgency=%.1f, direction=%s)", symbol, urgency, direction)
        else:
            logger.critical("EXIT for %s NOT sent — ZMQ down. Position unhedged!", symbol)
        return event

    def _dispatch_enter(self, symbol: str, notional_usd: float, direction: str = "long") -> None:
        """Send ENTER instruction. Skips if no mark price has been received yet."""
        mark_price = self._mark_prices.get(symbol, 0.0)
        if mark_price <= 0.0:
            logger.warning(
                "No mark price for %s yet — skipping ENTER (will retry next cycle)", symbol
            )
            return
        raw_qty = notional_usd / mark_price
        step = self._lot_step.get(symbol, 1e-5)
        qty = self._round_to_step(raw_qty, step)
        intent = "ENTER_SHORT" if direction == "short" else "ENTER_LONG"
        sent = self.execution.send_order_intent({
            "symbol": symbol,
            "intent": intent,
            "quantity": qty,
            "urgency": 0.8,
            "max_slippage_bps": 5.0,
            "exposure_scale": 1.0,
        })
        if sent:
            logger.info("ENTER dispatched for %s qty=%.5f (notional=$%.0f, price=$%.2f, direction=%s)",
                        symbol, qty, notional_usd, mark_price, direction)
        else:
            logger.critical("ENTER for %s NOT sent — ZMQ down.", symbol)

    async def _await_exit_confirmation(self, symbol: str) -> bool:
        """Wait for FILLED event. Returns True if confirmed, False on timeout."""
        event = self._exit_events.get(symbol)
        if event is None:
            return False
        try:
            await asyncio.wait_for(event.wait(), timeout=ROTATION_CONFIRM_TIMEOUT_S)
            return True
        except asyncio.TimeoutError:
            logger.warning("Exit confirmation timeout for %s — entry will be deferred", symbol)
            return False
        finally:
            self._exit_events.pop(symbol, None)

    async def _maybe_recompound(self) -> None:
        import time
        if time.time() - self._last_compound_check < 86400:
            return
        self._last_compound_check = time.time()
        equity = self.state_reader.get_account_equity()
        if equity and equity > 0:
            new_capital = equity / MAX_CONCURRENT_POSITIONS
            self.allocator = PortfolioAllocator(
                self.depth_tracker, self.funding_ranker, capital_per_slot_usd=new_capital
            )
            logger.info("Auto-compounding: equity=%.2f, new capital_per_slot=%.2f", equity, new_capital)

    async def _trading_loop(self) -> None:
        while True:
            try:
                open_positions = self._get_open_positions()
                funding_rates = {p.symbol: p.ann_funding for p in open_positions}

                # ── 0. Post-snapshot funding decay exit ──────────────────────
                # Within 5 minutes after a funding snapshot, funding rates that
                # have decayed below the exit threshold are acted on immediately
                # rather than waiting for the next allocator cycle.
                minutes_since_snap = self._minutes_since_last_snapshot()
                if minutes_since_snap <= 5 and open_positions:
                    for pos in open_positions:
                        if (
                            pos.ann_funding < EXIT_ANN_FUNDING_THRESHOLD
                            and pos.symbol not in self._exit_events
                        ):
                            logger.info(
                                "Post-snapshot decay: %s funding=%.1f%% < exit threshold — exiting",
                                pos.symbol, pos.ann_funding * 100,
                            )
                            self._dispatch_exit(
                                pos.symbol,
                                urgency=1.0,
                                direction=self._position_directions.get(pos.symbol, "long"),
                            )

                # ── 1. Circuit breaker ───────────────────────────────────────
                liquidity_map = {
                    p.symbol: self.depth_tracker.get_exit_depth(p.symbol)
                    for p in open_positions
                }
                breaker_decision = self.breaker.evaluate(funding_rates, liquidity_map=liquidity_map)

                if breaker_decision.state == "EMERGENCY":
                    logger.warning("CIRCUIT BREAKER: EMERGENCY — exiting all positions")
                    for symbol in breaker_decision.positions_to_exit:
                        if symbol not in self._exit_events:
                            self._dispatch_exit(
                                symbol,
                                urgency=1.0,
                                direction=self._position_directions.get(symbol, "long"),
                            )
                    await asyncio.sleep(1)
                    continue

                if not breaker_decision.allow_new_entries:
                    logger.info("CIRCUIT BREAKER: HALTED — blocking new entries")
                    await asyncio.sleep(1)
                    continue

                # ── 2. Allocation decision ───────────────────────────────────
                await self._maybe_recompound()
                import time as _time
                bybit_rates = self.bybit_monitor.get_rates()
                now = _time.monotonic()
                if bybit_rates and now - self._last_xval_check >= 60:
                    self._last_xval_check = now
                    for sym, bybit_rate in bybit_rates.items():
                        if not self.funding_ranker.has_symbol(sym):
                            continue
                        ranker_rate = self.funding_ranker.get_rate(sym)
                        if abs(bybit_rate - ranker_rate) > 0.01:
                            logger.warning(
                                "Cross-validation mismatch for %s: ranker=%.4f bybit=%.4f",
                                sym, ranker_rate, bybit_rate,
                            )
                decision = self.allocator.decide(open_positions)

                # ── 3. Dispatch exits ────────────────────────────────────────
                target_notional = CAPITAL_PER_SLOT_USD * TARGET_LEVERAGE
                for symbol, reason in decision.exit:
                    if symbol not in self._exit_events:
                        logger.info("Rotation: exiting %s (%s)", symbol, reason)
                        self._dispatch_exit(
                            symbol,
                            urgency=0.8,
                            direction=self._position_directions.get(symbol, "long"),
                        )

                # ── 4. Await exit confirmations, dispatch rotation entries ────
                # All rotation exits are awaited concurrently so a single slow fill
                # doesn't hold up others or block the circuit breaker for N×timeout.
                if decision.rotation_targets:
                    confirm_tasks = {
                        exited_symbol: asyncio.ensure_future(
                            self._await_exit_confirmation(exited_symbol)
                        )
                        for exited_symbol in decision.rotation_targets
                    }
                    results = await asyncio.gather(*confirm_tasks.values(), return_exceptions=True)
                    for (exited_symbol, rotation_target), confirmed in zip(
                        decision.rotation_targets.items(), results
                    ):
                        if confirmed is True:
                            rot_funding = self.funding_ranker.get_rate(rotation_target) or 0.0
                            rot_direction = (
                                "short"
                                if INVERSE_FUNDING_ENABLED and rot_funding < -ENTRY_ANN_FUNDING_THRESHOLD
                                else "long"
                            )
                            self._dispatch_enter(rotation_target, target_notional, direction=rot_direction)
                        else:
                            logger.warning(
                                "Skipping rotation entry for %s — exit of %s unconfirmed",
                                rotation_target, exited_symbol,
                            )

                # ── 5. Dispatch entries for empty slots ─────────────────────
                for symbol, notional in decision.enter:
                    if symbol not in self._exit_events:
                        ann_funding = self.funding_ranker.get_rate(symbol) or 0.0
                        if (
                            INVERSE_FUNDING_ENABLED
                            and ann_funding < -ENTRY_ANN_FUNDING_THRESHOLD
                        ):
                            self._dispatch_enter(symbol, notional, direction="short")
                        else:
                            self._dispatch_enter(symbol, notional, direction="long")

            except Exception as exc:
                logger.error("Error in trading loop: %s", exc, exc_info=True)

            await asyncio.sleep(1)

    async def run(self) -> None:
        logger.info("Starting LiveTraderV2 — monitoring %d symbols", len(MONITORED_SYMBOLS))
        await self._fetch_lot_step_sizes()
        # Prime both rate caches before the trading loop starts so cross-validation
        # comparisons don't see stale 0.0 ranker values on the very first iteration.
        await asyncio.gather(
            self.funding_ranker.refresh(),
            self.bybit_monitor.refresh(),
        )
        await asyncio.gather(
            self.subscriber.run(),
            self.funding_ranker.run_forever(interval_s=60),
            self.bybit_monitor.run_forever(),
            self._trading_loop(),
        )


async def main() -> None:
    trader = LiveTraderV2()
    await trader.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("LiveTraderV2 stopped.")
