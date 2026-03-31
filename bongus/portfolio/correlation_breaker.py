"""Cross-asset correlation circuit breaker.

Monitors open positions' funding rates and returns a graduated decision:
  CLEAR:        < 33% of positions troubled — entries allowed, no action
  WARNED:       ≥ 33% but < 50% troubled  — entries allowed, warning logged
  HALTED:       ≥ 50% but < 75% troubled  — block new entries, hold positions
  PARTIAL_EXIT: ≥ 75% but < 100% troubled — exit the most-troubled half first
  EMERGENCY:    100% troubled             — exit all positions immediately

States are mutually exclusive and collectively exhaustive.
Empty portfolio always returns CLEAR.

IMPORTANT: This breaker is direction-aware for inverse funding mode:
  - LONG positions: troubled if rate drops below EXIT_ANN_FUNDING_THRESHOLD
  - SHORT positions: troubled if rate rises above 0 (turns positive)
"""

from dataclasses import dataclass
from typing import Literal

from config import (
    BREAKER_EMERGENCY_RATIO,
    BREAKER_HALT_RATIO,
    BREAKER_PARTIAL_RATIO,
    BREAKER_WARN_RATIO,
    EXIT_ANN_FUNDING_THRESHOLD,
    INVERSE_FUNDING_ENABLED,
)


@dataclass
class BreakerDecision:
    state: Literal["CLEAR", "WARNED", "HALTED", "PARTIAL_EXIT", "EMERGENCY"]
    allow_new_entries: bool
    positions_to_exit: list[str]
    reason: str = ""


class CorrelationBreaker:
    def evaluate(
        self,
        open_positions: dict[str, float],
        liquidity_map: dict[str, float] | None = None,
        directions: dict[str, str] | None = None,
    ) -> BreakerDecision:
        """Evaluate portfolio state.

        Args:
            open_positions: {symbol: current_ann_funding_rate}
            liquidity_map: optional {symbol: exit_depth_usd}; used to sort
                exits most-liquid-first during PARTIAL_EXIT and EMERGENCY to
                reduce slippage when order book depth is thin.
            directions: optional {symbol: "long" or "short"} for direction awareness

        Returns:
            BreakerDecision with state, entry permission, and any forced exits.
        """
        if not open_positions:
            return BreakerDecision(
                state="CLEAR",
                allow_new_entries=True,
                positions_to_exit=[],
                reason="no open positions",
            )

        directions = directions or {}

        def _is_troubled(symbol: str, rate: float) -> bool:
            """Check if a position is in trouble based on direction."""
            direction = directions.get(symbol, "long")
            if direction == "short" and INVERSE_FUNDING_ENABLED:
                # SHORT positions collect negative funding; troubled if rate turns positive
                return rate > 0.0
            else:
                # LONG positions collect positive funding; troubled if rate drops below threshold
                return rate < EXIT_ANN_FUNDING_THRESHOLD

        troubled = [s for s, rate in open_positions.items() if _is_troubled(s, rate)]
        ratio = len(troubled) / len(open_positions)

        if ratio < BREAKER_WARN_RATIO:
            return BreakerDecision(
                state="CLEAR",
                allow_new_entries=True,
                positions_to_exit=[],
                reason=f"{len(troubled)}/{len(open_positions)} positions troubled",
            )

        if ratio < BREAKER_HALT_RATIO:
            return BreakerDecision(
                state="WARNED",
                allow_new_entries=True,
                positions_to_exit=[],
                reason=f"{len(troubled)}/{len(open_positions)} positions troubled — warned",
            )

        if ratio < BREAKER_PARTIAL_RATIO:
            return BreakerDecision(
                state="HALTED",
                allow_new_entries=False,
                positions_to_exit=[],
                reason=f"{len(troubled)}/{len(open_positions)} positions troubled — halted",
            )

        if ratio < BREAKER_EMERGENCY_RATIO:
            # Exit the most-troubled half (sorted most-liquid-first to minimise slippage)
            sorted_troubled = sorted(
                troubled,
                key=lambda s: (liquidity_map or {}).get(s, 0.0),
                reverse=True,
            )
            half = max(1, len(sorted_troubled) // 2)
            partial_exits = sorted_troubled[:half]
            return BreakerDecision(
                state="PARTIAL_EXIT",
                allow_new_entries=False,
                positions_to_exit=partial_exits,
                reason=(
                    f"{len(troubled)}/{len(open_positions)} positions troubled "
                    f"— partial exit ({len(partial_exits)} positions)"
                ),
            )

        # EMERGENCY: 100% troubled — exit everything, most-liquid first
        exits = sorted(
            troubled,
            key=lambda s: (liquidity_map or {}).get(s, 0.0),
            reverse=True,
        )
        return BreakerDecision(
            state="EMERGENCY",
            allow_new_entries=False,
            positions_to_exit=exits,
            reason=f"{len(troubled)}/{len(open_positions)} positions troubled — emergency exit",
        )
