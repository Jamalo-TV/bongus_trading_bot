"""Cross-asset correlation circuit breaker.

Monitors open positions' funding rates and returns a graduated decision:
  CLEAR:     < 50% of positions below EXIT_ANN_FUNDING_THRESHOLD
  HALTED:    ≥ 50% but < 100% below threshold — block new entries
  EMERGENCY: 100% below threshold — exit all positions immediately

States are mutually exclusive and collectively exhaustive.
Empty portfolio always returns CLEAR.
"""

from dataclasses import dataclass, field
from typing import Literal

from config import (
    EXIT_ANN_FUNDING_THRESHOLD,
    BREAKER_HALT_RATIO,
    BREAKER_EMERGENCY_RATIO,
)


@dataclass
class BreakerDecision:
    state: Literal["CLEAR", "HALTED", "EMERGENCY"]
    allow_new_entries: bool
    positions_to_exit: list[str]
    reason: str = ""


class CorrelationBreaker:
    def evaluate(self, open_positions: dict[str, float]) -> BreakerDecision:
        """Evaluate portfolio state.

        Args:
            open_positions: {symbol: current_ann_funding_rate}

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

        negative = [
            s for s, rate in open_positions.items()
            if rate < EXIT_ANN_FUNDING_THRESHOLD
        ]
        ratio = len(negative) / len(open_positions)

        if ratio < BREAKER_HALT_RATIO:
            return BreakerDecision(
                state="CLEAR",
                allow_new_entries=True,
                positions_to_exit=[],
                reason=f"{len(negative)}/{len(open_positions)} positions below threshold",
            )

        if ratio < BREAKER_EMERGENCY_RATIO:
            return BreakerDecision(
                state="HALTED",
                allow_new_entries=False,
                positions_to_exit=[],
                reason=f"{len(negative)}/{len(open_positions)} positions below threshold — halted",
            )

        return BreakerDecision(
            state="EMERGENCY",
            allow_new_entries=False,
            positions_to_exit=list(open_positions.keys()),
            reason="all positions below funding threshold — emergency exit",
        )
