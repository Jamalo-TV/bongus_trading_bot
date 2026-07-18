from __future__ import annotations

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import threading
from typing import Any

import pytest

from bongus.engine.exchange_filters import ExchangeFilterRegistry
from bongus.market_data.funding_calendar import FundingCalendar


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "execution_engine" / "Cargo.toml"
UTC = timezone.utc


def _exchange_info(
    *,
    tick_size: str = "0.01",
    step_size: str = "0.001",
    min_notional: str = "5",
    status: str = "TRADING",
) -> dict[str, Any]:
    return {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "status": status,
                "filters": [
                    {
                        "filterType": "PRICE_FILTER",
                        "minPrice": "0.01",
                        "maxPrice": "1000000",
                        "tickSize": tick_size,
                    },
                    {
                        "filterType": "LOT_SIZE",
                        "minQty": step_size,
                        "maxQty": "100",
                        "stepSize": step_size,
                    },
                    {
                        "filterType": "MARKET_LOT_SIZE",
                        "minQty": step_size,
                        "maxQty": "50",
                        "stepSize": step_size,
                    },
                    {
                        "filterType": "NOTIONAL",
                        "minNotional": min_notional,
                        "maxNotional": "10000000",
                        "applyMinToMarket": True,
                        "applyMaxToMarket": True,
                    },
                ],
            }
        ]
    }


def test_active_cycle_mutates_every_filter_and_funding_interval_across_processes() -> None:
    scenarios = [
        (_exchange_info(), 8),
        (_exchange_info(tick_size="0.10"), 8),
        (_exchange_info(step_size="0.01"), 8),
        (_exchange_info(min_notional="1000"), 8),
        (_exchange_info(status="BREAK"), 8),
        (_exchange_info(), 4),
    ]
    endpoint_counts = {"spot": 0, "perp": 0}
    lock = threading.Lock()

    class MetadataExchange(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:
            market = "perp" if self.path.startswith("/fapi/") else "spot"
            with lock:
                stage = endpoint_counts[market]
                endpoint_counts[market] += 1
            body = json.dumps(scenarios[stage][0], separators=(",", ":")).encode(
                "utf-8"
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), MetadataExchange)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        completed = subprocess.run(
            [
                "cargo",
                "run",
                "--quiet",
                "--locked",
                "--manifest-path",
                str(MANIFEST),
                "--",
                "--metadata-change-harness",
                f"http://127.0.0.1:{server.server_port}",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)

    rust_stages = json.loads(completed.stdout)["stages"]
    assert endpoint_counts == {"spot": 6, "perp": 6}
    assert [stage["available"] for stage in rust_stages] == [
        True,
        True,
        True,
        True,
        False,
        True,
    ]
    assert rust_stages[1]["spot_tick_size"] == pytest.approx(0.1)
    assert rust_stages[1]["futures_tick_size"] == pytest.approx(0.1)
    assert rust_stages[2]["spot_step_size"] == pytest.approx(0.01)
    assert rust_stages[2]["futures_step_size"] == pytest.approx(0.01)
    assert rust_stages[3]["spot_min_notional"] == pytest.approx(1000.0)
    assert rust_stages[3]["futures_min_notional"] == pytest.approx(1000.0)

    registry = ExchangeFilterRegistry(metadata_ttl_seconds=60.0, clock=lambda: 100.0)
    calendar = FundingCalendar()
    observed_reasons: list[tuple[str, ...]] = []
    observed_at = datetime(2026, 7, 18, 9, 0, tzinfo=UTC)
    for stage, (exchange_info, funding_interval) in enumerate(scenarios):
        update = registry.replace_market(
            "spot", exchange_info, received_at=100.0 + stage
        )
        registry.replace_market("perp", exchange_info, received_at=100.0 + stage)
        assert update.changed_symbols == ("BTCUSDT",)
        calendar.update_funding_info(
            [
                {
                    "symbol": "BTCUSDT",
                    "fundingIntervalHours": funding_interval,
                }
            ],
            observed_at=observed_at,
        )
        result = registry.validate_order(
            symbol="BTCUSDT",
            market="spot",
            side="BUY",
            order_type="LIMIT",
            quantity="0.011",
            price="60000.01",
            now=100.0 + stage,
        )
        observed_reasons.append(result.reasons)

    assert observed_reasons == [
        (),
        ("price_off_tick",),
        ("quantity_off_step",),
        ("notional_below_minimum",),
        ("symbol_status:BREAK",),
        (),
    ]
    calendar.update_premium_index(
        {
            "symbol": "BTCUSDT",
            "nextFundingTime": int(
                datetime(2026, 7, 18, 12, 0, tzinfo=UTC).timestamp() * 1000
            ),
        },
        observed_at=observed_at,
    )
    assert calendar.interval_hours("BTCUSDT") == 4
    assert calendar.next_settlement("BTCUSDT", after=observed_at) == datetime(
        2026, 7, 18, 12, 0, tzinfo=UTC
    )
