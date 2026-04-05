import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from bongus.engine.state_store import StateReader, StateWriter, Trade
from scripts.live_trader_v2 import LiveTraderV2


class _FakeResponse:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.text)


class TestLiveTraderStartupReconciliation(IsolatedAsyncioTestCase):
    def _build_trader(self, db_path: str) -> LiveTraderV2:
        with patch("scripts.live_trader_v2.ConfigManager.start_watching", autospec=True):
            trader = LiveTraderV2()
        trader.state_writer.close()
        trader.state_reader.close()
        trader.state_writer = StateWriter(db_path=db_path)
        trader.state_reader = StateReader(db_path=db_path)
        trader.state_writer.set_risk_snapshot(
            {
                "trading_mode": trader._trading_mode,
                "runtime_mode": trader._runtime_mode,
                "session_id": trader._session_id,
                "bot_started_at": trader._bot_started_at,
            }
        )
        return trader

    def test_config_callbacks_are_safe_before_state_writer_exists(self):
        trader = LiveTraderV2.__new__(LiveTraderV2)
        trader._on_config_reloaded({"pause_new_entries": (False, True)}, {})
        trader._on_config_validation_error("invalid live config")

    def test_calculate_trade_pnl_prorates_annualized_funding(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                net_pnl, funding_collected, basis_pnl, borrow_cost_usd = trader._calculate_trade_pnl(
                    qty=10.0,
                    direction="long",
                    ann_funding=0.1095,
                    hold_hours=8.0,
                    funding_periods=1.0,
                    spot_entry_price=100.0,
                    perp_entry_price=100.0,
                    spot_exit_price=100.0,
                    perp_exit_price=100.0,
                )
                self.assertAlmostEqual(funding_collected, 0.1, places=6)
                self.assertAlmostEqual(borrow_cost_usd, 1000.0 * 0.10 / 1095.0, places=6)
                self.assertAlmostEqual(net_pnl, 0.1 - borrow_cost_usd, places=6)
                self.assertAlmostEqual(basis_pnl, 0.0, places=6)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_calculate_trade_pnl_handles_inverse_trades(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                net_pnl, funding_collected, basis_pnl, borrow_cost_usd = trader._calculate_trade_pnl(
                    qty=2.0,
                    direction="short",
                    ann_funding=-0.219,
                    hold_hours=8.0,
                    funding_periods=1.0,
                    spot_entry_price=100.0,
                    perp_entry_price=101.0,
                    spot_exit_price=95.0,
                    perp_exit_price=97.0,
                )
                self.assertAlmostEqual(basis_pnl, 2.0, places=6)
                self.assertAlmostEqual(funding_collected, 0.0402, places=6)
                self.assertAlmostEqual(borrow_cost_usd, 201.0 * 0.10 / 1095.0, places=6)
                self.assertAlmostEqual(net_pnl, 2.0402 - borrow_cost_usd, places=6)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_count_funding_settlements_uses_discrete_snapshots(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                entry_dt = datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)
                self.assertEqual(
                    trader._count_funding_settlements(
                        entry_dt,
                        datetime(2026, 1, 1, 7, 59, tzinfo=timezone.utc),
                    ),
                    0,
                )
                self.assertEqual(
                    trader._count_funding_settlements(
                        entry_dt,
                        datetime(2026, 1, 1, 8, 1, tzinfo=timezone.utc),
                    ),
                    1,
                )
                self.assertEqual(
                    trader._count_funding_settlements(
                        entry_dt,
                        datetime(2026, 1, 1, 16, 1, tzinfo=timezone.utc),
                    ),
                    2,
                )
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_entry_fill_persists_ann_funding(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                fill_time = "2026-01-01T00:05:00+00:00"
                trader._pending_enters["BTCUSDT"] = {
                    "entry_time": "2026-01-01T00:00:00+00:00",
                    "entry_price": 100.0,
                    "qty": 2.0,
                    "direction": "long",
                    "ann_funding": 0.245,
                }

                trader._on_order_update(
                    "BTCUSDT",
                    "FILLED",
                    filled_qty=2.0,
                    avg_fill_price=101.0,
                    execution_type="FILLED_CYCLE",
                    event_time=fill_time,
                    spot_fill_price=101.0,
                    perp_fill_price=101.0,
                )

                positions = trader.state_reader.get_positions()
                self.assertEqual(len(positions), 1)
                self.assertAlmostEqual(positions[0]["ann_funding"], 0.245)
                self.assertAlmostEqual(positions[0]["entry_ann_funding"], 0.245)
                self.assertEqual(positions[0]["spot_live"], 101.0)
                self.assertEqual(positions[0]["perp_live"], 101.0)
                self.assertEqual(positions[0]["updated_at"], fill_time)
                self.assertEqual(trader._entry_times["BTCUSDT"], fill_time)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_late_entry_fill_replaces_stale_pending_reason(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                fill_time = "2026-01-01T00:05:00+00:00"
                intent_id = "intent-late-fill"
                trader.state_writer.upsert_pending_intent(
                    intent_id=intent_id,
                    symbol="BTCUSDT",
                    intent_type="ENTER_LONG",
                    status="TIMEOUT",
                    direction="long",
                    quantity=2.0,
                )
                trader._stale_pending_enters["BTCUSDT"] = {
                    "intent_id": intent_id,
                    "entry_time": "2026-01-01T00:00:00+00:00",
                    "timed_out_at": "2026-01-01T00:04:00+00:00",
                    "entry_price": 100.0,
                    "qty": 2.0,
                    "direction": "long",
                    "ann_funding": 0.245,
                }
                trader._refresh_stale_pending_flag()

                trader._on_order_update(
                    "BTCUSDT",
                    "FILLED",
                    filled_qty=2.0,
                    avg_fill_price=101.0,
                    execution_type="FILLED_CYCLE",
                    event_time=fill_time,
                    spot_fill_price=101.0,
                    perp_fill_price=101.0,
                )

                risk = trader.state_reader.get_risk()
                positions = trader.state_reader.get_positions()
                self.assertEqual(len(positions), 1)
                self.assertEqual(risk["runtime_mode"], "SAFE_MODE")
                self.assertEqual(risk["safe_mode_reason"], "late_entry_fill")
                self.assertIn("late_entry_fill", trader._safe_mode_flags)
                self.assertNotIn("stale_pending_intent", trader._safe_mode_flags)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_dispatch_exit_uses_tracked_position_quantity(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader.state_writer.upsert_position(
                    symbol="BTCUSDT",
                    side="LONG_SPOT_SHORT_PERP",
                    direction="long",
                    spot_entry=100.0,
                    perp_entry=100.0,
                    qty=2.75,
                    ann_funding=0.12,
                )

                with patch.object(trader.execution, "send_order_intent", return_value=True) as send_mock:
                    trader._dispatch_exit("BTCUSDT", urgency=0.9, direction="long")

                send_mock.assert_called_once()
                payload = send_mock.call_args.args[0]
                self.assertEqual(payload["symbol"], "BTCUSDT")
                self.assertEqual(payload["intent"], "EXIT_LONG")
                self.assertAlmostEqual(float(payload["quantity"]), 2.75)

                pending = trader.state_reader.get_pending_intents(statuses=["PENDING_ACK"])
                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0]["symbol"], "BTCUSDT")
                self.assertAlmostEqual(float(pending[0]["quantity"]), 2.75)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_dispatch_enter_sizes_quantity_from_gross_slot_notional(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader._mark_prices["BTCUSDT"] = 100.0
                trader._lot_step["BTCUSDT"] = 0.001

                with patch.object(trader.execution, "send_order_intent", return_value=True) as send_mock:
                    trader._dispatch_enter(
                        "BTCUSDT",
                        notional_usd=5_000.0,
                        direction="long",
                        ann_funding=0.12,
                    )

                send_mock.assert_called_once()
                payload = send_mock.call_args.args[0]
                self.assertEqual(payload["symbol"], "BTCUSDT")
                self.assertEqual(payload["intent"], "ENTER_LONG")
                self.assertAlmostEqual(float(payload["quantity"]), 25.0)

                pending = trader.state_reader.get_pending_intents(statuses=["PENDING_ACK"])
                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0]["symbol"], "BTCUSDT")
                self.assertAlmostEqual(float(pending[0]["quantity"]), 25.0)
                self.assertAlmostEqual(float(pending[0]["notional_usd"]), 5_000.0)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_leg_level_fills_do_not_finalize_pending_entry(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader._pending_enters["BTCUSDT"] = {
                    "entry_time": "2026-01-01T00:00:00+00:00",
                    "entry_price": 100.0,
                    "qty": 2.0,
                    "direction": "long",
                    "ann_funding": 0.245,
                    "estimated_entry_cost_usd": 1.5,
                }

                trader._on_order_update(
                    "BTCUSDT",
                    "FILLED",
                    filled_qty=2.0,
                    avg_fill_price=101.0,
                    execution_type="TRADE",
                )

                self.assertEqual(trader.state_reader.get_positions(), [])
                self.assertIn("BTCUSDT", trader._pending_enters)

                trader._on_order_update(
                    "BTCUSDT",
                    "FILLED",
                    filled_qty=2.0,
                    avg_fill_price=101.0,
                    execution_type="FILLED_CYCLE",
                    spot_fill_price=101.0,
                    perp_fill_price=100.5,
                )

                positions = trader.state_reader.get_positions()
                self.assertEqual(len(positions), 1)
                self.assertNotIn("BTCUSDT", trader._pending_enters)
                self.assertEqual(positions[0]["spot_entry"], 101.0)
                self.assertEqual(positions[0]["perp_entry"], 100.5)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_funding_decay_helper_is_direction_aware(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                self.assertFalse(trader._funding_has_decayed("short", -0.20))
                self.assertFalse(trader._funding_has_decayed("short", -0.02))
                self.assertTrue(trader._funding_has_decayed("short", -0.005))
                self.assertTrue(trader._funding_has_decayed("short", 0.01))
                self.assertFalse(trader._funding_has_decayed("long", 0.02))
                self.assertTrue(trader._funding_has_decayed("long", 0.005))
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_external_entry_block_reason_reads_kill_switch_flags(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader._preflight_status = "passed"
                self.assertIsNone(trader._external_entry_block_reason())
                trader.state_writer.set_risk("kill_switch", "true")
                self.assertEqual(trader._external_entry_block_reason(), "kill switch active")
                trader.state_writer.set_risk("kill_switch", "false")
                trader.state_writer.set_risk("allow_new_risk", "false")
                self.assertEqual(trader._external_entry_block_reason(), "allow_new_risk=false")
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_pause_new_entries_blocks_external_entry_policy_and_runtime_snapshot(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader._preflight_status = "passed"
                trader._config._values["pause_new_entries"] = True
                trader._last_telemetry_event_monotonic = time.monotonic()

                trader._persist_runtime_state()

                risk = trader.state_reader.get_risk()
                self.assertEqual(trader._external_entry_block_reason(), "new entries paused by operator")
                self.assertTrue(risk["pause_new_entries"])
                self.assertFalse(risk["allow_new_risk"])
                self.assertEqual(risk["entry_block_reason"], "new entries paused by operator")
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_runtime_snapshot_requires_preflight_for_readiness(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader.subscriber._connected_event.set()
                trader._last_telemetry_event_monotonic = time.monotonic()
                trader._preflight_status = "running"

                trader._persist_runtime_state()

                risk = trader.state_reader.get_risk()
                self.assertFalse(risk["runtime_ready"])
                self.assertFalse(risk["allow_new_risk"])
                self.assertFalse(risk["execution_bridge_healthy"])
                self.assertEqual(risk["entry_block_reason"], "starting up: preflight still running")
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_reset_runtime_dashboard_stats_clears_stale_header_values(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader.state_writer.set_stats(
                    {
                        "top_funding_rate": 213.98,
                        "top_funding_symbol": "1000000BOBUSDT",
                        "accepted_candidates": 383.0,
                        "rejected_candidates": 198.0,
                        "scanner_breadth": 581.0,
                        "live_enrichment_breadth": 30.0,
                    }
                )

                trader._reset_runtime_dashboard_stats()

                stats = trader.state_reader.get_stats()
                self.assertEqual(stats["top_funding_rate"], 0.0)
                self.assertEqual(stats["top_funding_symbol"], "")
                self.assertEqual(stats["accepted_candidates"], 0.0)
                self.assertEqual(stats["rejected_candidates"], 0.0)
                self.assertEqual(stats["scanner_breadth"], 0.0)
                self.assertEqual(stats["live_enrichment_breadth"], 0.0)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    async def test_preflight_heartbeat_waits_through_initial_subscription_race(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            sent_heartbeats: list[str] = []

            class _FakeReader:
                def __init__(self) -> None:
                    self.calls = 0

                async def readline(self):
                    self.calls += 1
                    if self.calls == 1:
                        await asyncio.sleep(0.2)
                        raise asyncio.TimeoutError()
                    return json.dumps(
                        {
                            "event": "HeartbeatAck",
                            "heartbeat_id": sent_heartbeats[-1],
                            "status": "ok",
                            "ts_ms": 1_712_000_000_000,
                        }
                    ).encode("utf-8")

            class _FakeWriter:
                def close(self) -> None:
                    return None

                async def wait_closed(self) -> None:
                    return None

            fake_reader = _FakeReader()

            async def _fake_open_connection(_host, _port):
                return fake_reader, _FakeWriter()

            try:
                with patch("scripts.live_trader_v2.asyncio.open_connection", side_effect=_fake_open_connection):
                    with patch.object(
                        trader.execution,
                        "send_heartbeat",
                        side_effect=lambda heartbeat_id: sent_heartbeats.append(heartbeat_id) or True,
                    ):
                        self.assertTrue(await trader._wait_for_heartbeat_ack_once(timeout_s=0.8))
                self.assertGreaterEqual(fake_reader.calls, 2)
                self.assertEqual(len(sent_heartbeats), 1)
                self.assertEqual(trader._last_heartbeat_ack_id, sent_heartbeats[-1])
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    async def test_paper_startup_clears_positions_without_recording_cancelled_trades(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader.state_writer.upsert_position(
                    symbol="DOGEUSDT",
                    side="SHORT_SPOT_LONG_PERP",
                    direction="short",
                    spot_entry=0.09,
                    perp_entry=0.09,
                    qty=10_000.0,
                    ann_funding=-0.12,
                )

                await trader._on_startup()

                self.assertEqual(trader.state_reader.get_positions(), [])
                self.assertEqual(trader.state_reader.get_trades(limit=10), [])
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_effective_entry_threshold_ignores_sentiment_when_disabled(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                base = trader._config.get("entry_ann_funding_threshold")
                trader._config._values["sentiment_enabled"] = False
                trader._sentiment_score = 1.0
                self.assertEqual(trader._effective_entry_threshold(), base)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_stale_pending_entry_times_out_and_blocks_reentry(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader._config._values["pending_intent_max_age_seconds"] = 60
                intent_id = "intent-timeout-1"
                trader.state_writer.upsert_pending_intent(
                    intent_id=intent_id,
                    symbol="ETHUSDT",
                    intent_type="ENTER_LONG",
                    status="PENDING_ACK",
                    direction="long",
                    quantity=1.0,
                )
                trader._pending_enters["ETHUSDT"] = {
                    "intent_id": intent_id,
                    "entry_time": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
                    "entry_price": 100.0,
                    "qty": 1.0,
                    "direction": "long",
                    "ann_funding": 0.15,
                }

                trader._expire_stale_pending_intents()

                self.assertNotIn("ETHUSDT", trader._pending_enters)
                self.assertIn("ETHUSDT", trader._stale_pending_enters)
                timed_out = trader.state_reader.get_pending_intents(statuses=["TIMEOUT"])
                self.assertEqual(len(timed_out), 1)
                self.assertEqual(timed_out[0]["intent_id"], intent_id)
                self.assertIn("stale_pending_intent", trader._safe_mode_flags)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    async def test_liveness_loop_persists_loop_last_alive_timestamp(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            task = None
            try:
                task = asyncio.create_task(trader._run_liveness_loop(interval_s=0.05))
                await asyncio.sleep(0.15)
                trader._shutdown_event.set()
                await task

                risk = trader.state_reader.get_risk()
                self.assertIn("loop_last_alive_at", risk)
                last_alive = datetime.fromisoformat(
                    str(risk["loop_last_alive_at"]).replace("Z", "+00:00")
                )
                self.assertLess(
                    (datetime.now(timezone.utc) - last_alive).total_seconds(),
                    5.0,
                )
            finally:
                trader._shutdown_event.set()
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_persist_runtime_state_refreshes_loop_last_alive_timestamp(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                before = datetime.now(timezone.utc)
                trader._persist_runtime_state()

                risk = trader.state_reader.get_risk()
                self.assertIn("loop_last_alive_at", risk)
                last_alive = datetime.fromisoformat(
                    str(risk["loop_last_alive_at"]).replace("Z", "+00:00")
                )
                self.assertGreaterEqual(last_alive, before - timedelta(seconds=1))
                self.assertLess(
                    (datetime.now(timezone.utc) - last_alive).total_seconds(),
                    5.0,
                )
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_candidate_cycle_stats_keep_full_scanner_breadth_when_snapshots_are_capped(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader._config._values["scanner_max_candidates"] = 64
                trader.funding_ranker.get_rate = lambda _symbol: 0.05
                ranked = [(f"SYM{i:03d}USDT", 0.05) for i in range(100)]

                snapshots = trader._record_candidate_cycle(
                    cycle_id=datetime.now(timezone.utc).isoformat(),
                    ranked=ranked,
                    decision=SimpleNamespace(enter=[], rejected={}),
                    regime_blocked={},
                    cooldown_blocked={},
                    entry_gate_blocked={},
                    external_entry_block_reason=None,
                )

                stats = trader.state_reader.get_stats()
                stored_snapshots = trader.state_reader.get_candidate_snapshots(limit=200)

                self.assertEqual(len(snapshots), 64)
                self.assertEqual(len(stored_snapshots), 64)
                self.assertEqual(stats["scanner_breadth"], 100.0)
                self.assertEqual(stats["accepted_candidates"], 100.0)
                self.assertEqual(stats["rejected_candidates"], 0.0)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    async def test_live_self_heal_clears_stale_pending_entry_when_exchange_is_flat(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(
            os.environ,
            {
                "TRADING_MODE": "live",
                "BINANCE_API_KEY": "fut-key",
                "BINANCE_API_SECRET": "fut-secret",
                "BINANCE_SPOT_API_KEY": "spot-key",
                "BINANCE_SPOT_API_SECRET": "spot-secret",
            },
            clear=False,
        ):
            trader = self._build_trader(db_name)
            try:
                trader._config._values["pending_intent_max_age_seconds"] = 60
                intent_id = "intent-live-enter-flat"
                trader.state_writer.upsert_pending_intent(
                    intent_id=intent_id,
                    symbol="ETHUSDT",
                    intent_type="ENTER_LONG",
                    status="TIMEOUT",
                    direction="long",
                    quantity=1.0,
                )
                trader._stale_pending_enters["ETHUSDT"] = {
                    "intent_id": intent_id,
                    "entry_time": (datetime.now(timezone.utc) - timedelta(minutes=12)).isoformat(),
                    "timed_out_at": (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat(),
                    "entry_price": 100.0,
                    "qty": 1.0,
                    "direction": "long",
                    "ann_funding": 0.18,
                }
                trader._refresh_stale_pending_flag()
                self.assertIn("stale_pending_intent", trader._safe_mode_flags)

                trader._fetch_exchange_startup_snapshot = AsyncMock(
                    return_value={
                        "futures_account": {},
                        "position_risk": [],
                        "futures_open_orders": [],
                        "spot_account": {"balances": []},
                        "spot_open_orders": [],
                        "funding_income": [],
                    }
                )

                await trader._live_self_heal_stale_pending_intents(datetime.now(timezone.utc))

                self.assertNotIn("ETHUSDT", trader._stale_pending_enters)
                self.assertEqual(
                    trader.state_reader.get_pending_intents(statuses=["TIMEOUT"]),
                    [],
                )
                self.assertNotIn("stale_pending_intent", trader._safe_mode_flags)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    async def test_live_self_heal_rebuilds_position_from_exchange_when_entry_filled_late(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(
            os.environ,
            {
                "TRADING_MODE": "live",
                "BINANCE_API_KEY": "fut-key",
                "BINANCE_API_SECRET": "fut-secret",
                "BINANCE_SPOT_API_KEY": "spot-key",
                "BINANCE_SPOT_API_SECRET": "spot-secret",
            },
            clear=False,
        ):
            trader = self._build_trader(db_name)
            try:
                trader._config._values["pending_intent_max_age_seconds"] = 60
                intent_id = "intent-live-enter-filled"
                trader.state_writer.upsert_pending_intent(
                    intent_id=intent_id,
                    symbol="BTCUSDT",
                    intent_type="ENTER_LONG",
                    status="TIMEOUT",
                    direction="long",
                    quantity=0.5,
                )
                trader._stale_pending_enters["BTCUSDT"] = {
                    "intent_id": intent_id,
                    "entry_time": (datetime.now(timezone.utc) - timedelta(minutes=12)).isoformat(),
                    "timed_out_at": (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat(),
                    "entry_price": 65000.0,
                    "qty": 0.5,
                    "direction": "long",
                    "ann_funding": 0.20,
                }
                trader._fetch_exchange_startup_snapshot = AsyncMock(
                    return_value={
                        "futures_account": {},
                        "position_risk": [
                            {
                                "symbol": "BTCUSDT",
                                "positionAmt": "-0.5",
                                "positionSide": "BOTH",
                                "entryPrice": "65000.0",
                                "breakEvenPrice": "65010.0",
                                "markPrice": "64900.0",
                                "unRealizedProfit": "55.0",
                                "updateTime": 1700000003000,
                            }
                        ],
                        "futures_open_orders": [],
                        "spot_account": {
                            "balances": [
                                {"asset": "BTC", "free": "0.50000000", "locked": "0.0"},
                            ]
                        },
                        "spot_open_orders": [],
                        "funding_income": [],
                    }
                )

                await trader._live_self_heal_stale_pending_intents(datetime.now(timezone.utc))

                positions = trader.state_reader.get_positions()
                self.assertEqual(len(positions), 1)
                self.assertEqual(positions[0]["symbol"], "BTCUSDT")
                self.assertEqual(positions[0]["direction"], "long")
                self.assertNotIn("BTCUSDT", trader._stale_pending_enters)
                self.assertEqual(
                    trader.state_reader.get_pending_intents(statuses=["TIMEOUT"]),
                    [],
                )
                self.assertEqual(
                    trader.state_reader.get_risk()["safe_mode_reason"],
                    "late_entry_fill",
                )
                self.assertIn("late_entry_fill", trader._safe_mode_flags)
                self.assertNotIn("stale_pending_intent", trader._safe_mode_flags)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    async def test_live_self_heal_clears_stale_pending_exit_when_exchange_is_flat(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(
            os.environ,
            {
                "TRADING_MODE": "live",
                "BINANCE_API_KEY": "fut-key",
                "BINANCE_API_SECRET": "fut-secret",
                "BINANCE_SPOT_API_KEY": "spot-key",
                "BINANCE_SPOT_API_SECRET": "spot-secret",
            },
            clear=False,
        ):
            trader = self._build_trader(db_name)
            try:
                trader._config._values["pending_intent_max_age_seconds"] = 60
                intent_id = "intent-live-exit-flat"
                created_at = (datetime.now(timezone.utc) - timedelta(minutes=12)).isoformat()
                trader.state_writer.upsert_position(
                    symbol="SOLUSDT",
                    side="LONG_SPOT_SHORT_PERP",
                    direction="long",
                    spot_entry=100.0,
                    perp_entry=100.0,
                    qty=2.0,
                    ann_funding=0.10,
                    updated_at=created_at,
                )
                trader.state_writer.upsert_pending_intent(
                    intent_id=intent_id,
                    symbol="SOLUSDT",
                    intent_type="EXIT_LONG",
                    status="TIMEOUT",
                    direction="long",
                    quantity=2.0,
                )
                trader._pending_exit_intents["SOLUSDT"] = intent_id
                trader._pending_exit_created_at["SOLUSDT"] = created_at
                trader._stale_pending_exits.add("SOLUSDT")
                trader._refresh_stale_pending_flag()
                self.assertIn("stale_pending_intent", trader._safe_mode_flags)

                trader._fetch_exchange_startup_snapshot = AsyncMock(
                    return_value={
                        "futures_account": {},
                        "position_risk": [],
                        "futures_open_orders": [],
                        "spot_account": {"balances": []},
                        "spot_open_orders": [],
                        "funding_income": [],
                    }
                )

                await trader._live_self_heal_stale_pending_intents(datetime.now(timezone.utc))

                self.assertEqual(trader.state_reader.get_positions(), [])
                self.assertEqual(trader._pending_exit_intents, {})
                self.assertEqual(trader._pending_exit_created_at, {})
                self.assertEqual(
                    trader.state_reader.get_pending_intents(statuses=["TIMEOUT"]),
                    [],
                )
                self.assertNotIn("stale_pending_intent", trader._safe_mode_flags)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_refresh_adaptive_state_applies_loss_streak_sizing_and_thresholds(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader._config._values["adaptive_thresholds_enabled"] = True
                trader._config._values["adaptive_rules_paper_only"] = False
                base_threshold = trader._config.get("entry_ann_funding_threshold")
                for idx in range(30):
                    trader.state_writer.record_market_sample(
                        symbol="BTCUSDT",
                        sample_minute=f"2026-03-01T00:{idx:02d}:00+00:00",
                        ann_funding=0.20 + idx * 0.01,
                        basis_pct=0.001,
                        mark_price=60_000.0,
                        minute_notional_volume=100_000.0,
                    )
                for idx, pnl in enumerate([-10.0, -12.0, -8.0]):
                    entry_time = f"2026-03-02T00:0{idx}:00+00:00"
                    exit_time = f"2026-03-02T01:0{idx}:00+00:00"
                    trader.state_writer.record_trade(
                        Trade(
                            symbol="BTCUSDT",
                            side="LONG",
                            entry_time=entry_time,
                            exit_time=exit_time,
                            entry_price=100.0,
                            exit_price=99.0,
                            qty=1.0,
                            net_pnl_usd=pnl,
                        )
                    )

                trader._refresh_adaptive_state()

                assert trader._loss_streak == 3
                assert trader._effective_notional_scale() == trader._config.get("loss_streak_notional_scale")
                assert trader._effective_entry_threshold() > base_threshold
                assert trader._effective_rotation_gap() >= 0.0
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_exit_trade_in_paper_mode_applies_borrow_carry(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                entry_time = "2026-01-01T00:01:00+00:00"
                exit_time = "2026-01-01T08:01:00+00:00"
                trader.state_writer.upsert_position(
                    symbol="BTCUSDT",
                    side="LONG_SPOT_SHORT_PERP",
                    direction="long",
                    spot_entry=100.0,
                    perp_entry=100.0,
                    qty=1.0,
                    ann_funding=-0.05,
                    entry_ann_funding=0.1095,
                    spot_live=100.0,
                    perp_live=100.0,
                    updated_at=entry_time,
                )
                trader._entry_times["BTCUSDT"] = entry_time
                trader._position_directions["BTCUSDT"] = "long"
                trader._exit_events["BTCUSDT"] = asyncio.Event()

                trader._on_order_update(
                    "BTCUSDT",
                    "FILLED",
                    filled_qty=1.0,
                    execution_type="FILLED_CYCLE",
                    event_time=exit_time,
                    spot_fill_price=100.0,
                    perp_fill_price=100.0,
                )

                trades = trader.state_reader.get_trades(limit=1)
                self.assertEqual(len(trades), 1)
                self.assertGreater(trades[0]["funding_collected"], 0.0)
                self.assertGreater(trades[0]["borrow_cost_usd"], 0.0)
                self.assertLess(trades[0]["net_pnl_usd"], trades[0]["funding_collected"])
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_exit_trade_in_live_mode_uses_actual_funding_cashflows(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(
            os.environ,
            {
                "TRADING_MODE": "live",
                "BINANCE_API_KEY": "fut-key",
                "BINANCE_API_SECRET": "fut-secret",
            },
            clear=False,
        ):
            trader = self._build_trader(db_name)
            try:
                entry_time = "2026-01-01T00:01:00+00:00"
                exit_time = "2026-01-01T08:01:00+00:00"
                funding_time_ms = int(datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc).timestamp() * 1000)
                trader.state_writer.set_risk_snapshot(
                    {
                        "trading_mode": "live",
                        "runtime_mode": "LIVE",
                        "session_id": trader._session_id,
                        "bot_started_at": trader._bot_started_at,
                    }
                )
                trader.state_writer.upsert_position(
                    symbol="BTCUSDT",
                    side="LONG_SPOT_SHORT_PERP",
                    direction="long",
                    spot_entry=100.0,
                    perp_entry=100.0,
                    qty=1.0,
                    ann_funding=-0.05,
                    entry_ann_funding=-0.05,
                    spot_live=100.0,
                    perp_live=100.0,
                    updated_at=entry_time,
                )
                trader._entry_times["BTCUSDT"] = entry_time
                trader._position_directions["BTCUSDT"] = "long"
                trader._exit_events["BTCUSDT"] = asyncio.Event()

                with patch.object(
                    trader,
                    "_signed_request_json_sync",
                    return_value=[
                        {
                            "symbol": "BTCUSDT",
                            "income": "0.50",
                            "asset": "USDT",
                            "incomeType": "FUNDING_FEE",
                            "time": funding_time_ms,
                            "tranId": 12345,
                        }
                    ],
                ):
                    trader._on_order_update(
                        "BTCUSDT",
                        "FILLED",
                        filled_qty=1.0,
                        execution_type="FILLED_CYCLE",
                        event_time=exit_time,
                        spot_fill_price=100.0,
                        perp_fill_price=100.0,
                    )

                trades = trader.state_reader.get_trades(limit=1)
                funding_events = trader.state_reader.get_trade_funding_cashflows(
                    "BTCUSDT",
                    entry_time,
                    exit_time,
                )
                self.assertEqual(len(trades), 1)
                self.assertEqual(trades[0]["funding_source"], "actual_rest")
                self.assertAlmostEqual(trades[0]["funding_collected"], 0.5, places=6)
                self.assertGreater(trades[0]["borrow_cost_usd"], 0.0)
                self.assertEqual(len(funding_events), 1)
                self.assertAlmostEqual(funding_events[0]["amount"], 0.5, places=6)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    async def test_live_startup_reconciliation_preserves_local_basis_for_matching_position(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(
            os.environ,
            {
                "TRADING_MODE": "live",
                "BINANCE_API_KEY": "fut-key",
                "BINANCE_API_SECRET": "fut-secret",
                "BINANCE_SPOT_API_KEY": "spot-key",
                "BINANCE_SPOT_API_SECRET": "spot-secret",
            },
            clear=False,
        ):
            trader = self._build_trader(db_name)
            try:
                trader.state_writer.upsert_position(
                    symbol="BTCUSDT",
                    side="LONG_SPOT_SHORT_PERP",
                    spot_entry=64950.0,
                    perp_entry=65025.0,
                    qty=0.5,
                    direction="long",
                    ann_funding=0.1,
                    entry_ann_funding=0.12,
                    updated_at="2026-01-01T00:00:00+00:00",
                )
                trader._entry_times["BTCUSDT"] = "2026-01-01T00:00:00+00:00"
                trader._fetch_exchange_startup_snapshot = AsyncMock(
                    return_value={
                        "futures_account": {
                            "totalMarginBalance": "12000.0",
                            "totalWalletBalance": "11950.0",
                            "availableBalance": "8900.0",
                        },
                        "position_risk": [
                            {
                                "symbol": "BTCUSDT",
                                "positionAmt": "-0.5",
                                "positionSide": "BOTH",
                                "entryPrice": "65000.0",
                                "breakEvenPrice": "65010.0",
                                "markPrice": "64900.0",
                                "unRealizedProfit": "55.0",
                                "updateTime": 1700000003000,
                            }
                        ],
                        "futures_open_orders": [],
                        "spot_account": {
                            "balances": [
                                {"asset": "BTC", "free": "0.50000000", "locked": "0.0"},
                            ]
                        },
                        "spot_open_orders": [],
                        "funding_income": [],
                    }
                )

                await trader._reconcile_live_startup_state()

                positions = trader.state_reader.get_positions()
                self.assertEqual(len(positions), 1)
                self.assertEqual(positions[0]["spot_entry"], 64950.0)
                self.assertEqual(positions[0]["perp_entry"], 65025.0)
                self.assertEqual(positions[0]["updated_at"], "2026-01-01T00:00:00+00:00")
                self.assertEqual(positions[0]["entry_ann_funding"], 0.12)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_monitored_symbols_honor_env(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(
            os.environ,
            {"TRADING_MODE": "paper", "MONITORED_SYMBOLS": "BTCUSDT,ETHUSDT,SOLUSDT,DOGEUSDT"},
            clear=False,
        ):
            trader = self._build_trader(db_name)
            try:
                self.assertEqual(
                    trader.monitored_symbols,
                    ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT"],
                )
                self.assertEqual(
                    trader.rest_depth_fetcher._symbols,
                    ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT"],
                )
                self.assertTrue(trader.funding_ranker.has_symbol("BTCUSDT"))
                self.assertFalse(trader.funding_ranker.has_symbol("PEPEUSDT"))
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_live_enriched_symbols_prioritize_dynamic_winners_over_extra_seed_symbols(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(
            os.environ,
            {"TRADING_MODE": "paper", "MONITORED_SYMBOLS": "BTCUSDT,ETHUSDT,XLMUSDT,ADAUSDT,LTCUSDT"},
            clear=False,
        ):
            trader = self._build_trader(db_name)
            try:
                trader.state_writer.upsert_position(
                    symbol="SOLUSDT",
                    side="LONG_SPOT_SHORT_PERP",
                    direction="long",
                    spot_entry=100.0,
                    perp_entry=100.0,
                    qty=1.0,
                    ann_funding=0.10,
                )
                trader._pending_enters["XRPUSDT"] = {
                    "entry_time": "2026-01-01T00:00:00+00:00",
                    "entry_price": 1.0,
                    "qty": 1.0,
                    "direction": "long",
                    "ann_funding": 0.12,
                }
                trader.funding_ranker._symbols = {
                    "BTCUSDT",
                    "ETHUSDT",
                    "XLMUSDT",
                    "ADAUSDT",
                    "LTCUSDT",
                    "SOLUSDT",
                    "XRPUSDT",
                    "DOGEUSDT",
                    "PEPEUSDT",
                }
                trader.funding_ranker._rates = {
                    "BTCUSDT": 0.25,
                    "ETHUSDT": 0.20,
                    "XLMUSDT": 0.08,
                    "ADAUSDT": 0.07,
                    "LTCUSDT": 0.06,
                    "SOLUSDT": 0.18,
                    "XRPUSDT": 0.16,
                    "DOGEUSDT": 0.22,
                    "PEPEUSDT": 0.21,
                }
                trader.funding_ranker._last_successful_refresh = datetime.now(timezone.utc)
                trader._tradable_perp_symbols = set(trader.funding_ranker._symbols)
                trader._tradable_spot_symbols = set(trader.funding_ranker._symbols)

                with patch("scripts.live_trader_v2.MAX_LIVE_ENRICHED_SYMBOLS", 5):
                    live_symbols = trader._live_enriched_symbols()

                self.assertEqual(live_symbols[:2], ["SOLUSDT", "XRPUSDT"])
                self.assertEqual(len(live_symbols), 5)
                self.assertIn("BTCUSDT", live_symbols)
                self.assertIn("DOGEUSDT", live_symbols)
                self.assertIn("PEPEUSDT", live_symbols)
                self.assertNotIn("ETHUSDT", live_symbols)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    async def test_fetch_lot_step_sizes_limits_dynamic_universe_to_symbols_with_spot_and_perp(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                def fake_get(url, timeout=None):
                    if url == "https://fapi.binance.com/fapi/v1/exchangeInfo":
                        return _FakeResponse(
                            {
                                "symbols": [
                                    {
                                        "symbol": "BTCUSDT",
                                        "contractType": "PERPETUAL",
                                        "status": "TRADING",
                                        "quoteAsset": "USDT",
                                        "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.001"}],
                                    },
                                    {
                                        "symbol": "PEPEUSDT",
                                        "contractType": "PERPETUAL",
                                        "status": "TRADING",
                                        "quoteAsset": "USDT",
                                        "filters": [{"filterType": "LOT_SIZE", "stepSize": "1"}],
                                    },
                                    {
                                        "symbol": "ETHBTC",
                                        "contractType": "PERPETUAL",
                                        "status": "TRADING",
                                        "quoteAsset": "BTC",
                                        "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.001"}],
                                    },
                                ]
                            }
                        )
                    if url == "https://api.binance.com/api/v3/exchangeInfo":
                        return _FakeResponse(
                            {
                                "symbols": [
                                    {"symbol": "BTCUSDT", "status": "TRADING", "quoteAsset": "USDT"},
                                    {"symbol": "ETHUSDT", "status": "TRADING", "quoteAsset": "USDT"},
                                ]
                            }
                        )
                    raise AssertionError(f"Unexpected URL: {url}")

                with patch("scripts.live_trader_v2.requests.get", side_effect=fake_get):
                    await trader._fetch_lot_step_sizes()

                self.assertEqual(trader._tradable_perp_symbols, {"BTCUSDT", "PEPEUSDT"})
                self.assertEqual(trader._tradable_spot_symbols, {"BTCUSDT", "ETHUSDT"})
                self.assertEqual(trader.funding_ranker._allowed_symbols, {"BTCUSDT"})
                self.assertAlmostEqual(trader._lot_step["BTCUSDT"], 0.001)
                self.assertAlmostEqual(trader._lot_step["PEPEUSDT"], 1.0)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    async def test_sync_rest_depth_to_tracker_backfills_basis_for_live_candidates(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader.funding_ranker._symbols = {"BTCUSDT", "ETHUSDT", "DOGEUSDT"}
                trader.funding_ranker._rates = {
                    "BTCUSDT": 0.20,
                    "ETHUSDT": 0.18,
                    "DOGEUSDT": 0.25,
                }
                trader.funding_ranker._last_successful_refresh = datetime.now(timezone.utc)
                trader._tradable_perp_symbols = set(trader.funding_ranker._symbols)

                trader.rest_depth_fetcher._last_fetch["DOGEUSDT"] = time.time()
                trader.rest_depth_fetcher._spot_depths["DOGEUSDT"] = 125_000.0
                trader.rest_depth_fetcher._perp_depths["DOGEUSDT"] = 150_000.0
                trader.rest_depth_fetcher._spot_quotes["DOGEUSDT"] = (0.099, 0.101)
                trader.rest_depth_fetcher._perp_quotes["DOGEUSDT"] = (0.102, 0.104)

                with patch("scripts.live_trader_v2.MAX_LIVE_ENRICHED_SYMBOLS", 3):
                    await trader._sync_rest_depth_to_tracker()

                basis_pct = trader.depth_tracker.basis_pct("DOGEUSDT")
                self.assertAlmostEqual(trader.depth_tracker.get_entry_depth("DOGEUSDT"), 125_000.0)
                self.assertAlmostEqual(trader.depth_tracker.spot_mid_price("DOGEUSDT"), 0.1)
                self.assertAlmostEqual(trader.depth_tracker.perp_mid_price("DOGEUSDT"), 0.103)
                self.assertIsNotNone(basis_pct)
                self.assertAlmostEqual(float(basis_pct), 0.03)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_risk_limits_allow_partial_portfolio_to_build_normally(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                decision = trader._evaluate_risk_controls(
                    [
                        {
                            "symbol": "BTCUSDT",
                            "qty": 1.0,
                            "spot_live": 100.0,
                            "perp_live": 100.0,
                            "spot_entry": 100.0,
                            "perp_entry": 100.0,
                            "net_pnl_usd": 0.0,
                        }
                    ]
                )

                self.assertTrue(decision.allow_new_risk)
                self.assertFalse(decision.derisk_required)
                self.assertAlmostEqual(trader._risk_engine.limits.max_symbol_concentration, 1.0)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_single_missed_heartbeat_does_not_trip_risk_limits(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader._heartbeat_misses = 1
                trader._last_heartbeat_rtt_ms = 125

                decision = trader._evaluate_risk_controls(
                    [
                        {
                            "symbol": "BTCUSDT",
                            "qty": 1.0,
                            "spot_live": 100.0,
                            "perp_live": 100.0,
                            "spot_entry": 100.0,
                            "perp_entry": 100.0,
                            "net_pnl_usd": 0.0,
                        }
                    ]
                )

                self.assertTrue(decision.allow_new_risk)
                self.assertFalse(decision.derisk_required)
                self.assertNotIn("risk_limits", trader._safe_mode_flags)
                self.assertEqual(trader.state_reader.get_risk()["venue_latency_ms"], 125)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_risk_limits_still_block_overconcentrated_partial_books(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                decision = trader._evaluate_risk_controls(
                    [
                        {
                            "symbol": "BTCUSDT",
                            "qty": 75.0,
                            "spot_live": 100.0,
                            "perp_live": 100.0,
                            "spot_entry": 100.0,
                            "perp_entry": 100.0,
                            "net_pnl_usd": 0.0,
                        },
                        {
                            "symbol": "ETHUSDT",
                            "qty": 25.0,
                            "spot_live": 100.0,
                            "perp_live": 100.0,
                            "spot_entry": 100.0,
                            "perp_entry": 100.0,
                            "net_pnl_usd": 0.0,
                        },
                    ]
                )

                self.assertFalse(decision.allow_new_risk)
                self.assertTrue(decision.derisk_required)
                self.assertIn("symbol concentration limit exceeded", decision.reasons)
                self.assertAlmostEqual(trader._risk_engine.limits.max_symbol_concentration, 0.5)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_cross_validation_mismatch_logging_is_throttled(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                with patch("scripts.live_trader_v2.logger.warning") as warning_mock:
                    trader._maybe_log_cross_validation_gap(
                        "BTCUSDT",
                        0.01,
                        0.05,
                        now=100.0,
                    )
                    trader._maybe_log_cross_validation_gap(
                        "BTCUSDT",
                        0.011,
                        0.051,
                        now=160.0,
                    )
                    trader._maybe_log_cross_validation_gap(
                        "BTCUSDT",
                        0.01,
                        0.05,
                        now=760.0,
                    )

                self.assertEqual(warning_mock.call_count, 2)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    async def test_live_startup_reconciles_signed_exchange_truth(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        requested_urls: list[tuple[str, dict | None]] = []

        with patch.dict(
            os.environ,
            {
                "TRADING_MODE": "live",
                "BINANCE_API_KEY": "fut-key",
                "BINANCE_API_SECRET": "fut-secret",
                "BINANCE_SPOT_API_KEY": "spot-key",
                "BINANCE_SPOT_API_SECRET": "spot-secret",
            },
            clear=False,
        ):
            trader = self._build_trader(db_name)
            try:
                trader._preflight_status = "passed"
                trader.state_writer.upsert_position(
                    symbol="SOLUSDT",
                    side="LONG_SPOT_SHORT_PERP",
                    spot_entry=150.0,
                    perp_entry=150.0,
                    qty=4.0,
                    direction="long",
                )

                def fake_get(url, headers=None, timeout=None):
                    requested_urls.append((url, headers))
                    if url == "https://fapi.binance.com/fapi/v1/time":
                        return _FakeResponse({"serverTime": 1700000005000})
                    if url.startswith("https://fapi.binance.com/fapi/v3/account?"):
                        return _FakeResponse(
                            {
                                "totalMarginBalance": "12000.0",
                                "totalWalletBalance": "11950.0",
                                "availableBalance": "8900.0",
                            }
                        )
                    if url.startswith("https://fapi.binance.com/fapi/v3/positionRisk?"):
                        return _FakeResponse(
                            [
                                {
                                    "symbol": "BTCUSDT",
                                    "positionAmt": "-0.5",
                                    "positionSide": "BOTH",
                                    "entryPrice": "65000.0",
                                    "breakEvenPrice": "65010.0",
                                    "markPrice": "64900.0",
                                    "unRealizedProfit": "55.0",
                                    "updateTime": 1700000003000,
                                },
                                {
                                    "symbol": "ETHUSDT",
                                    "positionAmt": "0",
                                    "positionSide": "BOTH",
                                    "entryPrice": "0",
                                    "breakEvenPrice": "0",
                                    "markPrice": "3500.0",
                                    "unRealizedProfit": "0.0",
                                    "updateTime": 0,
                                },
                            ]
                        )
                    if url.startswith("https://fapi.binance.com/fapi/v1/openOrders?"):
                        return _FakeResponse([])
                    if url.startswith("https://fapi.binance.com/fapi/v1/income?"):
                        return _FakeResponse(
                            [
                                {
                                    "incomeType": "FUNDING_FEE",
                                    "income": "5.25",
                                    "time": 1700000004000,
                                }
                            ]
                        )
                    if url.startswith("https://api.binance.com/api/v3/account?"):
                        return _FakeResponse(
                            {
                                "balances": [
                                    {"asset": "BTC", "free": "0.50000000", "locked": "0.0"},
                                    {"asset": "USDT", "free": "1000.0", "locked": "0.0"},
                                ]
                            }
                        )
                    if url.startswith("https://api.binance.com/api/v3/openOrders?"):
                        return _FakeResponse([])
                    raise AssertionError(f"Unexpected URL: {url}")

                with patch("scripts.live_trader_v2.requests.get", side_effect=fake_get):
                    await trader._on_startup()

                positions = trader.state_reader.get_positions()
                stats = trader.state_reader.get_stats()
                risk = trader.state_reader.get_risk()

                self.assertEqual(len(positions), 1)
                self.assertEqual(positions[0]["symbol"], "BTCUSDT")
                self.assertEqual(positions[0]["direction"], "long")
                self.assertEqual(positions[0]["side"], "LONG_SPOT_SHORT_PERP")
                self.assertEqual(positions[0]["qty"], 0.5)
                self.assertEqual(positions[0]["spot_entry"], 65010.0)
                self.assertEqual(positions[0]["perp_live"], 64900.0)
                self.assertEqual(positions[0]["updated_at"], trader._entry_times["BTCUSDT"])

                self.assertEqual(stats["account_equity"], 12000.0)
                self.assertEqual(stats["gross_exposure"], 64900.0)
                self.assertEqual(stats["max_gross_exposure"], trader._config.get("max_gross_exposure_usd"))

                self.assertEqual(risk["startup_reconciliation_status"], "ok")
                self.assertEqual(risk["startup_reconciliation_position_count"], 1)
                self.assertEqual(risk["startup_reconciliation_local_only_symbols"], ["SOLUSDT"])
                self.assertEqual(risk["startup_reconciliation_mismatched_symbols"], [])
                self.assertEqual(risk["startup_reconciliation_spot_hedge_gaps"], [])
                self.assertEqual(risk["startup_reconciliation_last_funding_fee"], 5.25)
                self.assertTrue(risk["allow_new_risk"])

                signed_urls = [
                    url
                    for url, _ in requested_urls
                    if "api/v3/account?" in url
                    or "api/v3/openOrders?" in url
                    or "fapi/v3/account?" in url
                    or "fapi/v3/positionRisk?" in url
                    or "fapi/v1/openOrders?" in url
                    or "fapi/v1/income?" in url
                ]
                self.assertTrue(signed_urls)
                for url in signed_urls:
                    self.assertIn("timestamp=", url)
                    self.assertIn("recvWindow=", url)
                    self.assertIn("signature=", url)

                futures_headers = [
                    headers for url, headers in requested_urls
                    if url.startswith("https://fapi.binance.com/") and headers
                ]
                spot_headers = [
                    headers for url, headers in requested_urls
                    if url.startswith("https://api.binance.com/") and headers
                ]
                self.assertTrue(all(header["X-MBX-APIKEY"] == "fut-key" for header in futures_headers))
                self.assertTrue(all(header["X-MBX-APIKEY"] == "spot-key" for header in spot_headers))
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    async def test_live_startup_blocks_when_exchange_has_open_orders(self):
        db_name = self.id().replace(".", "_") + ".db"
        with patch.dict(
            os.environ,
            {
                "TRADING_MODE": "live",
                "BINANCE_API_KEY": "fut-key",
                "BINANCE_API_SECRET": "fut-secret",
            },
            clear=False,
        ):
            trader = self._build_trader(db_name)
            try:
                def fake_get(url, headers=None, timeout=None):
                    if url == "https://fapi.binance.com/fapi/v1/time":
                        return _FakeResponse({"serverTime": 1700000005000})
                    if url.startswith("https://fapi.binance.com/fapi/v3/account?"):
                        return _FakeResponse(
                            {
                                "totalMarginBalance": "10000.0",
                                "totalWalletBalance": "9950.0",
                                "availableBalance": "9000.0",
                            }
                        )
                    if url.startswith("https://fapi.binance.com/fapi/v3/positionRisk?"):
                        return _FakeResponse([])
                    if url.startswith("https://fapi.binance.com/fapi/v1/openOrders?"):
                        return _FakeResponse(
                            [
                                {
                                    "symbol": "BTCUSDT",
                                    "clientOrderId": "bngs_live_1",
                                    "status": "NEW",
                                }
                            ]
                        )
                    if url.startswith("https://api.binance.com/api/v3/account?"):
                        return _FakeResponse({"balances": []})
                    if url.startswith("https://api.binance.com/api/v3/openOrders?"):
                        return _FakeResponse([])
                    if url.startswith("https://fapi.binance.com/fapi/v1/income?"):
                        return _FakeResponse([])
                    raise AssertionError(f"Unexpected URL: {url}")

                with patch("scripts.live_trader_v2.requests.get", side_effect=fake_get):
                    with self.assertRaises(RuntimeError):
                        await trader._on_startup()

                risk = trader.state_reader.get_risk()
                self.assertEqual(risk["startup_reconciliation_status"], "blocked_open_orders")
                self.assertEqual(risk["startup_reconciliation_open_order_symbols"], ["BTCUSDT"])
                self.assertEqual(risk["startup_reconciliation_open_order_count"], 1)
                self.assertFalse(risk["allow_new_risk"])
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    async def test_live_startup_blocks_unsupported_inverse_style_positions(self):
        db_name = self.id().replace(".", "_") + ".db"
        with patch.dict(
            os.environ,
            {
                "TRADING_MODE": "live",
                "BINANCE_API_KEY": "fut-key",
                "BINANCE_API_SECRET": "fut-secret",
                "BINANCE_SPOT_API_KEY": "spot-key",
                "BINANCE_SPOT_API_SECRET": "spot-secret",
            },
            clear=False,
        ):
            trader = self._build_trader(db_name)
            try:
                def fake_get(url, headers=None, timeout=None):
                    if url == "https://fapi.binance.com/fapi/v1/time":
                        return _FakeResponse({"serverTime": 1700000005000})
                    if url.startswith("https://fapi.binance.com/fapi/v3/account?"):
                        return _FakeResponse(
                            {
                                "totalMarginBalance": "10000.0",
                                "totalWalletBalance": "9950.0",
                                "availableBalance": "9000.0",
                            }
                        )
                    if url.startswith("https://fapi.binance.com/fapi/v3/positionRisk?"):
                        return _FakeResponse(
                            [
                                {
                                    "symbol": "BTCUSDT",
                                    "positionAmt": "0.5",
                                    "positionSide": "LONG",
                                    "entryPrice": "65000.0",
                                    "breakEvenPrice": "65010.0",
                                    "markPrice": "64900.0",
                                    "unRealizedProfit": "-55.0",
                                    "updateTime": 1700000003000,
                                }
                            ]
                        )
                    if url.startswith("https://fapi.binance.com/fapi/v1/openOrders?"):
                        return _FakeResponse([])
                    if url.startswith("https://fapi.binance.com/fapi/v1/income?"):
                        return _FakeResponse([])
                    if url.startswith("https://api.binance.com/api/v3/account?"):
                        return _FakeResponse({"balances": []})
                    if url.startswith("https://api.binance.com/api/v3/openOrders?"):
                        return _FakeResponse([])
                    raise AssertionError(f"Unexpected URL: {url}")

                with patch("scripts.live_trader_v2.requests.get", side_effect=fake_get):
                    with self.assertRaises(RuntimeError):
                        await trader._on_startup()

                risk = trader.state_reader.get_risk()
                self.assertEqual(risk["startup_reconciliation_status"], "blocked_mismatch")
                self.assertEqual(risk["startup_reconciliation_unsupported_directions"], ["BTCUSDT"])
                self.assertFalse(risk["allow_new_risk"])
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)
