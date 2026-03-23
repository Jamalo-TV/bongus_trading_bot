"""Funding rate decay predictor using linear extrapolation over recent samples."""

from collections import deque
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'core')))
from config import FUNDING_PREDICTOR_SAMPLES


class FundingPredictor:
    """Maintains a rolling window of funding rate samples per symbol and
    extrapolates linearly to predict the rate at the next funding snapshot.

    Feed samples via push_sample() on every WS MarkPrice event (~1s cadence).
    """

    def __init__(self, window: int = FUNDING_PREDICTOR_SAMPLES) -> None:
        self._window = window
        self._samples: dict[str, deque[float]] = {}

    def push_sample(self, symbol: str, ann_rate: float) -> None:
        """Add a new annualized rate sample for symbol."""
        if symbol not in self._samples:
            self._samples[symbol] = deque(maxlen=self._window)
        self._samples[symbol].append(ann_rate)

    def predict_rate_at_snapshot(self, symbol: str, minutes_to_snapshot: float) -> float:
        """Predict annualized funding rate at the next snapshot via linear extrapolation.

        Formula: rate[-1] + (rate[-1] - rate[-window]) * (minutes_to_snapshot / window_minutes)
        Falls back to the latest sample if fewer than 2 samples are available.

        Parameters
        ----------
        symbol : str
        minutes_to_snapshot : float
            Minutes until the next funding settlement.

        Returns
        -------
        float
            Predicted annualized rate, or 0.0 if no samples exist.
        """
        samples = self._samples.get(symbol)
        if not samples:
            return 0.0
        if len(samples) < 2:
            return samples[-1]

        latest = samples[-1]
        oldest = samples[0]
        n = len(samples)
        # Each sample is ~1s apart; window covers n seconds ≈ n/60 minutes
        window_minutes = n / 60.0
        if window_minutes <= 0:
            return latest

        slope = (latest - oldest) / window_minutes
        # Cap extrapolation horizon to 5× the observation window to prevent noise
        # amplification (e.g. a 12-sample window covers 0.2 min; extrapolating 480 min
        # ahead without clamping would amplify a 0.01% blip into a 24% predicted swing).
        capped_minutes = min(minutes_to_snapshot, window_minutes * 5)
        predicted = latest + slope * capped_minutes
        # Clamp to ±200% annualized — physically unreachable in practice
        return max(-2.0, min(2.0, predicted))

    def has_data(self, symbol: str) -> bool:
        """Return True if at least one sample exists for symbol."""
        return symbol in self._samples and len(self._samples[symbol]) > 0
