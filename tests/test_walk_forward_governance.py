import json

from bongus.engine.state_store import StateReader, StateWriter
from scripts.walk_forward import govern_walk_forward_result


def test_governance_promotes_live_config(tmp_path):
    db_path = tmp_path / "state.db"
    config_path = tmp_path / "live_config.json"
    writer = StateWriter(str(db_path))

    result = govern_walk_forward_result(
        {"ENTRY_ANN_FUNDING_THRESHOLD": 0.22, "MAX_TOP_N": 4},
        {
            "accepted": True,
            "windows": 3,
            "windows_passing": 3,
            "avg_utilization": 0.75,
            "max_drawdown_pct": 0.02,
            "total_trades": 25,
        },
        writer=writer,
        config_path=config_path,
    )

    assert result["go_no_go"] == "GO"
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["entry_ann_funding_threshold"] == 0.22
    assert saved["max_top_n"] == 4

    reader = StateReader(str(db_path))
    promotions = reader.get_parameter_promotions()
    validations = reader.get_validation_snapshots()
    assert promotions[0]["status"] == "promoted"
    assert validations[0]["go_no_go"] == "GO"
    reader.close()
    writer.close()
