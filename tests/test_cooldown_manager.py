"""Tests for time-penalized cooldowns."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from bongus.engine.cooldown_manager import CooldownManager


def test_global_cooldown_blocks_all_symbols_until_expiry():
    manager = CooldownManager()
    manager.activate_global(60.0, "stress", now_ts=100.0)

    assert manager.is_global_active(now_ts=120.0) is True
    allowed, reason = manager.allow_symbol("BTCUSDT", now_ts=120.0)
    assert allowed is False
    assert reason == "stress"

    assert manager.is_global_active(now_ts=161.0) is False
    allowed, reason = manager.allow_symbol("BTCUSDT", now_ts=161.0)
    assert allowed is True
    assert reason == ""


def test_symbol_cooldown_blocks_only_target_symbol():
    manager = CooldownManager()
    manager.activate_symbol("BTCUSDT", 120.0, "symbol stress", now_ts=50.0)

    allowed_btc, reason_btc = manager.allow_symbol("BTCUSDT", now_ts=100.0)
    allowed_eth, reason_eth = manager.allow_symbol("ETHUSDT", now_ts=100.0)

    assert allowed_btc is False
    assert reason_btc == "symbol stress"
    assert allowed_eth is True
    assert reason_eth == ""


def test_longer_cooldown_replaces_shorter_one():
    manager = CooldownManager()
    manager.activate_symbol("BTCUSDT", 60.0, "first", now_ts=0.0)
    manager.activate_symbol("BTCUSDT", 30.0, "shorter", now_ts=10.0)
    manager.activate_symbol("BTCUSDT", 300.0, "longer", now_ts=20.0)

    snapshot = manager.snapshot(now_ts=100.0)
    assert snapshot["symbol_cooldowns"]["BTCUSDT"]["reason"] == "longer"
    assert snapshot["symbol_cooldowns"]["BTCUSDT"]["remaining_s"] > 0.0
