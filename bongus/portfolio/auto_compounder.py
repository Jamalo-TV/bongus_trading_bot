"""
Auto-Compounding: Automatically scales capital allocation based on equity growth.

Phase 3 Enhancement: Recalculates CAPITAL_PER_SLOT_USD based on:
1. Current equity vs high water mark
2. Compound percentage of gains
3. Aggression setting (conservative by default)

This enables exponential growth during winning streaks while protecting
against drawdowns by using the high water mark.
"""

import logging
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'core')))

from config import (
    ACCOUNT_EQUITY_USD,
    AUTO_COMPOUND_ENABLED,
    CAPITAL_PER_SLOT_USD,
    COMPOUND_AGGESSION,
    COMPOUND_HIGH_WATERMARK,
    COMPOUND_MAX_EQUITY_PCT,
    COMPOUND_MIN_EQUITY_PCT,
    COMPOUND_UPDATE_INTERVAL_HOURS,
)

logger = logging.getLogger(__name__)


@dataclass
class CompoundState:
    """Current compounding state."""
    initial_equity: float
    high_water_mark: float
    current_equity: float
    compound_factor: float  # Multiplier for CAPITAL_PER_SLOT_USD
    last_update: str


class AutoCompounder:
    """
    Manages capital scaling based on equity performance.

    Strategy:
    - Track high water mark (peak equity reached)
    - When current equity exceeds high water mark by COMPOUND_MIN_EQUITY_PCT,
      increase capital allocation proportionally
    - Use COMPOUND_AGGESSION to control how much of gains go to capital
    """

    def __init__(
        self,
        initial_equity: float = ACCOUNT_EQUITY_USD,
        enabled: bool = AUTO_COMPOUND_ENABLED,
        update_interval_hours: int = COMPOUND_UPDATE_INTERVAL_HOURS,
        high_watermark_mode: bool = COMPOUND_HIGH_WATERMARK,
        min_gain_pct: float = COMPOUND_MIN_EQUITY_PCT,
        max_increase_pct: float = COMPOUND_MAX_EQUITY_PCT,
        aggression: float = COMPOUND_AGGESSION,
    ):
        self.enabled = enabled
        self.update_interval_hours = update_interval_hours
        self.high_watermark_mode = high_watermark_mode
        self.min_gain_pct = min_gain_pct
        self.max_increase_pct = max_increase_pct
        self.aggression = aggression

        # State
        self._initial_equity = initial_equity
        self._high_water_mark = initial_equity
        self._current_equity = initial_equity
        self._compound_factor = 1.0
        self._last_update: datetime | None = None
        self._base_capital_per_slot = CAPITAL_PER_SLOT_USD

    def get_compound_state(self) -> CompoundState:
        """Get current compounding state."""
        return CompoundState(
            initial_equity=self._initial_equity,
            high_water_mark=self._high_water_mark,
            current_equity=self._current_equity,
            compound_factor=self._compound_factor,
            last_update=self._last_update.isoformat() if self._last_update else "never",
        )

    def get_compounded_capital(self) -> float:
        """Get current CAPITAL_PER_SLOT_USD after compounding."""
        return self._base_capital_per_slot * self._compound_factor

    def update_equity(self, current_equity: float, db_path: str = "state.db") -> dict:
        """
        Update equity and recalculate compound factor if needed.

        Args:
            current_equity: Current account equity from state store
            db_path: Path to state database

        Returns:
            dict with update results
        """
        if not self.enabled:
            return {'updated': False, 'reason': 'disabled'}

        self._current_equity = current_equity

        # Check if update is needed
        now = datetime.now(timezone.utc)
        if self._last_update:
            hours_since_update = (now - self._last_update).total_seconds() / 3600
            if hours_since_update < self.update_interval_hours:
                return {
                    'updated': False,
                    'reason': f'only {hours_since_update:.1f}h since last update',
                    'compound_factor': self._compound_factor,
                }

        # Update high water mark
        if self.high_watermark_mode and current_equity > self._high_water_mark:
            self._high_water_mark = current_equity

        # Calculate new compound factor
        old_factor = self._compound_factor
        gain_pct = (current_equity - self._initial_equity) / self._initial_equity

        # Only compound if above minimum gain threshold
        if gain_pct >= self.min_gain_pct:
            # Calculate new factor: aggression * gain_pct added to factor
            incremental_gain = gain_pct * self.aggression
            new_factor = 1.0 + incremental_gain

            # Cap at maximum increase
            max_factor = 1.0 + self.max_increase_pct
            self._compound_factor = min(new_factor, max_factor)

            # Also ensure we don't go below 1.0
            self._compound_factor = max(1.0, self._compound_factor)
        elif current_equity < self._initial_equity * (1 - self.min_gain_pct):
            # During drawdown, reduce factor proportionally (but never below 1.0)
            drawdown_pct = (self._high_water_mark - current_equity) / self._high_water_mark
            self._compound_factor = max(1.0, 1.0 - drawdown_pct * self.aggression)

        self._last_update = now

        changed = abs(old_factor - self._compound_factor) > 0.001
        return {
            'updated': True,
            'changed': changed,
            'reason': 'scheduled update',
            'old_factor': old_factor,
            'new_factor': self._compound_factor,
            'current_capital': self.get_compounded_capital(),
            'equity': current_equity,
            'gain_pct': gain_pct * 100,
        }

    def update_from_db(self, db_path: str = "state.db") -> dict:
        """Update equity from state database."""
        try:
            conn = sqlite3.connect(db_path, timeout=5)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # Get latest portfolio stats
            cursor.execute("""
                SELECT value FROM stats
                WHERE key = 'equity_usd'
                ORDER BY updated_at DESC
                LIMIT 1
            """)
            row = cursor.fetchone()

            # Also check positions for unrealized P&L
            cursor.execute("""
                SELECT SUM(net_pnl_usd) as total_pnl
                FROM positions
                WHERE status = 'OPEN'
            """)
            pos_row = cursor.fetchone()
            unrealized_pnl = pos_row['total_pnl'] if pos_row and pos_row['total_pnl'] else 0.0

            conn.close()

            if row:
                equity = float(row['value']) + unrealized_pnl
            else:
                # Fallback: estimate from initial equity + trades
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT SUM(net_pnl_usd) as total_pnl
                    FROM trade_history
                """)
                trade_row = cursor.fetchone()
                total_pnl = trade_row['total_pnl'] if trade_row and trade_row['total_pnl'] else 0.0
                equity = self._initial_equity + total_pnl + unrealized_pnl

            return self.update_equity(equity, db_path)

        except Exception as e:
            logger.warning("Could not update from database: %s", e)
            return {'updated': False, 'reason': str(e)}

    def get_status(self) -> dict:
        """Get current status for logging/monitoring."""
        return {
            'enabled': self.enabled,
            'initial_equity': self._initial_equity,
            'high_water_mark': self._high_water_mark,
            'current_equity': self._current_equity,
            'compound_factor': self._compound_factor,
            'current_capital_per_slot': self.get_compounded_capital(),
            'gain_pct': ((self._current_equity - self._initial_equity) / self._initial_equity) * 100,
            'last_update': self._last_update.isoformat() if self._last_update else "never",
        }
