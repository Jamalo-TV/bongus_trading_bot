from __future__ import annotations

from typing import Any

from bongus.exchanges.binance_account_snapshot import BinanceAccountSnapshotClient
from scripts.collect_testnet_account_evidence import build_artifact


class _Response:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        return self._payload


def test_snapshot_client_uses_get_only_and_marks_demo_margin_unknown() -> None:
    calls: list[str] = []

    def fake_get(url: str, **_kwargs: Any) -> _Response:
        calls.append(url)
        path = url.split("?", 1)[0]
        if path.endswith(("/fapi/v1/time", "/api/v3/time")):
            return _Response({"serverTime": 1_700_000_000_000})
        if path.endswith("/fapi/v3/account") or path.endswith(
            "/fapi/v3/positionRisk"
        ):
            return _Response({"code": -404, "msg": "not found"}, 404)
        if path.endswith("/fapi/v2/account"):
            return _Response({"positions": []})
        if path.endswith("/fapi/v2/positionRisk"):
            return _Response([])
        if path.endswith("/fapi/v1/positionSide/dual"):
            return _Response({"dualSidePosition": False})
        if path.endswith(("/fapi/v1/openOrders", "/api/v3/openOrders")):
            return _Response([])
        if path.endswith("/fapi/v1/income"):
            return _Response([])
        if path.endswith("/api/v3/account"):
            return _Response({"uid": 7, "balances": []})
        if path.endswith("/api/v3/myTrades"):
            return _Response(
                [
                    {
                        "id": 11,
                        "orderId": 12,
                        "price": "60000.125",
                        "qty": "0.001",
                        "quoteQty": "60.000125",
                        "commission": "0.000001",
                        "commissionAsset": "BTC",
                        "time": 1_700_000_000_001,
                        "isBuyer": True,
                        "isMaker": False,
                    }
                ]
            )
        if "/sapi/v1/margin/" in path:
            return _Response("missing", 404)
        if path.endswith("/api/v3/ticker/price"):
            return _Response([{"symbol": "BTCUSDT", "price": "60000"}])
        raise AssertionError(path)

    client = BinanceAccountSnapshotClient(
        futures_base_url="https://futures.invalid",
        spot_base_url="https://spot.invalid",
        futures_api_key="key",
        futures_api_secret="secret",
        spot_api_key="spot-key",
        spot_api_secret="spot-secret",
        request_get=fake_get,
        spot_trade_symbols=("BTCUSDT",),
    )
    snapshot, prices, statuses = client.collect()

    assert calls
    assert snapshot["futures_account"] == {"positions": []}
    assert snapshot["futures_position_mode"] == {"dualSidePosition": False}
    assert snapshot["spot_trades"][0]["symbol"] == "BTCUSDT"
    assert snapshot["spot_trades"][0]["price"] == "60000.125"
    assert snapshot["spot_trades_status"] == "available"
    assert snapshot["availability_time"]
    assert snapshot["margin_account_status"] == "unknown"
    assert snapshot["margin_open_orders_status"] == "unknown"
    assert prices == {"BTC": "60000"}
    assert statuses["futures_account"] == "available"


def test_artifact_fails_closed_without_dedicated_uid_and_preserves_read_only_policy() -> None:
    snapshot = {
        "futures_account": {"positions": []},
        "position_risk": [],
        "futures_open_orders": [],
        "spot_account": {"uid": 7, "balances": []},
        "spot_open_orders": [],
        "margin_account": None,
        "margin_account_status": "disabled",
        "margin_open_orders": [],
        "margin_open_orders_status": "disabled",
        "snapshot_errors": {},
        "funding_income": [],
    }
    artifact = build_artifact(
        snapshot=snapshot,
        prices={},
        endpoint_statuses={},
        local_positions=[],
        pending_intents=[],
        expected_uid="",
        account_id="binance-default",
        generated_at="2026-07-18T00:00:00+00:00",
    )

    assert artifact["collection_policy"] == {
        "read_only": True,
        "http_methods": ["GET"],
        "orders_cancelled": 0,
        "orders_submitted": 0,
        "transfers_submitted": 0,
    }
    assert artifact["reconciliation"]["ready"] is False
    assert artifact["gate_metrics"]["actual_funding_settlements_observed"] == 0
    assert artifact["exchange_reconciliation_snapshot"] == {
        "observed_at": "2026-07-18T00:00:00+00:00",
        "snapshot_complete": True,
        "account_identity_verified": False,
        "combined_balances": {},
        "balance_tolerances": {},
        "asset_prices_usd": {
            "BUSD": "1",
            "FDUSD": "1",
            "USD": "1",
            "USDC": "1",
            "USDT": "1",
        },
        "perpetual_positions": {},
        "position_tolerance": "0.00000001",
    }
    assert artifact["reconciliation"]["issues"][0]["code"] == (
        "dedicated_account_identity_unconfigured"
    )
