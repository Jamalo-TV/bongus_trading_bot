import os
import sqlite3
import tempfile
import json
from datetime import datetime, timezone

import pytest

from bongus.engine.state_store import StateWriter, StateReader


@pytest.fixture
def temp_db_path():
    # Use a temporary file for the database to ensure a clean state per test
    fd, path = tempfile.mkstemp()
    os.close(fd)
    yield path
    os.remove(path)


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
