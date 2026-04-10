"""Tests for operational reliability helpers."""

import os
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from bongus.engine.reliability import (
    Incident,
    ServiceHealth,
    choose_failover_target,
    load_secret_env,
    open_incident,
    reconcile_state,
)


@pytest.mark.parametrize(
    "primary_ok, backup_ok, expected",
    [
        (True, True, "primary"),
        (True, False, "primary"),
        (False, True, "backup"),
        (False, False, "halt"),
    ],
)
def test_choose_failover_target(primary_ok: bool, backup_ok: bool, expected: str):
    assert choose_failover_target(primary_ok, backup_ok) == expected


def test_service_health():
    now = datetime.now(timezone.utc)

    # Healthy case
    healthy_service = ServiceHealth(name="test_service", last_heartbeat_utc=now, consecutive_failures=0)
    assert healthy_service.healthy is True

    # Unhealthy case
    unhealthy_service = ServiceHealth(name="test_service", last_heartbeat_utc=now, consecutive_failures=1)
    assert unhealthy_service.healthy is False

    unhealthy_service_2 = ServiceHealth(name="test_service", last_heartbeat_utc=now, consecutive_failures=5)
    assert unhealthy_service_2.healthy is False


def test_load_secret_env():
    # Test existing environment variable
    with patch.dict(os.environ, {"EXISTING_SECRET": "secret_value"}):
        value = load_secret_env("EXISTING_SECRET")
        assert value == "secret_value"

    # Test missing environment variable
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(RuntimeError, match="Missing required secret env var: MISSING_SECRET"):
            load_secret_env("MISSING_SECRET")

    # Test empty string environment variable
    with patch.dict(os.environ, {"EMPTY_SECRET": ""}):
        with pytest.raises(RuntimeError, match="Missing required secret env var: EMPTY_SECRET"):
            load_secret_env("EMPTY_SECRET")


def test_reconcile_state():
    # Exact match
    res = reconcile_state(expected_position=100.0, actual_position=100.0, expected_cash=500.0, actual_cash=500.0)
    assert res.matched is True
    assert res.position_diff == 0.0
    assert res.cash_diff == 0.0

    # Match within tolerance
    res = reconcile_state(expected_position=100.0, actual_position=100.0000001, expected_cash=500.0, actual_cash=499.9999999)
    assert res.matched is True
    assert res.position_diff == pytest.approx(0.0000001)
    assert res.cash_diff == pytest.approx(-0.0000001)

    # Match edge of tolerance
    res = reconcile_state(expected_position=100.0, actual_position=100.000001, expected_cash=500.0, actual_cash=499.999999, tolerance=1e-6)
    assert res.matched is True

    # Position mismatch outside tolerance
    res = reconcile_state(expected_position=100.0, actual_position=100.000002, expected_cash=500.0, actual_cash=500.0)
    assert res.matched is False
    assert res.position_diff == pytest.approx(0.000002)
    assert res.cash_diff == 0.0

    # Cash mismatch outside tolerance
    res = reconcile_state(expected_position=100.0, actual_position=100.0, expected_cash=500.0, actual_cash=500.01)
    assert res.matched is False
    assert res.position_diff == 0.0
    assert res.cash_diff == pytest.approx(0.01)

    # Both mismatch
    res = reconcile_state(expected_position=100.0, actual_position=101.0, expected_cash=500.0, actual_cash=499.0)
    assert res.matched is False
    assert res.position_diff == 1.0
    assert res.cash_diff == -1.0


@patch('bongus.engine.reliability.datetime')
def test_open_incident(mock_datetime):
    # Mock datetime.now to return a fixed time
    mock_now = datetime(2023, 10, 25, 12, 30, 45, tzinfo=timezone.utc)
    mock_datetime.now.return_value = mock_now

    severity = "CRITICAL"
    message = "System failure"

    incident = open_incident(severity=severity, message=message)

    assert isinstance(incident, Incident)
    assert incident.incident_id == "INC-20231025-123045"
    assert incident.severity == severity
    assert incident.message == message
    assert incident.created_at_utc == mock_now

    mock_datetime.now.assert_called_once_with(timezone.utc)