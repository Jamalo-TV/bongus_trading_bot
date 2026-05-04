"""Thread-safe hot-reload configuration manager."""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from bongus.core.config import (
    ADAPTIVE_RULES_PAPER_ONLY,
    ADAPTIVE_THRESHOLDS_ENABLED,
    AI_REPORT_AGENT_ENABLED,
    ACCOUNT_EQUITY_USD,
    BASIS_DEVIATION_STOP,
    COOLDOWN_EMERGENCY_MINUTES,
    COOLDOWN_ENABLED,
    COOLDOWN_HALTED_MINUTES,
    COOLDOWN_PARTIAL_EXIT_MINUTES,
    COOLDOWN_SYMBOL_MINUTES,
    DAILY_PNL_SUMMARY_HOUR_UTC,
    DAILY_PNL_SUMMARY_MINUTE_UTC,
    DATA_RETENTION_DAYS,
    FEATURE_RETENTION_DAYS,
    SNAPSHOT_RETENTION_DAYS,
    DEFAULT_CLUSTER,
    ENTRY_ANN_FUNDING_THRESHOLD,
    ENTRY_PREMIUM_THRESHOLD,
    EXECUTION_DEFAULT_MAX_SLIPPAGE_BPS,
    EXECUTION_MAX_PASSIVE_OFFSET_BPS,
    EXECUTION_MIN_MAKER_FILL_PROBABILITY,
    EXECUTION_QUALITY_TARGET_SLIPPAGE_BPS,
    EXECUTION_SEND_TIMEOUT_MS,
    EXECUTION_SLICE_MAX_NOTIONAL_USD,
    EXIT_ANN_FUNDING_THRESHOLD,
    EXIT_DISCOUNT_THRESHOLD,
    CORRELATION_FILTER_MIN_OBSERVATIONS,
    CORRELATION_FILTER_THRESHOLD,
    HEALTH_ALERT_ZSCORE,
    HEALTH_MONITOR_ENABLED,
    HEALTH_SAFE_MODE_ZSCORE,
    HEALTH_SAMPLE_RETENTION_DAYS,
    HEARTBEAT_INTERVAL_SECONDS,
    HEARTBEAT_MISS_THRESHOLD,
    HISTORICAL_VAR_CONFIDENCE,
    HISTORICAL_VAR_MIN_OBSERVATIONS,
    HISTORICAL_VAR_RISK_BUDGET_PCT,
    HISTORICAL_VAR_WINDOW,
    HWM_AUTO_DECAY_AFTER_HOURS,
    HWM_AUTO_DECAY_FRACTION,
    LIVE_CONFIG_PATH,
    LOSS_STREAK_ENTRY_MULTIPLIER,
    LOSS_STREAK_MIN_HOLD_HOURS,
    LOSS_STREAK_NOTIONAL_SCALE,
    LOSS_STREAK_TRIGGER,
    MAKER_FILL_PROBABILITY,
    MARKET_SAMPLE_RETENTION_DAYS,
    MAX_DRAWDOWN_PCT,
    MAX_DRAWDOWN_RELEASE_PCT,
    MAX_GROSS_EXPOSURE_USD,
    MAX_LEVERAGE,
    MAX_NOTIONAL_PER_TRADE,
    MAX_RUNTIME_STALENESS_SECONDS,
    MAX_TOP_N,
    MAX_VENUE_LATENCY_MS,
    MIN_EXPECTED_EDGE_BPS,
    MIN_INCREMENTAL_PORTFOLIO_EDGE_BPS,
    MIN_TOP_N,
    NOTIONAL_PER_TRADE,
    PAUSE_NEW_ENTRIES,
    PENDING_INTENT_MAX_AGE_SECONDS,
    PER_CLUSTER_NOTIONAL_CAP_USD,
    PER_SYMBOL_NOTIONAL_CAP_USD,
    PORTFOLIO_CLUSTER_MAP,
    OPERATOR_FLATTEN_ALL_REQUESTED_AT,
    OPERATOR_FLATTEN_ALL_REQUESTED_BY,
    OPERATOR_FLATTEN_ALL_REQUEST_ID,
    REGIME_FILTER_BASIS_ABS_FLOOR,
    REGIME_FILTER_BASIS_WIDENING_MAX,
    REGIME_FILTER_BASIS_ZSCORE_MAX,
    REGIME_FILTER_DEPTH_RATIO_MIN,
    REGIME_FILTER_ENABLED,
    REGIME_FILTER_FUNDING_DISPERSION_MAX,
    REGIME_FILTER_MIN_SAMPLES,
    REGIME_FILTER_PRICE_SHOCK_PCT,
    REGIME_FILTER_VOLUME_SPIKE_MAX,
    RANKER_WEIGHTS,
    RANKER_WINSORIZE_LOWER_PCT,
    RANKER_WINSORIZE_UPPER_PCT,
    RATCHETING_AGE_MINUTES,
    RATCHETING_BREAKEVEN_BPS,
    RATCHETING_ENABLED,
    RESET_EQUITY_HIGH_WATERMARK,
    ROTATION_MAX_PAYBACK_DAYS,
    RUNTIME_HEARTBEAT_INTERVAL_SECONDS,
    RUNTIME_SETTLING_SECONDS,
    SCANNER_ALLOWLIST,
    SCANNER_BLOCKLIST,
    SCANNER_MAX_CANDIDATES,
    SCANNER_MAX_DATA_STALE_SECONDS,
    SCANNER_MAX_SPREAD_BPS,
    SCANNER_MAX_TOXIC_SPREAD_BPS,
    SCANNER_MIN_BOOK_DEPTH_LEVELS,
    SCANNER_MIN_DEPTH_MULTIPLIER,
    SCANNER_MIN_DEPTH_USD,
    SCANNER_MIN_LISTING_AGE_DAYS,
    SCANNER_REQUIRE_SPOT_AND_PERP,
    SENTIMENT_ENABLED,
    SHADOW_EXIT_ENABLED,
    SHADOW_EXIT_MIN_INCREMENTAL_VALUE_USD,
    SHADOW_EXIT_MODEL_PATH,
    SNIPE_ANN_FUNDING_THRESHOLD,
    SOFT_DRAWDOWN_PCT,
    STARTUP_RECOVERY_ACKNOWLEDGE_SYMBOLS,
    STARTUP_RECOVERY_AUTO_EXIT_MANUAL_REVIEW,
    STRESS_TEST_SPOT_CRASH_PCT,
    TARGET_CONCURRENT_POSITIONS,
    TRADER_CYCLE_INTERVAL_SECONDS,
    VALIDATION_SNAPSHOT_INTERVAL_MINUTES,
    VALIDATION_GO_SHARPE_MIN,
    VALIDATION_ADJUST_SHARPE_MIN,
    VALIDATION_GO_MAX_DRAWDOWN_PCT,
    VALIDATION_NO_GO_MAX_DRAWDOWN_PCT,
    VALIDATION_GO_MIN_INTERVENTION_FREE_DAYS,
    VALIDATION_TARGET_MONTHLY_RETURN_MIN_PCT,
    VALIDATION_TARGET_MONTHLY_RETURN_MAX_PCT,
    VALIDATION_TARGET_WIN_RATE_MIN,
    VALIDATION_TARGET_COST_MODEL_ERROR_MAX_PCT,
    VALIDATION_TARGET_UPTIME_MIN_PCT,
    WF_MAX_DRAWDOWN_PCT,
    WF_MIN_AVG_OOS_EDGE,
    WF_MIN_SIGNAL_TO_NOISE,
    WF_MIN_TRADES_PER_WINDOW,
    WF_MIN_UTILIZATION,
    WF_MIN_WINDOWS_PASSING,
    WF_PROMOTION_ENABLED,
    WIN_STREAK_RESET,
    STALE_INTENT_COOLDOWN_BASE_SECONDS,
    VENUE_LATENCY_SMOOTHING_FACTOR,
    VENUE_LATENCY_DEBOUNCE_S,
    ALLOW_AUTONOMOUS_INVERSE_LIQUIDATION,
)

logger = logging.getLogger(__name__)

ConfigValue = str | int | float | bool | list[Any] | dict[str, Any]

_DEFAULTS: dict[str, ConfigValue] = {
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
    "max_drawdown_release_pct": MAX_DRAWDOWN_RELEASE_PCT,
    "max_venue_latency_ms": MAX_VENUE_LATENCY_MS,
    "runtime_settling_seconds": RUNTIME_SETTLING_SECONDS,
    "maker_fill_probability": MAKER_FILL_PROBABILITY,
    "snipe_ann_funding_threshold": SNIPE_ANN_FUNDING_THRESHOLD,
    "runtime_heartbeat_interval_seconds": RUNTIME_HEARTBEAT_INTERVAL_SECONDS,
    "trader_cycle_interval_seconds": TRADER_CYCLE_INTERVAL_SECONDS,
    "max_runtime_staleness_seconds": MAX_RUNTIME_STALENESS_SECONDS,
    "scanner_min_depth_usd": SCANNER_MIN_DEPTH_USD,
    "scanner_min_depth_multiplier": SCANNER_MIN_DEPTH_MULTIPLIER,
    "scanner_max_spread_bps": SCANNER_MAX_SPREAD_BPS,
    "scanner_max_toxic_spread_bps": SCANNER_MAX_TOXIC_SPREAD_BPS,
    "scanner_min_listing_age_days": SCANNER_MIN_LISTING_AGE_DAYS,
    "scanner_max_data_stale_seconds": SCANNER_MAX_DATA_STALE_SECONDS,
    "scanner_min_book_depth_levels": SCANNER_MIN_BOOK_DEPTH_LEVELS,
    "scanner_require_spot_and_perp": SCANNER_REQUIRE_SPOT_AND_PERP,
    "scanner_allowlist": list(SCANNER_ALLOWLIST),
    "scanner_blocklist": list(SCANNER_BLOCKLIST),
    "scanner_max_candidates": SCANNER_MAX_CANDIDATES,
    "ranker_weights": copy.deepcopy(RANKER_WEIGHTS),
    "ranker_winsorize_lower_pct": RANKER_WINSORIZE_LOWER_PCT,
    "ranker_winsorize_upper_pct": RANKER_WINSORIZE_UPPER_PCT,
    "historical_var_confidence": HISTORICAL_VAR_CONFIDENCE,
    "historical_var_window": HISTORICAL_VAR_WINDOW,
    "historical_var_min_observations": HISTORICAL_VAR_MIN_OBSERVATIONS,
    "historical_var_risk_budget_pct": HISTORICAL_VAR_RISK_BUDGET_PCT,
    "correlation_filter_threshold": CORRELATION_FILTER_THRESHOLD,
    "correlation_filter_min_observations": CORRELATION_FILTER_MIN_OBSERVATIONS,
    "stress_test_spot_crash_pct": STRESS_TEST_SPOT_CRASH_PCT,
    "min_expected_edge_bps": MIN_EXPECTED_EDGE_BPS,
    "min_incremental_portfolio_edge_bps": MIN_INCREMENTAL_PORTFOLIO_EDGE_BPS,
    "rotation_max_payback_days": ROTATION_MAX_PAYBACK_DAYS,
    "target_concurrent_positions": TARGET_CONCURRENT_POSITIONS,
    "min_top_n": MIN_TOP_N,
    "max_top_n": MAX_TOP_N,
    "per_symbol_notional_cap_usd": PER_SYMBOL_NOTIONAL_CAP_USD,
    "per_cluster_notional_cap_usd": PER_CLUSTER_NOTIONAL_CAP_USD,
    "portfolio_cluster_map": copy.deepcopy(PORTFOLIO_CLUSTER_MAP),
    "default_cluster": DEFAULT_CLUSTER,
    "execution_send_timeout_ms": EXECUTION_SEND_TIMEOUT_MS,
    "execution_slice_max_notional_usd": EXECUTION_SLICE_MAX_NOTIONAL_USD,
    "execution_default_max_slippage_bps": EXECUTION_DEFAULT_MAX_SLIPPAGE_BPS,
    "execution_max_passive_offset_bps": EXECUTION_MAX_PASSIVE_OFFSET_BPS,
    "execution_min_maker_fill_probability": EXECUTION_MIN_MAKER_FILL_PROBABILITY,
    "execution_quality_target_slippage_bps": EXECUTION_QUALITY_TARGET_SLIPPAGE_BPS,
    "wf_min_avg_oos_edge": WF_MIN_AVG_OOS_EDGE,
    "wf_min_windows_passing": WF_MIN_WINDOWS_PASSING,
    "wf_min_trades_per_window": WF_MIN_TRADES_PER_WINDOW,
    "wf_min_signal_to_noise": WF_MIN_SIGNAL_TO_NOISE,
    "wf_max_drawdown_pct": WF_MAX_DRAWDOWN_PCT,
    "wf_min_utilization": WF_MIN_UTILIZATION,
    "wf_promotion_enabled": WF_PROMOTION_ENABLED,
    "shadow_exit_enabled": SHADOW_EXIT_ENABLED,
    "shadow_exit_model_path": SHADOW_EXIT_MODEL_PATH,
    "shadow_exit_min_incremental_value_usd": SHADOW_EXIT_MIN_INCREMENTAL_VALUE_USD,
    "ratcheting_enabled": RATCHETING_ENABLED,
    "ratcheting_age_minutes": RATCHETING_AGE_MINUTES,
    "ratcheting_breakeven_bps": RATCHETING_BREAKEVEN_BPS,
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
    "pending_intent_max_age_seconds": PENDING_INTENT_MAX_AGE_SECONDS,
    "data_retention_days": DATA_RETENTION_DAYS,
    "feature_retention_days": FEATURE_RETENTION_DAYS,
    "snapshot_retention_days": SNAPSHOT_RETENTION_DAYS,
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
    "loss_streak_min_hold_hours": LOSS_STREAK_MIN_HOLD_HOURS,
    "daily_pnl_summary_hour_utc": DAILY_PNL_SUMMARY_HOUR_UTC,
    "daily_pnl_summary_minute_utc": DAILY_PNL_SUMMARY_MINUTE_UTC,
    "pause_new_entries": PAUSE_NEW_ENTRIES,
    "operator_flatten_all_request_id": OPERATOR_FLATTEN_ALL_REQUEST_ID,
    "operator_flatten_all_requested_at": OPERATOR_FLATTEN_ALL_REQUESTED_AT,
    "operator_flatten_all_requested_by": OPERATOR_FLATTEN_ALL_REQUESTED_BY,
    "startup_recovery_acknowledge_symbols": list(STARTUP_RECOVERY_ACKNOWLEDGE_SYMBOLS),
    "startup_recovery_auto_exit_manual_review": STARTUP_RECOVERY_AUTO_EXIT_MANUAL_REVIEW,
    "reset_equity_high_watermark": RESET_EQUITY_HIGH_WATERMARK,
    "hwm_auto_decay_after_hours": HWM_AUTO_DECAY_AFTER_HOURS,
    "hwm_auto_decay_fraction": HWM_AUTO_DECAY_FRACTION,
    "validation_snapshot_interval_minutes": VALIDATION_SNAPSHOT_INTERVAL_MINUTES,
    "validation_go_sharpe_min": VALIDATION_GO_SHARPE_MIN,
    "validation_adjust_sharpe_min": VALIDATION_ADJUST_SHARPE_MIN,
    "validation_go_max_drawdown_pct": VALIDATION_GO_MAX_DRAWDOWN_PCT,
    "validation_no_go_max_drawdown_pct": VALIDATION_NO_GO_MAX_DRAWDOWN_PCT,
    "validation_go_min_intervention_free_days": VALIDATION_GO_MIN_INTERVENTION_FREE_DAYS,
    "validation_target_monthly_return_min_pct": VALIDATION_TARGET_MONTHLY_RETURN_MIN_PCT,
    "validation_target_monthly_return_max_pct": VALIDATION_TARGET_MONTHLY_RETURN_MAX_PCT,
    "validation_target_win_rate_min": VALIDATION_TARGET_WIN_RATE_MIN,
    "validation_target_cost_model_error_max_pct": VALIDATION_TARGET_COST_MODEL_ERROR_MAX_PCT,
    "validation_target_uptime_min_pct": VALIDATION_TARGET_UPTIME_MIN_PCT,
    "stale_intent_cooldown_base_seconds": STALE_INTENT_COOLDOWN_BASE_SECONDS,
    "venue_latency_smoothing_factor": VENUE_LATENCY_SMOOTHING_FACTOR,
    "venue_latency_debounce_s": VENUE_LATENCY_DEBOUNCE_S,
    "allow_autonomous_inverse_liquidation": ALLOW_AUTONOMOUS_INVERSE_LIQUIDATION,
}


def validate_live_config(values: dict[str, Any]) -> dict[str, ConfigValue]:
    normalized: dict[str, ConfigValue] = {}
    unknown = sorted(key for key in values if key not in _DEFAULTS)
    if unknown:
        raise ValueError(f"unexpected_key(s): {', '.join(unknown)}")

    for key, value in values.items():
        default = _DEFAULTS[key]
        if isinstance(default, bool):
            normalized[key] = bool(value)
        elif isinstance(default, int) and not isinstance(default, bool):
            normalized[key] = int(value)
        elif isinstance(default, float):
            normalized[key] = float(value)
        elif isinstance(default, list):
            normalized[key] = list(value)
        elif isinstance(default, dict):
            normalized[key] = dict(value)
        else:
            normalized[key] = str(value)
    return normalized


class ConfigManager:
    """File-backed configuration manager with safe hot reloads."""

    def __init__(
        self,
        config_path: str | Path = LIVE_CONFIG_PATH,
        poll_interval: float = 30.0,
        on_validation_error=None,
        on_reload=None,
    ):
        self._path = Path(config_path)
        self._poll_interval = poll_interval
        self._lock = threading.Lock()
        self._values: dict[str, ConfigValue] = copy.deepcopy(_DEFAULTS)
        self._last_mtime: float = 0.0
        self._stop_event = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._on_validation_error = on_validation_error
        self._on_reload = on_reload
        self._last_error: str = ""
        self._try_load()

    def _normalize(self, key: str, value: Any) -> ConfigValue:
        default = _DEFAULTS[key]
        if isinstance(default, bool):
            return bool(value)
        if isinstance(default, int) and not isinstance(default, bool):
            return int(value)
        if isinstance(default, float):
            return float(value)
        if isinstance(default, list):
            return list(value)
        if isinstance(default, dict):
            return dict(value)
        return str(value)

    def _try_load(self) -> bool:
        if not self._path.exists():
            return False

        try:
            mtime = os.path.getmtime(self._path)
            if mtime <= self._last_mtime:
                return False

            with self._path.open(encoding="utf-8") as handle:
                raw = validate_live_config(json.load(handle))

            changed: dict[str, tuple[ConfigValue, ConfigValue]] = {}
            with self._lock:
                for key, value in raw.items():
                    normalized = self._normalize(key, value)
                    if self._values.get(key) != normalized:
                        changed[key] = (copy.deepcopy(self._values[key]), copy.deepcopy(normalized))
                        self._values[key] = normalized
                self._last_mtime = mtime
                self._last_error = ""

            for key, (old, new) in changed.items():
                logger.info("Config reloaded: %s: %r -> %r", key, old, new)
            if changed and self._on_reload is not None:
                try:
                    self._on_reload(changed, self.snapshot())
                except Exception as exc:
                    logger.warning("Config reload callback failed: %s", exc)

            return bool(changed)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self._last_error = str(exc)
            logger.warning("Failed to load config from %s: %s", self._path, exc)
            if self._on_validation_error is not None:
                try:
                    self._on_validation_error(str(exc))
                except Exception as callback_exc:
                    logger.warning("Config validation callback failed: %s", callback_exc)
            return False

    def get(self, key: str, default: Any | None = None) -> Any:
        with self._lock:
            if key in self._values:
                return copy.deepcopy(self._values[key])
        return default

    def get_bool(self, key: str) -> bool:
        return bool(self.get(key, False))

    def get_float(self, key: str) -> float:
        return float(self.get(key, 0.0))

    def get_int(self, key: str) -> int:
        return int(self.get(key, 0))

    def snapshot(self) -> dict[str, ConfigValue]:
        with self._lock:
            return copy.deepcopy(self._values)

    def write_overrides(self, overrides: dict[str, Any]) -> dict[str, ConfigValue]:
        payload: dict[str, ConfigValue] = {}
        if self._path.exists():
            try:
                with self._path.open(encoding="utf-8") as handle:
                    existing = json.load(handle)
                for key, value in existing.items():
                    if key in _DEFAULTS:
                        payload[key] = self._normalize(key, value)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                payload = {}

        for key, value in overrides.items():
            if key in _DEFAULTS:
                payload[key] = self._normalize(key, value)

        payload = validate_live_config(payload)

        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")

        with self._lock:
            self._values = copy.deepcopy(_DEFAULTS)
            self._values.update(payload)
            self._last_mtime = os.path.getmtime(self._path)
            self._last_error = ""

        return self.snapshot()

    @property
    def last_error(self) -> str:
        return self._last_error

    def reload_now(self) -> bool:
        return self._try_load()

    def apply_updates(self, updates: dict[str, Any]) -> dict[str, ConfigValue]:
        return self.write_overrides(updates)

    @classmethod
    def allowed_keys(cls) -> set[str]:
        return set(_DEFAULTS)

    def start_watching(self) -> None:
        if self._poll_thread is not None:
            return

        def _poll() -> None:
            while not self._stop_event.wait(self._poll_interval):
                self._try_load()

        self._poll_thread = threading.Thread(target=_poll, daemon=True)
        self._poll_thread.start()
        logger.info(
            "Config watcher started (polling every %.0fs): %s",
            self._poll_interval,
            self._path,
        )

    def stop_watching(self) -> None:
        self._stop_event.set()
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=5)
            self._poll_thread = None
