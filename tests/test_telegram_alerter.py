from unittest.mock import patch

import pytest

pytest.importorskip("aiohttp")

from bongus.monitoring import telegram_alerter


def setup_function():
    telegram_alerter._last_alert.clear()
    telegram_alerter._escalation_level.clear()
    telegram_alerter._disconnected_symbols.clear()


def test_disconnect_alerts_escalate_per_symbol():
    with patch("bongus.monitoring.telegram_alerter._time.monotonic", side_effect=[0.0, 100.0, 1000.0, 2500.0]):
        assert telegram_alerter._should_send_disconnect("btcusdt") is True
        assert telegram_alerter._escalation_level["BTCUSDT"] == 1

        assert telegram_alerter._should_send_disconnect("btcusdt") is False
        assert telegram_alerter._escalation_level["BTCUSDT"] == 1

        assert telegram_alerter._should_send_disconnect("btcusdt") is True
        assert telegram_alerter._escalation_level["BTCUSDT"] == 2

        assert telegram_alerter._should_send_disconnect("btcusdt") is False


def test_reconnect_resets_disconnect_state():
    with patch("bongus.monitoring.telegram_alerter._time.monotonic", return_value=0.0):
        assert telegram_alerter._should_send_disconnect("ethusdt") is True

    assert telegram_alerter._consume_reconnect("ethusdt") is True
    assert "ETHUSDT" not in telegram_alerter._disconnected_symbols
    assert "ETHUSDT" not in telegram_alerter._escalation_level
    assert "disconnect_ETHUSDT" not in telegram_alerter._last_alert
    assert telegram_alerter._consume_reconnect("ethusdt") is False
