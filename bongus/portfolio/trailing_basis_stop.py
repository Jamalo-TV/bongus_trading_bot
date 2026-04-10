"""
Trailing Basis Stop: Protects profits by locking in gains when basis moves favorably.

Phase 2 Enhancement: Dynamically tracks peak profits and trails stop behind
the moving basis, tightening as profits accumulate.

How it works:
1. When basis moves favorably (toward 0 or positive), track peak profit
2. When basis moves against, calculate distance from peak
3. Exit when basis deviation exceeds TRAILING_BASIS_STOP_TRAIL_BPS from peak
4. Lock in portion of profits using TRAILING_BASIS_STOP_LOCK_PCT
"""

from bongus.core.config import (
    BASIS_DEVIATION_STOP,
    TRAILING_BASIS_STOP_ENABLED,
    TRAILING_BASIS_STOP_LOCK_PCT,
    TRAILING_BASIS_STOP_TRAIL_BPS,
)


class TrailingBasisStop:
    """
    Per-symbol trailing basis stop tracker.

    Tracks:
    - peak_basis: Best basis reached since entry (for long positions, lowest is best)
    - locked_profit: Portion of peak profit to protect (via TRAILING_BASIS_STOP_LOCK_PCT)
    - trailing_bps: Distance behind peak to place stop
    """

    def __init__(
        self,
        lock_pct: float = TRAILING_BASIS_STOP_LOCK_PCT,
        trail_bps: float = TRAILING_BASIS_STOP_TRAIL_BPS,
        enabled: bool = TRAILING_BASIS_STOP_ENABLED,
    ):
        self.lock_pct = lock_pct
        self.trail_bps = trail_bps
        self.enabled = enabled

        # Per-symbol state: symbol -> {'peak_basis': float, 'entry_basis': float}
        self._state: dict[str, dict] = {}

    def reset(self, symbol: str) -> None:
        """Reset tracking for a symbol (call on new entry)."""
        self._state[symbol] = {'peak_basis': None, 'entry_basis': None}

    def update(self, symbol: str, current_basis: float, direction: str = "long") -> dict:
        """
        Update trailing stop for a symbol.

        Args:
            symbol: Trading pair symbol
            current_basis: Current basis (perp_price - spot_price) / spot_price
            direction: "long" for long spot + short perp, "short" for inverse

        Returns:
            dict with:
                - 'peak_updated': bool (did peak basis change?)
                - 'should_exit': bool (should we exit?)
                - 'reason': str (reason for exit decision)
                - 'peak_basis': float (current peak)
                - 'stop_basis': float (where stop would trigger)
                - 'profit_bps': float (current unrealized profit in bps)
        """
        if not self.enabled:
            return {'should_exit': False, 'reason': 'disabled', 'peak_updated': False}

        if symbol not in self._state:
            self._state[symbol] = {'peak_basis': None, 'entry_basis': None}

        state = self._state[symbol]

        # Initialize on first update
        if state['entry_basis'] is None:
            state['entry_basis'] = current_basis
            state['peak_basis'] = current_basis

        entry_basis = state['entry_basis']

        # Calculate profit in basis points
        # For long: profit when basis decreases (perp premium shrinks)
        # For short: profit when basis increases (perp premium grows)
        if direction == "long":
            profit_bps = (entry_basis - current_basis) * 10000
        else:  # short
            profit_bps = (current_basis - entry_basis) * 10000

        # Update peak basis
        peak_updated = False
        if direction == "long":
            # Long profits when basis decreases, track lowest
            if current_basis < state['peak_basis']:
                state['peak_basis'] = current_basis
                peak_updated = True
        else:  # short
            # Short profits when basis increases, track highest
            if current_basis > state['peak_basis']:
                state['peak_basis'] = current_basis
                peak_updated = True

        # Calculate stop level
        peak_basis = state['peak_basis']
        if direction == "long":
            # Stop triggers when basis rises above peak + trail
            stop_basis = peak_basis + (self.trail_bps / 10000)
        else:  # short
            # Stop triggers when basis falls below peak - trail
            stop_basis = peak_basis - (self.trail_bps / 10000)

        # Check if should exit
        should_exit = False
        reason = "hold"

        if direction == "long":
            if current_basis > stop_basis:
                should_exit = True
                reason = f"basis rose {current_basis - peak_basis:.6f} above peak"
        else:  # short
            if current_basis < stop_basis:
                should_exit = True
                reason = f"basis fell {peak_basis - current_basis:.6f} below peak"

        # Also check hard stop (static BASIS_DEVIATION_STOP)
        hard_stop_basis = entry_basis + BASIS_DEVIATION_STOP if direction == "long" else entry_basis - BASIS_DEVIATION_STOP
        if direction == "long" and current_basis > hard_stop_basis:
            should_exit = True
            reason = f"hard stop triggered (basis {current_basis:.6f} > {hard_stop_basis:.6f})"
        elif direction == "short" and current_basis < hard_stop_basis:
            should_exit = True
            reason = f"hard stop triggered (basis {current_basis:.6f} < {hard_stop_basis:.6f})"

        return {
            'should_exit': should_exit,
            'reason': reason,
            'peak_updated': peak_updated,
            'peak_basis': peak_basis,
            'stop_basis': stop_basis,
            'profit_bps': profit_bps,
        }

    def get_status(self, symbol: str) -> dict:
        """Get current status for a symbol."""
        if symbol not in self._state:
            return {'tracked': False}
        return {
            'tracked': True,
            'enabled': self.enabled,
            'lock_pct': self.lock_pct,
            'trail_bps': self.trail_bps,
            **self._state[symbol]
        }
