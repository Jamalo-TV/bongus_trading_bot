import os
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from bongus.engine.state_store import StateWriter, StateReader


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
    state_writer.record_trade(
        symbol="BTCUSDT",
        side="LONG",
        entry_time="2023-01-01T00:00:00Z",
        exit_time="2023-01-02T00:00:00Z",
        entry_price=100.0,
        exit_price=110.0,
        qty=1.0,
        net_pnl_usd=10.0,
        funding_collected=0.5
    )

    trades = state_reader.get_trades(limit=10)
    assert len(trades) == 1
    trade = trades[0]
    assert trade["symbol"] == "BTCUSDT"
    assert trade["side"] == "LONG"
    assert trade["net_pnl_usd"] == 10.0


def test_set_stat(state_writer, state_reader):
    state_writer.set_stat("total_pnl", 100.5)
    stats = state_reader.get_stats()
    assert stats["total_pnl"] == 100.5

    # Update stat
    state_writer.set_stat("total_pnl", 200.0)
    stats = state_reader.get_stats()
    assert stats["total_pnl"] == 200.0


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
    assert risk["another_key"] == '{"some": "json"}'


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