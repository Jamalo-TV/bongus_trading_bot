"""Basis-aware regime filter for new entries and rotations.

The goal is to block *new* risk when the market looks temporarily toxic:
  - basis is a large outlier versus recent history
  - recent perp price action is experiencing a short-term shock
  - entry-side liquidity has collapsed relative to recent normal

Existing positions are managed elsewhere; this module is a pre-trade veto.
"""

from collections import defaultdict, deque
from dataclasses import dataclass
from statistics import fmean, median, pstdev
from typing import Callable

from bongus.core.config import (
    REGIME_FILTER_BASIS_ABS_FLOOR,
    REGIME_FILTER_BASIS_ZSCORE_MAX,
    REGIME_FILTER_DEPTH_RATIO_MIN,
    REGIME_FILTER_ENABLED,
    REGIME_FILTER_MIN_SAMPLES,
    REGIME_FILTER_PRICE_SHOCK_PCT,
)


@dataclass
class RegimeDecision:
    allow_entry: bool
    reasons: list[str]
    basis_pct: float | None = None
    basis_zscore: float | None = None
    price_shock_pct: float | None = None
    depth_ratio: float | None = None


class RegimeFilter:
    def __init__(
        self,
        depth_tracker,
        config_get: Callable[[str], float | bool] | None = None,
        basis_window: int = 60,
        price_window: int = 60,
        depth_window: int = 30,
    ) -> None:
        self._depth = depth_tracker
        self._config_get = config_get
        self._basis_history: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=basis_window))
        self._mark_history: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=price_window))
        self._depth_history: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=depth_window))

    def _cfg(self, key: str, default):
        if self._config_get is None:
            return default
        value = self._config_get(key)
        return default if value is None else value

    def on_depth_update(self, symbol: str) -> None:
        basis_pct = self._depth.basis_pct(symbol)
        if basis_pct is not None:
            self._basis_history[symbol].append(float(basis_pct))

        entry_depth = self._depth.get_entry_depth(symbol)
        if entry_depth > 0.0:
            self._depth_history[symbol].append(float(entry_depth))

    def on_mark_price(self, symbol: str, mark_price: float) -> None:
        if mark_price > 0.0:
            self._mark_history[symbol].append(float(mark_price))

    def evaluate(self, symbol: str) -> RegimeDecision:
        if not bool(self._cfg("regime_filter_enabled", REGIME_FILTER_ENABLED)):
            return RegimeDecision(allow_entry=True, reasons=["disabled"])

        min_samples = int(self._cfg("regime_filter_min_samples", REGIME_FILTER_MIN_SAMPLES))
        basis_abs_floor = float(
            self._cfg("regime_filter_basis_abs_floor", REGIME_FILTER_BASIS_ABS_FLOOR)
        )
        basis_zscore_max = float(
            self._cfg("regime_filter_basis_zscore_max", REGIME_FILTER_BASIS_ZSCORE_MAX)
        )
        price_shock_max = float(
            self._cfg("regime_filter_price_shock_pct", REGIME_FILTER_PRICE_SHOCK_PCT)
        )
        depth_ratio_min = float(
            self._cfg("regime_filter_depth_ratio_min", REGIME_FILTER_DEPTH_RATIO_MIN)
        )

        basis_samples = self._basis_history.get(symbol, deque())
        mark_samples = self._mark_history.get(symbol, deque())
        depth_samples = self._depth_history.get(symbol, deque())

        reasons: list[str] = []
        basis_pct = basis_samples[-1] if basis_samples else None
        basis_zscore = None
        price_shock_pct = None
        depth_ratio = None

        if len(basis_samples) >= min_samples and basis_pct is not None:
            basis_mean = fmean(basis_samples)
            basis_sigma = pstdev(basis_samples)
            if basis_sigma > 1e-9:
                basis_zscore = abs(basis_pct - basis_mean) / basis_sigma
                if abs(basis_pct) >= basis_abs_floor and basis_zscore > basis_zscore_max:
                    reasons.append(
                        f"basis z-score {basis_zscore:.2f} > {basis_zscore_max:.2f}"
                    )

        if len(mark_samples) >= min_samples:
            current_mark = mark_samples[-1]
            if current_mark > 0.0:
                price_shock_pct = (max(mark_samples) - min(mark_samples)) / current_mark
                if price_shock_pct > price_shock_max:
                    reasons.append(
                        f"price shock {price_shock_pct:.2%} > {price_shock_max:.2%}"
                    )

        if len(depth_samples) >= min_samples:
            depth_baseline = median(depth_samples)
            current_depth = depth_samples[-1]
            if depth_baseline > 0.0:
                depth_ratio = current_depth / depth_baseline
                if depth_ratio < depth_ratio_min:
                    reasons.append(
                        f"depth ratio {depth_ratio:.2f} < {depth_ratio_min:.2f}"
                    )

        return RegimeDecision(
            allow_entry=not reasons,
            reasons=reasons,
            basis_pct=basis_pct,
            basis_zscore=basis_zscore,
            price_shock_pct=price_shock_pct,
            depth_ratio=depth_ratio,
        )

    def blocked_symbols(self, symbols: list[str]) -> dict[str, RegimeDecision]:
        blocked: dict[str, RegimeDecision] = {}
        for symbol in symbols:
            decision = self.evaluate(symbol)
            if not decision.allow_entry:
                blocked[symbol] = decision
        return blocked
