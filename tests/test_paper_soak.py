from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from bongus.monitoring.progress_contract import progress_loop_deadlines
from bongus.testing.paper_soak import (
    ContinuousWindow, health_errors, isolated_environment, projection_errors, shutdown_log_errors,
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


def test_projection_checks_actual_receipt_age_not_continuously_busy_queue():
    now = datetime.now(timezone.utc)
    for elapsed in (0, 30, 60, 300):
        observed = now + timedelta(seconds=elapsed)
        assert projection_errors(1, (observed - timedelta(seconds=1)).isoformat(), now=observed) == []
    assert projection_errors(1, (now - timedelta(seconds=30)).isoformat(), now=now)
    assert projection_errors(1, None, now=now)
    assert projection_errors(0, None, now=now) == []


@pytest.mark.parametrize("value", [None, True, -1, 1.5, float("nan"), 101])
def test_invalid_or_excessive_projection_backlog_fails(value):
    now = datetime.now(timezone.utc)
    assert projection_errors(value, now.isoformat(), now=now)


def test_projection_reads_durable_pending_receipts_and_ignores_processed_rows(tmp_path):
    from scripts.run_paper_soak import read_projection_status

    path = tmp_path / "state.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE telemetry_receipts (status TEXT, first_seen_at TEXT)")
        conn.executemany("INSERT INTO telemetry_receipts VALUES (?, ?)", [
            ("PROCESSED", "2020-01-01T00:00:00+00:00"),
            ("PROCESSING", "2026-09-05T12:00:02+00:00"),
            ("PROCESSING", "2026-09-05T12:00:01+00:00"),
        ])
    assert read_projection_status(path) == (2, "2026-09-05T12:00:01+00:00")
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE telemetry_receipts SET status='PROCESSED'")
    assert read_projection_status(path) == (0, None)


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


def test_shutdown_requires_drained_receipts_and_publications(tmp_path):
    from scripts.run_paper_soak import read_pending_critical_projections

    path = tmp_path / "state.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE telemetry_receipts (status TEXT)")
        conn.execute("CREATE TABLE schema_meta (key TEXT, value TEXT)")
        conn.executemany("INSERT INTO telemetry_receipts VALUES (?)", [("PROCESSED",), ("PROCESSING",)])
        conn.executemany("INSERT INTO schema_meta VALUES (?, ?)", [
            ("split_store_activation_mode", "fresh-split-v1"),
            ("telemetry_publication:v1:a", '{"status":"PROCESSED"}'),
            ("telemetry_publication:v1:b", '{"status":"PROCESSING"}'),
        ])
    assert read_pending_critical_projections(path) == {"telemetry_receipts": 1, "telemetry_publications": 1}
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE schema_meta SET value='{}' WHERE key='telemetry_publication:v1:b'")
    with pytest.raises(ValueError, match="invalid durable publication"):
        read_pending_critical_projections(path)


def test_forced_child_stop_is_not_clean_shutdown():
    assert shutdown_log_errors("trader did not terminate gracefully; sending SIGKILL.")
    assert shutdown_log_errors("[WATCHDOG] Error while stopping rust: timeout")
    assert shutdown_log_errors("Watchdog shutting down. Terminating child processes...") == []
