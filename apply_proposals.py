import sys
from bongus.engine.state_store import StateReader, StateWriter
from bongus.monitoring.telegram_alerter import _apply_proposal_to_config

reader = StateReader()
writer = StateWriter()

for p_id in ["weekly_20260519_01_3a4fc6ed", "weekly_20260519_02_b4f738da", "weekly_20260519_03_901bf4f9"]:
    proposal = reader.get_ai_report_proposal(p_id)
    if proposal:
        success, msg = _apply_proposal_to_config(proposal)
        if success:
            writer.update_ai_report_proposal(p_id, status="APPLIED", decision_source="telegram_sim", applied=True)
            print(f"Applied {p_id}: {msg}")
        else:
            print(f"Failed to apply {p_id}: {msg}")
    else:
        print(f"Proposal {p_id} not found")
