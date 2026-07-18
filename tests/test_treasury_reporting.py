from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import sqlite3

import pytest

from bongus.engine.economic_ledger import EconomicLedgerEvent, apply_economic_ledger_migration, ingest_economic_events
from bongus.engine.account_reconciliation import reconcile_account_snapshot
from bongus.portfolio.capital_reservations import (
    CapitalReservationBook,
    CapitalState,
    ReservationPolicy,
    ReservationPurpose,
    ReservationRequest,
)
from bongus.portfolio.treasury import ReservationAwareTreasury, TreasuryPolicy
from bongus.supervisor.daily_report import AlertTransitionTracker, build_reconciled_daily_report


NOW = datetime(2026, 7, 18, 12, tzinfo=timezone.utc)


def capital(**overrides):
    values = dict(
        equity_usd="10000",
        spot_cash_available_usd="6000",
        futures_margin_available_usd="2000",
        current_pair_gross_usd="0",
        max_pair_gross_usd="10000",
    )
    values.update(overrides)
    return CapitalState(**values)


def reservation_policy():
    return ReservationPolicy("500", "250", "1000")


def test_treasury_is_proposal_only_and_never_uses_reserved_assets(tmp_path) -> None:
    book = CapitalReservationBook(str(tmp_path / "capital.db"))
    admitted = book.reserve(
        ReservationRequest(
            "entry-1", ReservationPurpose.ENTRY, "BTCUSDT", "c1",
            "3000", "250", "10", "5000", "cfg",
        ),
        capital=capital(),
        policy=reservation_policy(),
        now=NOW,
    )
    assert admitted.allowed
    treasury = ReservationAwareTreasury(book)
    proposal = treasury.propose(
        capital=capital(),
        reservation_policy=reservation_policy(),
        treasury_policy=TreasuryPolicy("500", "1000", maximum_transfer_usd="500"),
        reconciliation_matched=True,
        critical_incident_active=False,
        now=NOW,
    )
    assert not proposal.executable
    assert Decimal(proposal.evidence["reserved_spot_cash_usd"]) == Decimal("3000")
    assert "proposal_only_policy" in proposal.reason_codes
    with pytest.raises(RuntimeError, match="intentionally unavailable"):
        treasury.execute(proposal)
    book.close()


def test_treasury_blocks_on_reconciliation_or_incident(tmp_path) -> None:
    book = CapitalReservationBook(str(tmp_path / "capital.db"))
    treasury = ReservationAwareTreasury(book)
    proposal = treasury.propose(
        capital=capital(),
        reservation_policy=reservation_policy(),
        treasury_policy=TreasuryPolicy("500", "1000"),
        reconciliation_matched=False,
        critical_incident_active=True,
        now=NOW,
    )
    assert proposal.direction == "none" and proposal.amount_usd == 0
    assert "account_reconciliation_not_proven" in proposal.reason_codes
    assert "critical_incident_active" in proposal.reason_codes
    book.close()


def test_retired_dust_path_cannot_touch_reserved_inventory_or_external_orders(tmp_path) -> None:
    book = CapitalReservationBook(str(tmp_path / "capital.db"))
    admitted = book.reserve(
        ReservationRequest(
            "repair-1",
            ReservationPurpose.HEDGE_REPAIR,
            "BTCUSDT",
            "cycle-1",
            "3000",
            "250",
            "10",
            "0",
            "cfg",
        ),
        capital=capital(),
        policy=reservation_policy(),
        now=NOW,
    )
    assert admitted.allowed
    snapshot = {
        "futures_account": {"positions": []},
        "position_risk": [],
        "futures_open_orders": [
            {
                "symbol": "BTCUSDT",
                "clientOrderId": "operator-order-1",
                "orderId": 42,
                "status": "NEW",
            }
        ],
        "spot_account": {"balances": []},
        "spot_open_orders": [],
        "margin_account": {"userAssets": []},
        "margin_account_status": "available",
        "margin_open_orders": [],
        "margin_open_orders_status": "available",
        "snapshot_errors": {},
    }
    reconciliation = reconcile_account_snapshot(snapshot, local_positions=[])

    treasury = ReservationAwareTreasury(book)
    proposal = treasury.propose_from_reconciliation(
        capital=capital(),
        reservation_policy=reservation_policy(),
        treasury_policy=TreasuryPolicy("500", "1000", maximum_transfer_usd="500"),
        reconciliation=reconciliation,
        critical_incident_active=False,
        now=NOW,
    )

    assert proposal.direction == "none"
    assert proposal.amount_usd == Decimal("0")
    assert not proposal.executable
    assert "account_reconciliation_not_proven" in proposal.reason_codes
    assert proposal.evidence["reserved_spot_cash_usd"] == "3000"
    assert proposal.evidence["unrelated_open_order_count"] == "1"
    assert "unrelated_open_order" in proposal.evidence["blocking_issue_codes"]
    with pytest.raises(RuntimeError, match="intentionally unavailable"):
        treasury.execute(proposal)
    book.close()


def test_daily_report_uses_economic_ledger_and_blocks_unreconciled_output() -> None:
    conn = sqlite3.connect(":memory:")
    apply_economic_ledger_migration(conn)
    common = dict(
        event_time=NOW.isoformat(), account_id="a", trading_mode="paper",
        venue="binance", strategy_id="s", symbol="BTCUSDT", amount_asset="USDT",
    )
    ingest_economic_events(
        conn,
        [
            EconomicLedgerEvent(
                event_type="FUNDING", exchange_event_id="f1", instrument_type="PERPETUAL",
                amount="10", amount_usd="10", **common,
            ),
            EconomicLedgerEvent(
                event_type="COMMISSION", source_event_id="c1", instrument_type="SPOT",
                amount="-2", amount_usd="-2", **common,
            ),
        ],
    )
    report = build_reconciled_daily_report(
        conn,
        start_time=NOW - timedelta(hours=1),
        end_time=NOW + timedelta(hours=1),
        reconciliation_matched=False,
        reserved_capital_usd="5000",
        account_equity_usd="10000",
        account_id="a",
        trading_mode="paper",
        strategy_id="s",
    )
    assert report.funding_usd == Decimal("10")
    assert report.commissions_usd == Decimal("-2")
    assert report.net_economic_effect_usd == Decimal("8")
    assert report.capital_utilization == 0.5
    assert "economic_ledger_reconciliation_failed" in report.blockers


def test_alert_tracker_emits_only_state_transitions() -> None:
    conn = sqlite3.connect(":memory:")
    tracker = AlertTransitionTracker(conn)
    assert tracker.observe(alert_key="feed:btc", active=False, severity=1, now=NOW) is None
    assert tracker.observe(alert_key="feed:btc", active=True, severity=1, now=NOW) == "OPENED"
    assert tracker.observe(alert_key="feed:btc", active=True, severity=1, now=NOW) is None
    assert tracker.observe(alert_key="feed:btc", active=True, severity=2, now=NOW) == "UPDATED"
    assert tracker.observe(alert_key="feed:btc", active=False, severity=0, now=NOW) == "RESOLVED"
