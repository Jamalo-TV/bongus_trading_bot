import asyncio
from collections import deque
import json
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import scripts.live_trader_v2
from bongus.engine.state_store import StateReader, StateWriter, Trade
from bongus.portfolio.portfolio_allocator import OpenPosition
from bongus.strategies.opportunity_kernel import OPPORTUNITY_KERNEL_VERSION
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
        config_path = db_path + ".config.json"
        with patch("scripts.live_trader_v2.ConfigManager.start_watching", autospec=True):
            trader = LiveTraderV2(db_path=db_path, config_path=config_path)
        trader._last_operator_flatten_request_id = ""
        trader.state_writer.set_risk_snapshot(
            {
                "trading_mode": trader._trading_mode,
                "runtime_mode": trader._runtime_mode,
                "session_id": trader._session_id,
                "bot_started_at": trader._bot_started_at,
                "runtime_settling_until_iso": trader._runtime_settling_until_iso,
                "runtime_settling_seconds": trader._runtime_settling_seconds,
            }
        )
        trader.state_writer.flush()
        return trader

    @staticmethod
    def _mark_funding_metadata_ready(trader: LiveTraderV2) -> None:
        """Seed the independent funding-calendar precondition for policy tests."""
        trader.funding_ranker._last_funding_info_refresh = datetime.now(timezone.utc)

    @staticmethod
    def _mark_config_consensus_ready(trader: LiveTraderV2) -> None:
        """Seed the independent Rust/Python config precondition for policy tests."""
        config_hash = trader._config.canonical_snapshot().sha256
        trader._config_hash_consensus = True
        trader._rust_config_version_hash = config_hash
        trader._config_sync_status = "applied"
        trader._config_sync_reason = ""

    @staticmethod
    def _mark_private_streams_ready(trader: LiveTraderV2) -> None:
        trader._private_stream_ready_markets = {"spot", "perp"}
        trader._private_stream_status = {
            "spot": {"status": "READY"},
            "perp": {"status": "READY"},
        }
        trader._safe_mode_flags.discard("private_stream_recovery")

    @staticmethod
    def _mark_rust_execution_ready(trader: LiveTraderV2) -> None:
        trader._rust_execution_ready = True
        trader._rust_execution_readiness_status = "READY"
        trader._rust_execution_readiness_reason = ""
        trader._safe_mode_flags.discard("rust_execution_readiness")

    @staticmethod
    def _seed_authoritative_funding(
        trader: LiveTraderV2,
        symbol: str = "BTCUSDT",
        annualized_rate: float = 25.0,
    ) -> None:
        observed_at = datetime.now(timezone.utc)
        interval_hours = 8
        trader.funding_ranker.calendar.update_funding_info(
            [{"symbol": symbol, "fundingIntervalHours": interval_hours}],
            observed_at=observed_at,
        )
        trader.funding_ranker._last_funding_info_refresh = observed_at
        trader.funding_ranker.update_rate(
            symbol,
            annualized_rate / (365.0 * 24.0 / interval_hours),
            next_funding_time_ms=int(
                (observed_at + timedelta(hours=1)).timestamp() * 1_000
            ),
        )

    def test_config_callbacks_are_safe_before_state_writer_exists(self):
        trader = LiveTraderV2.__new__(LiveTraderV2)
        trader._on_config_reloaded({"pause_new_entries": (False, True)}, {})
        trader._on_config_validation_error("invalid live config")

    def test_typed_config_ack_establishes_consensus_and_reload_revokes_it(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "live"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                with trader._config._lock:
                    trader._config._values["pause_new_entries"] = False
                    trader._config._values["per_symbol_notional_cap_usd"] = 2500.0
                    trader._config._values["max_gross_exposure_usd"] = 10000.0
                self.assertTrue(trader._dispatch_config_sync(force=True))
                commands = trader.state_reader.get_execution_command_outbox(
                    intent_id=trader._config_sync_intent_id
                )
                self.assertEqual(len(commands), 1)
                command = commands[0]
                envelope = command["envelope"]
                config_hash = str(envelope["config_version_hash"])
                trader._on_config_ack(
                    {
                        "event": "ConfigAck",
                        "schema_version": 2,
                        "intent_id": envelope["intent_id"],
                        "producer_id": envelope["producer_id"],
                        "sequence": envelope["sequence"],
                        "account_id": envelope["account_id"],
                        "environment": envelope["environment"],
                        "strategy_id": envelope["strategy_id"],
                        "cycle_id": envelope["cycle_id"],
                        "config_version_hash": config_hash,
                        "command_hash": envelope["command_hash"],
                        "ack_status": "TERMINAL",
                        "reason": "",
                        "event_time_ms": int(time.time() * 1000),
                        "replay": False,
                        "declared_config_hash": config_hash,
                        "applied_config_hash": config_hash,
                        "config_status": "APPLIED",
                    }
                )
                self.assertTrue(trader._config_hash_consensus)
                self.assertEqual(trader._rust_config_version_hash, config_hash)

                trader._on_config_reloaded(
                    {"entry_ann_funding_threshold": (0.15, 0.16)},
                    trader._config.snapshot(),
                )
                self.assertFalse(trader._config_hash_consensus)
                self.assertEqual(trader._config_sync_status, "pending_reload")
                trader._preflight_status = "passed"
                self.assertIn(
                    "execution config consensus unavailable",
                    trader._entry_policy_block_reason() or "",
                )
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_private_stream_quorum_is_structural_and_revoked_on_any_gap(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "testnet"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                self.assertEqual(trader._private_stream_ready_markets, set())
                trader._on_private_stream_status(
                    {
                        "event": "PrivateStreamStatus",
                        "market": "spot",
                        "status": "READY",
                        "start_time_ms": 100,
                        "end_time_ms": 200,
                        "orders_replayed": 1,
                        "trades_replayed": 2,
                        "error": None,
                    }
                )
                self.assertEqual(trader._private_stream_ready_markets, {"spot"})
                self.assertIn("private_stream_recovery", trader._safe_mode_flags)

                # A syntactically READY event without a proven range must fail closed.
                trader._on_private_stream_status(
                    {
                        "event": "PrivateStreamStatus",
                        "market": "perp",
                        "status": "READY",
                        "start_time_ms": None,
                        "end_time_ms": None,
                        "orders_replayed": 0,
                        "trades_replayed": 0,
                    }
                )
                self.assertEqual(trader._private_stream_ready_markets, {"spot"})

                trader._on_private_stream_status(
                    {
                        "event": "PrivateStreamStatus",
                        "market": "perp",
                        "status": "READY",
                        "start_time_ms": 100,
                        "end_time_ms": 250,
                        "orders_replayed": 1,
                        "trades_replayed": 1,
                        "error": None,
                    }
                )
                self.assertEqual(
                    trader._private_stream_ready_markets, {"spot", "perp"}
                )
                self.assertNotIn("private_stream_recovery", trader._safe_mode_flags)
                self.assertTrue(
                    trader.state_reader.get_risk()["private_stream_recovery_ready"]
                )
                self.assertFalse(trader._rust_execution_ready)
                trader._on_execution_readiness(
                    {
                        "event": "ExecutionReadiness",
                        "status": "READY",
                        "reason": "spot and futures exchange truth reconciled",
                        "event_time_ms": 251,
                    }
                )
                self.assertTrue(trader._rust_execution_ready)
                self.assertNotIn(
                    "rust_execution_readiness", trader._safe_mode_flags
                )

                trader._on_private_stream_status(
                    {
                        "event": "PrivateStreamStatus",
                        "market": "spot",
                        "status": "GAP_DETECTED",
                        "start_time_ms": 100,
                        "end_time_ms": 250,
                        "orders_replayed": 1,
                        "trades_replayed": 2,
                        "error": None,
                    }
                )
                self.assertEqual(trader._private_stream_ready_markets, {"perp"})
                self.assertIn("private_stream_recovery", trader._safe_mode_flags)
                self.assertFalse(
                    trader.state_reader.get_risk()["private_stream_recovery_ready"]
                )
                trader._on_execution_readiness(
                    {
                        "event": "ExecutionReadiness",
                        "status": "BLOCKED",
                        "reason": "spot open orders unavailable",
                        "event_time_ms": 252,
                    }
                )
                self.assertFalse(trader._rust_execution_ready)
                self.assertIn("rust_execution_readiness", trader._safe_mode_flags)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_telemetry_overflow_revokes_both_readiness_proofs_until_replay(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "testnet"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader._private_stream_ready_markets = {"spot", "perp"}
                trader._rust_execution_ready = True
                trader._safe_mode_flags.discard("private_stream_recovery")
                trader._safe_mode_flags.discard("rust_execution_readiness")

                trader._on_telemetry_gap(
                    {
                        "event": "TelemetryGap",
                        "skipped_messages": 17,
                        "reason": "broadcast_receiver_overflow",
                        "event_time_ms": 123456,
                    }
                )

                self.assertEqual(trader._private_stream_ready_markets, set())
                self.assertFalse(trader._rust_execution_ready)
                self.assertIn("private_stream_recovery", trader._safe_mode_flags)
                self.assertIn("rust_execution_readiness", trader._safe_mode_flags)
                risk = trader.state_reader.get_risk()
                self.assertTrue(risk["telemetry_gap_detected"])
                self.assertEqual(risk["telemetry_gap_skipped_messages"], 17)
                self.assertFalse(risk["private_stream_recovery_ready"])
                self.assertFalse(risk["rust_execution_ready"])

                for market in ("spot", "perp"):
                    trader._on_private_stream_status(
                        {
                            "event": "PrivateStreamStatus",
                            "market": market,
                            "status": "READY",
                            "start_time_ms": 123000,
                            "end_time_ms": 124000,
                            "orders_replayed": 1,
                            "trades_replayed": 1,
                            "error": None,
                        }
                    )
                trader._on_execution_readiness(
                    {
                        "event": "ExecutionReadiness",
                        "status": "READY",
                        "reason": "spot and futures exchange truth reconciled",
                        "event_time_ms": 124001,
                    }
                )
                recovered = trader.state_reader.get_risk()
                self.assertTrue(trader._rust_execution_ready)
                self.assertFalse(recovered["telemetry_gap_detected"])
                self.assertNotIn("private_stream_recovery", trader._safe_mode_flags)
                self.assertNotIn("rust_execution_readiness", trader._safe_mode_flags)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_runtime_ingests_full_exchange_statement_snapshot_idempotently(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                snapshot = {
                    "futures_income": [
                        {
                            "incomeType": "FUNDING_FEE",
                            "tranId": 101,
                            "time": 1_700_000_000_000,
                            "income": "1.25",
                            "asset": "USDT",
                            "symbol": "BTCUSDT",
                        },
                        {
                            "incomeType": "TRANSFER",
                            "tranId": 102,
                            "time": 1_700_000_000_001,
                            "income": "25",
                            "asset": "USDT",
                            "symbol": "",
                        },
                        {
                            "incomeType": "COMMISSION",
                            "tranId": 103,
                            "time": 1_700_000_000_002,
                            "income": "-0.20",
                            "asset": "USDT",
                            "symbol": "BTCUSDT",
                            "tradeId": 5001,
                        },
                    ],
                    "funding_income": [],
                    "margin_interest": [
                        {
                            "txId": 201,
                            "interestAccuredTime": 1_700_000_000_003,
                            "interest": "0.01",
                            "asset": "USDT",
                            "type": "PERIODIC",
                        }
                    ],
                    "margin_interest_status": "available",
                    "statement_history_status": {
                        "futures_income": "available",
                        "margin_interest": "available",
                    },
                    "snapshot_errors": {},
                }
                self.assertTrue(trader._ingest_exchange_statement_snapshot(snapshot))
                self.assertTrue(trader._ingest_exchange_statement_snapshot(snapshot))

                statements = trader.state_reader.get_exchange_statement_entries()
                self.assertEqual(len(statements), 4)
                self.assertEqual(
                    sorted(row["reconciliation_status"] for row in statements),
                    ["LEDGERED", "LEDGERED", "LEDGERED", "MATCH_REQUIRED"],
                )
                risk = trader.state_reader.get_risk()
                self.assertTrue(risk["exchange_statement_ingestion_ready"])
                self.assertEqual(risk["exchange_statement_duplicate_count"], 4)
                self.assertEqual(risk["exchange_statement_match_required_count"], 1)

                unknown_snapshot = dict(snapshot)
                unknown_snapshot["futures_income"] = [
                    {
                        "incomeType": "UNSUPPORTED_REBATE",
                        "tranId": 104,
                        "time": 1_700_000_000_004,
                        "income": "1",
                        "asset": "USDT",
                        "symbol": "BTCUSDT",
                    }
                ]
                self.assertFalse(
                    trader._ingest_exchange_statement_snapshot(unknown_snapshot)
                )
                self.assertEqual(
                    trader.state_reader.get_risk()["exchange_statement_unmapped_types"],
                    ["UNSUPPORTED_REBATE"],
                )
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_actual_fill_markout_is_durable_idempotent_and_measurement_only(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader.depth_tracker.on_l2depth(
                    "BTCUSDT",
                    "spot",
                    [(100.0, 10.0)],
                    [(102.0, 10.0)],
                )
                trader._queue_execution_markout(
                    symbol="BTCUSDT",
                    market="spot",
                    side="BUY",
                    fill_price=101.0,
                    filled_qty=1.0,
                    trade_id="trade-1",
                    order_id="order-1",
                    client_order_id="bngs_s_markout",
                    account_id="account-a",
                    commission=0.10,
                    commission_asset="USDT",
                    maker=True,
                    event_time="2026-01-01T00:00:00+00:00",
                )
                sample_id = next(iter(trader._pending_execution_markouts))
                trader._pending_execution_markouts[sample_id]["due_monotonic"] = 0.0
                trader.depth_tracker.on_l2depth(
                    "BTCUSDT",
                    "spot",
                    [(99.0, 10.0)],
                    [(101.0, 10.0)],
                )
                trader._drain_execution_markouts()

                rows = trader.state_reader.get_execution_quality(limit=10)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["sample_id"], sample_id)
                self.assertTrue(rows[0]["metadata"]["measurement_complete"])
                self.assertTrue(rows[0]["metadata"]["measurement_only"])
                self.assertEqual(trader.cost_calibrator.sample_count, 1)

                # A replay of the exchange fill must not schedule a second
                # horizon or create another economic measurement.
                trader._queue_execution_markout(
                    symbol="BTCUSDT",
                    market="spot",
                    side="BUY",
                    fill_price=101.0,
                    filled_qty=1.0,
                    trade_id="trade-1",
                    order_id="order-1",
                    client_order_id="bngs_s_markout",
                    account_id="account-a",
                    commission=0.10,
                    commission_asset="USDT",
                    maker=True,
                    event_time="2026-01-01T00:00:00+00:00",
                )
                self.assertFalse(trader._pending_execution_markouts)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_runtime_settling_window_written_on_boot(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                risk = trader.state_reader.get_risk()
                settling_until = datetime.fromisoformat(str(risk["runtime_settling_until_iso"]).replace("Z", "+00:00"))
                bot_started_at = datetime.fromisoformat(str(risk["bot_started_at"]).replace("Z", "+00:00"))

                self.assertAlmostEqual(float(risk["runtime_settling_seconds"]), 90.0, delta=0.1)
                self.assertGreater((settling_until - bot_started_at).total_seconds(), 80.0)
                self.assertLess((settling_until - bot_started_at).total_seconds(), 100.0)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_config_reload_persists_operator_flatten_request(self):
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
                    qty=1.0,
                    ann_funding=0.12,
                )
                trader._config.apply_updates(
                    {
                        "pause_new_entries": True,
                        "operator_flatten_all_request_id": "req-flat-1",
                        "operator_flatten_all_requested_at": "2026-01-01T00:00:00+00:00",
                        "operator_flatten_all_requested_by": "operator",
                    }
                )
                trader._on_config_reloaded(
                    {"operator_flatten_all_request_id": ("", "req-flat-1")},
                    trader._config.snapshot(),
                )

                risk = trader.state_reader.get_risk()
                self.assertEqual(risk["operator_flatten_all_request_id"], "req-flat-1")
                self.assertEqual(risk["operator_flatten_all_status"], "requested")
                self.assertEqual(risk["operator_flatten_all_requested_by"], "operator")
                self.assertEqual(risk["operator_flatten_all_remaining_symbols"], ["BTCUSDT"])
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_config_reload_acknowledges_manual_review_symbol(self):
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
                    qty=1.0,
                    hedge_ratio=0.0,
                    ann_funding=0.12,
                    recovery_state="manual_review",
                )
                trader._refresh_startup_recovery_flags(trader.state_reader.get_positions())
                self.assertIn("startup_manual_review", trader._safe_mode_flags)

                trader._config.apply_updates(
                    {
                        "startup_recovery_acknowledge_symbols": ["BTCUSDT"],
                    }
                )
                trader._on_config_reloaded(
                    {"startup_recovery_acknowledge_symbols": ([], ["BTCUSDT"])},
                    trader._config.snapshot(),
                )

                positions = trader.state_reader.get_positions()
                risk = trader.state_reader.get_risk()
                self.assertEqual(positions[0]["recovery_state"], "")
                self.assertNotIn("startup_manual_review", trader._safe_mode_flags)
                self.assertEqual(risk["startup_reconciliation_manual_review"], [])
                self.assertEqual(risk["startup_recovery_last_acknowledged_symbols"], ["BTCUSDT"])
                self.assertEqual(trader._config.get("startup_recovery_acknowledge_symbols"), [])
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_config_reload_resets_equity_high_watermark(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader._peak_account_equity = 12_500.0
                trader.state_writer.set_risk_snapshot({"account_equity": 10_250.0})
                trader.state_writer.flush()

                trader._config.apply_updates({"reset_equity_high_watermark": True})
                trader._on_config_reloaded(
                    {"reset_equity_high_watermark": (False, True)},
                    trader._config.snapshot(),
                )

                risk = trader.state_reader.get_risk()
                self.assertAlmostEqual(trader._peak_account_equity, 10_250.0)
                self.assertAlmostEqual(risk["account_equity_high_watermark"], 10_250.0)
                self.assertEqual(risk["account_equity_high_watermark_reset_source"], "live_config")
                self.assertFalse(trader._config.get("reset_equity_high_watermark"))
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_supervisor_acknowledgement_clears_manual_review_symbol(self):
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
                    qty=1.0,
                    hedge_ratio=0.0,
                    ann_funding=0.12,
                    recovery_state="manual_review",
                )
                trader._refresh_startup_recovery_flags(trader.state_reader.get_positions())
                trader.state_writer.set_risk_snapshot(
                    {
                        "startup_recovery_acknowledged_symbols": ["BTCUSDT"],
                        "startup_recovery_acknowledged_at": "2026-04-13T12:00:00+00:00",
                        "startup_recovery_acknowledged_by": "telegram:42",
                    }
                )
                trader.state_writer.flush()

                trader._consume_supervisor_startup_recovery_acknowledgements()

                positions = trader.state_reader.get_positions()
                risk = trader.state_reader.get_risk()
                self.assertEqual(positions[0]["recovery_state"], "")
                self.assertEqual(risk["startup_recovery_acknowledged_symbols"], [])
                self.assertEqual(risk["startup_recovery_last_acknowledged_symbols"], ["BTCUSDT"])
                self.assertEqual(risk["startup_recovery_last_acknowledged_by"], "telegram:42")
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_telemetry_health_tolerates_brief_reconnect_gap(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader.subscriber._connected_event.clear()
                trader._last_telemetry_event_monotonic = time.monotonic() - 1.0

                self.assertTrue(trader._telemetry_stream_healthy())

                stale_age = float(trader._config.get("max_runtime_staleness_seconds")) + 5.0
                trader._last_telemetry_event_monotonic = time.monotonic() - stale_age

                self.assertFalse(trader._telemetry_stream_healthy())
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_risk_controls_use_one_sided_gross_exposure_and_publish_stress_metrics(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                now_monotonic = time.monotonic()
                for index, symbol in enumerate(("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"), start=1):
                    trader.state_writer.upsert_position(
                        symbol=symbol,
                        side="LONG_SPOT_SHORT_PERP",
                        direction="long",
                        spot_entry=100.0,
                        perp_entry=100.0,
                        spot_live=100.0,
                        perp_live=100.0,
                        qty=50.0,
                        ann_funding=0.12 + index * 0.01,
                    )
                    trader._mark_price_updated_monotonic[symbol] = now_monotonic
                    trader.funding_ranker.update_rate(symbol, 0.12 + index * 0.01)

                rows = trader.state_reader.get_positions()
                trader._evaluate_risk_controls(rows)

                risk = trader.state_reader.get_risk()
                self.assertAlmostEqual(float(risk["gross_exposure"]), 20_000.0)
                expected_denominator = max(
                    float(risk["gross_exposure"]),
                    trader._risk_engine.limits.max_gross_exposure_usd,
                )
                self.assertAlmostEqual(
                    float(risk["symbol_concentration"]),
                    float(risk["largest_symbol_gross_exposure"]) / expected_denominator,
                )
                self.assertEqual(risk["gross_exposure_convention"], "one_sided")
                self.assertIn("liquidity_adjusted_open_pnl_usd", risk)
                self.assertIn("survival_margin_buffer_usd", risk)
                self.assertLessEqual(
                    float(risk["liquidity_adjusted_open_pnl_usd"]),
                    float(risk["mark_to_market_open_pnl_usd"]),
                )
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_drawdown_excludes_manual_review_mark_to_market(self):
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
                trader._latest_exchange_account_equity = 12_000.0
                trader._peak_account_equity = 12_000.0
                trader._mark_price_updated_monotonic["ATAUSDT"] = time.monotonic()
                trader.funding_ranker.update_rate("ATAUSDT", 0.10)
                trader.state_writer.upsert_position(
                    symbol="ATAUSDT",
                    side="SHORT_SPOT_LONG_PERP",
                    direction="short",
                    spot_entry=0.00928,
                    perp_entry=0.00951,
                    spot_live=0.01030,
                    perp_live=0.01030,
                    qty=2_424_564.0,
                    hedge_ratio=0.0,
                    ann_funding=0.1095,
                    net_pnl_usd=2_000.0,
                    exchange_pnl_usd=2_000.0,
                    recovery_state="manual_review",
                )

                decision = trader._evaluate_risk_controls(trader.state_reader.get_positions())
                risk = trader.state_reader.get_risk()

                self.assertTrue(decision.kill_switch)
                self.assertAlmostEqual(float(risk["account_equity"]), 10_000.0, places=6)
                self.assertAlmostEqual(float(risk["account_equity_mark_to_market"]), 12_000.0, places=6)
                self.assertAlmostEqual(float(risk["account_equity_excludes_manual_review_usd"]), 2_000.0, places=6)
                self.assertGreater(float(risk["drawdown_pct"]), 0.16)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_estimate_account_equity_uses_cached_exchange_basis_without_recursive_decay(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "live"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader._latest_exchange_account_equity = 10_000.0
                rows = [{"symbol": "BTCUSDT", "net_pnl_usd": 100.0}]

                self.assertAlmostEqual(
                    trader._estimate_account_equity(rows, open_pnl_override=100.0),
                    10_000.0,
                )
                self.assertAlmostEqual(
                    trader._estimate_account_equity(rows, open_pnl_override=80.0),
                    9_980.0,
                )
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_estimate_account_equity_falls_back_to_prior_mark_to_market_basis(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "live"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader.state_writer.set_risk_snapshot(
                    {
                        "account_equity": 4_789.14,
                        "account_equity_mark_to_market": 4_810.0,
                        "mark_to_market_open_pnl_usd": -40.0,
                    }
                )
                trader.state_writer.flush()
                rows = [{"symbol": "BTCUSDT", "net_pnl_usd": -10.0}]

                self.assertAlmostEqual(
                    trader._estimate_account_equity(rows, open_pnl_override=-25.0),
                    4_825.0,
                )
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_var_sizing_and_correlation_gate_use_basis_history(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                basis_returns = [0.05, -0.05] * 15
                trader._basis_returns["BTCUSDT"] = deque(basis_returns, maxlen=120)
                trader._basis_returns["ETHUSDT"] = deque(basis_returns, maxlen=120)
                sized_notional = trader._var_sized_notional("BTCUSDT", 5_000.0)
                self.assertLess(sized_notional, 5_000.0)

                blocked = trader._correlation_gate_blocked(
                    [("ETHUSDT", 0.20)],
                    [OpenPosition(symbol="BTCUSDT", notional_usd=5_000.0, ann_funding=0.15)],
                )
                self.assertIn("ETHUSDT", blocked)
                self.assertIn("correlation", blocked["ETHUSDT"][0])
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

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
                self.assertEqual(borrow_cost_usd, 0.0)
                self.assertAlmostEqual(net_pnl, 0.1, places=6)
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

    def test_testnet_reuses_shared_demo_key_and_demo_endpoints(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(
            os.environ,
            {
                "TRADING_MODE": "testnet",
                "BINANCE_API_KEY": "",
                "BINANCE_API_SECRET": "",
                "BINANCE_SPOT_API_KEY": "shared-key",
                "BINANCE_SPOT_API_SECRET": "shared-secret",
            },
            clear=False,
        ):
            trader = self._build_trader(db_name)
            try:
                self.assertEqual(trader._futures_api_key, "shared-key")
                self.assertEqual(trader._futures_api_secret, "shared-secret")
                self.assertEqual(trader._spot_api_key, "shared-key")
                self.assertEqual(trader._spot_api_secret, "shared-secret")
                self.assertEqual(trader._futures_base_url, "https://demo-fapi.binance.com")
                self.assertEqual(trader._spot_base_url, "https://demo-api.binance.com")
                trader._validate_required_credentials()
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_testnet_disables_bybit_cross_validation(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "testnet"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                self.assertFalse(trader._cross_validation_enabled())
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
                    "intent_id": "intent-entry-funding",
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

    def test_entry_fill_failure_keeps_symbol_pending_and_blocks_new_risk(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader._pending_enters["BTCUSDT"] = {
                    "intent_id": "intent-btc-fill",
                    "entry_time": "2026-01-01T00:00:00+00:00",
                    "entry_price": 100.0,
                    "qty": 2.0,
                    "direction": "long",
                    "ann_funding": 0.245,
                    "estimated_entry_cost_usd": 1.5,
                }

                with patch.object(
                    trader.state_writer,
                    "upsert_position",
                    side_effect=sqlite3.OperationalError("table positions has no column named direction"),
                ):
                    trader._on_order_update(
                        "BTCUSDT",
                        "FILLED",
                        filled_qty=2.0,
                        avg_fill_price=101.0,
                        execution_type="FILLED_CYCLE",
                        event_time="2026-01-01T00:05:00+00:00",
                        spot_fill_price=101.0,
                        perp_fill_price=101.0,
                    )

                self.assertIn("BTCUSDT", trader._pending_enters)
                self.assertEqual(trader.state_reader.get_positions(), [])
                self.assertIn("state_store_write", trader._safe_mode_flags)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_late_entry_fill_clears_after_stale_fill_when_no_other_entries_remain(self):
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
                self.assertEqual(risk["runtime_mode"], "LIVE")
                self.assertEqual(risk["safe_mode_reason"], "")
                self.assertNotIn("late_entry_fill", trader._safe_mode_flags)
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
                self.assertNotIn("skip_spot_leg", payload)
                self.assertNotIn("skip_perp_leg", payload)

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
                trader._config._values["per_symbol_notional_cap_usd"] = 10_000.0
                trader.depth_tracker.set_rest_snapshot(
                    "BTCUSDT",
                    spot_depth_usd=1_000_000.0,
                    perp_depth_usd=1_000_000.0,
                    spot_bid_price=100.0,
                    spot_ask_price=100.0,
                    perp_bid_price=100.0,
                    perp_ask_price=100.0,
                )
                self._seed_authoritative_funding(trader)

                with patch.object(trader.execution, "send_order_intent", return_value=True) as send_mock:
                    trader._dispatch_enter(
                        "BTCUSDT",
                        notional_usd=5_000.0,
                        direction="long",
                        ann_funding=25.0,
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
                    "intent_id": "intent-entry-leg-cycle",
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

    def test_exit_cycle_missing_required_perp_fill_keeps_position_for_reconciliation(self):
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
                    qty=0.0001,
                    hedge_ratio=1.0,
                    ann_funding=0.12,
                )

                with patch.object(
                    trader.execution,
                    "send_order_intent",
                    return_value=True,
                ) as send_mock:
                    trader._dispatch_exit("BTCUSDT", urgency=1.0, direction="long")

                payload = send_mock.call_args.args[0]
                trader._on_order_update(
                    "BTCUSDT",
                    "FILLED",
                    execution_type="FILLED_CYCLE",
                    intent_id=payload["intent_id"],
                    spot_fill_price=100.0,
                    perp_fill_price=0.0,
                )

                positions = trader.state_reader.get_positions()
                risk = trader.state_reader.get_risk()
                self.assertEqual(len(positions), 1)
                self.assertEqual(positions[0]["symbol"], "BTCUSDT")
                self.assertIn("BTCUSDT", trader._pending_exit_intents)
                self.assertIn("execution_reconciliation", trader._safe_mode_flags)
                self.assertTrue(risk["execution_reconciliation_required"])
                self.assertEqual(
                    risk["execution_reconciliation_issue"]["missing_legs"],
                    ["perp"],
                )
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
                self._mark_funding_metadata_ready(trader)
                self.assertIsNone(trader._external_entry_block_reason())
                trader.state_writer.set_risk("kill_switch", "true")
                self.assertEqual(trader._external_entry_block_reason(), "kill switch active")
                trader.state_writer.set_risk("kill_switch", "false")
                trader._risk_allow_new_risk = False
                self.assertEqual(
                    trader._external_entry_block_reason(),
                    "risk engine blocked new exposure",
                )
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_validation_no_go_and_hedge_gap_block_new_entries(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "testnet"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader._preflight_status = "passed"
                self._mark_config_consensus_ready(trader)
                trader.state_writer.set_risk_snapshot(
                    {
                        "validation_go_no_go": "NO_GO",
                        "validation_status": "FAILING",
                    }
                )
                trader.state_writer.flush()

                self.assertEqual(
                    trader._external_entry_block_reason(),
                    "validation not GO (NO_GO)",
                )

                trader.state_writer.set_risk_snapshot(
                    {
                        "validation_go_no_go": "GO",
                        "validation_status": "PASSING",
                        "hedge_gap_symbols": ["CATIUSDT"],
                    }
                )
                trader.state_writer.flush()

                self.assertEqual(
                    trader._external_entry_block_reason(),
                    "hedge gap active (CATIUSDT)",
                )
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_validation_adjust_auto_scales_without_blocking_entries(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "testnet"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader._preflight_status = "passed"
                self._mark_funding_metadata_ready(trader)
                self._mark_config_consensus_ready(trader)
                self._mark_private_streams_ready(trader)
                self._mark_rust_execution_ready(trader)
                # Validation-policy behavior is evaluated only after the
                # independent account-reconciliation gate has passed.
                trader._account_reconciliation_ready = True
                trader.state_writer.set_risk_snapshot(
                    {
                        "validation_go_no_go": "ADJUST",
                        "validation_status": "MONITORING",
                        "validation_blockers": [
                            "Observation window only 2.0d; need 14d",
                            "Clean run only 2.0d; need 14d",
                        ],
                    }
                )
                trader.state_writer.flush()

                self.assertIsNone(trader._external_entry_block_reason())
                self.assertAlmostEqual(trader._effective_notional_scale(), 0.50)

                trader._persist_runtime_state()
                risk = trader.state_reader.get_risk()
                self.assertTrue(risk["allow_new_risk"])
                self.assertEqual(risk["entry_block_reason"], "")
                self.assertEqual(risk["validation_entry_policy"], "auto_adjust")
                self.assertEqual(
                    risk["validation_adjustment_action"],
                    "collect_more_evidence_at_reduced_size",
                )
                self.assertAlmostEqual(float(risk["validation_position_scale"]), 0.50)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_validation_adjust_drawdown_uses_stronger_size_haircut(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "testnet"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader._preflight_status = "passed"
                trader.state_writer.set_risk_snapshot(
                    {
                        "validation_go_no_go": "ADJUST",
                        "validation_status": "MONITORING",
                        "validation_blockers": [
                            "Max drawdown 12.00% above GO target 10.00%",
                        ],
                    }
                )
                trader.state_writer.flush()

                policy = trader._validation_policy_snapshot()
                self.assertIsNone(policy["entry_block_reason"])
                self.assertEqual(
                    policy["validation_adjustment_action"],
                    "reduce_exposure_and_resize_smaller",
                )
                self.assertAlmostEqual(
                    float(str(policy["validation_position_scale"])), 0.25
                )
                self.assertAlmostEqual(trader._effective_notional_scale(), 0.25)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_entry_safety_rejects_stale_orderbook_data(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader.depth_tracker.on_l2depth(
                    "BTCUSDT",
                    "spot",
                    [(99.99, 2_000.0)],
                    [(100.01, 2_000.0)],
                )
                trader.depth_tracker.on_l2depth(
                    "BTCUSDT",
                    "perp",
                    [(100.19, 2_000.0)],
                    [(100.21, 2_000.0)],
                )
                depth = trader.depth_tracker._depths["BTCUSDT"]
                depth.spot_updated = time.time() - 120.0

                allowed, reasons, metrics = trader._entry_safety_decision(
                    "BTCUSDT",
                    1_000.0,
                    1.0,
                )

                self.assertFalse(allowed)
                self.assertTrue(any("stale orderbook data" in reason for reason in reasons))
                self.assertGreater(metrics["data_age_s"], 30.0)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_entry_safety_rejects_unprofitable_trade_after_costs(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader.depth_tracker.on_l2depth(
                    "BTCUSDT",
                    "spot",
                    [(99.99, 2_000.0)],
                    [(100.01, 2_000.0)],
                )
                trader.depth_tracker.on_l2depth(
                    "BTCUSDT",
                    "perp",
                    [(100.19, 2_000.0)],
                    [(100.21, 2_000.0)],
                )
                observed_at = datetime.now(timezone.utc)
                trader.funding_ranker.calendar.update_funding_info(
                    [{"symbol": "BTCUSDT", "fundingIntervalHours": 8}],
                    observed_at=observed_at,
                )
                trader.funding_ranker._last_funding_info_refresh = observed_at
                next_settlement = observed_at + timedelta(hours=1)
                trader.funding_ranker.update_rate(
                    "BTCUSDT",
                    0.02 / (365.0 * 3.0),
                    next_funding_time_ms=int(next_settlement.timestamp() * 1_000),
                )

                allowed, reasons, metrics = trader._entry_safety_decision(
                    "BTCUSDT",
                    1_000.0,
                    0.02,
                )

                self.assertFalse(allowed)
                self.assertTrue(any("expected net edge" in reason for reason in reasons))
                self.assertLess(metrics["predicted_net_edge_bps"], metrics["min_required_edge_bps"])
                # A payment one hour away is still one full discrete payment;
                # it is not reduced to one eighth of an eight-hour interval.
                self.assertAlmostEqual(
                    float(metrics["gross_funding_edge_bps"]),
                    (0.02 / (365.0 * 3.0)) * 10_000.0,
                )
                self.assertEqual(metrics["settlement_count"], 1)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_dispatch_enter_blocks_projected_gross_exposure_limit(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader._mark_prices["BTCUSDT"] = 100.0
                trader._lot_step["BTCUSDT"] = 0.001
                trader._current_gross_exposure_usd = 9_500.0
                trader._risk_engine.limits.max_gross_exposure_usd = 10_000.0

                with patch.object(trader.execution, "send_order_intent", return_value=True) as send_mock:
                    trader._dispatch_enter(
                        "BTCUSDT",
                        1_000.0,
                        direction="long",
                        ann_funding=1.0,
                    )

                send_mock.assert_not_called()
                self.assertNotIn("BTCUSDT", trader._pending_enters)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_dispatch_exit_marks_naked_manual_review_unwind_as_single_leg(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader.state_writer.upsert_position(
                    symbol="PHBUSDT",
                    side="LONG_SPOT_SHORT_PERP",
                    direction="long",
                    spot_entry=0.09,
                    perp_entry=0.09,
                    qty=51_538.0,
                    hedge_ratio=0.0,
                    ann_funding=0.12,
                    recovery_state="manual_review",
                )

                with patch.object(trader.execution, "send_order_intent", return_value=True) as send_mock:
                    trader._dispatch_exit("PHBUSDT", urgency=1.0, direction="long")

                payload = send_mock.call_args.args[0]
                self.assertTrue(payload["skip_spot_leg"])
                self.assertNotIn("skip_perp_leg", payload)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_dispatch_exit_uses_partial_spot_quantity_for_tracked_hedge_gap(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader.state_writer.upsert_position(
                    symbol="GTCUSDT",
                    side="LONG_SPOT_SHORT_PERP",
                    direction="long",
                    spot_entry=0.105,
                    perp_entry=0.106,
                    qty=23149.7,
                    hedge_ratio=0.7494459280249852,
                    ann_funding=0.12,
                    recovery_state="tracked",
                )

                with patch.object(trader.execution, "send_order_intent", return_value=True) as send_mock:
                    trader._dispatch_exit("GTCUSDT", urgency=1.0, direction="long")

                payload = send_mock.call_args.args[0]
                self.assertEqual(payload["intent"], "EXIT_LONG")
                self.assertAlmostEqual(float(payload["quantity"]), 23149.7)
                self.assertAlmostEqual(float(payload["perp_quantity"]), 23149.7)
                self.assertAlmostEqual(
                    float(payload["spot_quantity"]),
                    23149.7 * 0.7494459280249852,
                )
                self.assertNotIn("skip_spot_leg", payload)
                self.assertNotIn("skip_perp_leg", payload)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_exit_leg_skip_flags_for_unsupported_short_manual_review_orphan(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader.state_writer.upsert_position(
                    symbol="ATAUSDT",
                    side="SHORT_SPOT_LONG_PERP",
                    direction="short",
                    spot_entry=0.1,
                    perp_entry=0.1,
                    qty=2_424_564.0,
                    hedge_ratio=0.0,
                    ann_funding=-0.12,
                    recovery_state="manual_review",
                )

                skip_spot, skip_perp = trader._exit_leg_skip_flags(
                    "ATAUSDT",
                    direction="short",
                )

                self.assertTrue(skip_spot)
                self.assertFalse(skip_perp)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_dispatch_exit_short_manual_review_orphan_skips_spot_leg(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader.state_writer.upsert_position(
                    symbol="ATAUSDT",
                    side="SHORT_SPOT_LONG_PERP",
                    direction="short",
                    spot_entry=0.1,
                    perp_entry=0.1,
                    qty=2_424_564.0,
                    hedge_ratio=0.0,
                    ann_funding=-0.12,
                    recovery_state="manual_review",
                )

                with patch.object(trader.execution, "send_order_intent", return_value=True) as send_mock:
                    trader._dispatch_exit("ATAUSDT", urgency=1.0, direction="short")

                payload = send_mock.call_args.args[0]
                self.assertEqual(payload["intent"], "EXIT_SHORT")
                self.assertTrue(payload["skip_spot_leg"])
                self.assertNotIn("skip_perp_leg", payload)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_startup_manual_review_only_blocks_the_affected_symbol(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader._preflight_status = "passed"
                self._mark_funding_metadata_ready(trader)
                trader._startup_manual_review_symbols["PHBUSDT"] = "PHBUSDT requires startup manual review"
                trader._set_safe_mode_flag("startup_manual_review", True)
                trader._persist_runtime_state()

                risk = trader.state_reader.get_risk()
                self.assertEqual(trader._runtime_mode, "LIVE_WITH_SYMBOL_BLOCKS")
                self.assertIsNone(trader._entry_policy_block_reason())
                self.assertEqual(trader._blocked_entry_symbols(), {"PHBUSDT"})
                self.assertEqual(
                    trader._describe_symbol_block("PHBUSDT"),
                    "PHBUSDT requires startup manual review",
                )
                self.assertTrue(risk["allow_new_risk"])
                self.assertEqual(risk["runtime_mode"], "LIVE_WITH_SYMBOL_BLOCKS")
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_orphan_alone_does_not_trigger_safe_mode(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader._preflight_status = "passed"
                self._mark_funding_metadata_ready(trader)
                trader.state_writer.upsert_position(
                    symbol="ATAUSDT",
                    side="SHORT_SPOT_LONG_PERP",
                    direction="short",
                    spot_entry=0.1,
                    perp_entry=0.1,
                    qty=2_424_564.0,
                    hedge_ratio=0.0,
                    ann_funding=-0.12,
                    recovery_state="manual_review",
                )

                trader._refresh_startup_recovery_flags(trader.state_reader.get_positions())
                trader._persist_runtime_state()

                risk = trader.state_reader.get_risk()
                self.assertEqual(trader._runtime_mode, "LIVE_WITH_SYMBOL_BLOCKS")
                self.assertEqual(risk["runtime_mode"], "LIVE_WITH_SYMBOL_BLOCKS")
                self.assertTrue(risk["allow_new_risk"])
                self.assertIn("startup_manual_review", trader._safe_mode_flags)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_hwm_auto_decay_heals_risk_limits(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader._peak_account_equity = 15_000.0
                trader._config._values["hwm_auto_decay_after_hours"] = 1.0
                trader._config._values["hwm_auto_decay_fraction"] = 1.0
                trader._last_hwm_auto_decay_check_monotonic = 1.0

                with patch("scripts.live_trader_v2.time.monotonic", return_value=7_200.0):
                    decision = trader._evaluate_risk_controls([])

                risk = trader.state_reader.get_risk()
                self.assertFalse(decision.kill_switch)
                self.assertAlmostEqual(trader._peak_account_equity, 10_000.0)
                self.assertAlmostEqual(float(risk["account_equity_high_watermark"]), 10_000.0)
                self.assertEqual(risk["account_equity_high_watermark_reset_source"], "auto_decay")
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_hwm_auto_decay_disabled_by_default(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader._peak_account_equity = 15_000.0
                trader._last_hwm_auto_decay_check_monotonic = time.monotonic() - 7_200.0

                decision = trader._evaluate_risk_controls([])

                self.assertTrue(decision.kill_switch)
                self.assertAlmostEqual(trader._peak_account_equity, 15_000.0)
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

    def test_live_startup_refuses_missing_required_risk_config(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "testnet"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                try:
                    trader._validate_live_config_for_startup()
                except RuntimeError as exc:
                    self.assertIn("live_config missing required live risk key", str(exc))
                    self.assertIn("account_equity_usd", str(exc))
                else:
                    raise AssertionError("testnet startup should fail closed without required config keys")
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

    async def test_trading_loop_killswitch_dispatches_single_exit_per_symbol(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader._runtime_mode = "LIVE"
                trader.subscriber._connected_event.set()
                rows = [
                    {
                        "symbol": "BTCUSDT",
                        "qty": 1.0,
                        "spot_live": 100.0,
                        "perp_live": 100.0,
                        "net_pnl_usd": 0.0,
                        "recovery_state": "tracked",
                    }
                ]
                open_positions = [
                    scripts.live_trader_v2.OpenPosition(
                        symbol="BTCUSDT",
                        notional_usd=100.0,
                        ann_funding=0.12,
                    )
                ]
                risk_decision = scripts.live_trader_v2.RiskDecision(
                    allow_new_risk=False,
                    derisk_required=True,
                    kill_switch=True,
                    position_scale=1.0,
                    reasons=["max drawdown breached"],
                )

                with patch.object(trader, "_sync_rest_depth_to_tracker", new=AsyncMock()), patch.object(
                    trader._config,
                    "reload_now",
                ), patch.object(
                    trader,
                    "_consume_supervisor_startup_recovery_acknowledgements",
                ), patch.object(
                    trader,
                    "_refresh_open_position_metrics",
                    return_value=rows,
                ), patch.object(
                    trader,
                    "_maybe_process_operator_flatten_all_request",
                    return_value=False,
                ), patch.object(
                    trader,
                    "_dispatch_startup_recovery_exits",
                    return_value=0,
                ) as startup_dispatch, patch.object(
                    trader,
                    "_get_open_positions",
                    return_value=open_positions,
                ), patch.object(
                    trader,
                    "_expire_stale_pending_intents",
                ), patch.object(
                    trader,
                    "_evaluate_risk_controls",
                    return_value=risk_decision,
                ), patch.object(
                    trader,
                    "_sleep_or_shutdown",
                    new=AsyncMock(return_value=True),
                ), patch.object(
                    trader,
                    "_dispatch_exit",
                ) as dispatch_exit:
                    await trader._trading_loop()

                startup_dispatch.assert_called_once_with(rows)
                dispatch_exit.assert_called_once_with("BTCUSDT", urgency=1.0, direction="long")
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
                self.assertGreaterEqual(len(sent_heartbeats), 1)
                self.assertEqual(trader._last_heartbeat_ack_id, sent_heartbeats[-1])
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    async def test_run_preflight_uses_extended_startup_heartbeat_timeout(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                with patch.object(trader, "_db_write_probe", new=AsyncMock()) as db_probe_mock:
                    with patch.object(
                        trader,
                        "_wait_for_heartbeat_ack_once",
                        new=AsyncMock(return_value=True),
                    ) as heartbeat_mock:
                        await trader._run_preflight()

                db_probe_mock.assert_awaited_once()
                heartbeat_mock.assert_awaited_once_with(
                    timeout_s=scripts.live_trader_v2._STARTUP_HEARTBEAT_TIMEOUT_S
                )
                self.assertEqual(trader._preflight_status, "passed")
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    async def test_paper_startup_restores_positions_without_recording_cancelled_trades(self):
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

                positions = trader.state_reader.get_positions()
                self.assertEqual(len(positions), 1)
                self.assertEqual(positions[0]["symbol"], "DOGEUSDT")
                self.assertEqual(trader._position_directions["DOGEUSDT"], "short")
                self.assertEqual(
                    trader.state_reader.get_risk()["startup_reconciliation_status"],
                    "paper_restored",
                )
                self.assertEqual(trader.state_reader.get_trades(limit=10), [])
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    async def test_startup_reconciliation_failure_does_not_crash(self):
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
                with patch.object(
                    trader,
                    "_reconcile_live_startup_state",
                    new=AsyncMock(side_effect=RuntimeError("simulated startup reconcile error")),
                ), patch.object(
                    trader,
                    "_sync_positions_to_execution_engine",
                    return_value=0,
                ):
                    await trader._on_startup()

                risk = trader.state_reader.get_risk()
                self.assertIn("startup_reconciliation_failed", trader._safe_mode_flags)
                self.assertEqual(trader._runtime_mode, "SAFE_MODE")
                self.assertEqual(risk["startup_reconciliation_status"], "failed")
                self.assertIn("simulated startup reconcile error", risk["startup_reconciliation_error"])
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
                trader.funding_ranker.get_rate = lambda symbol: 0.05
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

    def test_depth_gap_block_auto_clears_only_after_both_markets_recover(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader._set_symbol_safe_mode_reason("BTCUSDT", "position_divergence", True)
                trader._handle_feed_gap(
                    {
                        "symbol": "BTCUSDT",
                        "market": "perp",
                        "last_update_id": 10,
                        "final_update_id": 12,
                    }
                )
                self.assertIn("BTCUSDT", trader._symbol_safe_mode_blocks)
                self.assertIn(
                    "depth_sequence_gap",
                    trader._symbol_safe_mode_reasons["BTCUSDT"],
                )

                trader._handle_sequenced_depth_event(
                    {
                        "symbol": "BTCUSDT",
                        "market": "perp",
                        "final_update_id": 20,
                        "sequence_contiguous": True,
                    }
                )
                self.assertIn(
                    "depth_sequence_gap",
                    trader._symbol_safe_mode_reasons["BTCUSDT"],
                )
                trader._handle_sequenced_depth_event(
                    {
                        "symbol": "BTCUSDT",
                        "market": "spot",
                        "final_update_id": 30,
                        "sequence_contiguous": True,
                    }
                )
                self.assertNotIn(
                    "depth_sequence_gap",
                    trader._symbol_safe_mode_reasons["BTCUSDT"],
                )
                # Feed recovery cannot accidentally clear a financial-state
                # incident owned by another recovery recipe.
                self.assertIn("position_divergence", trader._symbol_safe_mode_reasons["BTCUSDT"])
                self.assertIn("BTCUSDT", trader._symbol_safe_mode_blocks)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_inverse_entry_requires_fresh_symbol_borrow_availability(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader._mark_prices["BTCUSDT"] = 100.0
                trader._lot_step["BTCUSDT"] = 0.001
                trader._config._values["per_symbol_notional_cap_usd"] = 10_000.0
                trader._record_spot_borrow_availability(
                    "BTCUSDT",
                    10_000.0,
                    observed_at=datetime.now(timezone.utc) - timedelta(seconds=61),
                )
                metrics = {
                    "predicted_net_edge_bps": 100.0,
                    "round_trip_cost_bps": 1.0,
                    "spread_bps": 0.0,
                    "entry_depth_usd": 1_000_000.0,
                    "data_age_s": 0.0,
                    "expected_holding_hours": 8.0,
                    "maker_fill_probability": 1.0,
                    "max_slippage_bps": 5.0,
                }

                with patch.object(
                    trader,
                    "_entry_safety_decision",
                    return_value=(True, [], metrics),
                ), patch.object(
                    trader.execution, "send_order_intent", return_value=True
                ) as send_mock:
                    trader._dispatch_enter(
                        "BTCUSDT",
                        2_500.0,
                        direction="short",
                        ann_funding=-25.0,
                    )
                    send_mock.assert_not_called()
                    rejection = trader.state_reader.get_risk()[
                        "last_capital_reservation_reject"
                    ]
                    self.assertEqual(rejection["reasons"], ["spot_borrow_availability"])

                    trader._record_spot_borrow_availability("BTCUSDT", 1_250.0)
                    trader._dispatch_enter(
                        "BTCUSDT",
                        2_500.0,
                        direction="short",
                        ann_funding=-25.0,
                    )

                send_mock.assert_called_once()
                payload = send_mock.call_args.args[0]
                self.assertEqual(payload["intent"], "ENTER_SHORT")
                reservation = trader.capital_reservations.active()[0]
                self.assertEqual(reservation["spot_cash_usd"], "0.0")
                self.assertEqual(reservation["spot_borrow_usd"], "1250.0")
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    async def test_entry_only_risk_block_does_not_suppress_economic_exit(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader._runtime_mode = "LIVE"
                trader.subscriber._connected_event.set()
                rows = [
                    {
                        "symbol": "BTCUSDT",
                        "qty": 1.0,
                        "spot_live": 100.0,
                        "perp_live": 100.0,
                        "net_pnl_usd": 0.0,
                        "recovery_state": "tracked",
                    }
                ]
                open_positions = [
                    scripts.live_trader_v2.OpenPosition(
                        symbol="BTCUSDT",
                        notional_usd=100.0,
                        ann_funding=-0.01,
                    )
                ]
                risk_decision = scripts.live_trader_v2.RiskDecision(
                    allow_new_risk=False,
                    derisk_required=False,
                    kill_switch=False,
                    position_scale=1.0,
                    reasons=["entry data readiness unavailable"],
                )

                with patch.object(trader, "_sync_rest_depth_to_tracker", new=AsyncMock()), patch.object(
                    trader._config,
                    "reload_now",
                ), patch.object(
                    trader,
                    "_consume_supervisor_startup_recovery_acknowledgements",
                ), patch.object(
                    trader,
                    "_refresh_open_position_metrics",
                    return_value=rows,
                ), patch.object(
                    trader,
                    "_maybe_process_operator_flatten_all_request",
                    return_value=False,
                ), patch.object(
                    trader,
                    "_dispatch_startup_recovery_exits",
                    return_value=0,
                ), patch.object(
                    trader,
                    "_get_open_positions",
                    return_value=open_positions,
                ), patch.object(
                    trader,
                    "_expire_stale_pending_intents",
                ), patch.object(
                    trader,
                    "_evaluate_risk_controls",
                    return_value=risk_decision,
                ), patch.object(
                    trader,
                    "_minutes_since_last_snapshot",
                    return_value=10.0,
                ), patch.object(
                    trader,
                    "_sleep_or_shutdown",
                    new=AsyncMock(return_value=True),
                ), patch.object(
                    trader,
                    "_dispatch_exit",
                ) as dispatch_exit:
                    await trader._trading_loop()

                dispatch_exit.assert_called_once_with(
                    "BTCUSDT",
                    urgency=1.0,
                    direction="long",
                )
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_live_equity_fallback_excludes_modeled_and_incomplete_pnl(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "live"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                now = datetime.now(timezone.utc).isoformat()
                for symbol, pnl, status in (
                    ("BTCUSDT", 25.0, "RECONCILED"),
                    ("ETHUSDT", 500.0, "MODELED"),
                    ("SOLUSDT", 750.0, "INCOMPLETE"),
                ):
                    trader.state_writer.record_trade(
                        Trade(
                            symbol=symbol,
                            side="LONG_SPOT_SHORT_PERP",
                            entry_time=now,
                            exit_time=now,
                            entry_price=100.0,
                            exit_price=100.0,
                            qty=1.0,
                            net_pnl_usd=pnl,
                            economic_status=status,
                        )
                    )
                trader.state_writer.flush()

                self.assertAlmostEqual(trader._estimate_account_equity([]), 10_025.0)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_paper_equity_fallback_includes_modeled_but_not_incomplete_pnl(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                now = datetime.now(timezone.utc).isoformat()
                for symbol, pnl, status in (
                    ("BTCUSDT", 25.0, "RECONCILED"),
                    ("ETHUSDT", 50.0, "MODELED"),
                    ("SOLUSDT", 750.0, "INCOMPLETE"),
                ):
                    trader.state_writer.record_trade(
                        Trade(
                            symbol=symbol,
                            side="LONG_SPOT_SHORT_PERP",
                            entry_time=now,
                            exit_time=now,
                            entry_price=100.0,
                            exit_price=100.0,
                            qty=1.0,
                            net_pnl_usd=pnl,
                            economic_status=status,
                        )
                    )
                trader.state_writer.flush()

                self.assertAlmostEqual(trader._estimate_account_equity([]), 10_075.0)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_dispatch_enter_persists_decision_and_capital_reservation_before_send(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader._mark_prices["BTCUSDT"] = 100.0
                trader._lot_step["BTCUSDT"] = 0.001
                trader._config._values["per_symbol_notional_cap_usd"] = 10_000.0
                trader.depth_tracker.set_rest_snapshot(
                    "BTCUSDT",
                    spot_depth_usd=1_000_000.0,
                    perp_depth_usd=1_000_000.0,
                    spot_bid_price=100.0,
                    spot_ask_price=100.0,
                    perp_bid_price=100.0,
                    perp_ask_price=100.0,
                )
                self._seed_authoritative_funding(trader)

                with patch.object(trader.execution, "send_order_intent", return_value=True) as send:
                    trader._dispatch_enter(
                        "BTCUSDT",
                        2_500.0,
                        ann_funding=25.0,
                        cycle_id="cycle-reserved-entry",
                    )

                pending = trader.state_reader.get_pending_intents(statuses=["PENDING_ACK"])
                self.assertEqual(len(pending), 1)
                metadata = pending[0]["metadata"]
                decision = trader.state_reader.get_execution_decision(metadata["decision_id"])
                self.assertIsNotNone(decision)
                assert decision is not None
                self.assertTrue(decision["accepted"])
                self.assertEqual(decision["cycle_id"], "cycle-reserved-entry")
                self.assertEqual(
                    decision["decision_payload"]["payload"][
                        "opportunity_kernel_version"
                    ],
                    OPPORTUNITY_KERNEL_VERSION,
                )
                active = trader.capital_reservations.active()
                self.assertEqual(len(active), 1)
                self.assertEqual(active[0]["reservation_id"], metadata["reservation_id"])
                self.assertEqual(active[0]["state"], "DISPATCHED")
                self.assertEqual(
                    send.call_args.args[0]["cycle_id"],
                    "cycle-reserved-entry",
                )
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_entry_fill_releases_reservation_only_after_position_is_durable(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader._mark_prices["BTCUSDT"] = 100.0
                trader._lot_step["BTCUSDT"] = 0.001
                trader._config._values["per_symbol_notional_cap_usd"] = 10_000.0
                trader.depth_tracker.set_rest_snapshot(
                    "BTCUSDT",
                    spot_depth_usd=1_000_000.0,
                    perp_depth_usd=1_000_000.0,
                    spot_bid_price=100.0,
                    spot_ask_price=100.0,
                    perp_bid_price=100.0,
                    perp_ask_price=100.0,
                )
                self._seed_authoritative_funding(trader)
                with patch.object(trader.execution, "send_order_intent", return_value=True):
                    trader._dispatch_enter("BTCUSDT", 2_500.0, ann_funding=25.0)
                entry = dict(trader._pending_enters["BTCUSDT"])

                trader._finalize_entry_fill(
                    "BTCUSDT",
                    entry,
                    event_time="2026-01-01T00:00:00+00:00",
                    execution_type="PAPER_FILL",
                    spot_fill_price=100.0,
                    perp_fill_price=100.0,
                )

                self.assertEqual(len(trader.state_reader.get_positions()), 1)
                self.assertEqual(trader.capital_reservations.active(), [])
                released = trader.state_writer.conn.execute(
                    "SELECT state, exchange_terminal_proven FROM capital_reservations"
                ).fetchone()
                self.assertEqual(released["state"], "RELEASED")
                self.assertEqual(released["exchange_terminal_proven"], 1)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_candidate_cycle_marks_toxicity_as_unavailable_without_a_real_signal(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader.funding_ranker.get_rate = lambda symbol: 0.05
                trader.depth_tracker.set_rest_snapshot(
                    "BTCUSDT",
                    spot_depth_usd=200_000.0,
                    perp_depth_usd=220_000.0,
                    spot_bid_price=100.0,
                    spot_ask_price=100.0,
                    perp_bid_price=100.1,
                    perp_ask_price=100.1,
                )

                snapshots = trader._record_candidate_cycle(
                    cycle_id=datetime.now(timezone.utc).isoformat(),
                    ranked=[("BTCUSDT", 0.05)],
                    decision=SimpleNamespace(enter=[], rejected={}),
                    regime_blocked={},
                    cooldown_blocked={},
                    entry_gate_blocked={},
                    external_entry_block_reason=None,
                )
                stored_snapshots = trader.state_reader.get_candidate_snapshots(limit=10)

                # Both venue books are individually crossed at a zero spread;
                # the 10 bps spot/perpetual premium belongs to basis, not the
                # executable spread metric.
                self.assertEqual(snapshots[0].metrics["spread_bps"], 0.0)
                self.assertGreater(snapshots[0].metrics["basis_pct"], 0.0)
                self.assertIsNone(snapshots[0].metrics["toxicity_bps"])
                self.assertFalse(snapshots[0].metrics["toxicity_available"])
                self.assertIsNone(stored_snapshots[0]["metrics"]["toxicity_bps"])
                self.assertFalse(stored_snapshots[0]["metrics"]["toxicity_available"])
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_candidate_cycle_records_observational_net_edge_scores_without_changing_selection(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                rates = {"BTCUSDT": 0.20, "ETHUSDT": 0.15}
                trader.funding_ranker.get_rate = lambda symbol: rates[symbol]

                def shadow_economics(symbol, notional_usd, ann_funding):
                    predicted_edge = 8.0 if symbol == "BTCUSDT" else 24.0
                    return True, [], {
                        "predicted_net_edge_bps": predicted_edge,
                        "round_trip_cost_bps": 12.0,
                        "expected_holding_hours": 8.0,
                        "min_required_edge_bps": 5.0,
                        "required_depth_usd": 10_000.0,
                        "entry_depth_usd": 20_000.0,
                        "spread_bps": 3.0 if symbol == "BTCUSDT" else 2.0,
                    }

                trader._entry_safety_decision = shadow_economics
                cycle_id = datetime.now(timezone.utc).isoformat()
                # The legacy selector chose BTC first.  The shadow score must
                # record that fact, not silently replace the live decision.
                snapshots = trader._record_candidate_cycle(
                    cycle_id=cycle_id,
                    ranked=[("BTCUSDT", rates["BTCUSDT"]), ("ETHUSDT", rates["ETHUSDT"])],
                    decision=SimpleNamespace(
                        enter=[("BTCUSDT", 2_500.0)],
                        rejected={},
                    ),
                    regime_blocked={},
                    cooldown_blocked={},
                    entry_gate_blocked={},
                    external_entry_block_reason=None,
                )

                scores = trader.state_reader.get_opportunity_scores(cycle_id=cycle_id)
                snapshots_by_symbol = {snapshot.symbol: snapshot for snapshot in snapshots}

                self.assertEqual([score["symbol"] for score in scores], ["ETHUSDT", "BTCUSDT"])
                self.assertEqual(scores[0]["predicted_net_edge_bps"], 24.0)
                self.assertFalse(scores[0]["selected"])
                self.assertTrue(scores[1]["selected"])
                self.assertEqual(
                    snapshots_by_symbol["BTCUSDT"].metrics["active_selector_rank"],
                    1,
                )
                self.assertEqual(
                    snapshots_by_symbol["BTCUSDT"].metrics["shadow_net_ev_rank"],
                    2,
                )
                self.assertEqual(
                    snapshots_by_symbol["BTCUSDT"].metrics["spread_bps"],
                    3.0,
                )
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
                risk = trader.state_reader.get_risk()
                self.assertEqual(risk["runtime_mode"], "LIVE")
                self.assertEqual(risk["safe_mode_reason"], "")
                self.assertNotIn("late_entry_fill", trader._safe_mode_flags)
                self.assertNotIn("stale_pending_intent", trader._safe_mode_flags)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    async def test_late_entry_fill_persists_when_other_stale_entries_remain(self):
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
                healed_intent_id = "intent-live-enter-healed"
                still_stale_intent_id = "intent-live-enter-still-stale"
                stale_entry_time = (datetime.now(timezone.utc) - timedelta(minutes=12)).isoformat()
                timed_out_at = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
                trader.state_writer.upsert_pending_intent(
                    intent_id=healed_intent_id,
                    symbol="BTCUSDT",
                    intent_type="ENTER_LONG",
                    status="TIMEOUT",
                    direction="long",
                    quantity=0.5,
                )
                trader.state_writer.upsert_pending_intent(
                    intent_id=still_stale_intent_id,
                    symbol="ETHUSDT",
                    intent_type="ENTER_LONG",
                    status="TIMEOUT",
                    direction="long",
                    quantity=1.25,
                )
                trader._stale_pending_enters["BTCUSDT"] = {
                    "intent_id": healed_intent_id,
                    "entry_time": stale_entry_time,
                    "timed_out_at": timed_out_at,
                    "entry_price": 65000.0,
                    "qty": 0.5,
                    "direction": "long",
                    "ann_funding": 0.20,
                }
                trader._stale_pending_enters["ETHUSDT"] = {
                    "intent_id": still_stale_intent_id,
                    "entry_time": stale_entry_time,
                    "timed_out_at": timed_out_at,
                    "entry_price": 3200.0,
                    "qty": 1.25,
                    "direction": "long",
                    "ann_funding": 0.18,
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
                        "futures_open_orders": [
                            {
                                "symbol": "ETHUSDT",
                            }
                        ],
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
                risk = trader.state_reader.get_risk()
                self.assertEqual(len(positions), 1)
                self.assertEqual(positions[0]["symbol"], "BTCUSDT")
                self.assertNotIn("BTCUSDT", trader._stale_pending_enters)
                self.assertIn("ETHUSDT", trader._stale_pending_enters)
                self.assertEqual(risk["runtime_mode"], "SAFE_MODE")
                self.assertEqual(
                    risk["safe_mode_reason"],
                    "account_reconciliation, late_entry_fill, stale_pending_intent",
                )
                self.assertIn("late_entry_fill", trader._safe_mode_flags)
                self.assertIn("stale_pending_intent", trader._safe_mode_flags)
                self.assertEqual(
                    sorted(
                        row["symbol"]
                        for row in trader.state_reader.get_pending_intents(statuses=["TIMEOUT"])
                    ),
                    ["ETHUSDT"],
                )
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    async def test_live_self_heal_rebuilds_position_from_exchange_before_timeout_when_hedge_is_complete(self):
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
                intent_id = "intent-live-enter-active"
                trader.state_writer.upsert_pending_intent(
                    intent_id=intent_id,
                    symbol="ATAUSDT",
                    intent_type="ENTER_LONG",
                    status="FILLED",
                    direction="long",
                    quantity=284414.0,
                )
                trader._pending_enters["ATAUSDT"] = {
                    "intent_id": intent_id,
                    "entry_time": (datetime.now(timezone.utc) - timedelta(seconds=45)).isoformat(),
                    "entry_price": 0.0087,
                    "qty": 284414.0,
                    "direction": "long",
                    "ann_funding": 0.30,
                }
                trader._fetch_exchange_startup_snapshot = AsyncMock(
                    return_value={
                        "futures_account": {},
                        "position_risk": [
                            {
                                "symbol": "ATAUSDT",
                                "positionAmt": "-284414",
                                "positionSide": "BOTH",
                                "entryPrice": "0.0087",
                                "breakEvenPrice": "0.00869652",
                                "markPrice": "0.008752",
                                "unRealizedProfit": "-14.789528",
                                "updateTime": 1700000003000,
                            }
                        ],
                        "futures_open_orders": [],
                        "spot_account": {
                            "balances": [
                                {"asset": "ATA", "free": "284129.58600000", "locked": "0.0"},
                            ]
                        },
                        "spot_open_orders": [],
                        "funding_income": [],
                    }
                )

                await trader._live_self_heal_stale_pending_intents(datetime.now(timezone.utc))

                positions = trader.state_reader.get_positions()
                self.assertEqual(len(positions), 1)
                self.assertEqual(positions[0]["symbol"], "ATAUSDT")
                self.assertEqual(positions[0]["direction"], "long")
                self.assertNotIn("ATAUSDT", trader._pending_enters)
                self.assertEqual(
                    trader.state_reader.get_pending_intents(statuses=["FILLED"]),
                    [],
                )
                self.assertNotIn("late_entry_fill", trader._safe_mode_flags)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    async def test_live_entry_failure_recovers_exchange_position_for_manual_review(self):
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
                trader._preflight_status = "passed"
                intent_id = "intent-live-failed-entry"
                trader.state_writer.upsert_pending_intent(
                    intent_id=intent_id,
                    symbol="BTCUSDT",
                    intent_type="ENTER_LONG",
                    status="PENDING_ACK",
                    direction="long",
                    quantity=0.5,
                )
                trader._pending_enters["BTCUSDT"] = {
                    "intent_id": intent_id,
                    "entry_time": (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat(),
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
                        "spot_account": {"balances": []},
                        "spot_open_orders": [],
                        "funding_income": [],
                    }
                )
                trader._config._values["autonomous_startup_recovery"] = False

                with patch.object(
                    trader.execution,
                    "restore_position_tracking",
                    return_value=True,
                ) as restore_mock:
                    trader._on_order_update(
                        "BTCUSDT",
                        "REJECTED",
                        client_order_id="btc-reject",
                        execution_type="LEGGING_DEFENSE_FAILED",
                    )
                    recovery_task = trader._entry_failure_recovery_tasks["BTCUSDT"]
                    await recovery_task

                positions = trader.state_reader.get_positions()
                self.assertEqual(len(positions), 1)
                self.assertEqual(positions[0]["symbol"], "BTCUSDT")
                self.assertEqual(positions[0]["recovery_state"], "manual_review")
                self.assertEqual(positions[0]["hedge_ratio"], 0.0)
                self.assertEqual(positions[0]["exchange_pnl_usd"], 55.0)
                self.assertNotIn("BTCUSDT", trader._pending_enters)
                self.assertIn("BTCUSDT", trader._startup_manual_review_symbols)
                self.assertIn("hedge_gap", trader._safe_mode_flags)
                self.assertIn("startup_manual_review", trader._safe_mode_flags)
                restore_mock.assert_called_once_with(
                    symbol="BTCUSDT",
                    direction="long",
                    qty=0.5,
                    spot_entry_price=65000.0,
                    perp_entry_price=65000.0,
                    spot_mark_price=64900.0,
                    perp_mark_price=64900.0,
                    spot_quantity=0.0,
                    perp_quantity=0.5,
                )
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    async def test_live_self_heal_reconciles_pending_enter_when_spot_account_unavailable(self):
        """When the spot API is down (spot_account=None), the self-heal must still
        reconcile a pending ENTER whose perp position is visible on the futures side.
        Previously the empty spot_balances dict caused the hedge check to fail with
        0-balance, silently skipping the position write on every maintenance cycle."""
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
                intent_id = "intent-nilusdt-enter"
                trader.state_writer.upsert_pending_intent(
                    intent_id=intent_id,
                    symbol="NILUSDT",
                    intent_type="ENTER_LONG",
                    status="PENDING_ACK",
                    direction="long",
                    quantity=69816.6,
                )
                trader._pending_enters["NILUSDT"] = {
                    "intent_id": intent_id,
                    "entry_time": (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat(),
                    "entry_price": 0.035,
                    "qty": 69816.6,
                    "direction": "long",
                    "ann_funding": 1.4781,
                }
                # Spot account is None — simulates the spot API being unavailable
                # (e.g. testnet demo-api.binance.com is down or keys not accepted).
                trader._fetch_exchange_startup_snapshot = AsyncMock(
                    return_value={
                        "futures_account": {},
                        "position_risk": [
                            {
                                "symbol": "NILUSDT",
                                "positionAmt": "-69816.6",
                                "positionSide": "BOTH",
                                "entryPrice": "0.035",
                                "breakEvenPrice": "0.03497",
                                "markPrice": "0.0348",
                                "unRealizedProfit": "-14.0",
                                "updateTime": 1700000005000,
                            }
                        ],
                        "futures_open_orders": [],
                        "spot_account": None,  # spot API unavailable
                        "spot_open_orders": [],
                        "funding_income": [],
                    }
                )

                await trader._live_self_heal_stale_pending_intents(datetime.now(timezone.utc))

                positions = trader.state_reader.get_positions()
                self.assertEqual(len(positions), 1, "position must be written despite spot API being unavailable")
                self.assertEqual(positions[0]["symbol"], "NILUSDT")
                self.assertEqual(positions[0]["direction"], "long")
                self.assertNotIn("NILUSDT", trader._pending_enters)
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

    def test_exit_trade_in_paper_mode_models_economics_without_long_spot_borrow(self):
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
                trader._pending_exit_intents["BTCUSDT"] = "intent-exit-borrow"

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
                self.assertEqual(trades[0]["borrow_cost_usd"], 0.0)
                self.assertEqual(trades[0]["economic_status"], "MODELED")
                self.assertIn(
                    "paper_exchange_model",
                    trades[0]["economic_notes"],
                )
                self.assertLess(trades[0]["net_pnl_usd"], trades[0]["funding_collected"])
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_exit_fill_is_persisted_before_execution_cost_finalization(self):
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
                    ann_funding=0.0,
                    entry_ann_funding=0.0,
                    spot_live=100.0,
                    perp_live=100.0,
                    updated_at=entry_time,
                )
                trader._entry_times["BTCUSDT"] = entry_time
                trader._position_directions["BTCUSDT"] = "long"
                trader._exit_events["BTCUSDT"] = asyncio.Event()
                trader._pending_exit_intents["BTCUSDT"] = "intent-exit-cost"

                trader._on_order_update(
                    "BTCUSDT",
                    "FILLED",
                    filled_qty=1.0,
                    client_order_id="exit-cycle",
                    commission=0.75,
                    commission_asset="USDT",
                    execution_type="TRADE",
                    event_time=exit_time,
                    spot_fill_price=100.0,
                    perp_fill_price=100.0,
                )

                trades = trader.state_reader.get_trades(limit=1)
                events = trader.state_reader.get_execution_events(limit=10)
                self.assertEqual(len(trades), 1)
                self.assertAlmostEqual(trades[0]["execution_cost_usd"], 0.75, places=6)
                self.assertTrue(
                    any(event["client_order_id"] == "exit-cycle" for event in events)
                )
                self.assertTrue(trader._execution_event_queue.empty())
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
                trader._pending_exit_intents["BTCUSDT"] = "intent-exit-funding"

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
                
                while not trader._execution_event_queue.empty():
                    payload = trader._execution_event_queue.get_nowait()
                    trader.state_writer.record_execution_event(payload)
                trader.state_writer.flush()

                funding_events = trader.state_reader.get_trade_funding_cashflows(
                    "BTCUSDT",
                    entry_time,
                    exit_time,
                )
                self.assertEqual(len(trades), 1)
                self.assertEqual(trades[0]["funding_source"], "actual_rest")
                self.assertAlmostEqual(trades[0]["funding_collected"], 0.5, places=6)
                self.assertEqual(trades[0]["borrow_cost_usd"], 0.0)
                self.assertEqual(trades[0]["economic_status"], "INCOMPLETE")
                self.assertIn(
                    "commission_evidence_incomplete",
                    trades[0]["economic_notes"],
                )
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
                self.assertEqual(positions[0]["perp_entry"], 65000.0)
                self.assertEqual(positions[0]["updated_at"], "2026-01-01T00:00:00+00:00")
                self.assertEqual(positions[0]["entry_ann_funding"], 0.12)

                refreshed_positions = trader._refresh_open_position_metrics()
                self.assertEqual(len(refreshed_positions), 1)
                self.assertAlmostEqual(refreshed_positions[0]["net_pnl_usd"], 25.0, places=6)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    async def test_live_startup_reconciliation_sums_stable_collateral_when_demo_total_underreports(self):
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
                trader._fetch_exchange_startup_snapshot = AsyncMock(
                    return_value={
                        "futures_account": {
                            "totalMarginBalance": "5000.0",
                            "totalWalletBalance": "5000.0",
                            "availableBalance": "5000.0",
                            "assets": [
                                {"asset": "USDT", "marginBalance": "5000.0", "availableBalance": "5000.0"},
                                {"asset": "USDC", "marginBalance": "5000.0", "availableBalance": "5000.0"},
                                {"asset": "BTC", "marginBalance": "0.01", "availableBalance": "0.01"},
                            ],
                        },
                        "position_risk": [],
                        "futures_open_orders": [],
                        "spot_account": {"balances": []},
                        "spot_open_orders": [],
                        "funding_income": [],
                    }
                )

                await trader._reconcile_live_startup_state()

                stats = trader.state_reader.get_stats()
                risk = trader.state_reader.get_risk()

                self.assertEqual(stats["account_equity"], 10000.0)
                self.assertEqual(risk["account_equity"], 10000.0)
                self.assertEqual(risk["available_balance"], 10000.0)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    async def test_live_startup_equity_includes_spot_cash_consistently_with_periodic_audit(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(
            os.environ,
            {
                "TRADING_MODE": "testnet",
                "BONGUS_EXPECTED_ACCOUNT_UID": "12345",
            },
            clear=False,
        ):
            trader = self._build_trader(db_name)
            try:
                snapshot = {
                    "futures_account": {
                        "totalMarginBalance": "1000.0",
                        "totalWalletBalance": "1000.0",
                        "availableBalance": "900.0",
                        "positions": [],
                    },
                    "position_risk": [],
                    "futures_open_orders": [],
                    "spot_account": {
                        "uid": 12345,
                        "balances": [
                            {"asset": "USDT", "free": "500.0", "locked": "0"},
                        ],
                    },
                    "spot_open_orders": [],
                    "margin_account": {"userAssets": []},
                    "margin_account_status": "available",
                    "margin_open_orders": [],
                    "margin_open_orders_status": "available",
                    "funding_income": [],
                    "futures_income": [],
                    "margin_interest": [],
                    "margin_interest_status": "available",
                    "statement_history_status": {
                        "futures_income": "available",
                        "margin_interest": "available",
                    },
                    "snapshot_errors": {},
                }
                trader._fetch_exchange_startup_snapshot = AsyncMock(return_value=snapshot)

                await trader._reconcile_live_startup_state()

                stats = trader.state_reader.get_stats()
                risk = trader.state_reader.get_risk()
                self.assertEqual(stats["account_equity"], 1500.0)
                self.assertEqual(risk["account_equity"], 1500.0)
                self.assertEqual(risk["exchange_account_equity"], 1500.0)
                self.assertEqual(risk["exchange_spot_cash_available_usd"], 500.0)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    async def test_live_startup_reconciles_account_positions_when_position_risk_is_empty(self):
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
                trader._fetch_exchange_startup_snapshot = AsyncMock(
                    return_value={
                        "futures_account": {
                            "totalMarginBalance": "10000.0",
                            "totalWalletBalance": "9950.0",
                            "availableBalance": "9000.0",
                            "positions": [
                                {
                                    "symbol": "BTCUSDT",
                                    "positionAmt": "-0.5",
                                    "positionSide": "BOTH",
                                    "entryPrice": "65000.0",
                                    "unrealizedProfit": "55.0",
                                }
                            ],
                        },
                        "position_risk": [],
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
                risk = trader.state_reader.get_risk()

                self.assertEqual(len(positions), 1)
                self.assertEqual(positions[0]["symbol"], "BTCUSDT")
                self.assertEqual(positions[0]["direction"], "long")
                self.assertEqual(positions[0]["spot_entry"], 65000.0)
                self.assertEqual(positions[0]["spot_live"], 65000.0)
                self.assertEqual(positions[0]["perp_live"], 65000.0)
                self.assertAlmostEqual(positions[0]["net_pnl_usd"], 55.0, places=6)
                self.assertEqual(risk["startup_reconciliation_position_count"], 1)
                self.assertEqual(risk["startup_reconciliation_position_risk_count"], 0)
                self.assertEqual(risk["startup_reconciliation_account_position_count"], 1)
                self.assertEqual(risk["startup_reconciliation_position_source"], "account_fallback")
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
                self.assertEqual(trader.funding_ranker._allowed_symbols, {"BTCUSDT", "PEPEUSDT"})
                self.assertAlmostEqual(trader._lot_step["BTCUSDT"], 0.001)
                self.assertAlmostEqual(trader._lot_step["PEPEUSDT"], 1.0)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_tradable_trade_symbols_require_verified_spot_universe_in_live_mode(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "live"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader._tradable_perp_symbols = {"PHBUSDT", "BTCUSDT"}
                trader._tradable_spot_symbols = set()
                trader._spot_universe_loaded = False

                self.assertEqual(trader._tradable_trade_symbols(), set())
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_tradable_trade_symbols_allow_perp_universe_in_paper_mode_without_spot(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader._tradable_perp_symbols = {"PHBUSDT", "BTCUSDT"}
                trader._tradable_spot_symbols = set()
                trader._spot_universe_loaded = False

                self.assertEqual(trader._tradable_trade_symbols(), {"PHBUSDT", "BTCUSDT"})
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    async def test_fetch_lot_step_sizes_blocks_live_entries_when_spot_exchange_info_fails(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "live"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                def fake_get(url, timeout=None):
                    if url == "https://fapi.binance.com/fapi/v1/exchangeInfo":
                        return _FakeResponse(
                            {
                                "symbols": [
                                    {
                                        "symbol": "PHBUSDT",
                                        "contractType": "PERPETUAL",
                                        "status": "TRADING",
                                        "quoteAsset": "USDT",
                                        "filters": [{"filterType": "LOT_SIZE", "stepSize": "1"}],
                                    }
                                ]
                            }
                        )
                    if url == "https://api.binance.com/api/v3/exchangeInfo":
                        raise RuntimeError("spot exchangeInfo timeout")
                    raise AssertionError(f"Unexpected URL: {url}")

                with patch("scripts.live_trader_v2.requests.get", side_effect=fake_get):
                    await trader._fetch_lot_step_sizes()

                risk = trader.state_reader.get_risk()
                self.assertEqual(trader._tradable_trade_symbols(), set())
                self.assertEqual(trader.funding_ranker._allowed_symbols, set())
                self.assertIn("spot_universe_unavailable", trader._safe_mode_flags)
                self.assertFalse(risk["spot_universe_loaded"])
                self.assertIn("spot exchangeInfo timeout", risk["spot_universe_last_error"])
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
                assert basis_pct is not None
                self.assertAlmostEqual(basis_pct, 0.03)
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

    def test_risk_limits_allow_uneven_partial_portfolio_below_capacity(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                decision = trader._evaluate_risk_controls(
                    [
                        {
                            "symbol": "BTCUSDT",
                            "qty": 25.0,
                            "spot_live": 100.0,
                            "perp_live": 100.0,
                            "spot_entry": 100.0,
                            "perp_entry": 100.0,
                            "net_pnl_usd": 0.0,
                        },
                        {
                            "symbol": "ETHUSDT",
                            "qty": 5.0,
                            "spot_live": 100.0,
                            "perp_live": 100.0,
                            "spot_entry": 100.0,
                            "perp_entry": 100.0,
                            "net_pnl_usd": 0.0,
                        },
                    ]
                )

                trader.state_writer.flush()
                risk_snapshot = trader.state_reader.get_risk()
                self.assertTrue(decision.allow_new_risk)
                self.assertFalse(decision.derisk_required)
                self.assertAlmostEqual(risk_snapshot["largest_symbol_gross_exposure"], 2500.0)
                self.assertAlmostEqual(
                    risk_snapshot["symbol_concentration_denominator_usd"],
                    max(
                        risk_snapshot["gross_exposure"],
                        trader._risk_engine.limits.max_gross_exposure_usd,
                    ),
                )
                self.assertAlmostEqual(
                    risk_snapshot["symbol_concentration"],
                    risk_snapshot["largest_symbol_gross_exposure"]
                    / risk_snapshot["symbol_concentration_denominator_usd"],
                )
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
                trader.state_writer.flush()
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
                            "qty": 120.0,
                            "spot_live": 100.0,
                            "perp_live": 100.0,
                            "spot_entry": 100.0,
                            "perp_entry": 100.0,
                            "net_pnl_usd": 0.0,
                        },
                        {
                            "symbol": "ETHUSDT",
                            "qty": 20.0,
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
                trader._config._values["pause_new_entries"] = False
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

                with patch("scripts.live_trader_v2.requests.get", side_effect=fake_get), patch.object(
                    trader.execution,
                    "restore_position_tracking",
                    return_value=True,
                ) as restore_mock:
                    await trader._on_startup()

                positions = trader.state_reader.get_positions()
                stats = trader.state_reader.get_stats()
                risk = trader.state_reader.get_risk()

                self.assertEqual(len(positions), 1)
                self.assertEqual(positions[0]["symbol"], "BTCUSDT")
                self.assertEqual(positions[0]["direction"], "long")
                self.assertEqual(positions[0]["side"], "LONG_SPOT_SHORT_PERP")
                self.assertEqual(positions[0]["qty"], 0.5)
                self.assertEqual(positions[0]["spot_entry"], 65000.0)
                self.assertEqual(positions[0]["perp_live"], 64900.0)
                self.assertEqual(positions[0]["updated_at"], trader._entry_times["BTCUSDT"])

                # Startup and periodic audits both use futures margin plus
                # spot wallet value. The BTC fallback mark is 64,900 here:
                # 12,000 + 1,000 USDT + (0.5 * 64,900) = 45,450.
                self.assertEqual(stats["account_equity"], 45450.0)
                self.assertEqual(stats["gross_exposure"], 32450.0)
                self.assertEqual(stats["max_gross_exposure"], trader._config.get("max_gross_exposure_usd"))

                # Signed endpoints were ingested, but this legacy fixture does
                # not provide margin truth, futures-account positions, or the
                # configured dedicated-account UID.  It must remain fail closed.
                self.assertEqual(risk["startup_reconciliation_status"], "needs_review")
                self.assertEqual(risk["startup_reconciliation_position_count"], 1)
                self.assertEqual(risk["startup_reconciliation_local_only_symbols"], ["SOLUSDT"])
                self.assertEqual(risk["startup_reconciliation_mismatched_symbols"], [])
                self.assertEqual(risk["startup_reconciliation_spot_hedge_gaps"], [])
                self.assertEqual(risk["startup_reconciliation_last_funding_fee"], 5.25)
                self.assertFalse(risk["allow_new_risk"])
                self.assertFalse(risk["account_reconciliation_ready"])
                restore_mock.assert_called_once_with(
                    symbol="BTCUSDT",
                    direction="long",
                    qty=0.5,
                    spot_entry_price=65000.0,
                    perp_entry_price=65000.0,
                    spot_mark_price=64900.0,
                    perp_mark_price=64900.0,
                    spot_quantity=0.5,
                    perp_quantity=0.5,
                )

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

    async def test_fetch_exchange_startup_snapshot_falls_back_to_v2_position_risk(self):
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
                def fake_get(url, headers=None, timeout=None):
                    requested_urls.append((url, headers))
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
                            {"code": -5000, "msg": "positionRisk v3 unsupported"},
                            status_code=400,
                        )
                    if url.startswith("https://fapi.binance.com/fapi/v2/positionRisk?"):
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
                                }
                            ]
                        )
                    if url.startswith("https://fapi.binance.com/fapi/v1/openOrders?"):
                        return _FakeResponse([])
                    if url.startswith("https://fapi.binance.com/fapi/v1/income?"):
                        return _FakeResponse([])
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
                    snapshot = await trader._fetch_exchange_startup_snapshot()

                self.assertEqual(len(snapshot["position_risk"]), 1)
                self.assertEqual(snapshot["position_risk"][0]["symbol"], "BTCUSDT")
                self.assertTrue(
                    any("fapi/v3/positionRisk?" in url for url, _ in requested_urls),
                    "startup snapshot should try the primary v3 positionRisk endpoint first",
                )
                self.assertTrue(
                    any("fapi/v2/positionRisk?" in url for url, _ in requested_urls),
                    "startup snapshot should retry positionRisk on the v2 endpoint when v3 fails",
                )
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    async def test_live_startup_accepts_small_spot_commission_shortfall_but_requires_complete_account_proof(self):
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
                trader._fetch_exchange_startup_snapshot = AsyncMock(
                    return_value={
                        "futures_account": {
                            "totalMarginBalance": "10000.0",
                            "totalWalletBalance": "9950.0",
                            "availableBalance": "9000.0",
                        },
                        "position_risk": [
                            {
                                "symbol": "ATAUSDT",
                                "positionAmt": "-284414",
                                "positionSide": "BOTH",
                                "entryPrice": "0.0087",
                                "breakEvenPrice": "0.00869652",
                                "markPrice": "0.008752",
                                "unRealizedProfit": "-14.789528",
                                "updateTime": 1700000003000,
                            }
                        ],
                        "futures_open_orders": [],
                        "spot_account": {
                            "balances": [
                                {"asset": "ATA", "free": "284129.58600000", "locked": "0.0"},
                            ]
                        },
                        "spot_open_orders": [],
                        "funding_income": [],
                    }
                )

                await trader._reconcile_live_startup_state()

                risk = trader.state_reader.get_risk()
                positions = trader.state_reader.get_positions()

                self.assertEqual(risk["startup_reconciliation_spot_hedge_gaps"], [])
                self.assertFalse(risk["allow_new_risk"])
                self.assertFalse(risk["account_reconciliation_ready"])
                self.assertEqual(len(positions), 1)
                self.assertEqual(positions[0]["symbol"], "ATAUSDT")
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    async def test_live_startup_recovered_position_keeps_nonzero_exchange_pnl_without_local_basis(self):
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
                # Exchange-only exposure is adopted visibly but cannot be
                # promoted to managed state without durable ownership lineage.
                self.assertEqual(positions[0]["recovery_state"], "manual_review")
                self.assertAlmostEqual(positions[0]["net_pnl_usd"], 55.0, places=6)
                self.assertAlmostEqual(positions[0]["exchange_pnl_usd"], 55.0, places=6)

                refreshed_positions = trader._refresh_open_position_metrics()
                self.assertEqual(len(refreshed_positions), 1)
                self.assertAlmostEqual(refreshed_positions[0]["net_pnl_usd"], 50.0, places=6)
                self.assertAlmostEqual(refreshed_positions[0]["exchange_pnl_usd"], 55.0, places=6)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    async def test_live_startup_reconciliation_prefers_exchange_entry_price_over_persisted_break_even(self):
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
                    direction="long",
                    spot_entry=65000.0,
                    perp_entry=65010.0,
                    spot_live=64900.0,
                    perp_live=64900.0,
                    qty=0.5,
                    hedge_ratio=1.0,
                    ann_funding=0.20,
                )
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
                self.assertAlmostEqual(float(positions[0]["perp_entry"]), 65000.0, places=6)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_refresh_open_position_metrics_preserves_exchange_unrealized_profit_for_naked_row(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader.state_writer.upsert_position(
                    symbol="ATAUSDT",
                    side="SHORT_SPOT_LONG_PERP",
                    direction="short",
                    spot_entry=0.00928,
                    perp_entry=0.0095075,
                    spot_live=0.0103078,
                    perp_live=0.0103078,
                    qty=2_424_564.0,
                    hedge_ratio=0.0,
                    ann_funding=0.1095,
                    net_pnl_usd=1_940.16,
                    exchange_pnl_usd=1_940.16,
                    recovery_state="manual_review",
                )

                refreshed_positions = trader._refresh_open_position_metrics()

                self.assertEqual(len(refreshed_positions), 1)
                self.assertAlmostEqual(float(refreshed_positions[0]["exchange_pnl_usd"]), 1_940.16, places=6)
                self.assertAlmostEqual(float(refreshed_positions[0]["net_pnl_usd"]), 1_940.16, places=6)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_apply_exchange_position_snapshot_refreshes_naked_manual_review_unrealized_profit(self):
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
                    symbol="ATAUSDT",
                    side="SHORT_SPOT_LONG_PERP",
                    direction="short",
                    spot_entry=0.00928,
                    perp_entry=0.0095075,
                    spot_live=0.01030,
                    perp_live=0.01030,
                    qty=2_424_564.0,
                    hedge_ratio=0.0,
                    ann_funding=0.1095,
                    net_pnl_usd=1_940.16,
                    exchange_pnl_usd=1_940.16,
                    recovery_state="manual_review",
                )

                critical = trader._apply_exchange_position_snapshot(
                    {
                        "futures_account": {
                            "totalMarginBalance": "12000.0",
                            "totalWalletBalance": "11950.0",
                            "availableBalance": "8900.0",
                        },
                        "position_risk": [
                            {
                                "symbol": "ATAUSDT",
                                "positionAmt": "2424564",
                                "positionSide": "LONG",
                                "entryPrice": "0.0095075",
                                "breakEvenPrice": "0.009278144",
                                "markPrice": "0.0105078",
                                "unRealizedProfit": "2424.564",
                                "updateTime": 1700000003000,
                            }
                        ],
                        "futures_open_orders": [],
                        "spot_account": {"balances": []},
                        "spot_open_orders": [],
                        "funding_income": [],
                    },
                    sample_time=datetime.now(timezone.utc).isoformat(),
                    record_health_metrics=False,
                    log_prefix="audit",
                )

                positions = trader.state_reader.get_positions()
                # PnL is refreshed, but a naked/manual-review position and an
                # incomplete liability/account snapshot remain critical.
                self.assertTrue(critical)
                self.assertEqual(len(positions), 1)
                self.assertAlmostEqual(float(positions[0]["exchange_pnl_usd"]), 2424.564, places=6)
                self.assertAlmostEqual(float(positions[0]["perp_live"]), 0.0105078, places=7)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    async def test_exchange_position_audit_clears_manual_review_position_absent_on_binance(self):
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
                    direction="long",
                    spot_entry=65_000.0,
                    perp_entry=65_010.0,
                    spot_live=65_100.0,
                    perp_live=64_900.0,
                    qty=0.05,
                    hedge_ratio=0.0,
                    ann_funding=0.12,
                    recovery_state="manual_review",
                )
                trader._startup_manual_review_symbols["BTCUSDT"] = (
                    "BTCUSDT recovered with only 0.00% of the required spot hedge on exchange"
                )
                trader._set_safe_mode_flag("startup_manual_review", True)
                trader._fetch_exchange_startup_snapshot = AsyncMock(
                    return_value={
                        "futures_account": {
                            "totalMarginBalance": "10000.0",
                            "totalWalletBalance": "9950.0",
                            "availableBalance": "9000.0",
                        },
                        "position_risk": [],
                        "futures_open_orders": [],
                        "spot_account": {"balances": []},
                        "spot_open_orders": [],
                        "funding_income": [],
                    }
                )

                await trader._audit_tracked_positions_against_exchange(
                    "2026-01-01T00:00:00+00:00"
                )

                risk = trader.state_reader.get_risk()
                self.assertEqual(trader.state_reader.get_positions(), [])
                self.assertEqual(trader._startup_manual_review_symbols, {})
                self.assertNotIn("startup_manual_review", trader._safe_mode_flags)
                self.assertEqual(risk["startup_reconciliation_manual_review"], [])
                self.assertEqual(risk["startup_reconciliation_spot_hedge_gaps"], [])
                self.assertEqual(risk["audit_reconciliation_status"], "ok")
                self.assertEqual(risk["audit_reconciliation_position_count"], 0)
                self.assertEqual(risk["audit_reconciliation_position_risk_count"], 0)
                self.assertEqual(risk["audit_reconciliation_account_position_count"], 0)
                self.assertEqual(risk["audit_reconciliation_position_source"], "audit")
                self.assertEqual(risk["audit_reconciliation_local_only_symbols"], ["BTCUSDT"])
                self.assertEqual(risk["audit_reconciliation_manual_review"], [])
                self.assertEqual(risk["audit_reconciliation_spot_hedge_gaps"], [])
                self.assertEqual(risk["exchange_position_audit_last_status"], "ok")
                self.assertEqual(risk["exchange_position_audit_consecutive_failures"], 0)
                self.assertTrue(risk["exchange_position_audit_applied"])
                self.assertEqual(risk["exchange_position_audit_removed_symbols"], ["BTCUSDT"])
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    async def test_exchange_position_audit_sets_safe_mode_after_five_signed_failures(self):
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
                failure = scripts.live_trader_v2.BinanceSignedCallError(
                    endpoint="/fapi/v3/account",
                    code=-1021,
                    detail="code=-1021 msg=Timestamp outside recvWindow",
                )
                trader._fetch_exchange_startup_snapshot = AsyncMock(side_effect=[failure] * 5)

                for _ in range(5):
                    self.assertFalse(
                        await trader._audit_tracked_positions_against_exchange(
                            "2026-01-01T00:00:00+00:00"
                        )
                    )

                risk = trader.state_reader.get_risk()
                self.assertEqual(trader._audit_consecutive_failures, 5)
                self.assertIn("audit_unavailable", trader._safe_mode_flags)
                self.assertEqual(risk["exchange_position_audit_last_status"], "failed")
                self.assertIn("-1021", risk["exchange_position_audit_last_error"])
                self.assertEqual(risk["exchange_position_audit_consecutive_failures"], 5)
                self.assertFalse(risk["exchange_position_audit_applied"])
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    async def test_exchange_position_audit_clears_failure_counter_after_success(self):
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
                failure = scripts.live_trader_v2.BinanceSignedCallError(
                    endpoint="/fapi/v3/account",
                    code=-1021,
                    detail="code=-1021 msg=Timestamp outside recvWindow",
                )
                success_snapshot = {
                    "futures_account": {
                        "totalMarginBalance": "10000.0",
                        "totalWalletBalance": "9950.0",
                        "availableBalance": "9000.0",
                    },
                    "position_risk": [],
                    "futures_open_orders": [],
                    "spot_account": {"balances": []},
                    "spot_open_orders": [],
                    "funding_income": [],
                }
                trader._fetch_exchange_startup_snapshot = AsyncMock(
                    side_effect=[failure, failure, failure, failure, failure, success_snapshot]
                )

                for _ in range(5):
                    await trader._audit_tracked_positions_against_exchange(
                        "2026-01-01T00:00:00+00:00"
                    )

                self.assertIn("audit_unavailable", trader._safe_mode_flags)

                await trader._audit_tracked_positions_against_exchange(
                    "2026-01-01T00:01:00+00:00"
                )

                risk = trader.state_reader.get_risk()
                self.assertEqual(trader._audit_consecutive_failures, 0)
                self.assertNotIn("audit_unavailable", trader._safe_mode_flags)
                self.assertEqual(risk["exchange_position_audit_last_status"], "ok")
                self.assertEqual(risk["exchange_position_audit_consecutive_failures"], 0)
                self.assertEqual(risk["exchange_position_audit_last_error"], "")
                self.assertTrue(risk["exchange_position_audit_applied"])
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_manual_review_only_portfolio_skips_concentration_derisk(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                decision = trader._evaluate_risk_controls(
                    [
                        {
                            "symbol": "BTCUSDT",
                            "qty": 120.0,
                            "spot_live": 100.0,
                            "perp_live": 100.0,
                            "spot_entry": 100.0,
                            "perp_entry": 100.0,
                            "net_pnl_usd": 0.0,
                            "recovery_state": "manual_review",
                        },
                        {
                            "symbol": "ETHUSDT",
                            "qty": 20.0,
                            "spot_live": 100.0,
                            "perp_live": 100.0,
                            "spot_entry": 100.0,
                            "perp_entry": 100.0,
                            "net_pnl_usd": 0.0,
                            "recovery_state": "manual_review",
                        },
                    ]
                )

                self.assertFalse(decision.derisk_required)
                self.assertNotIn("symbol concentration limit exceeded", decision.reasons)
                self.assertAlmostEqual(trader._risk_engine.limits.max_symbol_concentration, 1.0)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_startup_stuck_symbols_are_excluded_from_risk_math(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                rows = [
                    {
                        "symbol": "BTCUSDT",
                        "qty": 100.0,
                        "spot_live": 100.0,
                        "perp_live": 100.0,
                        "spot_entry": 100.0,
                        "perp_entry": 100.0,
                        "net_pnl_usd": 100.0,
                        "recovery_state": "tracked",
                    },
                    {
                        "symbol": "ATAUSDT",
                        "qty": 500.0,
                        "spot_live": 100.0,
                        "perp_live": 100.0,
                        "spot_entry": 100.0,
                        "perp_entry": 100.0,
                        "net_pnl_usd": -5_000.0,
                        "recovery_state": "tracked",
                    },
                ]
                trader._startup_recovery_stuck_symbols["ATAUSDT"] = "stuck startup unwind"

                trader._evaluate_risk_controls(rows)

                risk = trader.state_reader.get_risk()
                self.assertAlmostEqual(float(risk["gross_exposure"]), 10_000.0)
                self.assertAlmostEqual(float(risk["mark_to_market_open_pnl_usd"]), -4_900.0)
                self.assertAlmostEqual(float(risk["account_equity_excludes_manual_review_usd"]), -5_000.0)
                self.assertGreater(float(risk["account_equity"]), 10_000.0)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_manual_review_positions_are_excluded_from_strategy_open_positions(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "testnet"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader.state_writer.upsert_position(
                    symbol="ETHUSDT",
                    side="LONG_SPOT_SHORT_PERP",
                    direction="long",
                    spot_entry=2_000.0,
                    perp_entry=2_000.0,
                    qty=0.1,
                    spot_live=2_050.0,
                    perp_live=2_040.0,
                    hedge_ratio=0.0,
                    recovery_state="manual_review",
                )
                trader.state_writer.upsert_position(
                    symbol="BTCUSDT",
                    side="LONG_SPOT_SHORT_PERP",
                    direction="long",
                    spot_entry=65_000.0,
                    perp_entry=65_010.0,
                    qty=0.05,
                    spot_live=65_100.0,
                    perp_live=64_900.0,
                    hedge_ratio=1.0,
                    recovery_state="tracked",
                )

                open_positions = trader._get_open_positions()

                self.assertEqual([pos.symbol for pos in open_positions], ["BTCUSDT", "ETHUSDT"])
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_operator_flatten_all_dispatches_exits_for_manual_review_positions(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "testnet"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader.state_writer.upsert_position(
                    symbol="ETHUSDT",
                    side="LONG_SPOT_SHORT_PERP",
                    direction="long",
                    spot_entry=2_000.0,
                    perp_entry=2_001.0,
                    qty=0.1,
                    hedge_ratio=0.0,
                    recovery_state="manual_review",
                )
                trader.state_writer.upsert_position(
                    symbol="BTCUSDT",
                    side="LONG_SPOT_SHORT_PERP",
                    direction="long",
                    spot_entry=65_000.0,
                    perp_entry=65_010.0,
                    qty=0.05,
                    hedge_ratio=1.0,
                    recovery_state="tracked",
                )
                trader.state_writer.set_risk_snapshot(
                    {
                        "operator_flatten_all_request_id": "req-123",
                        "operator_flatten_all_requested_by": "admin",
                        "operator_flatten_all_status": "requested",
                    }
                )
                trader.state_writer.flush()

                with patch.object(trader, "_dispatch_exit") as dispatch_exit:
                    active = trader._maybe_process_operator_flatten_all_request(
                        trader.state_reader.get_positions()
                    )

                self.assertTrue(active)
                self.assertTrue(trader._operator_pause_new_entries_bridge)
                dispatch_exit.assert_any_call("ETHUSDT", urgency=1.0, direction="long")
                dispatch_exit.assert_any_call("BTCUSDT", urgency=1.0, direction="long")

                risk = trader.state_reader.get_risk()
                self.assertEqual(risk["operator_flatten_all_status"], "in_progress")
                self.assertEqual(
                    sorted(risk["operator_flatten_all_remaining_symbols"]),
                    ["BTCUSDT", "ETHUSDT"],
                )
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_startup_recovery_zero_hedge_manual_review_respects_operator_toggle(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "testnet"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader._startup_complete_at = (
                    datetime.now(timezone.utc) - timedelta(seconds=61)
                ).isoformat()
                trader.state_writer.upsert_position(
                    symbol="ETHUSDT",
                    side="LONG_SPOT_SHORT_PERP",
                    direction="long",
                    spot_entry=2_000.0,
                    perp_entry=2_001.0,
                    qty=0.1,
                    hedge_ratio=0.0,
                    recovery_state="manual_review",
                )

                with patch.object(trader, "_dispatch_exit") as dispatch_exit:
                    dispatched = trader._dispatch_startup_recovery_exits(
                        trader.state_reader.get_positions()
                    )

                self.assertEqual(dispatched, 0)
                dispatch_exit.assert_not_called()
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_startup_recovery_auto_exit_dispatches_zero_hedge_manual_review_when_enabled(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "testnet"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader._startup_complete_at = (
                    datetime.now(timezone.utc) - timedelta(seconds=61)
                ).isoformat()
                trader._config.apply_updates({"startup_recovery_auto_exit_manual_review": True})
                trader.state_writer.upsert_position(
                    symbol="ETHUSDT",
                    side="LONG_SPOT_SHORT_PERP",
                    direction="long",
                    spot_entry=2_000.0,
                    perp_entry=2_001.0,
                    qty=0.1,
                    hedge_ratio=0.0,
                    recovery_state="manual_review",
                )

                with patch.object(trader, "_dispatch_exit") as dispatch_exit:
                    dispatched = trader._dispatch_startup_recovery_exits(
                        trader.state_reader.get_positions()
                    )

                self.assertEqual(dispatched, 1)
                dispatch_exit.assert_called_once_with("ETHUSDT", urgency=0.9, direction="long")
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_startup_recovery_auto_exit_dispatches_short_manual_review_when_enabled(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "testnet"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader._startup_complete_at = (
                    datetime.now(timezone.utc) - timedelta(seconds=61)
                ).isoformat()
                trader._config.apply_updates({"startup_recovery_auto_exit_manual_review": True})
                trader.state_writer.upsert_position(
                    symbol="ATAUSDT",
                    side="SHORT_SPOT_LONG_PERP",
                    direction="short",
                    spot_entry=0.1,
                    perp_entry=0.1,
                    qty=2_424_564.0,
                    hedge_ratio=0.0,
                    recovery_state="manual_review",
                )

                with patch.object(trader, "_dispatch_exit") as dispatch_exit:
                    dispatched = trader._dispatch_startup_recovery_exits(
                        trader.state_reader.get_positions()
                    )

                self.assertEqual(dispatched, 1)
                dispatch_exit.assert_called_once_with("ATAUSDT", urgency=0.9, direction="short")
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    async def test_trading_loop_dispatches_short_manual_review_startup_recovery_once(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "testnet"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader._runtime_mode = "LIVE"
                trader.subscriber._connected_event.set()
                trader._startup_complete_at = (
                    datetime.now(timezone.utc) - timedelta(seconds=61)
                ).isoformat()
                trader._config.apply_updates({"startup_recovery_auto_exit_manual_review": True})
                trader.state_writer.upsert_position(
                    symbol="ATAUSDT",
                    side="SHORT_SPOT_LONG_PERP",
                    direction="short",
                    spot_entry=0.1,
                    perp_entry=0.1,
                    qty=2_424_564.0,
                    hedge_ratio=0.0,
                    recovery_state="manual_review",
                )
                rows = trader.state_reader.get_positions()

                with patch.object(trader, "_sync_rest_depth_to_tracker", new=AsyncMock()), patch.object(
                    trader._config,
                    "reload_now",
                ), patch.object(
                    trader,
                    "_consume_supervisor_startup_recovery_acknowledgements",
                ), patch.object(
                    trader,
                    "_refresh_open_position_metrics",
                    return_value=rows,
                ), patch.object(
                    trader,
                    "_maybe_process_operator_flatten_all_request",
                    return_value=False,
                ), patch.object(
                    trader,
                    "_sleep_or_shutdown",
                    new=AsyncMock(return_value=True),
                ), patch.object(
                    trader,
                    "_dispatch_exit",
                ) as dispatch_exit:
                    await trader._trading_loop()

                dispatch_exit.assert_called_once_with("ATAUSDT", urgency=0.9, direction="short")
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_startup_recovery_zero_hedge_auto_exit_waits_for_startup_grace(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "testnet"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader._startup_complete_at = datetime.now(timezone.utc).isoformat()
                trader.state_writer.upsert_position(
                    symbol="ETHUSDT",
                    side="LONG_SPOT_SHORT_PERP",
                    direction="long",
                    spot_entry=2_000.0,
                    perp_entry=2_001.0,
                    qty=0.1,
                    hedge_ratio=0.0,
                    recovery_state="manual_review",
                )

                with patch.object(trader, "_dispatch_exit") as dispatch_exit:
                    dispatched = trader._dispatch_startup_recovery_exits(
                        trader.state_reader.get_positions()
                    )

                self.assertEqual(dispatched, 0)
                dispatch_exit.assert_not_called()
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_startup_recovery_partial_manual_review_respects_operator_toggle(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "testnet"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader._startup_complete_at = (
                    datetime.now(timezone.utc) - timedelta(seconds=61)
                ).isoformat()
                trader.state_writer.upsert_position(
                    symbol="ETHUSDT",
                    side="LONG_SPOT_SHORT_PERP",
                    direction="long",
                    spot_entry=2_000.0,
                    perp_entry=2_001.0,
                    qty=0.1,
                    hedge_ratio=0.6,
                    recovery_state="manual_review",
                )

                with patch.object(trader, "_dispatch_exit") as dispatch_exit:
                    dispatched = trader._dispatch_startup_recovery_exits(
                        trader.state_reader.get_positions()
                    )

                self.assertEqual(dispatched, 0)
                dispatch_exit.assert_not_called()
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_manual_review_exit_failure_sets_backoff_without_global_exit_failure(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "testnet"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader.state_writer.upsert_position(
                    symbol="PHBUSDT",
                    side="LONG_SPOT_SHORT_PERP",
                    direction="long",
                    spot_entry=0.09,
                    perp_entry=0.09,
                    qty=51_538.0,
                    hedge_ratio=0.0,
                    ann_funding=0.12,
                    recovery_state="manual_review",
                )
                trader._refresh_startup_recovery_flags(trader.state_reader.get_positions())

                for attempt in range(1, 4):
                    intent_id = f"exit_phbusdt_{attempt}"
                    created_at = datetime.now(timezone.utc).isoformat()
                    trader.state_writer.upsert_pending_intent(
                        intent_id=intent_id,
                        symbol="PHBUSDT",
                        intent_type="EXIT_LONG",
                        status="PENDING_ACK",
                        direction="long",
                        quantity=51_538.0,
                    )
                    trader._pending_exit_intents["PHBUSDT"] = intent_id
                    trader._pending_exit_created_at["PHBUSDT"] = created_at
                    trader._handle_failed_order_update(
                        "PHBUSDT",
                        "REJECTED",
                        client_order_id=f"cid-{attempt}",
                        execution_type="SINGLE_LEG_SUBMISSION_FAILED",
                    )

                self.assertNotIn("exit_failure", trader._safe_mode_flags)
                self.assertEqual(trader._startup_recovery_consecutive_failures["PHBUSDT"], 3)
                self.assertIn("PHBUSDT", trader._startup_recovery_stuck_symbols)
                self.assertIn("naked_leg_unwind_stuck", trader._safe_mode_flags)

                with patch.object(trader, "_dispatch_exit") as dispatch_exit:
                    dispatched = trader._dispatch_startup_recovery_exits(
                        trader.state_reader.get_positions()
                    )

                self.assertEqual(dispatched, 0)
                dispatch_exit.assert_not_called()
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_exit_candidate_exit_failure_stays_symbol_scoped(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "testnet"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader.state_writer.upsert_position(
                    symbol="PHBUSDT",
                    side="LONG_SPOT_SHORT_PERP",
                    direction="long",
                    spot_entry=0.09,
                    perp_entry=0.09,
                    qty=51_538.0,
                    hedge_ratio=1.0,
                    ann_funding=-0.12,
                    recovery_state="exit_candidate",
                )
                trader._refresh_startup_recovery_flags(trader.state_reader.get_positions())

                for attempt in range(1, 4):
                    intent_id = f"exit_phbusdt_candidate_{attempt}"
                    created_at = datetime.now(timezone.utc).isoformat()
                    trader.state_writer.upsert_pending_intent(
                        intent_id=intent_id,
                        symbol="PHBUSDT",
                        intent_type="EXIT_LONG",
                        status="PENDING_ACK",
                        direction="long",
                        quantity=51_538.0,
                    )
                    trader._pending_exit_intents["PHBUSDT"] = intent_id
                    trader._pending_exit_created_at["PHBUSDT"] = created_at
                    trader._handle_failed_order_update(
                        "PHBUSDT",
                        "REJECTED",
                        client_order_id=f"candidate-cid-{attempt}",
                        execution_type="PERCENT_PRICE_FILTER",
                    )

                self.assertNotIn("exit_failure", trader._safe_mode_flags)
                self.assertEqual(trader._startup_recovery_consecutive_failures["PHBUSDT"], 3)
                self.assertIn("PHBUSDT", trader._startup_recovery_stuck_symbols)
                self.assertIn("naked_leg_unwind_stuck", trader._safe_mode_flags)

                trader._refresh_startup_recovery_flags(trader.state_reader.get_positions())
                self.assertIn("PHBUSDT", trader._startup_recovery_stuck_symbols)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_dispatch_exit_zero_qty_startup_recovery_cleans_up_without_global_safe_mode(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader.state_writer.upsert_position(
                    symbol="PHBUSDT",
                    side="LONG_SPOT_SHORT_PERP",
                    direction="long",
                    spot_entry=0.09,
                    perp_entry=0.09,
                    qty=0.0,
                    hedge_ratio=1.0,
                    ann_funding=-0.12,
                    recovery_state="exit_candidate",
                )
                trader._refresh_startup_recovery_flags(trader.state_reader.get_positions())

                event = trader._dispatch_exit("PHBUSDT", urgency=0.9, direction="long")

                self.assertTrue(event.is_set())
                self.assertNotIn("exit_failure", trader._safe_mode_flags)
                self.assertEqual(trader.state_reader.get_positions(), [])
                self.assertNotIn("PHBUSDT", trader._startup_exit_candidates)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    async def test_recovered_position_reclassifies_and_dispatches_exit_when_funding_decays(self):
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
                self.assertEqual(trader._startup_exit_candidates, {})

                trader.funding_ranker.update_rate("BTCUSDT", 0.01 / 1095.0)
                refreshed_positions = trader._refresh_open_position_metrics()

                self.assertEqual(refreshed_positions[0]["recovery_state"], "exit_candidate")
                self.assertIn("BTCUSDT", trader._startup_exit_candidates)

                with patch.object(trader, "_dispatch_exit") as dispatch_exit:
                    dispatched = trader._dispatch_startup_recovery_exits(refreshed_positions)

                self.assertEqual(dispatched, 1)
                dispatch_exit.assert_called_once_with("BTCUSDT", urgency=0.9, direction="long")
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    async def test_live_startup_cancels_open_orders_before_reconciling(self):
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
                trader._preflight_status = "passed"
                trader._config._values["pause_new_entries"] = False
                trader._fetch_exchange_startup_snapshot = AsyncMock(
                    side_effect=[
                        {
                            "futures_account": {
                                "totalMarginBalance": "10000.0",
                                "totalWalletBalance": "9950.0",
                                "availableBalance": "9000.0",
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
                            "futures_open_orders": [
                                {
                                    "symbol": "BTCUSDT",
                                    "clientOrderId": "bngs_live_1",
                                    "status": "NEW",
                                }
                            ],
                            "spot_account": {
                                "balances": [
                                    {"asset": "BTC", "free": "0.50000000", "locked": "0.0"},
                                ]
                            },
                            "spot_open_orders": [],
                            "funding_income": [],
                        },
                        {
                            "futures_account": {
                                "totalMarginBalance": "10000.0",
                                "totalWalletBalance": "9950.0",
                                "availableBalance": "9000.0",
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
                        },
                    ]
                )
                trader._cancel_open_orders = AsyncMock(return_value=[])

                with patch.object(
                    trader.execution,
                    "restore_position_tracking",
                    return_value=True,
                ) as restore_mock:
                    await trader._on_startup()

                risk = trader.state_reader.get_risk()
                positions = trader.state_reader.get_positions()
                self.assertEqual(len(positions), 1)
                self.assertEqual(positions[0]["symbol"], "BTCUSDT")
                self.assertEqual(risk["startup_reconciliation_status"], "needs_review")
                self.assertEqual(risk["startup_reconciliation_cleared_open_order_symbols"], ["BTCUSDT"])
                self.assertEqual(risk["startup_reconciliation_cleared_open_order_count"], 1)
                self.assertFalse(risk["allow_new_risk"])
                trader._cancel_open_orders.assert_awaited_once()
                restore_mock.assert_called_once()
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    async def test_live_startup_blocks_when_open_order_cleanup_fails(self):
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
                trader._fetch_exchange_startup_snapshot = AsyncMock(
                    return_value={
                        "futures_account": {
                            "totalMarginBalance": "10000.0",
                            "totalWalletBalance": "9950.0",
                            "availableBalance": "9000.0",
                        },
                        "position_risk": [],
                        "futures_open_orders": [
                            {
                                "symbol": "BTCUSDT",
                                "clientOrderId": "bngs_live_1",
                                "status": "NEW",
                            }
                        ],
                        "spot_account": {"balances": []},
                        "spot_open_orders": [],
                        "funding_income": [],
                    }
                )
                trader._cancel_open_orders = AsyncMock(return_value=["BTCUSDT:cancel_failed"])

                await trader._on_startup()

                risk = trader.state_reader.get_risk()
                self.assertEqual(risk["startup_reconciliation_status"], "failed")
                self.assertIn("failed to cancel 1 exchange open order", risk["startup_reconciliation_error"])
                self.assertIn("startup_reconciliation_failed", trader._safe_mode_flags)
                self.assertEqual(trader._runtime_mode, "BLOCKED")
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    async def test_live_startup_tracks_unsupported_inverse_style_positions_for_manual_review(self):
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
                trader._preflight_status = "passed"

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

                with patch("scripts.live_trader_v2.requests.get", side_effect=fake_get), patch.object(
                    trader.execution,
                    "restore_position_tracking",
                    return_value=True,
                ) as restore_mock:
                    await trader._on_startup()

                risk = trader.state_reader.get_risk()
                positions = trader.state_reader.get_positions()
                self.assertEqual(risk["startup_reconciliation_status"], "needs_review")
                self.assertEqual(risk["startup_reconciliation_unsupported_directions"], ["BTCUSDT"])
                self.assertEqual(risk["startup_reconciliation_manual_review"], ["BTCUSDT"])
                self.assertEqual(
                    risk["startup_reconciliation_recovery_actions"]["BTCUSDT"]["state"],
                    "manual_review",
                )
                self.assertFalse(risk["allow_new_risk"])
                self.assertEqual(len(positions), 1)
                self.assertEqual(positions[0]["symbol"], "BTCUSDT")
                self.assertEqual(positions[0]["recovery_state"], "manual_review")
                self.assertEqual(trader._runtime_mode, "SAFE_MODE")
                self.assertIn("startup_manual_review", trader._safe_mode_flags)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    # ── Bug 1 tests ───────────────────────────────────────────────────────────

    def test_reconciliation_dust_tolerance_is_testnet_only_and_capped(self):
        cases = (
            ("testnet", "0.75", 0.75),
            ("testnet", "50", 1.0),
            ("testnet", "invalid", 0.01),
            ("live", "1.0", 0.01),
        )
        for index, (mode, configured, expected) in enumerate(cases):
            with self.subTest(mode=mode, configured=configured):
                db_name = os.path.join(
                    tempfile.gettempdir(),
                    self.id().replace(".", "_") + f"_{index}.db",
                )
                with patch.dict(
                    os.environ,
                    {
                        "TRADING_MODE": mode,
                        "BONGUS_TESTNET_RECONCILIATION_DUST_TOLERANCE_USD": configured,
                    },
                    clear=False,
                ):
                    trader = self._build_trader(db_name)
                    try:
                        self.assertAlmostEqual(
                            trader._account_reconciliation_cash_tolerance_usd(),
                            expected,
                        )
                    finally:
                        trader.execution.close()
                        trader.state_reader.close()
                        trader.state_writer.close()
                        if os.path.exists(db_name):
                            os.remove(db_name)

    def test_account_reconciliation_uses_startup_spot_ticker_prices(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "testnet"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                prices = trader._account_reconciliation_asset_prices(
                    {
                        "spot_ticker_prices": {"BTC": 65_000.0, "BCH": 220.0},
                        "spot_account": {
                            "balances": [
                                {"asset": "BTC", "free": "0.00000042", "locked": "0"},
                                {"asset": "BCH", "free": "0.000946", "locked": "0"},
                            ]
                        },
                    }
                )

                self.assertEqual(prices["BTC"], 65_000.0)
                self.assertEqual(prices["BCH"], 220.0)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_flat_account_proof_clears_execution_reconciliation_guard(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "testnet"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                trader._set_safe_mode_flag("execution_reconciliation", True)
                trader.state_writer.set_risk_snapshot(
                    {
                        "execution_reconciliation_required": True,
                        "execution_reconciliation_issue": {"missing_legs": ["perp"]},
                    }
                )
                trader.state_writer.flush()

                cleared = trader._try_clear_execution_reconciliation(
                    SimpleNamespace(ready=True, positions=(), orders=())
                )

                risk = trader.state_reader.get_risk()
                self.assertTrue(cleared)
                self.assertNotIn("execution_reconciliation", trader._safe_mode_flags)
                self.assertFalse(risk["execution_reconciliation_required"])
                self.assertEqual(risk["execution_reconciliation_issue"], {})
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    async def test_startup_recovery_projects_every_one_sided_overhedged_dust_and_exchange_only_shape(self):
        def snapshot(
            *,
            positions: list[dict] | None = None,
            balances: list[dict] | None = None,
        ) -> dict:
            position_rows = positions or []
            return {
                "futures_account": {
                    "positions": position_rows,
                    "totalMarginBalance": "10000",
                    "totalWalletBalance": "10000",
                    "availableBalance": "9000",
                },
                "position_risk": position_rows,
                "futures_open_orders": [],
                "spot_account": {"uid": 12345, "balances": balances or []},
                "spot_open_orders": [],
                "margin_account": {"userAssets": []},
                "margin_account_status": "available",
                "margin_open_orders": [],
                "margin_open_orders_status": "available",
                "funding_income": [],
                "snapshot_errors": {},
            }

        perp = {
            "symbol": "BTCUSDT",
            "positionAmt": "-1",
            "positionSide": "BOTH",
            "entryPrice": "100",
            "markPrice": "100",
            "unRealizedProfit": "0",
            "updateTime": 1_700_000_003_000,
        }
        cases = {
            "spot_only": {
                "snapshot": snapshot(
                    balances=[{"asset": "BTC", "free": "0.1", "locked": "0"}]
                ),
                "prices": {"BTCUSDT": 100.0},
                "expected_codes": {"unassigned_spot_inventory"},
                "position_state": None,
                "ready": False,
            },
            "perpetual_only": {
                "snapshot": snapshot(positions=[perp]),
                "prices": {"BTCUSDT": 100.0},
                "expected_codes": {
                    "managed_position_manual_review",
                    "spot_hedge_shortfall",
                },
                "position_state": "manual_review",
                "ready": False,
            },
            "overhedged": {
                "snapshot": snapshot(
                    positions=[perp],
                    balances=[{"asset": "BTC", "free": "1.5", "locked": "0"}],
                ),
                "prices": {"BTCUSDT": 100.0},
                "expected_codes": {"unassigned_spot_inventory"},
                "position_state": "tracked",
                "seed_local": True,
                "ready": False,
            },
            "dust": {
                "snapshot": snapshot(
                    balances=[{"asset": "DOGE", "free": "0.001", "locked": "0"}]
                ),
                "prices": {"DOGEUSDT": 0.1},
                "expected_codes": set(),
                "position_state": None,
                "ready": True,
            },
            "exchange_only": {
                "snapshot": snapshot(
                    positions=[perp],
                    balances=[{"asset": "BTC", "free": "1", "locked": "0"}],
                ),
                "prices": {"BTCUSDT": 100.0},
                "expected_codes": {"managed_position_manual_review"},
                "position_state": "manual_review",
                "ready": False,
            },
        }

        with patch.dict(
            os.environ,
            {
                "TRADING_MODE": "live",
                "BONGUS_EXPECTED_ACCOUNT_UID": "12345",
            },
            clear=False,
        ):
            for name, case in cases.items():
                with self.subTest(shape=name):
                    db_name = os.path.join(
                        tempfile.gettempdir(),
                        self.id().replace(".", "_") + f"_{name}.db",
                    )
                    trader = self._build_trader(db_name)
                    try:
                        trader._mark_prices.update(case["prices"])
                        if case.get("seed_local"):
                            trader.state_writer.upsert_position(
                                symbol="BTCUSDT",
                                side="LONG_SPOT_SHORT_PERP",
                                direction="long",
                                spot_entry=100.0,
                                perp_entry=100.0,
                                qty=1.0,
                            )
                        with patch.object(
                            trader,
                            "_fetch_exchange_startup_snapshot",
                            new=AsyncMock(return_value=case["snapshot"]),
                        ), patch.object(
                            trader,
                            "_clear_startup_open_orders",
                            new=AsyncMock(return_value=case["snapshot"]),
                        ), patch.object(
                            trader,
                            "_ingest_exchange_statement_snapshot",
                            return_value=False,
                        ):
                            await trader._reconcile_live_startup_state()

                        risk = trader.state_reader.get_risk()
                        codes = {
                            issue["code"]
                            for issue in risk["account_reconciliation_issues"]
                        }
                        self.assertTrue(case["expected_codes"].issubset(codes))
                        self.assertEqual(
                            risk["account_reconciliation_ready"], case["ready"]
                        )
                        positions = trader.state_reader.get_positions()
                        if case["position_state"] is None:
                            self.assertEqual(positions, [])
                        else:
                            self.assertEqual(len(positions), 1)
                            self.assertEqual(
                                positions[0]["recovery_state"],
                                case["position_state"],
                            )
                            if name == "perpetual_only":
                                self.assertEqual(positions[0]["hedge_ratio"], 0.0)
                            if name == "overhedged":
                                self.assertEqual(positions[0]["hedge_ratio"], 1.0)
                    finally:
                        trader.execution.close()
                        trader.state_reader.close()
                        trader.state_writer.close()
                        if os.path.exists(db_name):
                            os.remove(db_name)

    async def test_live_self_heal_cancels_open_order_for_stale_pending_enter(self):
        """Stale ENTER with a live open order on the exchange should be cancelled
        (not skipped forever) so safe-mode is cleared and the slot is freed."""
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
                intent_id = "intent-stale-enter-open-order"
                timed_out_at = (datetime.now(timezone.utc) - timedelta(minutes=12)).isoformat()
                trader.state_writer.upsert_pending_intent(
                    intent_id=intent_id,
                    symbol="LINKUSDT",
                    intent_type="ENTER_LONG",
                    status="TIMEOUT",
                    direction="long",
                    quantity=10.0,
                )
                trader._stale_pending_enters["LINKUSDT"] = {
                    "intent_id": intent_id,
                    "timed_out_at": timed_out_at,
                    "entry_time": timed_out_at,
                    "entry_price": 15.0,
                    "qty": 10.0,
                    "direction": "long",
                    "ann_funding": 0.20,
                }
                trader._refresh_stale_pending_flag()
                self.assertIn("stale_pending_intent", trader._safe_mode_flags)

                cancel_calls: list[tuple] = []

                async def fake_cancel(orders, futures):
                    cancel_calls.append((len(orders), futures))
                    return []  # no failures

                trader._cancel_open_orders = fake_cancel
                trader._fetch_exchange_startup_snapshot = AsyncMock(
                    return_value={
                        "futures_account": {},
                        "position_risk": [],
                        "futures_open_orders": [
                            {
                                "symbol": "LINKUSDT",
                                "orderId": 99,
                                "clientOrderId": "bngs_stale_link",
                                "side": "BUY",
                                "status": "NEW",
                            }
                        ],
                        "spot_open_orders": [],
                        "spot_account": {"balances": []},
                        "funding_income": [],
                    }
                )

                await trader._live_self_heal_stale_pending_intents(datetime.now(timezone.utc))

                # Cancel should have been called for the open order
                self.assertTrue(
                    any(futures for _, futures in cancel_calls),
                    "Expected futures cancel to be called",
                )
                # Stale entry should be cleared
                self.assertNotIn("LINKUSDT", trader._stale_pending_enters)
                # Safe-mode flag should be cleared
                self.assertNotIn("stale_pending_intent", trader._safe_mode_flags)
                # Pending intent should be marked CANCELED (status updated, record retained for audit)
                all_intents = trader.state_reader.get_pending_intents(statuses=["CANCELED", "TIMEOUT"])
                canceled = [i for i in all_intents if i["intent_id"] == intent_id and i["status"] == "CANCELED"]
                self.assertTrue(
                    canceled,
                    "Intent should be marked CANCELED after cancel-and-give-up",
                )
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    async def test_live_self_heal_stale_enter_parks_symbol_after_max_cancel_attempts(self):
        """After _STALE_ENTER_MAX_CANCEL_ATTEMPTS failed cancels the symbol stays
        parked (not crash-looping) and a CRITICAL log is emitted."""
        import scripts.live_trader_v2 as ltv2

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
                intent_id = "intent-stale-enter-cap"
                timed_out_at = (datetime.now(timezone.utc) - timedelta(minutes=12)).isoformat()
                trader.state_writer.upsert_pending_intent(
                    intent_id=intent_id,
                    symbol="1000CATUSDT",
                    intent_type="ENTER_LONG",
                    status="TIMEOUT",
                    direction="long",
                    quantity=100.0,
                )
                trader._stale_pending_enters["1000CATUSDT"] = {
                    "intent_id": intent_id,
                    "timed_out_at": timed_out_at,
                    "entry_time": timed_out_at,
                    "entry_price": 0.01,
                    "qty": 100.0,
                    "direction": "long",
                    "ann_funding": 0.15,
                }
                trader._stale_enter_cancel_attempts["1000CATUSDT"] = ltv2._STALE_ENTER_MAX_CANCEL_ATTEMPTS
                trader._refresh_stale_pending_flag()

                trader._fetch_exchange_startup_snapshot = AsyncMock(
                    return_value={
                        "futures_account": {},
                        "position_risk": [],
                        "futures_open_orders": [
                            {
                                "symbol": "1000CATUSDT",
                                "orderId": 77,
                                "clientOrderId": "bngs_stale_cat",
                                "side": "BUY",
                                "status": "NEW",
                            }
                        ],
                        "spot_open_orders": [],
                        "spot_account": {"balances": []},
                        "funding_income": [],
                    }
                )

                import logging

                with self.assertLogs("live_trader_v2", level="CRITICAL") as log_ctx:
                    await trader._live_self_heal_stale_pending_intents(datetime.now(timezone.utc))

                # Symbol should be abandoned and placed on cooldown, NOT parked.
                self.assertNotIn("1000CATUSDT", trader._stale_pending_enters)
                # CRITICAL log should mention the symbol
                self.assertTrue(
                    any("1000CATUSDT" in msg for msg in log_ctx.output),
                    "Expected CRITICAL log mentioning the stuck symbol",
                )
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    # ── Bug 2 tests ───────────────────────────────────────────────────────────

    def test_calculate_trade_pnl_with_zero_exit_price_still_returns_funding(self):
        """When exit prices are missing (0.0), basis_pnl should be 0 but
        a pre-computed funding_collected_usd must still be returned."""
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                # Simulate: entry prices valid, exit prices missing, funding pre-computed
                net_pnl, funding_collected, basis_pnl, borrow_cost_usd = trader._calculate_trade_pnl(
                    qty=10.0,
                    direction="long",
                    ann_funding=0.1095,  # 10% annualised
                    hold_hours=8.0,
                    funding_periods=1.0,
                    funding_collected_usd=0.80,  # pre-computed by _reconcile_funding_cashflows
                    execution_cost_usd=0.0,
                    spot_entry_price=100.0,
                    perp_entry_price=100.0,
                    spot_exit_price=0.0,   # missing
                    perp_exit_price=0.0,   # missing
                )
                # basis_pnl must be zero (prices invalid)
                self.assertAlmostEqual(basis_pnl, 0.0, places=8)
                # pre-computed funding must pass through unchanged
                self.assertAlmostEqual(funding_collected, 0.80, places=8)
                # Long spot uses owned quote cash; only the inverse short-spot
                # route incurs borrow interest.
                self.assertEqual(borrow_cost_usd, 0.0)
                self.assertAlmostEqual(net_pnl, 0.80, places=8)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_calculate_trade_pnl_returns_all_zeros_when_qty_is_zero(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                result = trader._calculate_trade_pnl(
                    qty=0.0,
                    direction="long",
                    ann_funding=0.20,
                    hold_hours=8.0,
                    funding_collected_usd=5.0,
                    spot_entry_price=100.0,
                    perp_entry_price=100.0,
                    spot_exit_price=101.0,
                    perp_exit_price=101.0,
                )
                self.assertEqual(result, (0.0, 0.0, 0.0, 0.0))
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    async def test_stale_exit_reconciler_records_trade_when_exchange_is_flat(self):
        """When the reconciler finds the exchange is flat for a stale EXIT,
        it should record a trade row (not just wipe the position) so the
        telegram alerter sees a proper PnL entry."""
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
                intent_id = "intent-stale-exit-flat-record"
                entry_time = (datetime.now(timezone.utc) - timedelta(hours=9)).isoformat()
                created_at = (datetime.now(timezone.utc) - timedelta(minutes=12)).isoformat()
                trader.state_writer.upsert_position(
                    symbol="HEIUSDT",
                    side="LONG_SPOT_SHORT_PERP",
                    direction="long",
                    spot_entry=1.0,
                    perp_entry=1.0,
                    qty=1000.0,
                    ann_funding=0.1095,
                    entry_ann_funding=0.1095,
                    updated_at=entry_time,
                )
                trader.state_writer.upsert_pending_intent(
                    intent_id=intent_id,
                    symbol="HEIUSDT",
                    intent_type="EXIT_LONG",
                    status="TIMEOUT",
                    direction="long",
                    quantity=1000.0,
                )
                trader._pending_exit_intents["HEIUSDT"] = intent_id
                trader._pending_exit_created_at["HEIUSDT"] = created_at
                trader._entry_times["HEIUSDT"] = entry_time
                trader._stale_pending_exits.add("HEIUSDT")
                trader._refresh_stale_pending_flag()
                self.assertIn("stale_pending_intent", trader._safe_mode_flags)

                # Exchange is flat — no open orders, no position
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

                # Position must be gone
                self.assertEqual(trader.state_reader.get_positions(), [])
                # A trade record must exist for HEIUSDT
                trades = trader.state_reader.get_trades(limit=10)
                trade = next((t for t in trades if t["symbol"] == "HEIUSDT"), None)
                self.assertIsNotNone(trade, "Expected a trade record after reconciler cleared HEIUSDT")
                assert trade is not None
                # Reconciliation found the exchange flat but could not fetch the
                # actual statement.  The estimate is retained separately and is
                # never promoted into realized funding/PnL.
                self.assertEqual(trade["funding_collected"], 0.0)
                self.assertEqual(trade["funding_source"], "missing_actual")
                self.assertEqual(trade["economic_status"], "INCOMPLETE")
                self.assertGreater(trade["estimated_funding_collected"], 0.0)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)
