import json
import os
import sys
import tempfile
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from bongus.engine.state_store import StateReader, StateWriter
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
        return trader

    def test_calculate_trade_pnl_prorates_annualized_funding(self):
        db_name = os.path.join(tempfile.gettempdir(), self.id().replace(".", "_") + ".db")
        with patch.dict(os.environ, {"TRADING_MODE": "paper"}, clear=False):
            trader = self._build_trader(db_name)
            try:
                net_pnl, funding_collected = trader._calculate_trade_pnl(
                    entry_price=100.0,
                    exit_price=100.0,
                    qty=10.0,
                    direction="long",
                    ann_funding=0.1095,
                    hold_hours=8.0,
                )
                self.assertAlmostEqual(funding_collected, 0.1, places=6)
                self.assertAlmostEqual(net_pnl, 0.1, places=6)
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
                )

                positions = trader.state_reader.get_positions()
                self.assertEqual(len(positions), 1)
                self.assertAlmostEqual(positions[0]["ann_funding"], 0.245)
                self.assertEqual(positions[0]["spot_live"], 101.0)
                self.assertEqual(positions[0]["perp_live"], 101.0)
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
