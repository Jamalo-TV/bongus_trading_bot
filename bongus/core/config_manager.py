"""Thread-safe hot-reload configuration manager."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import os
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from bongus.core.config import (
    ACCOUNT_EQUITY_USD,
    ADAPTIVE_RULES_PAPER_ONLY,
    ADAPTIVE_THRESHOLDS_ENABLED,
    AI_REPORT_AGENT_ENABLED,
    ALLOW_AUTONOMOUS_INVERSE_LIQUIDATION,
    ALLOW_REVERSE_SPOT_ENTRY,
    AUTONOMOUS_STARTUP_RECOVERY,
    BASIS_DEVIATION_STOP,
    COOLDOWN_EMERGENCY_MINUTES,
    COOLDOWN_ENABLED,
    COOLDOWN_HALTED_MINUTES,
    COOLDOWN_PARTIAL_EXIT_MINUTES,
    COOLDOWN_SYMBOL_MINUTES,
    CORRELATION_FILTER_MIN_OBSERVATIONS,
    CORRELATION_FILTER_THRESHOLD,
    DAILY_PNL_SUMMARY_HOUR_UTC,
    DAILY_PNL_SUMMARY_MINUTE_UTC,
    DATA_RETENTION_DAYS,
    DECISION_ENGINE_STAGE,
    DEFAULT_CLUSTER,
    EMERGENCY_EXIT_MAX_RETRIES,
    EMERGENCY_EXIT_MAX_SLIPPAGE_BPS,
    EMERGENCY_EXIT_READBACK_ATTEMPTS,
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
    FEATURE_RETENTION_DAYS,
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
    LIVE_APPROVAL_ARTIFACT_PATH,
    LIVE_APPROVAL_REQUIRED,
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
    OPERATOR_FLATTEN_ALL_REQUEST_ID,
    OPERATOR_FLATTEN_ALL_REQUESTED_AT,
    OPERATOR_FLATTEN_ALL_REQUESTED_BY,
    KILL_RECOVERY_REQUEST_ID,
    KILL_RECOVERY_REQUESTED_AT,
    KILL_RECOVERY_REQUESTED_BY,
    PAUSE_NEW_ENTRIES,
    PENDING_INTENT_MAX_AGE_SECONDS,
    PER_CLUSTER_NOTIONAL_CAP_USD,
    PER_SYMBOL_NOTIONAL_CAP_USD,
    PORTFOLIO_CLUSTER_MAP,
    RANKER_WEIGHTS,
    RANKER_WINSORIZE_LOWER_PCT,
    RANKER_WINSORIZE_UPPER_PCT,
    RATCHETING_AGE_MINUTES,
    RATCHETING_BREAKEVEN_BPS,
    RATCHETING_ENABLED,
    REGIME_FILTER_BASIS_ABS_FLOOR,
    REGIME_FILTER_BASIS_WIDENING_MAX,
    REGIME_FILTER_BASIS_ZSCORE_MAX,
    REGIME_FILTER_DEPTH_RATIO_MIN,
    REGIME_FILTER_ENABLED,
    REGIME_FILTER_FUNDING_DISPERSION_MAX,
    REGIME_FILTER_MIN_SAMPLES,
    REGIME_FILTER_PRICE_SHOCK_PCT,
    REGIME_FILTER_VOLUME_SPIKE_MAX,
    RESEARCH_EVIDENCE_MIN_INTERVAL_SECONDS,
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
    SNAPSHOT_RETENTION_DAYS,
    SNIPE_ANN_FUNDING_THRESHOLD,
    SOFT_DRAWDOWN_PCT,
    STALE_INTENT_COOLDOWN_BASE_SECONDS,
    STARTUP_RECOVERY_ACKNOWLEDGE_SYMBOLS,
    STARTUP_RECOVERY_AUTO_EXIT_MANUAL_REVIEW,
    STORAGE_COMPONENT_BUDGETS_BYTES,
    STORAGE_CONTROL_GENERATION,
    STORAGE_CRITICAL_FREE_BYTES,
    STORAGE_CRITICAL_FREE_FRACTION,
    STORAGE_CRITICAL_TTF_HOURS,
    STORAGE_DEGRADED_FREE_BYTES,
    STORAGE_DEGRADED_FREE_FRACTION,
    STORAGE_DEGRADED_TTF_HOURS,
    STORAGE_EMERGENCY_FREE_BYTES,
    STORAGE_EMERGENCY_FREE_FRACTION,
    STORAGE_EMERGENCY_LATCHED,
    STORAGE_EMERGENCY_TTF_HOURS,
    STORAGE_HEALTHY_FREE_BYTES,
    STORAGE_MONITOR_INTERVAL_SECONDS,
    STORAGE_RECOVERY_ACKNOWLEDGED,
    STORAGE_RECOVERY_HEALTHY_SAMPLES,
    STORAGE_RECOVERY_HYSTERESIS_BYTES,
    STORAGE_RECOVERY_REQUEST_ID,
    STORAGE_RECOVERY_REQUESTED_AT,
    STORAGE_RECOVERY_REQUESTED_BY,
    STORAGE_RESERVE_BYTES,
    STORAGE_VOLUME_BUDGET_BYTES,
    STORAGE_WARNING_FREE_BYTES,
    STORAGE_WARNING_FREE_FRACTION,
    STORAGE_WARNING_TTF_HOURS,
    STRESS_TEST_SPOT_CRASH_PCT,
    TARGET_CONCURRENT_POSITIONS,
    TRADER_CYCLE_INTERVAL_SECONDS,
    VALIDATION_ADJUST_NOTIONAL_SCALE,
    VALIDATION_ADJUST_SHARPE_MIN,
    VALIDATION_GO_MAX_DRAWDOWN_PCT,
    VALIDATION_GO_MIN_INTERVENTION_FREE_DAYS,
    VALIDATION_GO_SHARPE_MIN,
    VALIDATION_NO_GO_MAX_DRAWDOWN_PCT,
    VALIDATION_SNAPSHOT_INTERVAL_MINUTES,
    VALIDATION_TARGET_COST_MODEL_ERROR_MAX_PCT,
    VALIDATION_TARGET_MONTHLY_RETURN_MAX_PCT,
    VALIDATION_TARGET_MONTHLY_RETURN_MIN_PCT,
    VALIDATION_TARGET_UPTIME_MIN_PCT,
    VALIDATION_TARGET_WIN_RATE_MIN,
    VENUE_LATENCY_DEBOUNCE_S,
    VENUE_LATENCY_SMOOTHING_FACTOR,
    WF_MAX_DRAWDOWN_PCT,
    WF_MIN_AVG_OOS_EDGE,
    WF_MIN_SIGNAL_TO_NOISE,
    WF_MIN_TRADES_PER_WINDOW,
    WF_MIN_UTILIZATION,
    WF_MIN_WINDOWS_PASSING,
    WF_PROMOTION_ENABLED,
    WIN_STREAK_RESET,
)

logger = logging.getLogger(__name__)

ConfigValue = str | int | float | bool | list[Any] | dict[str, Any]


def _config_float(value: ConfigValue, *, key: str) -> float:
    if not isinstance(value, (str, int, float, bool)):
        raise ValueError(f"{key} must be numeric")
    return float(value)


def _config_int(value: ConfigValue, *, key: str) -> int:
    if not isinstance(value, (str, int, float, bool)):
        raise ValueError(f"{key} must be an integer")
    return int(value)


_LIVE_REQUIRED_KEYS: frozenset[str] = frozenset(
    {
        "account_equity_usd",
        "allow_reverse_spot_entry",
        "allow_autonomous_inverse_liquidation",
        "autonomous_startup_recovery",
        "entry_ann_funding_threshold",
        "entry_premium_threshold",
        "execution_default_max_slippage_bps",
        "emergency_exit_max_retries",
        "emergency_exit_readback_attempts",
        "emergency_exit_max_slippage_bps",
        "live_approval_artifact_path",
        "live_approval_required",
        "max_drawdown_pct",
        "max_drawdown_release_pct",
        "max_gross_exposure_usd",
        "min_expected_edge_bps",
        "notional_per_trade",
        "pause_new_entries",
        "per_symbol_notional_cap_usd",
        "reset_equity_high_watermark",
        "scanner_max_data_stale_seconds",
        "scanner_max_spread_bps",
        "scanner_max_toxic_spread_bps",
        "scanner_min_depth_multiplier",
        "scanner_min_depth_usd",
        "soft_drawdown_pct",
        "storage_component_budgets_bytes",
        "storage_critical_free_bytes",
        "storage_degraded_free_bytes",
        "storage_emergency_free_bytes",
        "storage_reserve_bytes",
        "storage_warning_free_bytes",
    }
)

_DECISION_ENGINE_STAGES = frozenset({"shadow", "paper_candidate", "testnet_candidate", "live_approved"})

_INTERNAL_STORAGE_CONTROL_KEYS = frozenset(
    {
        "storage_control_generation",
        "storage_emergency_latched",
        "storage_recovery_acknowledged",
    }
)


def _enforce_live_safety_floors(values: Mapping[str, ConfigValue]) -> None:
    """Reject live overrides that weaken immutable reviewed safety policy."""

    minimums = {
        "validation_go_sharpe_min": VALIDATION_GO_SHARPE_MIN,
        "validation_go_min_intervention_free_days": (VALIDATION_GO_MIN_INTERVENTION_FREE_DAYS),
        "validation_target_win_rate_min": VALIDATION_TARGET_WIN_RATE_MIN,
        "validation_target_uptime_min_pct": VALIDATION_TARGET_UPTIME_MIN_PCT,
        "storage_warning_free_bytes": STORAGE_WARNING_FREE_BYTES,
        "storage_degraded_free_bytes": STORAGE_DEGRADED_FREE_BYTES,
        "storage_emergency_free_bytes": STORAGE_EMERGENCY_FREE_BYTES,
        "storage_critical_free_bytes": STORAGE_CRITICAL_FREE_BYTES,
        "storage_reserve_bytes": STORAGE_RESERVE_BYTES,
        "storage_recovery_hysteresis_bytes": STORAGE_RECOVERY_HYSTERESIS_BYTES,
        "storage_recovery_healthy_samples": STORAGE_RECOVERY_HEALTHY_SAMPLES,
    }
    maximums = {
        "validation_go_max_drawdown_pct": VALIDATION_GO_MAX_DRAWDOWN_PCT,
        "validation_no_go_max_drawdown_pct": VALIDATION_NO_GO_MAX_DRAWDOWN_PCT,
        "validation_target_cost_model_error_max_pct": (VALIDATION_TARGET_COST_MODEL_ERROR_MAX_PCT),
        "max_leverage": MAX_LEVERAGE,
        "max_notional_per_trade": MAX_NOTIONAL_PER_TRADE,
        "notional_per_trade": NOTIONAL_PER_TRADE,
        "max_gross_exposure_usd": MAX_GROSS_EXPOSURE_USD,
        "per_symbol_notional_cap_usd": PER_SYMBOL_NOTIONAL_CAP_USD,
        "target_concurrent_positions": TARGET_CONCURRENT_POSITIONS,
        "snapshot_retention_days": SNAPSHOT_RETENTION_DAYS,
        "feature_retention_days": FEATURE_RETENTION_DAYS,
        "market_sample_retention_days": MARKET_SAMPLE_RETENTION_DAYS,
        "storage_volume_budget_bytes": STORAGE_VOLUME_BUDGET_BYTES,
    }
    for key, floor in minimums.items():
        if _config_float(values[key], key=key) < float(floor):
            raise ValueError(f"{key} cannot be below immutable live floor {floor}")
    for key, ceiling in maximums.items():
        if _config_float(values[key], key=key) > float(ceiling):
            raise ValueError(f"{key} cannot exceed immutable live ceiling {ceiling}")
    if bool(values.get("allow_reverse_spot_entry")):
        raise ValueError("reverse short-spot entry is not approved for live mode")
    if not bool(values.get("live_approval_required")):
        raise ValueError("live_approval_required cannot be disabled in live mode")
    stage = str(values.get("decision_engine_stage") or "").strip().lower()
    if stage != "live_approved":
        raise ValueError("live mode requires the live_approved decision stage")
    configured_budgets = values.get("storage_component_budgets_bytes")
    if not isinstance(configured_budgets, Mapping):
        raise ValueError("storage_component_budgets_bytes must be an object")
    for component, reviewed_cap in STORAGE_COMPONENT_BUDGETS_BYTES.items():
        configured_cap = configured_budgets.get(component)
        if configured_cap is None:
            raise ValueError(f"storage_component_budgets_bytes missing {component!r}")
        if int(configured_cap) > int(reviewed_cap):
            raise ValueError(f"storage budget for {component} exceeds immutable live cap {reviewed_cap}")


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
    "emergency_exit_max_retries": EMERGENCY_EXIT_MAX_RETRIES,
    "emergency_exit_readback_attempts": EMERGENCY_EXIT_READBACK_ATTEMPTS,
    "emergency_exit_max_slippage_bps": EMERGENCY_EXIT_MAX_SLIPPAGE_BPS,
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
    "research_evidence_min_interval_seconds": RESEARCH_EVIDENCE_MIN_INTERVAL_SECONDS,
    "storage_volume_budget_bytes": STORAGE_VOLUME_BUDGET_BYTES,
    "storage_component_budgets_bytes": copy.deepcopy(STORAGE_COMPONENT_BUDGETS_BYTES),
    "storage_healthy_free_bytes": STORAGE_HEALTHY_FREE_BYTES,
    "storage_warning_free_bytes": STORAGE_WARNING_FREE_BYTES,
    "storage_degraded_free_bytes": STORAGE_DEGRADED_FREE_BYTES,
    "storage_emergency_free_bytes": STORAGE_EMERGENCY_FREE_BYTES,
    "storage_critical_free_bytes": STORAGE_CRITICAL_FREE_BYTES,
    "storage_warning_free_fraction": STORAGE_WARNING_FREE_FRACTION,
    "storage_degraded_free_fraction": STORAGE_DEGRADED_FREE_FRACTION,
    "storage_emergency_free_fraction": STORAGE_EMERGENCY_FREE_FRACTION,
    "storage_critical_free_fraction": STORAGE_CRITICAL_FREE_FRACTION,
    "storage_warning_ttf_hours": STORAGE_WARNING_TTF_HOURS,
    "storage_degraded_ttf_hours": STORAGE_DEGRADED_TTF_HOURS,
    "storage_emergency_ttf_hours": STORAGE_EMERGENCY_TTF_HOURS,
    "storage_critical_ttf_hours": STORAGE_CRITICAL_TTF_HOURS,
    "storage_recovery_hysteresis_bytes": STORAGE_RECOVERY_HYSTERESIS_BYTES,
    "storage_recovery_healthy_samples": STORAGE_RECOVERY_HEALTHY_SAMPLES,
    "storage_reserve_bytes": STORAGE_RESERVE_BYTES,
    "storage_monitor_interval_seconds": STORAGE_MONITOR_INTERVAL_SECONDS,
    "storage_control_generation": STORAGE_CONTROL_GENERATION,
    "storage_emergency_latched": STORAGE_EMERGENCY_LATCHED,
    "storage_recovery_acknowledged": STORAGE_RECOVERY_ACKNOWLEDGED,
    "storage_recovery_request_id": STORAGE_RECOVERY_REQUEST_ID,
    "storage_recovery_requested_at": STORAGE_RECOVERY_REQUESTED_AT,
    "storage_recovery_requested_by": STORAGE_RECOVERY_REQUESTED_BY,
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
    "kill_recovery_request_id": KILL_RECOVERY_REQUEST_ID,
    "kill_recovery_requested_at": KILL_RECOVERY_REQUESTED_AT,
    "kill_recovery_requested_by": KILL_RECOVERY_REQUESTED_BY,
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
    "validation_adjust_notional_scale": VALIDATION_ADJUST_NOTIONAL_SCALE,
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
    "autonomous_startup_recovery": AUTONOMOUS_STARTUP_RECOVERY,
    "decision_engine_stage": DECISION_ENGINE_STAGE,
    "allow_reverse_spot_entry": ALLOW_REVERSE_SPOT_ENTRY,
    "live_approval_required": LIVE_APPROVAL_REQUIRED,
    "live_approval_artifact_path": LIVE_APPROVAL_ARTIFACT_PATH,
}


def _coerce_bool(key: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    raise ValueError(f"{key} must be a boolean")


def _validate_finite_number(key: str, value: float) -> None:
    if not math.isfinite(value):
        raise ValueError(f"{key} must be finite")


def _validate_live_ranges(normalized: dict[str, ConfigValue]) -> None:
    def number(key: str) -> float | None:
        if key not in normalized:
            return None
        value = normalized[key]
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"{key} must be numeric")
        result = float(value)
        _validate_finite_number(key, result)
        return result

    positive_keys = (
        "account_equity_usd",
        "max_gross_exposure_usd",
        "max_notional_per_trade",
        "notional_per_trade",
        "per_cluster_notional_cap_usd",
        "per_symbol_notional_cap_usd",
        "scanner_min_depth_usd",
    )
    for key in positive_keys:
        value = number(key)
        if value is not None and value <= 0.0:
            raise ValueError(f"{key} must be positive")

    non_negative_keys = (
        "execution_default_max_slippage_bps",
        "execution_max_passive_offset_bps",
        "emergency_exit_max_slippage_bps",
        "min_expected_edge_bps",
        "min_incremental_portfolio_edge_bps",
        "scanner_max_data_stale_seconds",
        "scanner_max_spread_bps",
        "scanner_max_toxic_spread_bps",
        "scanner_min_depth_multiplier",
    )
    for key in non_negative_keys:
        value = number(key)
        if value is not None and value < 0.0:
            raise ValueError(f"{key} must be non-negative")

    emergency_slippage = number("emergency_exit_max_slippage_bps")
    if emergency_slippage is not None and emergency_slippage > EMERGENCY_EXIT_MAX_SLIPPAGE_BPS:
        raise ValueError("emergency_exit_max_slippage_bps cannot exceed the compiled safety ceiling")

    emergency_integer_ceilings = {
        "emergency_exit_max_retries": EMERGENCY_EXIT_MAX_RETRIES,
        "emergency_exit_readback_attempts": EMERGENCY_EXIT_READBACK_ATTEMPTS,
    }
    for key, ceiling in emergency_integer_ceilings.items():
        if key in normalized:
            raw = normalized[key]
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1 or raw > ceiling:
                raise ValueError(f"{key} must be an integer between 1 and compiled ceiling {ceiling}")

    max_drawdown = number("max_drawdown_pct")
    if max_drawdown is not None and not (0.0 < max_drawdown <= 0.25):
        raise ValueError("max_drawdown_pct must be > 0 and <= 0.25 for live safety")

    soft_drawdown = number("soft_drawdown_pct")
    if soft_drawdown is not None:
        if soft_drawdown < 0.0:
            raise ValueError("soft_drawdown_pct must be non-negative")
        if max_drawdown is not None and soft_drawdown >= max_drawdown:
            raise ValueError("soft_drawdown_pct must be below max_drawdown_pct")

    release_drawdown = number("max_drawdown_release_pct")
    if release_drawdown is not None:
        if release_drawdown < 0.0 or release_drawdown > 0.20:
            raise ValueError("max_drawdown_release_pct must be between 0 and 0.20")
        if max_drawdown is not None and release_drawdown >= max_drawdown:
            raise ValueError("max_drawdown_release_pct must be below max_drawdown_pct")

    premium_threshold = number("entry_premium_threshold")
    if premium_threshold is not None and not (-0.001 <= premium_threshold <= 0.002):
        raise ValueError("entry_premium_threshold must be between -10bps and 20bps")

    scanner_spread = number("scanner_max_spread_bps")
    scanner_toxic_spread = number("scanner_max_toxic_spread_bps")
    if scanner_spread is not None and scanner_toxic_spread is not None and scanner_spread > scanner_toxic_spread:
        raise ValueError("scanner_max_spread_bps must not exceed scanner_max_toxic_spread_bps")

    maker_probability = number("maker_fill_probability")
    if maker_probability is not None and not (0.0 <= maker_probability <= 1.0):
        raise ValueError("maker_fill_probability must be between 0 and 1")

    validation_adjust_scale = number("validation_adjust_notional_scale")
    if validation_adjust_scale is not None and not (0.10 <= validation_adjust_scale <= 1.0):
        raise ValueError("validation_adjust_notional_scale must be between 0.10 and 1.0")

    decision_stage = str(normalized.get("decision_engine_stage") or "").strip().lower()
    if decision_stage not in _DECISION_ENGINE_STAGES:
        raise ValueError("decision_engine_stage must be one of " + ", ".join(sorted(_DECISION_ENGINE_STAGES)))

    free_thresholds = [
        _config_int(normalized[key], key=key)
        for key in (
            "storage_warning_free_bytes",
            "storage_degraded_free_bytes",
            "storage_emergency_free_bytes",
            "storage_critical_free_bytes",
        )
    ]
    if any(value <= 0 for value in free_thresholds):
        raise ValueError("storage free-byte thresholds must be positive")
    if free_thresholds != sorted(free_thresholds, reverse=True):
        raise ValueError("storage free-byte thresholds must descend warning -> critical")
    free_fractions = [
        _config_float(normalized[key], key=key)
        for key in (
            "storage_warning_free_fraction",
            "storage_degraded_free_fraction",
            "storage_emergency_free_fraction",
            "storage_critical_free_fraction",
        )
    ]
    if any(not (0.0 < value < 1.0) for value in free_fractions):
        raise ValueError("storage free fractions must be between zero and one")
    if free_fractions != sorted(free_fractions, reverse=True):
        raise ValueError("storage free fractions must descend warning -> critical")
    ttf_thresholds = [
        _config_float(normalized[key], key=key)
        for key in (
            "storage_warning_ttf_hours",
            "storage_degraded_ttf_hours",
            "storage_emergency_ttf_hours",
            "storage_critical_ttf_hours",
        )
    ]
    if any(value <= 0.0 for value in ttf_thresholds):
        raise ValueError("storage TTF thresholds must be positive")
    if ttf_thresholds != sorted(ttf_thresholds, reverse=True):
        raise ValueError("storage TTF thresholds must descend warning -> critical")
    component_budgets = normalized.get("storage_component_budgets_bytes")
    if not isinstance(component_budgets, dict) or not component_budgets:
        raise ValueError("storage_component_budgets_bytes must be a non-empty object")
    if any(int(value) <= 0 for value in component_budgets.values()):
        raise ValueError("every storage component budget must be positive")
    volume_budget = _config_int(
        normalized["storage_volume_budget_bytes"],
        key="storage_volume_budget_bytes",
    )
    if volume_budget <= free_thresholds[0]:
        raise ValueError("storage_volume_budget_bytes must exceed normal free-space headroom")
    if _config_int(normalized["storage_reserve_bytes"], key="storage_reserve_bytes") <= 0:
        raise ValueError("storage_reserve_bytes must be positive")
    if (
        _config_float(
            normalized["storage_monitor_interval_seconds"],
            key="storage_monitor_interval_seconds",
        )
        <= 0.0
    ):
        raise ValueError("storage_monitor_interval_seconds must be positive")
    control_generation = _config_int(
        normalized["storage_control_generation"],
        key="storage_control_generation",
    )
    if control_generation < 0:
        raise ValueError("storage_control_generation must not be negative")
    emergency_latched = bool(normalized["storage_emergency_latched"])
    recovery_acknowledged = bool(normalized["storage_recovery_acknowledged"])
    if control_generation == 0 and (emergency_latched or recovery_acknowledged):
        raise ValueError("generation-zero storage control must be unlatched and unacknowledged")
    if emergency_latched and recovery_acknowledged:
        raise ValueError("storage emergency cannot be latched and recovery-acknowledged together")
    if (
        _config_float(
            normalized["research_evidence_min_interval_seconds"],
            key="research_evidence_min_interval_seconds",
        )
        < 1.0
    ):
        raise ValueError("research_evidence_min_interval_seconds must be at least 1")


def validate_live_config(
    values: dict[str, Any],
    *,
    trading_mode: str | None = None,
) -> dict[str, ConfigValue]:
    normalized: dict[str, ConfigValue] = {}
    unknown = sorted(key for key in values if key not in _DEFAULTS)
    if unknown:
        raise ValueError(f"unexpected_key(s): {', '.join(unknown)}")
    forbidden_control_keys = sorted(_INTERNAL_STORAGE_CONTROL_KEYS.intersection(values))
    if forbidden_control_keys:
        raise ValueError(
            "storage control fields are internal and cannot be configured: " + ", ".join(forbidden_control_keys)
        )

    for key, value in values.items():
        default = _DEFAULTS[key]
        if isinstance(default, bool):
            normalized[key] = _coerce_bool(key, value)
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
    # Cross-field rules must be checked against the effective configuration,
    # not only against the keys present in a partial override.  Otherwise an
    # override can be individually valid but contradict a retained default.
    effective = copy.deepcopy(_DEFAULTS)
    effective.update(normalized)
    _validate_live_ranges(effective)
    if str(trading_mode or "").strip().lower() == "live":
        _enforce_live_safety_floors(effective)
    return normalized


def canonical_effective_config_json(values: dict[str, ConfigValue]) -> str:
    """Return the single wire representation of an effective configuration.

    The execution engine hashes these exact UTF-8 bytes independently.  Keep
    this representation compact, recursively key-sorted, and ASCII escaped so
    it is deterministic across Python and Rust.  ``allow_nan=False`` makes an
    invalid numeric value fail closed instead of emitting non-standard JSON.
    """

    return json.dumps(
        values,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def canonical_effective_config_bytes(values: dict[str, ConfigValue]) -> bytes:
    """Return the exact bytes covered by the effective-config SHA-256."""

    return canonical_effective_config_json(values).encode("utf-8")


def effective_config_hash(values: dict[str, ConfigValue]) -> str:
    """Return the public cross-process hash for an effective configuration."""

    return hashlib.sha256(canonical_effective_config_bytes(values)).hexdigest()


# Backward-compatible private alias for code written before the consensus API
# became public.  New callers should use ``effective_config_hash``.
_effective_config_hash = effective_config_hash


@dataclass(frozen=True, slots=True)
class CanonicalConfigSnapshot:
    """One atomically captured effective configuration and its wire identity."""

    values: dict[str, ConfigValue]
    canonical_json: str
    sha256: str

    @property
    def canonical_bytes(self) -> bytes:
        return self.canonical_json.encode("utf-8")


@contextmanager
def _config_file_lock(path: Path) -> Iterator[None]:
    """Serialize config readers/writers across processes.

    The lock lives beside the config rather than on the config inode because
    atomic replacement intentionally swaps that inode.  Keeping the sidecar
    stable prevents another process from bypassing a writer during replace.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_write_json(path: Path, payload: dict[str, ConfigValue]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o640)
        os.replace(temporary_path, path)
        if os.name == "posix":
            descriptor = os.open(
                path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


class ConfigManager:
    """File-backed configuration manager with safe hot reloads."""

    def __init__(
        self,
        config_path: str | Path = LIVE_CONFIG_PATH,
        poll_interval: float = 30.0,
        on_validation_error=None,
        on_reload=None,
        trading_mode: str | None = None,
    ):
        self._path = Path(config_path)
        self._trading_mode = (
            str(trading_mode if trading_mode is not None else os.getenv("TRADING_MODE", "paper")).strip().lower()
        )
        if self._trading_mode not in {"paper", "testnet", "live"}:
            self._trading_mode = "paper"
        self._poll_interval = poll_interval
        self._lock = threading.Lock()
        self._values: dict[str, ConfigValue] = copy.deepcopy(_DEFAULTS)
        self._last_mtime: float = 0.0
        self._stop_event = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._on_validation_error = on_validation_error
        self._on_reload = on_reload
        self._last_error: str = ""
        self._loaded_keys: set[str] = set()
        self._version_hash = effective_config_hash(self._values)
        self._try_load()

    def _normalize(self, key: str, value: Any) -> ConfigValue:
        default = _DEFAULTS[key]
        if isinstance(default, bool):
            return _coerce_bool(key, value)
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

            with _config_file_lock(self._path):
                with self._path.open(encoding="utf-8") as handle:
                    payload = json.load(handle)
            raw = validate_live_config(payload, trading_mode=self._trading_mode)
            effective = copy.deepcopy(_DEFAULTS)
            effective.update(raw)

            changed: dict[str, tuple[ConfigValue, ConfigValue]] = {}
            with self._lock:
                for key, value in effective.items():
                    normalized = self._normalize(key, value)
                    if self._values.get(key) != normalized:
                        changed[key] = (copy.deepcopy(self._values[key]), copy.deepcopy(normalized))
                        self._values[key] = normalized
                self._last_mtime = mtime
                self._last_error = ""
                self._loaded_keys = set(payload.keys())
                self._version_hash = effective_config_hash(self._values)

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

    def canonical_snapshot(self) -> CanonicalConfigSnapshot:
        """Capture values, canonical JSON, and SHA-256 under one lock.

        Returning all three from the same critical section prevents a hot
        reload from pairing the bytes for one configuration with the hash for
        another configuration during a cross-process sync.
        """

        with self._lock:
            values = copy.deepcopy(self._values)
            canonical_json = canonical_effective_config_json(values)
            sha256 = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
        return CanonicalConfigSnapshot(
            values=values,
            canonical_json=canonical_json,
            sha256=sha256,
        )

    def write_overrides(self, overrides: dict[str, Any]) -> dict[str, ConfigValue]:
        with _config_file_lock(self._path):
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

            payload = validate_live_config(
                payload,
                trading_mode=self._trading_mode,
            )
            _atomic_write_json(self._path, payload)

        with self._lock:
            self._values = copy.deepcopy(_DEFAULTS)
            self._values.update(payload)
            self._last_mtime = os.path.getmtime(self._path)
            self._last_error = ""
            self._loaded_keys = set(payload.keys())
            self._version_hash = effective_config_hash(self._values)

        return self.snapshot()

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def version_hash(self) -> str:
        with self._lock:
            return self._version_hash

    def reload_now(self) -> bool:
        return self._try_load()

    def apply_updates(self, updates: dict[str, Any]) -> dict[str, ConfigValue]:
        return self.write_overrides(updates)

    @classmethod
    def allowed_keys(cls) -> set[str]:
        return set(_DEFAULTS)

    @classmethod
    def required_live_keys(cls) -> set[str]:
        return set(_LIVE_REQUIRED_KEYS)

    def missing_required_live_keys(self) -> list[str]:
        with self._lock:
            loaded = set(self._loaded_keys)
        return sorted(_LIVE_REQUIRED_KEYS - loaded)

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
