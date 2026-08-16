from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from bongus.research.cross_venue.evaluation import (
    FIXED_SENSITIVITY_GRID,
    PREDECLARED_UNIVERSE,
    EvaluationProtocol,
    PurgedWalkForwardEvaluator,
    load_evaluation_fixture,
    verify_evaluation_report,
    write_evaluation_report,
)
from bongus.research.cross_venue.schema import CanonicalAsset

FIXTURE = Path(__file__).parent / "fixtures" / "cross_venue" / "evaluation.json"
PREREGISTRATION = Path(__file__).parents[1] / "research" / "experiments" / "binance_hyperliquid_v1.json"


def test_protocol_universe_and_sensitivity_grid_are_frozen_and_preregistered() -> None:
    protocol = EvaluationProtocol()
    preregistration = json.loads(PREREGISTRATION.read_text(encoding="utf-8"))
    assert tuple(asset.value for asset in PREDECLARED_UNIVERSE) == (
        "BTC",
        "ETH",
        "SOL",
        "XRP",
        "DOGE",
    )
    assert preregistration["universe"] == [asset.value for asset in protocol.universe]
    assert preregistration["sensitivity_grid"] == [case.name for case in FIXED_SENSITIVITY_GRID]
    assert preregistration["null_hypothesis"]
    assert len(preregistration["stop_rules"]) >= 5
    assert len(protocol.preregistration_sha256) == 64
    with pytest.raises(ValueError, match="universe is immutable"):
        replace(protocol, universe=(CanonicalAsset.BTC,))
    with pytest.raises(ValueError, match="sensitivity grid is immutable"):
        replace(protocol, sensitivity_grid=FIXED_SENSITIVITY_GRID[:-1])
    with pytest.raises(ValueError, match="purge and embargo"):
        replace(protocol, purge_days=0)


def test_purged_walk_forward_uses_only_finalized_oos_and_total_capital_days() -> None:
    outcomes, windows = load_evaluation_fixture(FIXTURE)
    report = PurgedWalkForwardEvaluator().evaluate(outcomes, windows)
    window = report.windows[0]
    assert window.train_candidates == 2
    assert window.purged_train_outcomes == 1
    assert window.retained_train_outcomes == 1
    assert window.out_of_sample_outcomes == 1

    by_name = {metric.scenario_name: metric for metric in window.scenario_metrics}
    baseline = by_name["baseline"]
    assert baseline.total_net_pnl_usd == Decimal("10.0")
    assert baseline.total_reserved_capital_days == Decimal("4200")
    assert baseline.net_usd_per_reserved_capital_day == Decimal("10") / Decimal("4200")
    assert baseline.simple_annualized_return == Decimal("10") / Decimal("4200") * Decimal("365")
    assert by_name["fees_plus_5bp"].total_net_pnl_usd == Decimal("8.750")
    assert by_name["funding_sign_reversal"].total_net_pnl_usd == Decimal("-10.0")
    assert by_name["usdc_usdt_deviation_5pct"].total_net_pnl_usd == Decimal("7.00")


def test_quality_flags_and_duplicate_outcomes_fail_closed() -> None:
    outcomes, windows = load_evaluation_fixture(FIXTURE)
    with pytest.raises(ValueError, match="quality-flagged"):
        PurgedWalkForwardEvaluator().evaluate((replace(outcomes[0], quality_flags=("gap",)),), windows)
    with pytest.raises(ValueError, match="duplicate outcome"):
        PurgedWalkForwardEvaluator().evaluate((outcomes[0], outcomes[0]), windows)


def test_report_is_atomic_deterministic_and_hash_verified(tmp_path: Path) -> None:
    outcomes, windows = load_evaluation_fixture(FIXTURE)
    report = PurgedWalkForwardEvaluator().evaluate(outcomes, windows)
    output = write_evaluation_report(report, tmp_path / "report.json")
    verified = verify_evaluation_report(output)
    assert verified["report_sha256"] == report.report_sha256
    assert verified["preregistration_sha256"] == report.preregistration_sha256
    first_bytes = output.read_bytes()
    write_evaluation_report(report, output)
    assert output.read_bytes() == first_bytes

    tampered = json.loads(output.read_text(encoding="utf-8"))
    tampered["unique_outcomes"] = 999
    output.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_evaluation_report(output)
