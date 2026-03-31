"""Funding rate TWAP predictor replicating Binance's 8-hour epoch averaging.

Phase 2 Enhancement: Added confidence weighting based on data quality.
"""

import os
import sys
from collections import deque

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'core')))
from config import FUNDING_PREDICTOR_SAMPLES

# Binance computes each funding payment as a TWAP of the Premium Index over the
# entire 8-hour epoch.  Linear slope extrapolation on seconds-long tick windows
# causes large mispredictions: a single spike looks like a secular trend and
# gets projected far into the future, producing entries that never materialise.
# Instead we replicate the exchange's own averaging logic:
#   projected = twap_so_far × elapsed_fraction + latest × remaining_fraction
# where elapsed_fraction = 1 − minutes_to_snapshot / EPOCH_MINUTES.
# This is stable by construction — a single spike is diluted by the historical
# average exactly as it will be on-exchange.

_EPOCH_MINUTES = 480.0  # Binance funding epoch: 8 hours

# Phase 2: Confidence thresholds
MIN_CONFIDENCE_FOR_ENTRY = 0.50  # Minimum 50% confidence to trust prediction
MIN_SAMPLES_FOR_HIGH_CONFIDENCE = 2880  # ~48 minutes of data for 80%+ confidence


class FundingPredictor:
    """Maintains a rolling window of annualized funding rate samples per symbol
    and projects the epoch TWAP to predict the rate at the next settlement.

    Feed samples via push_sample() on every WS MarkPrice event (~1s cadence).
    The default window (FUNDING_PREDICTOR_SAMPLES = 28 800) covers one full
    8-hour epoch at 1 sample/s so the TWAP approximation is tight.

    Phase 2: Added get_confidence() and predict_with_confidence() for
    weighted predictions based on data quality.
    """

    def __init__(self, window: int = FUNDING_PREDICTOR_SAMPLES) -> None:
        self._window = window
        self._samples: dict[str, deque[float]] = {}

    def push_sample(self, symbol: str, ann_rate: float) -> None:
        """Add a new annualized rate sample for symbol."""
        if symbol not in self._samples:
            self._samples[symbol] = deque(maxlen=self._window)
        self._samples[symbol].append(ann_rate)

    def get_confidence(self, symbol: str) -> float:
        """
        Phase 2: Return prediction confidence (0.0 to 1.0) based on data quality.

        Confidence is determined by:
        - Sample count relative to window (more history = higher confidence)
        - Minimum sample threshold for any confidence

        Returns:
            float: Confidence score between 0.0 and 1.0
        """
        samples = self._samples.get(symbol)
        if not samples or len(samples) < 10:
            return 0.0

        # Linear confidence scaling from 10 samples to full window
        sample_ratio = len(samples) / self._window

        # Add stability bonus if we have substantial history
        stability_bonus = 0.0
        if len(samples) >= MIN_SAMPLES_FOR_HIGH_CONFIDENCE:
            # Calculate variance - lower variance = higher confidence
            mean = sum(samples) / len(samples)
            variance = sum((x - mean) ** 2 for x in samples) / len(samples)
            # Normalize variance (assuming typical funding variance)
            stability = max(0, 1.0 - variance / 0.01)  # 1% variance = neutral
            stability_bonus = min(0.1, stability * 0.1)  # Max 10% bonus

        confidence = min(1.0, sample_ratio + stability_bonus)
        return confidence

    def predict_with_confidence(self, symbol: str, minutes_to_snapshot: float) -> tuple[float, float]:
        """
        Phase 2: Predict rate with confidence-weighted adjustment.

        When confidence is low, blend prediction toward current rate.
        When confidence is high, trust the TWAP projection.

        Args:
            symbol: Trading pair symbol
            minutes_to_snapshot: Minutes until funding settlement

        Returns:
            tuple of (predicted_rate, confidence_score)
        """
        confidence = self.get_confidence(symbol)
        raw_prediction = self.predict_rate_at_snapshot(symbol, minutes_to_snapshot)
        current_rate = self._samples.get(symbol, deque(maxlen=1))[-1] if self._samples.get(symbol) else 0.0

        # Weight prediction by confidence
        if confidence >= MIN_CONFIDENCE_FOR_ENTRY:
            # High confidence: trust prediction
            adjusted = raw_prediction
        else:
            # Low confidence: blend toward current rate
            adjusted = raw_prediction * confidence + current_rate * (1 - confidence)

        return adjusted, confidence

    def predict_rate_at_snapshot(self, symbol: str, minutes_to_snapshot: float) -> float:
        """Predict annualized funding rate at the next snapshot via TWAP projection.

        Formula:
            elapsed_fraction = clamp(1 - minutes_to_snapshot / 480, 0, 1)
            projected = twap_so_far * elapsed_fraction + latest * (1 - elapsed_fraction)

        At epoch start (minutes_to_snapshot ≈ 480) the projection equals the
        current rate (no history to bias it).  At epoch end (≈ 0 minutes left)
        the projection equals the full running average — matching what the
        exchange will actually settle at.

        Parameters
        ----------
        symbol : str
        minutes_to_snapshot : float
            Minutes until the next funding settlement.

        Returns
        -------
        float
            Projected annualized rate clamped to ±200%, or 0.0 if no data.
        """
        samples = self._samples.get(symbol)
        if not samples:
            return 0.0
        if len(samples) < 2:
            return samples[-1]

        latest = samples[-1]
        twap_so_far = sum(samples) / len(samples)

        # What fraction of the epoch has already elapsed?
        elapsed_fraction = max(0.0, min(1.0, 1.0 - minutes_to_snapshot / _EPOCH_MINUTES))

        projected = twap_so_far * elapsed_fraction + latest * (1.0 - elapsed_fraction)
        # Clamp to ±200% annualized — physically unreachable in practice
        return max(-2.0, min(2.0, projected))

    def has_data(self, symbol: str) -> bool:
        """Return True if at least one sample exists for symbol."""
        return symbol in self._samples and len(self._samples[symbol]) > 0
