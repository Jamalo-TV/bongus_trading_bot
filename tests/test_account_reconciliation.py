from __future__ import annotations

from decimal import Decimal

from bongus.engine.account_reconciliation import (
    BOT_CLIENT_ORDER_PREFIX,
    bot_owned_orders,
    is_bot_client_order_id,
    make_bot_client_order_id,
    reconcile_account_snapshot,
    unrelated_orders,
)


def _complete_snapshot() -> dict:
    return {
        "futures_account": {"positions": []},
        "position_risk": [],
        "futures_open_orders": [],
        "spot_account": {"balances": []},
        "spot_open_orders": [],
        "margin_account": {"userAssets": []},
        "margin_account_status": "available",
        "margin_open_orders": [],
        "margin_open_orders_status": "available",
        "snapshot_errors": {},
    }


def test_bot_client_order_namespace_is_valid_and_stable() -> None:
    first = make_bot_client_order_id(leg="spot", intent_id="enter_btc_1", nonce="0")
    second = make_bot_client_order_id(leg="spot", intent_id="enter_btc_1", nonce="0")
    other = make_bot_client_order_id(leg="futures", intent_id="enter_btc_1", nonce="0")

    assert first == second
    assert first != other
    assert first.startswith(BOT_CLIENT_ORDER_PREFIX)
    assert len(first) <= 36
    assert is_bot_client_order_id(first)
    assert not is_bot_client_order_id("manual-order")
    assert not is_bot_client_order_id("bngs_bad client id")


def test_order_partition_never_treats_symbol_match_as_ownership() -> None:
    orders = [
        {"symbol": "BTCUSDT", "clientOrderId": "bngs_fut_123"},
        {"symbol": "BTCUSDT", "clientOrderId": "operator-order"},
        {"symbol": "BTCUSDT", "orderId": 3},
    ]

    assert list(bot_owned_orders(orders)) == [orders[0]]
    assert list(unrelated_orders(orders)) == [orders[1], orders[2]]


def test_fully_matched_long_pair_is_ready() -> None:
    snapshot = _complete_snapshot()
    position = {
        "symbol": "BTCUSDT",
        "positionAmt": "-0.5",
        "positionSide": "BOTH",
        "markPrice": "60000",
    }
    snapshot["position_risk"] = [position]
    snapshot["futures_account"]["positions"] = [position]
    snapshot["spot_account"]["balances"] = [
        {"asset": "BTC", "free": "0.5", "locked": "0"},
        {"asset": "USDT", "free": "1000", "locked": "0"},
    ]

    report = reconcile_account_snapshot(
        snapshot,
        local_positions=[{"symbol": "BTCUSDT", "qty": 0.5, "direction": "long"}],
        asset_prices_usd={"BTC": 60000},
        generated_at="2026-01-01T00:00:00+00:00",
    )

    assert report.ready
    assert report.snapshot_complete
    assert report.blocking_issues == ()
    assert report.positions[0].classification == "matched"
    assert report.fingerprint == reconcile_account_snapshot(
        snapshot,
        local_positions=[{"symbol": "BTCUSDT", "qty": 0.5, "direction": "long"}],
        asset_prices_usd={"BTC": 60000},
        generated_at="2026-01-02T00:00:00+00:00",
    ).fingerprint


def test_exchange_only_position_is_adoptable_but_not_ready() -> None:
    snapshot = _complete_snapshot()
    position = {
        "symbol": "ETHUSDT",
        "positionAmt": "-2",
        "positionSide": "SHORT",
        "markPrice": "3000",
    }
    snapshot["position_risk"] = [position]
    snapshot["futures_account"]["positions"] = [position]
    snapshot["spot_account"]["balances"] = [
        {"asset": "ETH", "free": "2", "locked": "0"}
    ]

    report = reconcile_account_snapshot(
        snapshot,
        local_positions=[],
        asset_prices_usd={"ETH": 3000},
    )

    assert not report.ready
    assert report.exchange_only_symbols == ("ETHUSDT",)
    assert report.positions[0].classification == "exchange_only"
    assert {issue.code for issue in report.blocking_issues} == {"exchange_only_position"}
    assert report.blocking_issues[0].incident_id.startswith("recon_")


def test_external_order_blocks_readiness_but_is_never_cancellable() -> None:
    snapshot = _complete_snapshot()
    snapshot["futures_open_orders"] = [
        {
            "symbol": "BTCUSDT",
            "clientOrderId": "operator_order_1",
            "orderId": 42,
            "status": "NEW",
        }
    ]

    report = reconcile_account_snapshot(snapshot, local_positions=[])

    assert not report.ready
    assert len(report.unrelated_orders) == 1
    assert not report.unrelated_orders[0].cancellable_by_bot
    assert report.unrelated_orders[0].ownership.value == "external"
    assert {issue.code for issue in report.blocking_issues} == {"unrelated_open_order"}


def test_bot_order_requires_durable_symbol_or_intent_lineage() -> None:
    snapshot = _complete_snapshot()
    snapshot["spot_open_orders"] = [
        {"symbol": "SOLUSDT", "clientOrderId": "bngs_spot_123", "status": "NEW"}
    ]

    orphan = reconcile_account_snapshot(snapshot, local_positions=[])
    linked = reconcile_account_snapshot(
        snapshot,
        local_positions=[],
        pending_intents=[
            {"intent_id": "enter_sol_1", "symbol": "SOLUSDT", "status": "PENDING_ACK"}
        ],
    )

    assert not orphan.ready
    assert orphan.orders[0].ownership.value == "bot_owned_orphan"
    assert {issue.code for issue in orphan.blocking_issues} == {"orphan_bot_order"}
    assert linked.ready
    assert linked.orders[0].ownership.value == "bot_owned"


def test_margin_liability_must_match_inverse_position() -> None:
    snapshot = _complete_snapshot()
    position = {
        "symbol": "XRPUSDT",
        "positionAmt": "100",
        "positionSide": "LONG",
        "markPrice": "0.5",
    }
    snapshot["position_risk"] = [position]
    snapshot["futures_account"]["positions"] = [position]
    snapshot["margin_account"] = {
        "userAssets": [
            {"asset": "XRP", "borrowed": "100", "interest": "0", "free": "0"}
        ]
    }

    report = reconcile_account_snapshot(
        snapshot,
        local_positions=[{"symbol": "XRPUSDT", "qty": 100, "direction": "short"}],
        asset_prices_usd={"XRP": Decimal("0.5")},
    )

    assert report.ready
    assert report.liabilities[0].classification == "matched"
    assert report.liabilities[0].allocated_quantity == "100"


def test_unassigned_liability_and_missing_margin_truth_fail_closed() -> None:
    snapshot = _complete_snapshot()
    snapshot["margin_account"] = {
        "userAssets": [
            {"asset": "BTC", "borrowed": "0.01", "interest": "0.0001"}
        ]
    }
    unexplained = reconcile_account_snapshot(
        snapshot,
        local_positions=[],
        asset_prices_usd={"BTC": 60000},
    )

    snapshot["margin_account"] = None
    snapshot["margin_account_status"] = "error"
    missing = reconcile_account_snapshot(snapshot, local_positions=[])

    assert not unexplained.ready
    assert "unassigned_margin_liability" in {issue.code for issue in unexplained.blocking_issues}
    assert not missing.ready
    assert not missing.snapshot_complete
    assert "margin_liability_endpoint_unverified" in {
        issue.code for issue in missing.blocking_issues
    }


def test_spot_residual_uses_cash_precision_and_unknown_price_fails_closed() -> None:
    snapshot = _complete_snapshot()
    snapshot["spot_account"]["balances"] = [
        {"asset": "DOGE", "free": "0.001", "locked": "0"}
    ]

    dust = reconcile_account_snapshot(
        snapshot,
        local_positions=[],
        asset_prices_usd={"DOGE": "0.1"},
    )
    unknown = reconcile_account_snapshot(snapshot, local_positions=[])

    assert dust.ready
    assert not unknown.ready
    assert {issue.code for issue in unknown.blocking_issues} == {"unvalued_spot_inventory"}


def test_position_endpoint_disagreement_and_local_only_are_blockers() -> None:
    snapshot = _complete_snapshot()
    snapshot["position_risk"] = [
        {"symbol": "BTCUSDT", "positionAmt": "-0.1", "positionSide": "BOTH"}
    ]
    snapshot["futures_account"]["positions"] = [
        {"symbol": "BTCUSDT", "positionAmt": "-0.2", "positionSide": "BOTH"}
    ]
    snapshot["spot_account"]["balances"] = [
        {"asset": "BTC", "free": "0.1", "locked": "0"}
    ]

    report = reconcile_account_snapshot(
        snapshot,
        local_positions=[
            {"symbol": "BTCUSDT", "qty": 0.1, "direction": "long"},
            {"symbol": "ETHUSDT", "qty": 1, "direction": "long"},
        ],
        asset_prices_usd={"BTC": 60000},
    )

    codes = {issue.code for issue in report.blocking_issues}
    assert "futures_position_endpoint_mismatch" in codes
    assert "local_only_position" in codes
    assert report.local_only_symbols == ("ETHUSDT",)


def test_dedicated_account_identity_requires_explicit_matching_uid() -> None:
    snapshot = _complete_snapshot()
    snapshot["spot_account"]["uid"] = 12345

    unconfigured = reconcile_account_snapshot(
        snapshot,
        local_positions=[],
        require_account_uid=True,
    )
    mismatch = reconcile_account_snapshot(
        snapshot,
        local_positions=[],
        require_account_uid=True,
        expected_account_uid="67890",
    )
    matched = reconcile_account_snapshot(
        snapshot,
        local_positions=[],
        require_account_uid=True,
        expected_account_uid="12345",
    )

    assert not unconfigured.ready
    assert {issue.code for issue in unconfigured.blocking_issues} == {
        "dedicated_account_identity_unconfigured"
    }
    assert not mismatch.ready
    assert {issue.code for issue in mismatch.blocking_issues} == {
        "dedicated_account_identity_mismatch"
    }
    assert matched.ready
