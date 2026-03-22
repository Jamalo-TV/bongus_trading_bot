"""Hot-reloadable configuration manager.

Reads trading parameters from a JSON file and polls for changes.
Falls back to defaults from config.py if the file doesn't exist.
"""

import json
import logging
import os
import threading
from pathlib import Path

from config import (
    ACCOUNT_EQUITY_USD,
    BASIS_DEVIATION_STOP,
    ENTRY_ANN_FUNDING_THRESHOLD,
    ENTRY_PREMIUM_THRESHOLD,
    EXIT_ANN_FUNDING_THRESHOLD,
    EXIT_DISCOUNT_THRESHOLD,
    MAKER_FILL_PROBABILITY,
    MAX_DRAWDOWN_PCT,
    MAX_GROSS_EXPOSURE_USD,
    MAX_LEVERAGE,
    MAX_NOTIONAL_PER_TRADE,
    MAX_VENUE_LATENCY_MS,
    NOTIONAL_PER_TRADE,
    SNIPE_ANN_FUNDING_THRESHOLD,
    SOFT_DRAWDOWN_PCT,
)

logger = logging.getLogger(__name__)

_DEFAULTS = {
    "account_equity_usd": ACCOUNT_EQUITY_USD,
    "max_leverage": MAX_LEVERAGE,
    "entry_ann_funding_threshold": ENTRY_ANN_FUNDING_THRESHOLD,
    "entry_premium_threshold": ENTRY_PREMIUM_THRESHOLD,
    "exit_ann_funding_threshold": EXIT_ANN_FUNDING_THRESHOLD,
    "exit_discount_threshold": EXIT_DISCOUNT_THRESHOLD,
    "basis_deviation_stop": BASIS_DEVIATION_STOP,
    "notional_per_trade": NOTIONAL_PER_TRADE,
    "max_notional_per_trade": MAX_NOTIONAL_PER_TRADE,
    "max_gross_exposure_usd": MAX_GROSS_EXPOSURE_USD,
    "soft_drawdown_pct": SOFT_DRAWDOWN_PCT,
    "max_drawdown_pct": MAX_DRAWDOWN_PCT,
    "max_venue_latency_ms": MAX_VENUE_LATENCY_MS,
    "maker_fill_probability": MAKER_FILL_PROBABILITY,
    "snipe_ann_funding_threshold": SNIPE_ANN_FUNDING_THRESHOLD,
}


class ConfigManager:
    """Thread-safe configuration manager with file-based hot-reload."""

    def __init__(self, config_path: str | Path = "live_config.json", poll_interval: float = 30.0):
        self._path = Path(config_path)
        self._poll_interval = poll_interval
        self._lock = threading.Lock()
        self._values: dict = dict(_DEFAULTS)
        self._last_mtime: float = 0.0
        self._stop_event = threading.Event()
        self._poll_thread: threading.Thread | None = None

        self._try_load()

    def _try_load(self) -> bool:
        if not self._path.exists():
            return False

        try:
            mtime = os.path.getmtime(self._path)
            if mtime <= self._last_mtime:
                return False

            with open(self._path) as f:
                new_values = json.load(f)

            with self._lock:
                changed = {
                    k: (self._values.get(k), v)
                    for k, v in new_values.items()
                    if k in _DEFAULTS and self._values.get(k) != v
                }
                self._values.update({k: v for k, v in new_values.items() if k in _DEFAULTS})
                self._last_mtime = mtime

            if changed:
                for k, (old, new) in changed.items():
                    logger.info("Config reloaded: %s: %s -> %s", k, old, new)

            return bool(changed)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load config from %s: %s", self._path, e)
            return False

    def get(self, key: str) -> float:
        with self._lock:
            return self._values.get(key, _DEFAULTS.get(key, 0.0))

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._values)

    def start_watching(self) -> None:
        if self._poll_thread is not None:
            return

        def _poll():
            while not self._stop_event.wait(self._poll_interval):
                self._try_load()

        self._poll_thread = threading.Thread(target=_poll, daemon=True)
        self._poll_thread.start()
        logger.info("Config watcher started (polling every %.0fs): %s", self._poll_interval, self._path)

    def stop_watching(self) -> None:
        self._stop_event.set()
        if self._poll_thread:
            self._poll_thread.join(timeout=5)
            self._poll_thread = None
