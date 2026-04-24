from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import scripts.live_trader_v2
from bongus.engine.state_store import StateReader, StateWriter
from scripts.live_trader_v2 import LiveTraderV2
from bongus.monitoring.performance_metrics import calculate_metrics

class TestFixPlan2026_04_23(IsolatedAsyncioTestCase):
    def _build_trader(self, db_path: str) -> LiveTraderV2:
        config_path = db_path + ".config.json"
        with patch("scripts.live_trader_v2.ConfigManager.start_watching", autospec=True), \
             patch("bongus.engine.state_store.DB_PATH", db_path):
            trader = LiveTraderV2()
        trader._config = scripts.live_trader_v2.ConfigManager(
            config_path=config_path,
            on_validation_error=trader._on_config_validation_error,
            on_reload=trader._on_config_reloaded,
        )
        trader._last_operator_flatten_request_id = ""
        trader.state_writer.close()
        trader.state_reader.close()
        trader.state_writer = StateWriter(db_path=db_path)
        trader.state_reader = StateReader(db_path=db_path)
        return trader

    async def test_stale_intent_expiration_activates_cooldown(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                symbol = "BTCUSDT"
                now = datetime.now(timezone.utc)
                # Set entry_time far in the past to trigger timeout
                trader._pending_enters[symbol] = {
                    "intent_id": "test_intent",
                    "entry_time": (now - timedelta(seconds=600)).isoformat(),
                    "qty": 1.0,
                    "entry_price": 100.0,
                }
                
                trader._expire_stale_pending_intents()
                
                self.assertNotIn(symbol, trader._pending_enters)
                self.assertIn(symbol, trader._stale_pending_enters)
                
                # Verify cooldown is active
                allowed, reason = trader.cooldowns.allow_symbol(symbol)
                self.assertFalse(allowed)
                self.assertEqual(reason, "stale_pending_intent")
                
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    async def test_stale_intent_auto_clear_activates_cooldown(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            # We need to mock _fetch_exchange_startup_snapshot
            with patch.object(LiveTraderV2, "_fetch_exchange_startup_snapshot", return_value={
                "position_risk": [],
                "futures_account": {"assets": [], "positions": []},
                "spot_account": {"balances": []},
                "futures_open_orders": [],
                "spot_open_orders": [],
            }):
                trader = self._build_trader(db_name)
                try:
                    symbol = "BTCUSDT"
                    now = datetime.now(timezone.utc)
                    # Put it directly into stale
                    trader._stale_pending_enters[symbol] = {
                        "intent_id": "test_intent",
                        "timed_out_at": (now - timedelta(seconds=600)).isoformat(),
                    }
                    
                    # Need to mock trading_mode to "live" to trigger _live_self_heal_stale_pending_intents
                    trader._trading_mode = "live"
                    
                    await trader._live_self_heal_stale_pending_intents(now)
                    
                    self.assertNotIn(symbol, trader._stale_pending_enters)
                    
                    # Verify cooldown is active
                    allowed, reason = trader.cooldowns.allow_symbol(symbol)
                    self.assertFalse(allowed)
                    self.assertEqual(reason, "stale_pending_intent_no_activity")
                    
                finally:
                    trader.execution.close()
                    trader.state_reader.close()
                    trader.state_writer.close()
                    if os.path.exists(db_name):
                        os.remove(db_name)

    async def test_drawdown_consistency_performance_metrics(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                # Set a high-watermark in risk state
                hwm = 15000.0
                account_equity = 10000.0
                trader.state_writer.set_risk_snapshot({
                    "account_equity_high_watermark": hwm,
                    "account_equity": account_equity
                })
                trader.state_writer.flush()
                
                # Calculate metrics
                metrics = calculate_metrics(trader.state_reader)
                
                # Expected drawdown: (15000 - 10000) / 15000 = 5000 / 15000 = 1/3 = 0.333...
                self.assertAlmostEqual(metrics["max_drawdown_pct"], (hwm - account_equity) / hwm)
                
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)
