from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bongus.testing.daily_reconciliation_evidence import (
    DailyReconciliationError,
    append_record,
    build_bundle,
    build_interval,
    verify_journal,
)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _snapshot(*, balance: str = "100", position: str = "0", identity: bool = True) -> dict:
    return {
        "observed_at": NOW.isoformat(),
        "snapshot_complete": True,
        "account_identity_verified": identity,
        "combined_balances": {"USDT": balance},
        "balance_tolerances": {"USDT": "0.01"},
        "asset_prices_usd": {"USDT": "1", "BTC": "50000"},
        # Zero values are explicit.  Sparse/missing balance or position keys are
        # UNKNOWN to the reconciler, never an implicit zero.
        "perpetual_positions": {"BTCUSDT:BOTH": position},
        "position_tolerance": "0.00000001",
    }


def _daily_nav(*, closing: str | None = "101") -> dict[str, str | None]:
    return {
        "opening_nav_usd": "100",
        "closing_nav_usd": closing,
        "external_deposits_usd": "0",
        "external_withdrawals_usd": "0",
        "realized_price_pnl_usd": "0.5",
        "actual_funding_usd": "0.7",
        "commission_cost_usd": "0.1",
        "borrow_interest_cost_usd": "0.1",
        "unrealized_pnl_change_usd": "0",
        "stablecoin_fx_movement_usd": "0",
        "internal_transfers_usd": "0",
        "tolerance_usd": "0.01",
    }


def test_first_record_is_baseline_only_and_hash_chained(tmp_path: Path) -> None:
    journal = tmp_path / "journal"
    record, _ = append_record(
        journal,
        observed_at=NOW.isoformat(),
        environment="testnet",
        account_ref={"uri": "account.json", "sha256": "a" * 64},
        snapshot=_snapshot(),
        interval=None,
    )
    records = verify_journal(journal)
    assert records == [record]
    bundle, _ = build_bundle(
        records,
        journal_directory=journal,
        output_directory=tmp_path / "out",
        generated_at=NOW,
    )
    assert bundle["days"] == []
    assert bundle["machine_attestation"]["baseline_only"] is True
    assert bundle["machine_attestation"]["all_intervals_reconciled"] is False


def test_matching_exchange_and_ledger_deltas_reconcile() -> None:
    previous = _snapshot()
    current = {
        **_snapshot(balance="101", position="0.001"),
        "observed_at": (NOW + timedelta(days=1)).isoformat(),
    }
    interval = build_interval(
        previous,
        current,
        ledger_balance_deltas={"USDT": "1"},
        ledger_position_deltas={"BTCUSDT": "0.001"},
        ledger_event_count=2,
        ledger_unvalued_event_count=0,
        ledger_rows_sha256="b" * 64,
        daily_nav_components=_daily_nav(),
    )
    assert interval["within_exchange_precision"] is True
    assert interval["unexplained_max_usd"] == 0.0
    assert interval["status"] == "RECONCILED"


def test_unexplained_or_unverified_delta_fails_closed() -> None:
    previous = _snapshot(identity=False)
    current = {
        **_snapshot(balance="102"),
        "observed_at": (NOW + timedelta(days=1)).isoformat(),
    }
    interval = build_interval(
        previous,
        current,
        ledger_balance_deltas={"USDT": "1"},
        ledger_position_deltas={"BTCUSDT": "0"},
        ledger_event_count=1,
        ledger_unvalued_event_count=0,
        ledger_rows_sha256="c" * 64,
        daily_nav_components=_daily_nav(),
    )
    assert interval["within_exchange_precision"] is False
    assert interval["unexplained_max_usd"] == 1.0
    assert interval["prerequisites"]["previous_account_identity_verified"] is False


def test_absent_balance_position_and_nav_components_remain_unknown() -> None:
    previous = _snapshot()
    current = {
        **_snapshot(balance="101", position="0.001"),
        "observed_at": (NOW + timedelta(days=1)).isoformat(),
    }
    del previous["combined_balances"]["USDT"]
    del previous["perpetual_positions"]["BTCUSDT:BOTH"]
    nav = _daily_nav()
    nav["actual_funding_usd"] = None

    interval = build_interval(
        previous,
        current,
        ledger_balance_deltas={"USDT": "1"},
        ledger_position_deltas={"BTCUSDT": "0.001"},
        ledger_event_count=2,
        ledger_unvalued_event_count=0,
        ledger_rows_sha256="e" * 64,
        daily_nav_components=nav,
    )

    assert interval["status"] == "PNL_INCOMPLETE"
    assert interval["balance_differences"]["USDT"] == "UNKNOWN"
    assert interval["position_differences"]["BTCUSDT:BOTH"] == "UNKNOWN"
    assert "balance:USDT:previous_balance" in interval["unknown_components"]
    assert "position:BTCUSDT:BOTH:previous_position" in interval["unknown_components"]
    assert interval["daily_nav"]["actual_funding_usd"] == "UNKNOWN"
    assert interval["daily_nav"]["projected_closing_nav_usd"] == "UNKNOWN"
    assert interval["unexplained_max_usd"] is None


def test_complete_drivers_without_close_are_projected_not_finalized() -> None:
    previous = _snapshot()
    current = {
        **_snapshot(balance="101"),
        "observed_at": (NOW + timedelta(days=1)).isoformat(),
    }
    interval = build_interval(
        previous,
        current,
        ledger_balance_deltas={"USDT": "1"},
        ledger_position_deltas={"BTCUSDT": "0"},
        ledger_event_count=1,
        ledger_unvalued_event_count=0,
        ledger_rows_sha256="f" * 64,
        daily_nav_components=_daily_nav(closing=None),
    )

    assert interval["status"] == "PROJECTED"
    assert interval["daily_nav"]["projected_closing_nav_usd"] == "101.0"
    assert interval["daily_nav"]["closing_nav_usd"] == "UNKNOWN"
    assert interval["within_exchange_precision"] is False


def test_journal_tamper_is_detected(tmp_path: Path) -> None:
    journal = tmp_path / "journal"
    _, path = append_record(
        journal,
        observed_at=NOW.isoformat(),
        environment="paper",
        account_ref={"uri": "account.json", "sha256": "d" * 64},
        snapshot=_snapshot(),
        interval=None,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["snapshot"]["combined_balances"]["USDT"] = "999"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DailyReconciliationError, match="hash mismatch"):
        verify_journal(journal)
