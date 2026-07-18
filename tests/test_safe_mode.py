import re
from pathlib import Path

from bongus.engine.safe_mode import (
    describe_safe_mode_flags,
    restore_safe_mode_flags,
    safe_mode_catalog,
)


def test_describe_safe_mode_flags_returns_stable_machine_readable_codes():
    descriptors = describe_safe_mode_flags(["hedge_gap", "risk_limits", "hedge_gap"])

    assert [item["code"] for item in descriptors] == ["hedge_gap", "risk_limits"]
    assert descriptors[0]["scope"] == "symbol"
    assert descriptors[0]["recoverable"] is True
    assert descriptors[0]["next_action"] == "reconcile_or_exit_symbol"
    assert descriptors[1]["scope"] == "global"


def test_unknown_safe_mode_flag_fails_to_operator_review():
    descriptors = describe_safe_mode_flags(["new_unmapped_flag"])

    assert descriptors == [
        {
            "code": "new_unmapped_flag",
            "scope": "global",
            "recoverable": False,
            "next_action": "operator_review",
            "description": "Uncatalogued safe-mode flag.",
        }
    ]


def test_safe_mode_catalog_contains_runtime_flags():
    codes = {item["code"] for item in safe_mode_catalog()}

    assert "startup_manual_review" in codes
    assert "stale_pending_intent" in codes
    assert "execution_bridge" in codes

    trader_source = (
        Path(__file__).parents[1] / "scripts" / "live_trader_v2.py"
    ).read_text(encoding="utf-8")
    runtime_flags = set(
        re.findall(r'_set_safe_mode_flag\(\s*["\']([^"\']+)', trader_source)
    )
    assert runtime_flags <= codes


def test_every_catalogued_and_unknown_safe_mode_round_trips_from_durable_snapshot():
    codes = [str(item["code"]) for item in safe_mode_catalog()]
    snapshot = {
        "safe_mode_codes": [
            *describe_safe_mode_flags(codes),
            {"code": "future_unknown_guard", "scope": "global"},
        ],
        "safe_mode_reason": "display text is not authoritative",
    }

    assert restore_safe_mode_flags(snapshot) == {*codes, "future_unknown_guard"}
    assert restore_safe_mode_flags(
        {"safe_mode_reason": "risk_limits, startup_manual_review, risk_limits"}
    ) == {"risk_limits", "startup_manual_review"}
