"""Order-lifecycle tests for LiveTraderV2 — regression coverage for the
2026-04-21 fix plan (§4.4: Rust-side ENTER rejections must clear the
corresponding `_pending_enters` entry immediately)."""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import scripts.live_trader_v2
from bongus.engine.state_store import StateReader, StateWriter
from scripts.live_trader_v2 import LiveTraderV2


class TestOrderRejectedLifecycle(IsolatedAsyncioTestCase):
    def _build_trader(self, db_path: str) -> LiveTraderV2:
        config_path = db_path + ".config.json"
        with patch("scripts.live_trader_v2.ConfigManager.start_watching", autospec=True):
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

    def test_enter_rejection_clears_pending_enter(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                intent_id = "intent-truusdt-chase-active"
                trader.state_writer.upsert_pending_intent(
                    intent_id=intent_id,
                    symbol="TRUUSDT",
                    intent_type="ENTER_LONG",
                    direction="long",
                    status="PENDING",
                    quantity=526074.0,
                )
                trader._pending_enters["TRUUSDT"] = {
                    "intent_id": intent_id,
                    "entry_time": datetime.now(timezone.utc).isoformat(),
                    "entry_price": 0.01,
                    "qty": 526074.0,
                    "direction": "long",
                    "ann_funding": 0.25,
                }

                trader._on_order_rejected(
                    symbol="TRUUSDT",
                    intent="ENTER_LONG",
                    intent_id=intent_id,
                    reason="chase_active",
                )

                self.assertNotIn("TRUUSDT", trader._pending_enters)
                # The pending_intents row should be gone (deleted via
                # _resolve_pending_intent) rather than left as PENDING.
                remaining = [
                    row for row in trader.state_reader.get_pending_intents(statuses=["PENDING"])
                    if str(row.get("intent_id") or "") == intent_id
                ]
                self.assertEqual(remaining, [])
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)

    def test_enter_rejection_ignores_mismatched_intent_id(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                tracked_id = "intent-current"
                trader._pending_enters["TRUUSDT"] = {
                    "intent_id": tracked_id,
                    "entry_time": datetime.now(timezone.utc).isoformat(),
                    "entry_price": 0.01,
                    "qty": 1.0,
                    "direction": "long",
                    "ann_funding": 0.1,
                }

                trader._on_order_rejected(
                    symbol="TRUUSDT",
                    intent="ENTER_LONG",
                    intent_id="intent-superseded",
                    reason="chase_active",
                )

                # Mismatched intent_id must not pop the current row.
                self.assertIn("TRUUSDT", trader._pending_enters)
            finally:
                trader.execution.close()
                trader.state_reader.close()
                trader.state_writer.close()
                if os.path.exists(db_name):
                    os.remove(db_name)
