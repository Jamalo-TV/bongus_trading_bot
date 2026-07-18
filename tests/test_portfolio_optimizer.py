from __future__ import annotations

import numpy as np

from bongus.portfolio.portfolio_optimizer import (
    PortfolioCandidate,
    PortfolioConstraints,
    PortfolioPosition,
    ShadowPortfolioOptimizer,
    governed_leverage,
)


def constraints(**overrides):
    values = {
        "max_pair_gross_usd": 20_000,
        "per_symbol_cap_usd": 5_000,
        "per_cluster_cap_usd": 10_000,
        "per_settlement_cluster_cap_usd": 10_000,
        "per_venue_cap_usd": 20_000,
        "illiquid_tier_cap_usd": 2_500,
        "max_cvar_95_usd": 2_000,
        "max_stress_loss_usd": 5_000,
        "minimum_history": 5,
        "current_static_notional_cap_usd": 5_000,
    }
    values.update(overrides)
    return PortfolioConstraints(**values)


def candidate(symbol, **overrides):
    values = {
        "symbol": symbol,
        "net_ev_lcb_usd": 50,
        "requested_notional_usd": 5_000,
        "executable_capacity_usd": 10_000,
        "confidence": 1.0,
        "cluster": "majors",
        "settlement_cluster": "08:00",
        "liquidity_tier": "high",
        "venue": "binance",
        "beta": 1.0,
        "basis_stress_pct": 0.01,
    }
    values.update(overrides)
    return PortfolioCandidate(**values)


def test_confidence_capacity_and_current_cap_can_only_reduce_size() -> None:
    optimizer = ShadowPortfolioOptimizer(constraints(current_static_notional_cap_usd=2_500))
    result = optimizer.optimize(
        [candidate("BTCUSDT", requested_notional_usd=10_000, executable_capacity_usd=2_000, confidence=0.5)],
        [],
        {"BTCUSDT": [0.001, -0.001, 0.002, -0.001, 0.001]},
    )
    assert result.selected[0].target_notional_usd == 1_600
    assert result.diagnostics["mode"] == "shadow_only"


def test_cluster_and_settlement_caps_reject_crowded_candidate() -> None:
    optimizer = ShadowPortfolioOptimizer(constraints(per_cluster_cap_usd=7_000, per_settlement_cluster_cap_usd=7_000))
    current = [PortfolioPosition("BTCUSDT", 5_000, "majors", "08:00", "high", "binance")]
    result = optimizer.optimize(
        [candidate("ETHUSDT")],
        current,
        {
            "BTCUSDT": [0.01, -0.01, 0.005, -0.004, 0.002],
            "ETHUSDT": [0.009, -0.008, 0.006, -0.005, 0.003],
        },
    )
    assert not result.selected
    assert "factor_cluster_cap" in result.rejected[0].reasons
    assert "settlement_cluster_cap" in result.rejected[0].reasons


def test_missing_history_uses_conservative_correlated_fallback_and_psd_covariance() -> None:
    optimizer = ShadowPortfolioOptimizer(constraints(missing_volatility=0.05, missing_correlation=0.8))
    result = optimizer.optimize([candidate("NEWUSDT", requested_notional_usd=1_000)], [], {})
    assert result.selected[0].history_status == "conservative_missing_history"
    matrix = np.asarray(result.covariance_matrix)
    assert np.min(np.linalg.eigvalsh(matrix)) >= -1e-12
    assert matrix[0, 0] >= 0.05**2 - 1e-12


def test_shrinkage_is_deterministic_and_diversification_reduces_risk() -> None:
    optimizer = ShadowPortfolioOptimizer(constraints(max_cvar_95_usd=10_000, max_stress_loss_usd=10_000))
    histories = {
        "BTCUSDT": [0.02, -0.01, 0.015, -0.02, 0.01, -0.005],
        "ETHUSDT": [0.019, -0.011, 0.014, -0.018, 0.009, -0.006],
        "ALTUSDT": [-0.02, 0.01, -0.015, 0.02, -0.01, 0.005],
    }
    correlated = optimizer.optimize(
        [candidate("BTCUSDT", requested_notional_usd=2_000), candidate("ETHUSDT", requested_notional_usd=2_000)],
        [],
        histories,
    )
    diversified = optimizer.optimize(
        [candidate("BTCUSDT", requested_notional_usd=2_000), candidate("ALTUSDT", requested_notional_usd=2_000, cluster="alts")],
        [],
        histories,
    )
    assert float(diversified.diagnostics["final_cvar_95_usd"]) < float(
        correlated.diagnostics["final_cvar_95_usd"]
    )
    repeat = optimizer.optimize(
        [candidate("BTCUSDT", requested_notional_usd=2_000), candidate("ALTUSDT", requested_notional_usd=2_000, cluster="alts")],
        [],
        histories,
    )
    assert repeat.covariance_matrix == diversified.covariance_matrix


def test_nonpositive_lcb_and_joint_stress_are_hard_rejections() -> None:
    optimizer = ShadowPortfolioOptimizer(constraints(max_stress_loss_usd=100))
    result = optimizer.optimize(
        [candidate("BTCUSDT", net_ev_lcb_usd=-1, requested_notional_usd=2_000, funding_reversal_loss_usd=100)],
        [],
        {"BTCUSDT": [0.001, -0.001, 0.002, -0.001, 0.001]},
    )
    assert "non_positive_net_ev_lcb" in result.rejected[0].reasons
    assert "portfolio_stress_loss" in result.rejected[0].reasons


def test_dynamic_leverage_is_inert_without_independent_review() -> None:
    assert governed_leverage(
        current_leverage=2,
        requested_leverage=4,
        reviewed_maximum_leverage=3,
        dynamic_leverage_enabled=True,
        independent_review_passed=False,
    ) == 2
    assert governed_leverage(
        current_leverage=2,
        requested_leverage=4,
        reviewed_maximum_leverage=3,
        dynamic_leverage_enabled=True,
        independent_review_passed=True,
    ) == 3
