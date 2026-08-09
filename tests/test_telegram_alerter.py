import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from unittest.mock import patch

import pytest

pytest.importorskip("aiohttp")

from bongus.engine.state_store import StateReader, StateWriter
from bongus.monitoring import telegram_alerter


def setup_function():
    telegram_alerter._last_alert.clear()
    telegram_alerter._escalation_level.clear()
    telegram_alerter._disconnected_symbols.clear()


class _SequenceReader:
    def __init__(self, risk_states: list[dict], *, positions: list[dict] | None = None, trades: list[dict] | None = None):
        self._risk_states = [dict(state) for state in risk_states]
        self._index = 0
        self._positions = list(positions or [])
        self._trades = list(trades or [])

    def step(self) -> None:
        self._index += 1
        if self._index >= len(self._risk_states):
            raise asyncio.CancelledError

    def get_positions_for_current_mode(self):
        return list(self._positions)

    def get_trades(self, limit=500):
        del limit
        return list(self._trades)

    def get_risk(self):
        return dict(self._risk_states[self._index])


class _DummyWriter:
    pass


def _risk_snapshot(
    *,
    kill_switch: bool = False,
    runtime_mode: str = "LIVE",
    safe_mode_reason: str = "",
    settling_until: str = "",
) -> dict:
    return {
        "kill_switch": kill_switch,
        "runtime_mode": runtime_mode,
        "preflight_status": "",
        "safe_mode_reason": safe_mode_reason,
        "config_last_error": "",
        "heartbeat_status": "",
        "drawdown_pct": 0.0,
        "runtime_settling_until_iso": settling_until,
    }


def _run_poll_state_alerts(risk_states: list[dict]) -> list[str]:
    reader = _SequenceReader(risk_states)
    sent_messages: list[str] = []
    clock = {"value": 0.0}

    async def fake_sleep(_seconds: float) -> None:
        clock["value"] += 35.0
        reader.step()

    async def fake_send(_session, message: str) -> None:
        sent_messages.append(message)

    with (
        patch("bongus.monitoring.telegram_alerter._runtime_reader", return_value=reader),
        patch("bongus.monitoring.telegram_alerter._runtime_writer", return_value=_DummyWriter()),
        patch("bongus.monitoring.telegram_alerter.asyncio.sleep", side_effect=fake_sleep),
        patch("bongus.monitoring.telegram_alerter.send_telegram", side_effect=fake_send),
        patch("bongus.monitoring.telegram_alerter._time.monotonic", side_effect=lambda: clock["value"]),
    ):
        try:
            asyncio.run(telegram_alerter.poll_state_alerts(cast(Any, object())))
        except asyncio.CancelledError:
            pass

    return sent_messages


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


def test_apply_proposal_to_config_rejects_any_risk_increase_atomically(tmp_path):
    config_path = tmp_path / "live_config.json"
    initial = {
        "entry_ann_funding_threshold": 0.1,
        "notional_per_trade": 2_500.0,
        "pause_new_entries": True,
    }
    config_path.write_text(json.dumps(initial), encoding="utf-8")
    proposal = {
        "proposed_changes": {
            "entry_ann_funding_threshold": 0.2,
            "notional_per_trade": 3_000.0,
        }
    }

    with patch.object(telegram_alerter, "_LIVE_CONFIG_PATH", str(config_path)):
        applied, reason = telegram_alerter._apply_proposal_to_config(proposal)

    assert applied is False
    assert "notional_per_trade" in reason
    assert json.loads(config_path.read_text(encoding="utf-8")) == initial


def test_apply_proposal_to_config_never_allows_remote_resume(tmp_path):
    config_path = tmp_path / "live_config.json"
    config_path.write_text(json.dumps({"pause_new_entries": True}), encoding="utf-8")

    with patch.object(telegram_alerter, "_LIVE_CONFIG_PATH", str(config_path)):
        applied, reason = telegram_alerter._apply_proposal_to_config(
            {"proposed_changes": {"pause_new_entries": False}}
        )

    assert applied is False
    assert "pause_new_entries" in reason
    assert json.loads(config_path.read_text(encoding="utf-8"))["pause_new_entries"] is True


def test_daily_summary_uses_trading_mode_label(tmp_path):
    db_path = str(tmp_path / "state.db")
    writer = StateWriter(db_path=db_path)
    reader = StateReader(db_path=db_path)
    try:
        writer.set_risk_snapshot(
            {
                "trading_mode": "paper",
                "runtime_mode": "LIVE",
                "account_equity": 10_000.0,
            }
        )
        summary = telegram_alerter._format_daily_summary(reader)
        assert "Mode: `paper`" in summary
        assert "Realized / Open:" in summary
    finally:
        reader.close()
        writer.close()


def test_format_safe_mode_reason_includes_manual_review_and_hedge_gap_details():
    formatted = telegram_alerter._format_safe_mode_reason(
        {
            "safe_mode_reason": "hedge_gap, startup_manual_review",
            "startup_reconciliation_spot_hedge_gaps": ["NILUSDT"],
            "startup_reconciliation_manual_review": ["NILUSDT"],
        }
    )

    assert "hedge_gap, startup_manual_review" in formatted
    assert "hedge_gap=NILUSDT" in formatted
    assert "manual_review=NILUSDT" in formatted


def test_kill_switch_debounce_suppresses_single_tick_flap():
    messages = _run_poll_state_alerts(
        [
            _risk_snapshot(),
            _risk_snapshot(kill_switch=True),
            _risk_snapshot(kill_switch=False),
            _risk_snapshot(kill_switch=True),
            _risk_snapshot(kill_switch=True),
            _risk_snapshot(kill_switch=True),
        ]
    )

    assert sum("KILL SWITCH ACTIVATED" in message for message in messages) == 1


def test_settling_window_consolidates_mode_flap():
    settling_until = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    settled_past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    messages = _run_poll_state_alerts(
        [
            _risk_snapshot(runtime_mode="LIVE"),
            _risk_snapshot(
                runtime_mode="LIVE_WITH_SYMBOL_BLOCKS",
                safe_mode_reason="startup_manual_review",
                settling_until=settling_until,
            ),
            _risk_snapshot(
                runtime_mode="SAFE_MODE",
                safe_mode_reason="risk_limits, startup_manual_review",
                settling_until=settling_until,
            ),
            _risk_snapshot(
                runtime_mode="LIVE_WITH_SYMBOL_BLOCKS",
                safe_mode_reason="startup_manual_review",
                settling_until=settling_until,
            ),
            _risk_snapshot(
                runtime_mode="SAFE_MODE",
                safe_mode_reason="risk_limits, startup_manual_review",
                settling_until=settled_past,
            ),
        ]
    )

    restart_mode_messages = [message for message in messages if "Post-restart mode:" in message]
    assert len(restart_mode_messages) == 1
    assert "SAFE_MODE" in restart_mode_messages[0]


def test_settling_window_sends_single_restart_notice_for_persisted_kill_switch():
    settling_until = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    settled_past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    messages = _run_poll_state_alerts(
        [
            _risk_snapshot(
                kill_switch=True,
                runtime_mode="SAFE_MODE",
                safe_mode_reason="risk_limits",
                settling_until=settling_until,
            ),
            _risk_snapshot(
                kill_switch=True,
                runtime_mode="SAFE_MODE",
                safe_mode_reason="risk_limits",
                settling_until=settling_until,
            ),
            _risk_snapshot(
                kill_switch=True,
                runtime_mode="SAFE_MODE",
                safe_mode_reason="risk_limits",
                settling_until=settled_past,
            ),
        ]
    )

    assert sum("Kill switch remains active from the prior session" in message for message in messages) == 1
    assert not any("KILL SWITCH ACTIVATED" in message for message in messages)


