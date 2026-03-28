"""Cross-asset correlation circuit breaker.

Monitors open positions' funding rates and returns a graduated decision:
  CLEAR:     < 50% of positions below EXIT_ANN_FUNDING_THRESHOLD
  HALTED:    ≥ 50% but < 100% below threshold — block new entries
  EMERGENCY: 100% below threshold — exit all positions immediately

States are mutually exclusive and collectively exhaustive.
Empty portfolio always returns CLEAR.

IMPORTANT: This breaker is direction-aware for inverse funding mode:
  - LONG positions: emergency if rate drops below EXIT_ANN_FUNDING_THRESHOLD
  - SHORT positions: emergency if rate rises above 0 (turns positive)
"""

from dataclasses import dataclass, field
from typing import Literal

from config import (
    EXIT_ANN_FUNDING_THRESHOLD,
    BREAKER_HALT_RATIO,
    BREAKER_EMERGENCY_RATIO,
    INVERSE_FUNDING_ENABLED,
)


@dataclass
class BreakerDecision:
    state: Literal["CLEAR", "HALTED", "EMERGENCY"]
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
            liquidity_map: optional {symbol: exit_depth_usd}; when provided,
                EMERGENCY exits are sorted most-liquid-first to reduce slippage
                during a flash crash when book depth evaporates.
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
                # For SHORT positions in inverse mode: bad if funding turns POSITIVE
                # (we want NEGATIVE funding to collect)
                return rate > 0.0
            else:
                # For LONG positions: bad if funding drops below threshold
                return rate < EXIT_ANN_FUNDING_THRESHOLD

        troubled = [s for s, rate in open_positions.items() if _is_troubled(s, rate)]
        ratio = len(troubled) / len(open_positions)

        if ratio < BREAKER_HALT_RATIO:
            return BreakerDecision(
                state="CLEAR",
                allow_new_entries=True,
                positions_to_exit=[],
                reason=f"{len(troubled)}/{len(open_positions)} positions troubled",
            )

        if ratio < BREAKER_EMERGENCY_RATIO:
            return BreakerDecision(
                state="HALTED",
                allow_new_entries=False,
                positions_to_exit=[],
                reason=f"{len(troubled)}/{len(open_positions)} positions troubled — halted",
            )

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
