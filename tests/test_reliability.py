"""Tests for operational reliability helpers."""

import pytest
from bongus.engine.reliability import choose_failover_target

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
