import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from bongus.engine.economic_ledger import (
    COMMISSION,
    DEPOSIT,
    FUNDING,
    REALIZED_PNL,
    EconomicLedgerEvent,
    EconomicLedgerProjection,
    apply_economic_ledger_migration,
    ingest_economic_events,
)
from bongus.research.carry_economics import (
    CarryPortfolioWindow,
    CarryWindow,
    FundingSettlement,
    OperatingCost,
    actual_cost_report,
    capital_scenarios,
    compare_carry_to_baseline,
    evaluate_carry_portfolio,
    evaluate_carry_window,
    evidence_digest,
    ledger_cost_report,
    research_evidence_gate,
)
from bongus.supervisor.daily_report import calculate_daily_nav_close
from scripts.report_carry_economics import build_report, main

D = Decimal
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def opex(server="0", *, basis="actual"):
    return tuple(OperatingCost(category, category, D(server if category == "server" else "0"), basis)
                 for category in ("server", "data", "transfer", "other_operations"))


def window(days=7, **overrides):
    defaults = dict(
        label=f"carry_{days}d", start=START, end=START + timedelta(days=days),
        policy_frozen_at=START - timedelta(days=1), data_cutoff=START + timedelta(days=days, minutes=1),
        reserved_capital_usd=D("160"), spot_quantity=D("1"),
        spot_entry_usd=D("100"), spot_exit_usd=D("105"),
        perp_entry_usd=D("101"), perp_exit_usd=D("105"), prices_are_fills=True,
        commissions_usd=D("0.3"), borrow_cost_usd=D("0"), execution_shortfall_usd=D("0"),
        operating_costs=opex("1"), funding_history_complete=True, evidence_kind="historical",
        settlements=(FundingSettlement("funding-1", START + timedelta(hours=4),
                                       START + timedelta(hours=4, seconds=2), D("0.0001"), D("100")),),
    )
    defaults.update(overrides)
    return CarryWindow(**defaults)


def projection():
    return EconomicLedgerProjection(
        event_count=4, fill_count=4, balance_deltas={}, spot_inventory_deltas={},
        perpetual_position_deltas={}, amounts_by_type_and_asset={}, economic_effect_usd_by_type={},
        cashflow_usd_by_type={}, total_economic_effect_usd=D("3"), gross_fill_notional_usd=D("400"),
        unvalued_economic_event_count=0, unvalued_cashflow_event_count=0, incomplete_envelope_event_count=0,
    )


def nav(**overrides):
    values = dict(
        opening_nav_usd="100", closing_nav_usd="155", external_deposits_usd="50",
        external_withdrawals_usd="0", realized_price_pnl_usd="2", actual_funding_usd="3",
        commission_cost_usd="1", borrow_interest_cost_usd="1", unrealized_pnl_change_usd="2",
        stablecoin_fx_movement_usd="0", internal_transfers_usd="0", tolerance_usd="0",
    )
    values.update(overrides)
    return calculate_daily_nav_close(**values)


def actual(**overrides):
    args = dict(nav=nav(), projection=projection(), ledger_reconciled=True, ledger_digest="abc",
                average_reserved_capital_usd=D("50"), operating_costs=opex("1"))
    args.update(overrides)
    return actual_cost_report(**args)


def test_actual_cash_excludes_deposits_and_mtm_and_does_not_charge_markout_twice():
    report = actual(adverse_markout_diagnostic_usd=D("99"))
    assert report["status"] == "MEASURED"
    assert report["exchange_realized_cash_pnl_usd"] == "3"
    assert report["verified_realized_net_profit_usd"] == "2"
    assert report["net_return_on_reserved_capital"] == "0.04"
    assert report["mtm_cost_view_usd"] == "4"
    assert report["external_deposits_usd"] == "50"
    assert report["live_activation_authorized"] is False
    assert report["profitability_established"] is False


def test_assumed_or_missing_opex_never_becomes_verified_actual_profit():
    assumed = actual(operating_costs=opex("1", basis="assumption"))
    assert assumed["status"] == "COST_ASSUMPTION_VIEW"
    assert assumed["verified_realized_net_profit_usd"] is None
    missing = actual(operating_costs=())
    assert missing["net_cost_view_usd"] is None
    assert len(missing["blockers"]) == 4


def test_reconciliation_failure_blocks_actual_profit_even_if_arithmetic_positive():
    report = actual(nav=nav(closing_nav_usd="999"))
    assert report["verified_realized_net_profit_usd"] is None
    assert "nav_not_finalized" in report["blockers"]


def test_booked_cost_identity_cannot_be_deducted_twice():
    costs = tuple(replace(cost, source_id="fee-event", amount_usd=D("1"))
                  if cost.category == "transfer" else cost for cost in opex())
    with pytest.raises(ValueError, match="duplicate deduction"):
        actual(operating_costs=costs, booked_event_keys=frozenset({"fee-event"}))
    costs = tuple(replace(cost, included_in_exchange_pnl=True)
                  if cost.category == "transfer" else cost for cost in costs)
    report = actual(operating_costs=costs, booked_event_keys=frozenset({"fee-event"}))
    assert report["verified_realized_net_profit_usd"] == "3"


def test_no_eight_hour_assumption_and_signed_funding_uses_settlement_mark():
    events = tuple(FundingSettlement(f"f-{hour}", START + timedelta(hours=hour),
                                     START + timedelta(hours=hour, seconds=1), rate, D("120"))
                   for hour, rate in ((1, D("0.0001")), (2, D("-0.0002")), (4, D("0.0003"))))
    report = evaluate_carry_window(window(settlements=events))
    assert D(report["signed_funding_usd"]) == D("0.024")
    assert report["settlement_count"] == 3
    assert D(report["net_cost_view_usd"]) == D("-0.276")
    assert D(report["net_return_on_reserved_capital"]) == D("-0.276") / 160
    inverse = evaluate_carry_window(window(spot_quantity=D("-1"), settlements=events))
    assert D(inverse["signed_funding_usd"]) == D("-0.024")
    assert D(inverse["price_pnl_usd"]) == D("-1")


def test_reference_mid_shortfall_charged_once_and_fill_shortfall_rejected():
    with pytest.raises(ValueError, match="already contains"):
        evaluate_carry_window(window(execution_shortfall_usd=D("1")))
    report = evaluate_carry_window(window(prices_are_fills=False, execution_shortfall_usd=D("1")))
    assert D(report["net_cost_view_usd"]) == D("-1.29")


def test_duplicate_or_unavailable_funding_cannot_inflate_evidence():
    event = window().settlements[0]
    with pytest.raises(ValueError, match="unique"):
        evaluate_carry_window(window(settlements=(event, event)))
    with pytest.raises(ValueError, match="availability"):
        evaluate_carry_window(window(settlements=(replace(event, available_at=START + timedelta(days=10)),)))
    with pytest.raises(ValueError, match="eligibility"):
        evaluate_carry_window(window(settlements=(replace(event, settlement_time=START),)))


def test_incomplete_history_and_future_policy_freeze_produce_no_net_result():
    assert evaluate_carry_window(window(funding_history_complete=False))["net_cost_view_usd"] is None
    assert evaluate_carry_window(window(policy_frozen_at=START + timedelta(seconds=1)))["net_cost_view_usd"] is None


def test_paired_windows_require_matching_capital_and_digest_never_activates_live():
    with pytest.raises(ValueError, match="identical"):
        compare_carry_to_baseline(window(), window(reserved_capital_usd=D("1000")))
    comparisons = tuple(compare_carry_to_baseline(window(days), window(days, label="baseline")) for days in (7, 30))
    result = research_evidence_gate(comparisons, expected_digest=evidence_digest(comparisons))
    assert result["status"] == "READY_FOR_RESEARCH_REVIEW"
    assert result["capital_increase_authorized"] is False
    assert result["live_activation_authorized"] is False
    assert research_evidence_gate(comparisons, expected_digest="modified")["status"] == "INSUFFICIENT_EVIDENCE"
    assert research_evidence_gate(comparisons[:1], expected_digest=evidence_digest(comparisons[:1]))[
        "status"] == "INSUFFICIENT_EVIDENCE"


def test_ledger_adapter_replaces_total_price_pnl_and_excludes_deposit(tmp_path):
    database = tmp_path / "audit.db"
    with sqlite3.connect(database) as conn:
        apply_economic_ledger_migration(conn)
        rows = [EconomicLedgerEvent(
            event_type=kind, source_event_id=kind, event_time=START.isoformat(),
            availability_time=START.isoformat(), account_id="acct", trading_mode="paper",
            venue="BINANCE", strategy_id="carry", amount=amount, amount_usd=amount,
            symbol="ETHUSDT", instrument_type="PERPETUAL",
            amount_asset="USDT", code_hash="code", config_hash="cfg", schema_hash="schema",
        ) for kind, amount in ((FUNDING, "3"), (COMMISSION, "-1"), (REALIZED_PNL, "999"), (DEPOSIT, "50"))]
        ingest_economic_events(conn, rows)
        report = ledger_cost_report(
            conn, account_id="acct", trading_mode="paper", start_time=START.isoformat(),
            end_time=(START + timedelta(days=1)).isoformat(), ledger_reconciled=True,
            average_reserved_capital_usd=D("50"), operating_costs=opex("1"),
            nav_inputs=dict(opening_nav_usd="100", closing_nav_usd="155", external_withdrawals_usd="0",
                            realized_price_pnl_usd="2", borrow_interest_cost_usd="1",
                            unrealized_pnl_change_usd="2", stablecoin_fx_movement_usd="0",
                            internal_transfers_usd="0", tolerance_usd="0"),
        )
        assert report["verified_realized_net_profit_usd"] == "2"
        # A futures REALIZED_PNL subtotal is not consolidated spot/perp PnL.
        missing = ledger_cost_report(
            conn, account_id="acct", trading_mode="paper", start_time=START.isoformat(),
            end_time=(START + timedelta(days=1)).isoformat(), ledger_reconciled=True,
            average_reserved_capital_usd=D("50"), operating_costs=opex(), nav_inputs={},
        )
        assert missing["exchange_realized_cash_pnl_usd"] is None


def test_cli_rejects_modified_input_and_never_overwrites_artifact(tmp_path):
    source = tmp_path / "inputs.json"
    output = tmp_path / "result.json"
    source.write_text(json.dumps({"mode": "comparison", "comparisons": []}), encoding="utf-8")
    with pytest.raises(SystemExit):
        main(["--input", str(source), "--input-sha256", "wrong"])
    assert main(["--input", str(source), "--output", str(output)]) == 0
    before = output.read_bytes()
    assert json.loads(before)["live_activation_authorized"] is False
    with pytest.raises(SystemExit):
        main(["--input", str(source), "--output", str(output)])
    assert output.read_bytes() == before


def test_capital_cost_drag_break_even_and_compounding_are_explicit_assumptions():
    report = capital_scenarios(
        capitals_usd=(D("100"), D("500"), D("1000"), D("5000"), D("10000")),
        reserved_fraction=D("0.8"), annual_net_edge_on_reserved_before_opex=D("0.03"),
        monthly_opex_usd=D("5"), assumption_label="arithmetic sensitivity, not a forecast",
    )
    assert D(report["break_even_capital_usd"]) == D("2500")
    first = report["rows"][0]
    assert D(first["month_net_profit_usd"]) == D("-4.8")
    assert D(first["year_net_profit_no_reinvestment_usd"]) == D("-57.6")
    assert D(first["break_even_annual_net_edge_on_reserved_before_opex"]) == D("0.75")
    assert report["status"] == "ASSUMPTION_ONLY"
    assert report["loss_month_probability"] is None
    assert report["ruin_probability"] is None
    zero_cost = capital_scenarios(
        capitals_usd=(D("1000"),), reserved_fraction=D("1"),
        annual_net_edge_on_reserved_before_opex=D("0.12"), monthly_opex_usd=D("0"),
        assumption_label="synthetic",
    )
    assert D(zero_cost["rows"][0]["year_profit_with_monthly_reinvestment_usd"]) == D("1000") * D("1.01")**12 - 1000
    exhausted = build_report({
        "mode": "unit_economics", "capitals_usd": ["100"], "reserved_fraction": "1",
        "annual_net_edge_on_reserved_before_opex": "0", "monthly_opex_usd": "20", "assumption_label": "synthetic",
    })
    assert exhausted["break_even_capital_usd"] is None
    assert exhausted["rows"][0]["cash_budget_exhausted_month_in_reinvestment_scenario"] == 5
    assert exhausted["rows"][0]["year_profit_with_monthly_reinvestment_usd"] is None


def test_rotation_baseline_includes_every_cycle_fee_once_and_rejects_capital_overbooking():
    cycles = (window(end=START + timedelta(days=3), operating_costs=opex()),
              window(label="second", start=START + timedelta(days=3), operating_costs=opex(),
                     settlements=(FundingSettlement("funding-2", START + timedelta(days=3, hours=4),
                                                    START + timedelta(days=3, hours=4, seconds=2),
                                                    D("0.0001"), D("100")),)))
    portfolio = CarryPortfolioWindow(
        label="rotation_baseline", start=START, end=START + timedelta(days=7),
        policy_frozen_at=START - timedelta(days=1), data_cutoff=START + timedelta(days=8),
        reserved_capital_usd=D("160"), cycles=cycles, operating_costs=opex("1"),
        cycle_history_complete=True, evidence_kind="historical",
    )
    report = evaluate_carry_portfolio(portfolio)
    assert D(report["net_cost_view_usd"]) == D("0.42")  # 2 price + .02 funding - .6 fees - 1 OPEX
    paired = compare_carry_to_baseline(window(), portfolio)
    assert D(paired["incremental_net_usd"]) == D("-0.71")
    with pytest.raises(ValueError, match="exceed"):
        evaluate_carry_portfolio(replace(portfolio, cycles=(cycles[0], replace(cycles[1], start=START))))
    with pytest.raises(ValueError, match="OPEX"):
        evaluate_carry_portfolio(replace(portfolio, cycles=(replace(cycles[0], operating_costs=opex("1")),)))
    assert evaluate_carry_portfolio(replace(portfolio, cycle_history_complete=False))["net_cost_view_usd"] is None


def test_ledger_periods_do_not_overlap_and_supplied_cashflow_cannot_mask_actual_ledger(tmp_path):
    with sqlite3.connect(tmp_path / "boundaries.db") as conn:
        apply_economic_ledger_migration(conn)
        middle = START + timedelta(days=1)
        events = [EconomicLedgerEvent(
            event_type=FUNDING, source_event_id=str(index), event_time=when.isoformat(),
            availability_time=when.isoformat(), account_id="acct", trading_mode="paper", venue="BINANCE",
            strategy_id="carry", amount="1", amount_usd="1", symbol="ETHUSDT", instrument_type="PERPETUAL",
            amount_asset="USDT", code_hash="code", config_hash="cfg", schema_hash="schema",
        ) for index, when in enumerate((START, middle))]
        ingest_economic_events(conn, events)
        args = dict(conn=conn, account_id="acct", trading_mode="paper", ledger_reconciled=False,
                    average_reserved_capital_usd=D("100"), operating_costs=opex(),
                    nav_inputs=dict(realized_price_pnl_usd="0", commission_cost_usd="0", borrow_interest_cost_usd="0"))
        first = ledger_cost_report(**args, start_time=START.isoformat(), end_time=middle.isoformat())
        second = ledger_cost_report(
            **args, start_time=middle.isoformat(), end_time=(middle + timedelta(days=1)).isoformat())
        assert first["exchange_realized_cash_pnl_usd"] == second["exchange_realized_cash_pnl_usd"] == "1"
        assert first["scope"]["trading_mode"] == "paper"
        args["nav_inputs"]["actual_funding_usd"] = "999"
        with pytest.raises(ValueError, match="conflicts"):
            ledger_cost_report(**args, start_time=START.isoformat(), end_time=middle.isoformat())
