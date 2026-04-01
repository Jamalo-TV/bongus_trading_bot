from datetime import datetime, timedelta, timezone

from bongus.engine.state_store import StateWriter
from bongus.supervisor.models import SupervisorRecommendation
from bongus.supervisor.store import SupervisorStore


def test_supervisor_store_round_trip(tmp_path):
    db_path = str(tmp_path / "supervisor.db")
    writer = StateWriter(db_path=db_path)
    writer.set_stat("gross_exposure", 123.0)
    writer.close()

    store = SupervisorStore(db_path=db_path)
    now = datetime.now(timezone.utc)
    recommendation = SupervisorRecommendation(
        recommendation_id="abc123",
        created_at=now.isoformat(),
        report_kind="weekly",
        kind="tighten_entry_threshold",
        target_key="entry_ann_funding_threshold",
        current_value=0.15,
        proposed_value=0.18,
        rationale="Fees are too high relative to funding.",
        expires_at=(now + timedelta(days=7)).isoformat(),
    )

    store.save_recommendations([recommendation])
    pending = store.list_recommendations(limit=5)
    assert len(pending) == 1
    assert pending[0]["recommendation_id"] == "abc123"

    assert store.update_recommendation_status("abc123", "APPLIED") is True
    stored_rec = store.get_recommendation("abc123")
    assert stored_rec is not None
    assert stored_rec["status"] == "APPLIED"

    scheduled_for = now.isoformat()
    store.record_report("weekly", scheduled_for, "Weekly", "Hello", {"foo": "bar"})
    assert store.has_report_for_slot("weekly", scheduled_for) is True

    assert store.should_emit_alert("kill_switch", 60, now) is True
    store.record_alert("kill_switch", "CRITICAL", "active", {"drawdown": 0.1})
    assert store.should_emit_alert("kill_switch", 60, now) is False

    store.set_runtime_value("telegram_update_offset", "11")
    assert store.get_runtime_value("telegram_update_offset") == "11"

    timestamps = store.get_state_table_timestamps()
    assert "portfolio_stats" in timestamps
    store.close()
