import time
import pytest
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock
from dataclasses import dataclass
from datetime import datetime, timezone

from bongus.portfolio.portfolio_allocator import PortfolioAllocator, OpenPosition
from bongus.monitoring.telegram_alerter import poll_state_alerts
import bongus.monitoring.telegram_alerter as telegram_alerter
from bongus.supervisor.core import collect_snapshot, SupervisorSnapshot

# Helper to reset alerter state
def reset_alerter_state():
    telegram_alerter.prev_symbols = set()
    telegram_alerter.prev_trade_count = 0
    telegram_alerter.prev_kill_switch = False
    telegram_alerter.prev_runtime_mode = ""
    telegram_alerter.prev_preflight_status = ""
    telegram_alerter.prev_safe_mode_reason = ""
    telegram_alerter.prev_config_error = ""
    telegram_alerter.prev_heartbeat_status = ""
    telegram_alerter.last_daily_summary_date = ""
    telegram_alerter._candidate_runtime_mode = ""
    telegram_alerter._candidate_runtime_mode_first_seen = 0.0
    telegram_alerter._last_runtime_mode_alerted_at = 0.0
    telegram_alerter._candidate_safe_mode_reason = ""
    telegram_alerter._candidate_safe_mode_reason_first_seen = 0.0
    telegram_alerter._hb_candidate = ""
    telegram_alerter._hb_candidate_count = 0
    telegram_alerter._candidate_kill_switch = False
    telegram_alerter._candidate_kill_switch_first_seen = 0.0
    telegram_alerter._settling_runtime_mode = ""
    telegram_alerter._settling_runtime_mode_dirty = False
    telegram_alerter._settling_safe_mode_reason = ""
    telegram_alerter._settling_safe_mode_reason_dirty = False
    telegram_alerter._settling_kill_switch_notified = False
    telegram_alerter._was_settling = False

# --- Fix A: Over-Slot Orphans ---

def test_manual_review_orphan_consumes_slot():
    class MockDepth:
        def get_entry_depth(self, symbol): return 1_000_000.0
    class MockFunding:
        def get_ranked(self): return [("BTCUSDT", 0.5), ("ETHUSDT", 0.4)]
    
    allocator = PortfolioAllocator(MockDepth(), MockFunding())
    
    # 3 orphans
    open_positions = [
        OpenPosition("SOLUSDT", 2500.0, 0.1, recovery_state="manual_review"),
        OpenPosition("ADAUSDT", 2500.0, 0.1, recovery_state="manual_review"),
        OpenPosition("DOTUSDT", 2500.0, 0.1, recovery_state="manual_review"),
    ]
    
    # MAX_CONCURRENT_POSITIONS is 4 by default
    decision = allocator.decide(open_positions)
    
    assert len(decision.enter) == 1
    assert decision.enter[0][0] == "BTCUSDT"

def test_orphan_not_rotated_out():
    class MockDepth:
        def get_entry_depth(self, symbol): return 1_000_000.0
    class MockFunding:
        def get_ranked(self): return [("BTCUSDT", 0.8)]
    
    allocator = PortfolioAllocator(MockDepth(), MockFunding())
    
    # Orphan is weak (0.01) but strong enough to stay (no managed rotation)
    # Managed positions are strong (0.79)
    open_positions = [
        OpenPosition("SOLUSDT", 2500.0, 0.01, recovery_state="manual_review"),
        OpenPosition("ADAUSDT", 2500.0, 0.79, recovery_state="live"),
        OpenPosition("DOTUSDT", 2500.0, 0.79, recovery_state="live"),
        OpenPosition("DOGEUSDT", 2500.0, 0.79, recovery_state="live"),
    ]
    
    decision = allocator.decide(open_positions)
    
    assert len(decision.exit) == 0
    assert len(decision.enter) == 0

# --- Fix B: Entry-Rejection Cooldown ---

@pytest.mark.anyio
async def test_entry_rejection_cooldown_backoff():
    # We need to mock LiveTraderV2 or parts of it
    with patch("scripts.live_trader_v2.ConfigManager"), \
         patch("scripts.live_trader_v2.StateWriter"), \
         patch("scripts.live_trader_v2.StateReader"), \
         patch("scripts.live_trader_v2.ExecutionClient"), \
         patch("scripts.live_trader_v2.RustDataSubscriber"):
        
        from scripts.live_trader_v2 import LiveTraderV2
        trader = LiveTraderV2()
        trader.cooldowns = MagicMock()
        
        symbol = "BTCUSDT"
        # Must be in _pending_enters for _handle_failed_order_update to act
        trader._pending_enters[symbol] = {"intent_id": "test_id"}
        
        # First rejection
        trader._handle_failed_order_update(symbol, "REJECTED", **{"execution_type": "INSUFFICIENT_BALANCE"})
        
        # Should activate cooldown for 600s
        trader.cooldowns.activate_symbol.assert_called_with(symbol, 600.0, "entry_rejected:INSUFFICIENT_BALANCE")
        
        # Second rejection within window
        trader._pending_enters[symbol] = {"intent_id": "test_id_2"}
        trader._handle_failed_order_update(symbol, "REJECTED", **{"execution_type": "INSUFFICIENT_BALANCE"})
        
        # Should activate cooldown for 1200s (600 * 2^1)
        trader.cooldowns.activate_symbol.assert_called_with(symbol, 1200.0, "entry_rejected:INSUFFICIENT_BALANCE")

# --- Fix C/D: Alert Debounces ---

@pytest.mark.anyio
async def test_runtime_mode_debounce():
    reset_alerter_state()
    session = AsyncMock()
    session.post.return_value.__aenter__.return_value.status = 200
    
    with patch("bongus.monitoring.telegram_alerter.StateReader") as MockReader, \
         patch("bongus.monitoring.telegram_alerter.StateWriter"), \
         patch("bongus.monitoring.telegram_alerter._in_settling_window", return_value=False), \
         patch("bongus.monitoring.telegram_alerter.TELEGRAM_TOKEN", "test_token"), \
         patch("bongus.monitoring.telegram_alerter.CHAT_ID", "test_chat"):
        
        reader = MockReader.return_value
        reader.get_positions_for_current_mode.return_value = []
        reader.get_trades.return_value = []
        reader.get_health_samples.return_value = [{"value": 1.0}]
        
        start_mono = 10000.0
        
        reader.get_risk.side_effect = lambda *args, **kwargs: (
            {"runtime_mode": "LIVE"} if reader.get_risk.call_count == 1 else
            {"runtime_mode": "SAFE_MODE", "safe_mode_reason": "test"}
        )
        
        with patch("time.monotonic") as mock_mono:
            mock_mono.side_effect = lambda: (start_mono + 200 if reader.get_risk.call_count >= 3 else start_mono)
            
            with patch("asyncio.sleep", side_effect=[None, None, asyncio.CancelledError]):
                try:
                    await poll_state_alerts(session)
                except asyncio.CancelledError:
                    pass
                    
        payloads = [call.kwargs.get("json", {}).get("text", "") for call in session.post.call_args_list]
        assert any("RUNTIME MODE CHANGED" in t for t in payloads)

@pytest.mark.anyio
async def test_heartbeat_alert_debounce():
    reset_alerter_state()
    session = AsyncMock()
    session.post.return_value.__aenter__.return_value.status = 200
    
    with patch("bongus.monitoring.telegram_alerter.StateReader") as MockReader, \
         patch("bongus.monitoring.telegram_alerter.StateWriter"), \
         patch("bongus.monitoring.telegram_alerter._in_settling_window", return_value=False), \
         patch("bongus.monitoring.telegram_alerter.TELEGRAM_TOKEN", "test_token"), \
         patch("bongus.monitoring.telegram_alerter.CHAT_ID", "test_chat"), \
         patch("bongus.monitoring.telegram_alerter.HEARTBEAT_MISS_THRESHOLD", 1):
        
        reader = MockReader.return_value
        reader.get_positions_for_current_mode.return_value = []
        reader.get_trades.return_value = []
        
        reader.get_risk.side_effect = [
            {"heartbeat_status": "ok"}, # priming
            {"heartbeat_status": "missed"}, # loop 1
        ]
        
        with patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]):
            try:
                await poll_state_alerts(session)
            except asyncio.CancelledError:
                pass
            
        payloads = [call.kwargs.get("json", {}).get("text", "") for call in session.post.call_args_list]
        assert any("HEARTBEAT STATUS" in t for t in payloads)

# --- Fix E: Supervisor Anomaly Filter ---

def test_supervisor_anomaly_filters_orphan():
    reader = MagicMock()
    store = MagicMock()
    reader.get_positions.return_value = [
        {"symbol": "SOLUSDT", "ann_funding": 0.0, "recovery_state": "manual_review"},
        {"symbol": "ADAUSDT", "ann_funding": 0.0, "recovery_state": "manual_review"},
    ]
    reader.get_stats.return_value = {"account_equity": 10000.0, "ann_funding": 0.5}
    reader.get_pnl_summary.return_value = {}
    reader.get_risk.return_value = {}
    reader.get_trades.return_value = []
    
    store.get_state_table_timestamps.return_value = {"positions": datetime.now(timezone.utc).isoformat()}
    
    snapshot = collect_snapshot(reader, store)
    
    assert snapshot.open_positions == 0
    assert snapshot.ann_funding == 0.5
