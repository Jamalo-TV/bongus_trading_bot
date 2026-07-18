from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

from bongus.engine.leg_state_machine import (
    HedgeCycleState,
    Leg,
    LegStatus,
    LegUpdate,
)
from bongus.engine.risk_engine import RiskEngine, RiskState
from bongus.engine.route_optimizer import RouteInputs, RouteOptimizer, RoutePolicy
from scripts.live_trader_v2 import LiveTraderV2


def _build_trader(db_path: str) -> LiveTraderV2:
    with patch("scripts.live_trader_v2.ConfigManager.start_watching", autospec=True):
        return LiveTraderV2(
            db_path=db_path,
            config_path=f"{db_path}.config.json",
        )


def _close_trader(trader: LiveTraderV2) -> None:
    trader.execution.close()
    trader.capital_reservations.close()
    trader.feed_cursors.close()
    trader.cooldowns.close()
    trader.state_reader.close()
    trader.state_writer.close()


def test_combined_flash_book_withdrawal_partial_margin_and_failed_emergency_exit(
    tmp_path,
    monkeypatch,
) -> None:
    # Flash loss plus compressed available margin is an explicit kill switch,
    # not merely an entry-sizing rejection.
    risk = RiskEngine().evaluate(
        RiskState(
            gross_exposure_usd=2_500.0,
            symbol_concentration=0.25,
            drawdown_pct=0.11,
            data_staleness_minutes=0,
            venue_latency_ms=25,
            liquidation_buffer_usd=250.0,
            minimum_liquidation_buffer_usd=1_000.0,
        )
    )
    assert risk.kill_switch
    assert risk.derisk_required
    assert not risk.allow_new_risk
    assert "max drawdown breached" in risk.reasons
    assert any("liquidation margin buffer compressed" in item for item in risk.reasons)

    # A vanished spot book makes every pair route infeasible but must not hide
    # the perp-only liquidation-risk reduction route when perp depth is live.
    route = RouteOptimizer().recommend(
        RouteInputs(
            symbol="BTCUSDT",
            notional_usd=2_500.0,
            spot_spread_bps=50.0,
            perp_spread_bps=20.0,
            spot_depth_usd=0.0,
            perp_depth_usd=100_000.0,
            book_age_ms=25,
            filters_ready=True,
            is_exit=True,
            emergency=True,
            urgency=1.0,
            volatility_bps_per_second=120.0,
            adverse_markout_bps=80.0,
            impact_bps=20.0,
        )
    )
    assert route.selected is RoutePolicy.EMERGENCY_REDUCE_ONLY
    emergency = route.selected_estimate
    assert emergency is not None and emergency.feasible
    pair_routes = [
        item
        for item in route.estimates
        if item.policy is not RoutePolicy.EMERGENCY_REDUCE_ONLY
    ]
    assert pair_routes and all(not item.feasible for item in pair_routes)
    assert all("zero_depth" in item.reasons for item in pair_routes)

    # The adverse spot partial creates short delta.  The only quantified repair
    # is a perp BUY for exactly the residual, marked reduce-only.
    cycle = HedgeCycleState.exit(
        "emergency-exit-1",
        spot_quantity="1",
        perp_quantity="-1",
    )
    cycle.apply(
        LegUpdate(
            event_id="spot-partial-1",
            leg=Leg.SPOT,
            status=LegStatus.PARTIAL,
            cumulative_quantity="0.4",
            event_time_ms=1_000,
            exchange_verified=True,
        )
    )
    assert cycle.mismatch_quantity == Decimal("-0.4")
    repair = cycle.residual_repair(
        prefer_leg=Leg.PERP,
        emergency_reduce=True,
    )
    assert repair is not None
    assert repair.side == "BUY"
    assert repair.quantity == Decimal("0.4")
    assert repair.reduce_only

    cycle.apply(
        LegUpdate(
            event_id="perp-reduce-rejected-1",
            leg=Leg.PERP,
            status=LegStatus.REJECTED,
            cumulative_quantity="0",
            event_time_ms=1_100,
            exchange_verified=True,
        )
    )
    assert not cycle.hedged
    assert not cycle.safe_to_project_complete
    assert cycle.residual_repair(
        prefer_leg=Leg.PERP,
        emergency_reduce=True,
    ) == repair

    # The runtime likewise preserves the durable position after a hard
    # reduce-only rejection.  It records manual review, a symbol cooldown and a
    # safe-mode flag; exchange rejection can never be interpreted as flatness.
    monkeypatch.setenv("TRADING_MODE", "paper")
    trader = _build_trader(str(tmp_path / "state.db"))
    try:
        trader.state_writer.upsert_position(
            symbol="BTCUSDT",
            side="LONG_SPOT_SHORT_PERP",
            direction="long",
            spot_entry=100.0,
            perp_entry=100.0,
            spot_live=75.0,
            perp_live=125.0,
            qty=1.0,
            hedge_ratio=1.0,
            ann_funding=0.1,
        )
        send = MagicMock(return_value=True)
        trader.execution.send_order_intent = send
        trader._dispatch_exit("BTCUSDT", urgency=1.0, direction="long")
        payload = send.call_args.args[0]
        intent_id = str(payload["intent_id"])
        assert payload["intent"] == "EXIT_LONG"
        assert payload["urgency"] == 1.0
        assert payload["max_slippage_bps"] == 20.0

        trader._on_order_rejected(
            "BTCUSDT",
            "EXIT_LONG",
            intent_id,
            "reduce_only_failed: liquidation margin compressed",
        )

        positions = trader.state_reader.get_positions()
        assert len(positions) == 1
        assert positions[0]["symbol"] == "BTCUSDT"
        assert positions[0]["recovery_state"] == "manual_review"
        assert "exit_failure" in trader._safe_mode_flags
        assert trader.cooldowns.is_symbol_active("BTCUSDT")
        assert "BTCUSDT" not in trader._pending_exit_intents
    finally:
        _close_trader(trader)
