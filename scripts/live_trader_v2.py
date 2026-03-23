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
import os
from datetime import datetime, timezone

from dotenv import load_dotenv

from bongus.core.config import (
    MONITORED_SYMBOLS,
    CAPITAL_PER_SLOT_USD,
    TARGET_LEVERAGE,
    ROTATION_CONFIRM_TIMEOUT_S,
    EXIT_ANN_FUNDING_THRESHOLD,
)
from bongus.engine.state_store import StateWriter, StateReader
from bongus.ipc.execution import ExecutionClient
from bongus.market_data.depth_tracker import DepthTracker
from bongus.market_data.funding_ranker import FundingRanker
from bongus.market_data.rust_data_subscriber import RustDataSubscriber
from bongus.portfolio.correlation_breaker import CorrelationBreaker
from bongus.portfolio.portfolio_allocator import OpenPosition, PortfolioAllocator

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("live_trader_v2")


class LiveTraderV2:
    def __init__(self) -> None:
        self.depth_tracker = DepthTracker()
        self.funding_ranker = FundingRanker(MONITORED_SYMBOLS)
        self.breaker = CorrelationBreaker()
        self.allocator = PortfolioAllocator(self.depth_tracker, self.funding_ranker)
        self.execution = ExecutionClient(endpoint="tcp://127.0.0.1:5555")
        self.state_writer = StateWriter()
        self.state_reader = StateReader()

        # Pending exit tracking: symbol → asyncio.Event (set when FILLED received from Rust).
        # Note: spec described this as set[str]; dict[str, Event] enables per-symbol await
        # without a global polling loop — deliberate improvement over the spec.
        self._exit_events: dict[str, asyncio.Event] = {}

        # Mark price cache: populated from top-of-book perp bids in depth events.
        # Used by _dispatch_enter to compute base-asset qty from notional.
        self._mark_prices: dict[str, float] = {}

        self.subscriber = RustDataSubscriber(
            on_depth=self._on_depth_update,
            on_order_update=self._on_order_update,
        )

    def _on_depth_update(self, symbol: str, market: str, bids: list, asks: list) -> None:
        """Update depth cache; capture top perp bid as mark price proxy."""
        self.depth_tracker.on_l2depth(symbol, market, bids, asks)
        if market == "perp" and bids:
            # bids is list of [price, qty] — top bid is bids[0]
            self._mark_prices[symbol] = float(bids[0][0])

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
        return positions

    def _dispatch_exit(self, symbol: str, urgency: float = 0.8) -> asyncio.Event:
        """Send EXIT instruction and return an Event that fires when FILLED."""
        event = asyncio.Event()
        self._exit_events[symbol] = event
        self.execution.send_order_intent({
            "symbol": symbol,
            "intent": "EXIT_LONG",
            "quantity": 0.0,      # Rust reads from tracked position
            "urgency": urgency,
            "max_slippage_bps": 20.0 if urgency >= 1.0 else 5.0,
            "exposure_scale": 1.0,
        })
        logger.info("EXIT dispatched for %s (urgency=%.1f)", symbol, urgency)
        return event

    def _dispatch_enter(self, symbol: str, notional_usd: float) -> None:
        """Send ENTER instruction. Skips if no mark price has been received yet."""
        mark_price = self._mark_prices.get(symbol, 0.0)
        if mark_price <= 0.0:
            logger.warning(
                "No mark price for %s yet — skipping ENTER (will retry next cycle)", symbol
            )
            return
        qty = round(notional_usd / mark_price, 5)
        self.execution.send_order_intent({
            "symbol": symbol,
            "intent": "ENTER_LONG",
            "quantity": qty,
            "urgency": 0.8,
            "max_slippage_bps": 5.0,
            "exposure_scale": 1.0,
        })
        logger.info("ENTER dispatched for %s qty=%.5f (notional=$%.0f, price=$%.2f)",
                    symbol, qty, notional_usd, mark_price)

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

    async def _trading_loop(self) -> None:
        while True:
            try:
                open_positions = self._get_open_positions()
                funding_rates = {p.symbol: p.ann_funding for p in open_positions}

                # ── 1. Circuit breaker ───────────────────────────────────────
                breaker_decision = self.breaker.evaluate(funding_rates)

                if breaker_decision.state == "EMERGENCY":
                    logger.warning("CIRCUIT BREAKER: EMERGENCY — exiting all positions")
                    for symbol in breaker_decision.positions_to_exit:
                        if symbol not in self._exit_events:
                            self._dispatch_exit(symbol, urgency=1.0)
                    await asyncio.sleep(1)
                    continue

                if not breaker_decision.allow_new_entries:
                    logger.info("CIRCUIT BREAKER: HALTED — blocking new entries")
                    await asyncio.sleep(1)
                    continue

                # ── 2. Allocation decision ───────────────────────────────────
                decision = self.allocator.decide(open_positions)

                # ── 3. Dispatch exits ────────────────────────────────────────
                target_notional = CAPITAL_PER_SLOT_USD * TARGET_LEVERAGE
                for symbol, reason in decision.exit:
                    if symbol not in self._exit_events:
                        logger.info("Rotation: exiting %s (%s)", symbol, reason)
                        self._dispatch_exit(symbol, urgency=0.8)

                # ── 4. Await exit confirmations, dispatch rotation entries ────
                # Use AllocationDecision.rotation_targets (structured field, not string parsing)
                # Exit-before-enter invariant: ENTER only fires after FILLED confirmed.
                for exited_symbol, rotation_target in decision.rotation_targets.items():
                    confirmed = await self._await_exit_confirmation(exited_symbol)
                    if confirmed:
                        self._dispatch_enter(rotation_target, target_notional)
                    else:
                        logger.warning(
                            "Skipping rotation entry for %s — exit of %s unconfirmed",
                            rotation_target, exited_symbol,
                        )

                # ── 5. Dispatch entries for empty slots ─────────────────────
                for symbol, notional in decision.enter:
                    if symbol not in self._exit_events:
                        self._dispatch_enter(symbol, notional)

            except Exception as exc:
                logger.error("Error in trading loop: %s", exc, exc_info=True)

            await asyncio.sleep(1)

    async def run(self) -> None:
        logger.info("Starting LiveTraderV2 — monitoring %d symbols", len(MONITORED_SYMBOLS))
        await asyncio.gather(
            self.subscriber.run(),
            self.funding_ranker.run_forever(interval_s=60),
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
