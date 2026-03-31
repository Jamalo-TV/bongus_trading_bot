import json

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


def test_parse_approval_message_accepts_expected_format():
    assert telegram_alerter._parse_approval_message("ja weekly_20260331_01_abcd1234") == (
        "ja",
        "weekly_20260331_01_abcd1234",
    )
    assert telegram_alerter._parse_approval_message(" nein   foo_bar-123 ") == (
        "nein",
        "foo_bar-123",
    )
    assert telegram_alerter._parse_approval_message("maybe foo") is None


def test_apply_proposal_to_config_writes_whitelisted_keys(tmp_path):
    config_path = tmp_path / "live_config.json"
    config_path.write_text(json.dumps({"entry_ann_funding_threshold": 0.1}), encoding="utf-8")
    proposal = {
        "proposed_changes": {
            "entry_ann_funding_threshold": 0.2,
            "not_whitelisted": 123,
        }
    }

    with patch.object(telegram_alerter, "_LIVE_CONFIG_PATH", str(config_path)):
        applied, changed_keys = telegram_alerter._apply_proposal_to_config(proposal)

    assert applied is True
    assert changed_keys == "entry_ann_funding_threshold"
    written = json.loads(config_path.read_text(encoding="utf-8"))
    assert written["entry_ann_funding_threshold"] == 0.2
    assert "not_whitelisted" not in written
