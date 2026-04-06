"""Tests for BybitFundingMonitor — Bybit funding rate cross-validation."""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'bongus', 'market_data')))

from bybit_monitor import BybitFundingMonitor

_FUNDING_PERIODS_PER_YEAR = 1095  # 3 per day × 365


def test_get_rate_returns_none_before_first_refresh():
    """get_rate returns None when no refresh has been completed yet."""
    monitor = BybitFundingMonitor(["BTCUSDT", "ETHUSDT"])
    assert monitor.get_rate("BTCUSDT") is None
    assert monitor.get_rate("ETHUSDT") is None


def test_get_rate_returns_none_for_unknown_symbol_before_refresh():
    """get_rate returns None for symbols not tracked before any refresh."""
    monitor = BybitFundingMonitor(["BTCUSDT"])
    assert monitor.get_rate("XRPUSDT") is None


def test_get_rate_returns_correct_annualized_rate_after_population():
    """get_rate returns correct annualized rate when _rates is populated directly."""
    monitor = BybitFundingMonitor(["BTCUSDT", "ETHUSDT"])
    raw_btc = 0.0001
    raw_eth = 0.0003
    monitor._rates["BTCUSDT"] = raw_btc * _FUNDING_PERIODS_PER_YEAR
    monitor._rates["ETHUSDT"] = raw_eth * _FUNDING_PERIODS_PER_YEAR
    monitor._last_successful_refresh = datetime.now(timezone.utc)

    btc_rate = monitor.get_rate("BTCUSDT")
    eth_rate = monitor.get_rate("ETHUSDT")
    assert btc_rate is not None
    assert eth_rate is not None
    assert abs(btc_rate - 0.1095) < 1e-9
    assert abs(eth_rate - 0.3285) < 1e-9


def test_get_rate_returns_none_for_untracked_symbol_after_refresh():
    """get_rate returns None for symbols not in _rates even after a fresh refresh."""
    monitor = BybitFundingMonitor(["BTCUSDT"])
    monitor._rates["BTCUSDT"] = 0.0001 * _FUNDING_PERIODS_PER_YEAR
    monitor._last_successful_refresh = datetime.now(timezone.utc)

    assert monitor.get_rate("SOLUSDT") is None


def test_get_rate_returns_none_when_stale():
    """get_rate returns None when last refresh was more than 8 hours ago."""
    monitor = BybitFundingMonitor(["BTCUSDT"])
    monitor._rates["BTCUSDT"] = 0.0002 * _FUNDING_PERIODS_PER_YEAR
    # Set last refresh to 9 hours ago — past the 8h staleness threshold
    monitor._last_successful_refresh = datetime.now(timezone.utc) - timedelta(hours=9)

    assert monitor.get_rate("BTCUSDT") is None


def test_get_rate_returns_rate_when_just_within_staleness_window():
    """get_rate returns the rate when last refresh was just under 8 hours ago."""
    monitor = BybitFundingMonitor(["BTCUSDT"])
    raw = 0.0002
    monitor._rates["BTCUSDT"] = raw * _FUNDING_PERIODS_PER_YEAR
    # Set last refresh to 7h59m ago — still within the 8h window
    monitor._last_successful_refresh = datetime.now(timezone.utc) - timedelta(hours=7, minutes=59)

    result = monitor.get_rate("BTCUSDT")
    assert result is not None
    assert abs(result - raw * _FUNDING_PERIODS_PER_YEAR) < 1e-9


def test_dynamic_mode_accepts_all_symbols():
    """In dynamic mode (symbols=None), _rates accepts any symbol populated directly."""
    monitor = BybitFundingMonitor()  # dynamic mode
    assert monitor._dynamic is True
    monitor._rates["BTCUSDT"] = 0.0001 * _FUNDING_PERIODS_PER_YEAR
    monitor._last_successful_refresh = datetime.now(timezone.utc)

    assert monitor.get_rate("BTCUSDT") is not None
