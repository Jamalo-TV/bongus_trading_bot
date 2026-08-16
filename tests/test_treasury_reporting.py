from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from bongus.engine.account_reconciliation import reconcile_account_snapshot
from bongus.engine.economic_ledger import (
    BORROW_INTEREST,
    DEPOSIT,
    FUNDING,
    INTERNAL_TRANSFER,
    REALIZED_PNL,
    WITHDRAWAL,
    EconomicLedgerEvent,
    apply_economic_ledger_migration,
    build_cashflow_event,
    build_commission_event,
    ingest_economic_events,
)
from bongus.engine.state_store import (
    ExecutionTcaIntent,
    ExecutionTcaLeg,
    OpportunityFunnelEvent,
    StateWriter,
)
from bongus.portfolio.capital_reservations import (
    CapitalReservationBook,
    CapitalState,
    ReservationPolicy,
    ReservationPurpose,
    ReservationRequest,
)
from bongus.portfolio.treasury import ReservationAwareTreasury, TreasuryPolicy
from bongus.supervisor.daily_report import (
    FINALIZED,
    PNL_INCOMPLETE,
    PROJECTED,
    AlertTransitionTracker,
    build_reconciled_daily_report,
    calculate_daily_nav_close,
)

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
            "entry-1",
            ReservationPurpose.ENTRY,
            "BTCUSDT",
            "c1",
            "3000",
            "250",
            "10",
            "5000",
            "cfg",
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
    common: dict[str, Any] = dict(
        event_time=NOW.isoformat(),
        account_id="a",
        trading_mode="paper",
        venue="binance",
        strategy_id="s",
        symbol="BTCUSDT",
        amount_asset="USDT",
    )
    ingest_economic_events(
        conn,
        [
            EconomicLedgerEvent(
                event_type="FUNDING",
                exchange_event_id="f1",
                instrument_type="PERPETUAL",
                amount="10",
                amount_usd="10",
                **common,
            ),
            EconomicLedgerEvent(
                event_type="COMMISSION",
                source_event_id="c1",
                instrument_type="SPOT",
                amount="-2",
                amount_usd="-2",
                **common,
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
    assert report.net_economic_effect_usd is None
    assert report.capital_utilization == 0.5
    assert "economic_ledger_reconciliation_failed" in report.blockers


def test_daily_nav_close_uses_full_decimal_equation_and_zero_internal_transfers() -> None:
    close = calculate_daily_nav_close(
        opening_nav_usd="1000",
        closing_nav_usd="1082.5",
        external_deposits_usd="100",
        external_withdrawals_usd="30",
        realized_price_pnl_usd="20",
        actual_funding_usd="5",
        commission_cost_usd="2",
        borrow_interest_cost_usd="1",
        unrealized_pnl_change_usd="-10",
        stablecoin_fx_movement_usd="0.5",
        internal_transfers_usd="0",
        tolerance_usd="0.00000001",
    )

    assert close.status == FINALIZED
    assert close.projected_closing_nav_usd == Decimal("1082.5")
    assert close.equation_difference_usd == Decimal("0")
    assert close.missing_components == ()


def test_daily_nav_close_distinguishes_projected_and_unknown_delayed_inputs() -> None:
    common = dict(
        opening_nav_usd="100",
        external_deposits_usd="0",
        external_withdrawals_usd="0",
        realized_price_pnl_usd="1",
        actual_funding_usd="0.2",
        commission_cost_usd="0.1",
        borrow_interest_cost_usd="0.1",
        unrealized_pnl_change_usd="0",
        stablecoin_fx_movement_usd="0",
        internal_transfers_usd="0",
    )
    projected = calculate_daily_nav_close(
        **common,
        closing_nav_usd=None,
    )
    incomplete = calculate_daily_nav_close(
        **{**common, "actual_funding_usd": None},
        closing_nav_usd="101",
        tolerance_usd="0.01",
    )

    assert projected.status == PROJECTED
    assert projected.projected_closing_nav_usd == Decimal("101.0")
    assert projected.closing_nav_usd is None
    assert incomplete.status == PNL_INCOMPLETE
    assert incomplete.actual_funding_usd is None
    assert incomplete.projected_closing_nav_usd is None
    assert "actual_funding_usd" in incomplete.missing_components


def test_nonzero_consolidated_internal_transfer_cannot_finalize_nav() -> None:
    close = calculate_daily_nav_close(
        opening_nav_usd="100",
        closing_nav_usd="100",
        external_deposits_usd="0",
        external_withdrawals_usd="0",
        realized_price_pnl_usd="0",
        actual_funding_usd="0",
        commission_cost_usd="0",
        borrow_interest_cost_usd="0",
        unrealized_pnl_change_usd="0",
        stablecoin_fx_movement_usd="0",
        internal_transfers_usd="1",
        tolerance_usd="0.01",
    )

    assert close.status == PNL_INCOMPLETE
    assert "internal_transfers_not_net_zero" in close.blockers


def test_reconciled_daily_report_finalizes_from_explicit_ledger_cashflows() -> None:
    conn = sqlite3.connect(":memory:")
    apply_economic_ledger_migration(conn)
    provenance: dict[str, Any] = dict(
        availability_time=(NOW + timedelta(seconds=1)).isoformat(),
        code_hash="code-hash",
        config_hash="config-hash",
        schema_hash="schema-hash",
    )
    common: dict[str, Any] = dict(
        trading_mode="paper",
        venue="BINANCE",
        strategy_id="s",
        event_time=NOW.isoformat(),
        **provenance,
    )
    events = [
        build_cashflow_event(
            event_type=DEPOSIT,
            account_id="spot",
            asset="USDT",
            amount="100",
            exchange_event_id="deposit",
            **common,
        ),
        build_cashflow_event(
            event_type=WITHDRAWAL,
            account_id="spot",
            asset="USDT",
            amount="-20",
            exchange_event_id="withdrawal",
            **common,
        ),
        build_cashflow_event(
            event_type=REALIZED_PNL,
            account_id="futures",
            asset="USDT",
            amount="10",
            exchange_event_id="realized",
            symbol="BTCUSDT",
            instrument_type="PERPETUAL",
            **common,
        ),
        build_cashflow_event(
            event_type=FUNDING,
            account_id="futures",
            asset="USDT",
            amount="5",
            exchange_event_id="funding",
            symbol="BTCUSDT",
            instrument_type="PERPETUAL",
            **common,
        ),
        build_commission_event(
            account_id="spot",
            commission_amount="2",
            commission_asset="USDT",
            source_event_id="commission",
            symbol="BTCUSDT",
            instrument_type="SPOT",
            **common,
        ),
        build_cashflow_event(
            event_type=BORROW_INTEREST,
            account_id="spot",
            asset="USDT",
            amount="1",
            exchange_event_id="interest",
            **common,
        ),
        build_cashflow_event(
            event_type=INTERNAL_TRANSFER,
            account_id="spot",
            asset="USDT",
            amount="-50",
            exchange_event_id="transfer",
            **common,
        ),
        build_cashflow_event(
            event_type=INTERNAL_TRANSFER,
            account_id="futures",
            asset="USDT",
            amount="50",
            exchange_event_id="transfer",
            **common,
        ),
    ]
    ingest_economic_events(conn, events)

    report = build_reconciled_daily_report(
        conn,
        start_time=NOW - timedelta(hours=1),
        end_time=NOW + timedelta(hours=1),
        reconciliation_matched=True,
        reserved_capital_usd="500",
        account_equity_usd="1095.5",
        trading_mode="paper",
        strategy_id="s",
        opening_nav_usd="1000",
        closing_nav_usd="1095.5",
        unrealized_pnl_change_usd="3",
        stablecoin_fx_movement_usd="0.5",
        nav_tolerance_usd="0.01",
    )

    assert report.reconciled is True
    assert report.nav_close.status == FINALIZED
    assert report.nav_close.projected_closing_nav_usd == Decimal("1095.5")
    assert report.nav_close.internal_transfers_usd == Decimal("0")
    assert report.blockers == ()
    assert len(report.funding_availability_gaps) == 1
    assert report.funding_availability_gaps[0].gap_seconds == Decimal("1")
    assert report.statement_availability_gaps == ()
    assert report.opportunity_funnel["observed"].conversion_rate is None
    assert report.tca.intent_count == 0


def test_daily_report_includes_normalized_tca_and_funnel_denominators(tmp_path) -> None:
    writer = StateWriter(db_path=str(tmp_path / "daily-tca.db"))
    try:
        event_time = NOW.isoformat()
        writer.record_execution_tca(
            ExecutionTcaIntent(
                intent_id="daily-entry",
                cycle_id=event_time,
                decision_id="daily-decision",
                symbol="BTCUSDT",
                operation="ENTRY",
                decision_time=event_time,
            ),
            (
                ExecutionTcaLeg("daily-entry", "spot", "spot", "BUY"),
                ExecutionTcaLeg("daily-entry", "perp", "perp", "SELL"),
            ),
        )
        writer.record_opportunity_funnel_event(
            OpportunityFunnelEvent(
                cycle_id=event_time,
                stage="observed",
                numerator_count=8,
                denominator_count=8,
                event_time=event_time,
            )
        )
        writer.record_opportunity_funnel_event(
            OpportunityFunnelEvent(
                cycle_id=event_time,
                stage="data_complete",
                numerator_count=6,
                denominator_count=8,
                event_time=event_time,
            )
        )

        report = build_reconciled_daily_report(
            writer.conn,
            start_time=NOW - timedelta(hours=1),
            end_time=NOW + timedelta(hours=1),
            reconciliation_matched=True,
            opening_nav_usd="100",
            closing_nav_usd="100",
            external_deposits_usd="0",
            external_withdrawals_usd="0",
            realized_price_pnl_usd="0",
            actual_funding_usd="0",
            commission_cost_usd="0",
            borrow_interest_cost_usd="0",
            unrealized_pnl_change_usd="0",
            stablecoin_fx_movement_usd="0",
            internal_transfers_usd="0",
            nav_tolerance_usd="0.01",
        )
        assert report.opportunity_funnel["observed"].numerator == 8
        assert report.opportunity_funnel["data_complete"].conversion_rate == Decimal(
            "0.75"
        )
        assert report.tca.intent_count == 1
        assert report.tca.leg_count == 2
        assert report.tca.unknown_unhedged_intent_count == 1
        assert report.tca.complete_intent_count == 0
    finally:
        writer.close()


def test_alert_tracker_emits_only_state_transitions() -> None:
    conn = sqlite3.connect(":memory:")
    tracker = AlertTransitionTracker(conn)
    assert tracker.observe(alert_key="feed:btc", active=False, severity=1, now=NOW) is None
    assert tracker.observe(alert_key="feed:btc", active=True, severity=1, now=NOW) == "OPENED"
    assert tracker.observe(alert_key="feed:btc", active=True, severity=1, now=NOW) is None
    assert tracker.observe(alert_key="feed:btc", active=True, severity=2, now=NOW) == "UPDATED"
    assert tracker.observe(alert_key="feed:btc", active=False, severity=0, now=NOW) == "RESOLVED"
