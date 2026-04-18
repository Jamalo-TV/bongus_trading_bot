"""Multi-symbol live trader orchestrator.

Wires together:
  - RustDataSubscriber (depth + fill confirmations from Rust port 9000)
  - FundingRanker (single REST call every 60s)
  - CorrelationBreaker (portfolio-level circuit breaker)
  - PortfolioAllocator (sizing, liquidity filter, rotation)
  - ExecutionClient (ZMQ PUSH to Rust)
  - StateWriter/StateReader (SQLite shared state)

Execution invariant: exits are dispatched first; ENTER for a rotation target
only fires after FILLED confirmation from Rust (or timeout fallback).

The original live_trader.py is preserved as a single-symbol fallback.
"""

import asyncio
from collections import deque
import hashlib
import hmac
import json
import logging
import math
import os
import signal
import sys
import time
import uuid
from urllib.parse import urlencode
from datetime import datetime, timedelta, timezone
from statistics import fmean, pstdev

import requests
from dotenv import load_dotenv

if __package__ in {None, ""}:
    _BOOTSTRAP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if _BOOTSTRAP_ROOT not in sys.path:
        sys.path.insert(0, _BOOTSTRAP_ROOT)

from bongus.core.config import (
    CAPITAL_PER_SLOT_USD,
    TARGET_LEVERAGE,
    ROTATION_CONFIRM_TIMEOUT_S,
    FUNDING_SNAPSHOT_HOURS,
    DYNAMIC_SYMBOL_MODE,
    ENTRY_PREMIUM_THRESHOLD,
    INVERSE_FUNDING_ENABLED,
    MAX_ALLOWED_GAP_MINUTES,
    MAX_CONCURRENT_POSITIONS,
    MAX_LIVE_ENRICHED_SYMBOLS,
    MAX_NOTIONAL_PER_TRADE,
    MAX_SYMBOL_CONCENTRATION,
    FUNDING_INTERVAL_HOURS,
    FUNDING_PERIODS_PER_YEAR,
    LIQUIDITY_FILTER_MULTIPLIER,
    MARGIN_BORROW_RATE_ANNUAL,
    PENDING_INTENT_MAX_AGE_SECONDS,
    ROTATION_MIN_GAP_ANN,
    ADAPTIVE_RULES_PAPER_ONLY,
    ADAPTIVE_THRESHOLDS_ENABLED,
    AI_REPORT_AGENT_ENABLED,
    DAILY_PNL_SUMMARY_HOUR_UTC,
    DAILY_PNL_SUMMARY_MINUTE_UTC,
    DATA_RETENTION_DAYS,
    HEALTH_ALERT_ZSCORE,
    HEALTH_MONITOR_ENABLED,
    HEALTH_SAFE_MODE_ZSCORE,
    HEALTH_SAMPLE_RETENTION_DAYS,
    HEARTBEAT_INTERVAL_SECONDS,
    HEARTBEAT_MISS_THRESHOLD,
    LOSS_STREAK_ENTRY_MULTIPLIER,
    LOSS_STREAK_NOTIONAL_SCALE,
    LOSS_STREAK_TRIGGER,
    MARKET_SAMPLE_RETENTION_DAYS,
    VALIDATION_SNAPSHOT_INTERVAL_MINUTES,
    WIN_STREAK_RESET,
    get_monitored_symbols,
)
from bongus.core.binance_endpoints import get_rest_base_urls, resolve_binance_credentials
from bongus.core.config_manager import ConfigManager
from bongus.engine.cooldown_manager import CooldownManager
from bongus.engine.cost_model import blended_entry_cost, blended_exit_cost
from bongus.engine.risk_engine import RiskDecision, RiskEngine, RiskLimits, RiskState
from bongus.engine.state_store import CandidateSnapshot, StateWriter, StateReader, Trade
from bongus.ipc.execution import ExecutionClient
from bongus.market_data.bybit_monitor import BybitFundingMonitor
from bongus.market_data.depth_tracker import DepthTracker
from bongus.market_data.funding_predictor import FundingPredictor, MIN_CONFIDENCE_FOR_ENTRY
from bongus.market_data.funding_ranker import FundingRanker
from bongus.market_data.rust_data_subscriber import RustDataSubscriber
from bongus.market_data.rest_depth_fetcher import RestDepthFetcher
from bongus.monitoring.performance_metrics import calculate_metrics
from bongus.portfolio.correlation_breaker import CorrelationBreaker
from bongus.portfolio.portfolio_allocator import OpenPosition, PortfolioAllocator
from bongus.portfolio.regime_filter import RegimeDecision, RegimeFilter

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_DOTENV_PATH = os.path.join(_PROJECT_ROOT, ".env")
_SENTIMENT_PATH = os.path.join(_PROJECT_ROOT, "current_sentiment.json")

load_dotenv(_DOTENV_PATH)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("live_trader_v2")

# If the circuit breaker stays HALTED for this long, escalate to partial exits
# rather than holding troubled positions indefinitely with no recovery path.
_HALTED_ESCALATION_SECS: int = 1800  # 30 minutes
_STALE_EXIT_MAX_RESUBMIT_ATTEMPTS: int = 3
_STALE_ENTER_MAX_CANCEL_ATTEMPTS: int = 3
_SIGNED_RECV_WINDOW_MS: int = 10_000
_POSITION_QTY_TOLERANCE: float = 1e-9
_DEFAULT_COST_DEPTH_USD: float = 500_000.0
_BLOCKED_EXIT_CODE: int = 78
_STARTUP_HEARTBEAT_TIMEOUT_S: float = 15.0
_EXCHANGE_POSITION_AUDIT_INTERVAL_S: float = 300.0
_GUARDED_EXCHANGE_POSITION_AUDIT_INTERVAL_S: float = 60.0
_SYMBOL_UNIVERSE_REFRESH_INTERVAL_S: float = 900.0
_BINANCE_TIME_SYNC_TTL_S: float = 30.0
_AUDIT_FAILURE_SAFE_MODE_THRESHOLD: int = 5
_USD_COLLATERAL_ASSETS: frozenset[str] = frozenset({"USDT", "USDC", "FDUSD", "BUSD", "USDS"})
_SPOT_HEDGE_SHORTFALL_TOLERANCE_PCT: float = 0.0025
_ENTRY_READY_RUNTIME_MODES: frozenset[str] = frozenset({"LIVE", "LIVE_WITH_SYMBOL_BLOCKS"})
_RECOVERABLE_BINANCE_SIGNED_ERROR_CODES: frozenset[int] = frozenset(
    {-1021, -1022, -2014, -2015}
)
_PER_SYMBOL_SAFE_MODE_FLAGS: frozenset[str] = frozenset(
    {
        "naked_leg_unwind_stuck",
        "startup_manual_review",
        "startup_exit_candidate",
        "hedge_gap",
    }
)
_QUOTE_ASSET_SUFFIXES: tuple[str, ...] = (
    "USDT",
    "USDC",
    "FDUSD",
    "BUSD",
    "BTC",
    "ETH",
    "BNB",
    "TRY",
    "EUR",
)


class StartupBlockedError(RuntimeError):
    pass


class BinanceSignedCallError(RuntimeError):
    def __init__(
        self,
        *,
        endpoint: str,
        code: int,
        detail: str,
        http_status: int | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.code = int(code)
        self.detail = detail
        self.http_status = http_status
        status_fragment = f" HTTP {http_status}" if http_status is not None else ""
        super().__init__(
            f"Binance signed request failed for {endpoint}:{status_fragment} ({detail})"
        )


def _float_or_zero(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _iso_from_ms(value) -> str:
    try:
        timestamp_ms = int(float(value))
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).isoformat()
    if timestamp_ms <= 0:
        return datetime.now(timezone.utc).isoformat()
    return datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc).isoformat()


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    q = min(max(float(quantile), 0.0), 1.0)
    position = q * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _pearson_correlation(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    mean_x = fmean(xs)
    mean_y = fmean(ys)
    centered_x = [value - mean_x for value in xs]
    centered_y = [value - mean_y for value in ys]
    variance_x = sum(value * value for value in centered_x)
    variance_y = sum(value * value for value in centered_y)
    if variance_x <= 0.0 or variance_y <= 0.0:
        return None
    covariance = sum(left * right for left, right in zip(centered_x, centered_y))
    return covariance / math.sqrt(variance_x * variance_y)


def _extract_base_asset(symbol: str) -> str:
    upper_symbol = symbol.upper()
    for suffix in _QUOTE_ASSET_SUFFIXES:
        if upper_symbol.endswith(suffix) and len(upper_symbol) > len(suffix):
            return upper_symbol[:-len(suffix)]
    return upper_symbol


def _spot_inventory_covers_hedge(spot_qty: float, hedge_qty: float) -> bool:
    if hedge_qty <= _POSITION_QTY_TOLERANCE:
        return True
    minimum_required = max(
        0.0,
        hedge_qty * (1.0 - _SPOT_HEDGE_SHORTFALL_TOLERANCE_PCT),
    )
    return spot_qty + _POSITION_QTY_TOLERANCE >= minimum_required


def _sum_futures_collateral_assets(account: dict | None, *, field_name: str) -> float:
    if not isinstance(account, dict):
        return 0.0
    total = 0.0
    for asset_row in account.get("assets", []):
        asset = str(asset_row.get("asset", "")).upper()
        if asset in _USD_COLLATERAL_ASSETS:
            total += _float_or_zero(asset_row.get(field_name))
    return total


def _derive_futures_account_balance(
    account: dict | None,
    *,
    preferred_fields: tuple[str, ...],
    asset_field_name: str,
) -> float:
    if not isinstance(account, dict):
        return 0.0

    reported_total = 0.0
    for field_name in preferred_fields:
        reported_total = _float_or_zero(account.get(field_name))
        if reported_total > 0.0:
            break

    collateral_total = _sum_futures_collateral_assets(account, field_name=asset_field_name)
    if collateral_total > reported_total + 1e-9:
        logger.warning(
            "Futures account aggregate %s=%.2f under-reports collateral asset sum %.2f; using asset-derived balance",
            "/".join(preferred_fields),
            reported_total,
            collateral_total,
        )
        return collateral_total

    return reported_total


class LiveTraderV2:
    def __init__(self) -> None:
        self._trading_mode = os.getenv("TRADING_MODE", "paper").lower()
        logger.info("TRADING_MODE = %s", self._trading_mode)
        logger.info(
            "Runtime config: ACCOUNT_EQUITY_USD=%s MAX_GROSS_EXPOSURE_USD=%s MONITORED_SYMBOLS=%s",
            os.getenv("ACCOUNT_EQUITY_USD", "10000"),
            os.getenv("MAX_GROSS_EXPOSURE_USD", "50000"),
            os.getenv("MONITORED_SYMBOLS", "<default>"),
        )
        self.monitored_symbols = get_monitored_symbols()
        self._monitored_symbol_set = set(self.monitored_symbols)
        self._tradable_perp_symbols: set[str] = set(self.monitored_symbols)
        self._tradable_spot_symbols: set[str] = set(self.monitored_symbols)
        self._spot_universe_loaded = False

        self.depth_tracker = DepthTracker()
        # Always seed with monitored_symbols so they're tracked from startup.
        # In dynamic mode the ranker expands beyond them when refresh() runs.
        self.funding_ranker = FundingRanker(self.monitored_symbols, dynamic=DYNAMIC_SYMBOL_MODE)
        self.breaker = CorrelationBreaker()
        self.state_writer = StateWriter()
        self.state_reader = StateReader()
        self._config = ConfigManager(
            on_validation_error=self._on_config_validation_error,
            on_reload=self._on_config_reloaded,
        )
        self.allocator = PortfolioAllocator(self.depth_tracker, self.funding_ranker)
        self.predictor = FundingPredictor()
        self.bybit_monitor = BybitFundingMonitor(None if DYNAMIC_SYMBOL_MODE else self.monitored_symbols)
        self.regime_filter = RegimeFilter(self.depth_tracker, config_get=self._config.get)
        self.cooldowns = CooldownManager(config_get=self._config.get)
        # REST fallback depth fetcher - used when WebSocket depth is unavailable
        self.rest_depth_fetcher = RestDepthFetcher(self.monitored_symbols)
        self._last_compound_check: float = 0.0
        self._last_xval_check: float = 0.0
        self._xval_last_warn_at: dict[str, float] = {}
        self._xval_mismatch_snapshot: dict[str, tuple[float, float]] = {}
        self._sentiment_score: float = 0.0
        self._last_breaker_state: str = "CLEAR"
        # Tracks when the circuit breaker first entered HALTED state.
        # If HALTED persists beyond _HALTED_ESCALATION_SECS, exit troubled positions
        # rather than holding them indefinitely with no recovery path.
        self._halted_since: float = 0.0
        self.execution = ExecutionClient(endpoint="tcp://127.0.0.1:5555")
        self._config.start_watching()
        self._shutdown_started = False
        self._shutdown_event = asyncio.Event()
        self._background_tasks: list[asyncio.Task] = []
        self._safe_mode_flags: set[str] = set()
        self._blocked_reason: str = ""
        self._runtime_mode: str = "LIVE"
        self._last_runtime_mode_change: str = datetime.now(timezone.utc).isoformat()
        self._operator_pause_new_entries_bridge: bool = False
        self._last_heartbeat_sent_id: str = ""
        self._last_heartbeat_sent_monotonic: float = 0.0
        self._last_heartbeat_ack_monotonic: float = 0.0
        self._last_heartbeat_rtt_ms: int = 0
        self._heartbeat_misses: int = 0
        self._last_heartbeat_ack_id: str = ""
        self._last_heartbeat_ack_at: str = ""
        self._last_telemetry_event_monotonic: float = 0.0
        self._latest_volume_bar: dict[str, tuple[str, float]] = {}
        self._basis_levels: dict[str, deque[float]] = {}
        self._basis_returns: dict[str, deque[float]] = {}
        self._last_basis_sample_monotonic: dict[str, float] = {}
        self._last_operator_flatten_request_id: str = str(
            self._config.get("operator_flatten_all_request_id") or ""
        ).strip()
        self._last_sampled_minute: str = ""
        self._last_retention_run_date: str = ""
        self._last_validation_snapshot_bucket: int | None = None
        self._last_entry_funnel_log_monotonic: float = 0.0
        self._preflight_status: str = "idle"
        self._bot_started_at: str = datetime.now(timezone.utc).isoformat()
        self._session_id: str = f"run_{uuid.uuid4().hex[:12]}"
        self._adaptive_entry_threshold_base: float = float(self._config.get("entry_ann_funding_threshold"))
        self._adaptive_rotation_gap: float = ROTATION_MIN_GAP_ANN
        self._streak_notional_scale: float = 1.0
        self._risk_position_scale: float = 1.0
        self._risk_allow_new_risk: bool = True
        self._risk_derisk_required: bool = False
        self._risk_kill_switch: bool = False
        self._risk_reasons: list[str] = []
        self._risk_last_evaluated_at: str = ""
        self._last_risk_log_signature: tuple[bool, bool, bool, tuple[str, ...]] | None = None
        self._last_risk_log_monotonic: float = 0.0
        self._latest_exchange_account_equity: float | None = None
        self._latest_exchange_available_balance: float | None = None
        self._latest_exchange_account_equity_at: str = ""
        # Snapshot of gross exposure from the last _evaluate_risk_controls call.
        # Used by _dispatch_enter for a prospective check before committing a new order.
        self._current_gross_exposure_usd: float = 0.0
        self._current_gross_by_symbol: dict[str, float] = {}
        self._loss_streak: int = 0
        self._win_streak: int = 0
        self._last_exchange_health_check_monotonic: float = 0.0
        self._last_exchange_position_audit_monotonic: float = 0.0
        self._last_symbol_universe_refresh_monotonic: float = 0.0
        self._last_pending_intent_self_heal_monotonic: float = 0.0
        self._loop: asyncio.AbstractEventLoop | None = None
        credentials = resolve_binance_credentials()
        self._futures_api_key = credentials["futures_api_key"]
        self._futures_api_secret = credentials["futures_api_secret"]
        self._spot_api_key = credentials["spot_api_key"]
        self._spot_api_secret = credentials["spot_api_secret"]
        self._futures_base_url, self._spot_base_url = get_rest_base_urls(self._trading_mode)
        self._binance_time_offset_ms: int = 0
        self._binance_time_sync_expires_at_monotonic: float = 0.0
        self._audit_consecutive_failures: int = 0
        self._startup_complete_at: str = ""

        # Write trading mode to state DB so dashboard can display it
        self.state_writer.set_risk_snapshot(
            {
                "trading_mode": self._trading_mode,
                "runtime_mode": self._runtime_mode,
                "preflight_status": self._preflight_status,
                "bot_started_at": self._bot_started_at,
                "session_id": self._session_id,
                "allow_new_risk": True,
            }
        )
        # Restore the high-watermark from the previous session so a restart with
        # underwater positions does not reset drawdown to zero.  The risk snapshot
        # persists "account_equity_high_watermark" on every cycle; fall back to the
        # configured starting equity only when no prior snapshot exists.
        _persisted_hwm = None
        try:
            _persisted_hwm = self.state_reader.get_risk().get("account_equity_high_watermark")
        except Exception:
            pass
        _config_equity = float(self._config.get("account_equity_usd"))
        self._peak_account_equity: float = (
            float(_persisted_hwm)
            if _persisted_hwm is not None and float(_persisted_hwm) >= _config_equity
            else _config_equity
        )
        self._risk_engine = RiskEngine()

        # Pending exit tracking: symbol â†’ asyncio.Event (set when FILLED received from Rust).
        # Note: spec described this as set[str]; dict[str, Event] enables per-symbol await
        # without a global polling loop â€” deliberate improvement over the spec.
        self._exit_events: dict[str, asyncio.Event] = {}
        self._pending_exit_intents: dict[str, str] = {}
        self._pending_exit_created_at: dict[str, str] = {}

        # Pending enter tracking: symbol â†’ entry intent data stored at dispatch time.
        # Consumed when ENTER FILLED arrives to write position to SQLite.
        self._pending_enters: dict[str, dict] = {}
        self._stale_pending_enters: dict[str, dict] = {}
        self._stale_pending_exits: set[str] = set()
        self._abandoned_pending_enters: dict[str, dict] = {}
        self._abandoned_exit_intents: dict[str, dict] = {}
        self._stale_exit_resubmit_attempts: dict[str, int] = {}
        self._stale_enter_cancel_attempts: dict[str, int] = {}
        self._entry_failure_recovery_tasks: dict[str, asyncio.Task] = {}

        # Entry time cache: populated on ENTER fill, consumed on EXIT fill for trade record.
        self._entry_times: dict[str, str] = {}

        # Estimated entry-side execution cost, captured at dispatch time and used
        # as a fallback when live exchange commissions are unavailable.
        self._estimated_entry_costs: dict[str, float] = {}

        # Mark price cache: populated from perp markPrice WebSocket events.
        # Used by _dispatch_enter to compute base-asset qty from notional.
        self._mark_prices: dict[str, float] = {}
        self._mark_price_updated_monotonic: dict[str, float] = {}

        # Track when we first received mark price for each symbol (for startup readiness check)
        self._mark_price_ready: set[str] = set()

        # LOT_SIZE step sizes per symbol fetched from Binance at startup.
        # Keyed by symbol (e.g. "BTCUSDT" â†’ 0.001). Falls back to 1e-5 if absent.
        self._lot_step: dict[str, float] = {}

        # Direction cache: populated from state DB each loop iteration.
        # "long" = long spot + short perp; "short" = short spot + long perp (inverse funding).
        self._position_directions: dict[str, str] = {}
        self._startup_exit_candidates: dict[str, str] = {}
        self._startup_manual_review_symbols: dict[str, str] = {}
        self._startup_recovery_last_attempt_monotonic: dict[str, float] = {}
        self._startup_recovery_consecutive_failures: dict[str, int] = {}
        self._startup_recovery_stuck_symbols: dict[str, str] = {}

        self.subscriber = RustDataSubscriber(
            on_depth=self._on_depth_update,
            on_order_update=self._on_order_update,
            on_mark_price=self._on_mark_price,
            on_heartbeat_ack=self._on_heartbeat_ack,
            on_volume_bar=self._on_volume_bar,
            on_order_rejected=self._on_order_rejected,
        )

    def _cross_validation_enabled(self) -> bool:
        # Testnet mode should stay self-contained on Binance demo infrastructure.
        return self._trading_mode != "testnet"

    def _maybe_log_cross_validation_gap(
        self,
        symbol: str,
        ranker_rate: float,
        bybit_rate: float,
        *,
        now: float,
    ) -> None:
        gap = abs(bybit_rate - ranker_rate)
        if gap <= 0.01:
            if symbol in self._xval_mismatch_snapshot:
                logger.info(
                    "Cross-validation back within tolerance for %s: ranker=%.4f bybit=%.4f",
                    symbol,
                    ranker_rate,
                    bybit_rate,
                )
                self._xval_mismatch_snapshot.pop(symbol, None)
                self._xval_last_warn_at.pop(symbol, None)
            return

        previous = self._xval_mismatch_snapshot.get(symbol)
        significant_shift = previous is None
        if previous is not None:
            prev_ranker_rate, prev_bybit_rate = previous
            prev_gap = abs(prev_bybit_rate - prev_ranker_rate)
            significant_shift = (
                abs(gap - prev_gap) >= 0.02
                or (ranker_rate > 0.0) != (prev_ranker_rate > 0.0)
                or (bybit_rate > 0.0) != (prev_bybit_rate > 0.0)
            )

        last_warn = self._xval_last_warn_at.get(symbol)
        if last_warn is None or now - last_warn >= 600 or significant_shift:
            logger.warning(
                "Cross-validation mismatch for %s: ranker=%.4f bybit=%.4f",
                symbol,
                ranker_rate,
                bybit_rate,
            )
            self._xval_last_warn_at[symbol] = now

        self._xval_mismatch_snapshot[symbol] = (ranker_rate, bybit_rate)

    def _on_config_validation_error(self, error: str) -> None:
        logger.warning("Rejected live_config.json reload: %s", error)
        self._set_config_reload_status(
            {
                "config_last_error": error,
                "config_last_error_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    def _on_config_reloaded(self, changed: dict, snapshot: dict) -> None:
        del snapshot
        config = getattr(self, "_config", None)
        if config is None:
            return
        self._set_config_reload_status(
            {
                "config_last_error": "",
                "config_last_reload_at": datetime.now(timezone.utc).isoformat(),
                "config_last_reloaded_keys": sorted(changed.keys()),
            }
        )
        if "pause_new_entries" in changed:
            self._operator_pause_new_entries_bridge = False
            self._set_config_reload_status(
                {
                    "pause_new_entries": bool(config.get("pause_new_entries")),
                }
            )
        operator_request_id = str(config.get("operator_flatten_all_request_id") or "").strip()
        if (
            "operator_flatten_all_request_id" in changed
            and operator_request_id
            and operator_request_id != self._last_operator_flatten_request_id
        ):
            self._last_operator_flatten_request_id = operator_request_id
            open_positions = getattr(self, "state_reader", None)
            open_symbols = []
            if open_positions is not None:
                open_symbols = sorted(
                    str(position.get("symbol", "")).upper()
                    for position in self.state_reader.get_positions_for_current_mode()
                    if position.get("symbol")
                )
            self._set_config_reload_status(
                {
                    "operator_flatten_all_request_id": operator_request_id,
                    "operator_flatten_all_requested_at": str(config.get("operator_flatten_all_requested_at") or ""),
                    "operator_flatten_all_requested_by": str(config.get("operator_flatten_all_requested_by") or ""),
                    "operator_flatten_all_status": "requested",
                    "operator_flatten_all_acknowledged_at": "",
                    "operator_flatten_all_completed_at": "",
                    "operator_flatten_all_remaining_symbols": open_symbols,
                    "operator_flatten_all_dispatched_symbols": [],
                    "operator_flatten_all_note": (
                        "New entries paused. Trader will dispatch immediate exits for every open position."
                    ),
                    "operator_flatten_all_request_open_position_count": len(open_symbols),
                }
            )
        if "startup_recovery_acknowledge_symbols" in changed:
            acknowledge_symbols = self._normalized_symbol_list(
                config.get("startup_recovery_acknowledge_symbols")
            )
            if acknowledge_symbols and getattr(self, "state_writer", None) is not None:
                self._apply_startup_recovery_acknowledgements(
                    acknowledge_symbols,
                    source="live_config",
                    requested_by="live_config",
                    clear_live_config=True,
                )
        if (
            "reset_equity_high_watermark" in changed
            and bool(config.get("reset_equity_high_watermark"))
            and getattr(self, "state_writer", None) is not None
        ):
            self._reset_equity_high_watermark(
                source="live_config",
                requested_by="live_config",
                clear_live_config=True,
            )
        state_writer = getattr(self, "state_writer", None)
        if state_writer is not None:
            state_writer.flush()

    def _set_config_reload_status(self, payload: dict[str, object]) -> None:
        state_writer = getattr(self, "state_writer", None)
        if state_writer is None:
            return
        state_writer.set_risk_snapshot(payload)

    @staticmethod
    def _normalized_symbol_list(values: object) -> list[str]:
        raw_values: list[str] = []
        if isinstance(values, str):
            raw_values = [item.strip() for item in values.replace(",", " ").split()]
        elif isinstance(values, (list, tuple, set)):
            raw_values = [str(item).strip() for item in values]

        normalized: list[str] = []
        seen: set[str] = set()
        for item in raw_values:
            symbol = str(item or "").strip().upper()
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            normalized.append(symbol)
        return normalized

    def _clear_live_config_request(self, key: str, value: object) -> None:
        config = getattr(self, "_config", None)
        if config is None:
            return
        try:
            config.apply_updates({key: value})
        except Exception as exc:
            logger.warning("Could not clear live_config request %s: %s", key, exc)

    def _current_equity_reference_for_hwm(self) -> float:
        if self._latest_exchange_account_equity is not None and self._latest_exchange_account_equity > 0.0:
            return float(self._latest_exchange_account_equity)
        risk_state = self.state_reader.get_risk() if getattr(self, "state_reader", None) is not None else {}
        for key in ("exchange_account_equity", "account_equity", "account_equity_mark_to_market"):
            value = _float_or_zero(risk_state.get(key))
            if value > 0.0:
                return value
        return float(self._config.get("account_equity_usd"))

    def _reset_equity_high_watermark(
        self,
        *,
        source: str,
        requested_by: str = "",
        clear_live_config: bool = False,
    ) -> None:
        current_equity = max(0.0, self._current_equity_reference_for_hwm())
        if current_equity <= 0.0:
            current_equity = float(self._config.get("account_equity_usd"))
        self._peak_account_equity = current_equity
        now_iso = datetime.now(timezone.utc).isoformat()
        self.state_writer.set_risk_snapshot(
            {
                "account_equity_high_watermark": self._peak_account_equity,
                "account_equity_high_watermark_reset_at": now_iso,
                "account_equity_high_watermark_reset_source": source,
                "account_equity_high_watermark_reset_by": requested_by,
            }
        )
        self.state_writer.flush()
        logger.warning(
            "Account equity high watermark reset to %.2f via %s%s",
            self._peak_account_equity,
            source,
            f" ({requested_by})" if requested_by else "",
        )
        if clear_live_config:
            self._clear_live_config_request("reset_equity_high_watermark", False)

    def _apply_startup_recovery_acknowledgements(
        self,
        symbols: list[str],
        *,
        source: str,
        requested_by: str = "",
        requested_at: str = "",
        clear_live_config: bool = False,
    ) -> dict[str, object]:
        normalized_symbols = self._normalized_symbol_list(symbols)
        if not normalized_symbols:
            return {"acknowledged": [], "skipped": {}}

        rows = self.state_reader.get_positions()
        row_map = {
            str(row.get("symbol", "")).upper(): row
            for row in rows
            if row.get("symbol")
        }
        acknowledged: list[str] = []
        skipped: dict[str, str] = {}
        now_iso = requested_at or datetime.now(timezone.utc).isoformat()

        for symbol in normalized_symbols:
            row = row_map.get(symbol)
            if row is None:
                skipped[symbol] = "symbol not found in open positions"
                continue
            if str(row.get("recovery_state") or "").strip().lower() != "manual_review":
                skipped[symbol] = "symbol is not awaiting startup manual review"
                continue
            if str(row.get("direction") or "").strip().lower() != "long":
                skipped[symbol] = "unsupported recovered direction still requires manual intervention"
                continue

            self.state_writer.update_position_metrics(symbol, recovery_state="")
            self._startup_manual_review_symbols.pop(symbol, None)
            self._startup_exit_candidates.pop(symbol, None)
            acknowledged.append(symbol)
            logger.warning(
                "Startup recovery: operator acknowledged %s via %s%s; retaining the live position without blocking new entries",
                symbol,
                source,
                f" ({requested_by})" if requested_by else "",
            )

        self.state_writer.flush()
        rows_after = self.state_reader.get_positions()
        self._refresh_startup_recovery_flags(rows_after)
        self._publish_startup_reconciliation_state(rows_after, audit_time=now_iso)
        self.state_writer.set_risk_snapshot(
            {
                "startup_recovery_last_acknowledged_symbols": acknowledged,
                "startup_recovery_last_acknowledge_skipped": skipped,
                "startup_recovery_last_acknowledged_at": now_iso,
                "startup_recovery_last_acknowledged_source": source,
                "startup_recovery_last_acknowledged_by": requested_by,
            }
        )
        self.state_writer.flush()

        if clear_live_config:
            self._clear_live_config_request("startup_recovery_acknowledge_symbols", [])

        return {"acknowledged": acknowledged, "skipped": skipped}

    def _consume_supervisor_startup_recovery_acknowledgements(self) -> None:
        risk_state = self.state_reader.get_risk()
        symbols = self._normalized_symbol_list(risk_state.get("startup_recovery_acknowledged_symbols"))
        if not symbols:
            return
        requested_by = str(risk_state.get("startup_recovery_acknowledged_by") or "")
        requested_at = str(risk_state.get("startup_recovery_acknowledged_at") or "")
        self._apply_startup_recovery_acknowledgements(
            symbols,
            source="supervisor_telegram",
            requested_by=requested_by,
            requested_at=requested_at,
        )
        self.state_writer.set_risk_snapshot(
            {
                "startup_recovery_acknowledged_symbols": [],
                "startup_recovery_acknowledged_at": "",
                "startup_recovery_acknowledged_by": "",
            }
        )
        self.state_writer.flush()

    def _basis_history_window(self) -> int:
        return max(8, int(self._config.get("historical_var_window")))

    def _capture_basis_observations(self, symbols: list[str] | set[str] | tuple[str, ...]) -> None:
        now_monotonic = time.monotonic()
        window = self._basis_history_window()
        for raw_symbol in symbols:
            symbol = str(raw_symbol or "").upper()
            if not symbol:
                continue
            last_sample = self._last_basis_sample_monotonic.get(symbol, 0.0)
            if last_sample > 0.0 and now_monotonic - last_sample < 5.0:
                continue
            basis_pct = self.depth_tracker.basis_pct(symbol)
            if basis_pct is None:
                continue
            levels = self._basis_levels.get(symbol)
            returns = self._basis_returns.get(symbol)
            if levels is None or levels.maxlen != window + 1:
                levels = deque(levels or (), maxlen=window + 1)
                self._basis_levels[symbol] = levels
            if returns is None or returns.maxlen != window:
                returns = deque(returns or (), maxlen=window)
                self._basis_returns[symbol] = returns
            if levels:
                previous_basis = levels[-1]
                if abs(basis_pct - previous_basis) <= 1e-9:
                    self._last_basis_sample_monotonic[symbol] = now_monotonic
                    continue
                returns.append(basis_pct - previous_basis)
            levels.append(basis_pct)
            self._last_basis_sample_monotonic[symbol] = now_monotonic

    def _historical_var_fraction(self, symbol: str) -> float | None:
        returns = list(self._basis_returns.get(symbol.upper(), ()))
        min_observations = max(8, int(self._config.get("historical_var_min_observations")))
        if len(returns) < min_observations:
            return None
        losses = [abs(value) for value in returns]
        confidence = float(self._config.get("historical_var_confidence"))
        var_fraction = _percentile(losses, confidence)
        return max(var_fraction, 1e-6)

    def _var_sized_notional(self, symbol: str, base_notional: float) -> float:
        if base_notional <= 0.0:
            return 0.0
        var_fraction = self._historical_var_fraction(symbol)
        if var_fraction is None:
            return base_notional
        slot_capital = max(1.0, float(getattr(self.allocator, "_capital_per_slot", CAPITAL_PER_SLOT_USD)))
        risk_budget = slot_capital * max(0.0, float(self._config.get("historical_var_risk_budget_pct")))
        if risk_budget <= 0.0:
            return base_notional
        return max(base_notional * 0.10, min(base_notional, risk_budget / var_fraction))

    def _basis_correlation(self, left_symbol: str, right_symbol: str) -> tuple[float | None, int]:
        left_returns = list(self._basis_returns.get(left_symbol.upper(), ()))
        right_returns = list(self._basis_returns.get(right_symbol.upper(), ()))
        sample_count = min(len(left_returns), len(right_returns))
        min_observations = max(8, int(self._config.get("correlation_filter_min_observations")))
        if sample_count < min_observations:
            return None, sample_count
        correlation = _pearson_correlation(left_returns[-sample_count:], right_returns[-sample_count:])
        return correlation, sample_count

    def _correlation_gate_blocked(
        self,
        ranked: list[tuple[str, float]],
        open_positions: list[OpenPosition],
    ) -> dict[str, list[str]]:
        threshold = float(self._config.get("correlation_filter_threshold"))
        open_symbols = {position.symbol.upper() for position in open_positions if position.symbol}
        permitted_new_symbols: list[str] = []
        blocked: dict[str, list[str]] = {}
        for symbol, _ann_funding in ranked:
            upper_symbol = str(symbol or "").upper()
            if not upper_symbol or upper_symbol in open_symbols:
                continue
            peers = sorted(open_symbols | set(permitted_new_symbols))
            reasons: list[str] = []
            for peer in peers:
                correlation, sample_count = self._basis_correlation(upper_symbol, peer)
                if correlation is None:
                    continue
                if correlation >= threshold:
                    reasons.append(
                        f"correlation {correlation:.2f} with {peer} over {sample_count} samples exceeds {threshold:.2f}"
                    )
            if reasons:
                blocked[upper_symbol] = reasons
                continue
            permitted_new_symbols.append(upper_symbol)
        return blocked

    def _adaptive_controls_enabled(self) -> bool:
        enabled = bool(self._config.get("adaptive_thresholds_enabled"))
        if not enabled:
            return False
        if bool(self._config.get("adaptive_rules_paper_only")) and self._trading_mode == "live":
            return False
        return True

    def _health_monitor_enabled(self) -> bool:
        enabled = bool(self._config.get("health_monitor_enabled"))
        if not enabled:
            return False
        if bool(self._config.get("adaptive_rules_paper_only")) and self._trading_mode == "live":
            return False
        return True

    def _ai_report_agent_enabled(self) -> bool:
        enabled = bool(self._config.get("ai_report_agent_enabled"))
        if not enabled:
            return False
        if bool(self._config.get("adaptive_rules_paper_only")) and self._trading_mode == "live":
            return False
        return True

    def _reset_runtime_dashboard_stats(self) -> None:
        self.state_writer.set_stats(
            {
                "open_positions": 0.0,
                "top_funding_rate": 0.0,
                "top_funding_symbol": "",
                "accepted_candidates": 0.0,
                "rejected_candidates": 0.0,
                "scanner_breadth": 0.0,
                "live_enrichment_breadth": 0.0,
            }
        )
        self.state_writer.flush()

    def _safe_mode_reason(self) -> str:
        return ", ".join(sorted(self._safe_mode_flags))

    def _active_global_safe_mode_flags(self) -> set[str]:
        return {flag for flag in self._safe_mode_flags if flag not in _PER_SYMBOL_SAFE_MODE_FLAGS}

    def _active_symbol_block_flags(self) -> set[str]:
        return {flag for flag in self._safe_mode_flags if flag in _PER_SYMBOL_SAFE_MODE_FLAGS}

    def _blocked_entry_symbols(self) -> set[str]:
        return {
            str(symbol).upper()
            for symbol in (
                set(self._startup_manual_review_symbols)
                | set(self._startup_recovery_stuck_symbols)
                | set(self._startup_exit_candidates)
                | set(self._pending_exit_intents)
                | set(self._stale_pending_enters)
                | set(self._stale_pending_exits)
            )
            if symbol
        }

    def _describe_symbol_block(self, symbol: str) -> str:
        normalized = str(symbol or "").upper()
        if normalized in self._startup_recovery_stuck_symbols:
            return self._startup_recovery_stuck_symbols[normalized]
        if normalized in self._startup_manual_review_symbols:
            return self._startup_manual_review_symbols[normalized]
        if normalized in self._startup_exit_candidates:
            return self._startup_exit_candidates[normalized]
        if normalized in self._pending_exit_intents:
            return "exit already pending confirmation"
        if normalized in self._stale_pending_enters or normalized in self._stale_pending_exits:
            return "stale pending intent awaiting reconciliation"
        return "symbol is temporarily blocked"

    def _startup_recovery_backoff_seconds(self) -> float:
        return max(0.0, _float_or_zero(self._config.get("startup_recovery_exit_backoff_s")))

    def _startup_recovery_max_rejections(self) -> int:
        return max(1, int(_float_or_zero(self._config.get("startup_recovery_exit_max_rejections")) or 0))

    def _startup_recovery_attempt_allowed(self, symbol: str) -> bool:
        last_attempt = self._startup_recovery_last_attempt_monotonic.get(symbol.upper(), 0.0)
        if last_attempt <= 0.0:
            return True
        return (time.monotonic() - last_attempt) >= self._startup_recovery_backoff_seconds()

    def _record_startup_recovery_exit_attempt(self, symbol: str) -> None:
        self._startup_recovery_last_attempt_monotonic[str(symbol or "").upper()] = time.monotonic()

    def _record_startup_recovery_exit_failure(self, symbol: str, reason: str) -> None:
        normalized = str(symbol or "").upper()
        next_failures = self._startup_recovery_consecutive_failures.get(normalized, 0) + 1
        self._startup_recovery_consecutive_failures[normalized] = next_failures
        if next_failures < self._startup_recovery_max_rejections():
            return
        self._startup_recovery_stuck_symbols[normalized] = (
            f"{normalized} naked-leg unwind is stuck after {next_failures} rejected exits"
            f" ({reason or 'unknown reason'})"
        )
        self._set_safe_mode_flag("naked_leg_unwind_stuck", True)

    def _clear_startup_recovery_exit_tracking(self, symbol: str) -> None:
        normalized = str(symbol or "").upper()
        self._startup_recovery_last_attempt_monotonic.pop(normalized, None)
        self._startup_recovery_consecutive_failures.pop(normalized, None)
        self._startup_recovery_stuck_symbols.pop(normalized, None)
        self._set_safe_mode_flag("naked_leg_unwind_stuck", bool(self._startup_recovery_stuck_symbols))

    def _is_startup_manual_review_symbol(self, symbol: str) -> bool:
        normalized = str(symbol or "").upper()
        if normalized in self._startup_manual_review_symbols:
            return True
        row = next(
            (
                position
                for position in self.state_reader.get_positions()
                if str(position.get("symbol", "")).upper() == normalized
            ),
            None,
        )
        return str((row or {}).get("recovery_state") or "").strip().lower() == "manual_review"

    def _exit_leg_skip_flags(
        self,
        symbol: str,
        *,
        direction: str,
        position_row: dict | None = None,
    ) -> tuple[bool, bool]:
        row = position_row
        if row is None:
            row = next(
                (
                    position
                    for position in self.state_reader.get_positions()
                    if str(position.get("symbol", "")).upper() == str(symbol or "").upper()
                ),
                None,
            )
        row = row or {}
        hedge_ratio = _float_or_zero(row.get("hedge_ratio"))
        side_label = str(row.get("side") or "").strip().upper()

        if direction == "long":
            # Canonical long-spot / short-perp path: if the spot hedge is gone,
            # only unwind the perp leg.
            return hedge_ratio <= _POSITION_QTY_TOLERANCE, False

        if direction == "short" and side_label == "SHORT_SPOT_LONG_PERP":
            # Unsupported startup-recovery orphan semantics: this means the live
            # residual leg is the long perp. Never send a spot BUY unwind.
            return True, False

        return False, False

    def _spot_universe_ready_for_entries(self) -> bool:
        return self._trading_mode == "paper" or (
            self._spot_universe_loaded and bool(self._tradable_spot_symbols)
        )

    def _publish_symbol_universe_state(
        self,
        *,
        refreshed_at: str,
        error: str = "",
    ) -> None:
        tradable_symbols = self._tradable_trade_symbols()
        spot_universe_unavailable = (
            self._trading_mode != "paper" and not self._spot_universe_ready_for_entries()
        )
        self.state_writer.set_risk_snapshot(
            {
                "spot_universe_loaded": bool(self._spot_universe_loaded),
                "spot_universe_last_refresh_at": refreshed_at,
                "spot_universe_last_error": error,
                "tradable_perp_symbol_count": len(self._tradable_perp_symbols),
                "tradable_spot_symbol_count": len(self._tradable_spot_symbols),
                "tradable_trade_symbol_count": len(tradable_symbols),
                "tradable_trade_symbols": sorted(tradable_symbols),
            }
        )
        self._set_safe_mode_flag("spot_universe_unavailable", spot_universe_unavailable)
        if spot_universe_unavailable:
            logger.critical(
                "Spot universe unavailable in %s mode; blocking new entries until spot exchangeInfo succeeds",
                self._trading_mode,
            )

    @staticmethod
    def _binance_error_code(payload: object) -> int | None:
        if not isinstance(payload, dict):
            return None
        code = payload.get("code")
        if isinstance(code, (int, float)):
            return int(code)
        if isinstance(code, str):
            stripped = code.strip()
            if stripped.startswith("-"):
                stripped = stripped[1:]
            if stripped.isdigit():
                return int(payload.get("code"))  # type: ignore[arg-type]
        return None

    def _raise_binance_request_error(
        self,
        *,
        endpoint: str,
        response,
        payload: object = None,
    ) -> None:
        details = self._binance_response_detail(response, payload)
        code = self._binance_error_code(payload)
        if code is not None and code < 0:
            raise BinanceSignedCallError(
                endpoint=endpoint,
                code=code,
                detail=details,
                http_status=getattr(response, "status_code", None),
            )
        status_code = getattr(response, "status_code", None)
        if status_code is not None and status_code >= 400:
            raise RuntimeError(
                f"Binance request failed for {endpoint}: HTTP {status_code} ({details})"
            )
        raise RuntimeError(f"Binance request failed for {endpoint}: {details}")

    @staticmethod
    def _invalid_binance_json_error(
        *,
        endpoint: str,
        response,
        exc: ValueError,
    ) -> json.JSONDecodeError:
        if isinstance(exc, json.JSONDecodeError):
            return exc
        return json.JSONDecodeError(
            f"Invalid JSON from Binance for {endpoint}",
            str(getattr(response, "text", "") or ""),
            0,
        )

    def _set_exchange_position_audit_snapshot(
        self,
        *,
        status: str,
        sample_time: str,
        error: str = "",
        applied: bool,
    ) -> None:
        self.state_writer.set_risk_snapshot(
            {
                "exchange_position_audit_last_status": status,
                "exchange_position_audit_last_time": sample_time,
                "exchange_position_audit_last_error": error,
                "exchange_position_audit_consecutive_failures": self._audit_consecutive_failures,
                "exchange_position_audit_applied": applied,
            }
        )
        self.state_writer.flush()

    def _startup_recovery_auto_exit_grace_elapsed(self) -> bool:
        started_at = self._startup_complete_at or self._bot_started_at
        started_dt = self._parse_timestamp(started_at)
        if started_dt is None:
            return False
        return (datetime.now(timezone.utc) - started_dt).total_seconds() >= 60.0

    def _build_reconciliation_snapshot(
        self,
        *,
        prefix: str,
        snapshot: dict,
        rows: list[dict],
        audit_time: str,
        local_only_symbols: list[str] | None = None,
        mismatched_symbols: list[str] | None = None,
        unsupported_direction_symbols: list[str] | None = None,
        last_funding_fee: float = 0.0,
        last_funding_fee_time: str = "",
        position_source: str | None = None,
    ) -> dict[str, object]:
        position_risk_rows = [
            row
            for row in snapshot.get("position_risk") or []
            if isinstance(row, dict)
            and abs(_float_or_zero(row.get("positionAmt"))) > _POSITION_QTY_TOLERANCE
        ]
        account_position_rows = self._open_account_position_rows(snapshot.get("futures_account"))
        spot_balances = self._build_spot_balance_map(snapshot.get("spot_account"))
        hedge_gap_symbols = sorted(
            str(row.get("symbol", "")).upper()
            for row in rows
            if str(row.get("direction", "")).lower() == "long"
            and _float_or_zero(row.get("hedge_ratio")) < (1.0 - _SPOT_HEDGE_SHORTFALL_TOLERANCE_PCT)
        )
        recovery_actions = {
            symbol: {
                "state": "manual_review" if symbol in self._startup_manual_review_symbols else "exit_candidate",
                "reason": self._startup_manual_review_symbols.get(symbol)
                or self._startup_exit_candidates.get(symbol)
                or "",
            }
            for symbol in sorted(
                set(self._startup_exit_candidates) | set(self._startup_manual_review_symbols)
            )
        }
        derived_position_source = position_source
        if not derived_position_source:
            derived_position_source = (
                "merged"
                if position_risk_rows and account_position_rows
                else "position_risk"
                if position_risk_rows
                else "account_fallback"
                if account_position_rows
                else "none"
            )
        return {
            f"{prefix}_status": "needs_review" if recovery_actions else "ok",
            f"{prefix}_time": audit_time,
            f"{prefix}_position_count": len(rows),
            f"{prefix}_position_risk_count": len(position_risk_rows),
            f"{prefix}_account_position_count": len(account_position_rows),
            f"{prefix}_position_source": derived_position_source,
            f"{prefix}_local_only_symbols": sorted(local_only_symbols or []),
            f"{prefix}_mismatched_symbols": sorted(mismatched_symbols or []),
            f"{prefix}_spot_hedge_gaps": hedge_gap_symbols,
            f"{prefix}_unsupported_directions": sorted(unsupported_direction_symbols or []),
            f"{prefix}_exit_candidates": sorted(self._startup_exit_candidates),
            f"{prefix}_manual_review": sorted(self._startup_manual_review_symbols),
            f"{prefix}_recovery_actions": recovery_actions,
            f"{prefix}_spot_assets": sorted(spot_balances),
            f"{prefix}_last_funding_fee": last_funding_fee,
            f"{prefix}_last_funding_fee_time": last_funding_fee_time,
        }

    def _cache_exchange_equity_snapshot(
        self,
        *,
        account_equity: float | None,
        available_balance: float | None = None,
        captured_at: str | None = None,
    ) -> dict[str, float | str]:
        snapshot: dict[str, float | str] = {}
        timestamp = str(captured_at or datetime.now(timezone.utc).isoformat())
        if account_equity is not None and float(account_equity) > 0.0:
            self._latest_exchange_account_equity = float(account_equity)
            self._latest_exchange_account_equity_at = timestamp
            snapshot["exchange_account_equity"] = float(account_equity)
            snapshot["exchange_account_equity_updated_at"] = timestamp
        if available_balance is not None and float(available_balance) >= 0.0:
            self._latest_exchange_available_balance = float(available_balance)
            snapshot["exchange_available_balance"] = float(available_balance)
        return snapshot

    def _persist_runtime_state(self) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        safe_reason = self._safe_mode_reason()
        funding_status = self.funding_ranker.status_snapshot()
        open_rows = self.state_reader.get_positions_for_current_mode()
        manual_review_count = sum(
            1
            for row in open_rows
            if str(row.get("recovery_state") or "").strip().lower() == "manual_review"
        )
        max_runtime_staleness = float(self._config.get("max_runtime_staleness_seconds"))
        preflight_passed = self._preflight_status == "passed"
        heartbeat_threshold = max(1, int(self._config.get("heartbeat_miss_threshold")))
        telemetry_staleness_seconds = (
            max(0.0, time.monotonic() - self._last_telemetry_event_monotonic)
            if self._last_telemetry_event_monotonic > 0.0
            else 9_999.0
        )
        telemetry_connected = bool(self.subscriber.is_connected) and telemetry_staleness_seconds <= max_runtime_staleness
        execution_bridge_healthy = (
            preflight_passed
            and self._last_heartbeat_ack_monotonic > 0.0
            and self._heartbeat_misses < heartbeat_threshold
        )
        runtime_ready = self._runtime_mode in _ENTRY_READY_RUNTIME_MODES and preflight_passed
        pause_new_entries = bool(self._config.get("pause_new_entries"))
        allow_new_risk = runtime_ready and self._risk_allow_new_risk and not pause_new_entries
        entry_block_reason = self._entry_policy_block_reason()
        self.state_writer.set_risk_snapshot(
            {
                "trading_mode": self._trading_mode,
                "runtime_mode": self._runtime_mode,
                "session_id": self._session_id,
                "bot_started_at": self._bot_started_at,
                "loop_last_alive_at": now_iso,
                "safe_mode_reason": safe_reason,
                "blocked_reason": self._blocked_reason,
                "entry_block_reason": entry_block_reason or "",
                "pause_new_entries": pause_new_entries,
                "allow_new_risk": allow_new_risk,
                "preflight_status": self._preflight_status,
                "runtime_ready": runtime_ready,
                "execution_bridge_healthy": execution_bridge_healthy,
                "telemetry_connected": telemetry_connected,
                "telemetry_staleness_seconds": telemetry_staleness_seconds,
                "heartbeat_status": (
                    "ok"
                    if self._heartbeat_misses == 0 and self._last_heartbeat_ack_monotonic > 0.0
                    else ("missed" if self._heartbeat_misses > 0 else "unknown")
                ),
                "heartbeat_miss_count": self._heartbeat_misses,
                "heartbeat_last_ack_id": self._last_heartbeat_ack_id,
                "heartbeat_last_ack_at": (
                    self._last_heartbeat_ack_at
                    if self._last_heartbeat_ack_monotonic > 0.0
                    else ""
                ),
                "funding_staleness_status": funding_status["funding_staleness_status"],
                "funding_last_refresh_at": funding_status["funding_last_refresh_at"],
                "funding_last_refresh_age_s": funding_status["funding_last_refresh_age_s"],
                "funding_consecutive_failures": funding_status["funding_consecutive_failures"],
                "funding_last_error": funding_status["funding_last_error"],
                "last_runtime_mode_change": self._last_runtime_mode_change,
                "risk_derisk_required": self._risk_derisk_required,
                "risk_kill_switch": self._risk_kill_switch,
                "risk_position_scale": self._risk_position_scale,
                "risk_reasons": self._risk_reasons,
                "risk_last_evaluated_at": self._risk_last_evaluated_at,
                "pending_enter_count": len(self._pending_enters),
                "stale_pending_enter_count": len(self._stale_pending_enters),
                "pending_exit_count": len(self._pending_exit_intents),
                "open_position_count": len(open_rows),
                "managed_open_position_count": max(0, len(open_rows) - manual_review_count),
                "manual_review_position_count": manual_review_count,
                "startup_recovery_unwind_stuck_symbols": sorted(self._startup_recovery_stuck_symbols),
                "startup_recovery_unwind_failure_counts": dict(
                    sorted(self._startup_recovery_consecutive_failures.items())
                ),
                "operator_pause_new_entries_bridge": self._operator_pause_new_entries_bridge,
            }
        )
        self.state_writer.flush()

    async def _run_liveness_loop(self, interval_s: float = 5.0) -> None:
        while not self._shutdown_event.is_set():
            try:
                self.state_writer.set_risk_snapshot(
                    {"loop_last_alive_at": datetime.now(timezone.utc).isoformat()}
                )
                self.state_writer.flush()
            except Exception as exc:
                logger.debug("Could not persist trader liveness heartbeat: %s", exc)
            if await self._sleep_or_shutdown(interval_s):
                break

    def _recompute_runtime_mode(self) -> None:
        previous_mode = self._runtime_mode
        global_safe_mode_flags = self._active_global_safe_mode_flags()
        symbol_block_flags = self._active_symbol_block_flags()
        if self._blocked_reason:
            self._runtime_mode = "BLOCKED"
        elif global_safe_mode_flags:
            self._runtime_mode = "SAFE_MODE"
        elif symbol_block_flags:
            self._runtime_mode = "LIVE_WITH_SYMBOL_BLOCKS"
        else:
            self._runtime_mode = "LIVE"
        if self._runtime_mode != previous_mode:
            self._last_runtime_mode_change = datetime.now(timezone.utc).isoformat()
            if self._runtime_mode in {"SAFE_MODE", "BLOCKED"}:
                notes = (
                    f"{previous_mode}->{self._runtime_mode}: "
                    f"{self._safe_mode_reason() or self._blocked_reason or 'operator attention required'}"
                )
                self._record_runtime_incident(
                    sample_time=self._last_runtime_mode_change,
                    notes=notes,
                    alert_level="critical" if self._runtime_mode == "BLOCKED" else "warning",
                )
        self._persist_runtime_state()

    def _set_safe_mode_flag(self, reason: str, enabled: bool) -> None:
        """Track runtime guard flags.

        Flags listed in `_PER_SYMBOL_SAFE_MODE_FLAGS` block only the affected symbols
        and move the runtime into `LIVE_WITH_SYMBOL_BLOCKS`; all other flags are
        treated as portfolio-wide SAFE_MODE causes.
        """
        if enabled:
            self._safe_mode_flags.add(reason)
        else:
            self._safe_mode_flags.discard(reason)
        self._recompute_runtime_mode()

    def _set_blocked_reason(self, reason: str) -> None:
        self._blocked_reason = reason
        self._recompute_runtime_mode()

    def _install_signal_handlers(self) -> None:
        if self._loop is None:
            return
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                self._loop.add_signal_handler(
                    signum,
                    lambda current=signum: asyncio.create_task(
                        self.shutdown(reason=f"signal:{current.name.lower()}")
                    ),
                )
            except (NotImplementedError, RuntimeError, ValueError):
                try:
                    loop = self._loop
                    if loop is None:
                        continue
                    signal.signal(
                        signum,
                        lambda *_args, current=signum, current_loop=loop: current_loop.call_soon_threadsafe(
                            lambda: asyncio.create_task(
                                self.shutdown(reason=f"signal:{current.name.lower()}")
                            )
                        ),
                    )
                except Exception:
                    continue

    async def _sleep_or_shutdown(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._shutdown_event.wait(), timeout=max(0.0, seconds))
            return True
        except asyncio.TimeoutError:
            return False

    def _signed_timestamp_ms(self) -> int:
        return int(time.time() * 1000) + self._binance_time_offset_ms

    async def _sync_binance_time(self) -> None:
        now_monotonic = time.monotonic()
        if now_monotonic < self._binance_time_sync_expires_at_monotonic:
            return
        response = await asyncio.to_thread(
            requests.get,
            f"{self._futures_base_url}/fapi/v1/time",
            timeout=10,
        )
        response.raise_for_status()
        server_time = int(response.json()["serverTime"])
        self._binance_time_offset_ms = server_time - int(time.time() * 1000)
        self._binance_time_sync_expires_at_monotonic = now_monotonic + _BINANCE_TIME_SYNC_TTL_S

    async def _signed_get_json(
        self,
        *,
        base_url: str,
        endpoint: str,
        params: dict[str, str | int | float] | None = None,
        api_key: str,
        api_secret: str,
    ):
        return await self._signed_request_json(
            method="GET",
            base_url=base_url,
            endpoint=endpoint,
            params=params,
            api_key=api_key,
            api_secret=api_secret,
        )

    async def _signed_delete_json(
        self,
        *,
        base_url: str,
        endpoint: str,
        params: dict[str, str | int | float] | None = None,
        api_key: str,
        api_secret: str,
    ):
        return await self._signed_request_json(
            method="DELETE",
            base_url=base_url,
            endpoint=endpoint,
            params=params,
            api_key=api_key,
            api_secret=api_secret,
        )

    @staticmethod
    def _binance_response_detail(response, payload=None) -> str:
        data = payload
        if data is None:
            try:
                data = response.json()
            except ValueError:
                data = None

        if isinstance(data, dict):
            code = data.get("code")
            msg = data.get("msg")
            if code is not None or msg is not None:
                return f"code={code} msg={msg}"

        raw_text = str(getattr(response, "text", "") or "").strip()
        preview = " ".join(raw_text.split())
        if len(preview) > 240:
            preview = preview[:237] + "..."
        return preview or "empty response body"

    @staticmethod
    def _supports_signed_get_fallback(exc: Exception) -> bool:
        message = str(exc)
        return "HTTP 400" in message or "HTTP 404" in message

    async def _signed_get_json_with_fallback(
        self,
        *,
        base_url: str,
        endpoints: tuple[str, ...],
        params: dict[str, str | int | float] | None = None,
        api_key: str,
        api_secret: str,
    ):
        last_exc: Exception | None = None
        for index, endpoint in enumerate(endpoints):
            try:
                return await self._signed_get_json(
                    base_url=base_url,
                    endpoint=endpoint,
                    params=params,
                    api_key=api_key,
                    api_secret=api_secret,
                )
            except Exception as exc:
                last_exc = exc
                has_fallback = index + 1 < len(endpoints)
                if not has_fallback or not self._supports_signed_get_fallback(exc):
                    raise
                logger.warning(
                    "Signed GET %s failed (%s); retrying %s",
                    endpoint,
                    exc,
                    endpoints[index + 1],
                )

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Signed GET fallback exhausted without attempting any endpoint")

    async def _signed_request_json(
        self,
        *,
        method: str,
        base_url: str,
        endpoint: str,
        params: dict[str, str | int | float] | None = None,
        api_key: str,
        api_secret: str,
    ):
        if not api_key or not api_secret:
            raise RuntimeError(f"Missing Binance credentials for signed request {endpoint}")

        query_params: dict[str, str | int | float] = dict(params or {})
        query_params["recvWindow"] = int(query_params.get("recvWindow", _SIGNED_RECV_WINDOW_MS))
        query_params["timestamp"] = self._signed_timestamp_ms()
        query_string = urlencode(query_params)
        signature = hmac.new(
            api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        url = f"{base_url}{endpoint}?{query_string}&signature={signature}"
        request_fn = requests.get if method.upper() == "GET" else requests.delete
        response = await asyncio.to_thread(
            request_fn,
            url,
            headers={"X-MBX-APIKEY": api_key},
            timeout=10,
        )
        if response.status_code >= 400:
            payload = None
            try:
                payload = response.json()
            except ValueError:
                payload = None
            self._raise_binance_request_error(
                endpoint=endpoint,
                response=response,
                payload=payload,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise self._invalid_binance_json_error(
                endpoint=endpoint,
                response=response,
                exc=exc,
            ) from exc
        if isinstance(payload, dict):
            code = self._binance_error_code(payload)
            if code is not None and code < 0:
                self._raise_binance_request_error(
                    endpoint=endpoint,
                    response=response,
                    payload=payload,
                )
        return payload

    def _signed_request_json_sync(
        self,
        *,
        method: str,
        base_url: str,
        endpoint: str,
        params: dict[str, str | int | float] | None = None,
        api_key: str,
        api_secret: str,
    ):
        if not api_key or not api_secret:
            raise RuntimeError(f"Missing Binance credentials for signed request {endpoint}")

        query_params: dict[str, str | int | float] = dict(params or {})
        query_params["recvWindow"] = int(query_params.get("recvWindow", _SIGNED_RECV_WINDOW_MS))
        query_params["timestamp"] = self._signed_timestamp_ms()
        query_string = urlencode(query_params)
        signature = hmac.new(
            api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        url = f"{base_url}{endpoint}?{query_string}&signature={signature}"
        response = requests.request(
            method.upper(),
            url,
            headers={"X-MBX-APIKEY": api_key},
            timeout=10,
        )
        if response.status_code >= 400:
            payload = None
            try:
                payload = response.json()
            except ValueError:
                payload = None
            self._raise_binance_request_error(
                endpoint=endpoint,
                response=response,
                payload=payload,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise self._invalid_binance_json_error(
                endpoint=endpoint,
                response=response,
                exc=exc,
            ) from exc
        if isinstance(payload, dict):
            code = self._binance_error_code(payload)
            if code is not None and code < 0:
                self._raise_binance_request_error(
                    endpoint=endpoint,
                    response=response,
                    payload=payload,
                )
        return payload

    async def _public_get_json(self, url: str):
        response = await asyncio.to_thread(requests.get, url, timeout=10)
        response.raise_for_status()
        return response.json()

    async def _ping_exchange(self) -> None:
        errors: list[str] = []
        for label, url in (
            ("futures_time", f"{self._futures_base_url}/fapi/v1/time"),
            ("spot_ping", f"{self._spot_base_url}/api/v3/ping"),
        ):
            last_exc: Exception | None = None
            for attempt in range(3):
                try:
                    await self._public_get_json(url)
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    await asyncio.sleep(0.5 * (2 ** attempt))
            if last_exc is not None:
                errors.append(f"{label}: {last_exc}")
        if errors:
            raise RuntimeError("; ".join(errors))

    def _validate_required_credentials(self) -> None:
        if self._trading_mode == "paper":
            return
        missing = []
        if not self._futures_api_key:
            missing.append("BINANCE_API_KEY")
        if not self._futures_api_secret:
            missing.append("BINANCE_API_SECRET")
        if not self._spot_api_key:
            missing.append("BINANCE_SPOT_API_KEY")
        if not self._spot_api_secret:
            missing.append("BINANCE_SPOT_API_SECRET")
        if missing:
            raise StartupBlockedError(f"Missing required Binance credentials: {', '.join(missing)}")

    async def _db_write_probe(self) -> None:
        self.state_writer.set_stat("preflight_db_probe", time.time())

    async def _wait_for_heartbeat_ack_once(self, timeout_s: float = _STARTUP_HEARTBEAT_TIMEOUT_S) -> bool:
        heartbeat_id = f"hb_{uuid.uuid4().hex[:12]}"
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", 9000)
        except Exception as exc:
            logger.warning("Preflight heartbeat could not connect to Rust TCP bridge: %s", exc)
            return False

        try:
            deadline = time.monotonic() + timeout_s
            send_interval_s = 0.35
            next_send_at = time.monotonic()
            while time.monotonic() < deadline:
                now = time.monotonic()
                if now >= next_send_at:
                    self.execution.send_heartbeat(heartbeat_id)
                    next_send_at = now + send_interval_s
                remaining = min(next_send_at - time.monotonic(), deadline - time.monotonic())
                remaining = max(0.1, remaining)
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=remaining)
                except asyncio.TimeoutError:
                    continue
                if not line:
                    return False
                try:
                    event = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                if event.get("event") == "HeartbeatAck" and event.get("heartbeat_id") == heartbeat_id:
                    self._on_heartbeat_ack(
                        heartbeat_id=event.get("heartbeat_id"),
                        status=event.get("status", ""),
                        ts_ms=event.get("ts_ms"),
                    )
                    return True
        except Exception as exc:
            logger.warning("Preflight heartbeat failed: %s", exc)
            return False
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        return False

    async def _cancel_open_orders(self, orders: list[dict], *, futures: bool) -> list[str]:
        failures: list[str] = []
        for order in orders:
            symbol = str(order.get("symbol", "")).upper()
            if not symbol:
                continue
            params: dict[str, str | int | float] = {"symbol": symbol}
            client_order_id = str(order.get("clientOrderId", "")).strip()
            if client_order_id:
                params["origClientOrderId"] = client_order_id
            elif order.get("orderId") is not None:
                params["orderId"] = int(order["orderId"])
            else:
                failures.append(symbol)
                continue
            try:
                await self._signed_delete_json(
                    base_url=self._futures_base_url if futures else self._spot_base_url,
                    endpoint="/fapi/v1/order" if futures else "/api/v3/order",
                    params=params,
                    api_key=self._futures_api_key if futures else self._spot_api_key,
                    api_secret=self._futures_api_secret if futures else self._spot_api_secret,
                )
            except Exception as exc:
                failures.append(f"{symbol}:{exc}")
        return failures

    async def _cancel_exit_orders_for_symbol(self, symbol: str, snapshot: dict) -> bool:
        """Cancel all open futures and spot orders for symbol. Returns True if all cancels succeeded."""
        target = symbol.upper()
        futures_orders = [
            o for o in (snapshot.get("futures_open_orders") or [])
            if isinstance(o, dict) and str(o.get("symbol", "")).upper() == target
        ]
        spot_orders = [
            o for o in (snapshot.get("spot_open_orders") or [])
            if isinstance(o, dict) and str(o.get("symbol", "")).upper() == target
        ]
        if not futures_orders and not spot_orders:
            return True
        failures: list[str] = []
        if futures_orders:
            failures.extend(await self._cancel_open_orders(futures_orders, futures=True))
        if spot_orders:
            failures.extend(await self._cancel_open_orders(spot_orders, futures=False))
        if failures:
            logger.warning("Failed to cancel orders for %s: %s", symbol, failures)
            return False
        return True

    async def _cancel_enter_orders_for_symbol(self, symbol: str, snapshot: dict) -> bool:
        """Cancel all open entry orders (spot + futures) for symbol. Returns True if all cancels succeeded."""
        target = symbol.upper()
        futures_orders = [
            o for o in (snapshot.get("futures_open_orders") or [])
            if isinstance(o, dict) and str(o.get("symbol", "")).upper() == target
        ]
        spot_orders = [
            o for o in (snapshot.get("spot_open_orders") or [])
            if isinstance(o, dict) and str(o.get("symbol", "")).upper() == target
        ]
        if not futures_orders and not spot_orders:
            return True
        failures: list[str] = []
        if futures_orders:
            failures.extend(await self._cancel_open_orders(futures_orders, futures=True))
        if spot_orders:
            failures.extend(await self._cancel_open_orders(spot_orders, futures=False))
        if failures:
            logger.warning("Failed to cancel ENTER orders for %s: %s", symbol, failures)
            return False
        return True

    @staticmethod
    def _snapshot_open_orders(snapshot: dict | None) -> list[dict]:
        if not isinstance(snapshot, dict):
            return []
        return [
            order
            for order in list(snapshot.get("futures_open_orders") or []) + list(snapshot.get("spot_open_orders") or [])
            if isinstance(order, dict)
        ]

    @staticmethod
    def _open_order_symbols(open_orders: list[dict]) -> list[str]:
        return sorted(
            {
                str(order.get("symbol", "")).upper()
                for order in open_orders
                if order.get("symbol")
            }
        )

    async def _clear_startup_open_orders(self, snapshot: dict, *, stage: str) -> dict:
        open_orders = self._snapshot_open_orders(snapshot)
        if not open_orders:
            return snapshot

        order_symbols = self._open_order_symbols(open_orders)
        logger.warning(
            "%s found %d open exchange order(s) for %s; cancelling them before reconciliation",
            stage,
            len(open_orders),
            ", ".join(order_symbols or ["unknown"]),
        )

        futures_orders = [
            order for order in (snapshot.get("futures_open_orders") or [])
            if isinstance(order, dict)
        ]
        spot_orders = [
            order for order in (snapshot.get("spot_open_orders") or [])
            if isinstance(order, dict)
        ]
        cancel_failures: list[str] = []
        if futures_orders:
            cancel_failures.extend(await self._cancel_open_orders(futures_orders, futures=True))
        if spot_orders:
            cancel_failures.extend(await self._cancel_open_orders(spot_orders, futures=False))

        if cancel_failures:
            self.state_writer.set_risk_snapshot(
                {
                    "startup_reconciliation_status": "blocked_open_orders",
                    "startup_reconciliation_time": datetime.now(timezone.utc).isoformat(),
                    "startup_reconciliation_open_order_symbols": order_symbols,
                    "startup_reconciliation_open_order_count": len(open_orders),
                    "startup_reconciliation_open_order_cancel_failures": cancel_failures,
                    "allow_new_risk": False,
                    "reasons": [
                        f"{stage.lower()} blocked: failed to cancel exchange open orders",
                    ],
                }
            )
            reason = (
                f"{stage.lower()} blocked: failed to cancel "
                f"{len(cancel_failures)} exchange open order(s)"
            )
            self._set_blocked_reason(reason)
            raise StartupBlockedError(reason)

        await asyncio.sleep(0.5)
        refreshed_snapshot = await self._fetch_exchange_startup_snapshot()
        remaining_orders = self._snapshot_open_orders(refreshed_snapshot)
        if remaining_orders:
            remaining_symbols = self._open_order_symbols(remaining_orders)
            self.state_writer.set_risk_snapshot(
                {
                    "startup_reconciliation_status": "blocked_open_orders",
                    "startup_reconciliation_time": datetime.now(timezone.utc).isoformat(),
                    "startup_reconciliation_open_order_symbols": remaining_symbols,
                    "startup_reconciliation_open_order_count": len(remaining_orders),
                    "startup_reconciliation_cleared_open_order_symbols": order_symbols,
                    "startup_reconciliation_cleared_open_order_count": len(open_orders),
                    "allow_new_risk": False,
                    "reasons": [
                        f"{stage.lower()} blocked: exchange still reports open orders after cleanup",
                    ],
                }
            )
            reason = (
                f"{stage.lower()} blocked: exchange still reports "
                f"{len(remaining_orders)} open order(s) after cleanup"
            )
            self._set_blocked_reason(reason)
            raise StartupBlockedError(reason)

        self.state_writer.set_risk_snapshot(
            {
                "startup_reconciliation_cleared_open_order_symbols": order_symbols,
                "startup_reconciliation_cleared_open_order_count": len(open_orders),
                "startup_reconciliation_time": datetime.now(timezone.utc).isoformat(),
            }
        )
        logger.warning(
            "%s cancelled %d stale exchange open order(s) for %s",
            stage,
            len(open_orders),
            ", ".join(order_symbols or ["unknown"]),
        )
        return refreshed_snapshot

    async def _resolve_pending_intents_from_exchange(self, snapshot: dict) -> None:
        pending_rows = self.state_reader.get_pending_intents(
            statuses=["DISPATCHING", "PENDING_ACK", "TIMEOUT", "NEW", "FILLED"]
        )
        if not pending_rows:
            return

        futures_open_orders = snapshot.get("futures_open_orders") or []
        spot_open_orders = snapshot.get("spot_open_orders") or []
        open_order_symbols = {
            str(order.get("symbol", "")).upper()
            for order in list(futures_open_orders) + list(spot_open_orders)
            if isinstance(order, dict) and order.get("symbol")
        }
        position_symbols = {
            str(position.get("symbol", "")).upper()
            for position in self._open_snapshot_position_rows(snapshot)
        }

        for row in pending_rows:
            symbol = str(row.get("symbol", "")).upper()
            intent_type = str(row.get("intent_type", "")).upper()
            intent_id = str(row.get("intent_id", ""))
            if intent_type.startswith("ENTER"):
                if symbol in position_symbols or symbol not in open_order_symbols:
                    self.state_writer.delete_pending_intent(intent_id)
                else:
                    raise StartupBlockedError(
                        f"Unresolved pending ENTER after recovery: {symbol}:{intent_type} â€” "
                        "exchange has an open order with no matching position"
                    )
            elif intent_type.startswith("EXIT"):
                if symbol not in position_symbols and symbol not in open_order_symbols:
                    # Position already closed â€” intent is stale, clean it up.
                    self.state_writer.delete_pending_intent(intent_id)
                else:
                    # Position still open. Delete the stale intent and let the trading
                    # loop re-dispatch the exit on the first cycle. Blocking here causes
                    # a permanent deadlock: the position needs to be exited but the
                    # trader can't start to exit it.
                    logger.critical(
                        "Startup recovery: %s has confirmed open position but stale EXIT intent "
                        "(%s). Clearing intent â€” trading loop will re-dispatch exit.",
                        symbol, intent_id,
                    )
                    self.state_writer.delete_pending_intent(intent_id)

    async def _run_preflight(self) -> None:
        self._preflight_status = "running"
        self._persist_runtime_state()
        try:
            await self._db_write_probe()
            self._validate_required_credentials()

            if self._trading_mode != "paper":
                await self._ping_exchange()
                await self._sync_binance_time()
                await self._signed_get_json_with_fallback(
                    base_url=self._futures_base_url,
                    endpoints=("/fapi/v3/account", "/fapi/v2/account"),
                    api_key=self._futures_api_key,
                    api_secret=self._futures_api_secret,
                )
                await self._signed_get_json(
                    base_url=self._spot_base_url,
                    endpoint="/api/v3/account",
                    api_key=self._spot_api_key,
                    api_secret=self._spot_api_secret,
                )

            if not await self._wait_for_heartbeat_ack_once(timeout_s=_STARTUP_HEARTBEAT_TIMEOUT_S):
                self._preflight_status = "blocked_execution_bridge"
                self._set_blocked_reason("execution bridge preflight failed")
                raise StartupBlockedError("Rust execution bridge preflight failed")

            if self._trading_mode != "paper":
                snapshot = await self._fetch_exchange_startup_snapshot()
                snapshot = await self._clear_startup_open_orders(snapshot, stage="Startup preflight")
                await self._resolve_pending_intents_from_exchange(snapshot)

            self._preflight_status = "passed"
            self._persist_runtime_state()
        except StartupBlockedError:
            raise
        except Exception as exc:
            self._preflight_status = "blocked_preflight"
            self._set_blocked_reason(str(exc))
            raise StartupBlockedError(str(exc)) from exc

    def _direction_from_futures_position(self, position_amt: float, position_side: str) -> str:
        side = position_side.upper()
        if side == "SHORT":
            return "long"
        if side == "LONG":
            return "short"
        return "long" if position_amt < 0.0 else "short"

    def _build_spot_balance_map(self, spot_account: dict | None) -> dict[str, float]:
        if not isinstance(spot_account, dict):
            return {}
        balances: dict[str, float] = {}
        for balance in spot_account.get("balances", []):
            asset = str(balance.get("asset", "")).upper()
            total = _float_or_zero(balance.get("free")) + _float_or_zero(balance.get("locked"))
            if asset and total > _POSITION_QTY_TOLERANCE:
                balances[asset] = total
        return balances

    @staticmethod
    def _open_account_position_rows(futures_account: dict | None) -> list[dict]:
        if not isinstance(futures_account, dict):
            return []
        rows: list[dict] = []
        for raw_position in futures_account.get("positions", []):
            if not isinstance(raw_position, dict):
                continue
            symbol = str(raw_position.get("symbol", "")).upper()
            if not symbol:
                continue
            normalized = dict(raw_position)
            normalized["symbol"] = symbol
            if normalized.get("unRealizedProfit") is None and normalized.get("unrealizedProfit") is not None:
                normalized["unRealizedProfit"] = normalized.get("unrealizedProfit")
            if abs(_float_or_zero(normalized.get("positionAmt"))) <= _POSITION_QTY_TOLERANCE:
                continue
            rows.append(normalized)
        return rows

    def _open_snapshot_position_rows(self, snapshot: dict | None) -> list[dict]:
        if not isinstance(snapshot, dict):
            return []
        merged_rows: dict[str, dict] = {}
        for raw_position in self._open_account_position_rows(snapshot.get("futures_account")):
            merged_rows[str(raw_position.get("symbol", "")).upper()] = dict(raw_position)
        for raw_position in snapshot.get("position_risk") or []:
            if not isinstance(raw_position, dict):
                continue
            symbol = str(raw_position.get("symbol", "")).upper()
            if not symbol:
                continue
            normalized = dict(raw_position)
            normalized["symbol"] = symbol
            if normalized.get("unRealizedProfit") is None and normalized.get("unrealizedProfit") is not None:
                normalized["unRealizedProfit"] = normalized.get("unrealizedProfit")
            if abs(_float_or_zero(normalized.get("positionAmt"))) <= _POSITION_QTY_TOLERANCE:
                continue
            merged = dict(merged_rows.get(symbol, {}))
            merged.update(normalized)
            merged_rows[symbol] = merged
        return list(merged_rows.values())

    @staticmethod
    def _perp_leg_open_pnl(
        *,
        qty: float,
        direction: str,
        perp_entry: float,
        perp_live: float,
    ) -> float:
        if qty <= 0.0 or perp_entry <= 0.0 or perp_live <= 0.0:
            return 0.0
        if direction == "short":
            return (perp_live - perp_entry) * qty
        return (perp_entry - perp_live) * qty

    def _classify_startup_recovered_position(
        self,
        *,
        symbol: str,
        direction: str,
        ann_funding: float,
        hedge_ratio: float,
        unsupported_direction: bool,
        funding_signal_available: bool,
    ) -> tuple[str, str]:
        if unsupported_direction:
            return (
                "manual_review",
                f"{symbol} recovered with inverse/long-perp structure that this runtime cannot safely rebuild",
            )
        if direction == "long" and hedge_ratio < (1.0 - _SPOT_HEDGE_SHORTFALL_TOLERANCE_PCT):
            return (
                "manual_review",
                f"{symbol} recovered with only {hedge_ratio:.2%} of the required spot hedge on exchange",
            )
        if not funding_signal_available:
            return (
                "tracked",
                f"{symbol} recovered while funding data is stale; holding until fresh rates arrive",
            )
        if self._funding_has_decayed(direction, ann_funding):
            return (
                "exit_candidate",
                f"{symbol} funding decayed to {ann_funding * 100:.2f}% annualized and should be exited",
            )
        return (
            "tracked",
            f"{symbol} still passes the funding exit gate at {ann_funding * 100:.2f}% annualized",
        )

    def _track_recovery_action(self, symbol: str, recovery_state: str, recovery_note: str) -> None:
        if recovery_state == "exit_candidate":
            self._startup_exit_candidates[symbol] = recovery_note
        else:
            self._startup_exit_candidates.pop(symbol, None)
        if recovery_state == "manual_review":
            self._startup_manual_review_symbols[symbol] = recovery_note
        else:
            self._startup_manual_review_symbols.pop(symbol, None)

    def _classify_live_recovered_position(
        self,
        *,
        symbol: str,
        direction: str,
        qty: float,
        ann_funding: float,
        spot_balances: dict[str, float] | None = None,
    ) -> tuple[float, str, str]:
        hedge_ratio = 0.0
        if direction == "long":
            base_asset = _extract_base_asset(symbol)
            spot_qty = max(0.0, _float_or_zero((spot_balances or {}).get(base_asset)))
            hedge_ratio = min(1.0, max(0.0, spot_qty / qty)) if qty > _POSITION_QTY_TOLERANCE else 1.0

        recovery_state, recovery_note = self._classify_startup_recovered_position(
            symbol=symbol,
            direction=direction,
            ann_funding=ann_funding,
            hedge_ratio=hedge_ratio,
            unsupported_direction=direction != "long",
            funding_signal_available=(
                self.funding_ranker.status_snapshot().get("funding_staleness_status") == "fresh"
            ),
        )
        return hedge_ratio, recovery_state, recovery_note

    def _refresh_startup_recovery_flags(self, rows: list[dict] | None = None) -> None:
        rows = rows if rows is not None else self.state_reader.get_positions()
        row_map = {
            str(row.get("symbol", "")).upper(): row
            for row in rows
            if row.get("symbol")
        }
        self._startup_exit_candidates = {
            symbol: self._startup_exit_candidates.get(
                symbol,
                f"{symbol} recovered position remains an exit candidate until it is closed",
            )
            for symbol, row in row_map.items()
            if str(row.get("recovery_state") or "").strip().lower() == "exit_candidate"
        }
        self._startup_manual_review_symbols = {
            symbol: self._startup_manual_review_symbols.get(
                symbol,
                f"{symbol} recovered position still requires manual review",
            )
            for symbol, row in row_map.items()
            if str(row.get("recovery_state") or "").strip().lower() == "manual_review"
        }
        active_recovery_symbols = set(self._startup_exit_candidates) | set(self._startup_manual_review_symbols)
        self._startup_recovery_last_attempt_monotonic = {
            symbol: attempted_at
            for symbol, attempted_at in self._startup_recovery_last_attempt_monotonic.items()
            if symbol in active_recovery_symbols
        }
        self._startup_recovery_consecutive_failures = {
            symbol: failures
            for symbol, failures in self._startup_recovery_consecutive_failures.items()
            if symbol in active_recovery_symbols
        }
        self._startup_recovery_stuck_symbols = {
            symbol: reason
            for symbol, reason in self._startup_recovery_stuck_symbols.items()
            if symbol in self._startup_manual_review_symbols
        }
        self._set_safe_mode_flag("startup_exit_candidate", bool(self._startup_exit_candidates))
        self._set_safe_mode_flag(
            "startup_manual_review",
            self._manual_review_requires_distinct_safe_mode(rows),
        )
        self._set_safe_mode_flag("naked_leg_unwind_stuck", bool(self._startup_recovery_stuck_symbols))

    def _manual_review_requires_distinct_safe_mode(self, rows: list[dict] | None = None) -> bool:
        del rows
        # Startup manual-review positions are operator-actionable on their own.
        # Do not suppress this flag just because the same symbol also has a hedge gap.
        return bool(self._startup_manual_review_symbols)

    def _dispatch_startup_recovery_exits(self, rows: list[dict] | None = None) -> int:
        rows = rows if rows is not None else self.state_reader.get_positions()
        row_map = {
            str(row.get("symbol", "")).upper(): row
            for row in rows
            if row.get("symbol")
        }
        auto_exit_manual_review = bool(self._config.get("startup_recovery_auto_exit_manual_review"))
        grace_elapsed = self._startup_recovery_auto_exit_grace_elapsed()
        auto_exit_reasons: dict[str, str] = {}
        deferred_zero_hedge_symbols: list[str] = []
        for symbol, row in row_map.items():
            if str(row.get("recovery_state") or "").strip().lower() != "manual_review":
                continue
            hedge_ratio = _float_or_zero(row.get("hedge_ratio"))
            if hedge_ratio <= _POSITION_QTY_TOLERANCE:
                if not auto_exit_manual_review:
                    continue
                if not grace_elapsed:
                    deferred_zero_hedge_symbols.append(symbol)
                    continue
                auto_exit_reasons[symbol] = (
                    f"{symbol} (qty={_float_or_zero(row.get('qty')):.8f}, hedge={hedge_ratio:.4f}) "
                    "auto-close naked leg"
                )
                continue
            if auto_exit_manual_review and hedge_ratio < (1.0 - _SPOT_HEDGE_SHORTFALL_TOLERANCE_PCT):
                if not grace_elapsed:
                    continue
                auto_exit_reasons[symbol] = (
                    f"{symbol} has only {hedge_ratio:.2%} of the required spot hedge; "
                    "auto-exit is enabled for manual-review positions"
                )
        if deferred_zero_hedge_symbols:
            logger.info(
                "Startup recovery: deferring zero-hedge manual-review exits for %s until startup grace completes",
                ", ".join(sorted(deferred_zero_hedge_symbols)),
            )
        candidate_reasons = dict(self._startup_exit_candidates)
        candidate_reasons.update(auto_exit_reasons)
        dispatched = 0
        for symbol, reason in candidate_reasons.items():
            row = row_map.get(symbol.upper())
            if row is None:
                self._startup_exit_candidates.pop(symbol, None)
                self._clear_startup_recovery_exit_tracking(symbol)
                continue
            if symbol in self._exit_events:
                continue
            if symbol in self._startup_recovery_stuck_symbols:
                logger.warning(
                    "Startup recovery: holding %s after repeated unwind failures (%s)",
                    symbol,
                    self._startup_recovery_stuck_symbols[symbol],
                )
                continue
            if not self._startup_recovery_attempt_allowed(symbol):
                continue
            recovery_state = str(row.get("recovery_state") or "").strip().lower()
            if symbol in auto_exit_reasons:
                if recovery_state != "manual_review":
                    continue
            elif recovery_state != "exit_candidate":
                self._startup_exit_candidates.pop(symbol, None)
                continue
            direction = str(row.get("direction") or self._position_directions.get(symbol) or "long")
            logger.info("Startup recovery: exiting %s (%s)", symbol, reason)
            self._record_startup_recovery_exit_attempt(symbol)
            self._dispatch_exit(symbol, urgency=0.9, direction=direction)
            dispatched += 1
        self._refresh_startup_recovery_flags(rows)
        return dispatched

    async def _fetch_exchange_startup_snapshot(self) -> dict:
        await self._sync_binance_time()
        futures_account, position_risk, futures_open_orders = await asyncio.gather(
            self._signed_get_json_with_fallback(
                base_url=self._futures_base_url,
                endpoints=("/fapi/v3/account", "/fapi/v2/account"),
                api_key=self._futures_api_key,
                api_secret=self._futures_api_secret,
            ),
            self._signed_get_json_with_fallback(
                base_url=self._futures_base_url,
                endpoints=("/fapi/v3/positionRisk", "/fapi/v2/positionRisk"),
                api_key=self._futures_api_key,
                api_secret=self._futures_api_secret,
            ),
            self._signed_get_json(
                base_url=self._futures_base_url,
                endpoint="/fapi/v1/openOrders",
                api_key=self._futures_api_key,
                api_secret=self._futures_api_secret,
            ),
        )

        spot_account = None
        spot_open_orders: list[dict] = []
        try:
            spot_account, spot_open_orders = await asyncio.gather(  # type: ignore[assignment]
                self._signed_get_json(
                    base_url=self._spot_base_url,
                    endpoint="/api/v3/account",
                    api_key=self._spot_api_key,
                    api_secret=self._spot_api_secret,
                ),
                self._signed_get_json(
                    base_url=self._spot_base_url,
                    endpoint="/api/v3/openOrders",
                    api_key=self._spot_api_key,
                    api_secret=self._spot_api_secret,
                ),
            )
        except Exception as exc:
            logger.warning("Spot snapshot unavailable during startup reconciliation: %s", exc)

        funding_income: list[dict] = []
        try:
            funding_income = await self._signed_get_json(  # type: ignore[assignment]
                base_url=self._futures_base_url,
                endpoint="/fapi/v1/income",
                params={"incomeType": "FUNDING_FEE", "limit": 20},
                api_key=self._futures_api_key,
                api_secret=self._futures_api_secret,
            )
        except Exception as exc:
            logger.warning("Funding income snapshot unavailable during startup reconciliation: %s", exc)

        return {
            "futures_account": futures_account,
            "position_risk": position_risk,
            "futures_open_orders": futures_open_orders,
            "spot_account": spot_account,
            "spot_open_orders": spot_open_orders,
            "funding_income": funding_income,
        }

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float:
        if not values:
            return 0.0
        if len(values) == 1:
            return float(values[0])
        ordered = sorted(float(value) for value in values)
        rank = max(0.0, min(100.0, percentile)) / 100.0 * (len(ordered) - 1)
        lower = int(math.floor(rank))
        upper = int(math.ceil(rank))
        if lower == upper:
            return ordered[lower]
        weight = rank - lower
        return ordered[lower] * (1.0 - weight) + ordered[upper] * weight

    def _refresh_adaptive_state(self) -> None:
        recent_trades = self.state_reader.get_trades(limit=200, session_scoped=False)
        min_hold_hours = float(self._config.get("loss_streak_min_hold_hours"))
        loss_streak = 0
        win_streak = 0
        for trade in recent_trades:
            # Skip trades that were closed too quickly to be strategy-driven
            # (forced exits from risk engine / bridge errors typically last < 1 min).
            entry_time_str = str(trade.get("entry_time") or "")
            exit_time_str = str(trade.get("exit_time") or "")
            if entry_time_str and exit_time_str:
                try:
                    entry_dt = datetime.fromisoformat(entry_time_str.replace("Z", "+00:00"))
                    exit_dt = datetime.fromisoformat(exit_time_str.replace("Z", "+00:00"))
                    if entry_dt.tzinfo is None:
                        entry_dt = entry_dt.replace(tzinfo=timezone.utc)
                    if exit_dt.tzinfo is None:
                        exit_dt = exit_dt.replace(tzinfo=timezone.utc)
                    hold_hours = (exit_dt - entry_dt).total_seconds() / 3600.0
                    if hold_hours < min_hold_hours:
                        continue
                except (ValueError, TypeError):
                    pass
            pnl = _float_or_zero(trade.get("net_pnl_usd"))
            if pnl < 0.0 and win_streak == 0:
                loss_streak += 1
            elif pnl > 0.0 and loss_streak == 0:
                win_streak += 1
            else:
                break

        self._loss_streak = loss_streak
        self._win_streak = win_streak
        if self._loss_streak >= int(self._config.get("loss_streak_trigger")):
            self._streak_notional_scale = float(self._config.get("loss_streak_notional_scale"))
        else:
            self._streak_notional_scale = 1.0

        adaptive_enabled = self._adaptive_controls_enabled()
        adaptive_entry_base = float(self._config.get("entry_ann_funding_threshold"))
        adaptive_rotation_gap = ROTATION_MIN_GAP_ANN
        if adaptive_enabled:
            since = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
            market_samples = self.state_reader.get_market_samples(since=since, limit=100_000)
            funding_values = [
                abs(_float_or_zero(sample.get("ann_funding")))
                for sample in market_samples
                if abs(_float_or_zero(sample.get("ann_funding"))) > 0.0
            ]
            if funding_values:
                adaptive_entry_base = self._percentile(funding_values, 75.0)
                if len(funding_values) >= 2:
                    adaptive_rotation_gap = max(
                        ROTATION_MIN_GAP_ANN,
                        pstdev(funding_values) * 1.5,
                    )

        self._adaptive_entry_threshold_base = adaptive_entry_base
        self._adaptive_rotation_gap = adaptive_rotation_gap
        self.state_writer.set_risk_snapshot(
            {
                "adaptive_entry_threshold_base": adaptive_entry_base,
                "adaptive_rotation_gap": adaptive_rotation_gap,
                "adaptive_controls_active": adaptive_enabled,
                "loss_streak": self._loss_streak,
                "win_streak": self._win_streak,
                "streak_notional_scale": self._streak_notional_scale,
            }
        )

    def _record_health_metric(
        self,
        *,
        metric: str,
        value: float,
        symbol: str | None = None,
        expected_value: float = 0.0,
        notes: str = "",
        sample_time: str | None = None,
    ) -> tuple[str, float | None]:
        recent_samples = self.state_reader.get_health_samples(
            metric=metric,
            symbol=symbol,
            since=(datetime.now(timezone.utc) - timedelta(days=14)).isoformat(),
            limit=2_000,
        )
        values = [
            _float_or_zero(sample.get("value"))
            for sample in recent_samples
            if sample.get("value") is not None
        ]
        zscore = None
        if len(values) >= 20:
            mean_value = fmean(values)
            sigma = pstdev(values)
            if sigma > 1e-9:
                zscore = abs(value - mean_value) / sigma

        alert_level = ""
        if zscore is not None and zscore >= float(self._config.get("health_safe_mode_zscore")):
            alert_level = "critical"
        elif zscore is not None and zscore >= float(self._config.get("health_alert_zscore")):
            alert_level = "warning"

        self.state_writer.record_health_sample(
            metric=metric,
            value=value,
            symbol=symbol,
            expected_value=expected_value,
            zscore=zscore,
            alert_level=alert_level,
            runtime_mode=self._runtime_mode,
            notes=notes,
            sample_time=sample_time,
        )
        return alert_level, zscore

    def _record_market_samples_for_minute(self, sample_minute: str) -> None:
        for symbol in self._live_enriched_symbols():
            minute_key, volume_usd = self._latest_volume_bar.get(symbol, ("", 0.0))
            self.state_writer.record_market_sample(
                symbol=symbol,
                sample_minute=sample_minute,
                ann_funding=self.funding_ranker.get_rate(symbol),
                basis_pct=_float_or_zero(self.depth_tracker.basis_pct(symbol)),
                mark_price=_float_or_zero(
                    self._mark_prices.get(symbol) or self.depth_tracker.perp_mid_price(symbol)
                ),
                minute_notional_volume=volume_usd if minute_key == sample_minute[:16] else 0.0,
            )

    def _record_operator_intervention(self, *, sample_time: str, notes: str, alert_level: str) -> None:
        normalized_notes = notes if str(notes).lower().startswith("manual:") else f"manual:{notes}"
        self.state_writer.record_health_sample(
            metric="operator_intervention_required",
            value=1.0,
            expected_value=0.0,
            alert_level=alert_level,
            runtime_mode=self._runtime_mode,
            notes=normalized_notes,
            sample_time=sample_time,
        )

    def _record_runtime_incident(self, *, sample_time: str, notes: str, alert_level: str) -> None:
        self.state_writer.record_health_sample(
            metric="runtime_intervention_required",
            value=1.0,
            expected_value=0.0,
            alert_level=alert_level,
            runtime_mode=self._runtime_mode,
            notes=notes,
            sample_time=sample_time,
        )

    def _maybe_record_validation_snapshot(self, now: datetime) -> None:
        interval_minutes = max(1, int(VALIDATION_SNAPSHOT_INTERVAL_MINUTES))
        bucket = int(now.timestamp() // (interval_minutes * 60))
        if bucket == self._last_validation_snapshot_bucket:
            return
        self._last_validation_snapshot_bucket = bucket

        metrics = calculate_metrics(self.state_reader)
        snapshot_time = now.replace(second=0, microsecond=0).isoformat()
        self.state_writer.record_validation_snapshot(
            snapshot_time=snapshot_time,
            validation_status=str(metrics.get("validation_status", "UNKNOWN")),
            go_no_go=str(metrics.get("go_no_go", "ADJUST")),
            observation_days=_float_or_zero(metrics.get("observation_days")),
            trade_count=int(metrics.get("trade_count", 0)),
            blockers=list(metrics.get("validation_blockers") or []),
            metrics=metrics,
        )
        self.state_writer.set_risk_snapshot(
            {
                "validation_status": metrics.get("validation_status", "UNKNOWN"),
                "validation_go_no_go": metrics.get("go_no_go", "ADJUST"),
                "validation_observation_days": metrics.get("observation_days", 0.0),
                "validation_intervention_free_days": metrics.get("intervention_free_days", 0.0),
                "validation_blockers": metrics.get("validation_blockers", []),
                "last_validation_snapshot_at": snapshot_time,
            }
        )

    async def _sample_exchange_health(self, sample_time: str) -> bool:
        if self._trading_mode == "paper":
            return False
        try:
            snapshot = await self._fetch_exchange_startup_snapshot()
        except Exception as exc:
            logger.warning("Exchange health sample failed: %s", exc)
            return False

        return self._apply_exchange_position_snapshot(
            snapshot,
            sample_time=sample_time,
            record_health_metrics=True,
            log_prefix="Exchange health sample",
        )

    def _clear_local_position_tracking(self, symbol: str) -> None:
        target = symbol.upper()
        self.state_writer.remove_position(target)
        self._entry_times.pop(target, None)
        self._position_directions.pop(target, None)
        self._estimated_entry_costs.pop(target, None)
        self._startup_exit_candidates.pop(target, None)
        self._startup_manual_review_symbols.pop(target, None)
        self._clear_startup_recovery_exit_tracking(target)
        self._pending_exit_intents.pop(target, None)
        self._pending_exit_created_at.pop(target, None)
        self._stale_pending_exits.discard(target)
        self._stale_exit_resubmit_attempts.pop(target, None)
        self._stale_enter_cancel_attempts.pop(target, None)
        self._abandoned_exit_intents.pop(target, None)
        self._pending_enters.pop(target, None)
        self._stale_pending_enters.pop(target, None)
        self._abandoned_pending_enters.pop(target, None)
        recovery_task = self._entry_failure_recovery_tasks.pop(target, None)
        if recovery_task is not None and not recovery_task.done():
            recovery_task.cancel()
        event = self._exit_events.pop(target, None)
        if event is not None:
            event.set()
        for pending_intent in self.state_reader.get_pending_intents():
            if str(pending_intent.get("symbol", "")).upper() != target:
                continue
            intent_id = str(pending_intent.get("intent_id", "")).strip()
            if intent_id:
                self.state_writer.delete_pending_intent(intent_id)

    def _publish_startup_reconciliation_state(
        self,
        rows: list[dict],
        *,
        audit_time: str,
        snapshot: dict | None = None,
        removed_symbols: list[str] | None = None,
    ) -> None:
        exchange_snapshot = snapshot or {}
        audit_snapshot = self._build_reconciliation_snapshot(
            prefix="audit_reconciliation",
            snapshot=exchange_snapshot,
            rows=rows,
            audit_time=audit_time,
            local_only_symbols=sorted(removed_symbols or []),
            position_source="audit",
        )
        hedge_gap_symbols = list(audit_snapshot["audit_reconciliation_spot_hedge_gaps"])
        risk_snapshot: dict[str, object] = {
            **audit_snapshot,
            "startup_reconciliation_spot_hedge_gaps": hedge_gap_symbols,
            "startup_reconciliation_exit_candidates": sorted(self._startup_exit_candidates),
            "startup_reconciliation_manual_review": sorted(self._startup_manual_review_symbols),
            "startup_reconciliation_recovery_actions": audit_snapshot[
                "audit_reconciliation_recovery_actions"
            ],
            "hedge_gap_symbols": hedge_gap_symbols,
        }
        if removed_symbols is not None:
            risk_snapshot["exchange_position_audit_removed_symbols"] = sorted(removed_symbols)
            risk_snapshot["exchange_position_audit_time"] = audit_time
        self.state_writer.set_risk_snapshot(risk_snapshot)
        self.state_writer.flush()

    def _apply_exchange_position_snapshot(
        self,
        snapshot: dict,
        *,
        sample_time: str,
        record_health_metrics: bool,
        log_prefix: str,
    ) -> bool:
        critical = False

        exchange_equity_snapshot = self._cache_exchange_equity_snapshot(
            account_equity=_derive_futures_account_balance(
                snapshot.get("futures_account"),
                preferred_fields=("totalMarginBalance", "totalWalletBalance"),
                asset_field_name="marginBalance",
            ),
            available_balance=_derive_futures_account_balance(
                snapshot.get("futures_account"),
                preferred_fields=("availableBalance",),
                asset_field_name="availableBalance",
            ),
            captured_at=sample_time,
        )
        if exchange_equity_snapshot:
            self.state_writer.set_risk_snapshot(exchange_equity_snapshot)
        spot_balances = self._build_spot_balance_map(snapshot.get("spot_account"))
        spot_account_available = snapshot.get("spot_account") is not None
        db_positions = {
            str(row.get("symbol", "")).upper(): row
            for row in self.state_reader.get_positions()
            if row.get("symbol")
        }
        exchange_position_symbols: set[str] = set()
        for raw_position in self._open_snapshot_position_rows(snapshot):
            symbol = str(raw_position.get("symbol", "")).upper()
            position_amt = _float_or_zero(raw_position.get("positionAmt"))
            qty = abs(position_amt)
            if not symbol or qty <= _POSITION_QTY_TOLERANCE:
                continue
            exchange_position_symbols.add(symbol)
            direction = self._direction_from_futures_position(
                position_amt,
                str(raw_position.get("positionSide", "BOTH")),
            )
            if direction != "long":
                continue
            base_asset = _extract_base_asset(symbol)
            hedge_ratio = 0.0 if qty <= 0.0 else spot_balances.get(base_asset, 0.0) / qty
            if record_health_metrics:
                alert_level, _ = self._record_health_metric(
                    metric="hedge_ratio",
                    value=hedge_ratio,
                    symbol=symbol,
                    expected_value=1.0,
                    notes="exchange health sample",
                    sample_time=sample_time,
                )
                if alert_level == "critical":
                    critical = True
            # If live spot confirms hedge is intact and the DB has a stale hedge_ratio
            # (e.g. from startup when spot was unavailable), update the DB so the
            # hedge_gap / startup_manual_review safe-mode flags can clear this cycle.
            if spot_account_available and _spot_inventory_covers_hedge(spot_balances.get(base_asset, 0.0), qty):
                db_row = db_positions.get(symbol, {})
                db_hedge_ratio = _float_or_zero(db_row.get("hedge_ratio"))
                db_recovery_state = str(db_row.get("recovery_state") or "")
                live_hr = min(1.0, max(0.0, hedge_ratio))
                if db_hedge_ratio < (1.0 - _SPOT_HEDGE_SHORTFALL_TOLERANCE_PCT) or db_recovery_state == "manual_review":
                    self.state_writer.update_position_metrics(symbol, hedge_ratio=live_hr, recovery_state="")
                    self._startup_manual_review_symbols.pop(symbol, None)
                    logger.info(
                        "Health sample confirmed spot hedge for %s (live hedge_ratio=%.3f); "
                        "cleared stale hedge_gap / startup_manual_review flags",
                        symbol,
                        live_hr,
                    )
        open_order_symbols = {
            str(order.get("symbol", "")).upper()
            for order in self._snapshot_open_orders(snapshot)
            if isinstance(order, dict) and order.get("symbol")
        }
        removed_symbols: list[str] = []
        for symbol in sorted(db_positions):
            if symbol in exchange_position_symbols or symbol in open_order_symbols:
                continue
            self._clear_local_position_tracking(symbol)
            removed_symbols.append(symbol)
            logger.warning(
                "%s removed stale local position for %s because Binance reports it flat with no open order",
                log_prefix,
                symbol,
            )
        rows_after = self.state_reader.get_positions()
        self._refresh_startup_recovery_flags(rows_after)
        self._publish_startup_reconciliation_state(
            rows_after,
            audit_time=sample_time,
            snapshot=snapshot,
            removed_symbols=removed_symbols,
        )
        return critical

    async def _audit_tracked_positions_against_exchange(self, sample_time: str) -> bool:
        if self._trading_mode == "paper":
            return False
        try:
            snapshot = await self._fetch_exchange_startup_snapshot()
        except BinanceSignedCallError as exc:
            if exc.code not in _RECOVERABLE_BINANCE_SIGNED_ERROR_CODES:
                raise
            self._audit_consecutive_failures += 1
            if self._audit_consecutive_failures >= _AUDIT_FAILURE_SAFE_MODE_THRESHOLD:
                self._set_safe_mode_flag("audit_unavailable", True)
                if self._audit_consecutive_failures == _AUDIT_FAILURE_SAFE_MODE_THRESHOLD:
                    logger.critical(
                        "Exchange position audit failed %d times consecutively: %s",
                        self._audit_consecutive_failures,
                        exc,
                    )
            self._set_exchange_position_audit_snapshot(
                status="failed",
                sample_time=sample_time,
                error=str(exc),
                applied=False,
            )
            logger.warning("Exchange position audit failed: %s", exc)
            return False
        except (requests.RequestException, asyncio.TimeoutError, json.JSONDecodeError) as exc:
            self._audit_consecutive_failures += 1
            if self._audit_consecutive_failures >= _AUDIT_FAILURE_SAFE_MODE_THRESHOLD:
                self._set_safe_mode_flag("audit_unavailable", True)
                if self._audit_consecutive_failures == _AUDIT_FAILURE_SAFE_MODE_THRESHOLD:
                    logger.critical(
                        "Exchange position audit failed %d times consecutively: %s",
                        self._audit_consecutive_failures,
                        exc,
                    )
            self._set_exchange_position_audit_snapshot(
                status="failed",
                sample_time=sample_time,
                error=str(exc),
                applied=False,
            )
            logger.warning("Exchange position audit failed: %s", exc)
            return False
        critical = self._apply_exchange_position_snapshot(
            snapshot,
            sample_time=sample_time,
            record_health_metrics=False,
            log_prefix="Exchange position audit",
        )
        self._audit_consecutive_failures = 0
        self._set_safe_mode_flag("audit_unavailable", False)
        self._set_exchange_position_audit_snapshot(
            status="ok",
            sample_time=sample_time,
            error="",
            applied=True,
        )
        return critical

    async def _record_runtime_health(self, sample_time: str) -> None:
        positions = self.state_reader.get_positions()
        self._record_health_metric(
            metric="loop_alive",
            value=1.0,
            expected_value=1.0,
            notes="trader maintenance heartbeat",
            sample_time=sample_time,
        )

        critical_health_detected = False
        for position in positions:
            symbol = str(position.get("symbol", "")).upper()
            if not symbol:
                continue
            alert_level, _ = self._record_health_metric(
                metric="slot_pnl_usd",
                value=_float_or_zero(position.get("net_pnl_usd")),
                symbol=symbol,
                expected_value=0.0,
                notes="open slot pnl",
                sample_time=sample_time,
            )
            if alert_level == "critical":
                critical_health_detected = True

        now_monotonic = time.monotonic()
        tracked_positions_active = bool(
            positions or self._startup_manual_review_symbols or self._startup_exit_candidates
        )
        if (
            self._trading_mode != "paper"
            and self._preflight_status == "passed"
            and self._health_monitor_enabled()
            and now_monotonic - self._last_exchange_health_check_monotonic >= 300.0
        ):
            self._last_exchange_health_check_monotonic = now_monotonic
            critical_health_detected = (
                await self._sample_exchange_health(sample_time) or critical_health_detected
            )
            self._last_exchange_position_audit_monotonic = now_monotonic
        elif (
            self._trading_mode != "paper"
            and self._preflight_status == "passed"
            and tracked_positions_active
            and now_monotonic - self._last_exchange_position_audit_monotonic
            >= (
                _GUARDED_EXCHANGE_POSITION_AUDIT_INTERVAL_S
                if (self._startup_manual_review_symbols or self._startup_exit_candidates)
                else _EXCHANGE_POSITION_AUDIT_INTERVAL_S
            )
        ):
            await self._audit_tracked_positions_against_exchange(sample_time)
            self._last_exchange_position_audit_monotonic = now_monotonic

        self._set_safe_mode_flag(
            "health_monitor",
            self._health_monitor_enabled() and critical_health_detected,
        )

    async def _run_maintenance_loop(self) -> None:
        while not self._shutdown_event.is_set():
            funding_status = self.funding_ranker.status_snapshot()
            now = datetime.now(timezone.utc)
            now_monotonic = time.monotonic()
            sample_minute = now.replace(second=0, microsecond=0).isoformat()
            self._expire_stale_pending_intents()
            await self._self_heal_pending_intents()
            recent_execution_events = self.state_reader.get_execution_events_since(
                (now - timedelta(minutes=15)).isoformat(),
                limit=500,
            )
            recent_rejects = [
                event
                for event in recent_execution_events
                if str(event.get("status", "")).upper() in {"REJECTED", "EXPIRED", "CANCELED", "CANCELLED"}
            ]
            self.state_writer.set_risk_snapshot(
                {
                    **funding_status,
                    "loop_last_alive_at": now.isoformat(),
                    "recent_reject_count_15m": len(recent_rejects),
                    "recent_reject_symbols_15m": sorted(
                        {str(event.get("symbol", "")).upper() for event in recent_rejects if event.get("symbol")}
                    ),
                }
            )
            self._set_safe_mode_flag(
                "funding_stale",
                funding_status.get("funding_staleness_status") != "fresh",
            )
            self._set_safe_mode_flag(
                "rust_subscriber",
                self._preflight_status == "passed" and not self.subscriber.is_connected,
            )
            if (
                self._trading_mode != "paper"
                and self._preflight_status == "passed"
                and now_monotonic - self._last_symbol_universe_refresh_monotonic
                >= _SYMBOL_UNIVERSE_REFRESH_INTERVAL_S
            ):
                await self._fetch_lot_step_sizes()

            if sample_minute != self._last_sampled_minute:
                self._last_sampled_minute = sample_minute
                self._record_market_samples_for_minute(sample_minute)
                self._refresh_adaptive_state()
                await self._record_runtime_health(now.isoformat())

            current_date = now.date().isoformat()
            if current_date != self._last_retention_run_date:
                self._last_retention_run_date = current_date
                archive_counts = self.state_writer.archive_old_data(
                    retention_days=int(self._config.get("data_retention_days")),
                    market_retention_days=int(self._config.get("market_sample_retention_days")),
                    health_retention_days=int(self._config.get("health_sample_retention_days")),
                )
                self.state_writer.set_risk_snapshot(
                    {
                        "last_retention_run_at": now.isoformat(),
                        "last_retention_result": archive_counts,
                    }
                )

            self._maybe_record_validation_snapshot(now)
            self._persist_runtime_state()
            if await self._sleep_or_shutdown(5.0):
                break

    async def _run_heartbeat_loop(self) -> None:
        while not self._shutdown_event.is_set():
            heartbeat_id = f"hb_{uuid.uuid4().hex[:12]}"
            self._last_heartbeat_sent_id = heartbeat_id
            self._last_heartbeat_sent_monotonic = time.monotonic()
            sent = self.execution.send_heartbeat(heartbeat_id)
            interval = max(1, int(self._config.get("heartbeat_interval_seconds")))
            if await self._sleep_or_shutdown(interval):
                break

            missed = not sent or self._last_heartbeat_ack_id != heartbeat_id
            if missed:
                self._heartbeat_misses += 1
            else:
                self._heartbeat_misses = 0

            miss_threshold = max(1, int(self._config.get("heartbeat_miss_threshold")))
            if self._heartbeat_misses >= miss_threshold:
                self._set_safe_mode_flag("heartbeat_bridge", True)
                if (
                    not sent
                    and not self.subscriber.is_connected
                    and bool(self.state_reader.get_positions())
                ):
                    self._set_blocked_reason("execution bridge unavailable for exits")
            else:
                self._persist_runtime_state()

    async def shutdown(self, reason: str = "manual") -> None:
        if self._shutdown_started:
            return
        self._shutdown_started = True
        self._shutdown_event.set()
        shutdown_at = datetime.now(timezone.utc).isoformat()
        if reason == "manual":
            self._record_operator_intervention(
                sample_time=shutdown_at,
                notes=f"shutdown:{reason}",
                alert_level="warning",
            )
        elif reason.startswith("signal:"):
            self._record_runtime_incident(
                sample_time=shutdown_at,
                notes=f"shutdown:{reason}",
                alert_level="warning",
            )
        try:
            self.state_writer.set_risk_snapshot(
                {
                    "shutdown_reason": reason,
                    "shutdown_started_at": shutdown_at,
                    "allow_new_risk": False,
                }
            )
        except Exception:
            pass

        if self._trading_mode != "paper" and self._preflight_status == "passed":
            try:
                snapshot = await self._fetch_exchange_startup_snapshot()
                cancel_failures: list[str] = []
                cancel_failures.extend(
                    await self._cancel_open_orders(snapshot.get("futures_open_orders") or [], futures=True)
                )
                cancel_failures.extend(
                    await self._cancel_open_orders(snapshot.get("spot_open_orders") or [], futures=False)
                )
                self.state_writer.set_risk_snapshot(
                    {
                        "shutdown_order_cancelled_at": shutdown_at,
                        "shutdown_order_cancel_failures": cancel_failures,
                    }
                )
            except Exception as exc:
                logger.warning("Graceful shutdown order cancel failed: %s", exc)
                try:
                    self.state_writer.set_risk_snapshot(
                        {"shutdown_order_cancel_error": str(exc)[:300]}
                    )
                except Exception:
                    pass

        current_task = asyncio.current_task()
        tasks = [
            task
            for task in self._background_tasks
            if task is not current_task and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        self._config.stop_watching()
        try:
            self.execution.close()
        except Exception:
            pass
        try:
            self.state_reader.close()
        except Exception:
            pass
        try:
            self.state_writer.close()
        except Exception:
            pass

    async def _reconcile_live_startup_state(self) -> None:
        snapshot = await self._fetch_exchange_startup_snapshot()
        snapshot = await self._clear_startup_open_orders(snapshot, stage="Live startup reconciliation")

        futures_account = snapshot["futures_account"]
        position_risk_rows = [
            row
            for row in snapshot.get("position_risk") or []
            if isinstance(row, dict)
            and abs(_float_or_zero(row.get("positionAmt"))) > _POSITION_QTY_TOLERANCE
        ]
        account_position_rows = self._open_account_position_rows(futures_account)
        position_rows = self._open_snapshot_position_rows(snapshot)
        spot_balances = self._build_spot_balance_map(snapshot.get("spot_account"))
        spot_account_available = snapshot.get("spot_account") is not None
        funding_income = snapshot.get("funding_income") or []
        local_positions = {row["symbol"]: row for row in self.state_reader.get_positions()}
        funding_signal_available = (
            self.funding_ranker.status_snapshot().get("funding_staleness_status") == "fresh"
        )
        self._startup_exit_candidates.clear()
        self._startup_manual_review_symbols.clear()

        reconciled_symbols: set[str] = set()
        mismatched_symbols: list[str] = []
        hedge_gap_symbols: list[str] = []
        unsupported_direction_symbols: list[str] = []
        gross_exposure_usd = 0.0

        if not position_risk_rows and account_position_rows:
            logger.warning(
                "Startup reconciliation: /positionRisk reported no open positions, "
                "but /account returned %d open position row(s); adopting account fallback for %s",
                len(account_position_rows),
                ", ".join(
                    sorted(
                        {
                            str(row.get("symbol", "")).upper()
                            for row in account_position_rows
                            if row.get("symbol")
                        }
                    )
                ),
            )

        for raw_position in position_rows:
            symbol = str(raw_position.get("symbol", "")).upper()
            position_amt = _float_or_zero(raw_position.get("positionAmt"))
            qty = abs(position_amt)
            if not symbol or qty <= _POSITION_QTY_TOLERANCE:
                continue

            direction = self._direction_from_futures_position(
                position_amt,
                str(raw_position.get("positionSide", "BOTH")),
            )
            unsupported_direction = direction != "long"
            if unsupported_direction:
                unsupported_direction_symbols.append(symbol)
            entry_price = _float_or_zero(raw_position.get("breakEvenPrice"))
            if entry_price <= 0.0:
                entry_price = _float_or_zero(raw_position.get("entryPrice"))
            mark_price = _float_or_zero(raw_position.get("markPrice"))
            if entry_price <= 0.0:
                entry_price = mark_price
            if mark_price <= 0.0:
                mark_price = entry_price

            side_label = (
                "SHORT_SPOT_LONG_PERP" if direction == "short" else "LONG_SPOT_SHORT_PERP"
            )
            updated_at = _iso_from_ms(raw_position.get("updateTime"))
            local_position = local_positions.get(symbol)
            if local_position is not None:
                local_qty = _float_or_zero(local_position.get("qty"))
                if (
                    local_position.get("direction") != direction
                    or abs(local_qty - qty) > _POSITION_QTY_TOLERANCE
                ):
                    mismatched_symbols.append(symbol)

            hedge_ratio = 1.0
            if direction == "long":
                base_asset = _extract_base_asset(symbol)
                if spot_account_available:
                    spot_qty = spot_balances.get(base_asset, 0.0)
                    hedge_ratio = min(1.0, max(0.0, spot_qty / qty)) if qty > _POSITION_QTY_TOLERANCE else 1.0
                    if not _spot_inventory_covers_hedge(spot_qty, qty):
                        hedge_gap_symbols.append(symbol)
                else:
                    # Spot API unavailable at startup â€” cannot verify hedge right now.
                    # Use the existing DB hedge_ratio if present; otherwise optimistically
                    # assume intact. The periodic health check will confirm or flag the gap.
                    local_hr = _float_or_zero(local_position.get("hedge_ratio")) if local_position is not None else 0.0
                    hedge_ratio = local_hr if local_hr > 0.0 else 1.0
            else:
                hedge_ratio = 0.0

            current_ann_funding = (
                self.funding_ranker.get_rate(symbol)
                if funding_signal_available
                else _float_or_zero(local_position.get("ann_funding")) if local_position is not None else 0.0
            )
            spot_live = self.depth_tracker.spot_mid_price(symbol)
            if spot_live <= 0.0:
                spot_live = _float_or_zero(local_position.get("spot_live")) if local_position is not None else 0.0
            if spot_live <= 0.0:
                spot_live = mark_price
            spot_entry_price = _float_or_zero(local_position.get("spot_entry")) if local_position is not None else 0.0
            if spot_entry_price <= 0.0:
                # Preserve exchange entry basis when spot quotes are unavailable during startup.
                spot_entry_price = entry_price or spot_live
            perp_entry_price = _float_or_zero(local_position.get("perp_entry")) if local_position is not None else 0.0
            if perp_entry_price <= 0.0:
                perp_entry_price = entry_price
            entry_ann_funding = _float_or_zero(local_position.get("entry_ann_funding")) if local_position is not None else 0.0
            if entry_ann_funding == 0.0:
                entry_ann_funding = current_ann_funding
            if local_position is not None:
                updated_at = str(
                    self._entry_times.get(symbol)
                    or local_position.get("updated_at")
                    or updated_at
                )
            exchange_pnl_usd = _float_or_zero(raw_position.get("unRealizedProfit"))
            recovery_state, recovery_note = self._classify_startup_recovered_position(
                symbol=symbol,
                direction=direction,
                ann_funding=current_ann_funding,
                hedge_ratio=hedge_ratio,
                unsupported_direction=unsupported_direction,
                funding_signal_available=funding_signal_available,
            )
            self._track_recovery_action(symbol, recovery_state, recovery_note)
            self.state_writer.upsert_position(
                symbol=symbol,
                side=side_label,
                spot_entry=spot_entry_price,
                perp_entry=perp_entry_price,
                spot_live=spot_live,
                perp_live=mark_price,
                qty=qty,
                hedge_ratio=hedge_ratio,
                ann_funding=current_ann_funding,
                entry_ann_funding=entry_ann_funding,
                net_pnl_usd=exchange_pnl_usd,
                exchange_pnl_usd=exchange_pnl_usd,
                recovery_state=recovery_state,
                status="OPEN",
                direction=direction,
                updated_at=updated_at,
            )
            self._entry_times[symbol] = updated_at
            self._position_directions[symbol] = direction
            reconciled_symbols.add(symbol)
            gross_exposure_usd += qty * max(mark_price, 0.0)

        local_only_symbols = sorted(set(local_positions) - reconciled_symbols)
        for symbol in local_only_symbols:
            self.state_writer.remove_position(symbol)
            self._entry_times.pop(symbol, None)
            self._position_directions.pop(symbol, None)

        account_equity = _derive_futures_account_balance(
            futures_account,
            preferred_fields=("totalMarginBalance", "totalWalletBalance"),
            asset_field_name="marginBalance",
        )
        available_balance = _derive_futures_account_balance(
            futures_account,
            preferred_fields=("availableBalance",),
            asset_field_name="availableBalance",
        )
        last_funding_fee = 0.0
        last_funding_fee_time = ""
        if funding_income:
            latest_income = max(
                funding_income,
                key=lambda item: int(_float_or_zero(item.get("time"))),
            )
            last_funding_fee = _float_or_zero(latest_income.get("income"))
            last_funding_fee_time = _iso_from_ms(latest_income.get("time"))
        exchange_equity_snapshot = self._cache_exchange_equity_snapshot(
            account_equity=account_equity,
            available_balance=available_balance,
            captured_at=datetime.now(timezone.utc).isoformat(),
        )
        startup_snapshot = self._build_reconciliation_snapshot(
            prefix="startup_reconciliation",
            snapshot=snapshot,
            rows=self.state_reader.get_positions(),
            audit_time=datetime.now(timezone.utc).isoformat(),
            local_only_symbols=local_only_symbols,
            mismatched_symbols=mismatched_symbols,
            unsupported_direction_symbols=unsupported_direction_symbols,
            last_funding_fee=last_funding_fee,
            last_funding_fee_time=last_funding_fee_time,
        )

        self.state_writer.set_stat("account_equity", account_equity)
        self.state_writer.set_stat("gross_exposure", gross_exposure_usd)
        self.state_writer.set_stat(
            "max_gross_exposure",
            float(self._config.get("max_gross_exposure_usd")),
        )
        self.state_writer.set_risk_snapshot(
            {
                "account_equity": account_equity,
                "available_balance": available_balance,
                **startup_snapshot,
                "allow_new_risk": True,
                **exchange_equity_snapshot,
            }
        )
        if hedge_gap_symbols or "hedge_gap" in self._safe_mode_flags:
            self._set_safe_mode_flag("hedge_gap", bool(hedge_gap_symbols))
        self.state_writer.flush()
        logger.info(
            "Live startup reconciliation complete: %d exchange positions, %d stale local rows removed, %d mismatches, %d review items",
            len(reconciled_symbols),
            len(local_only_symbols),
            len(mismatched_symbols),
            len(startup_snapshot["startup_reconciliation_recovery_actions"]),
        )
        if hedge_gap_symbols:
            logger.critical(
                "Startup recovery: spot hedge gap for %s (perp open, spot missing). "
                "Keeping the position visible and blocking only the affected symbols until review.",
                ", ".join(sorted(hedge_gap_symbols)),
            )
        if mismatched_symbols:
            logger.warning(
                "Startup recovery adopted exchange truth for mismatched local rows: %s",
                ", ".join(sorted(mismatched_symbols)),
            )
        if unsupported_direction_symbols:
            logger.warning(
                "Startup recovery kept unsupported inverse positions visible for manual review: %s",
                ", ".join(sorted(unsupported_direction_symbols)),
            )

    def _sync_position_to_execution_engine(self, row: dict) -> bool:
        if self._trading_mode == "paper":
            return True

        symbol = str(row.get("symbol", "")).upper()
        qty = _float_or_zero(row.get("qty"))
        if not symbol or qty <= _POSITION_QTY_TOLERANCE:
            return True

        direction = str(row.get("direction", "long") or "long").lower()
        hedge_ratio = min(1.0, max(0.0, _float_or_zero(row.get("hedge_ratio"))))
        if direction != "long":
            hedge_ratio = 0.0
        spot_qty = qty * hedge_ratio if direction == "long" else 0.0
        perp_qty = qty
        spot_live, perp_live = self._leg_mark_prices(symbol, row)
        spot_entry = _float_or_zero(row.get("spot_entry")) or spot_live or _float_or_zero(row.get("perp_entry"))
        perp_entry = _float_or_zero(row.get("perp_entry")) or perp_live or spot_entry

        sent = self.execution.restore_position_tracking(
            symbol=symbol,
            direction=direction,
            qty=qty,
            spot_entry_price=spot_entry,
            perp_entry_price=perp_entry,
            spot_mark_price=spot_live or spot_entry,
            perp_mark_price=perp_live or perp_entry,
            spot_quantity=spot_qty,
            perp_quantity=perp_qty,
        )
        if not sent:
            logger.critical(
                "Failed to sync recovered position %s to execution engine; Rust will remain blind to live exposure until the bridge recovers",
                symbol,
            )
            self._set_safe_mode_flag("execution_bridge", True)
            return False

        logger.info(
            "Synced recovered position %s to execution engine (direction=%s, spot_qty=%.5f, perp_qty=%.5f)",
            symbol,
            direction,
            spot_qty,
            perp_qty,
        )
        return True

    def _sync_positions_to_execution_engine(self, rows: list[dict] | None = None) -> int:
        rows = rows if rows is not None else self.state_reader.get_positions()
        synced = 0
        for row in rows:
            if self._sync_position_to_execution_engine(row):
                synced += 1
        return synced

    async def _on_startup(self) -> None:
        """
        Phase 4: Smart startup - handles paper vs live mode correctly.
        
        Paper mode:  Clear all stale positions from local DB (fresh start)
        Live mode:   Sync positions from Binance API (true state from exchange)
        
        This prevents stale "OPEN" positions from previous runs affecting
        paper trading results.
        """
        import requests
        from datetime import datetime, timezone
        
        logger.info("="*50)
        logger.info("STARTUP MODE: %s", self._trading_mode.upper())
        logger.info("="*50)
        
        if self._trading_mode == "paper":
            # Paper mode: Clear all positions for fresh start
            logger.info("PAPER MODE: Clearing stale positions for fresh demo run...")
            self._pending_enters.clear()
            self._stale_pending_enters.clear()
            self._abandoned_pending_enters.clear()
            self._abandoned_exit_intents.clear()
            self._pending_exit_intents.clear()
            self._pending_exit_created_at.clear()
            self._exit_events.clear()
            for pending_intent in self.state_reader.get_pending_intents():
                self.state_writer.delete_pending_intent(str(pending_intent.get("intent_id")))
            
            # Get current positions
            positions = self.state_reader.get_positions()
            if positions:
                logger.info("Found %d stale positions to clear: %s", 
                           len(positions), [p.get('symbol') for p in positions])
                
                for pos in positions:
                    if pos.get('status') == 'OPEN':
                        self.state_writer.remove_position(pos['symbol'])
                        logger.info("  Cleared stale paper position: %s", pos['symbol'])
                
                logger.info("Paper mode startup complete - fresh start!")
            else:
                logger.info("No stale positions found - clean slate!")
            self.state_writer.set_risk_snapshot(
                {
                    "startup_reconciliation_status": "paper_reset",
                    "startup_reconciliation_time": datetime.now(timezone.utc).isoformat(),
                    "startup_reconciliation_position_count": 0,
                    "startup_reconciliation_spot_hedge_gaps": [],
                    "startup_reconciliation_mismatched_symbols": [],
                }
            )
                
        else:
            logger.info("%s MODE: Reconciling startup state against signed Binance account truth...", self._trading_mode.upper())
            await self._reconcile_live_startup_state()
            current_positions = self.state_reader.get_positions()
            synced_count = self._sync_positions_to_execution_engine(current_positions)
            if synced_count:
                logger.info(
                    "Startup recovery synced %d open position(s) back into the Rust execution engine",
                    synced_count,
                )
            hedge_gaps = [
                str(row.get("symbol", "")).upper()
                for row in current_positions
                if str(row.get("direction", "")).lower() == "long"
                and _float_or_zero(row.get("hedge_ratio")) < (1.0 - _SPOT_HEDGE_SHORTFALL_TOLERANCE_PCT)
            ]
            self._startup_manual_review_symbols.clear()
            self._refresh_startup_recovery_flags(current_positions)
            # hedge_gap is tracked as a warning but does NOT block trading â€”
            # an unhedged leg on one symbol is no reason to freeze the whole
            # portfolio; new pairs will each have their own proper spot hedge.
            self._set_safe_mode_flag("hedge_gap", False)
            if hedge_gaps:
                logger.warning(
                    "Spot hedge gap detected for %s â€” perp open but spot inventory low. "
                    "Bot will continue trading; verify spot wallet manually.",
                    ", ".join(sorted(hedge_gaps)),
                )
                self.state_writer.set_risk_snapshot({"hedge_gap_symbols": sorted(hedge_gaps)})
            else:
                self.state_writer.set_risk_snapshot({"hedge_gap_symbols": []})
            self._set_safe_mode_flag("startup_mismatch", False)
        
        logger.info("="*50)

    async def _fetch_lot_step_sizes(self) -> None:
        """Fetch lot sizes and the tradable spot+perp universe at startup.

        Rounds quantities to the exchange-mandated step size, preventing -1111
        (invalid quantity precision) order rejections on symbols like DOGEUSDT
        where stepSize=1.0 and PEPEUSDT where stepSize=1000.0.
        """
        refresh_time = datetime.now(timezone.utc).isoformat()
        self._last_symbol_universe_refresh_monotonic = time.monotonic()
        try:
            futures_resp, spot_resp = await asyncio.gather(
                asyncio.to_thread(
                    requests.get,
                    f"{self._futures_base_url}/fapi/v1/exchangeInfo",
                    timeout=10,
                ),
                asyncio.to_thread(
                    requests.get,
                    f"{self._spot_base_url}/api/v3/exchangeInfo",
                    timeout=10,
                ),
            )
            futures_resp.raise_for_status()
            spot_resp.raise_for_status()
            futures_data = futures_resp.json()
            spot_data = spot_resp.json()
        except Exception as exc:
            logger.warning("Could not fetch exchange info for lot sizes: %s", exc)
            current_tradable_symbols = self._tradable_trade_symbols()
            self.funding_ranker.set_allowed_symbols(current_tradable_symbols)
            self._publish_symbol_universe_state(
                refreshed_at=refresh_time,
                error=str(exc),
            )
            return

        spot_symbols = {
            str(sym_info.get("symbol", "")).upper()
            for sym_info in spot_data.get("symbols", [])
            if sym_info.get("status") == "TRADING" and sym_info.get("quoteAsset") == "USDT"
        }
        eligible_symbols: set[str] = set()
        lot_steps: dict[str, float] = {}
        for sym_info in futures_data.get("symbols", []):
            symbol = sym_info.get("symbol", "")
            if not symbol:
                continue
            if (
                sym_info.get("contractType") == "PERPETUAL"
                and sym_info.get("status") == "TRADING"
                and sym_info.get("quoteAsset") == "USDT"
            ):
                eligible_symbols.add(symbol.upper())
            for f in sym_info.get("filters", []):
                if f.get("filterType") == "LOT_SIZE":
                    try:
                        lot_steps[symbol] = float(f["stepSize"])
                    except (KeyError, ValueError):
                        pass
                    break

        if eligible_symbols:
            self._tradable_perp_symbols = eligible_symbols
        if spot_symbols:
            self._tradable_spot_symbols = spot_symbols
            self._spot_universe_loaded = True
        elif not self._spot_universe_loaded:
            logger.warning(
                "Spot exchangeInfo returned no tradable USDT symbols; leaving spot universe unverified"
            )
        else:
            logger.warning(
                "Spot exchangeInfo returned no tradable USDT symbols; keeping the previous verified spot universe (%d symbols)",
                len(self._tradable_spot_symbols),
            )
        self._lot_step.update(lot_steps)

        tradable_symbols = self._tradable_trade_symbols()
        self.funding_ranker.set_allowed_symbols(tradable_symbols)
        self._publish_symbol_universe_state(
            refreshed_at=refresh_time,
            error=(
                ""
                if self._spot_universe_ready_for_entries()
                else "spot exchangeInfo returned no tradable USDT symbols"
            ),
        )

        logger.info(
            "Lot step sizes loaded for %d symbols (%d tradable spot+perp symbols)",
            len(self._lot_step),
            len(tradable_symbols),
        )

    def _round_to_step(self, qty: float, step: float) -> float:
        """Round qty down to the nearest valid lot step size.

        Uses log10 to derive the correct number of decimal places:
          step=0.001 â†’ 3 dp, step=1.0 â†’ 0 dp, step=1000.0 â†’ 0 dp.
        """
        if step <= 0:
            return qty
        rounded = math.floor((qty / step) + 1e-9) * step
        decimals = max(0, -int(math.floor(math.log10(step))))
        return round(rounded, decimals)

    @staticmethod
    def _per_leg_notional_usd(gross_notional_usd: float) -> float:
        """Translate slot gross notional into the matched spot/perp leg size."""
        return max(gross_notional_usd, 0.0) / 2.0

    def _minutes_since_last_snapshot(self) -> float:
        """Return minutes elapsed since the most recent funding snapshot (0/8/16 UTC)."""
        now = datetime.now(timezone.utc)
        current_minutes = now.hour * 60 + now.minute
        snapshot_minutes = sorted(h * 60 for h in FUNDING_SNAPSHOT_HOURS)
        # Find the most recent snapshot that has already passed today
        elapsed = None
        for snap in reversed(snapshot_minutes):
            if current_minutes >= snap:
                elapsed = current_minutes - snap
                break
        if elapsed is None:
            # Past midnight but before first snapshot: measure from last snapshot of previous day
            elapsed = current_minutes + (24 * 60 - snapshot_minutes[-1])
        return float(elapsed)

    @staticmethod
    def _count_funding_settlements(entry_dt: datetime, exit_dt: datetime) -> int:
        """Count discrete funding settlements crossed while the position was open."""
        if exit_dt <= entry_dt:
            return 0

        start_day = entry_dt.astimezone(timezone.utc).date()
        end_day = exit_dt.astimezone(timezone.utc).date()
        settlement_hours = sorted(int(hour) for hour in FUNDING_SNAPSHOT_HOURS)
        settlements = 0
        day = start_day
        while day <= end_day:
            for hour in settlement_hours:
                settlement_dt = datetime(
                    day.year,
                    day.month,
                    day.day,
                    hour,
                    tzinfo=timezone.utc,
                )
                if entry_dt < settlement_dt <= exit_dt:
                    settlements += 1
            day += timedelta(days=1)
        return settlements

    def _synthetic_funding_collected_usd(
        self,
        *,
        qty: float,
        direction: str,
        ann_funding: float,
        hold_hours: float,
        funding_periods: float | None,
        spot_entry_price: float,
        perp_entry_price: float,
    ) -> float:
        effective_periods = funding_periods
        if effective_periods is None:
            effective_periods = max(hold_hours, 0.0) / FUNDING_INTERVAL_HOURS
        notional_usd = ((spot_entry_price + perp_entry_price) / 2.0) * qty
        signed_ann_funding = -ann_funding if str(direction or "long").lower() == "short" else ann_funding
        return (
            signed_ann_funding
            * (max(effective_periods, 0.0) / FUNDING_PERIODS_PER_YEAR)
            * notional_usd
        )

    @staticmethod
    def _borrow_cost_usd(*, notional_usd: float, hold_hours: float) -> float:
        if notional_usd <= 0.0 or hold_hours <= 0.0:
            return 0.0
        return notional_usd * MARGIN_BORROW_RATE_ANNUAL * (hold_hours / (24.0 * 365.0))

    def _reconcile_funding_cashflows(
        self,
        *,
        symbol: str,
        entry_time: str,
        exit_time: str,
        qty: float,
        direction: str,
        ann_funding: float,
        hold_hours: float,
        funding_periods: float | None,
        spot_entry_price: float,
        perp_entry_price: float,
    ) -> tuple[float, str]:
        recorded_cashflows = self.state_reader.get_trade_funding_cashflows(
            symbol,
            entry_time,
            exit_time,
            scope_current=False,
        )
        if recorded_cashflows:
            return (
                sum(_float_or_zero(item.get("amount")) for item in recorded_cashflows),
                "actual_ledger",
            )

        if self._trading_mode != "paper":
            try:
                start_dt = self._parse_timestamp(entry_time)
                end_dt = self._parse_timestamp(exit_time)
                if start_dt is not None and end_dt is not None:
                    income_rows = self._signed_request_json_sync(
                        method="GET",
                        base_url=self._futures_base_url,
                        endpoint="/fapi/v1/income",
                        params={
                            "symbol": symbol,
                            "incomeType": "FUNDING_FEE",
                            "startTime": int(start_dt.timestamp() * 1000),
                            "endTime": int(end_dt.timestamp() * 1000),
                            "limit": 1000,
                        },
                        api_key=self._futures_api_key,
                        api_secret=self._futures_api_secret,
                    )
                else:
                    income_rows = []
                total_funding = 0.0
                if isinstance(income_rows, list):
                    for row in income_rows:
                        if str(row.get("symbol", "")).upper() != symbol.upper():
                            continue
                        income_value = _float_or_zero(row.get("income"))
                        total_funding += income_value
                        event_time = _iso_from_ms(row.get("time"))
                        self.state_writer.record_execution_event(
                            {
                                "event_name": "FundingFee",
                                "symbol": symbol,
                                "client_order_id": f"funding_{row.get('tranId', row.get('time', 'unknown'))}",
                                "status": "SETTLED",
                                "asset": row.get("asset", "USDT"),
                                "amount": income_value,
                                "reason": row.get("incomeType", "FUNDING_FEE"),
                                "event_time": event_time,
                                "raw_income_row": row,
                            }
                        )
                if isinstance(income_rows, list) and income_rows:
                    return total_funding, "actual_rest"
            except Exception as exc:
                logger.warning(
                    "Could not reconcile actual funding fees for %s between %s and %s: %s",
                    symbol,
                    entry_time,
                    exit_time,
                    exc,
                )

        synthetic_funding = self._synthetic_funding_collected_usd(
            qty=qty,
            direction=direction,
            ann_funding=ann_funding,
            hold_hours=hold_hours,
            funding_periods=funding_periods,
            spot_entry_price=spot_entry_price,
            perp_entry_price=perp_entry_price,
        )
        return synthetic_funding, "synthetic"

    def _cost_depth_or_default(self, depth_usd: float) -> float:
        if depth_usd and depth_usd > 0.0:
            return depth_usd
        return _DEFAULT_COST_DEPTH_USD

    def _leg_mark_prices(self, symbol: str, row: dict | None = None) -> tuple[float, float]:
        row = row or {}

        spot_live = self.depth_tracker.spot_mid_price(symbol)
        if spot_live <= 0.0:
            spot_live = _float_or_zero(row.get("spot_live")) or _float_or_zero(row.get("spot_entry"))

        perp_live = _float_or_zero(self._mark_prices.get(symbol))
        if perp_live <= 0.0:
            perp_live = self.depth_tracker.perp_mid_price(symbol)
        if perp_live <= 0.0:
            perp_live = _float_or_zero(row.get("perp_live")) or _float_or_zero(row.get("perp_entry"))

        return spot_live, perp_live

    def _funding_has_decayed(self, direction: str, ann_funding: float) -> bool:
        exit_threshold = float(self._config.get("exit_ann_funding_threshold"))
        if direction == "short":
            return ann_funding > -exit_threshold
        return ann_funding < exit_threshold

    def _is_cycle_completion_event(
        self,
        execution_type: str | None,
        spot_fill_price,
        perp_fill_price,
    ) -> bool:
        execution_label = str(execution_type or "").upper()
        if execution_label in {"FILLED_CYCLE", "PAPER_FILL"}:
            return True
        return spot_fill_price is not None and perp_fill_price is not None

    def _next_intent_id(self, symbol: str, intent_type: str) -> str:
        return f"{intent_type.lower()}_{symbol.lower()}_{uuid.uuid4().hex[:12]}"

    def _persist_pending_intent(
        self,
        *,
        intent_id: str,
        symbol: str,
        intent_type: str,
        status: str,
        direction: str,
        quantity: float = 0.0,
        notional_usd: float = 0.0,
        client_order_id: str | None = None,
        retry_count: int = 0,
        last_error: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        self.state_writer.upsert_pending_intent(
            intent_id=intent_id,
            symbol=symbol,
            intent_type=intent_type,
            status=status,
            direction=direction,
            quantity=quantity,
            notional_usd=notional_usd,
            client_order_id=client_order_id,
            retry_count=retry_count,
            last_error=last_error,
            metadata=metadata,
        )

    def _resolve_pending_intent(self, intent_id: str | None) -> None:
        if not intent_id:
            return
        self.state_writer.delete_pending_intent(intent_id)

    @staticmethod
    def _parse_timestamp(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _intent_timeout_seconds(self) -> float:
        try:
            return max(30.0, float(self._config.get("pending_intent_max_age_seconds")))
        except (TypeError, ValueError):
            return float(PENDING_INTENT_MAX_AGE_SECONDS)

    def _pending_intent_self_heal_grace_seconds(self) -> float:
        return max(60.0, self._intent_timeout_seconds() / 2.0)

    def _abandoned_intent_retention_seconds(self) -> float:
        return max(300.0, self._intent_timeout_seconds() * 2.0)

    def _execution_events_for_symbol_since(self, symbol: str, start_time: str | None) -> list[dict]:
        if not start_time:
            return []
        try:
            events = self.state_reader.get_execution_events_since(start_time, limit=500)
        except Exception:
            return []
        symbol_upper = symbol.upper()
        return [
            event for event in events
            if str(event.get("symbol", "")).upper() == symbol_upper
        ]

    def _record_pending_intent_self_heal(
        self,
        *,
        symbol: str,
        intent_type: str,
        reason: str,
        sample_time: str,
    ) -> None:
        self.state_writer.set_risk_snapshot(
            {
                "last_pending_intent_self_heal": {
                    "symbol": symbol,
                    "intent_type": intent_type,
                    "reason": reason,
                    "resolved_at": sample_time,
                }
            }
        )
        self._record_runtime_incident(
            sample_time=sample_time,
            notes=f"auto_resolved_pending_intent:{intent_type}:{symbol}:{reason}",
            alert_level="warning",
        )

    def _prune_abandoned_pending_intents(self, now: datetime) -> None:
        retention_s = self._abandoned_intent_retention_seconds()
        for store in (self._abandoned_pending_enters, self._abandoned_exit_intents):
            for symbol, payload in list(store.items()):
                abandoned_dt = self._parse_timestamp(str(payload.get("abandoned_at") or ""))
                if abandoned_dt is None:
                    continue
                if (now - abandoned_dt).total_seconds() < retention_s:
                    continue
                store.pop(symbol, None)

    def _paper_self_heal_stale_pending_intents(self, now: datetime) -> None:
        if self._trading_mode != "paper":
            return

        timeout_s = self._intent_timeout_seconds()
        grace_s = self._pending_intent_self_heal_grace_seconds()
        terminal_failures = {"REJECTED", "EXPIRED", "CANCELED", "CANCELLED", "FAILED"}
        local_positions = {
            str(row.get("symbol", "")).upper()
            for row in self.state_reader.get_positions()
            if row.get("symbol")
        }
        pending_rows = {
            str(row.get("intent_id", "")): row
            for row in self.state_reader.get_pending_intents(limit=500)
        }

        for symbol, entry in list(self._stale_pending_enters.items()):
            timed_out_dt = self._parse_timestamp(
                str(entry.get("timed_out_at") or entry.get("entry_time") or "")
            )
            if timed_out_dt is None or (now - timed_out_dt).total_seconds() < grace_s:
                continue

            intent_id = str(entry.get("intent_id") or "")
            pending_row = pending_rows.get(intent_id, {})
            client_order_id = str(pending_row.get("client_order_id") or "")
            entry_start = str(entry.get("entry_time") or pending_row.get("created_at") or "")
            events = self._execution_events_for_symbol_since(symbol, entry_start)
            statuses = {str(event.get("status", "")).upper() for event in events if event.get("status")}
            active_statuses = statuses - terminal_failures - {"FILLED"}

            if symbol in local_positions:
                self._stale_pending_enters.pop(symbol, None)
                self._resolve_pending_intent(intent_id)
                logger.warning(
                    "Auto-resolved stale ENTER for %s because the local position already exists",
                    symbol,
                )
                self._record_pending_intent_self_heal(
                    symbol=symbol,
                    intent_type="ENTER",
                    reason="paper_position_already_present",
                    sample_time=now.isoformat(),
                )
                continue

            if client_order_id or active_statuses or "FILLED" in statuses:
                continue

            abandoned_entry = dict(entry)
            abandoned_entry["abandoned_at"] = now.isoformat()
            abandoned_entry["abandon_reason"] = "paper_no_execution_activity_after_timeout"
            self._abandoned_pending_enters[symbol] = abandoned_entry
            self._stale_pending_enters.pop(symbol, None)
            self._resolve_pending_intent(intent_id)
            logger.warning(
                "Auto-cleared stale ENTER for %s after %.0fs with no execution activity in paper mode",
                symbol,
                timeout_s + grace_s,
            )
            self._record_pending_intent_self_heal(
                symbol=symbol,
                intent_type="ENTER",
                reason="paper_no_execution_activity_after_timeout",
                sample_time=now.isoformat(),
            )

        for symbol, intent_id in list(self._pending_exit_intents.items()):
            created_dt = self._parse_timestamp(self._pending_exit_created_at.get(symbol))
            if created_dt is None or (now - created_dt).total_seconds() < (timeout_s + grace_s):
                continue

            pending_row = pending_rows.get(intent_id, {})
            client_order_id = str(pending_row.get("client_order_id") or "")
            events = self._execution_events_for_symbol_since(symbol, self._pending_exit_created_at.get(symbol))
            statuses = {str(event.get("status", "")).upper() for event in events if event.get("status")}
            active_statuses = statuses - terminal_failures - {"FILLED"}

            if symbol not in local_positions:
                self._pending_exit_intents.pop(symbol, None)
                self._pending_exit_created_at.pop(symbol, None)
                self._stale_pending_exits.discard(symbol)
                self._clear_startup_recovery_exit_tracking(symbol)
                event = self._exit_events.pop(symbol, None)
                if event is not None:
                    event.set()
                self._resolve_pending_intent(intent_id)
                logger.warning(
                    "Auto-resolved stale EXIT for %s because the local position is already gone",
                    symbol,
                )
                self._record_pending_intent_self_heal(
                    symbol=symbol,
                    intent_type="EXIT",
                    reason="paper_position_already_closed",
                    sample_time=now.isoformat(),
                )
                continue

            if client_order_id or active_statuses or "FILLED" in statuses:
                continue

            if not events or statuses.issubset(terminal_failures):
                self._abandoned_exit_intents[symbol] = {
                    "intent_id": intent_id,
                    "abandoned_at": now.isoformat(),
                    "created_at": self._pending_exit_created_at.get(symbol, ""),
                }
                self._pending_exit_intents.pop(symbol, None)
                self._pending_exit_created_at.pop(symbol, None)
                self._stale_pending_exits.discard(symbol)
                self._exit_events.pop(symbol, None)
                self._resolve_pending_intent(intent_id)
                logger.warning(
                    "Auto-cleared stale EXIT for %s after %.0fs with no live execution activity in paper mode",
                    symbol,
                    timeout_s + grace_s,
                )
                self._record_pending_intent_self_heal(
                    symbol=symbol,
                    intent_type="EXIT",
                    reason="paper_no_execution_activity_after_timeout",
                    sample_time=now.isoformat(),
                )
                self._set_safe_mode_flag("exit_failure", False)

        self._try_clear_late_entry_fill()

        self._refresh_stale_pending_flag()

    def _restore_live_position_from_exchange(
        self,
        raw_position: dict,
        *,
        entry_context: dict | None = None,
        spot_balances: dict[str, float] | None = None,
    ) -> None:
        symbol = str(raw_position.get("symbol", "")).upper()
        position_amt = _float_or_zero(raw_position.get("positionAmt"))
        qty = abs(position_amt)
        if not symbol or qty <= _POSITION_QTY_TOLERANCE:
            return

        direction = self._direction_from_futures_position(
            position_amt,
            str(raw_position.get("positionSide", "BOTH")),
        )
        entry_price = _float_or_zero(raw_position.get("breakEvenPrice"))
        if entry_price <= 0.0:
            entry_price = _float_or_zero(raw_position.get("entryPrice"))
        mark_price = _float_or_zero(raw_position.get("markPrice"))
        if entry_price <= 0.0:
            entry_price = mark_price
        if mark_price <= 0.0:
            mark_price = entry_price
        spot_live = self.depth_tracker.spot_mid_price(symbol)
        if spot_live <= 0.0:
            spot_live = mark_price
        updated_at = _iso_from_ms(raw_position.get("updateTime"))
        if not updated_at:
            updated_at = str((entry_context or {}).get("entry_time") or datetime.now(timezone.utc).isoformat())
        side_label = (
            "SHORT_SPOT_LONG_PERP" if direction == "short" else "LONG_SPOT_SHORT_PERP"
        )
        current_ann_funding = self.funding_ranker.get_rate(symbol)
        entry_ann_funding = _float_or_zero((entry_context or {}).get("ann_funding")) or current_ann_funding
        hedge_ratio, recovery_state, recovery_note = self._classify_live_recovered_position(
            symbol=symbol,
            direction=direction,
            qty=qty,
            ann_funding=current_ann_funding,
            spot_balances=spot_balances,
        )
        self._track_recovery_action(symbol, recovery_state, recovery_note)
        spot_entry = _float_or_zero((entry_context or {}).get("spot_entry"))
        if spot_entry <= 0.0:
            # Prefer recovered entry basis over current marks when rebuilding a live position.
            spot_entry = _float_or_zero((entry_context or {}).get("entry_price")) or entry_price or spot_live
        perp_entry = _float_or_zero((entry_context or {}).get("perp_entry")) or entry_price
        restored_row = {
            "symbol": symbol,
            "side": side_label,
            "spot_entry": spot_entry,
            "perp_entry": perp_entry,
            "spot_live": spot_live,
            "perp_live": mark_price,
            "qty": qty,
            "ann_funding": current_ann_funding,
            "entry_ann_funding": entry_ann_funding,
            "net_pnl_usd": _float_or_zero(raw_position.get("unRealizedProfit")),
            "exchange_pnl_usd": _float_or_zero(raw_position.get("unRealizedProfit")),
            "status": "OPEN",
            "direction": direction,
            "updated_at": updated_at,
            "hedge_ratio": hedge_ratio,
            "recovery_state": recovery_state,
        }
        self.state_writer.upsert_position(
            symbol=symbol,
            side=side_label,
            spot_entry=spot_entry,
            perp_entry=perp_entry,
            spot_live=spot_live,
            perp_live=mark_price,
            qty=qty,
            hedge_ratio=_float_or_zero(restored_row.get("hedge_ratio")),
            ann_funding=current_ann_funding,
            entry_ann_funding=entry_ann_funding,
            net_pnl_usd=_float_or_zero(restored_row.get("net_pnl_usd")),
            exchange_pnl_usd=_float_or_zero(restored_row.get("exchange_pnl_usd")),
            recovery_state=str(restored_row.get("recovery_state") or ""),
            status=str(restored_row["status"]),
            direction=direction,
            updated_at=updated_at,
        )
        self._entry_times[symbol] = updated_at
        self._position_directions[symbol] = direction
        self._sync_position_to_execution_engine(restored_row)
        rows = self.state_reader.get_positions()
        hedge_gaps = [
            str(row.get("symbol", "")).upper()
            for row in rows
            if str(row.get("direction", "")).lower() == "long"
            and _float_or_zero(row.get("hedge_ratio")) < (1.0 - _SPOT_HEDGE_SHORTFALL_TOLERANCE_PCT)
        ]
        # hedge_gap is a warning, not a trading halt â€” see _reconcile_live_startup_state.
        if hedge_gaps or "hedge_gap" in self._safe_mode_flags:
            self._set_safe_mode_flag("hedge_gap", bool(hedge_gaps))
        if hedge_gaps:
            logger.warning("Spot hedge gap after live recovery for %s", ", ".join(sorted(hedge_gaps)))
            self.state_writer.set_risk_snapshot({"hedge_gap_symbols": sorted(hedge_gaps)})
            self.state_writer.flush()
        self._refresh_startup_recovery_flags(rows)

    async def _recover_failed_entry_from_exchange(
        self,
        *,
        symbol: str,
        entry: dict,
        terminal_status: str,
        execution_type: str = "",
        client_order_id: str = "",
    ) -> None:
        if self._trading_mode == "paper":
            return

        try:
            snapshot = await self._fetch_exchange_startup_snapshot()
        except Exception as exc:
            logger.warning(
                "Entry failure reconciliation snapshot failed for %s after %s (%s): %s",
                symbol,
                terminal_status,
                execution_type or "unknown",
                exc,
            )
            return

        position_rows = {
            str(row.get("symbol", "")).upper(): row
            for row in self._open_snapshot_position_rows(snapshot)
        }
        exchange_position = position_rows.get(symbol)
        if exchange_position is None:
            logger.warning(
                "Entry failure for %s ended with %s (%s) but exchange shows no surviving position to recover",
                symbol,
                terminal_status,
                execution_type or "unknown",
            )
            return

        spot_balances = self._build_spot_balance_map(snapshot.get("spot_account"))
        self._restore_live_position_from_exchange(
            exchange_position,
            entry_context=entry,
            spot_balances=spot_balances,
        )
        restored = next(
            (
                row
                for row in self.state_reader.get_positions()
                if str(row.get("symbol", "")).upper() == symbol
            ),
            {},
        )
        hedge_ratio = _float_or_zero(restored.get("hedge_ratio"))
        recovery_state = str(restored.get("recovery_state") or "")
        logger.critical(
            "Recovered live position for %s from exchange after terminal entry status %s (%s, client_order_id=%s); hedge_ratio=%.2f recovery_state=%s",
            symbol,
            terminal_status,
            execution_type or "unknown",
            client_order_id or "n/a",
            hedge_ratio,
            recovery_state or "none",
        )
        self._record_pending_intent_self_heal(
            symbol=symbol,
            intent_type="ENTER",
            reason=f"entry_failure_recovered:{(execution_type or terminal_status).lower()}",
            sample_time=datetime.now(timezone.utc).isoformat(),
        )

    def _queue_entry_failure_exchange_reconciliation(
        self,
        *,
        symbol: str,
        entry: dict,
        terminal_status: str,
        execution_type: str = "",
        client_order_id: str = "",
    ) -> None:
        if self._trading_mode == "paper":
            return

        existing = self._entry_failure_recovery_tasks.get(symbol)
        if existing is not None and not existing.done():
            return

        task = asyncio.create_task(
            self._recover_failed_entry_from_exchange(
                symbol=symbol,
                entry=dict(entry),
                terminal_status=terminal_status,
                execution_type=execution_type,
                client_order_id=client_order_id,
            ),
            name=f"entry_failure_recovery:{symbol}",
        )
        self._entry_failure_recovery_tasks[symbol] = task
        self._background_tasks.append(task)

        def _cleanup(done_task: asyncio.Task, *, recovered_symbol: str = symbol) -> None:
            self._entry_failure_recovery_tasks.pop(recovered_symbol, None)
            if done_task.cancelled():
                return
            exc = done_task.exception()
            if exc is not None:
                logger.exception(
                    "Entry failure reconciliation task crashed for %s",
                    recovered_symbol,
                    exc_info=exc,
                )

        task.add_done_callback(_cleanup)

    async def _live_self_heal_stale_pending_intents(self, now: datetime) -> None:
        if self._trading_mode == "paper":
            return

        timeout_s = self._intent_timeout_seconds()
        grace_s = self._pending_intent_self_heal_grace_seconds()
        should_reconcile = bool(self._pending_enters)

        if not should_reconcile:
            for entry in self._stale_pending_enters.values():
                timed_out_dt = self._parse_timestamp(
                    str(entry.get("timed_out_at") or entry.get("entry_time") or "")
                )
                if timed_out_dt is not None and (now - timed_out_dt).total_seconds() >= grace_s:
                    should_reconcile = True
                    break

        if not should_reconcile:
            for created_at in self._pending_exit_created_at.values():
                created_dt = self._parse_timestamp(created_at)
                if created_dt is not None and (now - created_dt).total_seconds() >= (timeout_s + grace_s):
                    should_reconcile = True
                    break

        if not should_reconcile:
            return

        try:
            snapshot = await self._fetch_exchange_startup_snapshot()
        except Exception as exc:
            logger.warning("Live pending-intent reconciliation snapshot failed: %s", exc)
            return

        position_rows = {
            str(row.get("symbol", "")).upper(): row
            for row in self._open_snapshot_position_rows(snapshot)
        }
        spot_account_data = snapshot.get("spot_account")
        spot_balances = self._build_spot_balance_map(spot_account_data)
        spot_account_available = spot_account_data is not None
        open_order_symbols = {
            str(order.get("symbol", "")).upper()
            for order in list(snapshot.get("futures_open_orders") or []) + list(snapshot.get("spot_open_orders") or [])
            if isinstance(order, dict) and order.get("symbol")
        }
        local_position_symbols = {
            str(row.get("symbol", "")).upper()
            for row in self.state_reader.get_positions()
            if row.get("symbol")
        }

        for symbol, entry in list(self._pending_enters.items()):
            exchange_position = position_rows.get(symbol)
            if exchange_position is None or symbol in open_order_symbols:
                continue

            direction = self._direction_from_futures_position(
                _float_or_zero(exchange_position.get("positionAmt")),
                str(exchange_position.get("positionSide", "BOTH")),
            )
            if direction == "long" and spot_account_available:
                base_asset = _extract_base_asset(symbol)
                if not _spot_inventory_covers_hedge(
                    spot_balances.get(base_asset, 0.0),
                    abs(_float_or_zero(exchange_position.get("positionAmt"))),
                ):
                    continue

            intent_id = str(entry.get("intent_id") or "")
            self._pending_enters.pop(symbol, None)
            if symbol not in local_position_symbols:
                self._restore_live_position_from_exchange(
                    exchange_position,
                    entry_context=entry,
                    spot_balances=spot_balances,
                )
                local_position_symbols.add(symbol)
            self._resolve_pending_intent(intent_id)
            logger.warning(
                "Auto-reconciled pending ENTER for %s from live exchange state before timeout%s",
                symbol,
                " (spot account unavailable, hedge unverified)" if not spot_account_available else "",
            )
            self._record_pending_intent_self_heal(
                symbol=symbol,
                intent_type="ENTER",
                reason="live_position_present_on_exchange_before_timeout",
                sample_time=now.isoformat(),
            )

        for symbol, entry in list(self._stale_pending_enters.items()):
            timed_out_dt = self._parse_timestamp(
                str(entry.get("timed_out_at") or entry.get("entry_time") or "")
            )
            if timed_out_dt is None or (now - timed_out_dt).total_seconds() < grace_s:
                continue

            intent_id = str(entry.get("intent_id") or "")
            exchange_position = position_rows.get(symbol)
            if exchange_position is not None:
                self._stale_pending_enters.pop(symbol, None)
                if symbol not in local_position_symbols:
                    self._transition_late_entry_fill()
                    self._restore_live_position_from_exchange(
                        exchange_position,
                        entry_context=entry,
                        spot_balances=spot_balances,
                    )
                self._resolve_pending_intent(intent_id)
                logger.warning(
                    "Auto-reconciled stale ENTER for %s from live exchange state",
                    symbol,
                )
                self._record_pending_intent_self_heal(
                    symbol=symbol,
                    intent_type="ENTER",
                    reason="live_position_present_on_exchange",
                    sample_time=now.isoformat(),
                )
                continue

            if symbol in open_order_symbols:
                # Order is still live on the exchange but we have no fill confirmation.
                # Cancel it and give up — the portfolio allocator will re-pick the best
                # symbol on its next cycle.
                attempts = self._stale_enter_cancel_attempts.get(symbol, 0)
                if attempts >= _STALE_ENTER_MAX_CANCEL_ATTEMPTS:
                    logger.critical(
                        "Stale ENTER for %s has exceeded %d cancel attempts; "
                        "parked until watchdog restart — manual intervention required",
                        symbol,
                        _STALE_ENTER_MAX_CANCEL_ATTEMPTS,
                    )
                    continue
                cancel_ok = await self._cancel_enter_orders_for_symbol(symbol, snapshot)
                if not cancel_ok:
                    self._stale_enter_cancel_attempts[symbol] = attempts + 1
                    logger.warning(
                        "Cancel of stale ENTER orders for %s failed (attempt %d/%d); will retry",
                        symbol,
                        attempts + 1,
                        _STALE_ENTER_MAX_CANCEL_ATTEMPTS,
                    )
                    continue
                # Cancel succeeded — clear stale intent and let allocator re-select next cycle.
                # Update DB status for audit but do not delete the record (mirrors the
                # stale-EXIT cancel-and-resubmit pattern at _STALE_EXIT_MAX_RESUBMIT_ATTEMPTS).
                self._stale_enter_cancel_attempts.pop(symbol, None)
                self._stale_pending_enters.pop(symbol, None)
                self.state_writer.update_pending_intent(
                    intent_id,
                    status="CANCELED",
                    last_error="stale_enter_cancel_and_give_up",
                )
                logger.warning(
                    "Stale ENTER for %s: cancelled open order and gave up after %d attempt(s) "
                    "(portfolio allocator will re-pick on next cycle)",
                    symbol,
                    attempts + 1,
                )
                self._record_pending_intent_self_heal(
                    symbol=symbol,
                    intent_type="ENTER",
                    reason="stale_enter_cancel_and_give_up",
                    sample_time=now.isoformat(),
                )
                continue

            self._stale_pending_enters.pop(symbol, None)
            self._resolve_pending_intent(intent_id)
            logger.warning(
                "Auto-cleared stale ENTER for %s because exchange shows no open order or position",
                symbol,
            )
            self._record_pending_intent_self_heal(
                symbol=symbol,
                intent_type="ENTER",
                reason="live_no_open_order_or_position",
                sample_time=now.isoformat(),
            )

        for symbol, intent_id in list(self._pending_exit_intents.items()):
            created_dt = self._parse_timestamp(self._pending_exit_created_at.get(symbol))
            if created_dt is None or (now - created_dt).total_seconds() < (timeout_s + grace_s):
                continue

            if symbol in open_order_symbols:
                # Order still live on exchange but no fill confirmation received.
                # Cancel it and resubmit rather than waiting indefinitely.
                attempts = self._stale_exit_resubmit_attempts.get(symbol, 0)
                if attempts >= _STALE_EXIT_MAX_RESUBMIT_ATTEMPTS:
                    logger.critical(
                        "Stale EXIT for %s has exceeded %d cancel-and-resubmit attempts; "
                        "holding until watchdog restart resolves the position",
                        symbol,
                        _STALE_EXIT_MAX_RESUBMIT_ATTEMPTS,
                    )
                    continue
                cancel_ok = await self._cancel_exit_orders_for_symbol(symbol, snapshot)
                if not cancel_ok:
                    self._stale_exit_resubmit_attempts[symbol] = attempts + 1
                    logger.warning(
                        "Cancel of stale EXIT orders for %s failed (attempt %d/%d); will retry",
                        symbol,
                        attempts + 1,
                        _STALE_EXIT_MAX_RESUBMIT_ATTEMPTS,
                    )
                    continue
                # Cancel succeeded â€” clear the stale intent and resubmit a fresh exit.
                self._pending_exit_intents.pop(symbol, None)
                self._pending_exit_created_at.pop(symbol, None)
                self._stale_pending_exits.discard(symbol)
                self.state_writer.update_pending_intent(
                    intent_id,
                    status="CANCELED",
                    last_error="stale_exit_cancel_and_resubmit",
                )
                self._stale_exit_resubmit_attempts[symbol] = attempts + 1
                await asyncio.sleep(0.5)
                direction = self._position_directions.get(symbol, "long")
                self._dispatch_exit(symbol, urgency=1.0, direction=direction)
                logger.warning(
                    "Stale EXIT for %s: cancelled open order and resubmitted (attempt %d/%d)",
                    symbol,
                    attempts + 1,
                    _STALE_EXIT_MAX_RESUBMIT_ATTEMPTS,
                )
                self._record_pending_intent_self_heal(
                    symbol=symbol,
                    intent_type="EXIT",
                    reason="stale_exit_cancel_and_resubmit",
                    sample_time=now.isoformat(),
                )
                continue

            if symbol in position_rows:
                # Position exists but no open order â€” wait for WS fill event.
                continue

            # Exchange is flat: no open order and no position. Treat as resolved.
            self._pending_exit_intents.pop(symbol, None)
            self._pending_exit_created_at.pop(symbol, None)
            self._stale_pending_exits.discard(symbol)
            self._stale_exit_resubmit_attempts.pop(symbol, None)
            self._clear_startup_recovery_exit_tracking(symbol)
            event = self._exit_events.pop(symbol, None)
            if event is not None:
                event.set()
            # Attempt to record the trade before wiping the position so the
            # alerter sees a proper PnL row instead of +$0.00.
            reconcile_positions = self.state_reader.get_positions()
            reconcile_pos = next(
                (p for p in reconcile_positions if p["symbol"] == symbol), None
            )
            if reconcile_pos:
                self._finalize_exit_fill(
                    symbol,
                    reconcile_pos,
                    event_time=now.isoformat(),
                    execution_type="RECONCILED_FLAT",
                )
            else:
                self.state_writer.remove_position(symbol)
                self._entry_times.pop(symbol, None)
                self._position_directions.pop(symbol, None)
                self._estimated_entry_costs.pop(symbol, None)
            self._resolve_pending_intent(intent_id)
            logger.warning(
                "Auto-reconciled stale EXIT for %s because exchange is flat with no open order",
                symbol,
            )
            self._record_pending_intent_self_heal(
                symbol=symbol,
                intent_type="EXIT",
                reason="live_position_absent_on_exchange",
                sample_time=now.isoformat(),
            )
            self._set_safe_mode_flag("exit_failure", False)

        self._refresh_stale_pending_flag()
        self._try_clear_late_entry_fill()

    async def _self_heal_pending_intents(self) -> None:
        now_monotonic = time.monotonic()
        if now_monotonic - self._last_pending_intent_self_heal_monotonic < 30.0:
            return
        self._last_pending_intent_self_heal_monotonic = now_monotonic

        now = datetime.now(timezone.utc)
        self._prune_abandoned_pending_intents(now)
        if self._trading_mode == "paper":
            self._paper_self_heal_stale_pending_intents(now)
        else:
            await self._live_self_heal_stale_pending_intents(now)

    def _has_stale_pending_intents(self) -> bool:
        return bool(self._stale_pending_enters) or bool(self._stale_pending_exits)

    def _refresh_stale_pending_flag(self) -> None:
        self._set_safe_mode_flag(
            "stale_pending_intent",
            self._has_stale_pending_intents(),
        )

    def _transition_late_entry_fill(self) -> None:
        # Clear the stale-intent guard in the same runtime-state write when the only
        # remaining issue is a late fill. This avoids transient "position opened +
        # stale_pending_intent" snapshots in the alerter.
        if not self._has_stale_pending_intents():
            self._safe_mode_flags.discard("stale_pending_intent")
        self._safe_mode_flags.add("late_entry_fill")
        self._recompute_runtime_mode()

    def _try_clear_late_entry_fill(self) -> None:
        if "late_entry_fill" not in self._safe_mode_flags:
            return
        if not self._stale_pending_enters and not self._pending_enters:
            self._set_safe_mode_flag("late_entry_fill", False)

    def _record_fill_persistence_failure(self, symbol: str, intent_stage: str) -> None:
        logger.exception(
            "Failed to persist %s fill state for %s; keeping the symbol pending and blocking new risk until storage is healthy",
            intent_stage,
            symbol,
        )
        self._set_safe_mode_flag("state_store_write", True)
        self._refresh_stale_pending_flag()

    def _finalize_entry_fill(self, symbol: str, entry: dict, **fill_kwargs) -> None:
        def _float_or_none(value):
            if value is None:
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        def _pick_price(*candidates):
            for candidate in candidates:
                value = _float_or_none(candidate)
                if value is not None and value > 0.0:
                    return value
            return None

        direction = str(entry.get("direction", "long"))
        side_label = "SHORT_SPOT_LONG_PERP" if direction == "short" else "LONG_SPOT_SHORT_PERP"
        fill_time = str(fill_kwargs.get("event_time") or datetime.now(timezone.utc).isoformat())
        estimated_entry_cost_usd = float(entry.get("estimated_entry_cost_usd", 0.0))
        spot_entry_price = _pick_price(
            fill_kwargs.get("spot_fill_price"),
            fill_kwargs.get("avg_fill_price"),
            fill_kwargs.get("last_fill_price"),
            entry.get("entry_price"),
        ) or float(entry["entry_price"])
        perp_entry_price = _pick_price(
            fill_kwargs.get("perp_fill_price"),
            fill_kwargs.get("avg_fill_price"),
            fill_kwargs.get("last_fill_price"),
            entry.get("entry_price"),
        ) or float(entry["entry_price"])
        self.state_writer.upsert_position(
            symbol=symbol,
            side=side_label,
            spot_entry=spot_entry_price,
            perp_entry=perp_entry_price,
            qty=float(entry["qty"]),
            ann_funding=_float_or_zero(entry.get("ann_funding")),
            entry_ann_funding=_float_or_zero(entry.get("ann_funding")),
            spot_live=spot_entry_price,
            perp_live=perp_entry_price,
            direction=direction,
            status="OPEN",
            updated_at=fill_time,
        )
        self._entry_times[symbol] = fill_time
        self._position_directions[symbol] = direction
        self._estimated_entry_costs[symbol] = estimated_entry_cost_usd
        self._set_safe_mode_flag("state_store_write", False)
        logger.info(
            "Position opened for %s qty=%.5f spot=%.2f perp=%.2f (direction=%s)",
            symbol,
            float(entry["qty"]),
            spot_entry_price,
            perp_entry_price,
            direction,
        )

    def _handle_failed_order_update(self, symbol: str, status: str, **event_kwargs) -> None:
        terminal_status = status.upper()
        if terminal_status not in {"REJECTED", "CANCELED", "CANCELLED", "EXPIRED", "FAILED"}:
            return

        client_order_id = str(event_kwargs.get("client_order_id") or "")
        failed_entry: dict | None = None
        if symbol in self._pending_enters:
            entry = self._pending_enters.pop(symbol)
            failed_entry = dict(entry)
            self.state_writer.update_pending_intent(
                str(entry.get("intent_id") or ""),
                status=terminal_status,
                last_error=f"entry_{terminal_status.lower()}",
                client_order_id=client_order_id or None,
            )
            logger.error("Entry for %s failed with status %s", symbol, terminal_status)

        stale_entry = self._stale_pending_enters.pop(symbol, None)
        if stale_entry is not None:
            if failed_entry is None:
                failed_entry = dict(stale_entry)
            self.state_writer.update_pending_intent(
                str(stale_entry.get("intent_id") or ""),
                status=terminal_status,
                last_error=f"entry_{terminal_status.lower()}",
                client_order_id=client_order_id or None,
            )
        abandoned_entry = self._abandoned_pending_enters.pop(symbol, None)
        if abandoned_entry is not None:
            logger.warning(
                "Terminal update %s arrived for %s after a paper-mode ENTER intent was auto-cleared",
                terminal_status,
                symbol,
            )

        if failed_entry is not None:
            self._queue_entry_failure_exchange_reconciliation(
                symbol=symbol,
                entry=failed_entry,
                terminal_status=terminal_status,
                execution_type=str(event_kwargs.get("execution_type") or ""),
                client_order_id=client_order_id,
            )

        if symbol in self._pending_exit_intents:
            intent_id = self._pending_exit_intents.pop(symbol, None)
            self._pending_exit_created_at.pop(symbol, None)
            self._stale_pending_exits.discard(symbol)
            self._stale_exit_resubmit_attempts.pop(symbol, None)
            if intent_id:
                self.state_writer.update_pending_intent(
                    intent_id,
                    status=terminal_status,
                    last_error=f"exit_{terminal_status.lower()}",
                    client_order_id=client_order_id or None,
                )
            self._exit_events.pop(symbol, None)
            failure_reason = str(event_kwargs.get("execution_type") or terminal_status).strip() or terminal_status
            if self._is_startup_manual_review_symbol(symbol):
                self._record_startup_recovery_exit_failure(symbol, failure_reason)
                logger.warning(
                    "Startup recovery exit for %s failed with status %s (%s); leaving the symbol blocked for operator review",
                    symbol,
                    terminal_status,
                    failure_reason,
                )
            else:
                logger.critical(
                    "Exit for %s failed with status %s; blocking new risk until reconciled",
                    symbol,
                    terminal_status,
                )
                self._set_safe_mode_flag("exit_failure", True)
        elif symbol in self._abandoned_exit_intents:
            self._abandoned_exit_intents.pop(symbol, None)
            logger.warning(
                "Terminal update %s arrived for %s after a paper-mode EXIT intent was auto-cleared",
                terminal_status,
                symbol,
            )

        self._refresh_stale_pending_flag()

    def _expire_stale_pending_intents(self) -> None:
        timeout_s = self._intent_timeout_seconds()
        now = datetime.now(timezone.utc)
        stale_exit_symbols: list[str] = []

        for symbol, entry in list(self._pending_enters.items()):
            created_dt = self._parse_timestamp(str(entry.get("entry_time") or ""))
            if created_dt is None or (now - created_dt).total_seconds() < timeout_s:
                continue
            stale_entry = self._pending_enters.pop(symbol)
            stale_entry["timed_out_at"] = now.isoformat()
            self._stale_pending_enters[symbol] = stale_entry
            self.state_writer.update_pending_intent(
                str(stale_entry.get("intent_id") or ""),
                status="TIMEOUT",
                last_error="pending_enter_timeout",
            )
            logger.error(
                "Pending ENTER for %s timed out after %.0fs; symbol remains blocked until a terminal update arrives",
                symbol,
                timeout_s,
            )

        for symbol, intent_id in list(self._pending_exit_intents.items()):
            created_dt = self._parse_timestamp(self._pending_exit_created_at.get(symbol))
            if created_dt is None or (now - created_dt).total_seconds() < timeout_s:
                continue
            stale_exit_symbols.append(symbol)
            if symbol not in self._stale_pending_exits:
                self._stale_pending_exits.add(symbol)
                self.state_writer.update_pending_intent(
                    intent_id,
                    status="TIMEOUT",
                    last_error="pending_exit_timeout",
                )
                logger.critical(
                    "Pending EXIT for %s is older than %.0fs; trading remains in safe mode until it resolves",
                    symbol,
                    timeout_s,
                )

        self.state_writer.set_risk_snapshot(
            {
                "stale_pending_enter_symbols": sorted(self._stale_pending_enters.keys()),
                "stale_pending_exit_symbols": sorted(stale_exit_symbols),
            }
        )
        self._refresh_stale_pending_flag()

    def _current_risk_limits(
        self,
        active_symbol_count: int = 0,
        *,
        manual_review_only: bool = False,
    ) -> RiskLimits:
        target_positions = max(1, int(self._config.get("target_concurrent_positions")))
        effective_symbol_concentration = MAX_SYMBOL_CONCENTRATION
        if manual_review_only:
            effective_symbol_concentration = 1.0
        elif active_symbol_count > 0:
            equal_weight_limit = 1.0 / max(1, min(active_symbol_count, target_positions))
            effective_symbol_concentration = max(effective_symbol_concentration, equal_weight_limit)
        return RiskLimits(
            max_gross_exposure_usd=float(self._config.get("max_gross_exposure_usd")),
            max_symbol_concentration=effective_symbol_concentration,
            soft_drawdown_pct=float(self._config.get("soft_drawdown_pct")),
            max_drawdown_pct=float(self._config.get("max_drawdown_pct")),
            max_data_staleness_minutes=MAX_ALLOWED_GAP_MINUTES,
            max_latency_ms=int(self._config.get("max_venue_latency_ms")),
            max_consecutive_losses=max(1, int(self._config.get("loss_streak_trigger"))),
        )

    def _maybe_log_risk_engine_state(self, decision: RiskDecision) -> None:
        if not decision.reasons:
            self._last_risk_log_signature = None
            self._last_risk_log_monotonic = 0.0
            return
        signature = (
            bool(decision.kill_switch),
            bool(decision.derisk_required),
            bool(decision.allow_new_risk),
            tuple(decision.reasons),
        )
        now_monotonic = time.monotonic()
        if (
            signature != self._last_risk_log_signature
            or now_monotonic - self._last_risk_log_monotonic >= 60.0
        ):
            log_fn = logger.critical if decision.kill_switch else logger.warning
            log_fn("RISK ENGINE: %s", "; ".join(decision.reasons))
            self._last_risk_log_signature = signature
            self._last_risk_log_monotonic = now_monotonic

    def _liquidity_adjusted_open_pnl(self, rows: list[dict]) -> tuple[float, float, float]:
        mark_to_market_open_pnl = 0.0
        liquidity_adjusted_open_pnl = 0.0
        total_exit_cost_usd = 0.0
        for row in rows:
            mark_pnl = _float_or_zero(row.get("net_pnl_usd"))
            mark_to_market_open_pnl += mark_pnl

            symbol = str(row.get("symbol", "")).upper()
            if symbol in self._startup_recovery_stuck_symbols:
                # Keep mark-to-market visibility for operators, but do not let a
                # known-stuck startup orphan feed risk-triggered auto-unwind loops.
                continue

            qty = _float_or_zero(row.get("qty"))
            if not symbol or qty <= 0.0:
                liquidity_adjusted_open_pnl += mark_pnl
                continue

            spot_live, perp_live = self._leg_mark_prices(symbol, row)
            spot_entry = _float_or_zero(row.get("spot_entry")) or spot_live or perp_live
            perp_entry = _float_or_zero(row.get("perp_entry")) or perp_live or spot_entry
            one_sided_notional = qty * max(spot_live, perp_live, spot_entry, perp_entry, 0.0)
            mid_price = max((spot_live + perp_live) / 2.0, 1e-9) if spot_live > 0.0 and perp_live > 0.0 else 0.0
            spread_bps = abs(perp_live - spot_live) / mid_price * 10_000.0 if mid_price > 0.0 else 0.0
            exit_cost_usd = blended_exit_cost(
                one_sided_notional,
                depth_usd=self._cost_depth_or_default(self.depth_tracker.get_exit_depth(symbol)),
                spread_bps=spread_bps,
                maker_fill_probability=0.0,
            )
            total_exit_cost_usd += exit_cost_usd
            liquidity_adjusted_open_pnl += mark_pnl - exit_cost_usd
        return mark_to_market_open_pnl, liquidity_adjusted_open_pnl, total_exit_cost_usd

    def _stress_test_summary(
        self,
        rows: list[dict],
        *,
        current_liquidity_adjusted_open_pnl: float,
        current_account_equity: float,
    ) -> dict[str, float]:
        crash_pct = max(0.0, float(self._config.get("stress_test_spot_crash_pct")))
        stress_mark_to_market_open_pnl = 0.0
        stress_liquidity_adjusted_open_pnl = 0.0
        stress_exit_cost_usd = 0.0
        for row in rows:
            symbol = str(row.get("symbol", "")).upper()
            qty = _float_or_zero(row.get("qty"))
            if not symbol or qty <= 0.0:
                continue
            direction = str(row.get("direction") or self._position_directions.get(symbol) or "long")
            spot_live, perp_live = self._leg_mark_prices(symbol, row)
            spot_entry = _float_or_zero(row.get("spot_entry")) or spot_live or perp_live
            perp_entry = _float_or_zero(row.get("perp_entry")) or perp_live or spot_entry
            spot_scenario = max(0.0, spot_live * (1.0 - crash_pct))
            perp_scenario = max(0.0, perp_live * (1.0 - crash_pct))
            if direction == "short":
                spot_pnl = (spot_entry - spot_scenario) * qty if spot_entry > 0.0 else 0.0
                perp_pnl = (perp_scenario - perp_entry) * qty if perp_entry > 0.0 else 0.0
            else:
                spot_pnl = (spot_scenario - spot_entry) * qty if spot_entry > 0.0 else 0.0
                perp_pnl = (perp_entry - perp_scenario) * qty if perp_entry > 0.0 else 0.0
            scenario_mark_pnl = spot_pnl + perp_pnl
            stress_mark_to_market_open_pnl += scenario_mark_pnl

            one_sided_notional = qty * max(spot_scenario, perp_scenario, spot_entry, perp_entry, 0.0)
            mid_price = (
                max((spot_scenario + perp_scenario) / 2.0, 1e-9)
                if spot_scenario > 0.0 and perp_scenario > 0.0
                else 0.0
            )
            spread_bps = abs(perp_scenario - spot_scenario) / mid_price * 10_000.0 if mid_price > 0.0 else 0.0
            exit_cost_usd = blended_exit_cost(
                one_sided_notional,
                depth_usd=self._cost_depth_or_default(self.depth_tracker.get_exit_depth(symbol)),
                spread_bps=spread_bps,
                maker_fill_probability=0.0,
            )
            stress_exit_cost_usd += exit_cost_usd
            stress_liquidity_adjusted_open_pnl += scenario_mark_pnl - exit_cost_usd

        stress_account_equity = (
            current_account_equity
            - current_liquidity_adjusted_open_pnl
            + stress_liquidity_adjusted_open_pnl
        )
        peak_equity = max(self._peak_account_equity, current_account_equity)
        stress_drawdown_pct = (
            max(0.0, (peak_equity - stress_account_equity) / peak_equity)
            if peak_equity > 0.0
            else 0.0
        )
        return {
            "stress_test_spot_crash_pct": crash_pct,
            "stress_test_mark_to_market_open_pnl_usd": stress_mark_to_market_open_pnl,
            "stress_test_liquidity_adjusted_open_pnl_usd": stress_liquidity_adjusted_open_pnl,
            "stress_test_exit_cost_usd": stress_exit_cost_usd,
            "stress_test_account_equity_usd": stress_account_equity,
            "stress_test_drawdown_pct": stress_drawdown_pct,
            "survival_margin_buffer_usd": stress_account_equity,
        }

    def _estimate_account_equity(
        self,
        rows: list[dict],
        *,
        liquidity_exit_cost_usd: float = 0.0,
        open_pnl_override: float | None = None,
    ) -> float:
        current_mark_to_market_open_pnl = sum(_float_or_zero(row.get("net_pnl_usd")) for row in rows)
        open_pnl = (
            float(open_pnl_override)
            if open_pnl_override is not None
            else current_mark_to_market_open_pnl - max(0.0, liquidity_exit_cost_usd)
        )

        if self._trading_mode != "paper":
            if (
                self._latest_exchange_account_equity is not None
                and self._latest_exchange_account_equity > 0.0
            ):
                return (
                    float(self._latest_exchange_account_equity)
                    - current_mark_to_market_open_pnl
                    + open_pnl
                )

            risk_state = self.state_reader.get_risk()
            cached_mark_to_market_equity = _float_or_zero(risk_state.get("account_equity_mark_to_market"))
            if cached_mark_to_market_equity > 0.0:
                cached_mark_to_market_open_pnl = _float_or_zero(risk_state.get("mark_to_market_open_pnl_usd"))
                return cached_mark_to_market_equity - cached_mark_to_market_open_pnl + open_pnl

        starting_equity = float(self._config.get("account_equity_usd"))
        realized_pnl = sum(
            _float_or_zero(trade.get("net_pnl_usd"))
            for trade in self.state_reader.get_trades(limit=5_000, session_scoped=False)
        )
        return starting_equity + realized_pnl + open_pnl

    def _estimate_data_staleness_minutes(self, rows: list[dict]) -> int:
        funding_status = self.funding_ranker.status_snapshot()
        funding_age_minutes = int(max(0.0, _float_or_zero(funding_status.get("funding_last_refresh_age_s"))) // 60)
        tracked_symbols = [str(row.get("symbol", "")) for row in rows if row.get("symbol")] or list(self.monitored_symbols)
        latest_mark_age_s = 0.0
        now_monotonic = time.monotonic()
        for symbol in tracked_symbols:
            updated_at = self._mark_price_updated_monotonic.get(symbol)
            if updated_at is None:
                latest_mark_age_s = max(latest_mark_age_s, float(MAX_ALLOWED_GAP_MINUTES * 60))
                continue
            latest_mark_age_s = max(latest_mark_age_s, max(0.0, now_monotonic - updated_at))
        mark_age_minutes = int(latest_mark_age_s // 60)
        return max(funding_age_minutes, mark_age_minutes)

    def _heartbeat_implied_venue_latency_ms(self) -> int:
        latency_ms = max(0, int(self._last_heartbeat_rtt_ms))
        miss_threshold = max(1, int(self._config.get("heartbeat_miss_threshold")))
        if self._heartbeat_misses < miss_threshold:
            return latency_ms

        interval_ms = max(1, int(self._config.get("heartbeat_interval_seconds"))) * 1000
        overdue_ms = max(0, self._heartbeat_misses - miss_threshold + 1) * interval_ms
        # Cap the calculated latency at a reasonable maximum to prevent the risk engine
        # from triggering on stale/missed heartbeats when connectivity is actually fine.
        max_configured_latency = max(400, int(self._config.get('max_venue_latency_ms', 400)))
        max_latency_cap = max_configured_latency * 2
        return min(max(latency_ms, overdue_ms), max_latency_cap)

    def _evaluate_risk_controls(self, rows: list[dict]) -> RiskDecision:
        gross_by_symbol: dict[str, float] = {}
        for row in rows:
            symbol = str(row.get("symbol", "")).upper()
            qty = _float_or_zero(row.get("qty"))
            if not symbol or qty <= 0.0:
                continue
            if symbol in self._startup_recovery_stuck_symbols:
                continue
            # manual_review positions cannot be auto-exited by the normal allocator
            # flow, so counting them toward gross exposure would cause a permanent
            # SAFE_MODE deadlock when their notional pushes the total over the limit.
            # The derisk path handles them separately (dispatching exits there too).
            if str(row.get("recovery_state") or "").strip().lower() == "manual_review":
                continue
            spot_live = _float_or_zero(row.get("spot_live")) or _float_or_zero(row.get("spot_entry"))
            perp_live = _float_or_zero(row.get("perp_live")) or _float_or_zero(row.get("perp_entry"))
            leg_price = max(spot_live, perp_live, 0.0)
            if leg_price <= 0.0:
                # No price data yet (e.g. mark prices not yet received at startup);
                # skip so this symbol does not inflate active_symbol_count and
                # artificially tighten the equal-weight concentration limit.
                continue
            # Gross exposure is measured one-sided so it aligns with the configured
            # max_gross_exposure_usd budget derived from slot notional and leverage.
            gross_by_symbol[symbol] = qty * leg_price

        gross_exposure = sum(gross_by_symbol.values())
        largest_symbol_gross_exposure = max(gross_by_symbol.values(), default=0.0)
        active_symbol_count = len(gross_by_symbol)
        open_rows = [row for row in rows if row.get("symbol")]
        manual_review_only = bool(open_rows) and all(
            str(row.get("recovery_state") or "").strip().lower() == "manual_review"
            for row in open_rows
        )
        self._risk_engine.limits = self._current_risk_limits(
            active_symbol_count=active_symbol_count,
            manual_review_only=manual_review_only,
        )
        # Measure concentration against configured portfolio capacity so startup
        # and partial scale-ins do not look like a fully concentrated book.
        concentration_denominator = max(gross_exposure, self._risk_engine.limits.max_gross_exposure_usd)
        symbol_concentration = (
            largest_symbol_gross_exposure / concentration_denominator
            if concentration_denominator > 0.0
            else 0.0
        )
        mark_to_market_open_pnl, liquidity_adjusted_open_pnl, liquidity_exit_cost_usd = self._liquidity_adjusted_open_pnl(rows)
        account_equity = self._estimate_account_equity(
            rows,
            liquidity_exit_cost_usd=liquidity_exit_cost_usd,
            open_pnl_override=liquidity_adjusted_open_pnl,
        )
        if account_equity > self._peak_account_equity:
            self._peak_account_equity = account_equity
        drawdown_pct = (
            max(0.0, (self._peak_account_equity - account_equity) / self._peak_account_equity)
            if self._peak_account_equity > 0.0
            else 0.0
        )
        venue_latency_ms = self._heartbeat_implied_venue_latency_ms()
        stress_summary = self._stress_test_summary(
            rows,
            current_liquidity_adjusted_open_pnl=liquidity_adjusted_open_pnl,
            current_account_equity=account_equity,
        )

        decision = self._risk_engine.evaluate(
            RiskState(
                gross_exposure_usd=gross_exposure,
                symbol_concentration=symbol_concentration,
                drawdown_pct=drawdown_pct,
                data_staleness_minutes=self._estimate_data_staleness_minutes(rows),
                venue_latency_ms=venue_latency_ms,
                consecutive_losses=self._loss_streak,
            )
        )

        self._risk_allow_new_risk = decision.allow_new_risk
        self._risk_derisk_required = decision.derisk_required
        self._risk_kill_switch = decision.kill_switch
        self._risk_position_scale = decision.position_scale
        self._risk_reasons = list(decision.reasons)
        self._current_gross_exposure_usd = gross_exposure
        self._current_gross_by_symbol = dict(gross_by_symbol)
        self._risk_last_evaluated_at = datetime.now(timezone.utc).isoformat()

        self.state_writer.set_stat("account_equity", account_equity)
        self.state_writer.set_stat("gross_exposure", gross_exposure)
        self.state_writer.set_risk_snapshot(
            {
                "account_equity": account_equity,
                "account_equity_mark_to_market": self._estimate_account_equity(
                    rows,
                    open_pnl_override=mark_to_market_open_pnl,
                ),
                "account_equity_high_watermark": self._peak_account_equity,
                "gross_exposure": gross_exposure,
                "gross_exposure_convention": "one_sided",
                "largest_symbol_gross_exposure": largest_symbol_gross_exposure,
                "symbol_concentration": symbol_concentration,
                "symbol_concentration_denominator_usd": concentration_denominator,
                "effective_max_symbol_concentration": self._risk_engine.limits.max_symbol_concentration,
                "mark_to_market_open_pnl_usd": mark_to_market_open_pnl,
                "liquidity_adjusted_open_pnl_usd": liquidity_adjusted_open_pnl,
                "liquidity_adjusted_exit_cost_usd": liquidity_exit_cost_usd,
                "drawdown_pct": drawdown_pct,
                "venue_latency_ms": venue_latency_ms,
                "kill_switch": decision.kill_switch,
                "risk_reasons": decision.reasons,
                **stress_summary,
            }
        )
        self.state_writer.flush()
        self._set_safe_mode_flag("risk_limits", decision.derisk_required or decision.kill_switch)
        return decision

    def _on_depth_update(self, symbol: str, market: str, bids: list, asks: list) -> None:
        """Update depth cache; capture top perp bid as mark price proxy."""
        self._last_telemetry_event_monotonic = time.monotonic()
        self.depth_tracker.on_l2depth(symbol, market, bids, asks)
        self.regime_filter.on_depth_update(symbol)
        # Note: mark prices are now primarily set via _on_mark_price from MarkPrice WS events.
        # This depth-based fallback is kept for robustness if MarkPrice stream is delayed.

    def _on_mark_price(self, symbol: str, mark_price: float, next_funding_rate: float) -> None:
        """Update FundingRanker with live WS funding rate (~1s cadence).

        This provides sub-minute rate resolution compared to the 60s REST fallback,
        enabling the post-snapshot decay exit and rotation logic to react immediately
        when funding collapses at settlement rather than waiting for the next REST poll.
        """
        self._last_telemetry_event_monotonic = time.monotonic()
        self.funding_ranker.update_rate(symbol, next_funding_rate)
        self.predictor.push_sample(symbol, next_funding_rate * 1095)
        self.regime_filter.on_mark_price(symbol, mark_price, next_funding_rate * 1095.0)
        # Also keep mark price cache fresh for ENTER quantity calculations.
        if mark_price > 0.0:
            self._mark_prices[symbol] = mark_price
            self._mark_price_ready.add(symbol)
            self._mark_price_updated_monotonic[symbol] = time.monotonic()

    def _on_heartbeat_ack(self, heartbeat_id: str | None, status: str, ts_ms=None) -> None:
        if str(status).lower() != "ok":
            return
        now_monotonic = time.monotonic()
        self._last_telemetry_event_monotonic = now_monotonic
        self._last_heartbeat_ack_monotonic = now_monotonic
        self._heartbeat_misses = 0
        self._last_heartbeat_ack_id = str(heartbeat_id or "")
        if (
            self._last_heartbeat_ack_id
            and self._last_heartbeat_ack_id == self._last_heartbeat_sent_id
            and self._last_heartbeat_sent_monotonic > 0.0
        ):
            self._last_heartbeat_rtt_ms = max(
                0,
                int((now_monotonic - self._last_heartbeat_sent_monotonic) * 1000),
            )
        self._last_heartbeat_ack_at = _iso_from_ms(ts_ms)
        self._set_safe_mode_flag("heartbeat_bridge", False)

    def _on_volume_bar(self, symbol: str, minute_start_ms, notional_usd: float) -> None:
        self._last_telemetry_event_monotonic = time.monotonic()
        minute_iso = _iso_from_ms(minute_start_ms)
        self._latest_volume_bar[symbol] = (minute_iso[:16], _float_or_zero(notional_usd))
        self.regime_filter.on_volume_bar(symbol, _float_or_zero(notional_usd))

    def _on_order_rejected(self, symbol: str, intent: str, intent_id: str | None, reason: str) -> None:
        """Rust rejected an instruction â€” for exits, clear pending state and schedule an immediate retry."""
        logger.warning(
            "OrderRejected from Rust: symbol=%s intent=%s reason=%s intent_id=%s",
            symbol, intent, reason, intent_id,
        )
        is_exit = intent in ("EXIT_LONG", "EXIT_SHORT")
        if not is_exit:
            return
        tracked_id = self._pending_exit_intents.get(symbol)
        if intent_id and tracked_id and tracked_id != intent_id:
            # Stale rejection for an intent we've already superseded â€” ignore.
            return
        if symbol in self._pending_exit_intents:
            self._pending_exit_intents.pop(symbol, None)
            self._pending_exit_created_at.pop(symbol, None)
            self._stale_pending_exits.discard(symbol)
            if intent_id:
                self.state_writer.update_pending_intent(intent_id, status="REJECTED", last_error=reason)
            direction = "short" if intent == "EXIT_SHORT" else "long"
            logger.warning("Retrying rejected EXIT for %s (reason: %s)", symbol, reason)
            asyncio.ensure_future(self._retry_rejected_exit(symbol, direction))

    async def _retry_rejected_exit(self, symbol: str, direction: str) -> None:
        """Re-dispatch an exit that Rust rejected, after a brief delay."""
        await asyncio.sleep(0.5)
        self._dispatch_exit(symbol, urgency=1.0, direction=direction)

    def _entry_policy_block_reason(self, risk_state: dict | None = None) -> str | None:
        if self._runtime_mode == "BLOCKED":
            return f"blocked: {self._blocked_reason or 'unknown'}"
        if self._runtime_mode == "SAFE_MODE":
            return f"safe mode: {self._safe_mode_reason() or 'operator guard'}"
        if self._preflight_status != "passed":
            return (
                "starting up: preflight still running"
                if self._preflight_status in {"idle", "running"}
                else f"starting up: preflight {self._preflight_status.replace('_', ' ')}"
            )
        if self._operator_pause_new_entries_bridge:
            return "new entries paused by admin action"
        if bool(self._config.get("pause_new_entries")):
            return "new entries paused by operator"
        if self._risk_kill_switch:
            return "kill switch active"
        if not self._risk_allow_new_risk:
            return "risk engine blocked new exposure"
        risk_state = risk_state if risk_state is not None else self.state_reader.get_risk()
        if risk_state.get("pause_new_entries") is True:
            return "new entries paused by operator"
        if risk_state.get("kill_switch") or risk_state.get("is_kill_switch"):
            return "kill switch active"
        if risk_state.get("allow_new_risk") is False:
            return "allow_new_risk=false"
        return None

    def _external_entry_block_reason(self) -> str | None:
        return self._entry_policy_block_reason()

    def _refresh_open_position_metrics(self, rows: list[dict] | None = None) -> list[dict]:
        rows = rows if rows is not None else self.state_reader.get_positions()
        funding_signal_available = (
            self.funding_ranker.status_snapshot().get("funding_staleness_status") == "fresh"
        )
        for row in rows:
            symbol = str(row.get("symbol", ""))
            if not symbol:
                continue

            ann_funding = (
                self.funding_ranker.get_rate(symbol)
                if funding_signal_available
                else _float_or_zero(row.get("ann_funding"))
            )
            spot_live, perp_live = self._leg_mark_prices(symbol, row)

            qty = _float_or_zero(row.get("qty"))
            spot_entry = _float_or_zero(row.get("spot_entry"))
            perp_entry = _float_or_zero(row.get("perp_entry"))
            direction = str(row.get("direction", "long"))
            net_pnl_usd = _float_or_zero(row.get("net_pnl_usd"))
            exchange_pnl_usd = _float_or_zero(row.get("exchange_pnl_usd"))
            hedge_ratio = min(1.0, max(0.0, _float_or_zero(row.get("hedge_ratio"))))
            if direction != "long":
                hedge_ratio = 0.0

            perp_pnl = self._perp_leg_open_pnl(
                qty=qty,
                direction=direction,
                perp_entry=perp_entry,
                perp_live=perp_live,
            )
            if abs(perp_pnl) > 0.0:
                exchange_pnl_usd = perp_pnl

            if qty > 0.0 and direction == "long" and spot_live > 0.0 and perp_live > 0.0:
                hedged_qty = qty * hedge_ratio
                spot_pnl = (spot_live - spot_entry) * hedged_qty if spot_entry > 0.0 else 0.0
                combined_pnl = spot_pnl + perp_pnl
                basis_tolerance = max(1e-9, max(abs(spot_entry), abs(perp_entry), 1.0) * 1e-6)
                recovered_basis_uncertain = (
                    str(row.get("recovery_state") or "").strip().lower() in {"tracked", "exit_candidate", "manual_review"}
                    and abs(spot_entry - perp_entry) <= basis_tolerance
                    and abs(perp_pnl) > 0.0
                    and abs(combined_pnl) < max(1.0, abs(perp_pnl) * 0.25)
                )
                net_pnl_usd = perp_pnl if recovered_basis_uncertain else combined_pnl
            elif abs(exchange_pnl_usd) > 0.0:
                net_pnl_usd = exchange_pnl_usd

            if str(row.get("recovery_state") or "").strip():
                recovery_state, recovery_note = self._classify_startup_recovered_position(
                    symbol=symbol,
                    direction=direction,
                    ann_funding=ann_funding,
                    hedge_ratio=hedge_ratio,
                    unsupported_direction=direction != "long",
                    funding_signal_available=funding_signal_available,
                )
                row["recovery_state"] = recovery_state
                if recovery_state == "exit_candidate":
                    self._startup_exit_candidates[symbol] = recovery_note
                else:
                    self._startup_exit_candidates.pop(symbol, None)
                if recovery_state == "manual_review":
                    self._startup_manual_review_symbols[symbol] = recovery_note
                else:
                    self._startup_manual_review_symbols.pop(symbol, None)

            row["ann_funding"] = ann_funding
            row["spot_live"] = spot_live
            row["perp_live"] = perp_live
            row["net_pnl_usd"] = net_pnl_usd
            row["exchange_pnl_usd"] = exchange_pnl_usd

            self.state_writer.update_position_metrics(
                symbol,
                ann_funding=ann_funding,
                spot_live=spot_live,
                perp_live=perp_live,
                net_pnl_usd=net_pnl_usd,
                exchange_pnl_usd=exchange_pnl_usd,
                recovery_state=row.get("recovery_state", ""),
            )

        hedge_gaps = [
            str(row.get("symbol", "")).upper()
            for row in rows
            if str(row.get("direction", "")).lower() == "long"
            and _float_or_zero(row.get("hedge_ratio")) < (1.0 - _SPOT_HEDGE_SHORTFALL_TOLERANCE_PCT)
        ]
        # hedge_gap is a warning, not a trading halt â€” an unhedged leg on one
        # symbol should not freeze the whole portfolio; new pairs each have their
        # own spot hedge. The gap is tracked in the risk snapshot for visibility.
        self._set_safe_mode_flag("hedge_gap", False)
        if hedge_gaps:
            self.state_writer.set_risk_snapshot({"hedge_gap_symbols": sorted(hedge_gaps)})
        self._refresh_startup_recovery_flags(rows)
        return rows

    def _maybe_process_operator_flatten_all_request(self, rows: list[dict] | None = None) -> bool:
        risk_state = self.state_reader.get_risk()
        request_id = str(risk_state.get("operator_flatten_all_request_id") or "").strip()
        request_status = str(risk_state.get("operator_flatten_all_status") or "").strip().lower()
        requested_by = str(risk_state.get("operator_flatten_all_requested_by") or "").strip()
        if not request_id or request_status in {"", "completed", "failed", "cancelled"}:
            return False

        self._operator_pause_new_entries_bridge = True
        rows = rows if rows is not None else self.state_reader.get_positions()
        open_rows = [row for row in rows if row.get("symbol")]
        now_iso = datetime.now(timezone.utc).isoformat()

        if not open_rows:
            self.state_writer.set_risk_snapshot(
                {
                    "operator_flatten_all_status": "completed",
                    "operator_flatten_all_acknowledged_at": now_iso,
                    "operator_flatten_all_completed_at": now_iso,
                    "operator_flatten_all_dispatched_symbols": [],
                    "operator_flatten_all_remaining_symbols": [],
                    "operator_flatten_all_note": "Portfolio is flat. New entries remain paused.",
                }
            )
            self.state_writer.flush()
            logger.warning(
                "Operator flatten-all request %s completed immediately; portfolio was already flat",
                request_id,
            )
            return False

        dispatched_symbols: list[str] = []
        remaining_symbols: list[str] = []
        for row in open_rows:
            symbol = str(row.get("symbol", "")).upper()
            if not symbol:
                continue
            remaining_symbols.append(symbol)
            if symbol in self._exit_events:
                continue
            direction = str(row.get("direction") or self._position_directions.get(symbol) or "long")
            self._dispatch_exit(symbol, urgency=1.0, direction=direction)
            dispatched_symbols.append(symbol)

        self.state_writer.set_risk_snapshot(
            {
                "operator_flatten_all_status": "in_progress",
                "operator_flatten_all_acknowledged_at": now_iso,
                "operator_flatten_all_dispatched_symbols": dispatched_symbols,
                "operator_flatten_all_remaining_symbols": remaining_symbols,
                "operator_flatten_all_note": (
                    "Waiting for exit fills on all open positions."
                    if remaining_symbols
                    else "Portfolio is flat. New entries remain paused."
                ),
            }
        )
        self.state_writer.flush()
        if dispatched_symbols:
            logger.warning(
                "Operator flatten-all request %s from %s dispatched exits for %s",
                request_id,
                requested_by or "unknown-admin",
                ", ".join(dispatched_symbols),
            )
        return True

    async def _sync_rest_depth_to_tracker(self) -> None:
        """Sync REST fallback depth to the main depth tracker.
        
        This ensures we have depth data even when WebSocket depth isn't flowing.
        """
        updated_count = 0
        ranked = self.funding_ranker.get_ranked()
        symbols_to_sync = self._live_enriched_symbols(ranked)
        if symbols_to_sync:
            self.rest_depth_fetcher.update_symbols(symbols_to_sync)
        for symbol in symbols_to_sync:
            snapshot = self.rest_depth_fetcher.get_snapshot(symbol)
            spot_depth = snapshot["spot_depth_usd"]
            perp_depth = snapshot["perp_depth_usd"]
            # Only update if REST has fresh data
            if self.rest_depth_fetcher.has_fresh_depth(symbol) and (spot_depth > 0 or perp_depth > 0):
                self.depth_tracker.set_rest_snapshot(
                    symbol,
                    spot_depth_usd=spot_depth,
                    perp_depth_usd=perp_depth,
                    spot_bid_price=snapshot["spot_best_bid"],
                    spot_ask_price=snapshot["spot_best_ask"],
                    perp_bid_price=snapshot["perp_best_bid"],
                    perp_ask_price=snapshot["perp_best_ask"],
                )
                self.regime_filter.on_depth_update(symbol)
                updated_count += 1
        if updated_count > 0:
            logger.debug("Synced REST depth for %d symbols to tracker", updated_count)

    def _calculate_trade_pnl(
        self,
        *,
        qty: float,
        direction: str,
        ann_funding: float,
        hold_hours: float,
        funding_periods: float | None = None,
        funding_collected_usd: float | None = None,
        execution_cost_usd: float = 0.0,
        entry_price: float | None = None,
        exit_price: float | None = None,
        spot_entry_price: float | None = None,
        perp_entry_price: float | None = None,
        spot_exit_price: float | None = None,
        perp_exit_price: float | None = None,
    ) -> tuple[float, float, float, float]:
        """Calculate net PnL and funding collected for a funding arbitrage trade.

        For delta-neutral funding arbitrage:
        - The spot and perp positions offset each other, minimizing directional risk
        - Main profit comes from funding payments collected
        - For LONG (long spot + short perp): we receive positive funding
        - For SHORT (short spot + long perp): we receive funding when ann_funding < 0

        Returns: (net_pnl_usd, funding_collected, basis_pnl_usd, borrow_cost_usd)
        """
        spot_entry = _float_or_zero(spot_entry_price) or _float_or_zero(entry_price)
        perp_entry = _float_or_zero(perp_entry_price) or _float_or_zero(entry_price)
        spot_exit = _float_or_zero(spot_exit_price) or _float_or_zero(exit_price)
        perp_exit = _float_or_zero(perp_exit_price) or _float_or_zero(exit_price)
        if qty <= 0:
            return 0.0, 0.0, 0.0, 0.0

        direction = str(direction or "long").lower()
        entry_prices_valid = spot_entry > 0.0 and perp_entry > 0.0
        exit_prices_valid = spot_exit > 0.0 and perp_exit > 0.0

        if entry_prices_valid and exit_prices_valid:
            if direction == "short":
                basis_pnl = ((spot_entry - spot_exit) + (perp_exit - perp_entry)) * qty
            else:
                basis_pnl = ((spot_exit - spot_entry) + (perp_entry - perp_exit)) * qty
        else:
            basis_pnl = 0.0

        notional_usd = ((spot_entry + perp_entry) / 2.0) * qty if entry_prices_valid else 0.0
        funding_collected = (
            funding_collected_usd
            if funding_collected_usd is not None
            else self._synthetic_funding_collected_usd(
                qty=qty,
                direction=direction,
                ann_funding=ann_funding,
                hold_hours=hold_hours,
                funding_periods=funding_periods,
                spot_entry_price=spot_entry,
                perp_entry_price=perp_entry,
            )
        )
        borrow_cost_usd = self._borrow_cost_usd(
            notional_usd=notional_usd,
            hold_hours=max(hold_hours, 0.0),
        )

        net_pnl = basis_pnl + funding_collected - borrow_cost_usd - execution_cost_usd

        return net_pnl, funding_collected, basis_pnl, borrow_cost_usd

    def _finalize_exit_fill(
        self,
        symbol: str,
        pos: dict,
        *,
        event_time: str,
        spot_fill_price=None,
        perp_fill_price=None,
        avg_fill_price=None,
        last_fill_price=None,
        execution_type: str = "RECONCILED_FLAT",
    ) -> None:
        """Record a completed exit trade from either an order fill or a reconciliation event.

        Shared by _on_order_update (live WS fill) and _live_self_heal_stale_pending_intents
        (reconciler detects exchange is flat). Also calls state_writer.remove_position.
        """
        def _float_or_none(value):
            if value is None:
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        def _pick_price(*candidates):
            for candidate in candidates:
                value = _float_or_none(candidate)
                if value is not None and value > 0.0:
                    return value
            return None

        spot_entry_price = _float_or_zero(pos.get("spot_entry"))
        perp_entry_price = _float_or_zero(pos.get("perp_entry")) or spot_entry_price
        fallback_spot_price, fallback_perp_price = self._leg_mark_prices(symbol, pos)
        spot_exit_price = _pick_price(
            spot_fill_price,
            fallback_spot_price,
            pos.get("spot_live"),
            spot_entry_price,
        )
        perp_exit_price = _pick_price(
            perp_fill_price,
            avg_fill_price,
            last_fill_price,
            fallback_perp_price,
            pos.get("perp_live"),
            perp_entry_price,
        )
        if spot_fill_price is None or perp_fill_price is None:
            logger.critical(
                "Recording exit trade for %s with missing fill prices (execution_type=%s); "
                "basis_pnl will be zero — check Rust telemetry completeness",
                symbol,
                execution_type,
            )
        if spot_exit_price is None:
            spot_exit_price = spot_entry_price
            logger.warning("No spot exit price available for %s, using spot entry", symbol)
        if perp_exit_price is None:
            perp_exit_price = perp_entry_price
            logger.warning("No perp exit price available for %s, using perp entry", symbol)

        direction = pos.get("direction", "long")
        side_label = "SHORT_SPOT_LONG_PERP" if direction == "short" else "LONG_SPOT_SHORT_PERP"
        entry_time_str = self._entry_times.pop(symbol, pos.get("updated_at", ""))
        exit_time = event_time

        entry_dt = self._parse_timestamp(entry_time_str)
        exit_dt = self._parse_timestamp(exit_time)
        try:
            if entry_dt is None or exit_dt is None:
                raise ValueError("missing entry or exit timestamp")
            hold_hours = max(0.0, (exit_dt - entry_dt).total_seconds() / 3600)
            funding_periods = float(self._count_funding_settlements(entry_dt, exit_dt))
        except (ValueError, TypeError):
            hold_hours = 0.0
            funding_periods = None
            logger.warning("Could not parse entry time for %s, defaulting hold_hours=0", symbol)

        entry_ann_funding = _float_or_zero(pos.get("entry_ann_funding"))
        ann_funding = entry_ann_funding or _float_or_zero(pos.get("ann_funding"))
        qty = pos["qty"]
        entry_notional_usd = ((spot_entry_price + perp_entry_price) / 2.0) * qty
        estimated_entry_cost_usd = self._estimated_entry_costs.pop(symbol, 0.0)
        if estimated_entry_cost_usd <= 0.0 and entry_notional_usd > 0.0:
            estimated_entry_cost_usd = blended_entry_cost(
                entry_notional_usd,
                depth_usd=_DEFAULT_COST_DEPTH_USD,
            )

        actual_execution_cost_usd = self.state_reader.estimate_trade_execution_cost(
            symbol,
            entry_time_str,
            exit_time,
        )
        execution_cost_usd = actual_execution_cost_usd
        estimated_total_cost_usd = estimated_entry_cost_usd
        if execution_cost_usd <= 0.0:
            exit_notional_usd = ((spot_exit_price + perp_exit_price) / 2.0) * qty
            estimated_exit_cost_usd = blended_exit_cost(
                exit_notional_usd,
                depth_usd=self._cost_depth_or_default(self.depth_tracker.get_exit_depth(symbol)),
            )
            estimated_total_cost_usd += estimated_exit_cost_usd
            execution_cost_usd = estimated_entry_cost_usd + estimated_exit_cost_usd
        else:
            exit_notional_usd = ((spot_exit_price + perp_exit_price) / 2.0) * qty
            estimated_exit_cost_usd = blended_exit_cost(
                exit_notional_usd,
                depth_usd=self._cost_depth_or_default(self.depth_tracker.get_exit_depth(symbol)),
            )
            estimated_total_cost_usd += estimated_exit_cost_usd

        if estimated_total_cost_usd > 0.0:
            cost_model_error_pct = (
                abs(execution_cost_usd - estimated_total_cost_usd) / estimated_total_cost_usd
            ) * 100.0
            self._record_health_metric(
                metric="cost_model_error_pct",
                value=cost_model_error_pct,
                symbol=symbol,
                expected_value=0.0,
                notes="trade execution cost reconciliation",
                sample_time=exit_time,
            )

        funding_collected, funding_source = self._reconcile_funding_cashflows(
            symbol=symbol,
            entry_time=entry_time_str,
            exit_time=exit_time,
            qty=qty,
            direction=direction,
            ann_funding=ann_funding,
            hold_hours=max(hold_hours, 0.0),
            funding_periods=funding_periods,
            spot_entry_price=spot_entry_price,
            perp_entry_price=perp_entry_price,
        )

        net_pnl, funding_collected, basis_pnl_usd, borrow_cost_usd = self._calculate_trade_pnl(
            qty=qty,
            direction=direction,
            ann_funding=ann_funding,
            hold_hours=max(hold_hours, 0.0),
            funding_periods=funding_periods,
            funding_collected_usd=funding_collected,
            execution_cost_usd=execution_cost_usd,
            spot_entry_price=spot_entry_price,
            perp_entry_price=perp_entry_price,
            spot_exit_price=spot_exit_price,
            perp_exit_price=perp_exit_price,
        )

        trade = Trade(
            symbol=symbol,
            side=side_label,
            entry_time=entry_time_str,
            exit_time=exit_time,
            entry_price=(spot_entry_price + perp_entry_price) / 2.0,
            exit_price=(spot_exit_price + perp_exit_price) / 2.0,
            qty=qty,
            net_pnl_usd=net_pnl,
            funding_collected=funding_collected,
            execution_cost_usd=execution_cost_usd,
            basis_pnl_usd=basis_pnl_usd,
            borrow_cost_usd=borrow_cost_usd,
            funding_source=funding_source,
        )
        self.state_writer.record_trade(trade)
        self.state_writer.remove_position(symbol)
        self._position_directions.pop(symbol, None)
        logger.info(
            "Trade recorded for %s pnl=$%.4f funding=$%.4f basis=$%.4f borrow=$%.4f exec_cost=$%.4f hold_h=%.2f source=%s",
            symbol,
            net_pnl,
            funding_collected,
            basis_pnl_usd,
            borrow_cost_usd,
            execution_cost_usd,
            hold_hours,
            funding_source,
        )

    def _on_order_update(self, symbol: str, status: str, filled_qty: float = 0.0, **_kwargs) -> None:
        self._last_telemetry_event_monotonic = time.monotonic()

        def _float_or_none(value):
            if value is None:
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        def _pick_price(*candidates):
            for candidate in candidates:
                value = _float_or_none(candidate)
                if value is not None and value > 0.0:
                    return value
            return None

        event_time = str(_kwargs.get("event_time") or _iso_from_ms(_kwargs.get("event_time_ms")))
        _kwargs["event_time"] = event_time
        event_payload = {
            "symbol": symbol,
            "status": status,
            "filled_qty": filled_qty,
            "client_order_id": _kwargs.get("client_order_id", ""),
            "avg_fill_price": _kwargs.get("avg_fill_price"),
            "last_fill_price": _kwargs.get("last_fill_price"),
            "cumulative_quote_qty": _kwargs.get("cumulative_quote_qty"),
            "commission": _kwargs.get("commission"),
            "commission_asset": _kwargs.get("commission_asset"),
            "realized_pnl": _kwargs.get("realized_pnl"),
            "maker": _kwargs.get("maker"),
            "execution_type": _kwargs.get("execution_type"),
            "event_time": event_time,
            "spot_fill_price": _kwargs.get("spot_fill_price"),
            "perp_fill_price": _kwargs.get("perp_fill_price"),
        }
        self.state_writer.record_execution_event(event_payload)

        client_order_id = str(_kwargs.get("client_order_id") or "")
        pending_enter = self._pending_enters.get(symbol)
        if pending_enter is not None and client_order_id:
            self.state_writer.update_pending_intent(
                str(pending_enter.get("intent_id") or ""),
                client_order_id=client_order_id,
                status=status,
            )
        stale_pending_enter = self._stale_pending_enters.get(symbol)
        if stale_pending_enter is not None and client_order_id:
            self.state_writer.update_pending_intent(
                str(stale_pending_enter.get("intent_id") or ""),
                client_order_id=client_order_id,
                status=status,
            )
        pending_exit_intent_id = self._pending_exit_intents.get(symbol)
        if pending_exit_intent_id is not None and client_order_id:
            self.state_writer.update_pending_intent(
                pending_exit_intent_id,
                client_order_id=client_order_id,
                status=status,
            )

        self._handle_failed_order_update(symbol, status, **_kwargs)
        if status != "FILLED":
            return

        is_cycle_complete = self._is_cycle_completion_event(
            _kwargs.get("execution_type"),
            _kwargs.get("spot_fill_price"),
            _kwargs.get("perp_fill_price"),
        )
        if (
            symbol in self._exit_events
            or symbol in self._abandoned_exit_intents
            or symbol in self._pending_enters
            or symbol in self._stale_pending_enters
            or symbol in self._abandoned_pending_enters
        ) and not is_cycle_complete:
            logger.debug(
                "Ignoring leg-level FILLED for %s until hedge cycle completes (execution_type=%s)",
                symbol,
                _kwargs.get("execution_type"),
            )
            return

        # â”€â”€ Exit fill â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if symbol in self._exit_events or symbol in self._abandoned_exit_intents:
            if symbol in self._abandoned_exit_intents:
                logger.warning(
                    "Late FILLED arrived for %s after a paper-mode EXIT intent was auto-cleared; reconciling position now",
                    symbol,
                )
            logger.info("Exit FILLED confirmed for %s â€” releasing capital slot", symbol)
            positions = self.state_reader.get_positions()
            pos = next((p for p in positions if p["symbol"] == symbol), None)
            if pos:
                self._finalize_exit_fill(
                    symbol,
                    pos,
                    event_time=event_time,
                    spot_fill_price=_kwargs.get("spot_fill_price"),
                    perp_fill_price=_kwargs.get("perp_fill_price"),
                    avg_fill_price=_kwargs.get("avg_fill_price"),
                    last_fill_price=_kwargs.get("last_fill_price"),
                    execution_type=str(_kwargs.get("execution_type") or "TRADE"),
                )
            else:
                logger.critical(
                    "Exit FILLED for %s but no position in DB — likely reconciliation race; "
                    "no trade recorded (check _live_self_heal_stale_pending_intents timing)",
                    symbol,
                )
                self._entry_times.pop(symbol, None)
                self._estimated_entry_costs.pop(symbol, None)
            event = self._exit_events.pop(symbol, None)
            if event is not None:
                event.set()
            abandoned_exit = self._abandoned_exit_intents.pop(symbol, None)
            self._pending_exit_created_at.pop(symbol, None)
            self._stale_pending_exits.discard(symbol)
            self._stale_exit_resubmit_attempts.pop(symbol, None)
            self._clear_startup_recovery_exit_tracking(symbol)
            self._resolve_pending_intent(
                self._pending_exit_intents.pop(symbol, None)
                or str((abandoned_exit or {}).get("intent_id") or "")
            )
            self._set_safe_mode_flag("exit_failure", False)
            self._refresh_stale_pending_flag()

        # â”€â”€ Entry fill â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        elif symbol in self._pending_enters:
            entry = self._pending_enters[symbol]
            try:
                self._finalize_entry_fill(symbol, entry, **_kwargs)
            except Exception:
                self._record_fill_persistence_failure(symbol, "entry")
                return
            self._pending_enters.pop(symbol, None)
            self._resolve_pending_intent(str(entry.get("intent_id") or ""))
            self._refresh_stale_pending_flag()
        elif symbol in self._stale_pending_enters:
            entry = self._stale_pending_enters[symbol]
            logger.critical(
                "Late FILLED arrived for %s after entry timeout; position will be recorded and pending-enter reconciliation will continue",
                symbol,
            )
            self._transition_late_entry_fill()
            try:
                self._finalize_entry_fill(symbol, entry, **_kwargs)
            except Exception:
                self._record_fill_persistence_failure(symbol, "late entry")
                return
            self._stale_pending_enters.pop(symbol, None)
            self._resolve_pending_intent(str(entry.get("intent_id") or ""))
            self._refresh_stale_pending_flag()
            self._try_clear_late_entry_fill()
        elif symbol in self._abandoned_pending_enters:
            entry = self._abandoned_pending_enters[symbol]
            logger.warning(
                "Late FILLED arrived for %s after a paper-mode ENTER intent was auto-cleared; position will be recorded",
                symbol,
            )
            if self._trading_mode != "paper":
                self._transition_late_entry_fill()
            try:
                self._finalize_entry_fill(symbol, entry, **_kwargs)
            except Exception:
                self._record_fill_persistence_failure(symbol, "abandoned entry")
                return
            self._abandoned_pending_enters.pop(symbol, None)
            self._resolve_pending_intent(str(entry.get("intent_id") or ""))
            self._refresh_stale_pending_flag()
            self._try_clear_late_entry_fill()

    def _get_open_positions(self, rows: list[dict] | None = None) -> list[OpenPosition]:
        rows = rows if rows is not None else self.state_reader.get_positions()
        positions = []
        for r in rows:
            if str(r.get("recovery_state") or "").strip().lower() == "manual_review":
                # Recovered manual-review positions remain visible on the dashboard
                # and count toward risk, but we keep them out of allocator/exit
                # automation until an operator resolves the leg mismatch.
                continue
            spot_price = r.get("spot_live", 0.0)
            # If spot_live is populated (price > $1), use actual qty Ã— price.
            # Otherwise fall back to configured slot size (e.g., cold start with stale cache).
            if spot_price > 1.0:
                notional_usd = r["qty"] * spot_price
            else:
                notional_usd = CAPITAL_PER_SLOT_USD * TARGET_LEVERAGE
            positions.append(OpenPosition(
                symbol=r["symbol"],
                notional_usd=notional_usd,
                ann_funding=self.funding_ranker.get_rate(r["symbol"]),
            ))
            # Cache direction for use by exit dispatches
            self._position_directions[r["symbol"]] = r.get("direction", "long")
        return positions

    def _dispatch_exit(
        self,
        symbol: str,
        urgency: float = 0.8,
        direction: str = "long",
        *,
        position_row: dict | None = None,
    ) -> asyncio.Event:
        """Send EXIT instruction and return an Event that fires when FILLED.

        If the ZMQ send fails (Rust engine down), the event is registered but
        will never be set â€” callers rely on ROTATION_CONFIRM_TIMEOUT_S to unblock.
        The CRITICAL log from ExecutionClient is the alert signal.
        """
        event = asyncio.Event()
        position = position_row or next(
            (row for row in self.state_reader.get_positions() if str(row.get("symbol", "")).upper() == symbol.upper()),
            None,
        )
        qty = _float_or_zero(position.get("qty")) if position is not None else 0.0
        if qty <= 0.0:
            logger.critical("Refusing to dispatch EXIT for %s without a known position quantity", symbol)
            self._set_safe_mode_flag("exit_failure", True)
            return event
        skip_spot_leg, skip_perp_leg = self._exit_leg_skip_flags(
            symbol,
            direction=direction,
            position_row=position,
        )

        self._exit_events[symbol] = event
        intent = "EXIT_SHORT" if direction == "short" else "EXIT_LONG"
        intent_id = self._next_intent_id(symbol, intent)
        created_at = datetime.now(timezone.utc).isoformat()
        self._persist_pending_intent(
            intent_id=intent_id,
            symbol=symbol,
            intent_type=intent,
            status="DISPATCHING",
            direction=direction,
            quantity=qty,
            metadata={
                "urgency": urgency,
                "quantity": qty,
                "created_at": created_at,
                "skip_spot_leg": skip_spot_leg,
                "skip_perp_leg": skip_perp_leg,
            },
        )
        payload = {
            "symbol": symbol,
            "intent": intent,
            "quantity": qty,
            "urgency": urgency,
            "max_slippage_bps": 20.0 if urgency >= 1.0 else 5.0,
            "exposure_scale": 1.0,
            "intent_id": intent_id,
        }
        if skip_spot_leg:
            payload["skip_spot_leg"] = True
        if skip_perp_leg:
            payload["skip_perp_leg"] = True
        sent = self.execution.send_order_intent(payload)
        if sent:
            logger.info(
                "EXIT dispatched for %s qty=%.5f (urgency=%.1f, direction=%s, skip_spot=%s, skip_perp=%s)",
                symbol,
                qty,
                urgency,
                direction,
                skip_spot_leg,
                skip_perp_leg,
            )
            self._pending_exit_intents[symbol] = intent_id
            self._pending_exit_created_at[symbol] = created_at
            self.state_writer.update_pending_intent(intent_id, status="PENDING_ACK")
        else:
            logger.critical("EXIT for %s NOT sent â€” ZMQ down. Position unhedged!", symbol)
            self.state_writer.update_pending_intent(
                intent_id,
                status="FAILED",
                retry_count=1,
                last_error="zmq_send_timeout",
            )
            self._set_safe_mode_flag("execution_bridge", True)
            self._exit_events.pop(symbol, None)
        return event

    def _dispatch_enter(
        self,
        symbol: str,
        notional_usd: float,
        direction: str = "long",
        ann_funding: float | None = None,
    ) -> None:
        """Send ENTER instruction. Skips if no mark price has been received yet."""
        if symbol in self._stale_pending_enters:
            logger.warning(
                "Skipping ENTER for %s because a previous entry attempt timed out and has not been reconciled",
                symbol,
            )
            return
        if symbol in self._abandoned_pending_enters:
            logger.warning(
                "Skipping ENTER for %s because a previous paper-mode entry attempt is still in the late-fill watch window",
                symbol,
            )
            return
        mark_price = self._mark_prices.get(symbol, 0.0)
        if mark_price <= 0.0:
            logger.warning(
                "No mark price for %s yet â€” skipping ENTER (will retry next cycle)", symbol
            )
            return
        per_leg_notional_usd = self._per_leg_notional_usd(notional_usd)
        raw_qty = per_leg_notional_usd / mark_price
        step = self._lot_step.get(symbol, 1e-5)
        qty = self._round_to_step(raw_qty, step)
        if qty <= 0.0:
            logger.warning(
                "Rounded quantity for %s is 0 (raw=%.8f, step=%s) â€” skipping ENTER",
                symbol,
                raw_qty,
                step,
            )
            return

        # â”€â”€ Prospective exposure guard â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # Compute the notional already committed via pending (unconfirmed) entries
        # so that rapid sequential dispatches within a cycle don't collectively
        # overshoot the gross limit before any fill event arrives.
        pending_notional = sum(
            float(m.get("qty", 0.0)) * float(m.get("entry_price", 0.0))
            for m in self._pending_enters.values()
        )
        projected_gross = self._current_gross_exposure_usd + pending_notional + notional_usd
        max_gross = self._risk_engine.limits.max_gross_exposure_usd
        if projected_gross > max_gross:
            logger.warning(
                "ENTER blocked for %s â€” projected gross $%.0f would exceed limit $%.0f "
                "(open=$%.0f, pending=$%.0f, new=$%.0f)",
                symbol,
                projected_gross,
                max_gross,
                self._current_gross_exposure_usd,
                pending_notional,
                notional_usd,
            )
            return

        # Per-symbol notional cap: prevents accumulating multiple slots in one symbol.
        per_symbol_cap = float(self._config.get("per_symbol_notional_cap_usd"))
        existing_symbol_gross = self._current_gross_by_symbol.get(symbol, 0.0)
        if existing_symbol_gross + notional_usd > per_symbol_cap:
            logger.warning(
                "ENTER blocked for %s â€” projected symbol notional $%.0f would exceed "
                "per-symbol cap $%.0f (open=$%.0f, new=$%.0f)",
                symbol,
                existing_symbol_gross + notional_usd,
                per_symbol_cap,
                existing_symbol_gross,
                notional_usd,
            )
            return

        intent = "ENTER_SHORT" if direction == "short" else "ENTER_LONG"
        intent_id = self._next_intent_id(symbol, intent)
        entry_depth_usd = self._cost_depth_or_default(self.depth_tracker.get_entry_depth(symbol))
        entry_metadata = {
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "entry_price": mark_price,
            "qty": qty,
            "direction": direction,
            "ann_funding": self.funding_ranker.get_rate(symbol) if ann_funding is None else ann_funding,
            "estimated_entry_cost_usd": blended_entry_cost(
                per_leg_notional_usd,
                depth_usd=entry_depth_usd,
            ),
            "intent_id": intent_id,
        }
        self._persist_pending_intent(
            intent_id=intent_id,
            symbol=symbol,
            intent_type=intent,
            status="DISPATCHING",
            direction=direction,
            quantity=qty,
            notional_usd=notional_usd,
            metadata=entry_metadata,
        )
        sent = self.execution.send_order_intent({
            "symbol": symbol,
            "intent": intent,
            "quantity": qty,
            "urgency": 0.8,
            "max_slippage_bps": 5.0,
            "exposure_scale": 1.0,
            "intent_id": intent_id,
        })
        if sent:
            logger.info(
                "ENTER dispatched for %s qty=%.5f (gross_notional=$%.0f, leg_notional=$%.0f, price=$%.2f, direction=%s)",
                symbol,
                qty,
                notional_usd,
                per_leg_notional_usd,
                mark_price,
                direction,
            )
            self._pending_enters[symbol] = dict(entry_metadata)
            self.state_writer.update_pending_intent(intent_id, status="PENDING_ACK")
        else:
            logger.critical("ENTER for %s NOT sent â€” ZMQ down.", symbol)
            self.state_writer.update_pending_intent(
                intent_id,
                status="FAILED",
                retry_count=1,
                last_error="zmq_send_timeout",
            )
            self._set_safe_mode_flag("execution_bridge", True)
            return

    async def _await_exit_confirmation(self, symbol: str) -> bool:
        """Wait for FILLED event. Returns True if confirmed, False on timeout."""
        event = self._exit_events.get(symbol)
        if event is None:
            return False
        try:
            await asyncio.wait_for(event.wait(), timeout=ROTATION_CONFIRM_TIMEOUT_S)
            return True
        except asyncio.TimeoutError:
            logger.warning("Exit confirmation timeout for %s â€” entry will be deferred", symbol)
            pending_intent_id = self._pending_exit_intents.get(symbol)
            if pending_intent_id:
                self.state_writer.update_pending_intent(
                    pending_intent_id,
                    status="TIMEOUT",
                    last_error="exit_confirmation_timeout",
                )
            return False

    async def _maybe_recompound(self) -> None:
        import time
        if time.time() - self._last_compound_check < 86400:
            return
        self._last_compound_check = time.time()
        equity = self.state_reader.get_account_equity()
        if equity and equity > 0:
            new_capital = equity / MAX_CONCURRENT_POSITIONS
            self.allocator = PortfolioAllocator(
                self.depth_tracker, self.funding_ranker, capital_per_slot_usd=new_capital
            )
            logger.info("Auto-compounding: equity=%.2f, new capital_per_slot=%.2f", equity, new_capital)

    async def _watch_sentiment_file(self) -> None:
        """Read current_sentiment.json every 60s and persist score to SQLite for the dashboard."""
        import math
        while not self._shutdown_event.is_set():
            try:
                if not bool(self._config.get("sentiment_enabled")):
                    self._sentiment_score = 0.0
                    self.state_writer.set_stat("sentiment_score", 0.0)
                    if await self._sleep_or_shutdown(60.0):
                        break
                    continue
                if os.path.exists(_SENTIMENT_PATH):
                    with open(_SENTIMENT_PATH, encoding="utf-8") as f:
                        data = json.load(f)
                    raw = float(data.get("sentiment_score", 0.0))
                    # Guard against NaN/Inf from malformed or LLM-hallucinated AI responses.
                    if math.isnan(raw) or math.isinf(raw):
                        logger.warning("Sentiment score is non-finite (%s) â€” resetting to neutral", raw)
                        raw = 0.0
                    # Clamp to valid range [-1.0, 1.0] regardless of AI output.
                    self._sentiment_score = max(-1.0, min(1.0, raw))
                    self.state_writer.set_stat("sentiment_score", self._sentiment_score)
            except (json.JSONDecodeError, ValueError, TypeError, KeyError) as e:
                logger.warning("Failed to parse current_sentiment.json, resetting to neutral: %s", e)
                self._sentiment_score = 0.0
            except Exception as e:
                logger.error("Unexpected error reading sentiment file: %s", e)
                self._sentiment_score = 0.0
            if await self._sleep_or_shutdown(60.0):
                break

    def _effective_entry_threshold(self) -> float:
        """Entry threshold with adaptive baseline, sentiment, and streak penalties.

        Adaptive thresholds raise the floor based on recent funding distributions.
        Sentiment then tilts the threshold modestly, and loss streaks can temporarily
        harden the gate further in paper/testnet adaptive mode.
        """
        base = float(self._config.get("entry_ann_funding_threshold"))
        adaptive_base = self._adaptive_entry_threshold_base if self._adaptive_controls_enabled() else base
        threshold = max(base, adaptive_base)
        if bool(self._config.get("sentiment_enabled")):
            scale = max(0.50, min(1.50, 1.0 - 0.20 * self._sentiment_score))
            threshold *= scale
        if self._loss_streak >= int(self._config.get("loss_streak_trigger")):
            threshold *= float(self._config.get("loss_streak_entry_multiplier"))
        return threshold

    def _effective_rotation_gap(self) -> float:
        if not self._adaptive_controls_enabled():
            return ROTATION_MIN_GAP_ANN
        return max(ROTATION_MIN_GAP_ANN, self._adaptive_rotation_gap)

    def _effective_notional_scale(self) -> float:
        adaptive_scale = 1.0
        if self._adaptive_controls_enabled():
            adaptive_scale = max(0.1, min(1.0, self._streak_notional_scale))
        return max(0.1, min(1.0, min(adaptive_scale, self._risk_position_scale)))

    def _cooldown_seconds(self, key: str) -> float:
        try:
            minutes = float(self._config.get(key))
        except (TypeError, ValueError):
            minutes = 0.0
        return max(0.0, minutes * 60.0)

    def _persist_guard_snapshot(
        self,
        regime_blocked: dict[str, RegimeDecision] | None = None,
    ) -> None:
        cooldown_snapshot = self.cooldowns.snapshot()
        payload = {
            "cooldown_global_active": cooldown_snapshot["global_active"],
            "cooldown_global_reason": cooldown_snapshot["global_reason"],
            "cooldown_global_until": cooldown_snapshot["global_until"],
            "cooldown_global_remaining_s": cooldown_snapshot["global_remaining_s"],
            "cooldown_symbols": cooldown_snapshot["symbol_cooldowns"],
        }
        if regime_blocked is not None:
            payload["regime_blocked_symbols"] = sorted(regime_blocked.keys())
            payload["regime_blocked_reasons"] = {
                symbol: decision.reasons for symbol, decision in regime_blocked.items()
            }
        self.state_writer.set_risk_snapshot(payload)

    def _tradable_trade_symbols(self) -> set[str]:
        if self._trading_mode == "paper":
            return set(self._tradable_perp_symbols or self._tradable_spot_symbols)
        if not self._spot_universe_loaded or not self._tradable_spot_symbols:
            return set()
        if not self._tradable_perp_symbols:
            return set()
        return self._tradable_perp_symbols & self._tradable_spot_symbols

    def _pinned_live_symbols(self, open_positions: list[OpenPosition] | None = None) -> list[str]:
        pinned: list[str] = []
        open_symbols = (
            [position.symbol for position in open_positions]
            if open_positions is not None
            else [str(row.get("symbol", "")).upper() for row in self.state_reader.get_positions() if row.get("symbol")]
        )
        for symbol in open_symbols:
            if symbol:
                pinned.append(symbol.upper())
        for symbol in self._pending_enters:
            pinned.append(symbol.upper())
        for symbol in self._stale_pending_enters:
            pinned.append(symbol.upper())
        for symbol in self._abandoned_pending_enters:
            pinned.append(symbol.upper())
        for symbol in self._pending_exit_intents:
            pinned.append(symbol.upper())
        for symbol in self._abandoned_exit_intents:
            pinned.append(symbol.upper())
        return list(dict.fromkeys(pinned))

    def _live_enriched_symbols(
        self,
        ranked: list[tuple[str, float]] | None = None,
        open_positions: list[OpenPosition] | None = None,
    ) -> list[str]:
        live_symbols = self._pinned_live_symbols(open_positions)
        ranked = ranked if ranked is not None else self.funding_ranker.get_ranked()
        live_cap = int(MAX_LIVE_ENRICHED_SYMBOLS)
        tradable_symbols = self._tradable_trade_symbols()
        for symbol, _ in ranked:
            symbol = symbol.upper()
            if symbol in live_symbols:
                continue
            if tradable_symbols and symbol not in tradable_symbols:
                continue
            live_symbols.append(symbol)
            if live_cap > 0 and len(live_symbols) >= live_cap:
                break
        if live_cap > 0 and len(live_symbols) >= live_cap:
            return live_symbols
        for symbol in self.monitored_symbols:
            symbol = symbol.upper()
            if symbol in live_symbols:
                continue
            if tradable_symbols and symbol not in tradable_symbols:
                continue
            live_symbols.append(symbol)
            if live_cap > 0 and len(live_symbols) >= live_cap:
                break
        return live_symbols

    def _predictor_entry_block_reason(self, symbol: str, effective_threshold: float) -> str | None:
        if not self.predictor.has_data(symbol):
            return None
        minutes_since_snap = self._minutes_since_last_snapshot()
        minutes_to_next_snap = max(0.1, FUNDING_INTERVAL_HOURS * 60 - minutes_since_snap)
        projected_rate, confidence = self.predictor.predict_with_confidence(symbol, minutes_to_next_snap)
        projected_edge = projected_rate if not INVERSE_FUNDING_ENABLED else abs(projected_rate)
        if confidence >= MIN_CONFIDENCE_FOR_ENTRY and projected_edge < effective_threshold:
            return (
                f"predictor projects {projected_rate * 100:.2f}% below "
                f"{effective_threshold * 100:.2f}% at next snapshot"
            )
        return None

    def _entry_structure_block_reason(self, symbol: str) -> str | None:
        basis_pct = self.depth_tracker.basis_pct(symbol)
        threshold = float(self._config.get("entry_premium_threshold"))
        if basis_pct is None:
            return "no live spot/perp basis yet"
        if basis_pct <= threshold:
            return f"basis {basis_pct * 10_000:.2f}bps below required {threshold * 10_000:.2f}bps"
        minutes_to_next_snapshot = max(0.0, FUNDING_INTERVAL_HOURS * 60 - self._minutes_since_last_snapshot())
        if minutes_to_next_snapshot <= 15.0:
            return f"only {minutes_to_next_snapshot:.0f} minutes to next funding snapshot"
        return None

    def _symbol_entry_gate_reasons(self, symbol: str, ann_funding: float, *, entry_threshold: float) -> list[str]:
        reasons: list[str] = []
        if ann_funding < entry_threshold:
            reasons.append(f"funding {ann_funding * 100:.2f}% below threshold {entry_threshold * 100:.2f}%")
        structure_reason = self._entry_structure_block_reason(symbol)
        if structure_reason is not None:
            reasons.append(structure_reason)
        predictor_reason = self._predictor_entry_block_reason(symbol, entry_threshold)
        if predictor_reason is not None:
            reasons.append(predictor_reason)
        if _float_or_zero(self._mark_prices.get(symbol)) <= 0.0:
            reasons.append("no mark price yet")
        return reasons

    def _candidate_cluster(self, symbol: str) -> str:
        cluster_map = self._config.get("portfolio_cluster_map")
        default_cluster = str(self._config.get("default_cluster") or "OTHER")
        if isinstance(cluster_map, dict):
            return str(cluster_map.get(symbol, default_cluster))
        return default_cluster

    def _record_candidate_cycle(
        self,
        *,
        cycle_id: str,
        ranked: list[tuple[str, float]],
        decision,
        regime_blocked: dict[str, RegimeDecision],
        cooldown_blocked: dict[str, str],
        entry_gate_blocked: dict[str, list[str]],
        external_entry_block_reason: str | None,
        candidate_notional_overrides: dict[str, float] | None = None,
    ) -> list[CandidateSnapshot]:
        decision_enter_symbols = {symbol for symbol, _ in decision.enter}
        decision_rejected = decision.rejected or {}
        max_candidates = max(8, int(self._config.get("scanner_max_candidates")))

        candidate_symbols: list[str] = []
        for symbol, _ in ranked:
            upper_symbol = symbol.upper()
            if upper_symbol in candidate_symbols:
                continue
            candidate_symbols.append(upper_symbol)

        total_candidates = len(candidate_symbols)
        accepted_count = 0

        snapshots: list[CandidateSnapshot] = []
        for rank, symbol in enumerate(candidate_symbols, start=1):
            reasons: list[str] = []
            if symbol in decision_enter_symbols and external_entry_block_reason is not None:
                reasons.append(external_entry_block_reason)
            cooldown_reason = cooldown_blocked.get(symbol)
            if cooldown_reason:
                reasons.append(f"cooldown active ({cooldown_reason})")
            regime_decision = regime_blocked.get(symbol)
            if regime_decision is not None:
                reasons.extend(regime_decision.reasons)
            reasons.extend(entry_gate_blocked.get(symbol, []))

            for reason in decision_rejected.get(symbol, []):
                if reason == "blocked":
                    if reasons:
                        continue
                    reasons.append("blocked by portfolio gate")
                elif reason == "low_entry_depth":
                    target_notional = (
                        candidate_notional_overrides[symbol]
                        if candidate_notional_overrides is not None and symbol in candidate_notional_overrides
                        else min(
                            self.allocator._capital_per_slot * TARGET_LEVERAGE * max(0.1, self._effective_notional_scale()),
                            MAX_NOTIONAL_PER_TRADE,
                        )
                    )
                    required_depth = target_notional * LIQUIDITY_FILTER_MULTIPLIER
                    entry_depth = self.depth_tracker.get_entry_depth(symbol)
                    reasons.append(f"entry depth ${entry_depth:,.0f} below required ${required_depth:,.0f}")
                elif reason == "already_open":
                    reasons.append("already open")
                elif reason == "no_free_slots":
                    reasons.append("no free slots")
                else:
                    reasons.append(reason.replace("_", " "))

            deduped_reasons = list(dict.fromkeys(reason for reason in reasons if reason))
            accepted = not deduped_reasons
            if accepted:
                accepted_count += 1
            if rank > max_candidates:
                continue

            ann_funding = self.funding_ranker.get_rate(symbol) or 0.0
            spot_live = self.depth_tracker.spot_mid_price(symbol)
            perp_live = self.depth_tracker.perp_mid_price(symbol)
            spread_bps = 0.0
            if spot_live > 0.0 and perp_live > 0.0:
                spread_bps = abs(perp_live - spot_live) / max((perp_live + spot_live) / 2.0, 1e-9) * 10_000.0
            historical_var_pct = self._historical_var_fraction(symbol)

            snapshots.append(
                CandidateSnapshot(
                    cycle_id=cycle_id,
                    symbol=symbol,
                    direction="LONG_SPOT_SHORT_PERP",
                    accepted=accepted,
                    status="accepted" if accepted else "rejected",
                    cluster=self._candidate_cluster(symbol),
                    rejection_reasons=deduped_reasons,
                    metrics={
                        "annualized_funding": ann_funding,
                        "basis_pct": _float_or_zero(self.depth_tracker.basis_pct(symbol)),
                        "depth_usd": self.depth_tracker.get_entry_depth(symbol),
                        "mark_price": _float_or_zero(
                            self._mark_prices.get(symbol) or self.depth_tracker.perp_mid_price(symbol)
                        ),
                        "spread_bps": spread_bps,
                        "historical_var_pct": historical_var_pct,
                        "var_target_notional_usd": (
                            candidate_notional_overrides.get(symbol)
                            if candidate_notional_overrides is not None
                            else None
                        ),
                        "toxicity_bps": None,
                        "toxicity_available": False,
                        "selected": symbol in decision_enter_symbols,
                    },
                    snapshot_time=datetime.now(timezone.utc).isoformat(),
                    rank=rank,
                )
            )

        self.state_writer.record_candidate_snapshots(snapshots)
        self.state_writer.set_stat("accepted_candidates", float(accepted_count))
        self.state_writer.set_stat(
            "rejected_candidates",
            float(max(0, total_candidates - accepted_count)),
        )
        self.state_writer.set_stat("scanner_breadth", float(total_candidates))
        return snapshots

    @staticmethod
    def _summarize_rejection_reasons(rejected: dict[str, list[str]]) -> dict[str, int]:
        summary: dict[str, int] = {}
        for reasons in rejected.values():
            for reason in reasons:
                summary[reason] = summary.get(reason, 0) + 1
        return dict(sorted(summary.items(), key=lambda item: (-item[1], item[0])))

    @staticmethod
    def _format_reason_counts(summary: dict[str, int], limit: int = 5) -> str:
        items = list(summary.items())[:limit]
        return ", ".join(f"{reason}={count}" for reason, count in items) if items else "none"

    def _record_entry_funnel_state(
        self,
        decision,
        *,
        external_entry_block_reason: str | None = None,
        entry_gate_blocked: dict[str, list[str]] | None = None,
        now_monotonic: float | None = None,
    ) -> None:
        summary = self._summarize_rejection_reasons(decision.rejected)
        for reasons in (entry_gate_blocked or {}).values():
            for reason in reasons:
                summary[reason] = summary.get(reason, 0) + 1
        if external_entry_block_reason is not None and decision.enter:
            summary["external_gate"] = summary.get("external_gate", 0) + len(decision.enter)
        self.state_writer.set_risk_snapshot(
            {
                "entry_candidate_count": len(decision.enter),
                "entry_filter_summary": summary,
            }
        )
        should_log = (
            not decision.enter
            and bool(summary)
            and (now_monotonic is None or now_monotonic - self._last_entry_funnel_log_monotonic >= 60.0)
        )
        if should_log:
            self._last_entry_funnel_log_monotonic = float(now_monotonic or time.monotonic())
            logger.info(
                "ENTRY FUNNEL: 0 allocator entries | %s",
                self._format_reason_counts(summary),
            )

    def _activate_breaker_cooldown(self, state: str, symbols: list[str]) -> None:
        reason = f"breaker {state.lower()}"
        if state == "HALTED":
            self.cooldowns.activate_global(
                self._cooldown_seconds("cooldown_halted_minutes"),
                reason,
            )
        elif state == "PARTIAL_EXIT":
            self.cooldowns.activate_global(
                self._cooldown_seconds("cooldown_partial_exit_minutes"),
                reason,
            )
        elif state == "EMERGENCY":
            self.cooldowns.activate_global(
                self._cooldown_seconds("cooldown_emergency_minutes"),
                reason,
            )
        else:
            return

        symbol_duration_s = self._cooldown_seconds("cooldown_symbol_minutes")
        for symbol in symbols:
            self.cooldowns.activate_symbol(symbol, symbol_duration_s, reason)

        self._persist_guard_snapshot()

    def _predictor_allows_entry(self, symbol: str, effective_threshold: float) -> bool:
        """Return False if the FundingPredictor projects the rate will decay below
        the entry threshold by the next funding snapshot with sufficient confidence.

        Prevents entering a position whose funding rate is about to collapse.
        Returns True when there is insufficient predictor data (allow entry).
        """
        block_reason = self._predictor_entry_block_reason(symbol, effective_threshold)
        if block_reason is not None:
            logger.info("Predictor gate: skipping %s - %s", symbol, block_reason)
            return False
        return True

    def _entry_structure_allows_symbol(self, symbol: str) -> bool:
        block_reason = self._entry_structure_block_reason(symbol)
        if block_reason is not None:
            if block_reason.startswith("basis "):
                logger.debug("Skipping %s - %s", symbol, block_reason)
            else:
                logger.info("Skipping %s - %s", symbol, block_reason)
            return False
        return True

    async def _trading_loop(self) -> None:
        _last_heartbeat = 0.0
        _last_rest_sync = 0.0
        while not self._shutdown_event.is_set():
            try:
                if self._runtime_mode == "BLOCKED":
                    if await self._sleep_or_shutdown(1.0):
                        break
                    continue
                if not self.subscriber.is_connected:
                    logger.info("Waiting for Rust subscriber connection before dispatching entries")
                    if await self._sleep_or_shutdown(1.0):
                        break
                    continue

                # Sync REST depth to tracker every ~5 seconds
                import time as _sync_time
                now_sync = _sync_time.monotonic()
                if now_sync - _last_rest_sync >= 5:
                    _last_rest_sync = now_sync
                    await self._sync_rest_depth_to_tracker()
                self._config.reload_now()
                self._consume_supervisor_startup_recovery_acknowledgements()

                position_rows = self._refresh_open_position_metrics()
                if self._maybe_process_operator_flatten_all_request(position_rows):
                    if await self._sleep_or_shutdown(1.0):
                        break
                    continue
                if self._dispatch_startup_recovery_exits(position_rows):
                    if await self._sleep_or_shutdown(1.0):
                        break
                    continue
                open_positions = self._get_open_positions(position_rows)
                manual_review_count = sum(
                    1
                    for row in position_rows
                    if str(row.get("recovery_state") or "").strip().lower() == "manual_review"
                )
                funding_rates = {p.symbol: p.ann_funding for p in open_positions}
                self._expire_stale_pending_intents()
                risk_decision = self._evaluate_risk_controls(position_rows)
                if risk_decision.kill_switch or risk_decision.derisk_required:
                    self._maybe_log_risk_engine_state(risk_decision)
                    for pos in open_positions:
                        if pos.symbol in self._exit_events:
                            continue
                        if pos.symbol in self._startup_recovery_stuck_symbols:
                            continue
                        if not self._startup_recovery_attempt_allowed(pos.symbol):
                            continue
                        self._record_startup_recovery_exit_attempt(pos.symbol)
                        self._dispatch_exit(
                            pos.symbol,
                            urgency=1.0 if risk_decision.kill_switch else 0.9,
                            direction=self._position_directions.get(pos.symbol, "long"),
                        )
                    if await self._sleep_or_shutdown(1.0):
                        break
                    continue
                elif not risk_decision.allow_new_risk:
                    # New entries blocked (e.g. venue latency too high) but existing
                    # positions are left open â€” don't force exits at degraded execution.
                    self._maybe_log_risk_engine_state(risk_decision)
                    if await self._sleep_or_shutdown(1.0):
                        break
                    continue

                # â”€â”€ 0. Post-snapshot funding decay exit â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
                # Within 5 minutes after a funding snapshot, funding rates that
                # have decayed below the exit threshold are acted on immediately
                # rather than waiting for the next allocator cycle.
                self._last_risk_log_signature = None
                self._last_risk_log_monotonic = 0.0
                minutes_since_snap = self._minutes_since_last_snapshot()
                if minutes_since_snap <= 5 and open_positions:
                    for pos in open_positions:
                        if (
                            self._funding_has_decayed(
                                self._position_directions.get(pos.symbol, "long"),
                                pos.ann_funding,
                            )
                            and pos.symbol not in self._exit_events
                        ):
                            logger.info(
                                "Post-snapshot decay: %s funding=%.1f%% crossed exit threshold â€” exiting",
                                pos.symbol, pos.ann_funding * 100,
                            )
                            self._dispatch_exit(
                                pos.symbol,
                                urgency=1.0,
                                direction=self._position_directions.get(pos.symbol, "long"),
                            )

                # â”€â”€ 1. Circuit breaker â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
                liquidity_map = {
                    p.symbol: self.depth_tracker.get_exit_depth(p.symbol)
                    for p in open_positions
                }
                breaker_decision = self.breaker.evaluate(
                    funding_rates,
                    liquidity_map=liquidity_map,
                    directions=self._position_directions,
                )
                if breaker_decision.state != self._last_breaker_state:
                    self._activate_breaker_cooldown(
                        breaker_decision.state,
                        breaker_decision.positions_to_exit,
                    )
                    self._last_breaker_state = breaker_decision.state

                if breaker_decision.state == "WARNED":
                    logger.warning("CIRCUIT BREAKER: WARNED â€” %s", breaker_decision.reason)
                    # Entries still allowed; fall through to allocation logic

                elif breaker_decision.state == "PARTIAL_EXIT":
                    logger.warning("CIRCUIT BREAKER: PARTIAL_EXIT â€” %s", breaker_decision.reason)
                    for symbol in breaker_decision.positions_to_exit:
                        if symbol not in self._exit_events:
                            self._dispatch_exit(
                                symbol,
                                urgency=0.9,
                                direction=self._position_directions.get(symbol, "long"),
                            )
                    if await self._sleep_or_shutdown(1.0):
                        break
                    continue

                elif breaker_decision.state == "EMERGENCY":
                    logger.warning("CIRCUIT BREAKER: EMERGENCY â€” exiting all positions")
                    for symbol in breaker_decision.positions_to_exit:
                        if symbol not in self._exit_events:
                            self._dispatch_exit(
                                symbol,
                                urgency=1.0,
                                direction=self._position_directions.get(symbol, "long"),
                            )
                    if await self._sleep_or_shutdown(1.0):
                        break
                    continue

                if not breaker_decision.allow_new_entries:
                    import time as _halt_time
                    now_halt = _halt_time.monotonic()
                    if self._halted_since == 0.0:
                        self._halted_since = now_halt
                        logger.info("CIRCUIT BREAKER: HALTED â€” blocking new entries")
                    elif now_halt - self._halted_since >= _HALTED_ESCALATION_SECS:
                        logger.warning(
                            "CIRCUIT BREAKER: HALTED for %.0f min â€” escalating to partial exits",
                            (now_halt - self._halted_since) / 60,
                        )
                        self._halted_since = 0.0  # Reset so next HALTED gets a fresh clock
                        for pos in open_positions:
                            if (
                                self._funding_has_decayed(
                                    self._position_directions.get(pos.symbol, "long"),
                                    pos.ann_funding,
                                )
                                and pos.symbol not in self._exit_events
                            ):
                                self._dispatch_exit(
                                    pos.symbol,
                                    urgency=0.9,
                                    direction=self._position_directions.get(pos.symbol, "long"),
                                )
                    if await self._sleep_or_shutdown(1.0):
                        break
                    continue

                # Clear HALTED timer when breaker returns to non-blocking state
                self._halted_since = 0.0

                # â”€â”€ 2. Allocation decision â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
                await self._maybe_recompound()
                import time as _time
                bybit_rates = self.bybit_monitor.get_rates() if self._cross_validation_enabled() else None
                now = _time.monotonic()
                if bybit_rates and now - self._last_xval_check >= 60:
                    self._last_xval_check = now
                    for sym, bybit_rate in bybit_rates.items():
                        if not self.funding_ranker.has_symbol(sym):
                            continue
                        ranker_rate = self.funding_ranker.get_rate(sym)
                        self._maybe_log_cross_validation_gap(
                            sym,
                            ranker_rate,
                            bybit_rate,
                            now=now,
                        )
                ranked = self.funding_ranker.get_ranked()
                ranked_symbols = [sym for sym, _ in ranked]
                self._capture_basis_observations(set(ranked_symbols) | {position.symbol for position in open_positions})
                entry_threshold = self._effective_entry_threshold()
                entry_gate_blocked = {
                    symbol: self._symbol_entry_gate_reasons(symbol, ann_funding, entry_threshold=entry_threshold)
                    for symbol, ann_funding in ranked
                }
                correlation_blocked = self._correlation_gate_blocked(ranked, open_positions)
                for symbol, reasons in correlation_blocked.items():
                    entry_gate_blocked.setdefault(symbol, []).extend(reasons)
                entry_gate_blocked = {
                    symbol: list(dict.fromkeys(reasons))
                    for symbol, reasons in entry_gate_blocked.items()
                    if reasons
                }
                regime_blocked = self.regime_filter.blocked_symbols(ranked_symbols)
                cooldown_snapshot = self.cooldowns.snapshot()

                if cooldown_snapshot["global_active"]:
                    if now - _last_heartbeat >= 60:
                        _last_heartbeat = now
                        top_rate = ranked[0][1] if ranked else 0.0
                        logger.info(
                            "HEARTBEAT: %d managed positions | %d manual-review positions | "
                            "top funding=%.2f%% | threshold=%.1f%% | global cooldown active (%s, %.0fs left)",
                            len(open_positions),
                            manual_review_count,
                            top_rate * 100,
                            entry_threshold * 100,
                            cooldown_snapshot["global_reason"],
                            cooldown_snapshot["global_remaining_s"],
                        )
                        self.state_writer.set_stat("open_positions", float(len(position_rows)))
                        self.state_writer.set_stat("managed_open_positions", float(len(open_positions)))
                        self.state_writer.set_stat("manual_review_positions", float(manual_review_count))
                        self.state_writer.set_stat("top_funding_rate", top_rate * 100)
                        self.state_writer.set_stat("top_funding_symbol", ranked[0][0] if ranked else "")
                        self._persist_guard_snapshot(regime_blocked)
                    if await self._sleep_or_shutdown(1.0):
                        break
                    continue

                cooldown_blocked = self.cooldowns.blocked_symbols(ranked_symbols)
                blocked_symbols = set(regime_blocked) | set(cooldown_blocked) | set(entry_gate_blocked)
                base_target_notional = min(
                    self.allocator._capital_per_slot * TARGET_LEVERAGE * max(0.1, self._effective_notional_scale()),
                    MAX_NOTIONAL_PER_TRADE,
                )
                candidate_notional_overrides = {
                    symbol.upper(): round(self._var_sized_notional(symbol, base_target_notional), 2)
                    for symbol, _ann_funding in ranked
                }

                decision = self.allocator.decide(
                    open_positions,
                    blocked_symbols=blocked_symbols,
                    notional_scale=self._effective_notional_scale(),
                    rotation_min_gap_ann=self._effective_rotation_gap(),
                    notional_overrides=candidate_notional_overrides,
                )
                external_entry_block_reason = self._external_entry_block_reason()
                cycle_id = datetime.now(timezone.utc).isoformat()
                self._record_candidate_cycle(
                    cycle_id=cycle_id,
                    ranked=ranked,
                    decision=decision,
                    regime_blocked=regime_blocked,
                    cooldown_blocked=cooldown_blocked,
                    entry_gate_blocked=entry_gate_blocked,
                    external_entry_block_reason=external_entry_block_reason,
                    candidate_notional_overrides=candidate_notional_overrides,
                )
                self._record_entry_funnel_state(
                    decision,
                    external_entry_block_reason=external_entry_block_reason,
                    entry_gate_blocked=entry_gate_blocked,
                    now_monotonic=now,
                )

                # â”€â”€ 3. Dispatch exits â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
                for symbol, reason in decision.exit:
                    if symbol not in self._exit_events:
                        logger.info("Rotation: exiting %s (%s)", symbol, reason)
                        self._dispatch_exit(
                            symbol,
                            urgency=0.8,
                            direction=self._position_directions.get(symbol, "long"),
                        )

                # â”€â”€ 4. Await exit confirmations, dispatch rotation entries â”€â”€â”€â”€
                # All rotation exits are awaited concurrently so a single slow fill
                # doesn't hold up others or block the circuit breaker for NÃ—timeout.
                blocked_entry_symbols = self._blocked_entry_symbols()
                if decision.rotation_targets:
                    confirm_tasks = {
                        exited_symbol: asyncio.ensure_future(
                            self._await_exit_confirmation(exited_symbol)
                        )
                        for exited_symbol in decision.rotation_targets
                    }
                    results = await asyncio.gather(*confirm_tasks.values(), return_exceptions=True)
                    for (exited_symbol, rotation_target), confirmed in zip(
                        decision.rotation_targets.items(), results
                    ):
                        if confirmed is True:
                            if external_entry_block_reason is not None:
                                logger.info(
                                    "Skipping rotation entry for %s â€” external risk gate active (%s)",
                                    rotation_target,
                                    external_entry_block_reason,
                                )
                                continue
                            if rotation_target in blocked_entry_symbols:
                                logger.info(
                                    "Skipping rotation entry for %s Ã¢â‚¬â€ per-symbol block (%s)",
                                    rotation_target,
                                    self._describe_symbol_block(rotation_target),
                                )
                                continue
                            allowed, cooldown_reason = self.cooldowns.allow_symbol(rotation_target)
                            if not allowed:
                                logger.info(
                                    "Skipping rotation entry for %s â€” cooldown active (%s)",
                                    rotation_target, cooldown_reason,
                                )
                                continue
                            regime_decision = self.regime_filter.evaluate(rotation_target)
                            if not regime_decision.allow_entry:
                                logger.info(
                                    "Skipping rotation entry for %s â€” regime filter blocked (%s)",
                                    rotation_target, ", ".join(regime_decision.reasons),
                                )
                                continue
                            rot_funding = self.funding_ranker.get_rate(rotation_target) or 0.0
                            rot_threshold = self._effective_entry_threshold()
                            if rot_funding < rot_threshold:
                                logger.info(
                                    "Skipping rotation entry for %s â€” funding %.2f%% below threshold %.1f%%",
                                    rotation_target, rot_funding * 100, rot_threshold * 100,
                                )
                                continue
                            if not self._entry_structure_allows_symbol(rotation_target):
                                continue
                            if not self._predictor_allows_entry(rotation_target, rot_threshold):
                                continue
                            rotation_notional = decision.rotation_notionals.get(
                                exited_symbol,
                                CAPITAL_PER_SLOT_USD * TARGET_LEVERAGE,
                            )
                            self._dispatch_enter(
                                rotation_target,
                                rotation_notional,
                                direction="long",
                                ann_funding=rot_funding,
                            )
                        else:
                            logger.warning(
                                "Skipping rotation entry for %s â€” exit of %s unconfirmed",
                                rotation_target, exited_symbol,
                            )

                # â”€â”€ 5. Dispatch entries for empty slots â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
                entry_threshold = self._effective_entry_threshold()
                for symbol, notional in decision.enter:
                    if symbol in self._exit_events:
                        continue
                    if symbol in self._pending_enters:
                        logger.debug("Skipping %s â€” entry already pending confirmation", symbol)
                        continue
                    if external_entry_block_reason is not None:
                        logger.info(
                            "Skipping %s â€” external risk gate active (%s)",
                            symbol,
                            external_entry_block_reason,
                        )
                        continue
                    if symbol in blocked_entry_symbols:
                        logger.info(
                            "Skipping %s Ã¢â‚¬â€ per-symbol block (%s)",
                            symbol,
                            self._describe_symbol_block(symbol),
                        )
                        continue
                    allowed, cooldown_reason = self.cooldowns.allow_symbol(symbol)
                    if not allowed:
                        logger.info(
                            "Skipping %s â€” cooldown active (%s)",
                            symbol, cooldown_reason,
                        )
                        continue
                    regime_decision = self.regime_filter.evaluate(symbol)
                    if not regime_decision.allow_entry:
                        logger.info(
                            "Skipping %s â€” regime filter blocked (%s)",
                            symbol, ", ".join(regime_decision.reasons),
                        )
                        continue

                    ann_funding = self.funding_ranker.get_rate(symbol) or 0.0
                    # Long-only release candidate: only collect positive funding.
                    if ann_funding < entry_threshold:
                        logger.debug(
                            "Skipping %s â€” funding %.2f%% below threshold %.1f%%",
                            symbol, ann_funding * 100, entry_threshold * 100,
                        )
                        continue
                    if not self._entry_structure_allows_symbol(symbol):
                        continue
                    # Predictor gate: skip if projected rate decays below threshold at snapshot
                    if not self._predictor_allows_entry(symbol, entry_threshold):
                        continue
                    self._dispatch_enter(symbol, notional, direction="long", ann_funding=ann_funding)

                # â”€â”€ 6. Heartbeat â€” periodic status for logs + dashboard â”€â”€â”€â”€
                if now - _last_heartbeat >= 60:
                    _last_heartbeat = now
                    top_rate = ranked[0][1] if ranked else 0.0
                    live_enriched_symbols = self._live_enriched_symbols(ranked, open_positions)
                    logger.info(
                        "HEARTBEAT: %d managed positions | %d manual-review positions | "
                        "top funding=%.2f%% | threshold=%.1f%% | %d pending enters | %d pending exits | %d guarded symbols",
                        len(open_positions),
                        manual_review_count,
                        top_rate * 100,
                        entry_threshold * 100,
                        len(self._pending_enters),
                        len(self._exit_events),
                        len(blocked_symbols),
                    )
                    self.state_writer.set_stat("open_positions", float(len(position_rows)))
                    self.state_writer.set_stat("managed_open_positions", float(len(open_positions)))
                    self.state_writer.set_stat("manual_review_positions", float(manual_review_count))
                    self.state_writer.set_stat("top_funding_rate", top_rate * 100)
                    self.state_writer.set_stat("top_funding_symbol", ranked[0][0] if ranked else "")
                    self.state_writer.set_stat("live_enrichment_breadth", float(len(live_enriched_symbols)))
                    self._persist_guard_snapshot(regime_blocked)

            except Exception as exc:
                logger.error("Error in trading loop: %s", exc, exc_info=True)

            if await self._sleep_or_shutdown(1.0):
                break

    async def _fetch_mark_prices_via_rest(self) -> None:
        """Fetch current mark prices for all Binance futures symbols via REST.

        This populates _mark_prices cache before the trading loop starts,
        preventing "No mark price yet" warnings during startup.
        """
        try:
            # Fetch all prices from Binance futures ticker
            resp = await asyncio.to_thread(
                requests.get,
                f"{self._futures_base_url}/fapi/v1/ticker/price",
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            count = 0
            for item in data:
                sym = item.get("symbol", "")
                if not sym:
                    continue
                try:
                    price = float(item.get("price", 0.0))
                    if price > 0.0:
                        self._mark_prices[sym] = price
                        self._mark_price_ready.add(sym)
                        self._mark_price_updated_monotonic[sym] = time.monotonic()
                        self.regime_filter.on_mark_price(
                            sym,
                            price,
                            self.funding_ranker.get_rate(sym) if self.funding_ranker.has_symbol(sym) else None,
                        )
                        count += 1
                except (ValueError, TypeError):
                    pass

            logger.info("REST mark prices fetched for %d symbols", count)
        except Exception as exc:
            logger.warning("Could not fetch REST mark prices: %s", exc)

    async def _run_mark_price_refresh_loop(self, interval_s: float = 60.0) -> None:
        while not self._shutdown_event.is_set():
            await self._fetch_mark_prices_via_rest()
            if await self._sleep_or_shutdown(interval_s):
                break

    async def run(self) -> None:
        logger.info("Starting LiveTraderV2 - seeded with %d symbols", len(self.monitored_symbols))
        self._loop = asyncio.get_running_loop()
        self._install_signal_handlers()
        self._reset_runtime_dashboard_stats()
        self._persist_runtime_state()
        self._background_tasks = [
            asyncio.create_task(self._run_liveness_loop(), name="liveness_loop"),
        ]
        try:
            await self._run_preflight()
            await self._on_startup()
            self._refresh_adaptive_state()

            await self._fetch_lot_step_sizes()
            startup_refresh_tasks = [
                self.funding_ranker.refresh(),
                self.rest_depth_fetcher.refresh_all(),
                self._fetch_mark_prices_via_rest(),
            ]
            if self._cross_validation_enabled():
                startup_refresh_tasks.insert(1, self.bybit_monitor.refresh())  # type: ignore[arg-type]
            else:
                logger.info("Bybit cross-validation disabled in %s mode", self._trading_mode)
            await asyncio.gather(*startup_refresh_tasks)
            self.rest_depth_fetcher.update_symbols(self._live_enriched_symbols())
            await self.rest_depth_fetcher.refresh_all()
            await self._sync_rest_depth_to_tracker()
            live_enriched_symbols = self._live_enriched_symbols()
            ready_count = sum(1 for symbol in live_enriched_symbols if symbol in self._mark_price_ready)
            logger.info(
                "Startup primed: %d/%d live-enriched symbols with mark prices ready",
                ready_count, len(live_enriched_symbols),
            )
            self._startup_complete_at = datetime.now(timezone.utc).isoformat()
            self._background_tasks.extend([
                asyncio.create_task(self.subscriber.run(), name="rust_subscriber"),
                asyncio.create_task(
                    self.funding_ranker.run_forever(interval_s=60),
                    name="funding_ranker",
                ),
                asyncio.create_task(
                    self.rest_depth_fetcher.run_forever(interval_s=30),
                    name="rest_depth_fetcher",
                ),
                asyncio.create_task(
                    self._run_mark_price_refresh_loop(interval_s=60),
                    name="mark_price_refresh",
                ),
                asyncio.create_task(self._watch_sentiment_file(), name="sentiment_watch"),
                asyncio.create_task(self._run_heartbeat_loop(), name="heartbeat_loop"),
                asyncio.create_task(self._run_maintenance_loop(), name="maintenance_loop"),
                asyncio.create_task(self._trading_loop(), name="trading_loop"),
            ])
            if self._cross_validation_enabled():
                self._background_tasks.append(
                    asyncio.create_task(self.bybit_monitor.run_forever(), name="bybit_monitor")
                )
            await asyncio.gather(*self._background_tasks)
        except asyncio.CancelledError:
            if not self._shutdown_started:
                raise
        except StartupBlockedError:
            await self.shutdown(reason="startup_blocked")
            raise
        finally:
            await self.shutdown(reason="run_exit")


async def main() -> None:
    trader = LiveTraderV2()
    try:
        await trader.run()
    except StartupBlockedError as exc:
        logger.error("Trader startup blocked: %s", exc)
        raise SystemExit(_BLOCKED_EXIT_CODE) from exc


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("LiveTraderV2 stopped.")
    finally:
        logging.shutdown()
