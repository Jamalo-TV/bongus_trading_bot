"""Regime filters for both legacy and canonical portfolio paths."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from statistics import fmean, median, pstdev
from typing import Callable

from bongus.core.config import (
    REGIME_FILTER_BASIS_ABS_FLOOR,
    REGIME_FILTER_BASIS_WIDENING_MAX,
    REGIME_FILTER_BASIS_ZSCORE_MAX,
    REGIME_FILTER_DEPTH_RATIO_MIN,
    REGIME_FILTER_ENABLED,
    REGIME_FILTER_FUNDING_DISPERSION_MAX,
    REGIME_FILTER_MIN_SAMPLES,
    REGIME_FILTER_PRICE_SHOCK_PCT,
    REGIME_FILTER_VOLUME_SPIKE_MAX,
)


def compute_regime_health(spread_bps: float, realized_volatility: float, data_staleness_seconds: float) -> float:
    spread_penalty = min(1.0, max(0.0, spread_bps / 25.0))
    vol_penalty = min(1.0, max(0.0, realized_volatility * 100.0))
    stale_penalty = min(1.0, max(0.0, data_staleness_seconds / 60.0))
    score = 1.0 - (0.4 * spread_penalty + 0.4 * vol_penalty + 0.2 * stale_penalty)
    return max(0.0, min(1.0, score))


@dataclass(slots=True)
class RegimeDecision:
    allow_entry: bool
    reasons: list[str]
    basis_pct: float | None = None
    basis_zscore: float | None = None
    basis_widening_ratio: float | None = None
    funding_dispersion: float | None = None
    price_shock_pct: float | None = None
    depth_ratio: float | None = None
    volume_spike_ratio: float | None = None


class RegimeFilter:
    def __init__(
        self,
        depth_tracker,
        config_get: Callable[[str], float | bool] | None = None,
        basis_window: int = 60,
        price_window: int = 60,
        depth_window: int = 30,
        funding_window: int = 120,
        volume_window: int = 120,
    ) -> None:
        self._depth = depth_tracker
        self._config_get = config_get
        self._basis_history: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=basis_window))
        self._mark_history: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=price_window))
        self._depth_history: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=depth_window))
        self._funding_history: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=funding_window))
        self._volume_history: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=volume_window))

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

    def on_mark_price(self, symbol: str, mark_price: float, ann_funding: float | None = None) -> None:
        if mark_price > 0.0:
            self._mark_history[symbol].append(float(mark_price))
        if ann_funding is not None:
            self._funding_history[symbol].append(float(ann_funding))

    def on_volume_bar(self, symbol: str, minute_notional_volume: float) -> None:
        if minute_notional_volume >= 0.0:
            self._volume_history[symbol].append(float(minute_notional_volume))

    def evaluate(self, symbol: str) -> RegimeDecision:
        if not bool(self._cfg("regime_filter_enabled", REGIME_FILTER_ENABLED)):
            return RegimeDecision(allow_entry=True, reasons=["disabled"])

        min_samples = int(self._cfg("regime_filter_min_samples", REGIME_FILTER_MIN_SAMPLES))
        basis_abs_floor = float(self._cfg("regime_filter_basis_abs_floor", REGIME_FILTER_BASIS_ABS_FLOOR))
        basis_zscore_max = float(self._cfg("regime_filter_basis_zscore_max", REGIME_FILTER_BASIS_ZSCORE_MAX))
        price_shock_max = float(self._cfg("regime_filter_price_shock_pct", REGIME_FILTER_PRICE_SHOCK_PCT))
        depth_ratio_min = float(self._cfg("regime_filter_depth_ratio_min", REGIME_FILTER_DEPTH_RATIO_MIN))
        funding_dispersion_max = float(
            self._cfg("regime_filter_funding_dispersion_max", REGIME_FILTER_FUNDING_DISPERSION_MAX)
        )
        basis_widening_max = float(
            self._cfg("regime_filter_basis_widening_max", REGIME_FILTER_BASIS_WIDENING_MAX)
        )
        volume_spike_max = float(self._cfg("regime_filter_volume_spike_max", REGIME_FILTER_VOLUME_SPIKE_MAX))

        basis_samples = self._basis_history.get(symbol, deque())
        mark_samples = self._mark_history.get(symbol, deque())
        depth_samples = self._depth_history.get(symbol, deque())
        funding_samples = self._funding_history.get(symbol, deque())
        volume_samples = self._volume_history.get(symbol, deque())

        reasons: list[str] = []
        basis_pct = basis_samples[-1] if basis_samples else None
        basis_zscore = None
        basis_widening_ratio = None
        funding_dispersion = None
        price_shock_pct = None
        depth_ratio = None
        volume_spike_ratio = None

        if len(basis_samples) >= min_samples and basis_pct is not None:
            basis_mean = fmean(basis_samples)
            basis_sigma = pstdev(basis_samples)
            if basis_sigma > 1e-9:
                basis_zscore = abs(basis_pct - basis_mean) / basis_sigma
                if abs(basis_pct) >= basis_abs_floor and basis_zscore > basis_zscore_max:
                    reasons.append(f"basis z-score {basis_zscore:.2f} > {basis_zscore_max:.2f}")
            basis_abs_values = [abs(sample) for sample in basis_samples if abs(sample) > 0.0]
            basis_abs_baseline = median(basis_abs_values) if basis_abs_values else 0.0
            if basis_abs_baseline > 0.0:
                basis_widening_ratio = abs(basis_pct) / basis_abs_baseline
                if basis_widening_ratio > basis_widening_max:
                    reasons.append(f"basis widening {basis_widening_ratio:.2f}x > {basis_widening_max:.2f}x")

        if len(mark_samples) >= min_samples:
            current_mark = mark_samples[-1]
            if current_mark > 0.0:
                price_shock_pct = (max(mark_samples) - min(mark_samples)) / current_mark
                if price_shock_pct > price_shock_max:
                    reasons.append(f"price shock {price_shock_pct:.2%} > {price_shock_max:.2%}")

        if len(depth_samples) >= min_samples:
            depth_baseline = median(depth_samples)
            current_depth = depth_samples[-1]
            if depth_baseline > 0.0:
                depth_ratio = current_depth / depth_baseline
                if depth_ratio < depth_ratio_min:
                    reasons.append(f"depth ratio {depth_ratio:.2f} < {depth_ratio_min:.2f}")

        if len(funding_samples) >= min_samples:
            current_funding = abs(funding_samples[-1])
            funding_abs_values = [abs(sample) for sample in funding_samples if abs(sample) > 0.0]
            funding_baseline = median(funding_abs_values) if funding_abs_values else 0.0
            if funding_baseline > 0.0:
                funding_dispersion = current_funding / funding_baseline
                if funding_dispersion > funding_dispersion_max:
                    reasons.append(f"funding dispersion {funding_dispersion:.2f}x > {funding_dispersion_max:.2f}x")

        if len(volume_samples) >= min_samples:
            current_volume = volume_samples[-1]
            historical_volumes = list(volume_samples)[:-1]
            volume_baseline = fmean(historical_volumes) if historical_volumes else 0.0
            if volume_baseline > 0.0:
                volume_spike_ratio = current_volume / volume_baseline
                if volume_spike_ratio > volume_spike_max:
                    reasons.append(f"volume spike {volume_spike_ratio:.2f}x > {volume_spike_max:.2f}x")

        return RegimeDecision(
            allow_entry=not reasons,
            reasons=reasons,
            basis_pct=basis_pct,
            basis_zscore=basis_zscore,
            basis_widening_ratio=basis_widening_ratio,
            funding_dispersion=funding_dispersion,
            price_shock_pct=price_shock_pct,
            depth_ratio=depth_ratio,
            volume_spike_ratio=volume_spike_ratio,
        )

    def blocked_symbols(self, symbols: list[str]) -> dict[str, RegimeDecision]:
        blocked: dict[str, RegimeDecision] = {}
        for symbol in symbols:
            decision = self.evaluate(symbol)
            if not decision.allow_entry:
                blocked[symbol] = decision
        return blocked
