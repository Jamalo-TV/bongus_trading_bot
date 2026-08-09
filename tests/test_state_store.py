import os
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from bongus.engine.state_store import (
    ArchiveVerificationError,
    ExecutionQualitySample,
    StateReader,
    StateWriter,
    Trade,
)


def test_readonly_reader_never_creates_a_missing_database(tmp_path):
    missing = tmp_path / "missing-state.db"

    with pytest.raises(FileNotFoundError):
        StateReader(db_path=str(missing))

    assert not missing.exists()


def test_online_maintenance_refuses_full_vacuum(tmp_path):
    writer = StateWriter(db_path=str(tmp_path / "state.db"))
    try:
        with pytest.raises(ValueError, match="full VACUUM is disabled"):
            writer.maintenance(run_vacuum=True)
    finally:
        writer.close()


def test_operator_metric_reset_preserves_tier_a_audit_evidence(tmp_path):
    writer = StateWriter(db_path=str(tmp_path / "state.db"))
    try:
        event_time = "2026-01-01T00:00:00+00:00"
        writer.record_trade(
            Trade(
                symbol="BTCUSDT",
                side="LONG",
                entry_time=event_time,
                exit_time=event_time,
                entry_price=100.0,
                exit_price=101.0,
                qty=1.0,
                net_pnl_usd=1.0,
            )
        )
        writer.record_execution_event(
            {
                "symbol": "BTCUSDT",
                "client_order_id": "immutable-audit-event",
                "status": "FILLED",
                "event_time": event_time,
            }
        )

        writer.clear_trade_history()
        writer.clear_execution_events()

        assert writer.conn.execute(
            "SELECT COUNT(*) FROM trade_history"
        ).fetchone()[0] == 1
        assert writer.conn.execute(
            "SELECT COUNT(*) FROM execution_events"
        ).fetchone()[0] == 1
    finally:
        writer.close()


def _quality_sample(sample_id: str, *, slippage: float = 1.5) -> ExecutionQualitySample:
    return ExecutionQualitySample(
        sample_id=sample_id,
        symbol="BTCUSDT",
        client_order_id="bngs_s_quality",
        side="BUY",
        order_type="legacy_dual_maker:spot",
        urgency=0.5,
        expected_cost_bps=2.0,
        realized_slippage_bps=slippage,
        spread_bps=1.0,
        depth_usd=100_000.0,
        maker=True,
        quality_score=0.8,
        metadata={"route": "legacy_dual_maker"},
    )


def test_execution_quality_sample_ids_are_idempotent_and_collision_safe(
    state_writer,
    state_reader,
):
    sample = _quality_sample("fill:spot:1:60s")
    assert state_writer.record_execution_quality(sample)
    assert not state_writer.record_execution_quality(sample)
    with pytest.raises(ValueError, match="sample_id collision"):
        state_writer.record_execution_quality(
            _quality_sample("fill:spot:1:60s", slippage=9.0)
        )
    state_writer.flush()
    rows = state_reader.get_execution_quality(limit=10)
    assert len(rows) == 1
    assert rows[0]["sample_id"] == "fill:spot:1:60s"


def test_lifecycle_entry_and_exit_projections_are_atomic_and_idempotent(
    state_writer,
    state_reader,
):
    state_writer.upsert_pending_intent(
        intent_id="intent-entry",
        symbol="BTCUSDT",
        intent_type="ENTER",
        direction="long",
        status="SUBMITTED",
        quantity=1.0,
    )
    position = {
        "symbol": "BTCUSDT",
        "side": "LONG_SPOT_SHORT_PERP",
        "spot_entry": 100.0,
        "perp_entry": 101.0,
        "qty": 1.0,
        "direction": "long",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    assert state_writer.project_entry_lifecycle(
        event_key="entry:intent-entry",
        intent_id="intent-entry",
        event_time="2026-01-01T00:00:00+00:00",
        position_fields=position,
        evidence={"cycle_id": "cycle-entry"},
    )
    assert not state_writer.project_entry_lifecycle(
        event_key="entry:intent-entry",
        intent_id="intent-entry",
        event_time="2026-01-01T00:00:00+00:00",
        position_fields=position,
        evidence={"cycle_id": "cycle-entry"},
    )
    assert len(state_reader.get_positions()) == 1
    assert state_reader.get_pending_intents() == []

    state_writer.upsert_pending_intent(
        intent_id="intent-exit",
        symbol="BTCUSDT",
        intent_type="EXIT",
        direction="long",
        status="SUBMITTED",
        quantity=1.0,
    )
    trade = Trade(
        symbol="BTCUSDT",
        side="LONG_SPOT_SHORT_PERP",
        entry_time="2026-01-01T00:00:00+00:00",
        exit_time="2026-01-01T08:00:00+00:00",
        entry_price=100.5,
        exit_price=102.0,
        qty=1.0,
        net_pnl_usd=1.0,
    )
    assert state_writer.project_exit_lifecycle(
        event_key="exit:intent-exit",
        intent_id="intent-exit",
        event_time=trade.exit_time,
        trade=trade,
        evidence={"cycle_id": "cycle-exit"},
    )
    assert not state_writer.project_exit_lifecycle(
        event_key="exit:intent-exit",
        intent_id="intent-exit",
        event_time=trade.exit_time,
        trade=trade,
        evidence={"cycle_id": "cycle-exit"},
    )
    assert state_reader.get_positions() == []
    assert len(state_reader.get_trades(limit=10, session_scoped=False)) == 1


def test_partial_exit_lifecycle_preserves_residual_and_rebuilds_exactly_once(
    state_writer,
    state_reader,
):
    entry_time = "2026-01-01T00:00:00+00:00"
    partial_time = "2026-01-01T04:00:00+00:00"
    final_time = "2026-01-01T08:00:00+00:00"
    original = {
        "symbol": "BTCUSDT",
        "side": "LONG_SPOT_SHORT_PERP",
        "spot_entry": 100.0,
        "perp_entry": 101.0,
        "qty": 1.0,
        "direction": "long",
        "updated_at": entry_time,
    }
    state_writer.project_entry_lifecycle(
        event_key="entry:partial-position",
        intent_id="entry-partial-position",
        event_time=entry_time,
        position_fields=original,
        evidence={},
    )
    state_writer.upsert_pending_intent(
        intent_id="partial-exit",
        symbol="BTCUSDT",
        intent_type="EXIT_LONG",
        direction="long",
        status="SUBMITTED",
        quantity=0.4,
    )
    residual = {**original, "qty": 0.6}
    assert state_writer.project_partial_exit_lifecycle(
        event_key="partial_exit:partial-exit",
        intent_id="partial-exit",
        event_time=partial_time,
        remaining_position_fields=residual,
        evidence={
            "exit_quantity": 0.4,
            "spot_fill_price": 102.0,
            "perp_fill_price": 103.0,
        },
    )
    assert state_reader.get_positions()[0]["qty"] == pytest.approx(0.6)
    assert state_reader.get_trades(limit=10, session_scoped=False) == []
    partial_events = state_reader.get_partial_exit_lifecycle_events("BTCUSDT")
    assert [event["event_key"] for event in partial_events] == [
        "partial_exit:partial-exit"
    ]
    assert partial_events[0]["remaining_position"]["qty"] == pytest.approx(0.6)

    state_writer.upsert_pending_intent(
        intent_id="final-exit",
        symbol="BTCUSDT",
        intent_type="EXIT_LONG",
        direction="long",
        status="SUBMITTED",
        quantity=0.6,
    )
    trade = Trade(
        symbol="BTCUSDT",
        side="LONG_SPOT_SHORT_PERP",
        entry_time=entry_time,
        exit_time=final_time,
        entry_price=100.5,
        exit_price=103.5,
        qty=1.0,
        net_pnl_usd=3.0,
    )
    assert state_writer.project_exit_lifecycle(
        event_key="exit:final-exit",
        intent_id="final-exit",
        event_time=final_time,
        trade=trade,
        evidence={"partial_exit_event_keys": ["partial_exit:partial-exit"]},
    )
    assert state_reader.get_positions() == []
    assert len(state_reader.get_trades(limit=10, session_scoped=False)) == 1

    proof = state_writer.rebuild_lifecycle_projections(authoritative_positions=[])
    assert proof["event_count"] == 3
    assert proof["trade_count"] == 1
    rebuilt_trades = state_reader.get_trades(limit=10, session_scoped=False)
    assert len(rebuilt_trades) == 1
    assert rebuilt_trades[0]["qty"] == pytest.approx(1.0)


def test_lifecycle_identity_collision_rolls_back_projection(state_writer, state_reader):
    position = {
        "symbol": "ETHUSDT",
        "side": "LONG_SPOT_SHORT_PERP",
        "spot_entry": 2000.0,
        "perp_entry": 2001.0,
        "qty": 1.0,
    }
    state_writer.project_entry_lifecycle(
        event_key="entry:collision",
        intent_id="collision",
        event_time="2026-01-01T00:00:00Z",
        position_fields=position,
        evidence={},
    )
    with pytest.raises(ValueError, match="identity collision"):
        state_writer.project_entry_lifecycle(
            event_key="entry:collision",
            intent_id="collision",
            event_time="2026-01-01T00:00:00Z",
            position_fields={**position, "qty": 2.0},
            evidence={},
        )
    assert state_reader.get_positions()[0]["qty"] == pytest.approx(1.0)


@pytest.fixture
def temp_db_path(tmp_path):
    # tmp_path is a built-in pytest fixture that automatically cleans up
    return str(tmp_path / "test_state.db")


@pytest.fixture
def state_writer(temp_db_path):
    writer = StateWriter(db_path=temp_db_path)
    yield writer
    writer.close()


@pytest.fixture
def state_reader(temp_db_path):
    reader = StateReader(db_path=temp_db_path)
    yield reader
    reader.close()


# --- Core State Store CRUD Tests ---

def test_upsert_position(state_writer, state_reader):
    state_writer.upsert_position(
        symbol="BTCUSDT",
        side="LONG",
        spot_entry=100.0,
        perp_entry=101.0,
        qty=1.5,
        ann_funding=0.1,
        basis_pct=0.01,
        net_pnl_usd=50.0,
        status="OPEN",
        spot_live=102.0,
        perp_live=103.0
    )

    positions = state_reader.get_positions()
    assert len(positions) == 1
    pos = positions[0]
    assert pos["symbol"] == "BTCUSDT"
    assert pos["side"] == "LONG"
    assert pos["spot_entry"] == 100.0
    assert pos["perp_entry"] == 101.0
    assert pos["qty"] == 1.5
    assert pos["status"] == "OPEN"

    # Update position (change qty and status)
    state_writer.upsert_position(
        symbol="BTCUSDT",
        side="LONG",
        spot_entry=100.0,
        perp_entry=101.0,
        qty=2.5,
        status="CLOSED"
    )

    # get_positions filters out CLOSED status
    positions = state_reader.get_positions()
    assert len(positions) == 0


def test_remove_position(state_writer, state_reader):
    state_writer.upsert_position(
        symbol="ETHUSDT",
        side="SHORT",
        spot_entry=2000.0,
        perp_entry=1990.0,
        qty=10.0
    )
    assert len(state_reader.get_positions()) == 1

    state_writer.remove_position("ETHUSDT")
    assert len(state_reader.get_positions()) == 0


def test_record_trade(state_writer, state_reader):
    trade = Trade(
        symbol="BTCUSDT",
        side="LONG",
        entry_time="2023-01-01T00:00:00Z",
        exit_time="2023-01-02T00:00:00Z",
        entry_price=100.0,
        exit_price=110.0,
        qty=1.0,
        net_pnl_usd=10.0,
        funding_collected=0.5,
    )
    state_writer.record_trade(trade)

    trades = state_reader.get_trades(limit=10)
    assert len(trades) == 1
    trade = trades[0]
    assert trade["symbol"] == "BTCUSDT"
    assert trade["side"] == "LONG"
    assert trade["net_pnl_usd"] == 10.0


def test_trade_reads_scope_to_active_session(state_writer, state_reader):
    state_writer.set_risk_snapshot(
        {
            "trading_mode": "paper",
            "runtime_mode": "LIVE",
            "session_id": "session-paper",
            "bot_started_at": "2026-01-01T00:00:00+00:00",
        }
    )
    state_writer.record_trade(
        Trade(
            symbol="BTCUSDT",
            side="LONG",
            entry_time="2026-01-01T00:00:00+00:00",
            exit_time="2026-01-01T08:00:00+00:00",
            entry_price=100.0,
            exit_price=101.0,
            qty=1.0,
            net_pnl_usd=1.0,
        )
    )
    state_writer.set_risk_snapshot(
        {
            "trading_mode": "live",
            "runtime_mode": "LIVE",
            "session_id": "session-live",
            "bot_started_at": "2026-01-02T00:00:00+00:00",
        }
    )
    state_writer.record_trade(
        Trade(
            symbol="ETHUSDT",
            side="LONG",
            entry_time="2026-01-02T00:00:00+00:00",
            exit_time="2026-01-02T08:00:00+00:00",
            entry_price=200.0,
            exit_price=202.0,
            qty=1.0,
            net_pnl_usd=2.0,
        )
    )

    scoped_trades = state_reader.get_trades(limit=10)
    all_trades = state_reader.get_trades(limit=10, scope_current=False)

    assert len(scoped_trades) == 1
    assert scoped_trades[0]["symbol"] == "ETHUSDT"
    assert len(all_trades) == 2


def test_trade_reads_same_mode_history_across_restarts_when_session_scope_is_disabled(state_writer, state_reader):
    state_writer.set_risk_snapshot(
        {
            "trading_mode": "live",
            "runtime_mode": "LIVE",
            "session_id": "session-live-1",
            "bot_started_at": "2026-01-01T00:00:00+00:00",
        }
    )
    state_writer.record_trade(
        Trade(
            symbol="BTCUSDT",
            side="LONG",
            entry_time="2026-01-01T00:00:00+00:00",
            exit_time="2026-01-01T08:00:00+00:00",
            entry_price=100.0,
            exit_price=101.0,
            qty=1.0,
            net_pnl_usd=1.0,
        )
    )
    state_writer.set_risk_snapshot(
        {
            "trading_mode": "live",
            "runtime_mode": "LIVE",
            "session_id": "session-live-2",
            "bot_started_at": "2026-01-02T00:00:00+00:00",
        }
    )
    state_writer.record_trade(
        Trade(
            symbol="ETHUSDT",
            side="LONG",
            entry_time="2026-01-02T00:00:00+00:00",
            exit_time="2026-01-02T08:00:00+00:00",
            entry_price=200.0,
            exit_price=202.0,
            qty=1.0,
            net_pnl_usd=2.0,
        )
    )

    scoped_trades = state_reader.get_trades(limit=10)
    continuity_trades = state_reader.get_trades(limit=10, session_scoped=False)
    pnl = state_reader.get_pnl_attribution(session_scoped=False)

    assert len(scoped_trades) == 1
    assert scoped_trades[0]["symbol"] == "ETHUSDT"
    assert [trade["symbol"] for trade in continuity_trades] == ["ETHUSDT", "BTCUSDT"]
    assert pnl["trade_count"] == 2
    assert pnl["total_net_pnl"] == 3.0


def test_set_stat(state_writer, state_reader):
    state_writer.set_stat("total_pnl", 100.5)
    stats = state_reader.get_stats()
    assert stats["total_pnl"] == 100.5

    # Update stat
    state_writer.set_stat("total_pnl", 200.0)
    stats = state_reader.get_stats()
    assert stats["total_pnl"] == 200.0


def test_get_open_pnl_summary(state_writer, state_reader):
    state_writer.set_risk_snapshot(
        {
            "trading_mode": "testnet",
            "runtime_mode": "LIVE",
            "session_id": "session-testnet",
            "bot_started_at": "2026-04-10T00:00:00+00:00",
        }
    )
    state_writer.upsert_position(
        symbol="BTCUSDT",
        side="LONG_SPOT_SHORT_PERP",
        direction="long",
        spot_entry=100.0,
        perp_entry=101.0,
        qty=1.0,
        net_pnl_usd=12.5,
        exchange_pnl_usd=8.0,
        recovery_state="tracked",
        trading_mode="testnet",
    )
    state_writer.upsert_position(
        symbol="ETHUSDT",
        side="LONG_SPOT_SHORT_PERP",
        direction="long",
        spot_entry=200.0,
        perp_entry=201.0,
        qty=2.0,
        net_pnl_usd=-3.0,
        exchange_pnl_usd=-5.5,
        recovery_state="manual_review",
        trading_mode="testnet",
    )

    summary = state_reader.get_open_pnl_summary()

    assert summary["open_position_count"] == 2.0
    assert summary["managed_open_position_count"] == 1.0
    assert summary["manual_review_position_count"] == 1.0
    assert summary["current_unrealized_pnl"] == 9.5
    assert summary["current_exchange_unrealized_pnl"] == 2.5


def test_set_risk_and_get_risk(state_writer, state_reader):
    # String value
    state_writer.set_risk("status", "NORMAL")

    # JSON list
    state_writer.set_risk("drawdown", json.dumps([0.1, 0.2]))

    risk = state_reader.get_risk()
    assert risk["status"] == "NORMAL"
    assert risk["drawdown"] == [0.1, 0.2]


def test_set_risk_snapshot(state_writer, state_reader):
    snapshot = {
        "gross_exposure": 50000.0,
        "is_kill_switch": True,
        "reasons": ["high_volatility", "drawdown"]
    }
    state_writer.set_risk_snapshot(snapshot)

    risk = state_reader.get_risk()
    assert risk["gross_exposure"] == 50000.0
    assert risk["is_kill_switch"] is True
    assert risk["reasons"] == ["high_volatility", "drawdown"]


# --- Risk Parsing Edge Case Tests ---

def test_get_risk_valid_types(state_writer, state_reader):
    # Write valid data
    state_writer.set_risk("drawdown_pct", "0.05")
    state_writer.set_risk("spread_toxicity", "15.5")
    state_writer.set_risk("venue_latency", "120.0")
    state_writer.set_risk("kill_switch", "True")
    state_writer.set_risk("allow_new_risk", "false")
    state_writer.set_risk("reasons", json.dumps(["max drawdown breached"]))
    state_writer.set_risk("unknown_key", "arbitrary_string")
    state_writer.set_risk("another_key", '{"some": "json"}')

    risk = state_reader.get_risk()

    assert risk["drawdown_pct"] == 0.05
    assert isinstance(risk["drawdown_pct"], float)

    assert risk["spread_toxicity"] == 15.5
    assert isinstance(risk["spread_toxicity"], float)

    assert risk["venue_latency"] == 120.0
    assert isinstance(risk["venue_latency"], float)

    assert risk["kill_switch"] is True
    assert isinstance(risk["kill_switch"], bool)

    assert risk["allow_new_risk"] is False
    assert isinstance(risk["allow_new_risk"], bool)

    assert risk["reasons"] == ["max drawdown breached"]
    assert isinstance(risk["reasons"], list)

    # Unknown keys are left as strings
    assert risk["unknown_key"] == "arbitrary_string"
    assert risk["another_key"] == {"some": "json"}


def test_get_risk_invalid_floats_ignored(state_writer, state_reader):
    state_writer.set_risk("drawdown_pct", "not_a_float")
    state_writer.set_risk("spread_toxicity", "invalid")
    state_writer.set_risk("venue_latency", "---")

    risk = state_reader.get_risk()

    # Invalid floats should be ignored entirely from the result
    assert "drawdown_pct" not in risk
    assert "spread_toxicity" not in risk
    assert "venue_latency" not in risk


def test_get_risk_invalid_bools_are_false(state_writer, state_reader):
    state_writer.set_risk("kill_switch", "random_string")
    state_writer.set_risk("allow_new_risk", "1")

    risk = state_reader.get_risk()

    # Anything that isn't exactly "true" (case insensitive) is False
    assert risk["kill_switch"] is False
    assert risk["allow_new_risk"] is False


def test_get_risk_invalid_reasons_default_empty_list(state_writer, state_reader):
    # Invalid JSON
    state_writer.set_risk("reasons", "not_json")
    risk1 = state_reader.get_risk()
    assert risk1["reasons"] == []

    # Valid JSON, but not a list
    state_writer.set_risk("reasons", '{"not": "a_list"}')
    risk2 = state_reader.get_risk()
    assert risk2["reasons"] == []

    # Valid JSON list, but contains non-strings (should be cast to strings)
    state_writer.set_risk("reasons", json.dumps([1, 2.5, True]))
    risk3 = state_reader.get_risk()
    assert risk3["reasons"] == ["1", "2.5", "True"]


def test_record_execution_event_round_trip(state_writer, state_reader):
    state_writer.record_execution_event(
        {
            "symbol": "BTCUSDT",
            "client_order_id": "cid-123",
            "status": "FILLED",
            "filled_qty": 0.25,
            "avg_fill_price": 65000.5,
            "last_fill_price": 65001.0,
            "cumulative_quote_qty": 16250.125,
            "commission": 1.25,
            "commission_asset": "USDT",
            "realized_pnl": 5.0,
            "maker": True,
            "execution_type": "TRADE",
            "event_time": "2026-01-01T00:00:00+00:00",
        }
    )

    events = state_reader.get_execution_events(limit=10)
    assert len(events) == 1
    event = events[0]
    assert event["symbol"] == "BTCUSDT"
    assert event["client_order_id"] == "cid-123"
    assert event["status"] == "FILLED"
    assert event["filled_qty"] == 0.25
    assert event["avg_fill_price"] == 65000.5
    assert event["last_fill_price"] == 65001.0
    assert event["commission"] == 1.25
    assert event["commission_asset"] == "USDT"
    assert event["realized_pnl"] == 5.0
    assert event["maker"] == 1
    assert event["execution_type"] == "TRADE"


def test_record_execution_event_tolerates_missing_optional_fields(state_writer, state_reader):
    state_writer.record_execution_event(
        {
            "symbol": "ETHUSDT",
            "client_order_id": "cid-456",
            "status": "NEW",
        }
    )

    events = state_reader.get_execution_events(limit=10)
    event = events[0]
    assert event["symbol"] == "ETHUSDT"
    assert event["client_order_id"] == "cid-456"
    assert event["status"] == "NEW"
    assert event["filled_qty"] == 0.0
    assert event["avg_fill_price"] is None


def test_estimate_trade_execution_cost_converts_base_asset_commission(state_writer, state_reader):
    state_writer.record_execution_event(
        {
            "symbol": "BTCUSDT",
            "client_order_id": "entry-spot",
            "status": "FILLED",
            "commission": 2.0,
            "commission_asset": "USDT",
            "avg_fill_price": 65000.0,
            "event_time": "2026-01-01T00:00:01+00:00",
        }
    )
    state_writer.record_execution_event(
        {
            "symbol": "BTCUSDT",
            "client_order_id": "exit-spot",
            "status": "FILLED",
            "commission": 0.00001,
            "commission_asset": "BTC",
            "avg_fill_price": 70000.0,
            "event_time": "2026-01-01T08:00:00+00:00",
        }
    )

    total_cost = state_reader.estimate_trade_execution_cost(
        "BTCUSDT",
        "2026-01-01T00:00:00+00:00",
        "2026-01-01T08:00:01+00:00",
    )

    assert total_cost == pytest.approx(2.7)


def test_estimate_trade_execution_cost_sums_partial_fills_without_counting_cycle_summary(
    state_writer,
    state_reader,
):
    event_time = "2026-01-01T08:00:00+00:00"
    for status, commission in (
        ("PARTIALLY_FILLED", 0.20),
        ("PARTIALLY_FILLED", 0.30),
        ("FILLED", 0.50),
    ):
        state_writer.record_execution_event(
            {
                "symbol": "BTCUSDT",
                "client_order_id": "exit-perp",
                "status": status,
                # Some historical events exposed cumulative quantity here.  Fee
                # accounting must use each event's incremental commission instead.
                "filled_qty": 1.0,
                "commission": commission,
                "commission_asset": "USDT",
                "avg_fill_price": 65_000.0,
                "execution_type": "TRADE",
                "event_time": event_time,
            }
        )
    state_writer.record_execution_event(
        {
            "symbol": "BTCUSDT",
            "client_order_id": "",
            "status": "FILLED",
            "filled_qty": 1.0,
            # A cycle summary may eventually expose the cumulative fee.  It is
            # not another exchange fill and must not be added to the three rows.
            "commission": 1.0,
            "commission_asset": "USDT",
            "avg_fill_price": 65_000.0,
            "execution_type": "FILLED_CYCLE",
            "event_time": event_time,
        }
    )

    total_cost = state_reader.estimate_trade_execution_cost(
        "BTCUSDT",
        "2026-01-01T00:00:00+00:00",
        "2026-01-01T08:00:01+00:00",
    )

    assert total_cost == pytest.approx(1.0)


def test_estimate_trade_execution_cost_converts_third_asset_commission_via_market_samples(state_writer, state_reader):
    state_writer.record_market_sample(
        symbol="BNBUSDT",
        sample_minute="2026-01-01T08:00:00+00:00",
        ann_funding=0.0,
        basis_pct=0.0,
        mark_price=600.0,
    )
    state_writer.record_execution_event(
        {
            "symbol": "BTCUSDT",
            "client_order_id": "entry-bnb-fee",
            "status": "FILLED",
            "commission": 0.01,
            "commission_asset": "BNB",
            "avg_fill_price": 65000.0,
            "event_time": "2026-01-01T00:00:01+00:00",
        }
    )

    total_cost = state_reader.estimate_trade_execution_cost(
        "BTCUSDT",
        "2026-01-01T00:00:00+00:00",
        "2026-01-01T08:00:01+00:00",
    )

    assert total_cost == pytest.approx(6.0)


def test_state_writer_migrates_legacy_schema(temp_db_path):
    conn = sqlite3.connect(temp_db_path)
    conn.executescript(
        """
        CREATE TABLE positions (
            symbol        TEXT PRIMARY KEY,
            side          TEXT NOT NULL,
            spot_entry    REAL NOT NULL,
            perp_entry    REAL NOT NULL,
            spot_live     REAL DEFAULT 0.0,
            perp_live     REAL DEFAULT 0.0,
            qty           REAL NOT NULL,
            ann_funding   REAL DEFAULT 0.0,
            basis_pct     REAL DEFAULT 0.0,
            net_pnl_usd   REAL DEFAULT 0.0,
            status        TEXT DEFAULT 'OPEN',
            updated_at    TEXT NOT NULL
        );

        CREATE TABLE trade_history (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol            TEXT NOT NULL,
            side              TEXT NOT NULL,
            entry_time        TEXT NOT NULL,
            exit_time         TEXT NOT NULL,
            entry_price       REAL NOT NULL,
            exit_price        REAL NOT NULL,
            qty               REAL NOT NULL,
            net_pnl_usd       REAL NOT NULL,
            funding_collected REAL DEFAULT 0.0
        );
        """
    )
    conn.commit()
    conn.close()

    writer = StateWriter(db_path=temp_db_path)
    reader = StateReader(db_path=temp_db_path)
    try:
        writer.upsert_position(
            symbol="SOLUSDT",
            side="LONG",
            direction="short",
            spot_entry=150.0,
            perp_entry=149.5,
            qty=4.0,
        )
        writer.record_trade(
            Trade(
                symbol="SOLUSDT",
                side="LONG",
                entry_time="2026-01-01T00:00:00Z",
                exit_time="2026-01-01T08:00:00Z",
                entry_price=150.0,
                exit_price=151.0,
                qty=4.0,
                net_pnl_usd=4.0,
                execution_cost_usd=0.75,
                basis_pnl_usd=1.25,
            )
        )

        positions = reader.get_positions()
        trades = reader.get_trades(limit=10)
        position_columns = {
            row["name"]
            for row in reader.conn.execute("PRAGMA table_info(positions)").fetchall()
        }
        trade_columns = {
            row["name"]
            for row in reader.conn.execute("PRAGMA table_info(trade_history)").fetchall()
        }

        assert "direction" in position_columns
        assert "hedge_ratio" in position_columns
        assert "entry_ann_funding" in position_columns
        assert "exchange_pnl_usd" in position_columns
        assert "recovery_state" in position_columns
        assert "trading_mode" in position_columns
        assert "execution_cost_usd" in trade_columns
        assert "basis_pnl_usd" in trade_columns
        assert "borrow_cost_usd" in trade_columns
        assert "trading_mode" in trade_columns
        assert "runtime_mode" in trade_columns
        assert "session_id" in trade_columns
        assert "funding_source" in trade_columns
        assert positions[0]["direction"] == "short"
        assert positions[0]["hedge_ratio"] == 1.0
        assert positions[0]["entry_ann_funding"] == 0.0
        assert positions[0]["exchange_pnl_usd"] == 0.0
        assert positions[0]["recovery_state"] == ""
        assert trades[0]["execution_cost_usd"] == 0.75
        assert trades[0]["basis_pnl_usd"] == 1.25
    finally:
        reader.close()
        writer.close()


def test_pending_intents_and_samples_round_trip(state_writer, state_reader):
    state_writer.upsert_pending_intent(
        intent_id="intent-1",
        symbol="BTCUSDT",
        intent_type="ENTER_LONG",
        status="PENDING_ACK",
        direction="long",
        quantity=0.25,
        notional_usd=15_000.0,
        metadata={"source": "test"},
    )
    state_writer.record_market_sample(
        symbol="BTCUSDT",
        sample_minute="2026-01-01T00:00:00+00:00",
        ann_funding=0.15,
        basis_pct=0.001,
        mark_price=65_000.0,
        minute_notional_volume=125_000.0,
    )
    state_writer.record_health_sample(
        metric="slot_pnl_usd",
        value=12.5,
        symbol="BTCUSDT",
        expected_value=0.0,
        zscore=1.5,
        alert_level="warning",
        runtime_mode="SAFE_MODE",
    )

    pending = state_reader.get_pending_intents(statuses=["PENDING_ACK"])
    market_samples = state_reader.get_market_samples(symbol="BTCUSDT", limit=10)
    health_samples = state_reader.get_health_samples(metric="slot_pnl_usd", symbol="BTCUSDT", limit=10)

    assert pending[0]["intent_id"] == "intent-1"
    assert pending[0]["metadata"] == {"source": "test"}
    assert market_samples[0]["minute_notional_volume"] == 125_000.0
    assert health_samples[0]["alert_level"] == "warning"
    assert health_samples[0]["runtime_mode"] == "SAFE_MODE"


def test_ai_report_proposals_round_trip(state_writer, state_reader):
    state_writer.record_ai_report_proposal(
        proposal_id="weekly_20260331_01_abcd1234",
        summary="Raise entry threshold slightly",
        proposed_changes={"entry_ann_funding_threshold": 0.12},
        status="PENDING",
        report_period_start="2026-03-24T00:00:00+00:00",
        report_period_end="2026-03-31T00:00:00+00:00",
    )
    state_writer.update_ai_report_proposal(
        "weekly_20260331_01_abcd1234",
        status="APPLIED",
        decision_source="telegram",
        applied=True,
    )

    proposal = state_reader.get_ai_report_proposal("weekly_20260331_01_abcd1234")
    proposals = state_reader.get_ai_report_proposals(status="APPLIED", limit=10)

    assert proposal is not None
    assert proposal["proposed_changes"] == {"entry_ann_funding_threshold": 0.12}
    assert proposal["status"] == "APPLIED"
    assert proposal["decision_source"] == "telegram"
    assert len(proposals) == 1


def test_validation_snapshots_round_trip(state_writer, state_reader):
    metrics = {
        "go_no_go": "ADJUST",
        "validation_status": "MONITORING",
        "trade_count": 42,
        "validation_blockers": ["Sharpe 1.50 below GO target 2.00"],
    }
    state_writer.record_validation_snapshot(
        snapshot_time="2026-03-31T12:00:00+00:00",
        validation_status="MONITORING",
        go_no_go="ADJUST",
        observation_days=21.0,
        trade_count=42,
        blockers=["Sharpe 1.50 below GO target 2.00"],
        metrics=metrics,
    )

    latest = state_reader.get_latest_validation_snapshot()
    history = state_reader.get_validation_snapshots(limit=10)

    assert latest is not None
    assert latest["go_no_go"] == "ADJUST"
    assert latest["blockers"] == ["Sharpe 1.50 below GO target 2.00"]
    assert latest["metrics_json"]["trade_count"] == 42
    assert len(history) == 1


def test_archive_old_data_moves_trades_and_execution_events(tmp_path):
    db_path = str(tmp_path / "state.db")
    archive_path = str(tmp_path / "archive" / "archive.db")
    writer = StateWriter(db_path=db_path)
    reader = StateReader(db_path=db_path)
    try:
        old_time = "2025-01-01T00:00:00+00:00"
        writer.record_trade(
            Trade(
                symbol="BTCUSDT",
                side="LONG",
                entry_time=old_time,
                exit_time=old_time,
                entry_price=100.0,
                exit_price=101.0,
                qty=1.0,
                net_pnl_usd=1.0,
            )
        )
        writer.record_execution_event(
            {
                "symbol": "BTCUSDT",
                "client_order_id": "cid-archive",
                "status": "FILLED",
                "event_time": old_time,
            }
        )

        result = writer.archive_old_data(
            archive_db_path=archive_path,
            retention_days=30,
            market_retention_days=1,
            health_retention_days=1,
        )

        assert result["trade_history_archived"] == 1
        assert result["execution_events_archived"] == 1
        assert reader.get_trades(limit=10) == []
        assert reader.get_execution_events(limit=10) == []

        archive_conn = sqlite3.connect(archive_path)
        try:
            trade_count = archive_conn.execute("SELECT COUNT(*) FROM trade_history").fetchone()[0]
            event_count = archive_conn.execute("SELECT COUNT(*) FROM execution_events").fetchone()[0]
        finally:
            archive_conn.close()

        assert trade_count == 1
        assert event_count == 1
    finally:
        reader.close()
        writer.close()


def test_archive_collision_retains_the_only_source_row(tmp_path):
    source_path = str(tmp_path / "state.db")
    archive_path = str(tmp_path / "audit.db")
    old_time = "2025-01-01T00:00:00+00:00"
    source = StateWriter(db_path=source_path)
    archive = StateWriter(db_path=archive_path)
    try:
        source.record_trade(
            Trade(
                symbol="BTCUSDT",
                side="LONG",
                entry_time=old_time,
                exit_time=old_time,
                entry_price=100.0,
                exit_price=101.0,
                qty=1.0,
                net_pnl_usd=1.0,
            )
        )
        archive.record_trade(
            Trade(
                symbol="ETHUSDT",
                side="LONG",
                entry_time=old_time,
                exit_time=old_time,
                entry_price=200.0,
                exit_price=201.0,
                qty=1.0,
                net_pnl_usd=1.0,
            )
        )
    finally:
        archive.close()

    try:
        with pytest.raises(ArchiveVerificationError, match="conflicting archive"):
            source.archive_old_data(
                archive_db_path=archive_path,
                retention_days=30,
                market_retention_days=1,
                health_retention_days=1,
            )
        source_row = source.conn.execute(
            "SELECT symbol FROM trade_history WHERE id = 1"
        ).fetchone()
        assert source_row is not None
        assert source_row["symbol"] == "BTCUSDT"
    finally:
        source.close()
