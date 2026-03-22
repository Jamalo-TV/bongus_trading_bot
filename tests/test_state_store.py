import os
import json
import pytest
import sqlite3

from bongus.engine.state_store import StateWriter, StateReader


@pytest.fixture
def temp_db(tmp_path):
    db_path = str(tmp_path / "test_state.db")
    writer = StateWriter(db_path)
    reader = StateReader(db_path)
    yield writer, reader
    writer.close()
    reader.close()
    if os.path.exists(db_path):
        os.remove(db_path)


def test_get_risk_valid_types(temp_db):
    writer, reader = temp_db

    # Write valid data
    writer.set_risk("drawdown_pct", "0.05")
    writer.set_risk("spread_toxicity", "15.5")
    writer.set_risk("venue_latency", "120.0")
    writer.set_risk("kill_switch", "True")
    writer.set_risk("allow_new_risk", "false")
    writer.set_risk("reasons", json.dumps(["max drawdown breached"]))
    writer.set_risk("unknown_key", "arbitrary_string")
    writer.set_risk("another_key", '{"some": "json"}')

    risk = reader.get_risk()

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


def test_get_risk_invalid_floats_ignored(temp_db):
    writer, reader = temp_db

    writer.set_risk("drawdown_pct", "not_a_float")
    writer.set_risk("spread_toxicity", "invalid")
    writer.set_risk("venue_latency", "---")

    risk = reader.get_risk()

    # Invalid floats should be ignored entirely from the result
    assert "drawdown_pct" not in risk
    assert "spread_toxicity" not in risk
    assert "venue_latency" not in risk


def test_get_risk_invalid_bools_are_false(temp_db):
    writer, reader = temp_db

    writer.set_risk("kill_switch", "random_string")
    writer.set_risk("allow_new_risk", "1")

    risk = reader.get_risk()

    # Anything that isn't exactly "true" (case insensitive) is False
    assert risk["kill_switch"] is False
    assert risk["allow_new_risk"] is False


def test_get_risk_invalid_reasons_default_empty_list(temp_db):
    writer, reader = temp_db

    # Invalid JSON
    writer.set_risk("reasons", "not_json")
    risk1 = reader.get_risk()
    assert risk1["reasons"] == []

    # Valid JSON, but not a list
    writer.set_risk("reasons", '{"not": "a_list"}')
    risk2 = reader.get_risk()
    assert risk2["reasons"] == []

    # Valid JSON list, but contains non-strings (should be cast to strings)
    writer.set_risk("reasons", json.dumps([1, 2.5, True]))
    risk3 = reader.get_risk()
    assert risk3["reasons"] == ["1", "2.5", "True"]
