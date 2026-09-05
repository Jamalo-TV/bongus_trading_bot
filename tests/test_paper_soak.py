from datetime import datetime, timezone

import pytest

from bongus.monitoring.progress_contract import progress_loop_deadlines
from bongus.testing.paper_soak import (
    ContinuousWindow, ProjectionDrain, health_errors, isolated_environment, shutdown_log_errors,
)


def test_paper_environment_drops_secrets_and_host_mode(tmp_path):
    env = isolated_environment({"Path": "runtime", "TRADING_MODE": "live",
                                "BINANCE_API_KEY": "secret", "TELEGRAM_TOKEN_BONGUS": "secret",
                                "BONGUS_STATE_DB_PATH": "old.db", "PYTHONPATH": "unsafe"}, tmp_path)
    assert env["TRADING_MODE"] == "paper"
    assert env["PYTHON_DOTENV_DISABLED"] == env["BONGUS_DISABLE_DOTENV"] == "1"
    assert env["Path"] == "runtime"
    assert not any(key in env for key in ("BINANCE_API_KEY", "TELEGRAM_TOKEN_BONGUS",
                                          "BONGUS_STATE_DB_PATH", "PYTHONPATH"))


def healthy_payloads():
    now = datetime.now(timezone.utc)
    heartbeat = {"updated_at": now.isoformat(), "session_id": "run_test",
                 "loop_heartbeat_ages": {name: 1.0 for name in progress_loop_deadlines()}}
    risk = {"trading_mode": "paper", "session_id": "run_test", "preflight_status": "passed", "runtime_ready": True,
            "execution_bridge_healthy": True, "telemetry_connected": True,
            "critical_telemetry_receipt_healthy": True, "rust_execution_ready": True,
            "loop_last_alive_at": now.isoformat(), "funding_fresh_symbol_count": 2}
    return now, heartbeat, risk


def test_idle_aggregate_heartbeat_cannot_hide_dead_trading_loop():
    now, heartbeat, risk = healthy_payloads()
    assert health_errors(heartbeat, risk, now=now) == []
    heartbeat["loop_heartbeat_ages"]["trading_loop"] = 121
    assert "loop_stale:trading_loop" in health_errors(heartbeat, risk, now=now)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, None, True])
def test_invalid_progress_never_passes(value):
    now, heartbeat, risk = healthy_payloads()
    heartbeat["loop_heartbeat_ages"]["trading_loop"] = value
    assert health_errors(heartbeat, risk, now=now)


def test_testnet_or_unready_engine_is_not_accepted_as_paper_soak():
    now, heartbeat, risk = healthy_payloads()
    risk.update(trading_mode="testnet", rust_execution_ready=False)
    errors = health_errors(heartbeat, risk, now=now)
    assert "mode_is_not_paper" in errors
    assert "not_ready:rust_execution_ready" in errors


def test_continuous_window_excludes_startup_and_rejects_restart():
    window = ContinuousWindow(1800)
    assert window.observe(0, "old", ["starting"]) == 0
    assert window.observe(100, "new", []) == 0
    assert window.observe(110, "new", []) == 10
    with pytest.raises(RuntimeError, match="identity changed"):
        window.observe(120, "restarted", [])


def test_continuous_window_fails_on_observer_gap_or_health_failure():
    for errors, timestamp in (([], 121), (["lost IPC"], 105)):
        window = ContinuousWindow(1800)
        window.observe(100, "same", [])
        with pytest.raises(RuntimeError):
            window.observe(timestamp, "same", errors)


def test_short_test_cannot_claim_30_minute_success():
    with pytest.raises(ValueError):
        ContinuousWindow(1799)


def test_projection_burst_must_drain_completely_within_deadline():
    drain = ProjectionDrain()
    assert drain.observe(0, 1) == []
    assert drain.observe(15, 2) == []
    assert drain.observe(29, 0) == []
    assert drain.observe(30, 2) == []
    # Partial progress cannot reset the absolute drain deadline.
    assert drain.observe(59, 1) == []
    assert drain.observe(60, 1) == ["critical_projection_drain_failed"]


@pytest.mark.parametrize("value", [None, True, -1, 1.5, float("nan"), 101])
def test_invalid_or_excessive_projection_backlog_fails(value):
    assert ProjectionDrain().observe(0, value)


@pytest.mark.parametrize("value", [None, True, -1, float("nan"), float("inf")])
def test_invalid_funding_freshness_fails(value):
    now, heartbeat, risk = healthy_payloads()
    risk["funding_fresh_symbol_count"] = value
    assert "no_fresh_funding" in health_errors(heartbeat, risk, now=now)


@pytest.mark.parametrize("session", [None, "", "old_session"])
def test_wrong_or_missing_session_cannot_pass(session):
    now, heartbeat, risk = healthy_payloads()
    heartbeat["session_id"] = session
    assert "runtime_session_mismatch" in health_errors(heartbeat, risk, now=now)


def test_forced_child_stop_is_not_clean_shutdown():
    assert shutdown_log_errors("trader did not terminate gracefully; sending SIGKILL.")
    assert shutdown_log_errors("[WATCHDOG] Error while stopping rust: timeout")
    assert shutdown_log_errors("Watchdog shutting down. Terminating child processes...") == []
