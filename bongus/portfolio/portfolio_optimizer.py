"""Shadow portfolio optimizer with shrinkage risk and hard concentration caps.

The optimizer consumes lower-bound net value, not headline funding.  It is
deliberately conservative for missing history and never increases the current
static per-symbol cap or leverage unless a separate reviewed promotion proof is
provided.  Nothing in this module mutates live configuration.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping, Sequence

import numpy as np


@dataclass(frozen=True, slots=True)
class PortfolioCandidate:
    symbol: str
    net_ev_lcb_usd: float
    requested_notional_usd: float
    executable_capacity_usd: float
    confidence: float
    cluster: str
    settlement_cluster: str
    liquidity_tier: str
    venue: str
    beta: float = 1.0
    basis_stress_pct: float = 0.02
    funding_reversal_loss_usd: float = 0.0


@dataclass(frozen=True, slots=True)
class PortfolioPosition:
    symbol: str
    notional_usd: float
    cluster: str
    settlement_cluster: str
    liquidity_tier: str
    venue: str
    beta: float = 1.0
    basis_stress_pct: float = 0.02
    funding_reversal_loss_usd: float = 0.0


@dataclass(frozen=True, slots=True)
class PortfolioConstraints:
    max_pair_gross_usd: float
    per_symbol_cap_usd: float
    per_cluster_cap_usd: float
    per_settlement_cluster_cap_usd: float
    per_venue_cap_usd: float
    illiquid_tier_cap_usd: float
    max_cvar_95_usd: float
    max_stress_loss_usd: float
    minimum_history: int = 30
    covariance_shrinkage: float = 0.35
    missing_volatility: float = 0.04
    missing_correlation: float = 0.75
    current_static_notional_cap_usd: float | None = None


@dataclass(frozen=True, slots=True)
class CandidateAssessment:
    symbol: str
    accepted: bool
    target_notional_usd: float
    reasons: tuple[str, ...]
    marginal_net_ev_lcb_usd: float
    projected_volatility_usd: float
    projected_cvar_95_usd: float
    projected_stress_loss_usd: float
    history_status: str


@dataclass(frozen=True, slots=True)
class PortfolioOptimization:
    selected: tuple[CandidateAssessment, ...]
    rejected: tuple[CandidateAssessment, ...]
    covariance_symbols: tuple[str, ...]
    covariance_matrix: tuple[tuple[float, ...], ...]
    diagnostics: dict[str, float | int | str] = field(default_factory=dict)


def governed_leverage(
    *,
    current_leverage: float,
    requested_leverage: float,
    reviewed_maximum_leverage: float,
    dynamic_leverage_enabled: bool,
    independent_review_passed: bool,
) -> float:
    """Return leverage without silently promoting a research recommendation."""

    if current_leverage <= 0 or requested_leverage <= 0 or reviewed_maximum_leverage <= 0:
        raise ValueError("leverage inputs must be positive")
    if not dynamic_leverage_enabled or not independent_review_passed:
        return current_leverage
    return min(requested_leverage, reviewed_maximum_leverage)


class ShadowPortfolioOptimizer:
    def __init__(self, constraints: PortfolioConstraints) -> None:
        self.constraints = constraints
        self._validate_constraints()

    def optimize(
        self,
        candidates: Sequence[PortfolioCandidate],
        current_positions: Sequence[PortfolioPosition],
        point_in_time_returns: Mapping[str, Sequence[float]],
    ) -> PortfolioOptimization:
        candidate_by_symbol = {item.symbol.upper(): item for item in candidates}
        if len(candidate_by_symbol) != len(candidates):
            raise ValueError("candidate symbols must be unique")
        position_by_symbol = {item.symbol.upper(): item for item in current_positions}
        all_symbols = tuple(sorted(set(candidate_by_symbol) | set(position_by_symbol)))
        covariance, statuses = self._covariance(all_symbols, point_in_time_returns)
        index = {symbol: idx for idx, symbol in enumerate(all_symbols)}

        exposures: dict[str, float] = {
            symbol: max(0.0, float(position.notional_usd))
            for symbol, position in position_by_symbol.items()
        }
        descriptors: dict[str, PortfolioPosition | PortfolioCandidate] = {
            **position_by_symbol,
            **{symbol: candidate for symbol, candidate in candidate_by_symbol.items() if symbol not in position_by_symbol},
        }
        selected: list[CandidateAssessment] = []
        rejected: list[CandidateAssessment] = []

        # Lower-bound dollars per requested capital is the primary ordering.
        ordered = sorted(
            candidates,
            key=lambda item: (
                item.net_ev_lcb_usd / max(item.requested_notional_usd, 1e-9),
                item.net_ev_lcb_usd,
                item.symbol,
            ),
            reverse=True,
        )
        for candidate in ordered:
            symbol = candidate.symbol.upper()
            target = self._governed_size(candidate)
            proposed = dict(exposures)
            proposed[symbol] = max(proposed.get(symbol, 0.0), target)
            reasons = self._hard_cap_reasons(proposed, descriptors)
            volatility, cvar = self._risk(proposed, covariance, index)
            stress = self._stress_loss(proposed, descriptors)
            if cvar > self.constraints.max_cvar_95_usd:
                reasons.append("portfolio_cvar")
            if stress > self.constraints.max_stress_loss_usd:
                reasons.append("portfolio_stress_loss")
            if candidate.net_ev_lcb_usd <= 0:
                reasons.append("non_positive_net_ev_lcb")
            if target <= 0:
                reasons.append("zero_governed_capacity")

            assessment = CandidateAssessment(
                symbol=symbol,
                accepted=not reasons,
                target_notional_usd=target,
                reasons=tuple(dict.fromkeys(reasons)),
                marginal_net_ev_lcb_usd=float(candidate.net_ev_lcb_usd),
                projected_volatility_usd=volatility,
                projected_cvar_95_usd=cvar,
                projected_stress_loss_usd=stress,
                history_status=statuses[symbol],
            )
            if reasons:
                rejected.append(assessment)
            else:
                selected.append(assessment)
                exposures = proposed

        final_vol, final_cvar = self._risk(exposures, covariance, index)
        return PortfolioOptimization(
            selected=tuple(selected),
            rejected=tuple(rejected),
            covariance_symbols=all_symbols,
            covariance_matrix=tuple(tuple(float(value) for value in row) for row in covariance),
            diagnostics={
                "selected_count": len(selected),
                "rejected_count": len(rejected),
                "final_pair_gross_usd": sum(exposures.values()),
                "final_volatility_usd": final_vol,
                "final_cvar_95_usd": final_cvar,
                "mode": "shadow_only",
            },
        )

    def _governed_size(self, candidate: PortfolioCandidate) -> float:
        confidence = min(1.0, max(0.0, float(candidate.confidence)))
        static_cap = self.constraints.current_static_notional_cap_usd
        if static_cap is None:
            static_cap = self.constraints.per_symbol_cap_usd
        # Confidence may reduce a request, never enlarge it.  Capacity gets a
        # 20% safety haircut until route-cost calibration has enough samples.
        return max(
            0.0,
            min(
                float(candidate.requested_notional_usd) * confidence,
                float(candidate.executable_capacity_usd) * 0.80,
                float(self.constraints.per_symbol_cap_usd),
                float(static_cap),
            ),
        )

    def _covariance(
        self,
        symbols: tuple[str, ...],
        histories: Mapping[str, Sequence[float]],
    ) -> tuple[np.ndarray, dict[str, str]]:
        if not symbols:
            return np.zeros((0, 0), dtype=float), {}
        valid: dict[str, np.ndarray] = {}
        statuses: dict[str, str] = {}
        for symbol in symbols:
            values = np.asarray(histories.get(symbol, ()), dtype=float)
            values = values[np.isfinite(values)]
            if len(values) >= self.constraints.minimum_history:
                valid[symbol] = values
                statuses[symbol] = "observed"
            else:
                statuses[symbol] = "conservative_missing_history"

        observed_symbols = tuple(symbol for symbol in symbols if symbol in valid)
        observed_cov = np.zeros((0, 0), dtype=float)
        if observed_symbols:
            aligned_length = min(len(valid[symbol]) for symbol in observed_symbols)
            matrix = np.vstack([valid[symbol][-aligned_length:] for symbol in observed_symbols])
            if len(observed_symbols) == 1:
                variance = float(np.var(matrix[0], ddof=1)) if aligned_length > 1 else 0.0
                observed_cov = np.array([[variance]], dtype=float)
            else:
                observed_cov = np.asarray(np.cov(matrix, ddof=1), dtype=float)
            diagonal = np.diag(np.diag(observed_cov))
            shrinkage = self.constraints.covariance_shrinkage
            observed_cov = (1.0 - shrinkage) * observed_cov + shrinkage * diagonal

        result = np.zeros((len(symbols), len(symbols)), dtype=float)
        missing_var = self.constraints.missing_volatility**2
        observed_index = {symbol: idx for idx, symbol in enumerate(observed_symbols)}
        for i, first in enumerate(symbols):
            for j, second in enumerate(symbols):
                if first in observed_index and second in observed_index:
                    result[i, j] = observed_cov[observed_index[first], observed_index[second]]
                elif i == j:
                    result[i, j] = missing_var
                else:
                    first_var = result[i, i] if result[i, i] > 0 else missing_var
                    second_var = result[j, j] if result[j, j] > 0 else missing_var
                    result[i, j] = self.constraints.missing_correlation * math.sqrt(first_var * second_var)
        # Fill cross terms after all diagonals are known and force numerical PSD.
        for i in range(len(symbols)):
            for j in range(i + 1, len(symbols)):
                if symbols[i] not in observed_index or symbols[j] not in observed_index:
                    value = self.constraints.missing_correlation * math.sqrt(result[i, i] * result[j, j])
                    result[i, j] = result[j, i] = value
        result = (result + result.T) / 2.0
        eigenvalues, eigenvectors = np.linalg.eigh(result)
        eigenvalues = np.maximum(eigenvalues, 1e-12)
        result = eigenvectors @ np.diag(eigenvalues) @ eigenvectors.T
        return result, statuses

    @staticmethod
    def _risk(
        exposures: Mapping[str, float],
        covariance: np.ndarray,
        index: Mapping[str, int],
    ) -> tuple[float, float]:
        if not exposures or covariance.size == 0:
            return 0.0, 0.0
        vector = np.zeros(len(index), dtype=float)
        for symbol, notional in exposures.items():
            if symbol in index:
                vector[index[symbol]] = float(notional)
        variance = max(0.0, float(vector @ covariance @ vector))
        volatility = math.sqrt(variance)
        # Expected shortfall for a zero-mean Gaussian at 95%: phi(z)/(1-a).
        cvar = 2.0627128075 * volatility
        return volatility, cvar

    def _hard_cap_reasons(
        self,
        exposures: Mapping[str, float],
        descriptors: Mapping[str, PortfolioPosition | PortfolioCandidate],
    ) -> list[str]:
        reasons: list[str] = []
        if sum(exposures.values()) > self.constraints.max_pair_gross_usd + 1e-9:
            reasons.append("pair_gross_cap")
        grouped: dict[str, dict[str, float]] = {
            "cluster": {},
            "settlement": {},
            "venue": {},
            "liquidity": {},
        }
        for symbol, notional in exposures.items():
            descriptor = descriptors[symbol]
            for group, key in (
                ("cluster", descriptor.cluster),
                ("settlement", descriptor.settlement_cluster),
                ("venue", descriptor.venue),
                ("liquidity", descriptor.liquidity_tier),
            ):
                grouped[group][key] = grouped[group].get(key, 0.0) + notional
        if any(value > self.constraints.per_cluster_cap_usd + 1e-9 for value in grouped["cluster"].values()):
            reasons.append("factor_cluster_cap")
        if any(
            value > self.constraints.per_settlement_cluster_cap_usd + 1e-9
            for value in grouped["settlement"].values()
        ):
            reasons.append("settlement_cluster_cap")
        if any(value > self.constraints.per_venue_cap_usd + 1e-9 for value in grouped["venue"].values()):
            reasons.append("venue_cap")
        illiquid = sum(
            value for tier, value in grouped["liquidity"].items() if tier.strip().lower() in {"low", "illiquid"}
        )
        if illiquid > self.constraints.illiquid_tier_cap_usd + 1e-9:
            reasons.append("illiquid_tier_cap")
        return reasons

    @staticmethod
    def _stress_loss(
        exposures: Mapping[str, float],
        descriptors: Mapping[str, PortfolioPosition | PortfolioCandidate],
    ) -> float:
        # Joint stress: a 5% common crypto factor, candidate-specific basis
        # shock, and loss of the expected funding payment.
        beta_loss = abs(sum(exposures[symbol] * descriptors[symbol].beta for symbol in exposures)) * 0.05
        basis_loss = sum(exposures[symbol] * abs(descriptors[symbol].basis_stress_pct) for symbol in exposures)
        funding_loss = sum(abs(descriptors[symbol].funding_reversal_loss_usd) for symbol in exposures)
        return beta_loss + basis_loss + funding_loss

    def _validate_constraints(self) -> None:
        values = (
            self.constraints.max_pair_gross_usd,
            self.constraints.per_symbol_cap_usd,
            self.constraints.per_cluster_cap_usd,
            self.constraints.per_settlement_cluster_cap_usd,
            self.constraints.per_venue_cap_usd,
            self.constraints.illiquid_tier_cap_usd,
            self.constraints.max_cvar_95_usd,
            self.constraints.max_stress_loss_usd,
            self.constraints.missing_volatility,
        )
        if any(value <= 0 for value in values):
            raise ValueError("portfolio constraints must be positive")
        if not 0.0 <= self.constraints.covariance_shrinkage <= 1.0:
            raise ValueError("covariance_shrinkage must be in [0, 1]")
        if not 0.0 <= self.constraints.missing_correlation <= 1.0:
            raise ValueError("missing_correlation must be in [0, 1]")

