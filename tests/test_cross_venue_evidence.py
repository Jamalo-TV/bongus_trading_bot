from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from bongus.research.cross_venue.evaluation import (
    FIXED_SENSITIVITY_GRID,
    ScenarioMetrics,
    load_evaluation_fixture,
)
from bongus.research.cross_venue.evidence import (
    BootstrapEstimate,
    DailyEvidenceObservation,
    ExclusionMetric,
    RobustnessDiagnostics,
    VerdictEvidence,
    deterministic_block_bootstrap,
    evaluate_research_evidence,
    preregistered_verdict,
    robustness_diagnostics,
    verify_evidence_report,
    write_evidence_report,
)
from bongus.research.cross_venue.schema import CanonicalAsset

FIXTURE = Path(__file__).parent / "fixtures" / "cross_venue" / "evaluation.json"
DAY_NS = 86_400_000_000_000
BASE_DAY_NS = (1_700_000_000_000_000_000 // DAY_NS) * DAY_NS


def _daily(
    day: int,
    asset: CanonicalAsset,
    *,
    net: str = "1",
    capital_days: str = "100",
) -> DailyEvidenceObservation:
    start = BASE_DAY_NS + day * DAY_NS
    return DailyEvidenceObservation(
        event_id=f"daily-{day}-{asset.value}",
        canonical_asset=asset,
        utc_day_start_ns=start,
        net_pnl_usd=Decimal(net),
        total_reserved_capital_days=Decimal(capital_days),
        funding_minus_cost_usd=Decimal("0.8"),
        binance_only_net_pnl_usd=Decimal("0.2"),
        available_time_ns=start + DAY_NS,
    )


def _bootstrap(kind: str, estimate: str, lower: str) -> BootstrapEstimate:
    return BootstrapEstimate(
        block_kind=kind,  # type: ignore[arg-type]
        blocks=20,
        samples=2_000,
        point_simple_annualized_return=Decimal(estimate),
        one_sided_95_lcb=Decimal(lower),
        sample_sha256="a" * 64,
    )


def _diagnostics(*, stress_positive: bool = True) -> RobustnessDiagnostics:
    stress_net = Decimal("1") if stress_positive else Decimal("-1")
    scenarios = tuple(
        ScenarioMetrics(
            scenario_name=case.name,
            outcomes=20,
            total_net_pnl_usd=(Decimal("10") if case.name == "baseline" else stress_net),
            total_reserved_capital_days=Decimal("1000"),
            net_usd_per_reserved_capital_day=stress_net / Decimal("1000"),
            simple_annualized_return=stress_net / Decimal("1000") * Decimal("365"),
        )
        for case in FIXED_SENSITIVITY_GRID
    )
    return RobustnessDiagnostics(
        primary_net_pnl_usd=Decimal("100"),
        binance_only_net_pnl_usd=Decimal("20"),
        funding_minus_cost_usd=Decimal("50"),
        top_five_profit_contribution_fraction=Decimal("0.25"),
        leave_one_symbol_out=tuple(ExclusionMetric(asset.value, 16, Decimal("0.08")) for asset in CanonicalAsset),
        leave_one_month_out=(
            ExclusionMetric("2026-01", 10, Decimal("0.08")),
            ExclusionMetric("2026-02", 10, Decimal("0.07")),
        ),
        sensitivity_metrics=scenarios,
    )


def _evidence(**overrides: object) -> VerdictEvidence:
    values: dict[str, object] = {
        "complete_utc_days": 90,
        "sealed_final_days": 30,
        "storage_sizing_pilot_hours": 48,
        "optimistic_oracle_net_pnl_usd": Decimal("1"),
        "max_drawdown_fraction": Decimal("0.05"),
        "required_depth_multiple": Decimal("5"),
        "dataset_sha256": "b" * 64,
        "input_report_sha256": "c" * 64,
        "dataset_integrity_passed": True,
        "scheduled_cadence_passed": True,
        "decision_anchor_gate_passed": True,
        "funding_reconciliation_passed": True,
        "replay_hash_reproduced": True,
        "policy_frozen_before_oos": True,
        "stress_inputs_complete": True,
        "liquidation_survival_passed": True,
        "secondary_family_correction_applied": True,
    }
    values.update(overrides)
    return VerdictEvidence(**values)  # type: ignore[arg-type]


def test_daily_and_weekly_block_bootstrap_are_deterministic_and_exact() -> None:
    observations = tuple(_daily(day, asset) for day in range(14) for asset in CanonicalAsset)
    first = deterministic_block_bootstrap(observations, block_kind="daily", samples=200)
    second = deterministic_block_bootstrap(observations, block_kind="daily", samples=200)
    weekly = deterministic_block_bootstrap(observations, block_kind="weekly", samples=200)

    assert first == second
    assert first.blocks == 14
    assert weekly.blocks >= 2
    assert first.point_simple_annualized_return == Decimal("0.01") * Decimal("365")
    assert first.one_sided_95_lcb == first.point_simple_annualized_return
    assert weekly.point_simple_annualized_return == first.point_simple_annualized_return


def test_full_stress_matrix_uses_measured_explicit_losses() -> None:
    outcomes, _windows = load_evaluation_fixture(FIXTURE)
    stressed = replace(
        outcomes[-1],
        exit_depth_50pct_loss_usd=Decimal("1"),
        exit_depth_90pct_loss_usd=Decimal("2"),
        underlying_up_30pct_loss_usd=Decimal("3"),
        underlying_down_30pct_loss_usd=Decimal("4"),
        basis_widening_loss_usd=Decimal("5"),
        delisting_loss_usd=Decimal("6"),
        open_interest_cap_loss_usd=Decimal("7"),
        adl_loss_usd=Decimal("8"),
        liquidation_loss_usd=Decimal("9"),
        worse_leg_order_loss_usd=Decimal("10"),
    )
    daily = tuple(_daily(day, asset, net="0.5") for day in range(40) for asset in CanonicalAsset)
    diagnostics = robustness_diagnostics(daily, (stressed,))
    by_name = {value.scenario_name: value for value in diagnostics.sensitivity_metrics}

    assert len(by_name) == 31
    assert by_name["exit_depth_reduced_50pct"].total_net_pnl_usd == Decimal("9.0")
    assert by_name["underlying_move_minus_30pct"].total_net_pnl_usd == Decimal("6.0")
    assert by_name["liquidation"].total_net_pnl_usd == Decimal("1.0")
    assert by_name["worse_leg_execution_order"].total_net_pnl_usd == Decimal("0.0")


def test_preregistered_verdict_state_machine_never_grants_live_authority() -> None:
    diagnostics = _diagnostics()
    viable_daily = _bootstrap("daily", "0.08", "0.06")
    viable_weekly = _bootstrap("weekly", "0.08", "0.055")

    assert (
        preregistered_verdict(
            viable_daily,
            viable_weekly,
            diagnostics,
            _evidence(dataset_integrity_passed=False),
        ).status
        == "invalid_dataset"
    )
    assert (
        preregistered_verdict(
            viable_daily,
            viable_weekly,
            diagnostics,
            _evidence(optimistic_oracle_net_pnl_usd=Decimal("0")),
        ).status
        == "abandon_optimistic_oracle"
    )
    assert (
        preregistered_verdict(viable_daily, viable_weekly, diagnostics, _evidence(complete_utc_days=13)).status
        == "collector_qa_only"
    )
    assert (
        preregistered_verdict(viable_daily, viable_weekly, diagnostics, _evidence(complete_utc_days=89)).status
        == "collecting_forward_oos"
    )
    assert (
        preregistered_verdict(_bootstrap("daily", "0.08", "0"), viable_weekly, diagnostics, _evidence()).status
        == "fail_and_archive"
    )
    assert (
        preregistered_verdict(
            _bootstrap("daily", "0.04", "0.02"), _bootstrap("weekly", "0.04", "0.02"), diagnostics, _evidence()
        ).status
        == "economically_weak_archive"
    )
    assert (
        preregistered_verdict(
            _bootstrap("daily", "0.08", "0.04"), _bootstrap("weekly", "0.08", "0.03"), diagnostics, _evidence()
        ).status
        == "continue_to_180_days"
    )
    assert (
        preregistered_verdict(
            _bootstrap("daily", "0.08", "0.04"),
            _bootstrap("weekly", "0.08", "0.03"),
            diagnostics,
            _evidence(complete_utc_days=180),
        ).status
        == "inconclusive_archive"
    )

    viable = preregistered_verdict(viable_daily, viable_weekly, diagnostics, _evidence())
    assert viable.status == "viable"
    assert viable.grants_live_authority is False
    strong = preregistered_verdict(
        _bootstrap("daily", "0.15", "0.13"),
        _bootstrap("weekly", "0.15", "0.125"),
        diagnostics,
        _evidence(),
    )
    assert strong.status == "strong"
    assert (
        preregistered_verdict(viable_daily, viable_weekly, _diagnostics(stress_positive=False), _evidence()).status
        == "fail_and_archive"
    )


def test_evidence_report_is_hash_bound_and_reproducible(tmp_path: Path) -> None:
    outcomes, windows = load_evaluation_fixture(FIXTURE)
    observations = tuple(_daily(day, CanonicalAsset.BTC) for day in range(13))
    report = evaluate_research_evidence(
        daily_observations=observations,
        outcomes=outcomes,
        windows=windows,
        evidence=_evidence(complete_utc_days=13),
    )
    assert report.verdict.status == "collector_qa_only"
    output = write_evidence_report(report, tmp_path / "evidence.json")
    verified = verify_evidence_report(output)
    assert verified["report_sha256"] == report.report_sha256
    first = output.read_bytes()
    write_evidence_report(report, output)
    assert output.read_bytes() == first
