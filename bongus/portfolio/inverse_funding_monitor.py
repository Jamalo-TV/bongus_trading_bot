"""
Inverse Funding Monitor: Manages short spot + long perp positions for negative funding.

Phase 3 Enhancement: Hardens the inverse funding mode with:
1. Entry quality thresholds
2. Rate decay monitoring
3. Exit triggers for when rates normalize
4. Alerting for significant events

This module works alongside CorrelationBreaker to ensure inverse positions
are entered and exited with proper risk controls.
"""

import logging
from dataclasses import dataclass
from typing import Literal, Optional

from bongus.core.config import (
    INVERSE_FUNDING_ENABLED,
)

logger = logging.getLogger(__name__)

# Inverse funding thresholds
MIN_INVERSE_ENTRY_RATE = -0.05   # Minimum -5% annualized to enter inverse
INVERSE_RATE_WARNING = -0.02     # Alert when rate approaches 0 (less negative)
INVERSE_RATE_CONVERGENCE = -0.01  # Exit when rate converges to -1% (near zero)


@dataclass
class InversePosition:
    """Represents an inverse funding position."""
    symbol: str
    entry_rate: float
    entry_time: str
    current_rate: float = 0.0
    collected_funding: float = 0.0


@dataclass
class InverseDecision:
    """Decision on inverse funding positions."""
    action: Literal["enter", "exit", "hold", "skip"]
    symbol: Optional[str] = None
    reason: str = ""
    confidence: float = 0.0


class InverseFundingMonitor:
    """
    Monitors and manages inverse funding positions.

    Strategy:
    1. Enter when funding rate is sufficiently negative (< MIN_INVERSE_ENTRY_RATE)
    2. Monitor rate convergence toward zero
    3. Exit when rate approaches zero or turns positive
    4. Track collected funding from negative rates
    """

    def __init__(
        self,
        enabled: bool = INVERSE_FUNDING_ENABLED,
        min_entry_rate: float = MIN_INVERSE_ENTRY_RATE,
        warning_rate: float = INVERSE_RATE_WARNING,
        exit_rate: float = INVERSE_RATE_CONVERGENCE,
    ):
        self.enabled = enabled
        self.min_entry_rate = min_entry_rate
        self.warning_rate = warning_rate
        self.exit_rate = exit_rate

        # Track inverse positions
        self._positions: dict[str, InversePosition] = {}

    def should_enter_inverse(self, symbol: str, rate: float, confidence: float = 1.0) -> InverseDecision:
        """
        Determine if we should enter an inverse position.

        Args:
            symbol: Trading pair
            rate: Current annualized funding rate (negative for inverse opportunity)
            confidence: Prediction confidence (0.0 to 1.0)

        Returns:
            InverseDecision with action recommendation
        """
        if not self.enabled:
            return InverseDecision(action="skip", reason="inverse funding disabled")

        # Rate must be sufficiently negative
        if rate >= self.min_entry_rate:
            return InverseDecision(
                action="skip",
                reason=f"rate {rate*100:.3f}% not negative enough (need < {self.min_entry_rate*100:.1f}%)",
                confidence=confidence,
            )

        # Confidence must be reasonable
        if confidence < 0.5:
            return InverseDecision(
                action="skip",
                reason=f"low confidence {confidence:.0%}",
                confidence=confidence,
            )

        # Check if already have position
        if symbol in self._positions:
            return InverseDecision(
                action="hold",
                symbol=symbol,
                reason="already in inverse position",
                confidence=1.0,
            )

        # Calculate confidence-adjusted entry quality
        rate_quality = abs(rate) / abs(self.min_entry_rate)  # How much better than minimum
        entry_score = rate_quality * confidence

        logger.info(
            "Inverse funding opportunity: %s at %.3f%% (score=%.2f)",
            symbol, rate * 100, entry_score
        )

        return InverseDecision(
            action="enter",
            symbol=symbol,
            reason=f"rate {rate*100:.3f}% annualized",
            confidence=min(1.0, entry_score),
        )

    def should_exit_inverse(self, symbol: str, current_rate: float) -> InverseDecision:
        """
        Determine if we should exit an inverse position.

        Args:
            symbol: Trading pair
            current_rate: Current annualized funding rate

        Returns:
            InverseDecision with action recommendation
        """
        if symbol not in self._positions:
            return InverseDecision(action="skip", reason="no inverse position")

        position = self._positions[symbol]
        rate_change = current_rate - position.entry_rate

        # Calculate time-in-position effect
        hours_in_position = 0  # Would need entry time calculation

        # Exit triggers:
        # 1. Rate turned positive (losing money)
        # 2. Rate converged to near zero
        # 3. Rate significantly improved (take profit)

        if current_rate >= 0:
            return InverseDecision(
                action="exit",
                symbol=symbol,
                reason=f"rate turned positive: {current_rate*100:.3f}%",
                confidence=1.0,
            )

        if current_rate >= self.exit_rate:
            return InverseDecision(
                action="exit",
                symbol=symbol,
                reason=f"rate converged to {current_rate*100:.3f}%",
                confidence=0.9,
            )

        # Warning: rate approaching zero
        if current_rate >= self.warning_rate:
            logger.warning(
                "Inverse position at risk: %s rate %.3f%% approaching zero",
                symbol, current_rate * 100
            )
            return InverseDecision(
                action="hold",
                symbol=symbol,
                reason=f"rate warning: {current_rate*100:.3f}% (still collecting)",
                confidence=0.7,
            )

        # Calculate unrealized P&L from rate improvement
        rate_improved = abs(current_rate) > abs(position.entry_rate)
        if rate_improved:
            pnl_pct = (abs(current_rate) - abs(position.entry_rate)) / abs(position.entry_rate) * 100
            return InverseDecision(
                action="exit",
                symbol=symbol,
                reason=f"take profit: rate improved {pnl_pct:.1f}%",
                confidence=0.85,
            )

        return InverseDecision(
            action="hold",
            symbol=symbol,
            reason=f"still collecting: {current_rate*100:.3f}%",
            confidence=1.0,
        )

    def open_position(self, symbol: str, entry_rate: float, entry_time: str) -> None:
        """Record opening of inverse position."""
        self._positions[symbol] = InversePosition(
            symbol=symbol,
            entry_rate=entry_rate,
            entry_time=entry_time,
            current_rate=entry_rate,
        )
        logger.info("Opened inverse position: %s at %.3f%%", symbol, entry_rate * 100)

    def close_position(self, symbol: str) -> Optional[InversePosition]:
        """Record closing of inverse position and return it."""
        if symbol in self._positions:
            position = self._positions.pop(symbol)
            logger.info(
                "Closed inverse position: %s collected %.4f funding",
                symbol, position.collected_funding
            )
            return position
        return None

    def update_rate(self, symbol: str, current_rate: float) -> None:
        """Update current rate for a position."""
        if symbol in self._positions:
            self._positions[symbol].current_rate = current_rate

    def get_positions(self) -> dict[str, InversePosition]:
        """Get all inverse positions."""
        return self._positions.copy()

    def get_status(self) -> dict:
        """Get status summary."""
        if not self._positions:
            return {
                'enabled': self.enabled,
                'positions': 0,
                'total_collected': 0.0,
            }

        total_collected = sum(p.collected_funding for p in self._positions.values())
        return {
            'enabled': self.enabled,
            'positions': len(self._positions),
            'total_collected': total_collected,
            'positions_detail': [
                {
                    'symbol': p.symbol,
                    'entry_rate': p.entry_rate * 100,
                    'current_rate': p.current_rate * 100,
                    'rate_diff': (p.current_rate - p.entry_rate) * 100,
                }
                for p in self._positions.values()
            ]
        }
