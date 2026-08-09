from datetime import datetime, timedelta, timezone

from bongus.engine.state_store import StateReader, StateWriter, Trade
from bongus.monitoring.performance_metrics import calculate_metrics


def test_calculate_metrics_aggregates_trades_and_health_samples(tmp_path):
    db_path = str(tmp_path / "state.db")
    writer = StateWriter(db_path=db_path)
    reader = StateReader(db_path=db_path)
    try:
        writer.set_risk_snapshot({"account_equity": 10_000.0})
        base_time = datetime.now(timezone.utc) - timedelta(days=20)
        pnls = [20.0, 35.0, 18.0, 42.0, 30.0, 25.0, 28.0]
        for index, pnl in enumerate(pnls):
            event_time = (base_time + timedelta(days=index)).isoformat()
            writer.record_trade(
                Trade(
                    symbol="BTCUSDT",
                    side="LONG",
                    entry_time=event_time,
                    exit_time=event_time,
                    entry_price=100.0,
                    exit_price=101.0,
                    qty=1.0,
                    net_pnl_usd=pnl,
                )
            )

        for minute in range(60):
            sample_time = (datetime.now(timezone.utc) - timedelta(minutes=minute)).isoformat()
            writer.record_health_sample(
                metric="loop_alive",
                value=1.0,
                runtime_mode="LIVE",
                sample_time=sample_time,
            )
        writer.record_health_sample(
            metric="cost_model_error_pct",
            value=7.5,
            symbol="BTCUSDT",
            sample_time=datetime.now(timezone.utc).isoformat(),
        )

        metrics = calculate_metrics(reader)

        assert metrics["trade_count"] == len(pnls)
        assert metrics["total_pnl"] == sum(pnls)
        assert metrics["win_rate"] == 1.0
        assert metrics["cost_model_error_pct"] == 7.5
        assert metrics["uptime_without_manual_intervention_pct"] > 0.0
        assert metrics["intervention_free_days"] >= 14.0
        assert metrics["validation_status"] == "INSUFFICIENT_DATA"
        assert any("Clean run" in reason for reason in metrics["validation_blockers"])
        assert metrics["go_no_go"] == "ADJUST"
    finally:
        reader.close()
        writer.close()


def test_calculate_metrics_marks_no_go_on_large_drawdown(tmp_path):
    db_path = str(tmp_path / "state.db")
    writer = StateWriter(db_path=db_path)
    reader = StateReader(db_path=db_path)
    try:
        writer.set_risk_snapshot({"account_equity": 1_000.0})
        base_time = datetime(2026, 3, 1, tzinfo=timezone.utc)
        for index, pnl in enumerate([50.0, -250.0, -200.0, -100.0]):
            event_time = (base_time + timedelta(days=index)).isoformat()
            writer.record_trade(
                Trade(
                    symbol="ETHUSDT",
                    side="LONG",
                    entry_time=event_time,
                    exit_time=event_time,
                    entry_price=100.0,
                    exit_price=99.0,
                    qty=1.0,
                    net_pnl_usd=pnl,
                )
            )

        metrics = calculate_metrics(reader)

        assert metrics["max_drawdown_pct"] > 0.15
        assert metrics["go_no_go"] == "NO_GO"
    finally:
        reader.close()
        writer.close()


def test_calculate_metrics_includes_open_position_losses_in_equity_curve(tmp_path):
    db_path = str(tmp_path / "state.db")
    writer = StateWriter(db_path=db_path)
    reader = StateReader(db_path=db_path)
    try:
        writer.set_risk_snapshot({"account_equity": 5_100.0})
        event_time = datetime(2026, 3, 1, tzinfo=timezone.utc).isoformat()
        writer.record_trade(
            Trade(
                symbol="BTCUSDT",
                side="LONG",
                entry_time=event_time,
                exit_time=event_time,
                entry_price=100.0,
                exit_price=101.0,
                qty=1.0,
                net_pnl_usd=100.0,
            )
        )
        writer.upsert_position(
            symbol="ETHUSDT",
            side="LONG_SPOT_SHORT_PERP",
            direction="long",
            spot_entry=2_000.0,
            perp_entry=2_000.0,
            qty=1.0,
            net_pnl_usd=-5_000.0,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

        metrics = calculate_metrics(reader)

        assert metrics["realized_pnl"] == 100.0
        assert metrics["open_pnl_usd"] == -5_000.0
        assert metrics["total_pnl"] == -4_900.0
        assert metrics["trade_count"] == 2
        assert metrics["win_rate"] == 0.5
        assert metrics["max_drawdown_pct"] > 0.45
    finally:
        reader.close()
        writer.close()


def test_calculate_metrics_uses_same_mode_trade_history_across_restarts(tmp_path):
    db_path = str(tmp_path / "state.db")
    writer = StateWriter(db_path=db_path)
    reader = StateReader(db_path=db_path)
    try:
        writer.set_risk_snapshot(
            {
                "trading_mode": "live",
                "runtime_mode": "LIVE",
                "session_id": "live-session-1",
                "bot_started_at": "2026-03-01T00:00:00+00:00",
                "account_equity": 10_030.0,
            }
        )
        writer.record_trade(
            Trade(
                symbol="BTCUSDT",
                side="LONG",
                entry_time="2026-03-01T00:00:00+00:00",
                exit_time="2026-03-01T08:00:00+00:00",
                entry_price=100.0,
                exit_price=101.0,
                qty=1.0,
                net_pnl_usd=10.0,
            )
        )
        writer.set_risk_snapshot(
            {
                "trading_mode": "live",
                "runtime_mode": "LIVE",
                "session_id": "live-session-2",
                "bot_started_at": "2026-03-02T00:00:00+00:00",
                "account_equity": 10_030.0,
            }
        )
        writer.record_trade(
            Trade(
                symbol="ETHUSDT",
                side="LONG",
                entry_time="2026-03-02T00:00:00+00:00",
                exit_time="2026-03-02T08:00:00+00:00",
                entry_price=200.0,
                exit_price=202.0,
                qty=1.0,
                net_pnl_usd=20.0,
            )
        )

        metrics = calculate_metrics(reader)

        assert metrics["realized_pnl"] == 30.0
        assert metrics["total_pnl"] == 30.0
        assert metrics["trade_count"] == 2
    finally:
        reader.close()
        writer.close()


def test_calculate_metrics_requires_clean_run_for_go(tmp_path):
    db_path = str(tmp_path / "state.db")
    writer = StateWriter(db_path=db_path)
    reader = StateReader(db_path=db_path)
    try:
        writer.set_risk_snapshot({"account_equity": 10_000.0})
        base_time = datetime.now(timezone.utc) - timedelta(days=18)
        for index, pnl in enumerate([25.0, 22.0, 28.0, 24.0, 26.0]):
            event_time = (base_time + timedelta(days=index * 2)).isoformat()
            writer.record_trade(
                Trade(
                    symbol="SOLUSDT",
                    side="LONG",
                    entry_time=event_time,
                    exit_time=event_time,
                    entry_price=100.0,
                    exit_price=101.0,
                    qty=1.0,
                    net_pnl_usd=pnl,
                )
            )

        for minute in range(180):
            sample_time = (datetime.now(timezone.utc) - timedelta(minutes=minute)).isoformat()
            writer.record_health_sample(
                metric="loop_alive",
                value=1.0,
                runtime_mode="LIVE",
                sample_time=sample_time,
            )

        writer.record_health_sample(
            metric="operator_intervention_required",
            value=1.0,
            runtime_mode="SAFE_MODE",
            alert_level="warning",
            notes="manual restart",
            sample_time=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
        )

        metrics = calculate_metrics(reader)

        assert metrics["go_no_go"] == "ADJUST"
        assert metrics["validation_status"] in {"MONITORING", "INSUFFICIENT_DATA"}
        assert any("Clean run" in reason for reason in metrics["validation_blockers"])
    finally:
        reader.close()
        writer.close()


def test_calculate_metrics_ignores_automatic_runtime_incidents_for_manual_count(tmp_path):
    db_path = str(tmp_path / "state.db")
    writer = StateWriter(db_path=db_path)
    reader = StateReader(db_path=db_path)
    try:
        writer.set_risk_snapshot({"account_equity": 10_000.0})
        base_time = datetime.now(timezone.utc) - timedelta(days=18)
        for index, pnl in enumerate([25.0, 22.0, 28.0, 24.0, 26.0]):
            event_time = (base_time + timedelta(days=index * 2)).isoformat()
            writer.record_trade(
                Trade(
                    symbol="SOLUSDT",
                    side="LONG",
                    entry_time=event_time,
                    exit_time=event_time,
                    entry_price=100.0,
                    exit_price=101.0,
                    qty=1.0,
                    net_pnl_usd=pnl,
                )
            )

        for minute in range(180):
            sample_time = (datetime.now(timezone.utc) - timedelta(minutes=minute)).isoformat()
            writer.record_health_sample(
                metric="loop_alive",
                value=1.0,
                runtime_mode="LIVE",
                sample_time=sample_time,
            )

        writer.record_health_sample(
            metric="operator_intervention_required",
            value=1.0,
            runtime_mode="BLOCKED",
            alert_level="critical",
            notes="LIVE->BLOCKED: execution bridge preflight failed",
            sample_time=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
        )

        metrics = calculate_metrics(reader)

        assert metrics["manual_intervention_count"] == 0
        assert metrics["intervention_free_days"] >= 14.0
        assert any("Clean run" in reason for reason in metrics["validation_blockers"])
        assert not any(
            "manual intervention" in reason.lower()
            for reason in metrics["validation_blockers"]
        )
    finally:
        reader.close()
        writer.close()
