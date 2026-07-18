from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bongus.market_data.funding_calendar import FundingCalendar
from bongus.market_data.settlement_model import FundingObservation, SettlementFundingModel
from bongus.strategies.plugins import (
    BasisConvergencePlugin,
    FundingCalendarOptimizationPlugin,
    PluginAction,
    StrategyContext,
    StrategyPluginRegistry,
    StrategyRiskBudget,
)


NOW = datetime(2026, 7, 18, 7, tzinfo=timezone.utc)


def funding_forecast():
    calendar = FundingCalendar()
    calendar.update_premium_index(
        {"symbol": "BTCUSDT", "nextFundingTime": int((NOW + timedelta(minutes=30)).timestamp() * 1000)},
        observed_at=NOW,
    )
    model = SettlementFundingModel(uncertainty_floor_rate=1e-8)
    for index in range(10):
        model.observe(FundingObservation("BTCUSDT", NOW - timedelta(minutes=10-index), 0.50))
    return model.forecast(
        symbol="BTCUSDT", decision_time=NOW, horizon_hours=0.51,
        notional_usd=2_500, direction="long_spot_short_perp", calendar=calendar,
    )


def context(**overrides):
    values = dict(
        decision_time=NOW,
        symbol="BTCUSDT",
        venue="binance",
        requested_notional_usd=2_500,
        executable_capacity_usd=100_000,
        entry_exit_cost_usd=1,
        basis_pct=0.01,
        expected_exit_basis_pct=0.002,
        basis_cvar_usd=2,
        seconds_to_settlement=1800,
        settlement_forecast=funding_forecast(),
        config_hash="a" * 64,
        model_hash="b" * 64,
    )
    values.update(overrides)
    return StrategyContext(**values)


def budget(strategy_id, **overrides):
    values = dict(
        strategy_id=strategy_id,
        max_gross_notional_usd=2_500,
        max_position_notional_usd=2_500,
        max_expected_loss_usd=100,
        max_cvar_usd=100,
        max_concurrent_positions=1,
    )
    values.update(overrides)
    return StrategyRiskBudget(**values)


def test_calendar_plugin_values_discrete_payment_and_is_always_shadow() -> None:
    plugin = FundingCalendarOptimizationPlugin(minimum_lcb_usd=-100)
    proposal = plugin.evaluate(context(), budget(plugin.strategy_id))
    assert proposal.action is PluginAction.ENTER
    assert proposal.shadow_only
    assert proposal.strategy_id == "funding-calendar-v1"
    too_late = plugin.evaluate(context(seconds_to_settlement=10), budget(plugin.strategy_id))
    assert too_late.action is PluginAction.REJECT
    assert "insufficient_settlement_eligibility_buffer" in too_late.reason_codes


def test_basis_plugin_direction_cost_and_cvar_budget() -> None:
    plugin = BasisConvergencePlugin(minimum_lcb_usd=1)
    proposal = plugin.evaluate(context(), budget(plugin.strategy_id))
    assert proposal.action is PluginAction.ENTER
    assert proposal.direction == "long_spot_short_perp"
    blocked = plugin.evaluate(
        context(basis_cvar_usd=200), budget(plugin.strategy_id, max_cvar_usd=100)
    )
    assert blocked.action is PluginAction.REJECT
    assert "basis_cvar_budget" in blocked.reason_codes


def test_registry_enforces_separate_strategy_budgets_and_never_merges_risk() -> None:
    registry = StrategyPluginRegistry()
    calendar = FundingCalendarOptimizationPlugin(minimum_lcb_usd=-100)
    basis = BasisConvergencePlugin(minimum_lcb_usd=1)
    registry.register(calendar, budget(calendar.strategy_id))
    registry.register(basis, budget(basis.strategy_id))
    proposals = registry.evaluate([context(), context(symbol="ETHUSDT")])
    grouped = {}
    for proposal in proposals:
        grouped.setdefault(proposal.strategy_id, []).append(proposal)
    assert set(grouped) == {calendar.strategy_id, basis.strategy_id}
    assert sum(item.action is PluginAction.ENTER for item in grouped[calendar.strategy_id]) == 1
    assert sum(item.action is PluginAction.ENTER for item in grouped[basis.strategy_id]) == 1
