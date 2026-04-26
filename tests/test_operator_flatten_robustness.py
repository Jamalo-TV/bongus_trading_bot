
import asyncio
import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

from scripts.live_trader_v2 import LiveTraderV2

class TestFlattenFix(unittest.TestCase):
    def setUp(self):
        self.mock_config = MagicMock()
        self.mock_config.get.return_value = "test"
        with patch('scripts.live_trader_v2.ConfigManager'), \
             patch('scripts.live_trader_v2.StateWriter'), \
             patch('scripts.live_trader_v2.StateReader'), \
             patch('scripts.live_trader_v2.ExecutionClient'), \
             patch('scripts.live_trader_v2.RustDataSubscriber'), \
             patch('scripts.live_trader_v2.FundingRanker'), \
             patch('scripts.live_trader_v2.CooldownManager'), \
             patch('scripts.live_trader_v2.DepthTracker'), \
             patch('scripts.live_trader_v2.RegimeFilter'):
            self.trader = LiveTraderV2(self.mock_config)
            self.trader._last_operator_flatten_request_id = ""
        
        # Patch asyncio.ensure_future to avoid loop errors in unittest
        self.ensure_future_patcher = patch('asyncio.ensure_future')
        self.mock_ensure_future = self.ensure_future_patcher.start()

    def tearDown(self):
        self.ensure_future_patcher.stop()

    def test_on_order_rejected_pops_exit_events(self):
        symbol = "BTCUSDT"
        event = asyncio.Event()
        self.trader._exit_events[symbol] = event
        self.trader._pending_exit_intents[symbol] = "intent-123"
        
        # Call _on_order_rejected
        self.trader._on_order_rejected(symbol, "EXIT_LONG", "intent-123", "min_notional")
        
        # Verify event was popped
        self.assertNotIn(symbol, self.trader._exit_events)

    def test_maybe_process_operator_flatten_all_request_dust_handling(self):
        # Mock position rows with one dust position
        position_rows = [
            {
                "symbol": "DUSTUSDT",
                "qty": 0.1,
                "direction": "long",
                "spot_entry": 1.0,
                "perp_entry": 1.0
            },
            {
                "symbol": "REALUSDT",
                "qty": 100.0,
                "direction": "long",
                "spot_entry": 1.0,
                "perp_entry": 1.0
            }
        ]
        
        # Mock mark prices so DUSTUSDT is < $1.0 and REALUSDT is > $1.0
        def side_effect_mark_prices(symbol, row):
            if symbol == "DUSTUSDT":
                return 1.0, 1.0 # $0.1 notional
            return 10.0, 10.0 # $1000 notional
            
        self.trader._leg_mark_prices = MagicMock(side_effect=side_effect_mark_prices)
        self.trader.state_reader.get_risk.return_value = {
            "operator_flatten_all_request_id": "req-1",
            "operator_flatten_all_status": "requested"
        }
        self.trader.state_reader.get_positions.return_value = position_rows
        self.trader.cooldowns.allow_symbol.return_value = (True, "")
        
        # Mock execution.send_order_intent to return True
        self.trader.execution.send_order_intent.return_value = True
        
        # Call _maybe_process_operator_flatten_all_request
        self.trader._maybe_process_operator_flatten_all_request(position_rows)
        
        # Verify REALUSDT was dispatched but DUSTUSDT was skipped
        self.trader.execution.send_order_intent.assert_called_once()
        args = self.trader.execution.send_order_intent.call_args[0][0]
        self.assertEqual(args["symbol"], "REALUSDT")
        
        # Check snapshot - remaining should ONLY have REALUSDT
        risk_snap = self.trader.state_writer.set_risk_snapshot.call_args[0][0]
        self.assertEqual(risk_snap["operator_flatten_all_remaining_symbols"], ["REALUSDT"])

    def test_maybe_process_operator_flatten_all_request_stuck_limit(self):
        symbol = "STUCKUSDT"
        position_rows = [{
            "symbol": symbol,
            "qty": 100.0,
            "direction": "long"
        }]
        self.trader.state_reader.get_positions.return_value = position_rows
        self.trader.state_reader.get_risk.return_value = {
            "operator_flatten_all_request_id": "req-1",
            "operator_flatten_all_status": "requested"
        }
        self.trader._last_operator_flatten_request_id = "req-1"
        self.trader.cooldowns.allow_symbol.return_value = (True, "")
        self.trader._leg_mark_prices = MagicMock(return_value=(10.0, 10.0))
        
        # Simulate 9 attempts
        self.trader._operator_flatten_attempts[symbol] = 9
        
        # 10th call - should still dispatch
        self.trader.execution.send_order_intent.return_value = True
        self.trader._maybe_process_operator_flatten_all_request(position_rows)
        self.assertEqual(self.trader._operator_flatten_attempts[symbol], 10)
        self.trader.execution.send_order_intent.assert_called()
        
        # Simulate a rejection to clear _exit_events
        self.trader._on_order_rejected(symbol, "EXIT_LONG", None, "some failure")
        self.assertNotIn(symbol, self.trader._exit_events)
        
        # 11th call - should NOT dispatch and mark as stuck
        self.trader.execution.send_order_intent.reset_mock()
        self.trader._operator_flatten_cycle_count = 10 
        self.trader._maybe_process_operator_flatten_all_request(position_rows)
        self.trader.execution.send_order_intent.assert_not_called()
        
        # Check risk snapshot - status should be partial_failed because the only symbol is stuck
        risk_snap = self.trader.state_writer.set_risk_snapshot.call_args[0][0]
        self.assertEqual(risk_snap["operator_flatten_all_status"], "partial_failed")
        self.assertIn(symbol, risk_snap["operator_flatten_all_remaining_symbols"])

if __name__ == "__main__":
    unittest.main()
