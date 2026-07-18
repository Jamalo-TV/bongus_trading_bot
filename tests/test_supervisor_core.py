import json
from datetime import datetime, timedelta, timezone

from bongus.engine.state_store import StateReader, StateWriter, Trade
from bongus.supervisor.core import build_recommendations, collect_snapshot
from bongus.supervisor.store import SupervisorStore


def test_collect_snapshot_and_build_recommendations(tmp_path):
    db_path = str(tmp_path / "supervisor_core.db")
    writer = StateWriter(db_path=db_path)
    writer.set_stat("account_equity", 10_000.0)
    writer.set_stat("total_pnl", -250.0)
    writer.set_stat("gross_exposure", 46_000.0)
    writer.set_stat("max_gross_exposure", 50_000.0)
    writer.set_stat("ann_funding", -0.01)
    writer.set_stat("win_rate", 0.35)
    writer.set_stat("trade_count", 6)
    writer.set_risk("drawdown_pct", "0.05")
    writer.set_risk("kill_switch", "false")
    writer.set_risk("allow_new_risk", "false")
    writer.set_risk("pause_new_entries", "false")
    writer.set_risk("reasons", json.dumps(["venue latency too high"]))
    writer.upsert_position(
        symbol="BTCUSDT",
        side="LONG_SPOT_SHORT_PERP",
        spot_entry=100.0,
        perp_entry=101.0,
        qty=1.0,
        ann_funding=-0.01,
        basis_pct=0.001,
        spot_live=99.0,
        perp_live=100.0,
    )
    writer.record_trade(
        Trade(
            symbol="BTCUSDT",
            side="LONG",
            entry_time=(datetime.now(timezone.utc) - timedelta(hours=8)).isoformat(),
            exit_time=datetime.now(timezone.utc).isoformat(),
            entry_price=100.0,
            exit_price=99.0,
            qty=1.0,
            net_pnl_usd=-25.0,
            funding_collected=10.0,
            execution_cost_usd=12.0,
            basis_pnl_usd=-3.0,
        )
    )
    writer.close()

    reader = StateReader(db_path=db_path)
    store = SupervisorStore(db_path=db_path)
    snapshot = collect_snapshot(reader, store)

    assert snapshot.regime == "STRESS"
    assert any("Execution costs" in item for item in snapshot.anomalies)
    assert any("Open position exists" in item for item in snapshot.anomalies)

    recommendations = build_recommendations(
        snapshot,
        {
            "entry_ann_funding_threshold": 0.15,
            "exit_ann_funding_threshold": 0.01,
            "notional_per_trade": 20_000.0,
        },
        "weekly",
    )

    target_keys = {rec.target_key for rec in recommendations}
    assert "entry_ann_funding_threshold" in target_keys
    assert "notional_per_trade" in target_keys
    assert "exit_ann_funding_threshold" in target_keys

    reader.close()
    store.close()


def test_collect_snapshot_uses_open_position_funding_when_stats_are_missing(tmp_path):
    db_path = str(tmp_path / "supervisor_core_live_position.db")
    writer = StateWriter(db_path=db_path)
    writer.set_stat("account_equity", 10_000.0)
    writer.set_stat("gross_exposure", 10_000.0)
    writer.set_stat("max_gross_exposure", 50_000.0)
    writer.set_risk("drawdown_pct", "0.0")
    writer.set_risk("kill_switch", "false")
    writer.set_risk("allow_new_risk", "true")
    writer.set_risk("pause_new_entries", "false")
    writer.set_risk("funding_staleness_status", "fresh")
    writer.upsert_position(
        symbol="ETHUSDT",
        side="LONG_SPOT_SHORT_PERP",
        spot_entry=100.0,
        perp_entry=101.0,
        qty=1.0,
        ann_funding=0.18,
        basis_pct=0.001,
        spot_live=99.0,
        perp_live=100.0,
    )
    writer.close()

    reader = StateReader(db_path=db_path)
    store = SupervisorStore(db_path=db_path)
    snapshot = collect_snapshot(reader, store)

    assert snapshot.ann_funding == 0.18
    assert not any("Open position exists" in item for item in snapshot.anomalies)

    reader.close()
    store.close()


def test_collect_snapshot_ignores_old_cost_vs_funding_history(tmp_path):
    db_path = str(tmp_path / "supervisor_core_old_history.db")
    writer = StateWriter(db_path=db_path)
    writer.set_stat("account_equity", 10_000.0)
    writer.set_stat("total_pnl", 0.0)
    writer.set_stat("gross_exposure", 0.0)
    writer.set_stat("max_gross_exposure", 50_000.0)
    writer.set_risk("drawdown_pct", "0.0")
    writer.set_risk("kill_switch", "false")
    writer.set_risk("allow_new_risk", "true")
    writer.set_risk("pause_new_entries", "false")
    old_exit_time = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    old_entry_time = (datetime.now(timezone.utc) - timedelta(days=30, hours=8)).isoformat()
    for idx in range(3):
        writer.record_trade(
            Trade(
                symbol=f"BTCUSDT_{idx}",
                side="LONG",
                entry_time=old_entry_time,
                exit_time=old_exit_time,
                entry_price=100.0,
                exit_price=99.0,
                qty=1.0,
                net_pnl_usd=-5.0,
                funding_collected=1.0,
                execution_cost_usd=2.0,
                basis_pnl_usd=-4.0,
            )
        )
    writer.close()

    reader = StateReader(db_path=db_path)
    store = SupervisorStore(db_path=db_path)
    snapshot = collect_snapshot(reader, store)

    assert snapshot.total_funding_usd == 0.0
    assert snapshot.total_execution_cost_usd == 0.0
    assert not any("Execution costs are consuming most of realized funding." == item for item in snapshot.anomalies)
    assert not any("Recent realized funding is non-positive while execution costs remain positive." == item for item in snapshot.anomalies)

    reader.close()
    store.close()


def test_live_supervisor_metrics_exclude_unreconciled_trade_economics(tmp_path):
    db_path = str(tmp_path / "supervisor_core_economic_status.db")
    writer = StateWriter(db_path=db_path)
    writer.set_risk("trading_mode", "live")
    writer.set_risk("drawdown_pct", "0.0")
    now = datetime.now(timezone.utc).isoformat()
    for symbol, pnl, status in (
        ("BTCUSDT", 10.0, "RECONCILED"),
        ("ETHUSDT", 500.0, "MODELED"),
        ("SOLUSDT", 750.0, "INCOMPLETE"),
    ):
        writer.record_trade(
            Trade(
                symbol=symbol,
                side="LONG_SPOT_SHORT_PERP",
                entry_time=now,
                exit_time=now,
                entry_price=100.0,
                exit_price=100.0,
                qty=1.0,
                net_pnl_usd=pnl,
                funding_collected=pnl,
                economic_status=status,
                trading_mode="live",
            )
        )
    writer.close()

    reader = StateReader(db_path=db_path)
    store = SupervisorStore(db_path=db_path)
    snapshot = collect_snapshot(reader, store)

    assert snapshot.trade_count == 1
    assert snapshot.total_pnl_usd == 10.0
    assert snapshot.total_funding_usd == 10.0

    reader.close()
    store.close()
