from datetime import datetime, timedelta, timezone

from bongus.engine.state_store import StateReader, StateWriter, Trade
from bongus.monitoring.performance_metrics import calculate_metrics


def test_calculate_metrics_aggregates_trades_and_health_samples(tmp_path):
    db_path = str(tmp_path / "state.db")
    writer = StateWriter(db_path=db_path)
    reader = StateReader(db_path=db_path)
    try:
        writer.set_risk_snapshot({"account_equity": 10_000.0})
        base_time = datetime(2026, 3, 1, tzinfo=timezone.utc)
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
        assert metrics["go_no_go"] in {"GO", "ADJUST"}
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
