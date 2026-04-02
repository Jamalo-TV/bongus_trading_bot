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
    ACCOUNT_EQUITY_USD,
    BASIS_DEVIATION_STOP,
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
    LIVE_CONFIG_PATH,
    MAKER_FILL_PROBABILITY,
    MAX_DRAWDOWN_PCT,
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
    PER_CLUSTER_NOTIONAL_CAP_USD,
    PER_SYMBOL_NOTIONAL_CAP_USD,
    PORTFOLIO_CLUSTER_MAP,
    RANKER_WEIGHTS,
    RANKER_WINSORIZE_LOWER_PCT,
    RANKER_WINSORIZE_UPPER_PCT,
    RATCHETING_AGE_MINUTES,
    RATCHETING_BREAKEVEN_BPS,
    RATCHETING_ENABLED,
    ROTATION_MAX_PAYBACK_DAYS,
    RUNTIME_HEARTBEAT_INTERVAL_SECONDS,
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
    SHADOW_EXIT_ENABLED,
    SHADOW_EXIT_MIN_INCREMENTAL_VALUE_USD,
    SHADOW_EXIT_MODEL_PATH,
    SNIPE_ANN_FUNDING_THRESHOLD,
    SOFT_DRAWDOWN_PCT,
    TARGET_CONCURRENT_POSITIONS,
    TRADER_CYCLE_INTERVAL_SECONDS,
    WF_MAX_DRAWDOWN_PCT,
    WF_MIN_AVG_OOS_EDGE,
    WF_MIN_SIGNAL_TO_NOISE,
    WF_MIN_TRADES_PER_WINDOW,
    WF_MIN_UTILIZATION,
    WF_MIN_WINDOWS_PASSING,
    WF_PROMOTION_ENABLED,
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
    "max_venue_latency_ms": MAX_VENUE_LATENCY_MS,
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
}


class ConfigManager:
    """File-backed configuration manager with safe hot reloads."""

    def __init__(self, config_path: str | Path = LIVE_CONFIG_PATH, poll_interval: float = 30.0):
        self._path = Path(config_path)
        self._poll_interval = poll_interval
        self._lock = threading.Lock()
        self._values: dict[str, ConfigValue] = copy.deepcopy(_DEFAULTS)
        self._last_mtime: float = 0.0
        self._stop_event = threading.Event()
        self._poll_thread: threading.Thread | None = None
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
                raw = json.load(handle)

            changed: dict[str, tuple[ConfigValue, ConfigValue]] = {}
            with self._lock:
                for key, value in raw.items():
                    if key not in _DEFAULTS:
                        continue
                    normalized = self._normalize(key, value)
                    if self._values.get(key) != normalized:
                        changed[key] = (copy.deepcopy(self._values[key]), copy.deepcopy(normalized))
                        self._values[key] = normalized
                self._last_mtime = mtime

            for key, (old, new) in changed.items():
                logger.info("Config reloaded: %s: %r -> %r", key, old, new)

            return bool(changed)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning("Failed to load config from %s: %s", self._path, exc)
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

        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")

        with self._lock:
            self._values = copy.deepcopy(_DEFAULTS)
            self._values.update(payload)
            self._last_mtime = os.path.getmtime(self._path)

        return self.snapshot()

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
