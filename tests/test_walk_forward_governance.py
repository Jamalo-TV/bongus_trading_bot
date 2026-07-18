from bongus.engine.state_store import StateReader, StateWriter
from scripts.walk_forward import govern_walk_forward_result


def test_governance_records_proposal_without_mutating_live_config(tmp_path):
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
    assert result["promotion_status"] == "proposed"
    assert result["live_config_mutated"] is False
    assert not config_path.exists()

    reader = StateReader(str(db_path))
    promotions = reader.get_parameter_promotions()
    validations = reader.get_validation_snapshots()
    assert promotions[0]["status"] == "proposed"
    assert promotions[0]["params"]["entry_ann_funding_threshold"] == 0.22
    assert promotions[0]["metadata"]["requires_operator_approval"] is True
    assert promotions[0]["metadata"]["live_config_mutated"] is False
    assert validations[0]["go_no_go"] == "GO"
    reader.close()
    writer.close()
