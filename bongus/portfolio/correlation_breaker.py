"""Simple cluster-cap enforcement for correlated trades."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from bongus.core.config import (
    BREAKER_EMERGENCY_RATIO,
    BREAKER_HALT_RATIO,
    BREAKER_PARTIAL_RATIO,
    BREAKER_WARN_RATIO,
    EXIT_ANN_FUNDING_THRESHOLD,
    INVERSE_FUNDING_ENABLED,
)


@dataclass(slots=True)
class CorrelationState:
    gross_exposure_usd: float
    clustered_exposure_usd: dict[str, float]


@dataclass(slots=True)
class CorrelationDecision:
    allowed: bool
    reasons: list[str]


@dataclass(slots=True)
class BreakerDecision:
    state: Literal["CLEAR", "WARNED", "HALTED", "PARTIAL_EXIT", "EMERGENCY"]
    allow_new_entries: bool
    positions_to_exit: list[str]
    reason: str = ""


def evaluate_cluster_caps(
    state: CorrelationState,
    cluster: str,
    incremental_notional_usd: float,
    gross_cap_usd: float,
    cluster_cap_usd: float,
) -> CorrelationDecision:
    reasons: list[str] = []
    if state.gross_exposure_usd + incremental_notional_usd > gross_cap_usd:
        reasons.append("gross_cap")
    if state.clustered_exposure_usd.get(cluster, 0.0) + incremental_notional_usd > cluster_cap_usd:
        reasons.append("cluster_cap")
    return CorrelationDecision(allowed=not reasons, reasons=reasons)


class CorrelationBreaker:
    def evaluate(
        self,
        open_positions: dict[str, float],
        liquidity_map: dict[str, float] | None = None,
        directions: dict[str, str] | None = None,
    ) -> BreakerDecision:
        if not open_positions:
            return BreakerDecision(
                state="CLEAR",
                allow_new_entries=True,
                positions_to_exit=[],
                reason="no open positions",
            )

        directions = directions or {}

        def _is_troubled(symbol: str, rate: float) -> bool:
            direction = directions.get(symbol, "long")
            if direction == "short" and INVERSE_FUNDING_ENABLED:
                return rate > 0.0
            return rate < EXIT_ANN_FUNDING_THRESHOLD

        troubled = [symbol for symbol, rate in open_positions.items() if _is_troubled(symbol, rate)]
        ratio = len(troubled) / len(open_positions)

        if ratio < BREAKER_WARN_RATIO:
            return BreakerDecision("CLEAR", True, [], f"{len(troubled)}/{len(open_positions)} positions troubled")
        if ratio < BREAKER_HALT_RATIO:
            return BreakerDecision("WARNED", True, [], f"{len(troubled)}/{len(open_positions)} positions troubled")
        if ratio < BREAKER_PARTIAL_RATIO:
            return BreakerDecision("HALTED", False, [], f"{len(troubled)}/{len(open_positions)} positions troubled")
        if ratio < BREAKER_EMERGENCY_RATIO:
            sorted_troubled = sorted(
                troubled,
                key=lambda symbol: (liquidity_map or {}).get(symbol, 0.0),
                reverse=True,
            )
            half = max(1, len(sorted_troubled) // 2)
            partial_exits = sorted_troubled[:half]
            return BreakerDecision(
                "PARTIAL_EXIT",
                False,
                partial_exits,
                f"{len(troubled)}/{len(open_positions)} positions troubled",
            )

        exits = sorted(
            troubled,
            key=lambda symbol: (liquidity_map or {}).get(symbol, 0.0),
            reverse=True,
        )
        return BreakerDecision(
            "EMERGENCY",
            False,
            exits,
            f"{len(troubled)}/{len(open_positions)} positions troubled",
        )
