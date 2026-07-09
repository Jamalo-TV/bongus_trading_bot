import time
from typing import Any, cast

from bongus.core.config_manager import ConfigManager
from bongus.engine.state_store import StateReader, StateWriter
from bongus.runtime.live_trader import CanonicalMultiSymbolTrader


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeSession:
    def get(self, url, timeout=10):
        if "fapi.binance.com/fapi/v1/exchangeInfo" in url:
            return _FakeResponse(
                {
                    "symbols": [
                        {
                            "symbol": "BTCUSDT",
                            "contractType": "PERPETUAL",
                            "quoteAsset": "USDT",
                            "onboardDate": 1_700_000_000_000,
                            "status": "TRADING",
                        }
                    ]
                }
            )
        if "api.binance.com/api/v3/exchangeInfo" in url:
            return _FakeResponse(
                {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING", "quoteAsset": "USDT"}]}
            )
        return _FakeResponse(
            [
                {
                    "symbol": "BTCUSDT",
                    "lastFundingRate": "0.0100",
                    "markPrice": "100.0",
                    "indexPrice": "99.9",
                }
            ]
        )

    def close(self):
        return None


class _StubExecutionClient:
    def __init__(self):
        self.payloads = []

    def send_order_intent(self, payload):
        self.payloads.append(payload)

    def close(self):
        return None


def test_canonical_trader_run_cycle_records_runtime_and_shadow(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    db_path = tmp_path / "state.db"
    config_path = tmp_path / "live_config.json"
    writer = StateWriter(str(db_path))
    reader = StateReader(str(db_path))
    config_manager = ConfigManager(config_path)
    config_manager.write_overrides(
        {
            "notional_per_trade": 1_000.0,
            "min_expected_edge_bps": 0.0,
            "min_incremental_portfolio_edge_bps": 0.0,
            "scanner_min_depth_usd": 1.0,
            "scanner_min_depth_multiplier": 1.0,
            "target_concurrent_positions": 1,
            "min_top_n": 1,
            "max_top_n": 1,
        }
    )
    trader = CanonicalMultiSymbolTrader(
        writer=writer,
        reader=reader,
        config_manager=config_manager,
        execution_client=cast(Any, _StubExecutionClient()),
        session=cast(Any, _FakeSession()),
    )
    trader.refresh_universe()
    trader.depth_tracker.on_book_ticker("BTCUSDT", 99.95, 100.05)
    trader.depth_tracker.on_l2_depth("BTCUSDT", [(99.95, 2_500.0)], [(100.05, 2_500.0)])
    trader.last_telemetry_ts = time.time()

    first = trader.run_cycle()
    second = trader.run_cycle()

    assert first["selected"][0]["symbol"] == "BTCUSDT"
    assert second["selected"][0]["symbol"] == "BTCUSDT"
    assert reader.get_stats()["scanner_breadth"] == 1.0
    assert reader.get_candidate_snapshots()[0]["symbol"] == "BTCUSDT"
    assert reader.get_shadow_decisions()[0]["symbol"] == "BTCUSDT"
    assert reader.get_feature_snapshots()[0]["symbol"] == "BTCUSDT"

    trader.close()
