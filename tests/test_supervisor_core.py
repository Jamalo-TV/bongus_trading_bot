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
