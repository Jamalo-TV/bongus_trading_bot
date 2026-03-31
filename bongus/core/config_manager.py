"""Hot-reloadable configuration manager.

Reads trading parameters from a JSON file and polls for changes.
Falls back to defaults from config.py if the file doesn't exist.
"""

import json
import logging
import os
import threading
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from bongus.core.config import (
    ACCOUNT_EQUITY_USD,
    ADAPTIVE_RULES_PAPER_ONLY,
    ADAPTIVE_THRESHOLDS_ENABLED,
    AI_REPORT_AGENT_ENABLED,
    BASIS_DEVIATION_STOP,
    COOLDOWN_EMERGENCY_MINUTES,
    COOLDOWN_ENABLED,
    COOLDOWN_HALTED_MINUTES,
    COOLDOWN_PARTIAL_EXIT_MINUTES,
    COOLDOWN_SYMBOL_MINUTES,
    DAILY_PNL_SUMMARY_HOUR_UTC,
    DAILY_PNL_SUMMARY_MINUTE_UTC,
    DATA_RETENTION_DAYS,
    ENTRY_ANN_FUNDING_THRESHOLD,
    ENTRY_PREMIUM_THRESHOLD,
    EXIT_ANN_FUNDING_THRESHOLD,
    EXIT_DISCOUNT_THRESHOLD,
    HEALTH_ALERT_ZSCORE,
    HEALTH_MONITOR_ENABLED,
    HEALTH_SAFE_MODE_ZSCORE,
    HEALTH_SAMPLE_RETENTION_DAYS,
    HEARTBEAT_INTERVAL_SECONDS,
    HEARTBEAT_MISS_THRESHOLD,
    MAKER_FILL_PROBABILITY,
    MARKET_SAMPLE_RETENTION_DAYS,
    MAX_DRAWDOWN_PCT,
    MAX_GROSS_EXPOSURE_USD,
    MAX_LEVERAGE,
    MAX_NOTIONAL_PER_TRADE,
    MAX_VENUE_LATENCY_MS,
    NOTIONAL_PER_TRADE,
    LOSS_STREAK_ENTRY_MULTIPLIER,
    LOSS_STREAK_NOTIONAL_SCALE,
    LOSS_STREAK_TRIGGER,
    REGIME_FILTER_BASIS_ABS_FLOOR,
    REGIME_FILTER_BASIS_WIDENING_MAX,
    REGIME_FILTER_BASIS_ZSCORE_MAX,
    REGIME_FILTER_DEPTH_RATIO_MIN,
    REGIME_FILTER_ENABLED,
    REGIME_FILTER_FUNDING_DISPERSION_MAX,
    REGIME_FILTER_MIN_SAMPLES,
    REGIME_FILTER_PRICE_SHOCK_PCT,
    REGIME_FILTER_VOLUME_SPIKE_MAX,
    SENTIMENT_ENABLED,
    SNIPE_ANN_FUNDING_THRESHOLD,
    SOFT_DRAWDOWN_PCT,
    WIN_STREAK_RESET,
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
    "regime_filter_enabled": REGIME_FILTER_ENABLED,
    "regime_filter_min_samples": REGIME_FILTER_MIN_SAMPLES,
    "regime_filter_basis_zscore_max": REGIME_FILTER_BASIS_ZSCORE_MAX,
    "regime_filter_basis_abs_floor": REGIME_FILTER_BASIS_ABS_FLOOR,
    "regime_filter_price_shock_pct": REGIME_FILTER_PRICE_SHOCK_PCT,
    "regime_filter_depth_ratio_min": REGIME_FILTER_DEPTH_RATIO_MIN,
    "regime_filter_funding_dispersion_max": REGIME_FILTER_FUNDING_DISPERSION_MAX,
    "regime_filter_basis_widening_max": REGIME_FILTER_BASIS_WIDENING_MAX,
    "regime_filter_volume_spike_max": REGIME_FILTER_VOLUME_SPIKE_MAX,
    "cooldown_enabled": COOLDOWN_ENABLED,
    "cooldown_halted_minutes": COOLDOWN_HALTED_MINUTES,
    "cooldown_partial_exit_minutes": COOLDOWN_PARTIAL_EXIT_MINUTES,
    "cooldown_emergency_minutes": COOLDOWN_EMERGENCY_MINUTES,
    "cooldown_symbol_minutes": COOLDOWN_SYMBOL_MINUTES,
    "sentiment_enabled": SENTIMENT_ENABLED,
    "heartbeat_interval_seconds": HEARTBEAT_INTERVAL_SECONDS,
    "heartbeat_miss_threshold": HEARTBEAT_MISS_THRESHOLD,
    "data_retention_days": DATA_RETENTION_DAYS,
    "market_sample_retention_days": MARKET_SAMPLE_RETENTION_DAYS,
    "health_sample_retention_days": HEALTH_SAMPLE_RETENTION_DAYS,
    "adaptive_thresholds_enabled": ADAPTIVE_THRESHOLDS_ENABLED,
    "health_monitor_enabled": HEALTH_MONITOR_ENABLED,
    "ai_report_agent_enabled": AI_REPORT_AGENT_ENABLED,
    "adaptive_rules_paper_only": ADAPTIVE_RULES_PAPER_ONLY,
    "health_alert_zscore": HEALTH_ALERT_ZSCORE,
    "health_safe_mode_zscore": HEALTH_SAFE_MODE_ZSCORE,
    "loss_streak_trigger": LOSS_STREAK_TRIGGER,
    "win_streak_reset": WIN_STREAK_RESET,
    "loss_streak_notional_scale": LOSS_STREAK_NOTIONAL_SCALE,
    "loss_streak_entry_multiplier": LOSS_STREAK_ENTRY_MULTIPLIER,
    "daily_pnl_summary_hour_utc": DAILY_PNL_SUMMARY_HOUR_UTC,
    "daily_pnl_summary_minute_utc": DAILY_PNL_SUMMARY_MINUTE_UTC,
}


class LiveConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_equity_usd: float | None = None
    max_leverage: float | None = None
    entry_ann_funding_threshold: float | None = None
    entry_premium_threshold: float | None = None
    exit_ann_funding_threshold: float | None = None
    exit_discount_threshold: float | None = None
    basis_deviation_stop: float | None = None
    notional_per_trade: float | None = None
    max_notional_per_trade: float | None = None
    max_gross_exposure_usd: float | None = None
    soft_drawdown_pct: float | None = None
    max_drawdown_pct: float | None = None
    max_venue_latency_ms: float | None = None
    maker_fill_probability: float | None = None
    snipe_ann_funding_threshold: float | None = None
    regime_filter_enabled: bool | None = None
    regime_filter_min_samples: int | None = None
    regime_filter_basis_zscore_max: float | None = None
    regime_filter_basis_abs_floor: float | None = None
    regime_filter_price_shock_pct: float | None = None
    regime_filter_depth_ratio_min: float | None = None
    regime_filter_funding_dispersion_max: float | None = None
    regime_filter_basis_widening_max: float | None = None
    regime_filter_volume_spike_max: float | None = None
    cooldown_enabled: bool | None = None
    cooldown_halted_minutes: float | None = None
    cooldown_partial_exit_minutes: float | None = None
    cooldown_emergency_minutes: float | None = None
    cooldown_symbol_minutes: float | None = None
    sentiment_enabled: bool | None = None
    heartbeat_interval_seconds: int | None = None
    heartbeat_miss_threshold: int | None = None
    data_retention_days: int | None = None
    market_sample_retention_days: int | None = None
    health_sample_retention_days: int | None = None
    adaptive_thresholds_enabled: bool | None = None
    health_monitor_enabled: bool | None = None
    ai_report_agent_enabled: bool | None = None
    adaptive_rules_paper_only: bool | None = None
    health_alert_zscore: float | None = None
    health_safe_mode_zscore: float | None = None
    loss_streak_trigger: int | None = None
    win_streak_reset: int | None = None
    loss_streak_notional_scale: float | None = None
    loss_streak_entry_multiplier: float | None = None
    daily_pnl_summary_hour_utc: int | None = None
    daily_pnl_summary_minute_utc: int | None = None


def validate_live_config(values: dict) -> dict:
    model = LiveConfigModel.model_validate(values)
    return model.model_dump(exclude_none=True)


class ConfigManager:
    """Thread-safe configuration manager with file-based hot-reload."""

    def __init__(
        self,
        config_path: str | Path = "live_config.json",
        poll_interval: float = 30.0,
        on_validation_error=None,
        on_reload=None,
    ):
        self._path = Path(config_path)
        self._poll_interval = poll_interval
        self._lock = threading.Lock()
        self._values: dict = dict(_DEFAULTS)
        self._last_mtime: float = 0.0
        self._stop_event = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._on_validation_error = on_validation_error
        self._on_reload = on_reload
        self._last_error: str = ""

        self._try_load()

    def _try_load(self) -> bool:
        if not self._path.exists():
            return False

        try:
            mtime = os.path.getmtime(self._path)
            if mtime <= self._last_mtime:
                return False

            with open(self._path) as f:
                raw_values = json.load(f)
            new_values = validate_live_config(raw_values)

            with self._lock:
                changed = {
                    k: (self._values.get(k), v)
                    for k, v in new_values.items()
                    if k in _DEFAULTS and self._values.get(k) != v
                }
                self._values.update({k: v for k, v in new_values.items() if k in _DEFAULTS})
                self._last_mtime = mtime
                self._last_error = ""

            if changed:
                for k, (old, new) in changed.items():
                    logger.info("Config reloaded: %s: %s -> %s", k, old, new)
                if self._on_reload is not None:
                    try:
                        self._on_reload(changed, self.snapshot())
                    except Exception as exc:
                        logger.warning("Config reload callback failed: %s", exc)

            return bool(changed)
        except (ValidationError, json.JSONDecodeError, OSError) as e:
            self._last_error = str(e)
            logger.warning("Failed to load config from %s: %s", self._path, e)
            if self._on_validation_error is not None:
                try:
                    self._on_validation_error(str(e))
                except Exception as exc:
                    logger.warning("Config validation callback failed: %s", exc)
            return False

    def get(self, key: str) -> float | bool:
        with self._lock:
            return self._values.get(key, _DEFAULTS.get(key, 0.0))

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._values)

    @property
    def last_error(self) -> str:
        return self._last_error

    def reload_now(self) -> bool:
        return self._try_load()

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
