"""Multi-symbol live trader orchestrator.

Wires together:
  - RustDataSubscriber (depth + fill confirmations from Rust port 9000)
  - FundingRanker (single REST call every 60s)
  - CorrelationBreaker (portfolio-level circuit breaker)
  - PortfolioAllocator (sizing, liquidity filter, rotation)
  - ExecutionClient (ZMQ PUSH to Rust)
  - StateWriter/StateReader (SQLite shared state)

Execution invariant: exits are dispatched first; ENTER for a rotation target
only fires after FILLED confirmation from Rust.  A timeout defers the entry.

This is the sole supervised trading runtime; compatibility entrypoints delegate
to this module.
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
from decimal import Decimal, InvalidOperation
from statistics import fmean, pstdev
from typing import Any

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
    MAX_DRAWDOWN_RELEASE_PCT,
    MAX_LIVE_ENRICHED_SYMBOLS,
    MAX_NOTIONAL_PER_TRADE,
    MAX_SYMBOL_CONCENTRATION,
    FUNDING_INTERVAL_HOURS,
    FUNDING_PERIODS_PER_YEAR,
    MAX_FUNDING_STALENESS_MINUTES,
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
    RUNTIME_SETTLING_SECONDS,
    LIVE_CONFIG_PATH,
    VALIDATION_SNAPSHOT_INTERVAL_MINUTES,
    WIN_STREAK_RESET,
    STALE_INTENT_COOLDOWN_BASE_SECONDS,
    VENUE_LATENCY_SMOOTHING_FACTOR,
    VENUE_LATENCY_DEBOUNCE_S,
    ALLOW_AUTONOMOUS_INVERSE_LIQUIDATION,
    get_monitored_symbols,
)
from bongus.core.binance_endpoints import get_rest_base_urls, resolve_binance_credentials
from bongus.core.config_manager import ConfigManager
from bongus.engine.cooldown_manager import CooldownManager
from bongus.engine.account_reconciliation import (
    AccountReconciliationReport,
    bot_owned_orders,
    is_bot_client_order_id,
    order_client_id,
    reconcile_account_snapshot,
    unrelated_orders,
)
from bongus.engine.cost_calibration import (
    CostMarkoutCalibrator,
    adverse_markout_bps,
    observation_from_execution_quality,
    observations_from_execution_quality,
)
from bongus.engine.cost_model import (
    blended_entry_cost,
    blended_exit_cost,
    quality_score_from_slippage,
)
from bongus.engine.exchange_statements import (
    BINANCE_FUTURES_INCOME,
    BINANCE_MARGIN_INTEREST,
    MATCH_REQUIRED,
    UNMAPPED,
)
from bongus.engine.risk_engine import RiskDecision, RiskEngine, RiskLimits, RiskState
from bongus.engine.safe_mode import describe_safe_mode_flags, restore_safe_mode_flags
from bongus.engine.state_store import (
    CandidateSnapshot,
    ExecutionQualitySample,
    OpportunityScore,
    ShadowDecision,
    StateWriter,
    StateReader,
    Trade,
)
from bongus.engine.route_optimizer import RouteInputs, RouteOptimizer, RoutePolicy
from bongus.ipc.execution import ExecutionClient
from bongus.market_data.bybit_monitor import BybitFundingMonitor
from bongus.market_data.depth_tracker import DepthTracker
from bongus.market_data.funding_predictor import FundingPredictor, MIN_CONFIDENCE_FOR_ENTRY
from bongus.market_data.funding_ranker import FundingRanker
from bongus.market_data.feed_recovery import FeedCursorStore, FeedSource, FeedState
from bongus.market_data.settlement_model import (
    FundingObservation,
    SettlementForecast,
    SettlementFundingModel,
)
from bongus.market_data.rust_data_subscriber import RustDataSubscriber
from bongus.market_data.rest_depth_fetcher import RestDepthFetcher
from bongus.monitoring.performance_metrics import calculate_metrics
from bongus.portfolio.correlation_breaker import CorrelationBreaker
from bongus.portfolio.capital_reservations import (
    CapitalReservationBook,
    CapitalState,
    ReservationError,
    ReservationPolicy,
    ReservationPurpose,
    ReservationRequest,
)
from bongus.portfolio.portfolio_allocator import OpenPosition, PortfolioAllocator
from bongus.portfolio.portfolio_optimizer import (
    PortfolioCandidate,
    PortfolioConstraints,
    PortfolioPosition,
    ShadowPortfolioOptimizer,
)
from bongus.portfolio.regime_filter import RegimeDecision, RegimeFilter
from bongus.portfolio.rotation_policy import (
    IncrementalRotationPolicy,
    RotationAction,
    RotationInputs,
)
from bongus.strategies.hold_exit_policy import (
    DirectionAwareHoldExitPolicy,
    HoldExitInputs,
)
from bongus.strategies.opportunity_scorer import (
    CandidateEconomics,
    LowerConfidenceNetEVScorer,
    NetEVScore,
)
from bongus.strategies.opportunity_adapters import (
    LIVE_OPPORTUNITY_ADAPTER,
    PAPER_OPPORTUNITY_ADAPTER,
)
from bongus.strategies.opportunity_kernel import (
    OPPORTUNITY_KERNEL_VERSION,
    OpportunityEvaluationInput,
    SettlementExpectation,
)
from bongus.strategies.plugins import (
    BasisConvergencePlugin,
    FundingCalendarOptimizationPlugin,
    StrategyContext,
    StrategyPluginRegistry,
    StrategyRiskBudget,
)

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_DOTENV_PATH = os.path.join(_PROJECT_ROOT, ".env")
_SENTIMENT_PATH = os.path.join(_PROJECT_ROOT, "current_sentiment.json")
_WATCHDOG_HEARTBEAT_PATH = os.path.join(_PROJECT_ROOT, "runtime_heartbeat.json")

load_dotenv(_DOTENV_PATH)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("live_trader_v2")

# If the circuit breaker stays HALTED for this long, escalate to partial exits
# rather than holding troubled positions indefinitely with no recovery path.
_HALTED_ESCALATION_SECS: int = 1800  # 30 minutes
_STALE_EXIT_MAX_RESUBMIT_ATTEMPTS: int = 3
_STALE_ENTER_MAX_CANCEL_ATTEMPTS: int = 3
_SIGNED_RECV_WINDOW_MS: int = 60_000
_POSITION_QTY_TOLERANCE: float = 1e-9
_DEFAULT_COST_DEPTH_USD: float = 500_000.0
_EXECUTION_MARKOUT_HORIZON_SECONDS: float = 60.0
_EXECUTION_MARKOUT_MAX_WAIT_SECONDS: float = 300.0
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
_VALIDATION_ADJUST_STATUSES: frozenset[str] = frozenset({"MONITORING", "INSUFFICIENT_DATA", "ADJUST"})
_VALIDATION_HARD_BLOCK_STATUSES: frozenset[str] = frozenset({"FAILING", "NO_GO", "REJECTED"})
_RECOVERABLE_BINANCE_SIGNED_ERROR_CODES: frozenset[int] = frozenset(
    {-1021, -1022, -2014, -2015}
)
_PER_SYMBOL_SAFE_MODE_FLAGS: frozenset[str] = frozenset(
    {
        "naked_leg_unwind_stuck",
        "startup_manual_review",
        "startup_exit_candidate",
        "hedge_gap",
        "stale_pending_intent",
        "exit_failure",
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


def _testnet_margin_endpoint_is_unsupported(exc: BinanceSignedCallError) -> bool:
    """Recognize demo Spot's deliberate lack of cross-margin SAPI endpoints.

    Binance demo Spot returns HTTP 404 for these endpoints rather than the
    mainnet ``-3003`` response used to prove cross-margin is disabled.  Treat
    that narrowly as the same no-liability state in testnet only; live-mode
    endpoint failures must remain reconciliation blockers.
    """

    return (
        str(os.getenv("TRADING_MODE", "paper")).strip().lower() == "testnet"
        and exc.http_status == 404
        and exc.endpoint
        in {
            "/sapi/v1/margin/account",
            "/sapi/v1/margin/openOrders",
            "/sapi/v1/margin/interestHistory",
        }
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


def _extract_quote_asset(symbol: str) -> str:
    upper_symbol = symbol.upper()
    for suffix in _QUOTE_ASSET_SUFFIXES:
        if upper_symbol.endswith(suffix) and len(upper_symbol) > len(suffix):
            return suffix
    return ""


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
    def __init__(self, db_path: str | None = None, config_path: str | None = None) -> None:
        self._trading_mode = os.getenv("TRADING_MODE", "paper").lower()
        logger.info("TRADING_MODE = %s", self._trading_mode)
        logger.info(
            "Runtime config: ACCOUNT_EQUITY_USD=%s MAX_GROSS_EXPOSURE_USD=%s MONITORED_SYMBOLS=%s",
            os.getenv("ACCOUNT_EQUITY_USD", "10000"),
            os.getenv("MAX_GROSS_EXPOSURE_USD", "10000"),
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
        self.state_writer = StateWriter(db_path=db_path) if db_path else StateWriter()
        self.state_reader = StateReader(db_path=db_path) if db_path else StateReader()
        state_db_path = str(
            self.state_writer.conn.execute("PRAGMA database_list").fetchone()[2]
        )
        self.capital_reservations = CapitalReservationBook(
            state_db_path,
            connection=self.state_writer.conn,
        )
        self._config = ConfigManager(
            config_path=config_path or LIVE_CONFIG_PATH,
            on_validation_error=self._on_config_validation_error,
            on_reload=self._on_config_reloaded,
        )
        self.allocator = PortfolioAllocator(
            self.depth_tracker,
            self.funding_ranker,
            capital_per_slot_usd=CAPITAL_PER_SLOT_USD,
            per_symbol_cap_usd=float(self._config.get("per_symbol_notional_cap_usd")),
        )
        self.predictor = FundingPredictor()
        self.bybit_monitor = BybitFundingMonitor(None if DYNAMIC_SYMBOL_MODE else self.monitored_symbols)
        self.regime_filter = RegimeFilter(self.depth_tracker, config_get=self._config.get)
        self.cooldowns = CooldownManager(
            config_get=self._config.get,
            connection=self.state_writer._cooldown_conn,
            lock=self.state_writer._guard_lock,
        )
        self.feed_cursors = FeedCursorStore(
            state_db_path,
            connection=self.state_writer._feed_recovery_conn,
            lock=self.state_writer._guard_lock,
        )
        self.route_optimizer = RouteOptimizer()
        self.cost_calibrator = CostMarkoutCalibrator(measurement_only=True)
        try:
            self.cost_calibrator.add_observations(
                observations_from_execution_quality(
                    self.state_reader.get_execution_quality(limit=100_000)
                )
            )
        except Exception:
            logger.exception("Could not restore measurement-only route calibration samples")
        self.settlement_model = SettlementFundingModel()
        self.net_ev_scorer = LowerConfidenceNetEVScorer()
        self.rotation_policy = IncrementalRotationPolicy()
        self.hold_exit_policy = DirectionAwareHoldExitPolicy()
        self.strategy_plugins = StrategyPluginRegistry()
        plugin_notional_cap = float(
            self._config.get("per_symbol_notional_cap_usd")
        )
        plugin_loss_cap = max(
            1.0, float(self._config.get("account_equity_usd")) * 0.01
        )
        for plugin in (
            FundingCalendarOptimizationPlugin(),
            BasisConvergencePlugin(),
        ):
            self.strategy_plugins.register(
                plugin,
                StrategyRiskBudget(
                    strategy_id=plugin.strategy_id,
                    max_gross_notional_usd=plugin_notional_cap,
                    max_position_notional_usd=plugin_notional_cap,
                    max_expected_loss_usd=plugin_loss_cap,
                    max_cvar_usd=plugin_loss_cap,
                    max_concurrent_positions=1,
                ),
            )
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
        self.execution = ExecutionClient(
            endpoint="tcp://127.0.0.1:5555",
            state_writer=self.state_writer,
            producer_id="live-trader-v2",
            command_context=lambda: {
                "account_id": os.getenv("BINANCE_ACCOUNT_ID", "binance-default"),
                "environment": self._trading_mode,
                "strategy_id": "funding-arbitrage-v2",
                "config_version_hash": self._config.version_hash,
            },
        )
        # Rust starts every process without an active config snapshot.  A
        # matching typed ConfigAck is required before testnet/live entries are
        # eligible; paper keeps the same state observable but does not block.
        self._config_hash_consensus: bool = self._trading_mode == "paper"
        self._rust_config_version_hash: str = (
            self._config.version_hash if self._trading_mode == "paper" else ""
        )
        self._config_sync_status: str = (
            "paper_bypass" if self._trading_mode == "paper" else "pending"
        )
        self._config_sync_reason: str = ""
        self._config_sync_intent_id: str = ""
        self._config_sync_inflight_hash: str = ""
        self._config_sync_last_sent_monotonic: float = 0.0
        self._config_sync_event = asyncio.Event()
        if self._trading_mode == "paper":
            self._config_sync_event.set()
        self._private_stream_ready_markets: set[str] = (
            {"spot", "perp"} if self._trading_mode == "paper" else set()
        )
        self._private_stream_status: dict[str, dict] = {}
        self._rust_execution_ready: bool = self._trading_mode == "paper"
        self._rust_execution_readiness_status: str = (
            "paper_bypass" if self._trading_mode == "paper" else "pending"
        )
        self._rust_execution_readiness_reason: str = ""
        self._shutdown_started = False
        self._shutdown_event = asyncio.Event()
        self._background_tasks: list[asyncio.Task] = []
        persisted_runtime_risk = self.state_reader.get_risk()
        self._safe_mode_flags: set[str] = restore_safe_mode_flags(
            persisted_runtime_risk
        )
        self._symbol_safe_mode_blocks: set[str] = set()
        self._symbol_safe_mode_reasons: dict[str, set[str]] = {}
        self._feed_sequence_ready_markets: dict[str, set[str]] = {}
        self._restore_durable_feed_blocks()
        self._blocked_reason: str = ""
        persisted_global_flags = self._safe_mode_flags - _PER_SYMBOL_SAFE_MODE_FLAGS
        persisted_symbol_flags = self._safe_mode_flags & _PER_SYMBOL_SAFE_MODE_FLAGS
        self._runtime_mode: str = (
            "SAFE_MODE"
            if persisted_global_flags
            else ("LIVE_WITH_SYMBOL_BLOCKS" if persisted_symbol_flags else "LIVE")
        )
        self._last_runtime_mode_change: str = str(
            persisted_runtime_risk.get("last_runtime_mode_change")
            or datetime.now(timezone.utc).isoformat()
        )
        self._operator_pause_new_entries_bridge: bool = False
        self._operator_flatten_attempts: dict[str, int] = {}
        self._operator_flatten_cycle_count: int = 0
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
        self._last_retention_run_date: str = str(
            self.state_reader.get_risk().get("last_retention_run_date") or ""
        )
        self._exit_retry_counts: dict[str, int] = {}
        self._execution_event_queue: asyncio.Queue = asyncio.Queue()
        self._loop_heartbeats: dict[str, float] = {}
        self._last_validation_snapshot_bucket: int | None = None
        self._last_entry_funnel_log_monotonic: float = 0.0
        self._preflight_status: str = "idle"
        # Non-paper runtimes remain entry-ineligible until a complete account
        # reconciliation proves every order, position, spot hedge and liability.
        self._account_reconciliation_ready: bool = self._trading_mode == "paper"
        self._bot_started_at: str = datetime.now(timezone.utc).isoformat()
        self._runtime_settling_seconds: float = max(
            0.0,
            _float_or_zero(self._config.get("runtime_settling_seconds") or RUNTIME_SETTLING_SECONDS),
        )
        self._runtime_settling_until_iso: str = (
            datetime.now(timezone.utc) + timedelta(seconds=self._runtime_settling_seconds)
        ).isoformat()
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
        self._latest_exchange_spot_cash_available: float | None = None
        # Symbol -> (authoritative borrowable spot notional in USD, observed_at).
        # No equity/cash fallback is permitted: an absent or stale proof means
        # inverse entry capacity is zero.
        self._spot_borrow_availability_usd: dict[str, tuple[float, datetime]] = {}
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
        self._last_hwm_auto_decay_check_monotonic: float = 0.0
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
                "runtime_settling_until_iso": self._runtime_settling_until_iso,
                "runtime_settling_seconds": self._runtime_settling_seconds,
                "session_id": self._session_id,
                "account_reconciliation_ready": self._account_reconciliation_ready,
                "allow_new_risk": self._account_reconciliation_ready,
                "config_hash_consensus": self._config_hash_consensus,
                "python_config_version_hash": self._config.version_hash,
                "rust_config_version_hash": self._rust_config_version_hash,
                "config_sync_status": self._config_sync_status,
                "config_sync_reason": self._config_sync_reason,
                "private_stream_recovery_ready": self._trading_mode == "paper",
                "private_stream_ready_markets": sorted(
                    self._private_stream_ready_markets
                ),
                "rust_execution_ready": self._rust_execution_ready,
                "rust_execution_readiness_status": self._rust_execution_readiness_status,
                "rust_execution_readiness_reason": self._rust_execution_readiness_reason,
            }
        )
        self.state_writer.flush()
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
        # without a global polling loop - deliberate improvement over the spec.
        self._exit_events: dict[str, asyncio.Event] = {}
        self._exit_rejections: set[str] = set()
        self._pending_exit_intents: dict[str, str] = {}
        self._pending_exit_created_at: dict[str, str] = {}

        # Pending enter tracking: symbol â†’ entry intent data stored at dispatch time.
        # Consumed when ENTER FILLED arrives to write position to SQLite.
        self._pending_enters: dict[str, dict] = {}
        self._stale_pending_enters: dict[str, dict] = {}
        self._recent_entry_rejects: dict[str, list[float]] = {}
        self._recent_stale_intents: dict[str, list[float]] = {}
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
        self._pending_execution_markouts: dict[str, dict[str, object]] = {}

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
        self.subscriber.on("PositionDivergence", self._handle_position_divergence)
        self.subscriber.on("IntentAck", self._on_intent_ack)
        self.subscriber.on("ConfigAck", self._on_config_ack)
        self.subscriber.on("PrivateStreamStatus", self._on_private_stream_status)
        self.subscriber.on("ExecutionReadiness", self._on_execution_readiness)
        self.subscriber.on("TelemetryGap", self._on_telemetry_gap)
        self.subscriber.on("FeedGap", self._handle_feed_gap)
        self.subscriber.on("L2Depth", self._handle_sequenced_depth_event)
        # Start the watcher only after every callback-visible field exists.
        # This also removes a constructor race in the pre-existing reload path.
        self._config.start_watching()

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
        if changed and getattr(self, "_trading_mode", "paper") != "paper":
            # The watcher callback runs on its own thread. Never use the ZMQ
            # socket here; revoke eligibility now and let the event-loop-owned
            # maintenance/startup path transmit the new canonical snapshot.
            self._config_hash_consensus = False
            self._rust_config_version_hash = ""
            self._config_sync_status = "pending_reload"
            self._config_sync_reason = "effective config changed"
            event = getattr(self, "_config_sync_event", None)
            loop = getattr(self, "_loop", None)
            if event is not None:
                if loop is not None and loop.is_running():
                    loop.call_soon_threadsafe(event.clear)
                else:
                    event.clear()
            self._set_config_reload_status(
                {
                    "config_hash_consensus": False,
                    "rust_config_version_hash": "",
                    "config_sync_status": self._config_sync_status,
                    "config_sync_reason": self._config_sync_reason,
                }
            )
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
        if (
            "reset_trade_history" in changed
            and bool(config.get("reset_trade_history"))
            and getattr(self, "state_writer", None) is not None
        ):
            self._reset_all_trades(
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
        self._last_hwm_auto_decay_check_monotonic = 0.0
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

    def _reset_all_trades(
        self,
        *,
        source: str,
        requested_by: str = "",
        clear_live_config: bool = False,
    ) -> None:
        """Clear trade history and reset streaks/HWM."""
        logger.warning(
            "RESET ALL TRADES requested via %s%s - clearing history and streaks",
            source,
            f" ({requested_by})" if requested_by else "",
        )
        self.state_writer.clear_trade_history()
        self.state_writer.clear_execution_events()
        self._loss_streak = 0
        self._win_streak = 0
        self._streak_notional_scale = 1.0
        self.state_writer.set_risk_snapshot(
            {
                "loss_streak": 0,
                "win_streak": 0,
                "streak_notional_scale": 1.0,
            }
        )
        self._reset_equity_high_watermark(source=source, requested_by=requested_by)
        if clear_live_config:
            self._clear_live_config_request("reset_trade_history", False)
        self.state_writer.flush()

    def _maybe_auto_decay_equity_high_watermark(self, account_equity: float) -> None:
        decay_hours = max(0.0, _float_or_zero(self._config.get("hwm_auto_decay_after_hours")))
        if decay_hours <= 0.0 or account_equity <= 0.0 or self._peak_account_equity <= account_equity:
            self._last_hwm_auto_decay_check_monotonic = 0.0
            return

        now_monotonic = time.monotonic()
        if self._last_hwm_auto_decay_check_monotonic <= 0.0:
            self._last_hwm_auto_decay_check_monotonic = now_monotonic
            return

        if (now_monotonic - self._last_hwm_auto_decay_check_monotonic) < (decay_hours * 3600.0):
            return

        self._last_hwm_auto_decay_check_monotonic = now_monotonic
        fraction = float(self._config.get("hwm_auto_decay_fraction") or 0.25)
        fraction = max(0.0, min(1.0, fraction))
        if fraction <= 0.0:
            return

        previous_hwm = self._peak_account_equity
        self._peak_account_equity = max(
            account_equity,
            self._peak_account_equity
            - (self._peak_account_equity - account_equity) * fraction,
        )
        if self._peak_account_equity >= previous_hwm:
            return

        now_iso = datetime.now(timezone.utc).isoformat()
        logger.critical(
            "HWM auto-decay applied: %.2f -> %.2f after %.1f idle hour(s) (current equity %.2f, fraction %.2f)",
            previous_hwm,
            self._peak_account_equity,
            decay_hours,
            account_equity,
            fraction,
        )
        self.state_writer.set_risk_snapshot(
            {
                "account_equity_high_watermark": self._peak_account_equity,
                "account_equity_high_watermark_reset_at": now_iso,
                "account_equity_high_watermark_reset_source": "auto_decay",
                "account_equity_high_watermark_reset_by": f"idle_{decay_hours:.1f}h",
            }
        )

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

    def _safe_mode_codes(self) -> list[dict[str, str | bool]]:
        return describe_safe_mode_flags(self._safe_mode_flags)

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
                | set(self._symbol_safe_mode_blocks)
            )
            if symbol
        }

    def _describe_symbol_block(self, symbol: str) -> str:
        normalized = str(symbol or "").upper()
        if normalized in self._symbol_safe_mode_blocks:
            reasons = self._symbol_safe_mode_reasons.get(normalized, set())
            if reasons == {"depth_sequence_gap"}:
                return "depth sequence gap awaiting fresh spot/perp readiness proofs"
            if reasons:
                return f"symbol safe mode ({', '.join(sorted(reasons))})"
            return "symbol safe mode"
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

    def _is_startup_recovery_symbol(self, symbol: str) -> bool:
        normalized = str(symbol or "").upper()
        if normalized in self._startup_manual_review_symbols:
            return True
        if normalized in self._startup_exit_candidates:
            return True
        row = next(
            (
                position
                for position in self.state_reader.get_positions()
                if str(position.get("symbol", "")).upper() == normalized
            ),
            None,
        )
        return str((row or {}).get("recovery_state") or "").strip().lower() in {
            "manual_review",
            "exit_candidate",
        }

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
            # Canonical long-spot / short-perp path: if the spot hedge is essentially gone,
            # only unwind the perp leg. Skip spot if hedge is below 1% to avoid dust rejections.
            return hedge_ratio < 0.01, False

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
            # Preserve endpoint/status metadata for callers which can make a
            # narrow, environment-specific decision (for example demo Spot's
            # unsupported margin endpoints).  Callers that do not recognise
            # the error still fail closed exactly as before.
            raise BinanceSignedCallError(
                endpoint=endpoint,
                code=code or 0,
                detail=details,
                http_status=status_code,
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
        telemetry_connected = self._telemetry_stream_healthy()
        execution_bridge_healthy = (
            preflight_passed
            and self._last_heartbeat_ack_monotonic > 0.0
            and self._heartbeat_misses < heartbeat_threshold
        )
        python_config_hash = self._config.canonical_snapshot().sha256
        config_consensus_ready = (
            self._trading_mode == "paper"
            or (
                self._config_hash_consensus
                and self._rust_config_version_hash == python_config_hash
            )
        )
        private_stream_recovery_ready = self._trading_mode == "paper" or (
            self._private_stream_ready_markets == {"spot", "perp"}
        )
        runtime_ready = (
            self._runtime_mode in _ENTRY_READY_RUNTIME_MODES
            and preflight_passed
            and self._account_reconciliation_ready
            and config_consensus_ready
            and private_stream_recovery_ready
            and self._rust_execution_ready
        )
        pause_new_entries = bool(self._config.get("pause_new_entries"))
        risk_state = self._ensure_validation_snapshot_for_policy(self.state_reader.get_risk())
        entry_block_reason = self._entry_policy_block_reason(risk_state)
        validation_policy = self._validation_policy_snapshot(risk_state)
        allow_new_risk = (
            runtime_ready
            and self._risk_allow_new_risk
            and not pause_new_entries
            and entry_block_reason is None
        )
        self.state_writer.set_risk_snapshot(
            {
                "trading_mode": self._trading_mode,
                "runtime_mode": self._runtime_mode,
                "session_id": self._session_id,
                "bot_started_at": self._bot_started_at,
                "runtime_settling_until_iso": self._runtime_settling_until_iso,
                "runtime_settling_seconds": self._runtime_settling_seconds,
                "loop_last_alive_at": now_iso,
                "safe_mode_reason": safe_reason,
                "safe_mode_codes": self._safe_mode_codes(),
                "blocked_reason": self._blocked_reason,
                "entry_block_reason": entry_block_reason or "",
                "pause_new_entries": pause_new_entries,
                "allow_new_risk": allow_new_risk,
                "validation_entry_policy": validation_policy["validation_entry_policy"],
                "validation_adjustment_action": validation_policy["validation_adjustment_action"],
                "validation_position_scale": validation_policy["validation_position_scale"],
                "preflight_status": self._preflight_status,
                "runtime_ready": runtime_ready,
                "account_reconciliation_ready": self._account_reconciliation_ready,
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
                "funding_fresh_symbol_count": funding_status["funding_fresh_symbol_count"],
                "funding_stale_symbol_count": funding_status["funding_stale_symbol_count"],
                "funding_stale_symbols": funding_status["funding_stale_symbols"],
                "funding_info_last_refresh_at": funding_status[
                    "funding_info_last_refresh_at"
                ],
                "funding_info_last_error": funding_status["funding_info_last_error"],
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
                "config_version_hash": python_config_hash,
                "python_config_version_hash": python_config_hash,
                "rust_config_version_hash": self._rust_config_version_hash,
                "config_hash_consensus": config_consensus_ready,
                "config_sync_status": self._config_sync_status,
                "config_sync_reason": self._config_sync_reason,
                "config_sync_intent_id": self._config_sync_intent_id,
                "private_stream_recovery_ready": private_stream_recovery_ready,
                "private_stream_ready_markets": sorted(
                    self._private_stream_ready_markets
                ),
                "private_stream_status": dict(self._private_stream_status),
                "rust_execution_ready": self._rust_execution_ready,
                "rust_execution_readiness_status": self._rust_execution_readiness_status,
                "rust_execution_readiness_reason": self._rust_execution_readiness_reason,
            }
        )
        self.state_writer.flush()
        self._write_watchdog_heartbeat(now_iso=now_iso)

    def _write_watchdog_heartbeat(self, *, now_iso: str | None = None) -> None:
        import sys
        path = _WATCHDOG_HEARTBEAT_PATH
        if "pytest" in sys.modules:
            import tempfile
            path = os.path.join(tempfile.gettempdir(), f"runtime_heartbeat_{self._session_id}.json")
        heartbeat_time = now_iso or datetime.now(timezone.utc).isoformat()
        now_monotonic = time.monotonic()
        heartbeat_ages = {
            name: round(max(0.0, now_monotonic - last_seen), 1)
            for name, last_seen in self._loop_heartbeats.items()
        }
        payload = {
            "pid": os.getpid(),
            "session_id": self._session_id,
            "runtime_mode": self._runtime_mode,
            "preflight_status": self._preflight_status,
            "safe_mode_reason": self._safe_mode_reason(),
            "safe_mode_codes": self._safe_mode_codes(),
            "blocked_reason": self._blocked_reason,
            "last_runtime_mode_change": self._last_runtime_mode_change,
            "loop_last_alive_at": heartbeat_time,
            "loop_heartbeat_ages": heartbeat_ages,
            "updated_at": heartbeat_time,
        }
        temp_path = f"{path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(temp_path, path)
        except PermissionError:
            # Windows can reject replacing an existing destination even after
            # all Python handles have been closed.  Preserve the last complete
            # heartbeat while installing the new one instead of falling back
            # to an in-place (and therefore partially readable) write.
            rollback_path = f"{path}.previous"
            if os.path.exists(rollback_path):
                os.remove(rollback_path)
            if os.path.exists(path):
                os.replace(path, rollback_path)
            try:
                os.replace(temp_path, path)
            except BaseException:
                if not os.path.exists(path) and os.path.exists(rollback_path):
                    os.replace(rollback_path, path)
                raise
            if os.path.exists(rollback_path):
                os.remove(rollback_path)

    async def _run_liveness_loop(self, interval_s: float = 5.0) -> None:
        while not self._shutdown_event.is_set():
            try:
                self._loop_heartbeats["liveness_loop"] = time.monotonic()
                now_iso = datetime.now(timezone.utc).isoformat()
                self._write_watchdog_heartbeat(now_iso=now_iso)
                now_ts = time.monotonic()
                heartbeat_ages = {k: round(now_ts - v, 1) for k, v in self._loop_heartbeats.items()}
                self.state_writer.set_risk_snapshot(
                    {
                        "loop_last_alive_at": now_iso,
                        "loop_heartbeat_ages": heartbeat_ages,
                        "execution_queue_backlog": self._execution_event_queue.qsize(),
                    }
                )
                self.state_writer.flush()
            except Exception as exc:
                logger.debug("Could not persist trader liveness heartbeat: %s", exc)
            if await self._sleep_or_shutdown(interval_s):
                break

    def _telemetry_stream_healthy(self) -> bool:
        max_runtime_staleness = float(self._config.get("max_runtime_staleness_seconds"))
        if self.subscriber.is_connected:
            return True
        if self._last_telemetry_event_monotonic <= 0.0:
            return False
        return (time.monotonic() - self._last_telemetry_event_monotonic) <= max_runtime_staleness

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
        if isinstance(exc, BinanceSignedCallError):
            return exc.code in _RECOVERABLE_BINANCE_SIGNED_ERROR_CODES or exc.http_status in (400, 404, 408, 500, 502, 503, 504)
        message = str(exc)
        return "HTTP 400" in message or "HTTP 404" in message or "HTTP 408" in message or "HTTP 5" in message

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
            (api_secret or "").encode("utf-8"),
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
            (api_secret or "").encode("utf-8"),
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
        import msgpack
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
            unpacker = msgpack.Unpacker(raw=False)

            while time.monotonic() < deadline:
                now = time.monotonic()
                if now >= next_send_at:
                    self.execution.send_heartbeat(heartbeat_id)
                    next_send_at = now + send_interval_s

                remaining = min(next_send_at - time.monotonic(), deadline - time.monotonic())
                remaining = max(0.1, remaining)

                try:
                    read_method = getattr(reader, "read", None)
                    if read_method is not None:
                        read_coro = read_method(8192)
                    else:
                        read_method = getattr(reader, "readline", None)
                        if read_method is not None:
                            read_coro = read_method()
                        else:
                            read_coro = None
                    if read_coro is None:
                        raise RuntimeError("heartbeat reader has neither read() nor readline()")
                    chunk = await asyncio.wait_for(read_coro, timeout=remaining)
                    if not chunk:
                        return False
                    events = []
                    try:
                        unpacker.feed(chunk)
                        events.extend(event for event in unpacker if isinstance(event, dict))
                    except Exception:
                        events.clear()
                    if not events:
                        try:
                            decoded = json.loads(chunk.decode("utf-8").strip())
                            if isinstance(decoded, dict):
                                events.append(decoded)
                        except Exception:
                            pass
                    for event in events:
                        if event.get("event") == "HeartbeatAck" and event.get("heartbeat_id") == heartbeat_id:
                            self._on_heartbeat_ack(
                                heartbeat_id=event.get("heartbeat_id"),
                                status=event.get("status", ""),
                                ts_ms=event.get("ts_ms"),
                            )
                            return True
                except asyncio.TimeoutError:
                    continue
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
            client_order_id = order_client_id(order)
            if not is_bot_client_order_id(client_order_id):
                # A symbol/order-id match does not establish ownership.  Refuse
                # the side effect and leave the unrelated order untouched.
                failures.append(
                    f"{symbol}:ownership_refused:{client_order_id or 'missing_client_order_id'}"
                )
                continue
            params: dict[str, str | int | float] = {"symbol": symbol}
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
        """Cancel bot-owned entry orders for a symbol, never unrelated orders."""
        target = symbol.upper()
        futures_orders = [
            o for o in (snapshot.get("futures_open_orders") or [])
            if (
                isinstance(o, dict)
                and str(o.get("symbol", "")).upper() == target
                and is_bot_client_order_id(order_client_id(o))
            )
        ]
        spot_orders = [
            o for o in (snapshot.get("spot_open_orders") or [])
            if (
                isinstance(o, dict)
                and str(o.get("symbol", "")).upper() == target
                and is_bot_client_order_id(order_client_id(o))
            )
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
            for order in (
                list(snapshot.get("futures_open_orders") or [])
                + list(snapshot.get("spot_open_orders") or [])
                + list(snapshot.get("margin_open_orders") or [])
            )
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

    @staticmethod
    def _bot_open_order_symbols(snapshot: dict | None) -> set[str]:
        return {
            str(order.get("symbol") or "").upper()
            for order in LiveTraderV2._snapshot_open_orders(snapshot)
            if order.get("symbol") and is_bot_client_order_id(order_client_id(order))
        }

    async def _clear_startup_open_orders(self, snapshot: dict, *, stage: str) -> dict:
        open_orders = self._snapshot_open_orders(snapshot)
        if not open_orders:
            return snapshot

        order_symbols = self._open_order_symbols(open_orders)
        owned_orders = list(bot_owned_orders(open_orders))
        external_orders = list(unrelated_orders(open_orders))
        logger.warning(
            "%s found %d open exchange order(s) for %s (%d bot-owned, %d unrelated)",
            stage,
            len(open_orders),
            ", ".join(order_symbols or ["unknown"]),
            len(owned_orders),
            len(external_orders),
        )

        futures_orders = [
            order for order in (snapshot.get("futures_open_orders") or [])
            if isinstance(order, dict) and is_bot_client_order_id(order_client_id(order))
        ]
        spot_orders = [
            order for order in (snapshot.get("spot_open_orders") or [])
            if isinstance(order, dict) and is_bot_client_order_id(order_client_id(order))
        ]
        margin_orders = [
            order for order in (snapshot.get("margin_open_orders") or [])
            if isinstance(order, dict) and is_bot_client_order_id(order_client_id(order))
        ]
        cancel_failures: list[str] = []
        if futures_orders:
            cancel_failures.extend(await self._cancel_open_orders(futures_orders, futures=True))
        if spot_orders:
            cancel_failures.extend(await self._cancel_open_orders(spot_orders, futures=False))
        if margin_orders:
            # This runtime never creates margin working orders, so no cancellation
            # endpoint is authorised here.  Preserve them as an ownership incident.
            cancel_failures.extend(
                f"{str(order.get('symbol') or '').upper()}:unsupported_margin_order_cleanup"
                for order in margin_orders
            )

        if cancel_failures:
            self.state_writer.set_risk_snapshot(
                {
                    "startup_reconciliation_status": "blocked_open_orders",
                    "startup_reconciliation_time": datetime.now(timezone.utc).isoformat(),
                    "startup_reconciliation_open_order_symbols": order_symbols,
                    "startup_reconciliation_open_order_count": len(open_orders),
                    "startup_reconciliation_bot_owned_open_order_count": len(owned_orders),
                    "startup_reconciliation_unrelated_open_order_count": len(external_orders),
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

        if owned_orders:
            await asyncio.sleep(0.5)
            refreshed_snapshot = await self._fetch_exchange_startup_snapshot()
        else:
            refreshed_snapshot = snapshot
        remaining_orders = self._snapshot_open_orders(refreshed_snapshot)
        if remaining_orders:
            remaining_symbols = self._open_order_symbols(remaining_orders)
            remaining_owned = list(bot_owned_orders(remaining_orders))
            remaining_external = list(unrelated_orders(remaining_orders))
            self.state_writer.set_risk_snapshot(
                {
                    "startup_reconciliation_status": "blocked_open_orders",
                    "startup_reconciliation_time": datetime.now(timezone.utc).isoformat(),
                    "startup_reconciliation_open_order_symbols": remaining_symbols,
                    "startup_reconciliation_open_order_count": len(remaining_orders),
                    "startup_reconciliation_bot_owned_open_order_count": len(remaining_owned),
                    "startup_reconciliation_unrelated_open_order_count": len(remaining_external),
                    "startup_reconciliation_unrelated_open_orders": [
                        {
                            "symbol": str(order.get("symbol") or "").upper(),
                            "client_order_id": order_client_id(order),
                        }
                        for order in remaining_external
                    ],
                    "startup_reconciliation_cleared_open_order_symbols": order_symbols,
                    "startup_reconciliation_cleared_open_order_count": len(owned_orders),
                    "allow_new_risk": False,
                    "reasons": [
                        (
                            f"{stage.lower()} blocked: unrelated exchange orders require ownership review"
                            if remaining_external
                            else f"{stage.lower()} blocked: exchange still reports bot orders after cleanup"
                        ),
                    ],
                }
            )
            reason = (
                f"{stage.lower()} blocked: exchange still reports {len(remaining_orders)} "
                "open order(s); unrelated orders were left untouched"
            )
            self._set_blocked_reason(reason)
            raise StartupBlockedError(reason)

        self.state_writer.set_risk_snapshot(
            {
                "startup_reconciliation_cleared_open_order_symbols": order_symbols,
                "startup_reconciliation_cleared_open_order_count": len(owned_orders),
                "startup_reconciliation_time": datetime.now(timezone.utc).isoformat(),
            }
        )
        logger.warning(
            "%s cancelled %d bot-owned exchange open order(s) for %s",
            stage,
            len(owned_orders),
            ", ".join(order_symbols or ["unknown"]),
        )
        return refreshed_snapshot

    async def _resolve_pending_intents_from_exchange(self, snapshot: dict) -> None:
        pending_rows = self.state_reader.get_pending_intents(
            statuses=["DISPATCHING", "PENDING_ACK", "TIMEOUT", "NEW", "FILLED"]
        )
        if not pending_rows:
            return

        all_open_order_symbols = set(
            self._open_order_symbols(self._snapshot_open_orders(snapshot))
        )
        position_symbols = {
            str(position.get("symbol", "")).upper()
            for position in self._open_snapshot_position_rows(snapshot)
        }

        for row in pending_rows:
            symbol = str(row.get("symbol", "")).upper()
            intent_type = str(row.get("intent_type", "")).upper()
            intent_id = str(row.get("intent_id", ""))
            if intent_type.startswith("ENTER"):
                if symbol in position_symbols:
                    self.state_writer.delete_pending_intent(intent_id)
                elif symbol in all_open_order_symbols:
                    raise StartupBlockedError(
                        f"Unresolved pending ENTER after recovery: {symbol}:{intent_type} - "
                        "an exchange open order exists but ownership/effect is not terminal"
                    )
                else:
                    self.state_writer.delete_pending_intent(intent_id)
            elif intent_type.startswith("EXIT"):
                if symbol in all_open_order_symbols:
                    raise StartupBlockedError(
                        f"Unresolved pending EXIT after recovery: {symbol}:{intent_type} - "
                        "an exchange open order exists but ownership/effect is not terminal"
                    )
                if symbol not in position_symbols:
                    # Position already closed - intent is stale, clean it up.
                    self.state_writer.delete_pending_intent(intent_id)
                else:
                    # Position still open. Delete the stale intent and let the trading
                    # loop re-dispatch the exit on the first cycle. Blocking here causes
                    # a permanent deadlock: the position needs to be exited but the
                    # trader can't start to exit it.
                    logger.critical(
                        "Startup recovery: %s has confirmed open position but stale EXIT intent "
                        "(%s). Clearing intent - trading loop will re-dispatch exit.",
                        symbol, intent_id,
                    )
                    self.state_writer.delete_pending_intent(intent_id)

    async def _retry_async_fn(self, fn, *args, **kwargs):
        backoffs = [1.0, 2.0, 4.0]
        for attempt in range(len(backoffs) + 1):
            try:
                return await fn(*args, **kwargs)
            except Exception as exc:
                if attempt == len(backoffs):
                    raise
                logger.warning("Preflight REST call failed (%s); retrying in %.1fs", exc, backoffs[attempt])
                await asyncio.sleep(backoffs[attempt])

    def _validate_live_config_for_startup(self) -> None:
        if self._trading_mode == "paper":
            return
        if self._config.last_error:
            raise RuntimeError(f"live_config validation failed: {self._config.last_error}")
        missing = self._config.missing_required_live_keys()
        if missing:
            raise RuntimeError(
                "live_config missing required live risk key(s): " + ", ".join(missing)
            )
        dangerous_keys = [
            "allow_autonomous_inverse_liquidation",
            "reset_equity_high_watermark",
        ]
        # Testnet may autonomously classify and unwind ordinary startup hedge
        # gaps. Live keeps the explicit operator gate.
        if self._trading_mode == "live":
            dangerous_keys.append("autonomous_startup_recovery")
        dangerous_enabled = [
            key
            for key in dangerous_keys
            if bool(self._config.get(key))
        ]
        if dangerous_enabled:
            raise RuntimeError(
                "live_config dangerous flag(s) must be disabled for startup: "
                + ", ".join(dangerous_enabled)
            )

    async def _run_preflight(self) -> None:
        self._preflight_status = "running"
        self._persist_runtime_state()
        try:
            await self._db_write_probe()
            self._validate_live_config_for_startup()
            self._validate_required_credentials()

            if self._trading_mode != "paper":
                await self._retry_async_fn(self._ping_exchange)
                await self._retry_async_fn(self._sync_binance_time)
                await self._retry_async_fn(
                    self._signed_get_json_with_fallback,
                    base_url=self._futures_base_url,
                    endpoints=("/fapi/v3/account", "/fapi/v2/account"),
                    api_key=self._futures_api_key,
                    api_secret=self._futures_api_secret,
                )
                await self._retry_async_fn(
                    self._signed_get_json,
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
                snapshot = await self._retry_async_fn(self._fetch_exchange_startup_snapshot)
                snapshot = await self._clear_startup_open_orders(snapshot or {}, stage="Startup preflight")
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

    def _account_reconciliation_asset_prices(self, snapshot: dict) -> dict[str, float]:
        prices: dict[str, float] = {}
        for asset, price in dict(snapshot.get("spot_ticker_prices") or {}).items():
            normalized_price = _float_or_zero(price)
            if normalized_price > 0.0:
                prices[str(asset).upper()] = normalized_price
        for row in self._open_snapshot_position_rows(snapshot):
            symbol = str(row.get("symbol") or "").upper()
            price = _float_or_zero(row.get("markPrice"))
            if symbol and price > 0.0:
                prices[_extract_base_asset(symbol)] = price
        for symbol, price in self._mark_prices.items():
            normalized_price = _float_or_zero(price)
            if normalized_price > 0.0:
                prices[_extract_base_asset(symbol)] = normalized_price
        for asset in self._build_spot_balance_map(snapshot.get("spot_account")):
            if asset in prices or asset in _USD_COLLATERAL_ASSETS:
                continue
            symbol = f"{asset}USDT"
            price = self.depth_tracker.spot_mid_price(symbol)
            if price > 0.0:
                prices[asset] = price
        return prices

    def _account_reconciliation_cash_tolerance_usd(self) -> float:
        """Return the explicit testnet dust tolerance without weakening live."""

        default = 0.01
        if self._trading_mode != "testnet":
            return default
        try:
            configured = float(
                os.getenv(
                    "BONGUS_TESTNET_RECONCILIATION_DUST_TOLERANCE_USD",
                    str(default),
                )
            )
        except (TypeError, ValueError):
            return default
        # Testnet can accumulate balances below its available conversion/order
        # routes. Keep the exception explicit and economically immaterial.
        return min(1.0, max(default, configured))

    def _build_account_reconciliation_report(
        self,
        snapshot: dict,
        *,
        generated_at: str | None = None,
    ) -> AccountReconciliationReport:
        return reconcile_account_snapshot(
            snapshot,
            local_positions=self.state_reader.get_positions_for_current_mode(),
            pending_intents=self.state_reader.get_pending_intents(),
            asset_prices_usd=self._account_reconciliation_asset_prices(snapshot),
            cash_tolerance_usd=str(self._account_reconciliation_cash_tolerance_usd()),
            expected_account_uid=os.getenv("BONGUS_EXPECTED_ACCOUNT_UID", "").strip(),
            require_account_uid=self._trading_mode != "paper",
            generated_at=generated_at,
        )

    def _publish_account_reconciliation(
        self,
        snapshot: dict,
        *,
        generated_at: str | None = None,
    ) -> AccountReconciliationReport:
        report = self._build_account_reconciliation_report(
            snapshot,
            generated_at=generated_at,
        )
        self._account_reconciliation_ready = report.ready
        self.state_writer.set_risk_snapshot(
            {
                **report.risk_snapshot(),
                "allow_new_risk": report.ready,
            }
        )
        self.state_writer.flush()
        self._set_safe_mode_flag("account_reconciliation", not report.ready)
        self._try_clear_execution_reconciliation(report)
        return report

    def _try_clear_execution_reconciliation(
        self,
        report: AccountReconciliationReport,
    ) -> bool:
        """Clear an ambiguity guard only after both exchange and local state are flat."""

        if not report.ready or report.positions or report.orders:
            return False
        if self.state_reader.get_positions_for_current_mode():
            return False
        nonterminal = {
            "DISPATCHING",
            "PENDING_ACK",
            "SENT",
            "NEW",
            "PARTIALLY_FILLED",
            "TIMEOUT",
            "STALE",
            "NEEDS_RECONCILIATION",
        }
        if any(
            str(row.get("status") or "").upper() in nonterminal
            for row in self.state_reader.get_pending_intents(limit=10_000)
        ):
            return False
        risk = self.state_reader.get_risk()
        if (
            "execution_reconciliation" not in self._safe_mode_flags
            and not bool(risk.get("execution_reconciliation_required"))
        ):
            return True

        self.state_writer.set_risk_snapshot(
            {
                "execution_reconciliation_required": False,
                "execution_reconciliation_issue": {},
                "execution_reconciliation_symbol": "",
                "execution_reconciliation_reason": "",
            }
        )
        self.state_writer.flush()
        self._set_safe_mode_flag("execution_reconciliation", False)
        logger.warning(
            "Cleared execution-reconciliation guard after signed exchange and local projections proved flat"
        )
        return True

    def _derive_spot_account_balance_usd(self, spot_balances: dict[str, float], snapshot: dict) -> float:
        total_usd = 0.0
        for asset, qty in spot_balances.items():
            if asset in _USD_COLLATERAL_ASSETS:
                total_usd += qty
                continue
            symbol = f"{asset}USDT"
            mark_price = self._mark_prices.get(symbol, 0.0)
            if mark_price <= 0.0:
                for pos in snapshot.get("position_risk", []):
                    if pos.get("symbol") == symbol:
                        mark_price = _float_or_zero(pos.get("markPrice"))
                        break
            if mark_price > 0.0:
                total_usd += qty * mark_price
        return total_usd

    def _derive_spot_free_balance_usd(self, spot_account: dict | None, snapshot: dict) -> float:
        if not isinstance(spot_account, dict):
            return 0.0
        free_balances = {
            str(row.get("asset") or "").upper(): _float_or_zero(row.get("free"))
            for row in spot_account.get("balances", [])
            if str(row.get("asset") or "").strip() and _float_or_zero(row.get("free")) > 0.0
        }
        return self._derive_spot_account_balance_usd(free_balances, snapshot)

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
        if direction == "long" and hedge_ratio < (1.0 - _SPOT_HEDGE_SHORTFALL_TOLERANCE_PCT):
            if not bool(self._config.get("autonomous_startup_recovery")):
                return (
                    "manual_review",
                    f"{symbol} has a spot hedge gap (ratio {hedge_ratio:.2%}) and requires manual review",
                )

        if unsupported_direction:
            if bool(self._config.get("allow_autonomous_inverse_liquidation") or ALLOW_AUTONOMOUS_INVERSE_LIQUIDATION):
                return (
                    "exit_candidate",
                    f"{symbol} recovered with inverse/long-perp structure; auto-liquidating via perp-only exit",
                )
            return (
                "manual_review",
                f"{symbol} recovered with inverse/long-perp structure that this runtime cannot safely rebuild",
            )

        if not funding_signal_available:
            return (
                "tracked",
                f"{symbol} recovered while funding data is stale; holding until fresh rates arrive",
            )

        # 1. Basic Worth Check: Check if funding has decayed below the exit threshold.
        if self._funding_has_decayed(direction, ann_funding):
            return (
                "exit_candidate",
                f"{symbol} funding decayed to {ann_funding * 100:.2f}% annualized and should be exited",
            )

        # 2. Hedge Integrity: If there is a hedge gap, decide between continuing or recycling.
        if direction == "long" and hedge_ratio < (1.0 - _SPOT_HEDGE_SHORTFALL_TOLERANCE_PCT):
            entry_threshold = self._effective_entry_threshold()
            # Funding yield never proves exposure integrity.  Even an exceptional
            # rate cannot authorize carrying an unexplained naked leg.
            if ann_funding >= (entry_threshold * 2.0):
                return (
                    "exit_candidate",
                    f"{symbol} has hedge gap (ratio {hedge_ratio:.2%}); exceptional funding "
                    f"({ann_funding * 100:.2f}%) does not override reconciliation safety",
                )
            
            # If funding is good (above entry threshold) but not exceptional, recycle the position.
            # Marking as exit_candidate will close the messy position; the allocator will then 
            # naturally re-enter it correctly hedged if it remains a top candidate.
            if ann_funding >= entry_threshold:
                return (
                    "exit_candidate",
                    f"{symbol} has hedge gap (ratio {hedge_ratio:.2%}) and will be recycled to restore delta-neutrality (funding {ann_funding * 100:.2f}% >= threshold {entry_threshold * 100:.2f}%)",
                )

            # If funding is mediocre (between exit and entry thresholds), just exit. 
            # It wouldn't be re-entered anyway, so it's not worth keeping a messy version.
            return (
                "exit_candidate",
                f"{symbol} has hedge gap (ratio {hedge_ratio:.2%}) and funding ({ann_funding * 100:.2f}%) is below entry threshold; closing to clean up",
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
            if symbol in active_recovery_symbols
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

    def _statement_history_start_ms(self, statement_source: str, *, now_ms: int) -> int:
        """Resume statement history with a one-minute overlap for late rows."""

        account_id = os.getenv("BINANCE_ACCOUNT_ID", "binance-default")
        cursor = self.state_reader.get_exchange_statement_cursor(
            venue="BINANCE",
            account_id=account_id,
            statement_source=statement_source,
        )
        retention_floor = now_ms - 90 * 24 * 60 * 60 * 1000
        if cursor is None:
            return retention_floor
        return max(
            retention_floor,
            int(cursor.get("event_time_ms") or retention_floor) - 60_000,
        )

    async def _fetch_futures_income_history(self, *, now_ms: int) -> list[dict]:
        start_ms = self._statement_history_start_ms(
            BINANCE_FUTURES_INCOME,
            now_ms=now_ms,
        )
        rows: list[dict] = []
        cursor_ms = start_ms
        for _page in range(100):
            payload = await self._signed_get_json(
                base_url=self._futures_base_url,
                endpoint="/fapi/v1/income",
                params={"startTime": cursor_ms, "endTime": now_ms, "limit": 1000},
                api_key=self._futures_api_key,
                api_secret=self._futures_api_secret,
            )
            if not isinstance(payload, list):
                raise RuntimeError("futures income history response was not a list")
            page_rows = [row for row in payload if isinstance(row, dict)]
            rows.extend(page_rows)
            if len(page_rows) < 1000:
                break
            next_cursor = max(int(_float_or_zero(row.get("time"))) for row in page_rows) + 1
            if next_cursor <= cursor_ms:
                raise RuntimeError("futures income pagination did not advance")
            cursor_ms = next_cursor
        else:
            raise RuntimeError("futures income pagination exceeded 100 pages")

        deduplicated: dict[tuple[str, str], dict] = {}
        for row in rows:
            key = (
                str(row.get("incomeType") or "").upper(),
                str(row.get("tranId") or ""),
            )
            deduplicated[key] = row
        return sorted(
            deduplicated.values(),
            key=lambda row: (
                int(_float_or_zero(row.get("time"))),
                str(row.get("incomeType") or ""),
                str(row.get("tranId") or ""),
            ),
        )

    async def _fetch_margin_interest_history(self, *, now_ms: int) -> list[dict]:
        start_ms = self._statement_history_start_ms(
            BINANCE_MARGIN_INTEREST,
            now_ms=now_ms,
        )
        max_window_ms = 30 * 24 * 60 * 60 * 1000 - 1
        rows: list[dict] = []
        window_start = start_ms
        while window_start <= now_ms:
            window_end = min(now_ms, window_start + max_window_ms)
            page_number = 1
            while page_number <= 1_000:
                payload = await self._signed_get_json(
                    base_url=self._spot_base_url,
                    endpoint="/sapi/v1/margin/interestHistory",
                    params={
                        "startTime": window_start,
                        "endTime": window_end,
                        "current": page_number,
                        "size": 100,
                    },
                    api_key=self._spot_api_key,
                    api_secret=self._spot_api_secret,
                )
                if isinstance(payload, dict):
                    raw_rows = payload.get("rows") or []
                    total = int(_float_or_zero(payload.get("total")))
                elif isinstance(payload, list):
                    raw_rows = payload
                    total = len(payload)
                else:
                    raise RuntimeError("margin interest history response was not an object")
                if not isinstance(raw_rows, list):
                    raise RuntimeError("margin interest history rows were not a list")
                page_rows = [row for row in raw_rows if isinstance(row, dict)]
                rows.extend(page_rows)
                if len(page_rows) < 100 or page_number * 100 >= total:
                    break
                page_number += 1
            else:
                raise RuntimeError("margin interest pagination exceeded 1000 pages")
            window_start = window_end + 1

        deduplicated: dict[tuple[str, str], dict] = {}
        for row in rows:
            event_time = row.get("interestAccuredTime") or row.get("interestAccruedTime")
            key = (str(row.get("txId") or ""), str(event_time or ""))
            deduplicated[key] = row
        return sorted(
            deduplicated.values(),
            key=lambda row: (
                int(
                    _float_or_zero(
                        row.get("interestAccuredTime")
                        or row.get("interestAccruedTime")
                    )
                ),
                str(row.get("txId") or ""),
            ),
        )

    async def _fetch_exchange_startup_snapshot(self) -> dict:
        await self._sync_binance_time()
        snapshot_errors: dict[str, str] = {}
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
            snapshot_errors["spot_account"] = str(exc)[:300]
            snapshot_errors["spot_open_orders"] = str(exc)[:300]

        spot_ticker_prices: dict[str, float] = {}
        try:
            ticker_rows = await self._public_get_json(
                f"{self._spot_base_url}/api/v3/ticker/price"
            )
            if not isinstance(ticker_rows, list):
                raise RuntimeError("spot ticker response was not a list")
            for row in ticker_rows:
                if not isinstance(row, dict):
                    continue
                symbol = str(row.get("symbol") or "").upper()
                price = _float_or_zero(row.get("price"))
                if not symbol.endswith("USDT") or price <= 0.0:
                    continue
                spot_ticker_prices[_extract_base_asset(symbol)] = price
        except Exception as exc:
            # Non-stable residual inventory will remain unvalued and therefore
            # block reconciliation. Stable-only accounts do not need this
            # public endpoint to establish ownership readiness.
            logger.warning(
                "Spot ticker prices unavailable during startup reconciliation: %s",
                exc,
            )

        margin_account = None
        margin_open_orders: list[dict] = []
        margin_account_status = "unknown"
        margin_open_orders_status = "unknown"
        try:
            margin_account = await self._signed_get_json(
                base_url=self._spot_base_url,
                endpoint="/sapi/v1/margin/account",
                api_key=self._spot_api_key,
                api_secret=self._spot_api_secret,
            )
            margin_account_status = "available"
        except BinanceSignedCallError as exc:
            # Binance -3003 means the key/account has no cross-margin account;
            # that is positive proof of no margin liabilities, not missing truth.
            if exc.code == -3003 or _testnet_margin_endpoint_is_unsupported(exc):
                margin_account_status = "disabled"
            else:
                snapshot_errors["margin_account"] = str(exc)[:300]
                logger.warning("Margin liability snapshot unavailable: %s", exc)
        except Exception as exc:
            snapshot_errors["margin_account"] = str(exc)[:300]
            logger.warning("Margin liability snapshot unavailable: %s", exc)

        if margin_account_status == "disabled":
            margin_open_orders_status = "disabled"
        else:
            try:
                margin_open_orders = await self._signed_get_json(  # type: ignore[assignment]
                    base_url=self._spot_base_url,
                    endpoint="/sapi/v1/margin/openOrders",
                    api_key=self._spot_api_key,
                    api_secret=self._spot_api_secret,
                )
                margin_open_orders_status = "available"
            except BinanceSignedCallError as exc:
                if exc.code == -3003 or _testnet_margin_endpoint_is_unsupported(exc):
                    margin_open_orders_status = "disabled"
                    if margin_account_status == "unknown":
                        margin_account_status = "disabled"
                else:
                    snapshot_errors["margin_open_orders"] = str(exc)[:300]
                    logger.warning("Margin open-order snapshot unavailable: %s", exc)
            except Exception as exc:
                snapshot_errors["margin_open_orders"] = str(exc)[:300]
                logger.warning("Margin open-order snapshot unavailable: %s", exc)

        statement_now_ms = int(time.time() * 1000) + self._binance_time_offset_ms
        futures_income: list[dict] = []
        try:
            futures_income = await self._fetch_futures_income_history(now_ms=statement_now_ms)
        except Exception as exc:
            logger.warning("Futures income history unavailable during reconciliation: %s", exc)
            snapshot_errors["futures_income"] = str(exc)[:300]
            snapshot_errors["funding_income"] = str(exc)[:300]
        funding_income = [
            row
            for row in futures_income
            if str(row.get("incomeType") or "").upper() == "FUNDING_FEE"
        ]

        margin_interest: list[dict] = []
        margin_interest_status = "unknown"
        if margin_account_status == "disabled":
            margin_interest_status = "disabled"
        else:
            try:
                margin_interest = await self._fetch_margin_interest_history(
                    now_ms=statement_now_ms
                )
                margin_interest_status = "available"
            except BinanceSignedCallError as exc:
                if exc.code == -3003 or _testnet_margin_endpoint_is_unsupported(exc):
                    margin_interest_status = "disabled"
                    if margin_account_status == "unknown":
                        margin_account_status = "disabled"
                else:
                    snapshot_errors["margin_interest"] = str(exc)[:300]
                    logger.warning("Margin interest history unavailable: %s", exc)
            except Exception as exc:
                snapshot_errors["margin_interest"] = str(exc)[:300]
                logger.warning("Margin interest history unavailable: %s", exc)

        return {
            "futures_account": futures_account,
            "position_risk": position_risk,
            "futures_open_orders": futures_open_orders,
            "spot_account": spot_account,
            "spot_open_orders": spot_open_orders,
            "spot_ticker_prices": spot_ticker_prices,
            "margin_account": margin_account,
            "margin_account_status": margin_account_status,
            "margin_open_orders": margin_open_orders,
            "margin_open_orders_status": margin_open_orders_status,
            "futures_income": futures_income,
            "funding_income": funding_income,
            "margin_interest": margin_interest,
            "margin_interest_status": margin_interest_status,
            "statement_history_status": {
                "futures_income": (
                    "error" if "futures_income" in snapshot_errors else "available"
                ),
                "margin_interest": margin_interest_status,
            },
            "snapshot_errors": snapshot_errors,
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

        metrics = calculate_metrics(self.state_reader, config=self._config)
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
        self._persist_validation_policy_snapshot()

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
        raw_hedge_gap_symbols = audit_snapshot.get("audit_reconciliation_spot_hedge_gaps", [])
        hedge_gap_symbols = (
            [str(symbol) for symbol in raw_hedge_gap_symbols]
            if isinstance(raw_hedge_gap_symbols, list)
            else []
        )
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
        statement_history_ready = self._ingest_exchange_statement_snapshot(snapshot)
        if not statement_history_ready:
            critical = True

        spot_balances = self._build_spot_balance_map(snapshot.get("spot_account"))
        spot_usd = self._derive_spot_account_balance_usd(spot_balances, snapshot)
        spot_free_usd = self._derive_spot_free_balance_usd(
            snapshot.get("spot_account"),
            snapshot,
        )
        self._latest_exchange_spot_cash_available = spot_free_usd
        futures_account_equity = _derive_futures_account_balance(
            snapshot.get("futures_account"),
            preferred_fields=("totalMarginBalance", "totalWalletBalance"),
            asset_field_name="marginBalance",
        )
        total_account_equity = futures_account_equity + spot_usd if futures_account_equity > 0.0 else futures_account_equity

        exchange_equity_snapshot = self._cache_exchange_equity_snapshot(
            account_equity=total_account_equity,
            available_balance=_derive_futures_account_balance(
                snapshot.get("futures_account"),
                preferred_fields=("availableBalance",),
                asset_field_name="availableBalance",
            ),
            captured_at=sample_time,
        )
        if exchange_equity_snapshot:
            self.state_writer.set_risk_snapshot(exchange_equity_snapshot)
        self.state_writer.set_risk_snapshot(
            {"exchange_spot_cash_available_usd": spot_free_usd}
        )
        spot_balances = self._build_spot_balance_map(snapshot.get("spot_account"))
        spot_account_available = snapshot.get("spot_account") is not None
        db_positions = {
            str(row.get("symbol", "")).upper(): row
            for row in self.state_reader.get_positions()
            if row.get("symbol")
        }
        ownership_before_apply = self._build_account_reconciliation_report(
            snapshot,
            generated_at=sample_time,
        )
        exchange_only_symbols = set(ownership_before_apply.exchange_only_symbols)
        exchange_position_symbols: set[str] = set()
        for raw_position in self._open_snapshot_position_rows(snapshot):
            symbol = str(raw_position.get("symbol", "")).upper()
            position_amt = _float_or_zero(raw_position.get("positionAmt"))
            qty = abs(position_amt)
            exchange_mark_price = _float_or_zero(raw_position.get("markPrice"))
            notional = qty * exchange_mark_price if exchange_mark_price > 0.0 else qty
            if not symbol or qty <= _POSITION_QTY_TOLERANCE or notional < 5.0:
                continue
            exchange_position_symbols.add(symbol)
            if symbol in exchange_only_symbols:
                self._restore_live_position_from_exchange(
                    raw_position,
                    entry_context={},
                    spot_balances=spot_balances,
                )
                manual_reason = (
                    f"{symbol} appeared on the exchange during periodic reconciliation "
                    "without durable local ownership lineage"
                )
                self.state_writer.update_position_metrics(
                    symbol,
                    recovery_state="manual_review",
                )
                self._startup_manual_review_symbols[symbol] = manual_reason
                db_positions[symbol] = next(
                    (
                        row
                        for row in self.state_reader.get_positions()
                        if str(row.get("symbol") or "").upper() == symbol
                    ),
                    {},
                )
                critical = True
            direction = self._direction_from_futures_position(
                position_amt,
                str(raw_position.get("positionSide", "BOTH")),
            )
            exchange_metric_updates: dict[str, float] = {
                "exchange_pnl_usd": _float_or_zero(raw_position.get("unRealizedProfit"))
            }
            exchange_mark_price = _float_or_zero(raw_position.get("markPrice"))
            if exchange_mark_price > 0.0:
                exchange_metric_updates["perp_live"] = exchange_mark_price
            self.state_writer.update_position_metrics(symbol, **exchange_metric_updates)
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
        open_order_symbols = self._bot_open_order_symbols(snapshot)
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
        account_report = self._publish_account_reconciliation(
            snapshot,
            generated_at=sample_time,
        )
        if not account_report.ready:
            critical = True
        economic_ledger_reconciled = False
        if account_report.ready and statement_history_ready:
            try:
                economic_ledger_reconciled = self._reconcile_economic_ledger_snapshot(snapshot)
            except Exception:
                logger.exception("Periodic economic-ledger reconciliation failed")
        self._set_safe_mode_flag(
            "economic_ledger_reconciliation",
            not economic_ledger_reconciled,
        )
        if not economic_ledger_reconciled:
            critical = True
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
            self._loop_heartbeats["maintenance_loop"] = time.monotonic()
            funding_status = self.funding_ranker.status_snapshot()
            now = datetime.now(timezone.utc)
            now_monotonic = time.monotonic()
            sample_minute = now.replace(second=0, microsecond=0).isoformat()
            if (
                self._trading_mode != "paper"
                and self._preflight_status == "passed"
                and (
                    not self._config_hash_consensus
                    or self._rust_config_version_hash != self._config.version_hash
                )
            ):
                self._dispatch_config_sync()
            self._expire_stale_pending_intents()
            await self._self_heal_pending_intents()
            # Auto-clear stale exit_failure when there is nothing left to exit.
            # The flag is set when an exit dispatch fails, but if the position
            # has since been removed (e.g. by audit reconciliation) and there
            # are no pending exit intents, the flag is orphaned and blocks
            # the entire portfolio indefinitely.
            if (
                "exit_failure" in self._safe_mode_flags
                and not self.state_reader.get_positions()
                and not self._pending_exit_intents
                and not self._stale_pending_exits
            ):
                logger.warning(
                    "Auto-clearing stale exit_failure safe-mode flag: "
                    "no positions and no pending exits remain"
                )
                self._set_safe_mode_flag("exit_failure", False)
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
                self._preflight_status == "passed" and not self._telemetry_stream_healthy(),
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
            stored_retention_date = self.state_reader.get_risk().get("last_retention_run_date")
            if current_date != stored_retention_date:
                archive_counts, db_stats = await asyncio.to_thread(
                    self._run_retention_maintenance_once
                )
                self.state_writer.set_risk_snapshot(
                    {
                        "last_retention_run_at": now.isoformat(),
                        "last_retention_run_date": current_date,
                        "last_retention_result": archive_counts,
                        "db_stats": db_stats,
                    }
                )
                self.state_writer.flush()
                self._last_retention_run_date = current_date

            self._maybe_record_validation_snapshot(now)
            self._persist_runtime_state()
            if await self._sleep_or_shutdown(5.0):
                break

    def _run_retention_maintenance_once(self) -> tuple[dict, dict]:
        retention_writer = StateWriter()
        retention_reader = StateReader()
        try:
            archive_counts = retention_writer.archive_old_data(
                retention_days=int(self._config.get("data_retention_days")),
                market_retention_days=int(self._config.get("market_sample_retention_days")),
                health_retention_days=int(self._config.get("health_sample_retention_days")),
                snapshot_retention_days=int(self._config.get("snapshot_retention_days")),
                feature_retention_days=int(self._config.get("feature_retention_days")),
            )
            # Keep the checkpoint work off the event loop too; this can still touch disk
            # heavily on large WAL files even without a full VACUUM.
            retention_writer.maintenance(run_vacuum=False)
            db_stats = retention_reader.get_db_stats()
            return archive_counts, db_stats
        finally:
            retention_reader.close()
            retention_writer.close()

    async def _run_execution_event_writer(self) -> None:
        """Background worker to drain the execution event queue and persist to DB."""
        while True:
            try:
                self._loop_heartbeats["execution_event_writer"] = time.monotonic()
                # Batch up to 10 events or wait for a small timeout
                events = []
                try:
                    event = await asyncio.wait_for(self._execution_event_queue.get(), timeout=1.0)
                    events.append(event)
                    while len(events) < 10:
                        try:
                            event = self._execution_event_queue.get_nowait()
                            events.append(event)
                        except asyncio.QueueEmpty:
                            break
                except (asyncio.TimeoutError, asyncio.QueueEmpty):
                    pass

                if events:
                    for payload in events:
                        self.state_writer.record_execution_event(payload)
                        self._execution_event_queue.task_done()
                    # Batch flush for efficiency
                    self.state_writer.flush()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in execution_event_writer: %s", e)
                await asyncio.sleep(1.0)

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
                # Clear bridge-related blocker if we're now healthy
                if self._blocked_reason == "execution bridge unavailable for exits":
                    self._set_blocked_reason("")
                self._set_safe_mode_flag("heartbeat_bridge", False)

            miss_threshold = max(1, int(self._config.get("heartbeat_miss_threshold")))
            if self._heartbeat_misses >= miss_threshold:
                self._set_safe_mode_flag("heartbeat_bridge", True)
                if (
                    not sent
                    and not self._telemetry_stream_healthy()
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
            self.capital_reservations.close()
        except Exception:
            pass
        try:
            self.feed_cursors.close()
        except Exception:
            pass
        try:
            self.cooldowns.close()
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

    @staticmethod
    def _combined_exchange_balances(snapshot: dict) -> dict[str, str]:
        """Return exact combined wallet/inventory balances by asset.

        The dedicated-account invariant lets spot, futures and margin wallets
        be treated as one reconciliation scope.  Values stay as ``Decimal``
        strings so an exchange precision unit is never lost through float math.
        """

        balances: dict[str, Decimal] = {}

        def add(asset: object, *values: object) -> None:
            asset_name = str(asset or "").strip().upper()
            if not asset_name:
                return
            total = Decimal("0")
            for value in values:
                try:
                    parsed = Decimal(str(value or "0"))
                except (InvalidOperation, TypeError, ValueError):
                    continue
                if parsed.is_finite():
                    total += parsed
            balances[asset_name] = balances.get(asset_name, Decimal("0")) + total

        futures_account = snapshot.get("futures_account") or {}
        futures_assets = futures_account.get("assets") or []
        if futures_assets:
            for row in futures_assets:
                if isinstance(row, dict):
                    add(row.get("asset"), row.get("walletBalance"))
        elif futures_account:
            add("USDT", futures_account.get("totalWalletBalance"))

        spot_account = snapshot.get("spot_account") or {}
        for row in spot_account.get("balances") or []:
            if isinstance(row, dict):
                add(row.get("asset"), row.get("free"), row.get("locked"))

        margin_account = snapshot.get("margin_account") or {}
        for row in margin_account.get("userAssets") or []:
            if isinstance(row, dict):
                add(row.get("asset"), row.get("netAsset"))

        return {
            asset: format(value, "f")
            for asset, value in sorted(balances.items())
            if value != 0
        }

    def _ingest_exchange_statement_snapshot(self, snapshot: dict) -> bool:
        """Journal authoritative income/interest rows without double counting.

        Funding, transfers, and borrow interest create ledger cashflows.
        Commission and realized-PnL rows remain ``MATCH_REQUIRED`` evidence
        because their incremental economics are already sourced from fills.
        Unknown statement types fail completeness closed.
        """

        account_id = os.getenv("BINANCE_ACCOUNT_ID", "binance-default")
        snapshot_errors = dict(snapshot.get("snapshot_errors") or {})
        authoritative_shape = any(
            key in snapshot
            for key in (
                "futures_income",
                "margin_interest",
                "statement_history_status",
            )
        )
        futures_rows = (
            snapshot.get("futures_income")
            if "futures_income" in snapshot
            else snapshot.get("funding_income")
        ) or []
        margin_rows = snapshot.get("margin_interest") or []
        margin_status = str(snapshot.get("margin_interest_status") or "").lower()

        futures_complete = (
            "futures_income" not in snapshot_errors
            and "funding_income" not in snapshot_errors
            and isinstance(futures_rows, list)
        )
        margin_complete = True
        if authoritative_shape:
            margin_complete = margin_status == "disabled" or (
                margin_status == "available"
                and "margin_interest" not in snapshot_errors
                and isinstance(margin_rows, list)
            )
        complete = futures_complete and margin_complete
        inserted = 0
        duplicates = 0
        ledger_events_inserted = 0
        status_counts = {MATCH_REQUIRED: 0, UNMAPPED: 0, "LEDGERED": 0}
        unmapped_types: set[str] = set()
        failures: list[str] = []

        for row in futures_rows if isinstance(futures_rows, list) else []:
            if not isinstance(row, dict):
                complete = False
                failures.append("futures_income_row_not_object")
                continue
            try:
                result = self.state_writer.record_binance_futures_income_statement(
                    row,
                    account_id=account_id,
                    trading_mode=self._trading_mode,
                    strategy_id="funding-arbitrage-v2",
                    runtime_mode=self._runtime_mode,
                    session_id=self._session_id,
                )
                inserted += int(result.inserted)
                duplicates += int(result.duplicate)
                ledger_events_inserted += result.ledger_result.inserted
                status_counts[result.reconciliation_status] = (
                    status_counts.get(result.reconciliation_status, 0) + 1
                )
                if result.reconciliation_status == UNMAPPED:
                    complete = False
                    unmapped_types.add(str(row.get("incomeType") or "UNKNOWN").upper())
            except Exception as exc:
                complete = False
                failures.append(
                    f"futures:{row.get('incomeType')}:{row.get('tranId')}:{type(exc).__name__}"
                )
                logger.exception(
                    "Futures statement ingestion failed for incomeType=%s tranId=%s",
                    row.get("incomeType"),
                    row.get("tranId"),
                )

        for row in margin_rows if isinstance(margin_rows, list) else []:
            if not isinstance(row, dict):
                complete = False
                failures.append("margin_interest_row_not_object")
                continue
            try:
                result = self.state_writer.record_binance_margin_interest_statement(
                    row,
                    account_id=account_id,
                    trading_mode=self._trading_mode,
                    strategy_id="funding-arbitrage-v2",
                    runtime_mode=self._runtime_mode,
                    session_id=self._session_id,
                )
                inserted += int(result.inserted)
                duplicates += int(result.duplicate)
                ledger_events_inserted += result.ledger_result.inserted
                status_counts[result.reconciliation_status] = (
                    status_counts.get(result.reconciliation_status, 0) + 1
                )
            except Exception as exc:
                complete = False
                failures.append(
                    f"margin:{row.get('txId')}:{type(exc).__name__}"
                )
                logger.exception(
                    "Margin-interest statement ingestion failed for txId=%s",
                    row.get("txId"),
                )

        now_iso = datetime.now(timezone.utc).isoformat()
        self.state_writer.set_risk_snapshot(
            {
                "exchange_statement_ingestion_ready": complete,
                "exchange_statement_last_ingested_at": now_iso,
                "exchange_statement_inserted_count": inserted,
                "exchange_statement_duplicate_count": duplicates,
                "exchange_statement_ledger_event_count": ledger_events_inserted,
                "exchange_statement_ledgered_count": status_counts.get("LEDGERED", 0),
                "exchange_statement_match_required_count": status_counts.get(
                    MATCH_REQUIRED, 0
                ),
                "exchange_statement_unmapped_count": status_counts.get(UNMAPPED, 0),
                "exchange_statement_unmapped_types": sorted(unmapped_types),
                "exchange_statement_failures": failures[:50],
                "exchange_statement_history_status": dict(
                    snapshot.get("statement_history_status") or {}
                ),
                "economic_ledger_ingestion_healthy": complete,
            }
        )
        if self._trading_mode != "paper":
            self._set_safe_mode_flag("exchange_statement_ingestion", not complete)
        return complete

    def _ingest_startup_funding_rows(self, rows: list[dict]) -> bool:
        """Compatibility wrapper for callers that only have funding rows."""

        return self._ingest_exchange_statement_snapshot({"funding_income": rows})

    def _reconcile_economic_ledger_snapshot(self, snapshot: dict) -> bool:
        account_id = os.getenv("BINANCE_ACCOUNT_ID", "binance-default")
        exchange_balances = self._combined_exchange_balances(snapshot)
        risk = self.state_reader.get_risk()
        anchor = risk.get("economic_ledger_anchor")
        if not isinstance(anchor, dict) or (
            str(anchor.get("account_id") or "") != account_id
            or str(anchor.get("trading_mode") or "") != self._trading_mode
        ):
            anchor = {
                "account_id": account_id,
                "trading_mode": self._trading_mode,
                "start_time": datetime.now(timezone.utc).isoformat(),
                "opening_balances": exchange_balances,
            }
            self.state_writer.set_risk_snapshot({"economic_ledger_anchor": anchor})

        tolerances = {
            asset: ("0.01" if asset in {"USDT", "USDC", "FDUSD", "BUSD", "USD"} else "0.00000001")
            for asset in set(exchange_balances)
            | set(dict(anchor.get("opening_balances") or {}))
        }
        result = self.state_reader.reconcile_economic_ledger(
            exchange_balances=exchange_balances,
            opening_balances=dict(anchor.get("opening_balances") or {}),
            tolerances=tolerances,
            account_id=account_id,
            trading_mode=self._trading_mode,
            venue="BINANCE",
            start_time=str(anchor.get("start_time") or ""),
        )
        self.state_writer.set_risk_snapshot(
            {
                "economic_ledger_reconciled": result.matched,
                "economic_ledger_reconciled_at": datetime.now(timezone.utc).isoformat(),
                "economic_ledger_unexplained_assets": list(result.unexplained_assets),
                "economic_ledger_differences": {
                    key: str(value) for key, value in result.differences.items()
                },
                "economic_ledger_event_count": result.projection.event_count,
            }
        )
        if not result.matched:
            logger.critical(
                "Economic ledger does not reconcile for assets: %s",
                ", ".join(result.unexplained_assets),
            )
        return result.matched

    async def _reconcile_live_startup_state(self) -> None:
        snapshot = await self._fetch_exchange_startup_snapshot()
        snapshot = await self._clear_startup_open_orders(snapshot, stage="Live startup reconciliation")
        initial_ownership_report = self._build_account_reconciliation_report(snapshot)
        exchange_only_at_startup = set(initial_ownership_report.exchange_only_symbols)
        mismatched_at_startup = set(initial_ownership_report.mismatched_symbols)

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
        spot_free_usd = self._derive_spot_free_balance_usd(
            snapshot.get("spot_account"),
            snapshot,
        )
        self._latest_exchange_spot_cash_available = spot_free_usd
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
            entry_price = _float_or_zero(raw_position.get("entryPrice"))
            if entry_price <= 0.0:
                entry_price = _float_or_zero(raw_position.get("breakEvenPrice"))
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
                    # Spot API unavailable at startup - cannot verify hedge right now.
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
            perp_entry_price = entry_price
            if perp_entry_price <= 0.0:
                perp_entry_price = (
                    _float_or_zero(local_position.get("perp_entry")) if local_position is not None else 0.0
                )
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
            if symbol in exchange_only_at_startup:
                recovery_state = "manual_review"
                recovery_note = (
                    f"{symbol} exists only on the exchange and has no durable bot ownership lineage; "
                    "adopted for visibility and risk-reducing repair only"
                )
            elif symbol in mismatched_at_startup:
                recovery_state = "manual_review"
                recovery_note = (
                    f"{symbol} exchange quantity/direction did not match durable local state; "
                    "exchange truth was adopted pending ownership review"
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

        futures_account_equity = _derive_futures_account_balance(
            futures_account,
            preferred_fields=("totalMarginBalance", "totalWalletBalance"),
            asset_field_name="marginBalance",
        )
        spot_account_equity = self._derive_spot_account_balance_usd(
            spot_balances,
            snapshot,
        )
        account_equity = (
            futures_account_equity + spot_account_equity
            if futures_account_equity > 0.0
            else futures_account_equity
        )
        available_balance = _derive_futures_account_balance(
            futures_account,
            preferred_fields=("availableBalance",),
            asset_field_name="availableBalance",
        )
        last_funding_fee = 0.0
        last_funding_fee_time = ""
        funding_ledger_complete = self._ingest_exchange_statement_snapshot(snapshot)
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
        exchange_equity_snapshot["exchange_spot_cash_available_usd"] = spot_free_usd
        account_reconciliation = self._publish_account_reconciliation(snapshot)
        economic_ledger_reconciled = False
        if account_reconciliation.ready and funding_ledger_complete:
            try:
                economic_ledger_reconciled = self._reconcile_economic_ledger_snapshot(
                    snapshot
                )
            except Exception:
                logger.exception("Economic-ledger startup reconciliation failed")
        self.state_writer.set_risk_snapshot(
            {
                "economic_ledger_reconciled": economic_ledger_reconciled,
                "economic_ledger_funding_history_complete": funding_ledger_complete,
                "economic_ledger_statement_history_complete": funding_ledger_complete,
            }
        )
        self._set_safe_mode_flag(
            "economic_ledger_reconciliation",
            not economic_ledger_reconciled,
        )
        if not account_reconciliation.ready:
            startup_snapshot["startup_reconciliation_status"] = "needs_review"

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
                "allow_new_risk": account_reconciliation.ready,
                **exchange_equity_snapshot,
            }
        )
        unresolved_divergence_symbols = (
            set(mismatched_symbols)
            | set(hedge_gap_symbols)
            | set(unsupported_direction_symbols)
            | set(self._startup_manual_review_symbols)
        )
        self._clear_reconciled_position_divergence_blocks(
            unresolved_divergence_symbols
        )
        if hedge_gap_symbols or "hedge_gap" in self._safe_mode_flags:
            self._set_safe_mode_flag("hedge_gap", bool(hedge_gap_symbols))
        self.state_writer.flush()
        recovery_actions = startup_snapshot.get("startup_reconciliation_recovery_actions", {})
        review_item_count = len(recovery_actions) if isinstance(recovery_actions, dict) else 0
        logger.info(
            "Live startup reconciliation complete: %d exchange positions, %d stale local rows removed, %d mismatches, %d review items",
            len(reconciled_symbols),
            len(local_only_symbols),
            len(mismatched_symbols),
            review_item_count,
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

        # -- Autonomous recovery readiness -----------------------------------
        # ``pause_new_entries`` is an operator control.  Recovery may clear its
        # own incident/readiness blocks after proof, but it must never erase an
        # explicit operator pause.
        if bool(self._config.get("autonomous_startup_recovery")):
            if not self._startup_manual_review_symbols and not self._active_global_safe_mode_flags():
                if bool(self._config.get("pause_new_entries")):
                    logger.info(
                        "Autonomous startup recovery is clean; preserving the explicit "
                        "operator pause on new entries."
                    )
                else:
                    logger.info("Autonomous startup recovery: system is clean and already unpaused.")
            elif self._startup_manual_review_symbols:
                logger.info(
                    "Autonomous startup recovery: staying paused due to remaining manual review items: %s",
                    ", ".join(sorted(self._startup_manual_review_symbols))
                )

    def _sync_position_to_execution_engine(self, row: dict) -> bool:
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
        Reconstruct durable state before the decision loop starts.

        Paper is a persistent simulated exchange, not a disposable demo.  Its
        positions and in-flight intents survive process restarts and the
        durable command outbox is replayed after telemetry reconnects.  Live
        and testnet additionally reconcile against signed exchange truth.
        """
        import requests
        from datetime import datetime, timezone
        
        logger.info("="*50)
        logger.info("STARTUP MODE: %s", self._trading_mode.upper())
        logger.info("="*50)
        
        if self._trading_mode == "paper":
            logger.info("PAPER MODE: Restoring durable simulated exchange state...")
            self._pending_enters.clear()
            self._stale_pending_enters.clear()
            self._abandoned_pending_enters.clear()
            self._abandoned_exit_intents.clear()
            self._pending_exit_intents.clear()
            self._pending_exit_created_at.clear()
            self._exit_events.clear()
            positions = self.state_reader.get_positions()
            for pos in positions:
                symbol = str(pos.get("symbol") or "").upper()
                if not symbol:
                    continue
                self._position_directions[symbol] = str(
                    pos.get("direction") or "long"
                ).lower()
                restored_at = str(pos.get("updated_at") or "")
                if restored_at:
                    self._entry_times[symbol] = restored_at

            pending_rows = self.state_reader.get_pending_intents(limit=500)
            for row in pending_rows:
                symbol = str(row.get("symbol") or "").upper()
                intent_id = str(row.get("intent_id") or "")
                intent_type = str(row.get("intent_type") or "").upper()
                status = str(row.get("status") or "").upper()
                metadata = dict(row.get("metadata") or {})
                if not symbol or not intent_id:
                    continue
                if intent_type.startswith("ENTER"):
                    entry = {
                        **metadata,
                        "intent_id": intent_id,
                        "entry_time": str(
                            metadata.get("entry_time") or row.get("created_at") or ""
                        ),
                        "entry_price": _float_or_zero(
                            metadata.get("entry_price")
                        ),
                        "qty": _float_or_zero(
                            metadata.get("qty") or row.get("quantity")
                        ),
                        "direction": str(
                            metadata.get("direction") or row.get("direction") or "long"
                        ).lower(),
                    }
                    if status == "TIMEOUT":
                        entry["timed_out_at"] = str(row.get("updated_at") or "")
                        self._stale_pending_enters[symbol] = entry
                    elif status not in {"FILLED", "REJECTED", "CANCELLED", "CANCELED", "FAILED"}:
                        self._pending_enters[symbol] = entry
                elif intent_type.startswith("EXIT") and status not in {
                    "FILLED",
                    "REJECTED",
                    "CANCELLED",
                    "CANCELED",
                    "FAILED",
                }:
                    self._pending_exit_intents[symbol] = intent_id
                    self._pending_exit_created_at[symbol] = str(
                        row.get("created_at") or row.get("updated_at") or ""
                    )
                    self._exit_events[symbol] = asyncio.Event()
                    if status == "TIMEOUT":
                        self._stale_pending_exits.add(symbol)

            synced_count = self._sync_positions_to_execution_engine(positions)
            logger.info(
                "Paper startup restored %d position(s), %d pending enter(s), "
                "%d pending exit(s); %d position(s) synced to Rust tracking",
                len(positions),
                len(self._pending_enters) + len(self._stale_pending_enters),
                len(self._pending_exit_intents),
                synced_count,
            )
            self.state_writer.set_risk_snapshot(
                {
                    "startup_reconciliation_status": "paper_restored",
                    "startup_reconciliation_time": datetime.now(timezone.utc).isoformat(),
                    "startup_reconciliation_position_count": len(positions),
                    "startup_reconciliation_pending_intent_count": len(pending_rows),
                    "startup_reconciliation_spot_hedge_gaps": [],
                    "startup_reconciliation_mismatched_symbols": [],
                }
            )
                
        else:
            logger.info("%s MODE: Reconciling startup state against signed Binance account truth...", self._trading_mode.upper())
            reconciliation_succeeded = False
            try:
                await self._reconcile_live_startup_state()
                reconciliation_succeeded = True
                self._set_safe_mode_flag("startup_reconciliation_failed", False)
            except Exception as exc:
                logger.critical(
                    "Startup reconciliation failed; persisted positions remain observation-only: %s",
                    exc,
                    exc_info=True,
                )
                self._account_reconciliation_ready = False
                self._set_safe_mode_flag("account_reconciliation", True)
                self._set_safe_mode_flag("startup_reconciliation_failed", True)
                self.state_writer.set_risk_snapshot(
                    {
                        "startup_reconciliation_status": "failed",
                        "startup_reconciliation_time": datetime.now(timezone.utc).isoformat(),
                        "startup_reconciliation_error": str(exc)[:300],
                        "account_reconciliation_ready": False,
                        "allow_new_risk": False,
                    }
                )
                self.state_writer.flush()
            current_positions = self.state_reader.get_positions()
            synced_count = (
                self._sync_positions_to_execution_engine(current_positions)
                if reconciliation_succeeded
                else 0
            )
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
            self._set_safe_mode_flag("hedge_gap", bool(hedge_gaps))
            if hedge_gaps:
                logger.critical(
                    "Spot hedge gap detected for %s - perp open but spot inventory low. "
                    "New risk remains blocked until account reconciliation succeeds.",
                    ", ".join(sorted(hedge_gaps)),
                )
                self.state_writer.set_risk_snapshot({"hedge_gap_symbols": sorted(hedge_gaps)})
            else:
                self.state_writer.set_risk_snapshot({"hedge_gap_symbols": []})
            self._set_safe_mode_flag(
                "startup_mismatch",
                reconciliation_succeeded and not self._account_reconciliation_ready,
            )
        
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
        self._retire_untradable_depth_gap_blocks(tradable_symbols)
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

    def _minutes_since_last_snapshot(self, symbol: str | None = None) -> float:
        """Return minutes elapsed since the symbol's most recent settlement."""
        if symbol:
            return self.funding_ranker.minutes_since_last_settlement(symbol)
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
                all_funding_rows_ledgered = True
                if isinstance(income_rows, list):
                    for row in income_rows:
                        if str(row.get("symbol", "")).upper() != symbol.upper():
                            continue
                        income_value = _float_or_zero(row.get("income"))
                        total_funding += income_value
                        event_time = _iso_from_ms(row.get("time"))
                        tran_id = row.get("tranId")
                        execution_payload = {
                            "event_name": "FundingFee",
                            "symbol": symbol,
                            "client_order_id": f"funding_{tran_id or row.get('time', 'unknown')}",
                            "status": "SETTLED",
                            "asset": row.get("asset", "USDT"),
                            "amount": income_value,
                            "reason": row.get("incomeType", "FUNDING_FEE"),
                            "event_time": event_time,
                            "account_id": os.getenv(
                                "BINANCE_ACCOUNT_ID", "binance-default"
                            ),
                            "environment": self._trading_mode,
                            "strategy_id": "funding-arbitrage-v2",
                            "raw_income_row": row,
                        }
                        try:
                            if income_value != 0.0 and tran_id is not None:
                                source_id = (
                                    f"binance:{execution_payload['account_id']}:funding:"
                                    f"{tran_id}"
                                )
                                self.state_writer.record_execution_and_economic_funding(
                                    execution_payload,
                                    {
                                        "account_id": execution_payload["account_id"],
                                        "trading_mode": self._trading_mode,
                                        "venue": "BINANCE",
                                        "strategy_id": "funding-arbitrage-v2",
                                        "event_time": event_time,
                                        "asset": str(row.get("asset") or "USDT"),
                                        "amount": str(row.get("income")),
                                        "exchange_event_id": source_id,
                                        "source_event_id": source_id,
                                        "symbol": symbol.upper(),
                                        "instrument_type": "PERPETUAL",
                                        "runtime_mode": self._runtime_mode,
                                        "session_id": self._session_id,
                                        "metadata": {
                                            "income_type": row.get(
                                                "incomeType", "FUNDING_FEE"
                                            )
                                        },
                                        "raw_payload": row,
                                    },
                                )
                            else:
                                self.state_writer.record_execution_event(execution_payload)
                                if income_value != 0.0 and tran_id is None:
                                    all_funding_rows_ledgered = False
                                    self.state_writer.set_risk_snapshot(
                                        {
                                            "economic_ledger_reconciled": False,
                                            "economic_ledger_ingestion_healthy": False,
                                            "economic_ledger_lineage_error": (
                                                "funding_missing_tran_id"
                                            ),
                                            "allow_new_risk": False,
                                        }
                                    )
                        except Exception:
                            all_funding_rows_ledgered = False
                            logger.exception(
                                "Could not dual-write funding fee for %s tranId=%s",
                                symbol,
                                tran_id,
                            )
                if isinstance(income_rows, list):
                    return (
                        total_funding,
                        "actual_rest"
                        if income_rows and all_funding_rows_ledgered
                        else "actual_rest_zero"
                        if not income_rows
                        else "actual_rest_unledgered",
                    )
            except Exception as exc:
                logger.warning(
                    "Could not reconcile actual funding fees for %s between %s and %s: %s",
                    symbol,
                    entry_time,
                    exit_time,
                    exc,
                )

        if self._trading_mode == "paper":
            synthetic_funding = self._synthetic_funding_collected_usd(
                qty=qty,
                direction=direction,
                ann_funding=ann_funding,
                hold_hours=hold_hours,
                funding_periods=funding_periods,
                spot_entry_price=spot_entry_price,
                perp_entry_price=perp_entry_price,
            )
            return synthetic_funding, "modeled_paper"

        # Exchange cash flow is never estimated in a non-paper environment.
        # The caller persists known components with INCOMPLETE status so
        # reconciliation can repair the attribution later without fabricating
        # realized performance.
        return 0.0, "missing_actual"

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
        # If execution_type is TRADE and we don't have individual leg prices, 
        # it might be a single-leg fill from a simpler execution engine or paper mode.
        if execution_label == "TRADE" and (spot_fill_price is None and perp_fill_price is None):
            return False
        return spot_fill_price is not None and perp_fill_price is not None

    def _cycle_completion_matches_pending_intent(
        self,
        symbol: str,
        *,
        intent_id: str | None,
        spot_fill_price,
        perp_fill_price,
    ) -> bool:
        """Reject aggregate completion when a required leg has no fill proof.

        Rust may intentionally complete a single-leg recovery exit, but that is
        only valid when the durable intent explicitly skipped the absent leg.
        A zero fill price for an implicitly skipped leg must never release the
        Python position projection.
        """

        durable_intent_id = str(
            intent_id
            or self._pending_exit_intents.get(symbol)
            or (self._pending_enters.get(symbol) or {}).get("intent_id")
            or ""
        ).strip()
        if not durable_intent_id:
            return True

        pending = next(
            (
                row
                for row in self.state_reader.get_pending_intents(limit=1_000)
                if str(row.get("intent_id") or "") == durable_intent_id
            ),
            None,
        )
        if pending is None:
            # Older/manual test paths can predate durable leg metadata. Account
            # reconciliation remains the authority for those legacy events.
            return True

        metadata = pending.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        intent_type = str(pending.get("intent_type") or "").upper()
        is_exit = intent_type.startswith("EXIT")
        requires_spot = not (is_exit and bool(metadata.get("skip_spot_leg")))
        requires_perp = not (is_exit and bool(metadata.get("skip_perp_leg")))
        spot_proven = _float_or_zero(spot_fill_price) > 0.0
        perp_proven = _float_or_zero(perp_fill_price) > 0.0
        missing_legs = []
        if requires_spot and not spot_proven:
            missing_legs.append("spot")
        if requires_perp and not perp_proven:
            missing_legs.append("perp")
        if not missing_legs:
            return True

        issue = {
            "symbol": symbol,
            "intent_id": durable_intent_id,
            "missing_legs": missing_legs,
            "observed_spot_fill_price": spot_fill_price,
            "observed_perp_fill_price": perp_fill_price,
        }
        logger.critical(
            "Refusing to finalize %s cycle %s: required fill proof missing for %s",
            symbol,
            durable_intent_id,
            ", ".join(missing_legs),
        )
        self.state_writer.set_risk_snapshot(
            {
                "execution_reconciliation_required": True,
                "execution_reconciliation_issue": issue,
                "allow_new_risk": False,
            }
        )
        self.state_writer.flush()
        self._set_safe_mode_flag("execution_reconciliation", True)
        return False

    def _next_intent_id(self, symbol: str, intent_type: str) -> str:
        return self.state_writer.next_execution_intent_id(
            producer_id="live-trader-v2",
            symbol=symbol,
            intent_type=intent_type,
        )

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
        entry_price = _float_or_zero(raw_position.get("entryPrice"))
        if entry_price <= 0.0:
            entry_price = _float_or_zero(raw_position.get("breakEvenPrice"))
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
        perp_entry = entry_price
        if perp_entry <= 0.0:
            perp_entry = _float_or_zero((entry_context or {}).get("perp_entry"))
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
        # hedge_gap is a warning, not a trading halt - see _reconcile_live_startup_state.
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
            open_order_symbols = set(
                self._open_order_symbols(self._snapshot_open_orders(snapshot))
            )
            if symbol not in open_order_symbols:
                self._release_entry_reservation(
                    entry,
                    reason=f"entry_{terminal_status.lower()}_exchange_flat",
                    exchange_terminal_proven=True,
                )
            else:
                reservation_id = str(entry.get("reservation_id") or "")
                if reservation_id:
                    self.capital_reservations.mark_delivery_unknown(
                        reservation_id,
                        evidence={
                            "symbol": symbol,
                            "terminal_status": terminal_status,
                            "reason": "exchange_order_still_open",
                        },
                    )
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
        self._release_entry_reservation(
            entry,
            reason="entry_failure_position_recovered",
            exchange_terminal_proven=True,
        )

    def _schedule_background_coroutine(
        self,
        coroutine,
        *,
        name: str,
    ) -> asyncio.Task | None:
        """Schedule an owned coroutine, or close it if no loop is running."""

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coroutine.close()
            logger.warning("Could not schedule %s because no event loop is running", name)
            return None
        task = loop.create_task(coroutine, name=name)
        self._background_tasks.append(task)
        return task

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

        task = self._schedule_background_coroutine(
            self._recover_failed_entry_from_exchange(
                symbol=symbol,
                entry=dict(entry),
                terminal_status=terminal_status,
                execution_type=execution_type,
                client_order_id=client_order_id,
            ),
            name=f"entry_failure_recovery:{symbol}",
        )
        if task is None:
            return
        self._entry_failure_recovery_tasks[symbol] = task

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
        open_order_symbols = self._bot_open_order_symbols(snapshot)
        all_open_order_symbols = set(
            self._open_order_symbols(self._snapshot_open_orders(snapshot))
        )
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
                        "Max cancel attempts (%d) reached for stale ENTER on %s; "
                        "abandoning intent and placing symbol on cooldown",
                        _STALE_ENTER_MAX_CANCEL_ATTEMPTS,
                        symbol,
                    )
                    self._stale_enter_cancel_attempts.pop(symbol, None)
                    self._stale_pending_enters.pop(symbol, None)
                    self.state_writer.update_pending_intent(
                        intent_id,
                        status="CANCELED",
                        last_error="stale_enter_max_cancel_attempts",
                    )
                    self._activate_stale_intent_cooldown(
                        symbol,
                        reason="stale_pending_intent_max_cancel",
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
                self._activate_stale_intent_cooldown(
                    symbol,
                    reason="stale_pending_intent_cancel",
                )
                logger.warning(
                    "Stale ENTER for %s: cancelled open order and gave up after %d attempt(s) "
                    "(portfolio allocator will ignore symbol due to cooldown)",
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

            if symbol in all_open_order_symbols:
                # An order exists, but its client ID is outside (or missing
                # from) the bot namespace.  Symbol/side/quantity are not proof
                # of ownership.  Preserve the stale intent and block readiness
                # until account reconciliation or an operator classifies it.
                self._account_reconciliation_ready = False
                self._set_safe_mode_flag("account_reconciliation", True)
                self.state_writer.set_risk_snapshot(
                    {
                        "account_reconciliation_ready": False,
                        "allow_new_risk": False,
                        "stale_pending_unrelated_order_symbols": sorted(
                            set(
                                self.state_reader.get_risk().get(
                                    "stale_pending_unrelated_order_symbols", []
                                )
                                or []
                            )
                            | {symbol}
                        ),
                    }
                )
                logger.error(
                    "Stale ENTER for %s overlaps an unrelated/unknown exchange order; "
                    "leaving both untouched and retaining the pending intent",
                    symbol,
                )
                continue

            self._stale_pending_enters.pop(symbol, None)
            self._resolve_pending_intent(intent_id)
            self._activate_stale_intent_cooldown(
                symbol,
                reason="stale_pending_intent_no_activity",
            )
            logger.warning(
                "Auto-cleared stale ENTER for %s because exchange shows no open order or position; symbol on cooldown",
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
                        "falling back to pure taker market order",
                        symbol,
                        _STALE_EXIT_MAX_RESUBMIT_ATTEMPTS,
                    )
                    self._stale_exit_resubmit_attempts[symbol] = 0
                    self.state_writer.update_pending_intent(
                        intent_id,
                        status="CANCELED",
                        last_error="stale_exit_max_resubmit_fallback_to_taker",
                    )
                    self._pending_exit_intents.pop(symbol, None)
                    self._pending_exit_created_at.pop(symbol, None)
                    self._stale_pending_exits.discard(symbol)
                    direction = self._position_directions.get(symbol, "long")
                    self._dispatch_exit(symbol, urgency=10.0, direction=direction)
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
                # Cancel succeeded - clear the stale intent and resubmit a fresh exit.
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
                # Position exists but no open order - order was likely silently dropped or rejected without intent update.
                self._stale_exit_resubmit_attempts[symbol] = self._stale_exit_resubmit_attempts.get(symbol, 0) + 1
                self.state_writer.update_pending_intent(
                    intent_id,
                    status="CANCELED",
                    last_error="stale_exit_no_open_order_resubmit",
                )
                self._pending_exit_intents.pop(symbol, None)
                self._pending_exit_created_at.pop(symbol, None)
                self._stale_pending_exits.discard(symbol)
                direction = self._position_directions.get(symbol, "long")
                self._dispatch_exit(symbol, urgency=1.0, direction=direction)
                logger.warning(
                    "Stale EXIT for %s: position exists but no open order found; assuming silently dropped and resubmitting",
                    symbol,
                )
                self._record_pending_intent_self_heal(
                    symbol=symbol,
                    intent_type="EXIT",
                    reason="stale_exit_no_open_order_resubmit",
                    sample_time=now.isoformat(),
                )
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
                    intent_id=intent_id,
                )
            else:
                self.state_writer.remove_position(symbol)
                self._entry_times.pop(symbol, None)
                self._position_directions.pop(symbol, None)
                self._estimated_entry_costs.pop(symbol, None)
            self._resolve_pending_intent(intent_id)
            self._activate_stale_intent_cooldown(
                symbol,
                reason="stale_pending_exit_reconciled",
            )
            logger.warning(
                "Auto-reconciled stale EXIT for %s because exchange is flat with no open order; symbol on cooldown",
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
        intent_id = str(entry.get("intent_id") or fill_kwargs.get("intent_id") or "")
        if not intent_id:
            raise RuntimeError(f"Entry fill for {symbol} has no durable intent_id")
        self.state_writer.project_entry_lifecycle(
            event_key=f"entry:{intent_id}",
            intent_id=intent_id,
            event_time=fill_time,
            position_fields={
                "symbol": symbol,
                "side": side_label,
                "spot_entry": spot_entry_price,
                "perp_entry": perp_entry_price,
                "qty": float(entry["qty"]),
                "ann_funding": _float_or_zero(entry.get("ann_funding")),
                "entry_ann_funding": _float_or_zero(entry.get("ann_funding")),
                "spot_live": spot_entry_price,
                "perp_live": perp_entry_price,
                "direction": direction,
                "status": "OPEN",
                "updated_at": fill_time,
            },
            evidence={
                "execution_type": fill_kwargs.get("execution_type"),
                "client_order_id": fill_kwargs.get("client_order_id"),
                "cycle_id": fill_kwargs.get("cycle_id"),
                "spot_fill_price": fill_kwargs.get("spot_fill_price"),
                "perp_fill_price": fill_kwargs.get("perp_fill_price"),
            },
        )
        # The open position now consumes the projected-gross budget, so the
        # transient pending-order reservation can be released with terminal
        # exchange proof without freeing unaccounted capital.
        self._release_entry_reservation(
            entry,
            reason="entry_filled_position_persisted",
            exchange_terminal_proven=True,
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
        if terminal_status == "RECONCILIATION_REQUIRED":
            reason = str(
                event_kwargs.get("execution_type") or "execution_state_ambiguous"
            )
            self._set_symbol_safe_mode_reason(
                symbol, "execution_reconciliation_required", True
            )
            if self._trading_mode != "paper":
                self._set_safe_mode_flag("execution_reconciliation", True)
            intent_id = str(event_kwargs.get("intent_id") or "")
            if not intent_id:
                pending = (
                    self._pending_enters.get(symbol)
                    or self._stale_pending_enters.get(symbol)
                    or {}
                )
                intent_id = str(
                    pending.get("intent_id")
                    or self._pending_exit_intents.get(symbol)
                    or ""
                )
            if intent_id:
                self.state_writer.update_pending_intent(
                    intent_id,
                    status="NEEDS_RECONCILIATION",
                    last_error=reason,
                    client_order_id=str(event_kwargs.get("client_order_id") or "")
                    or None,
                )
            self.state_writer.set_risk_snapshot(
                {
                    "execution_reconciliation_required": True,
                    "execution_reconciliation_symbol": symbol.upper(),
                    "execution_reconciliation_reason": reason,
                    "allow_new_risk": False,
                }
            )
            logger.critical(
                "Execution state for %s requires exchange reconciliation: %s",
                symbol,
                reason,
            )
            return
        if terminal_status not in {"REJECTED", "CANCELED", "CANCELLED", "EXPIRED", "FAILED"}:
            return

        client_order_id = str(event_kwargs.get("client_order_id") or "")
        failed_entry: dict | None = None
        if symbol in self._pending_enters:
            entry = self._pending_enters.pop(symbol)
            failed_entry = dict(entry)
            intent_id = str(entry.get("intent_id") or "")
            self.state_writer.update_pending_intent(
                intent_id,
                status=terminal_status,
                last_error=f"entry_{terminal_status.lower()}",
                client_order_id=client_order_id or None,
            )
            self._resolve_pending_intent(intent_id)
            logger.error("Entry for %s failed with status %s", symbol, terminal_status)

            # Fix B (4.2): Entry-rejection cooldown (stops the flap at the source)
            if terminal_status not in {"CANCELED", "CANCELLED", "EXPIRED"}:
                from bongus.core.config import (
                    ENTRY_REJECT_COOLDOWN_BASE_SECONDS,
                    ENTRY_REJECT_COOLDOWN_MAX_SECONDS,
                    ENTRY_REJECT_COOLDOWN_BACKOFF_WINDOW_SECONDS,
                    ENTRY_REJECT_COOLDOWN_BACKOFF_FACTOR,
                )
                now_ts = time.time()
                recent = [
                    t for t in self._recent_entry_rejects.get(symbol, [])
                    if now_ts - t < ENTRY_REJECT_COOLDOWN_BACKOFF_WINDOW_SECONDS
                ]
                n = len(recent)
                duration = min(
                    ENTRY_REJECT_COOLDOWN_BASE_SECONDS * (ENTRY_REJECT_COOLDOWN_BACKOFF_FACTOR ** n),
                    ENTRY_REJECT_COOLDOWN_MAX_SECONDS,
                )
                reason_code = str(event_kwargs.get("execution_type") or terminal_status).strip()
                self.cooldowns.activate_symbol(symbol, duration, f"entry_rejected:{reason_code}")
                recent.append(now_ts)
                self._recent_entry_rejects[symbol] = recent
                logger.warning(
                    "Entry cooldown armed for %s: %.0fs (recent=%d, reason=%s)",
                    symbol, duration, n + 1, reason_code
                )

        stale_entry = self._stale_pending_enters.pop(symbol, None)
        if stale_entry is not None:
            if failed_entry is None:
                failed_entry = dict(stale_entry)
            intent_id = str(stale_entry.get("intent_id") or "")
            self.state_writer.update_pending_intent(
                intent_id,
                status=terminal_status,
                last_error=f"entry_{terminal_status.lower()}",
                client_order_id=client_order_id or None,
            )
            self._resolve_pending_intent(intent_id)
        abandoned_entry = self._abandoned_pending_enters.pop(symbol, None)
        if abandoned_entry is not None:
            logger.warning(
                "Terminal update %s arrived for %s after a paper-mode ENTER intent was auto-cleared",
                terminal_status,
                symbol,
            )

        if failed_entry is not None:
            if self._trading_mode == "paper":
                self._release_entry_reservation(
                    failed_entry,
                    reason=f"paper_entry_{terminal_status.lower()}",
                    exchange_terminal_proven=True,
                )
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
            if self._is_startup_recovery_symbol(symbol):
                self._record_startup_recovery_exit_failure(symbol, failure_reason)
                logger.warning(
                    "Startup recovery exit for %s failed with status %s (%s); leaving the symbol blocked for startup-recovery backoff",
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
            self._activate_stale_intent_cooldown(
                symbol,
                reason="stale_pending_intent",
            )
            logger.error(
                "Pending ENTER for %s timed out after %.0fs; symbol on cooldown and remains blocked until reconciliation",
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
                self._activate_stale_intent_cooldown(
                    symbol,
                    reason="stale_pending_intent",
                )
                logger.critical(
                    "Pending EXIT for %s is older than %.0fs; symbol on cooldown and remains blocked until reconciliation",
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

    def _activate_stale_intent_cooldown(self, symbol: str, reason: str) -> None:
        from bongus.core.config import (
            STALE_INTENT_COOLDOWN_BASE_SECONDS,
            STALE_INTENT_COOLDOWN_MAX_SECONDS,
            STALE_INTENT_COOLDOWN_BACKOFF_FACTOR,
        )
        now_ts = time.time()
        # Use a 1 hour window to count recent stale intents for backoff
        window = 3600.0
        recent = [
            t for t in self._recent_stale_intents.get(symbol, [])
            if now_ts - t < window
        ]
        n = len(recent)
        duration = min(
            STALE_INTENT_COOLDOWN_BASE_SECONDS * (STALE_INTENT_COOLDOWN_BACKOFF_FACTOR ** n),
            STALE_INTENT_COOLDOWN_MAX_SECONDS,
        )
        self.cooldowns.activate_symbol(symbol, duration, reason)
        recent.append(now_ts)
        self._recent_stale_intents[symbol] = recent
        logger.warning(
            "Stale intent cooldown armed for %s: %.0fs (recent=%d, reason=%s)",
            symbol,
            duration,
            n + 1,
            reason,
        )

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
            max_drawdown_release_pct=float(
                self._config.get("max_drawdown_release_pct") or MAX_DRAWDOWN_RELEASE_PCT
            ),
            max_data_staleness_minutes=MAX_ALLOWED_GAP_MINUTES,
            max_latency_ms=int(self._config.get("max_venue_latency_ms", 400)) if self._trading_mode != "testnet" else max(1000, int(self._config.get("max_venue_latency_ms", 400))),
            max_consecutive_losses=max(1, int(self._config.get("loss_streak_trigger"))),
            venue_latency_debounce_s=float(
                self._config.get("venue_latency_debounce_s") or VENUE_LATENCY_DEBOUNCE_S
            ),
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

    def _position_excluded_from_managed_risk(self, row: dict) -> bool:
        symbol = str(row.get("symbol", "")).upper()
        if symbol in self._startup_recovery_stuck_symbols:
            return True
        return str(row.get("recovery_state") or "").strip().lower() == "manual_review"

    def _liquidity_adjusted_open_pnl(self, rows: list[dict]) -> tuple[float, float, float]:
        mark_to_market_open_pnl = 0.0
        liquidity_adjusted_open_pnl = 0.0
        total_exit_cost_usd = 0.0
        for row in rows:
            mark_pnl = _float_or_zero(row.get("net_pnl_usd"))
            mark_to_market_open_pnl += mark_pnl

            symbol = str(row.get("symbol", "")).upper()
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
        economic_statuses = (
            ["RECONCILED", "MODELED"]
            if self._trading_mode == "paper"
            else ["RECONCILED"]
        )
        realized_pnl = sum(
            _float_or_zero(trade.get("net_pnl_usd"))
            for trade in self.state_reader.get_trades(
                limit=5_000,
                session_scoped=False,
                economic_statuses=economic_statuses,
            )
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
        # Scale the penalty for misses more gradually.
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
        risk_managed_rows = [row for row in open_rows if not self._position_excluded_from_managed_risk(row)]
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
        (
            risk_mark_to_market_open_pnl,
            risk_input_open_pnl,
            risk_input_exit_cost_usd,
        ) = self._liquidity_adjusted_open_pnl(risk_managed_rows)
        account_equity = self._estimate_account_equity(
            rows,
            liquidity_exit_cost_usd=risk_input_exit_cost_usd,
            open_pnl_override=risk_input_open_pnl,
        )
        account_equity_mark_to_market = self._estimate_account_equity(
            rows,
            open_pnl_override=mark_to_market_open_pnl,
        )
        excluded_manual_review_mtm = mark_to_market_open_pnl - risk_mark_to_market_open_pnl
        self._maybe_auto_decay_equity_high_watermark(account_equity)
        if account_equity > self._peak_account_equity:
            self._peak_account_equity = account_equity
        drawdown_pct = (
            max(0.0, (self._peak_account_equity - account_equity) / self._peak_account_equity)
            if self._peak_account_equity > 0.0
            else 0.0
        )
        venue_latency_ms = self._heartbeat_implied_venue_latency_ms()
        stress_summary = self._stress_test_summary(
            risk_managed_rows,
            current_liquidity_adjusted_open_pnl=risk_input_open_pnl,
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
                previous_kill_switch=self._risk_kill_switch,
                liquidation_buffer_usd=self._latest_exchange_available_balance,
                minimum_liquidation_buffer_usd=float(
                    self._capital_reservation_policy().minimum_liquidation_buffer_usd
                ),
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
                "account_equity_mark_to_market": account_equity_mark_to_market,
                "account_equity_excludes_manual_review_usd": excluded_manual_review_mtm,
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
                "liquidation_buffer_usd": self._latest_exchange_available_balance,
                "minimum_liquidation_buffer_usd": float(
                    self._capital_reservation_policy().minimum_liquidation_buffer_usd
                ),
                "kill_switch": decision.kill_switch,
                "risk_reasons": decision.reasons,
                **stress_summary,
            }
        )
        self.state_writer.flush()
        self._set_safe_mode_flag("risk_limits", decision.derisk_required or decision.kill_switch)
        return decision

    def _execution_market_snapshot(
        self,
        symbol: str,
        market: str,
        side: str,
    ) -> tuple[float, float, float]:
        normalized_market = market.strip().lower()
        normalized_side = side.strip().upper()
        if normalized_market == "spot":
            mid = self.depth_tracker.spot_mid_price(symbol)
            spread = self.depth_tracker.spot_spread_bps(symbol)
            depth = (
                self.depth_tracker.spot_ask_depth(symbol)
                if normalized_side == "BUY"
                else self.depth_tracker.spot_bid_depth(symbol)
            )
        elif normalized_market == "perp":
            mid = _float_or_zero(
                self._mark_prices.get(symbol)
                or self.depth_tracker.perp_mid_price(symbol)
            )
            spread = self.depth_tracker.perp_spread_bps(symbol)
            depth = (
                self.depth_tracker.perp_ask_depth(symbol)
                if normalized_side == "BUY"
                else self.depth_tracker.perp_bid_depth(symbol)
            )
        else:
            return 0.0, float("inf"), 0.0
        return mid, spread, max(0.0, depth)

    def _queue_execution_markout(
        self,
        *,
        symbol: str,
        market: str,
        side: str,
        fill_price: float,
        filled_qty: float,
        trade_id: str,
        order_id: str,
        client_order_id: str,
        account_id: str,
        commission,
        commission_asset: str,
        maker: bool,
        event_time: str,
    ) -> None:
        """Capture fill-time inputs for a causal post-fill markout."""

        reference_mid, spread_bps, depth_usd = self._execution_market_snapshot(
            symbol,
            market,
            side,
        )
        if (
            fill_price <= 0.0
            or filled_qty <= 0.0
            or reference_mid <= 0.0
            or not math.isfinite(spread_bps)
        ):
            self.state_writer.set_risk_snapshot(
                {
                    "execution_markout_last_incomplete_fill": (
                        f"{symbol}:{market}:{trade_id}"
                    ),
                    "execution_markout_last_incomplete_reason": "missing_fill_time_market_snapshot",
                    "execution_markout_last_incomplete_at": event_time,
                }
            )
            return

        sample_id = (
            f"binance:{account_id}:{market}:{symbol.upper()}:{trade_id}:"
            f"markout:{int(_EXECUTION_MARKOUT_HORIZON_SECONDS)}s"
        )
        if (
            sample_id in self._pending_execution_markouts
            or self.state_reader.has_execution_quality_sample(sample_id)
        ):
            return

        notional_usd = filled_qty * fill_price
        commission_value = _float_or_zero(commission)
        normalized_asset = str(commission_asset or "").strip().upper()
        quote_asset = _extract_quote_asset(symbol)
        base_asset = _extract_base_asset(symbol)
        fee_usd: float | None = None
        if commission_value >= 0.0 and normalized_asset == quote_asset:
            fee_usd = commission_value
        elif commission_value >= 0.0 and normalized_asset == base_asset:
            fee_usd = commission_value * fill_price
        fee_bps = (
            fee_usd / notional_usd * 10_000.0
            if fee_usd is not None and notional_usd > 0.0
            else None
        )

        pending_entry = (
            self._pending_enters.get(symbol)
            or self._stale_pending_enters.get(symbol)
            or self._abandoned_pending_enters.get(symbol)
            or {}
        )
        safety_metrics = pending_entry.get("entry_safety_metrics", {})
        if not isinstance(safety_metrics, dict):
            safety_metrics = {}
        expected_cost_bps = max(
            0.0,
            _float_or_zero(safety_metrics.get("round_trip_cost_bps")) / 4.0,
        )
        now_monotonic = time.monotonic()
        self._pending_execution_markouts[sample_id] = {
            "sample_id": sample_id,
            "symbol": symbol.upper(),
            "market": market.strip().lower(),
            "side": side.strip().upper(),
            "fill_price": fill_price,
            "filled_qty": filled_qty,
            "notional_usd": notional_usd,
            "trade_id": trade_id,
            "order_id": order_id,
            "client_order_id": client_order_id,
            "event_time": event_time,
            "reference_mid": reference_mid,
            "spread_bps": max(0.0, spread_bps),
            "depth_usd": depth_usd,
            "maker": bool(maker),
            "fee_bps": fee_bps,
            "commission_asset": normalized_asset,
            "expected_cost_bps": expected_cost_bps,
            "urgency": 0.8,
            "route": "legacy_dual_maker",
            "due_monotonic": now_monotonic + _EXECUTION_MARKOUT_HORIZON_SECONDS,
            "expires_monotonic": now_monotonic + _EXECUTION_MARKOUT_MAX_WAIT_SECONDS,
        }

    def _drain_execution_markouts(self) -> None:
        """Persist matured markouts; incomplete fees remain ineligible for calibration."""

        if not self._pending_execution_markouts:
            return
        now_monotonic = time.monotonic()
        recorded = 0
        for sample_id, pending in list(self._pending_execution_markouts.items()):
            if now_monotonic < _float_or_zero(pending["due_monotonic"]):
                continue
            symbol = str(pending["symbol"])
            market = str(pending["market"])
            side = str(pending["side"])
            future_mid, _, _ = self._execution_market_snapshot(symbol, market, side)
            if future_mid <= 0.0:
                if now_monotonic < _float_or_zero(pending["expires_monotonic"]):
                    continue
                self._pending_execution_markouts.pop(sample_id, None)
                self.state_writer.set_risk_snapshot(
                    {
                        "execution_markout_last_expired_sample_id": sample_id,
                        "execution_markout_last_incomplete_reason": "missing_future_midpoint",
                        "execution_markout_last_incomplete_at": datetime.now(
                            timezone.utc
                        ).isoformat(),
                    }
                )
                continue

            fill_price = _float_or_zero(pending["fill_price"])
            reference_mid = _float_or_zero(pending["reference_mid"])
            if side == "BUY":
                realized_slippage_bps = (
                    (fill_price - reference_mid) / reference_mid * 10_000.0
                )
            else:
                realized_slippage_bps = (
                    (reference_mid - fill_price) / reference_mid * 10_000.0
                )
            markout_bps = adverse_markout_bps(side, fill_price, future_mid)
            spread_cost_bps = _float_or_zero(pending["spread_bps"]) / 2.0
            impact_bps = max(0.0, realized_slippage_bps - spread_cost_bps)
            fee_bps_value = pending.get("fee_bps")
            fee_bps = 0.0 if fee_bps_value is None else _float_or_zero(fee_bps_value)
            regime = (
                "calm"
                if _float_or_zero(pending["spread_bps"]) <= 5.0
                else "normal"
                if _float_or_zero(pending["spread_bps"]) <= 12.0
                else "stressed"
            )
            measured_at = datetime.now(timezone.utc).isoformat()
            metadata = {
                "sample_id": sample_id,
                "market": market,
                "route": str(pending["route"]),
                "regime": regime,
                "fee_bps": fee_bps,
                "fee_converted": fee_bps_value is not None,
                "commission_asset": str(pending["commission_asset"]),
                "spread_cost_bps": spread_cost_bps,
                "impact_bps": impact_bps,
                "markout_bps": markout_bps,
                "legging_bps": 0.0,
                "legging_scope": "pair_route_calibrated_separately",
                "notional_usd": _float_or_zero(pending["notional_usd"]),
                "reference_mid": reference_mid,
                "future_mid": future_mid,
                "fill_price": fill_price,
                "trade_id": str(pending["trade_id"]),
                "order_id": str(pending["order_id"]),
                "fill_event_time": str(pending["event_time"]),
                "markout_horizon_seconds": _EXECUTION_MARKOUT_HORIZON_SECONDS,
                "measurement_complete": fee_bps_value is not None,
                "measurement_only": True,
            }
            sample = ExecutionQualitySample(
                sample_id=sample_id,
                sample_time=measured_at,
                symbol=symbol,
                client_order_id=str(pending["client_order_id"]),
                side=side,
                order_type=f"{pending['route']}:{market}",
                urgency=_float_or_zero(pending["urgency"]),
                expected_cost_bps=_float_or_zero(pending["expected_cost_bps"]),
                realized_slippage_bps=realized_slippage_bps,
                spread_bps=_float_or_zero(pending["spread_bps"]),
                depth_usd=_float_or_zero(pending["depth_usd"]),
                maker=bool(pending["maker"]),
                quality_score=quality_score_from_slippage(realized_slippage_bps),
                metadata=metadata,
            )
            inserted = self.state_writer.record_execution_quality(sample)
            if inserted:
                observation = observation_from_execution_quality(
                    {
                        "sample_id": sample_id,
                        "sample_time": measured_at,
                        "symbol": symbol,
                        "metadata": metadata,
                    }
                )
                if observation is not None:
                    self.cost_calibrator.add_observation(observation)
                recorded += 1
            self._pending_execution_markouts.pop(sample_id, None)

        if recorded:
            self.state_writer.set_risk_snapshot(
                {
                    "execution_markout_last_recorded_at": datetime.now(
                        timezone.utc
                    ).isoformat(),
                    "execution_markout_pending_count": len(
                        self._pending_execution_markouts
                    ),
                    "execution_cost_calibration_sample_count": self.cost_calibrator.sample_count,
                    "execution_cost_calibration_measurement_only": True,
                }
            )
            self.state_writer.flush()

    def _on_depth_update(self, symbol: str, market: str, bids: list, asks: list) -> None:
        """Update depth cache; capture top perp bid as mark price proxy."""
        self._last_telemetry_event_monotonic = time.monotonic()
        self.depth_tracker.on_l2depth(symbol, market, bids, asks)
        self.regime_filter.on_depth_update(symbol)
        self._drain_execution_markouts()
        # Note: mark prices are now primarily set via _on_mark_price from MarkPrice WS events.
        # This depth-based fallback is kept for robustness if MarkPrice stream is delayed.

    def _on_mark_price(
        self,
        symbol: str,
        mark_price: float,
        next_funding_rate: float,
        next_funding_time_ms: int | float | None = None,
    ) -> None:
        """Update FundingRanker with live WS funding rate (~1s cadence).

        This provides sub-minute rate resolution compared to the 60s REST fallback,
        enabling the post-snapshot decay exit and rotation logic to react immediately
        when funding collapses at settlement rather than waiting for the next REST poll.
        """
        self._last_telemetry_event_monotonic = time.monotonic()
        self.funding_ranker.update_rate(
            symbol,
            next_funding_rate,
            next_funding_time_ms=next_funding_time_ms,
        )
        self.predictor.push_sample(symbol, next_funding_rate * 1095)
        self.regime_filter.on_mark_price(symbol, mark_price, next_funding_rate * 1095.0)
        # Also keep mark price cache fresh for ENTER quantity calculations.
        if mark_price > 0.0:
            self._mark_prices[symbol] = mark_price
            self._mark_price_ready.add(symbol)
            self._mark_price_updated_monotonic[symbol] = time.monotonic()
            self._drain_execution_markouts()

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
            rtt_sample = max(
                0,
                int((now_monotonic - self._last_heartbeat_sent_monotonic) * 1000),
            )
            # Cap the sample used for the EMA to prevent a single massive event-loop block
            # (e.g. 90s) from instantly inflating the smoothed RTT to thousands of ms,
            # which would keep the bot in SAFE_MODE long after the block is cleared.
            # 2000ms is enough to trigger the 400ms risk limit but avoids extreme spikes.
            rtt_sample_ema = min(rtt_sample, 2000)

            alpha = float(self._config.get("venue_latency_smoothing_factor") or VENUE_LATENCY_SMOOTHING_FACTOR)
            if self._last_heartbeat_rtt_ms <= 0:
                self._last_heartbeat_rtt_ms = rtt_sample_ema
            else:
                self._last_heartbeat_rtt_ms = int(
                    (1.0 - alpha) * self._last_heartbeat_rtt_ms + alpha * rtt_sample_ema
                )
        self._last_heartbeat_ack_at = _iso_from_ms(ts_ms)
        self._set_safe_mode_flag("heartbeat_bridge", False)

    def _on_private_stream_status(self, event: dict) -> None:
        """Gate non-paper readiness on complete spot and futures replay."""

        now_monotonic = time.monotonic()
        self._last_telemetry_event_monotonic = now_monotonic
        self._loop_heartbeats["private_stream_status"] = now_monotonic
        market = str(event.get("market") or "").strip().lower()
        status = str(event.get("status") or "").strip().upper()
        recognized_statuses = {
            "BACKFILLING",
            "READY",
            "GAP_DETECTED",
            "BACKFILL_FAILED",
        }
        error = str(event.get("error") or "").strip()

        def _event_int(name: str, default: int | None = None) -> int:
            value = event.get(name, default)
            if value is None:
                raise ValueError(f"{name} is missing")
            return int(value)

        try:
            start_time_ms = _event_int("start_time_ms")
            end_time_ms = _event_int("end_time_ms")
            orders_replayed = _event_int("orders_replayed", 0)
            trades_replayed = _event_int("trades_replayed", 0)
        except (TypeError, ValueError):
            start_time_ms = -1
            end_time_ms = -1
            orders_replayed = -1
            trades_replayed = -1

        structurally_ready = (
            market in {"spot", "perp"}
            and status == "READY"
            and not error
            and 0 <= start_time_ms <= end_time_ms
            and orders_replayed >= 0
            and trades_replayed >= 0
        )
        if market in {"spot", "perp"}:
            if structurally_ready:
                self._private_stream_ready_markets.add(market)
            else:
                self._private_stream_ready_markets.discard(market)
            self._private_stream_status[market] = {
                "status": status if status in recognized_statuses else "INVALID",
                "start_time_ms": start_time_ms if start_time_ms >= 0 else None,
                "end_time_ms": end_time_ms if end_time_ms >= 0 else None,
                "orders_replayed": max(0, orders_replayed),
                "trades_replayed": max(0, trades_replayed),
                "error": error,
            }
        else:
            self._private_stream_ready_markets.clear()
            self._private_stream_status["invalid"] = {
                "status": "INVALID",
                "error": error or "missing or invalid private-stream market",
            }

        quorum_ready = self._trading_mode == "paper" or (
            self._private_stream_ready_markets == {"spot", "perp"}
        )
        self._set_safe_mode_flag(
            "private_stream_recovery",
            self._trading_mode != "paper" and not quorum_ready,
        )
        self.state_writer.set_risk_snapshot(
            {
                "private_stream_recovery_ready": quorum_ready,
                "private_stream_ready_markets": sorted(
                    self._private_stream_ready_markets
                ),
                "private_stream_status": dict(self._private_stream_status),
            }
        )
        self.state_writer.flush()

    def _on_telemetry_gap(self, event: dict) -> None:
        """Revoke execution truth until private replay and reconcile finish."""

        now_monotonic = time.monotonic()
        self._last_telemetry_event_monotonic = now_monotonic
        self._loop_heartbeats["telemetry_gap"] = now_monotonic
        try:
            raw_skipped_messages = event.get("skipped_messages")
            raw_event_time_ms = event.get("event_time_ms")
            if raw_skipped_messages is None or raw_event_time_ms is None:
                raise ValueError("telemetry gap fields are missing")
            skipped_messages = int(raw_skipped_messages)
            event_time_ms = int(raw_event_time_ms)
            if skipped_messages <= 0 or event_time_ms < 0:
                raise ValueError("invalid telemetry gap range")
        except (TypeError, ValueError):
            skipped_messages = -1
            event_time_ms = -1
        reason = str(event.get("reason") or "telemetry gap").strip()

        self._private_stream_ready_markets.clear()
        self._rust_execution_ready = False
        self._rust_execution_readiness_status = "BLOCKED"
        self._rust_execution_readiness_reason = (
            reason if skipped_messages > 0 else "invalid telemetry gap event"
        )
        if self._trading_mode != "paper":
            self._set_safe_mode_flag("private_stream_recovery", True)
            self._set_safe_mode_flag("rust_execution_readiness", True)
        self.state_writer.set_risk_snapshot(
            {
                "private_stream_recovery_ready": self._trading_mode == "paper",
                "private_stream_ready_markets": [],
                "rust_execution_ready": self._trading_mode == "paper",
                "rust_execution_readiness_status": self._rust_execution_readiness_status,
                "rust_execution_readiness_reason": self._rust_execution_readiness_reason,
                "telemetry_gap_detected": True,
                "telemetry_gap_skipped_messages": skipped_messages,
                "telemetry_gap_event_time_ms": (
                    event_time_ms if event_time_ms >= 0 else None
                ),
            }
        )
        self.state_writer.flush()

    def _on_execution_readiness(self, event: dict) -> None:
        """Accept Rust readiness only after its two-venue reconciliation."""

        now_monotonic = time.monotonic()
        self._last_telemetry_event_monotonic = now_monotonic
        self._loop_heartbeats["execution_readiness"] = now_monotonic
        status = str(event.get("status") or "").strip().upper()
        reason = str(event.get("reason") or "").strip()
        try:
            raw_event_time_ms = event.get("event_time_ms")
            if raw_event_time_ms is None:
                raise ValueError("event_time_ms is missing")
            event_time_ms = int(raw_event_time_ms)
        except (TypeError, ValueError):
            event_time_ms = -1
        valid_status = status in {"READY", "RECONCILING", "BLOCKED", "DISCONNECTED"}
        private_quorum = self._trading_mode == "paper" or (
            self._private_stream_ready_markets == {"spot", "perp"}
        )
        ready = (
            self._trading_mode == "paper"
            or (
                valid_status
                and status == "READY"
                and event_time_ms >= 0
                and private_quorum
            )
        )
        self._rust_execution_ready = ready
        self._rust_execution_readiness_status = (
            "paper_bypass"
            if self._trading_mode == "paper"
            else status if valid_status else "INVALID"
        )
        self._rust_execution_readiness_reason = (
            ""
            if ready
            else reason
            or (
                "private stream quorum is not ready"
                if status == "READY" and not private_quorum
                else "Rust execution reconciliation is not ready"
            )
        )
        self._set_safe_mode_flag(
            "rust_execution_readiness",
            self._trading_mode != "paper" and not ready,
        )
        readiness_snapshot: dict[str, object] = {
            "rust_execution_ready": self._rust_execution_ready,
            "rust_execution_readiness_status": self._rust_execution_readiness_status,
            "rust_execution_readiness_reason": self._rust_execution_readiness_reason,
            "rust_execution_readiness_event_time_ms": (
                event_time_ms if event_time_ms >= 0 else None
            ),
        }
        if ready:
            readiness_snapshot.update(
                {
                    "telemetry_gap_detected": False,
                    "telemetry_gap_recovered_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        self.state_writer.set_risk_snapshot(readiness_snapshot)
        self.state_writer.flush()

    def _on_intent_ack(self, event: dict) -> None:
        """Persist Rust command lifecycle progress without mutating positions."""

        self._last_telemetry_event_monotonic = time.monotonic()
        try:
            known = self.execution.handle_ack(event)
        except (TypeError, ValueError):
            logger.exception("Rejected invalid/conflicting execution ACK: %r", event)
            self._set_safe_mode_flag("execution_bridge", True)
            return
        if not known:
            logger.error(
                "Received execution ACK for unknown durable intent %s",
                event.get("intent_id"),
            )
            self._set_safe_mode_flag("execution_bridge", True)

    def _on_config_ack(self, event: dict) -> None:
        """Establish config consensus only from a current, typed Rust ACK."""

        self._last_telemetry_event_monotonic = time.monotonic()
        try:
            known = self.execution.handle_ack(event)
        except (TypeError, ValueError):
            logger.exception("Rejected invalid/conflicting ConfigAck: %r", event)
            self._config_hash_consensus = False
            self._config_sync_status = "invalid_ack"
            self._config_sync_reason = "invalid or conflicting ConfigAck"
            self._config_sync_event.set()
            return

        declared_hash = str(event.get("declared_config_hash") or "")
        applied_hash = str(event.get("applied_config_hash") or "")
        config_status = str(event.get("config_status") or "").upper()
        reason = str(event.get("reason") or "")
        current_hash = self._config.canonical_snapshot().sha256
        if not known:
            self._config_hash_consensus = False
            self._config_sync_status = "unknown_ack"
            self._config_sync_reason = "ConfigAck did not match the durable outbox"
        elif (
            config_status == "APPLIED"
            and declared_hash == current_hash
            and applied_hash == current_hash
        ):
            self._config_hash_consensus = True
            self._rust_config_version_hash = applied_hash
            self._config_sync_status = "applied"
            self._config_sync_reason = ""
            logger.info("Rust/Python config consensus established: %s", current_hash)
        elif config_status == "REJECTED" and declared_hash == current_hash:
            self._config_hash_consensus = False
            self._rust_config_version_hash = applied_hash
            self._config_sync_status = "rejected"
            self._config_sync_reason = reason or "Rust rejected effective config"
            logger.error(
                "Rust rejected effective config %s: %s",
                current_hash,
                self._config_sync_reason,
            )
        else:
            # An ACK for the snapshot that was active before a hot reload is
            # useful telemetry, but it cannot authorize the current config.
            self._config_hash_consensus = False
            self._rust_config_version_hash = applied_hash
            self._config_sync_status = "stale_ack"
            self._config_sync_reason = (
                f"ConfigAck declared {declared_hash or '<empty>'}; current is {current_hash}"
            )

        self.state_writer.set_risk_snapshot(
            {
                "config_hash_consensus": self._config_hash_consensus,
                "python_config_version_hash": current_hash,
                "rust_config_version_hash": self._rust_config_version_hash,
                "config_sync_status": self._config_sync_status,
                "config_sync_reason": self._config_sync_reason,
                "config_sync_last_ack_at": datetime.now(timezone.utc).isoformat(),
                "config_sync_intent_id": str(event.get("intent_id") or ""),
            }
        )
        self.state_writer.flush()
        self._config_sync_event.set()

    def _dispatch_config_sync(self, *, force: bool = False) -> bool:
        """Send a canonical snapshot from the event-loop-owned ZMQ socket."""

        if self._trading_mode == "paper":
            return True
        snapshot = self._config.canonical_snapshot()
        if (
            self._config_hash_consensus
            and self._rust_config_version_hash == snapshot.sha256
        ):
            return True

        now_monotonic = time.monotonic()
        if (
            not force
            and self._config_sync_inflight_hash == snapshot.sha256
            and now_monotonic - self._config_sync_last_sent_monotonic < 10.0
        ):
            return True

        intent_id = f"config-sync:{snapshot.sha256[:16]}:{uuid.uuid4().hex[:12]}"
        self._config_sync_event.clear()
        self._config_sync_intent_id = intent_id
        self._config_sync_inflight_hash = snapshot.sha256
        self._config_sync_last_sent_monotonic = now_monotonic
        self._config_sync_status = "sending"
        self._config_sync_reason = ""
        sent = self.execution.send_config_sync(
            intent_id=intent_id,
            canonical_json=snapshot.canonical_json,
            config_version_hash=snapshot.sha256,
            cycle_id=intent_id,
        )
        if not sent:
            self._config_hash_consensus = False
            self._config_sync_inflight_hash = ""
            self._config_sync_status = "send_failed"
            self._config_sync_reason = "could not send CONFIG_SYNC to Rust"
            logger.error(self._config_sync_reason)
        elif self._config.canonical_snapshot().sha256 != snapshot.sha256:
            # A watcher reload raced the send. The old snapshot remains safe
            # but cannot establish eligibility for the new one.
            self._config_hash_consensus = False
            self._config_sync_status = "pending_reload"
            self._config_sync_reason = "effective config changed during CONFIG_SYNC send"
        self.state_writer.set_risk_snapshot(
            {
                "config_hash_consensus": self._config_hash_consensus,
                "python_config_version_hash": self._config.version_hash,
                "rust_config_version_hash": self._rust_config_version_hash,
                "config_sync_status": self._config_sync_status,
                "config_sync_reason": self._config_sync_reason,
                "config_sync_last_sent_at": datetime.now(timezone.utc).isoformat(),
                "config_sync_intent_id": intent_id,
            }
        )
        self.state_writer.flush()
        return sent

    async def _ensure_config_consensus(self, *, timeout_s: float = 10.0) -> bool:
        if self._trading_mode == "paper":
            return True
        deadline = time.monotonic() + max(0.1, float(timeout_s))
        force = True
        while time.monotonic() < deadline:
            current_hash = self._config.canonical_snapshot().sha256
            if self._config_hash_consensus and self._rust_config_version_hash == current_hash:
                return True
            if not self._dispatch_config_sync(force=force):
                return False
            force = False
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                break
            try:
                await asyncio.wait_for(
                    self._config_sync_event.wait(),
                    timeout=min(2.0, remaining),
                )
            except asyncio.TimeoutError:
                continue
            self._config_sync_event.clear()
            current_hash = self._config.canonical_snapshot().sha256
            if self._config_hash_consensus and self._rust_config_version_hash == current_hash:
                return True
            if self._config_sync_status == "rejected":
                return False
            # Stale ACK after a reload: immediately send the current snapshot.
            force = self._config_sync_status == "stale_ack"
        self._config_hash_consensus = False
        self._config_sync_status = "timeout"
        self._config_sync_reason = "timed out waiting for matching ConfigAck"
        return False

    def _on_volume_bar(self, symbol: str, minute_start_ms, notional_usd: float) -> None:
        self._last_telemetry_event_monotonic = time.monotonic()
        minute_iso = _iso_from_ms(minute_start_ms)
        self._latest_volume_bar[symbol] = (minute_iso[:16], _float_or_zero(notional_usd))
        self.regime_filter.on_volume_bar(symbol, _float_or_zero(notional_usd))

    def _is_hard_rejection(self, reason: str) -> bool:
        """Classify if a rejection reason is a hard failure (should not retry immediately)."""
        hard_substrings = [
            "InsufficientBalance",
            "AccountIneligible",
            "MarginInsufficient",
            "OrderForbidden",
            "InvalidQuantity",
            "invalid_quantity",
            "InvalidPrice",
            "PositionSideMismatch",
            "reduce_only_failed",
            "insufficient_balance",
            "-2010",  # Account has insufficient balance
            "-1102",  # Invalid quantity
            "-4004",  # Position side mismatch
            "-1111",  # Precision error
            "-1013",  # Filter failure (lot size, min_notional)
            "-4005",  # Quantity greater than max quantity
            "min_notional",
        ]
        reason_lower = reason.lower()
        return any(s.lower() in reason_lower for s in hard_substrings)

    def _on_order_rejected(self, symbol: str, intent: str, intent_id: str | None, reason: str) -> None:
        """Rust rejected an instruction.

        Exits: clear pending state and schedule an immediate retry.
        Enters: clear the pending_enter row immediately so a terminal
        Rust-side rejection (e.g. reason=chase_active) does not wedge the
        symbol for 300s waiting on the stale-pending-intent timer. The
        main decision loop will re-evaluate on its next tick.
        """
        logger.warning(
            "OrderRejected from Rust: symbol=%s intent=%s reason=%s intent_id=%s",
            symbol, intent, reason, intent_id,
        )
        normalized_reason = str(reason or "").strip().lower()
        if self._trading_mode != "paper" and normalized_reason in {
            "config_consensus_unavailable",
            "config_consensus_hash_mismatch",
        }:
            # Most commonly this means the Rust process restarted and lost its
            # intentionally in-memory consensus. Revoke local eligibility; the
            # maintenance loop will replay a canonical snapshot before retry.
            self._config_hash_consensus = False
            self._rust_config_version_hash = ""
            self._config_sync_status = "rust_consensus_lost"
            self._config_sync_reason = normalized_reason
            self._config_sync_event.clear()
            self.state_writer.set_risk_snapshot(
                {
                    "config_hash_consensus": False,
                    "rust_config_version_hash": "",
                    "config_sync_status": self._config_sync_status,
                    "config_sync_reason": self._config_sync_reason,
                }
            )
        is_exit = intent in ("EXIT_LONG", "EXIT_SHORT")
        is_enter = intent in ("ENTER_LONG", "ENTER_SHORT")

        if is_enter:
            tracked = self._pending_enters.get(symbol)
            if tracked is not None and (
                not intent_id or str(tracked.get("intent_id") or "") == intent_id
            ):
                self._pending_enters.pop(symbol, None)
                self._release_entry_reservation(
                    tracked,
                    reason=f"entry_terminal_rejection:{reason}",
                    exchange_terminal_proven=True,
                )
                tracked_intent_id = str(tracked.get("intent_id") or "") or intent_id
                if tracked_intent_id:
                    self.state_writer.update_pending_intent(
                        tracked_intent_id,
                        status="REJECTED",
                        last_error=reason,
                    )
                    self._resolve_pending_intent(tracked_intent_id)
                if self._is_hard_rejection(reason):
                    self.cooldowns.activate_symbol(symbol, 1800, f"hard_reject_enter:{reason}")
                elif "circuit_breaker" in reason.lower():
                    self.cooldowns.activate_symbol(symbol, 60, f"circuit_breaker:{reason}")
            return

        if not is_exit:
            return
        tracked_id = self._pending_exit_intents.get(symbol)
        if intent_id and tracked_id and tracked_id != intent_id:
            # Stale rejection for an intent we've already superseded
            return
        if symbol in self._pending_exit_intents:
            self._pending_exit_intents.pop(symbol, None)
            self._pending_exit_created_at.pop(symbol, None)
            self._stale_pending_exits.discard(symbol)
            if intent_id:
                self.state_writer.update_pending_intent(intent_id, status="REJECTED", last_error=reason)
                self._resolve_pending_intent(intent_id)

            self._exit_rejections.add(symbol)
            event = self._exit_events.pop(symbol, None)
            if event:
                event.set()

            if self._is_hard_rejection(reason):
                # A rejected close is not evidence that exchange exposure is
                # flat, even for precision/min-notional failures.  Preserve the
                # position and require reconciliation/operator repair.
                logger.error(
                    "Hard rejection for %s EXIT: %s. Preserving exposure for manual review.",
                    symbol,
                    reason,
                )
                self.state_writer.update_position_metrics(
                    symbol, recovery_state="manual_review"
                )
                self.cooldowns.activate_symbol(
                    symbol, 3600, f"hard_reject_exit:{reason}"
                )
                self._set_safe_mode_flag("exit_failure", True)
            else:
                retry_count = self._exit_retry_counts.get(symbol, 0) + 1
                self._exit_retry_counts[symbol] = retry_count
                if retry_count > 3:
                    logger.error(
                        "Exit retry limit reached for %s (reason: %s). Escalating to manual review.",
                        symbol, reason,
                    )
                    self.state_writer.update_position_metrics(
                        symbol, recovery_state="manual_review"
                    )
                    self.cooldowns.activate_symbol(symbol, 3600, f"exit_retry_limit:{reason}")
                    self._set_safe_mode_flag("exit_failure", True)
                    return

                direction = "short" if intent == "EXIT_SHORT" else "long"
                logger.warning(
                    "Retrying rejected EXIT for %s (attempt %d/3, reason: %s)",
                    symbol, retry_count, reason,
                )
                self._schedule_background_coroutine(
                    self._retry_rejected_exit(symbol, direction),
                    name=f"retry_rejected_exit:{symbol}",
                )

    async def _retry_rejected_exit(self, symbol: str, direction: str) -> None:
        """Re-dispatch an exit that Rust rejected, after a brief delay."""
        await asyncio.sleep(0.5)
        self._dispatch_exit(symbol, urgency=1.0, direction=direction)

    def _entry_policy_block_reason(self, risk_state: dict | None = None) -> str | None:
        if self._runtime_mode == "BLOCKED":
            return f"blocked: {self._blocked_reason or 'unknown'}"
        if self._runtime_mode == "SAFE_MODE":
            global_flags = self._active_global_safe_mode_flags()
            reason = ", ".join(sorted(global_flags)) if global_flags else self._safe_mode_reason()
            return f"safe mode: {reason or 'operator guard'}"
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
        if self._trading_mode != "paper":
            current_config_hash = self._config.canonical_snapshot().sha256
            if not self._config_hash_consensus:
                detail = self._config_sync_reason or self._config_sync_status
                return f"execution config consensus unavailable: {detail}"
            if self._rust_config_version_hash != current_config_hash:
                return "execution config consensus hash mismatch"
        if self._risk_kill_switch:
            return "kill switch active"
        if not self._risk_allow_new_risk:
            return "risk engine blocked new exposure"
        risk_state = (
            self._ensure_validation_snapshot_for_policy(risk_state)
            if risk_state is not None
            else self._ensure_validation_snapshot_for_policy(self.state_reader.get_risk())
        )
        if risk_state.get("pause_new_entries") is True:
            return "new entries paused by operator"
        if risk_state.get("kill_switch") or risk_state.get("is_kill_switch"):
            return "kill switch active"
        validation_reason = self._validation_entry_block_reason(risk_state)
        if validation_reason is not None:
            return validation_reason
        hedge_gap_reason = self._hedge_gap_entry_block_reason(risk_state)
        if hedge_gap_reason is not None:
            return hedge_gap_reason
        funding_status = self.funding_ranker.status_snapshot()
        if not bool(funding_status.get("funding_metadata_ready")):
            detail = str(funding_status.get("funding_info_last_error") or "not yet fetched")
            return f"authoritative funding calendar unavailable: {detail}"
        return None

    def _external_entry_block_reason(self) -> str | None:
        return self._entry_policy_block_reason()

    @staticmethod
    def _coerce_symbol_list(value) -> list[str]:
        if isinstance(value, list):
            return [str(item).upper() for item in value if str(item).strip()]
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return [item.strip().upper() for item in text.split(",") if item.strip()]
            if isinstance(parsed, list):
                return [str(item).upper() for item in parsed if str(item).strip()]
        return []

    def _validation_entry_block_reason(self, risk_state: dict) -> str | None:
        return str(self._validation_policy_snapshot(risk_state)["entry_block_reason"] or "") or None

    def _ensure_validation_snapshot_for_policy(self, risk_state: dict) -> dict:
        if self._trading_mode == "paper":
            return risk_state
        if risk_state.get("validation_go_no_go") and risk_state.get("validation_status"):
            return risk_state
        try:
            self._maybe_record_validation_snapshot(datetime.now(timezone.utc))
            return self.state_reader.get_risk()
        except Exception as exc:
            logger.warning("Could not create validation snapshot for entry policy: %s", exc)
            return risk_state

    @staticmethod
    def _coerce_string_list(value) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value if str(item).strip()]
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return [item.strip() for item in text.split(",") if item.strip()]
            if isinstance(parsed, list):
                return [str(item) for item in parsed if str(item).strip()]
        return []

    def _validation_adjustment_action(self, blockers: list[str]) -> tuple[str, float]:
        """Map validation ADJUST blockers to conservative autonomous policy."""
        lower_blockers = [reason.lower() for reason in blockers]
        configured_scale = max(
            0.10,
            min(1.0, _float_or_zero(self._config.get("validation_adjust_notional_scale") or 0.50)),
        )
        if any("drawdown" in reason for reason in lower_blockers):
            return "reduce_exposure_and_resize_smaller", min(configured_scale, 0.25)
        if any("sharpe" in reason for reason in lower_blockers):
            return "resize_smaller_until_signal_quality_recovers", min(configured_scale, 0.50)
        if any("cost model" in reason or "uptime" in reason for reason in lower_blockers):
            return "cautious_entries_with_existing_reconciliation", min(configured_scale, 0.50)
        if any("observation window" in reason or "clean run" in reason for reason in lower_blockers):
            return "collect_more_evidence_at_reduced_size", configured_scale
        return "auto_adjust_reduced_size", configured_scale

    def _validation_policy_snapshot(self, risk_state: dict | None = None) -> dict[str, object]:
        if self._trading_mode == "paper":
            return {
                "entry_block_reason": None,
                "validation_entry_policy": "paper_bypass",
                "validation_adjustment_action": "",
                "validation_position_scale": 1.0,
            }
        risk_state = risk_state if risk_state is not None else self.state_reader.get_risk()
        go_no_go = str(risk_state.get("validation_go_no_go") or "").strip().upper()
        status = str(risk_state.get("validation_status") or "").strip().upper()
        blockers = self._coerce_string_list(risk_state.get("validation_blockers"))

        if go_no_go == "GO" and status not in _VALIDATION_HARD_BLOCK_STATUSES:
            return {
                "entry_block_reason": None,
                "validation_entry_policy": "go",
                "validation_adjustment_action": "",
                "validation_position_scale": 1.0,
            }
        if go_no_go == "ADJUST":
            if status not in _VALIDATION_ADJUST_STATUSES:
                return {
                    "entry_block_reason": f"validation ADJUST has unsafe status ({status or 'missing'})",
                    "validation_entry_policy": "blocked",
                    "validation_adjustment_action": "fail_closed_unknown_adjust_status",
                    "validation_position_scale": 0.0,
                }
            action, scale = self._validation_adjustment_action(blockers)
            return {
                "entry_block_reason": None,
                "validation_entry_policy": "auto_adjust",
                "validation_adjustment_action": action,
                "validation_position_scale": scale,
            }
        if go_no_go in {"NO_GO", "HALT"}:
            return {
                "entry_block_reason": f"validation not GO ({go_no_go})",
                "validation_entry_policy": "blocked",
                "validation_adjustment_action": "fail_closed_validation_no_go",
                "validation_position_scale": 0.0,
            }
        if status in _VALIDATION_HARD_BLOCK_STATUSES:
            return {
                "entry_block_reason": f"validation status {status}",
                "validation_entry_policy": "blocked",
                "validation_adjustment_action": "fail_closed_validation_status",
                "validation_position_scale": 0.0,
            }
        return {
            "entry_block_reason": f"validation not GO ({go_no_go or 'missing'})",
            "validation_entry_policy": "blocked",
            "validation_adjustment_action": "fail_closed_missing_or_unknown_validation",
            "validation_position_scale": 0.0,
        }

    def _persist_validation_policy_snapshot(self, risk_state: dict | None = None) -> None:
        policy = self._validation_policy_snapshot(risk_state)
        self.state_writer.set_risk_snapshot(
            {
                "validation_entry_policy": policy["validation_entry_policy"],
                "validation_adjustment_action": policy["validation_adjustment_action"],
                "validation_position_scale": policy["validation_position_scale"],
            }
        )

    def _hedge_gap_entry_block_reason(self, risk_state: dict) -> str | None:
        hedge_gap_symbols = self._coerce_symbol_list(risk_state.get("hedge_gap_symbols"))
        if not hedge_gap_symbols:
            hedge_gap_symbols = self._coerce_symbol_list(
                risk_state.get("startup_reconciliation_spot_hedge_gaps")
            )
        if not hedge_gap_symbols:
            return None
        return f"hedge gap active ({', '.join(sorted(hedge_gap_symbols))})"

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
            if exchange_pnl_usd == 0.0 and abs(perp_pnl) > 0.0:
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
        # A hedge gap means the book is no longer delta-neutral. Keep exits and
        # manual recovery available, but fail closed for new entries until the
        # exchange/local reconciliation proves the gap is gone.
        self._set_safe_mode_flag("hedge_gap", bool(hedge_gaps))
        if hedge_gaps:
            self.state_writer.set_risk_snapshot({"hedge_gap_symbols": sorted(hedge_gaps)})
        else:
            self.state_writer.set_risk_snapshot({"hedge_gap_symbols": []})
        self._refresh_startup_recovery_flags(rows)
        return rows

    def _maybe_process_operator_flatten_all_request(self, rows: list[dict] | None = None) -> bool:
        risk_state = self.state_reader.get_risk()
        request_id = str(risk_state.get("operator_flatten_all_request_id") or "").strip()
        request_status = str(risk_state.get("operator_flatten_all_status") or "").strip().lower()
        requested_by = str(risk_state.get("operator_flatten_all_requested_by") or "").strip()
        
        if not request_id or request_status in {"", "completed", "failed", "cancelled", "partial_failed"}:
            self._operator_flatten_cycle_count = 0
            self._operator_flatten_attempts.clear()
            return False

        # Reset cycle count if request ID changed
        if request_id != self._last_operator_flatten_request_id:
            logger.info("New operator flatten-all request detected: %s", request_id)
            self._last_operator_flatten_request_id = request_id
            self._operator_flatten_cycle_count = 0
            self._operator_flatten_attempts.clear()

        self._operator_flatten_cycle_count += 1
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
                "Operator flatten-all request %s completed; portfolio is flat",
                request_id,
            )
            self._operator_flatten_cycle_count = 0
            self._operator_flatten_attempts.clear()
            return False

        dispatched_symbols: list[str] = []
        remaining_symbols: list[str] = []
        stuck_symbols: list[str] = []

        for row in open_rows:
            symbol = str(row.get("symbol", "")).upper()
            if not symbol:
                continue

            # 0. Skip 'dust' positions that are too small to close and shouldn't block progress
            qty = abs(_float_or_zero(row.get("qty")))
            spot_live, perp_live = self._leg_mark_prices(symbol, row)
            mark_price = perp_live if perp_live > 0 else spot_live
            if mark_price > 0 and (qty * mark_price) < 5.0:
                logger.warning("Flatten request %s: ignoring dust position for %s ($%.2f)", request_id, symbol, qty * mark_price)
                continue

            remaining_symbols.append(symbol)

            # 1. Check for stale states and clear them to allow a fresh attempt
            if symbol in self._stale_pending_exits or symbol in self._stale_pending_enters:
                logger.warning("Flatten request %s: clearing stale intent state for %s to force retry", request_id, symbol)
                self._stale_pending_exits.discard(symbol)
                self._stale_pending_enters.pop(symbol, None)
                self._pending_exit_intents.pop(symbol, None)
                self._pending_exit_created_at.pop(symbol, None)
                self._exit_events.pop(symbol, None)
                self._pending_enters.pop(symbol, None)

            if symbol in self._exit_events:
                continue

            # 2. Track attempts to avoid infinite loops on hard rejects
            attempts = self._operator_flatten_attempts.get(symbol, 0)
            if attempts >= 10:
                stuck_symbols.append(symbol)
                continue

            # Cooldowns are entry guards.  They must never suppress an
            # operator-requested reduce-only flatten.
            allowed, cooldown_reason = self.cooldowns.allow_symbol(symbol)
            if not allowed:
                logger.info(
                    "Flatten request %s: bypassing entry cooldown for %s (%s)",
                    request_id,
                    symbol,
                    cooldown_reason,
                )

            direction = str(row.get("direction") or self._position_directions.get(symbol) or "long")
            self._dispatch_exit(symbol, urgency=1.0, direction=direction)
            dispatched_symbols.append(symbol)
            self._operator_flatten_attempts[symbol] = attempts + 1

        # 4. Check if we have converged or hit a wall
        status = "in_progress"
        note = "Waiting for exit fills on all open positions."

        if stuck_symbols and len(stuck_symbols) == len(remaining_symbols):
            status = "partial_failed"
            note = f"Flatten request reached limit for symbols: {', '.join(stuck_symbols)}. Manual intervention required."
            logger.error("Operator flatten-all request %s reached terminal partial failure: %s", request_id, note)
        elif self._operator_flatten_cycle_count > 300 and not dispatched_symbols and any(s not in self._exit_events for s in remaining_symbols):
            # If we've been trying for 5 minutes and nothing is happening, mark as partially failed
            status = "partial_failed"
            note = f"Flatten request timed out. Remaining: {', '.join(remaining_symbols)}. Stuck: {', '.join(stuck_symbols)}"
            logger.error("Operator flatten-all request %s timed out: %s", request_id, note)
        elif not remaining_symbols:
            status = "completed"
            note = "Portfolio is flat. New entries remain paused."
        # Periodically log progress
        if self._operator_flatten_cycle_count % 10 == 1 or dispatched_symbols:
            logger.warning(
                "Operator flatten-all request %s status=%s | remaining=%d | dispatched=%d | stuck=%d",
                request_id, status, len(remaining_symbols), len(dispatched_symbols), len(stuck_symbols)
            )

        self.state_writer.set_risk_snapshot(
            {
                "operator_flatten_all_status": status,
                "operator_flatten_all_acknowledged_at": now_iso,
                "operator_flatten_all_dispatched_symbols": dispatched_symbols,
                "operator_flatten_all_remaining_symbols": remaining_symbols,
                "operator_flatten_all_note": note,
            }
        )
        self.state_writer.flush()
        
        if status in {"completed", "partial_failed"}:
            self._operator_flatten_cycle_count = 0
            self._operator_flatten_attempts.clear()
            return False
            
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
        # Long-spot/short-perp uses owned quote cash and has no spot borrow.
        # The inverse route shorts spot and therefore carries borrow interest.
        borrow_cost_usd = (
            self._borrow_cost_usd(
                notional_usd=notional_usd,
                hold_hours=max(hold_hours, 0.0),
            )
            if direction == "short"
            else 0.0
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
        intent_id: str = "",
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
        reported_spot_exit_price = _pick_price(spot_fill_price)
        reported_perp_exit_price = _pick_price(
            perp_fill_price,
            avg_fill_price,
            last_fill_price,
        )
        spot_exit_price = _pick_price(
            reported_spot_exit_price,
            fallback_spot_price,
            pos.get("spot_live"),
            spot_entry_price,
        )
        perp_exit_price = _pick_price(
            reported_perp_exit_price,
            fallback_perp_price,
            pos.get("perp_live"),
            perp_entry_price,
        )
        if reported_spot_exit_price is None or reported_perp_exit_price is None:
            logger.critical(
                "Exit attribution for %s has missing exchange fill prices "
                "(execution_type=%s); fallback marks are model-only and the "
                "non-paper trade will remain INCOMPLETE",
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

        execution_evidence = self.state_reader.get_trade_execution_cost_evidence(
            symbol,
            entry_time_str,
            exit_time,
        )
        actual_execution_cost_usd = float(
            execution_evidence.get("execution_cost_usd") or 0.0
        )
        estimated_total_cost_usd = estimated_entry_cost_usd
        exit_notional_usd = ((spot_exit_price + perp_exit_price) / 2.0) * qty
        estimated_exit_cost_usd = blended_exit_cost(
            exit_notional_usd,
            depth_usd=self._cost_depth_or_default(self.depth_tracker.get_exit_depth(symbol)),
        )
        estimated_total_cost_usd += estimated_exit_cost_usd

        if estimated_total_cost_usd > 0.0 and bool(execution_evidence.get("complete")):
            cost_model_error_pct = (
                abs(actual_execution_cost_usd - estimated_total_cost_usd)
                / estimated_total_cost_usd
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

        modeled_funding_input = (
            funding_collected
            if self._trading_mode == "paper"
            else self._synthetic_funding_collected_usd(
                qty=qty,
                direction=direction,
                ann_funding=ann_funding,
                hold_hours=max(hold_hours, 0.0),
                funding_periods=funding_periods,
                spot_entry_price=spot_entry_price,
                perp_entry_price=perp_entry_price,
            )
        )
        (
            modeled_net_pnl,
            modeled_funding_collected,
            modeled_basis_pnl_usd,
            modeled_borrow_cost_usd,
        ) = self._calculate_trade_pnl(
            qty=qty,
            direction=direction,
            ann_funding=ann_funding,
            hold_hours=max(hold_hours, 0.0),
            funding_periods=funding_periods,
            funding_collected_usd=modeled_funding_input,
            execution_cost_usd=estimated_total_cost_usd,
            spot_entry_price=spot_entry_price,
            perp_entry_price=perp_entry_price,
            spot_exit_price=spot_exit_price,
            perp_exit_price=perp_exit_price,
        )

        durable_intent_id = str(
            intent_id
            or self._pending_exit_intents.get(symbol)
            or (self._abandoned_exit_intents.get(symbol) or {}).get("intent_id")
            or ""
        )
        if not durable_intent_id:
            raise RuntimeError(f"Exit fill for {symbol} has no durable intent_id")

        if self._trading_mode == "paper":
            economic_status = "MODELED"
            economic_reasons = ["paper_exchange_model"]
            net_pnl = modeled_net_pnl
            funding_collected = modeled_funding_collected
            execution_cost_usd = (
                actual_execution_cost_usd
                if bool(execution_evidence.get("complete"))
                else estimated_total_cost_usd
            )
            net_pnl += estimated_total_cost_usd - execution_cost_usd
            basis_pnl_usd = modeled_basis_pnl_usd
            borrow_cost_usd = modeled_borrow_cost_usd
        else:
            economic_reasons: list[str] = []
            if not funding_source.startswith("actual_"):
                economic_reasons.append("funding_cashflow_missing")
            if not bool(execution_evidence.get("complete")):
                economic_reasons.append("commission_evidence_incomplete")

            actual_prices_complete = (
                reported_spot_exit_price is not None
                and reported_perp_exit_price is not None
                and spot_entry_price > 0.0
                and perp_entry_price > 0.0
            )
            if actual_prices_complete:
                _, _, actual_basis_pnl_usd, _ = self._calculate_trade_pnl(
                    qty=qty,
                    direction=direction,
                    ann_funding=ann_funding,
                    hold_hours=max(hold_hours, 0.0),
                    funding_periods=funding_periods,
                    funding_collected_usd=0.0,
                    execution_cost_usd=0.0,
                    spot_entry_price=spot_entry_price,
                    perp_entry_price=perp_entry_price,
                    spot_exit_price=reported_spot_exit_price,
                    perp_exit_price=reported_perp_exit_price,
                )
            else:
                actual_basis_pnl_usd = 0.0
                economic_reasons.append("exchange_fill_prices_incomplete")

            cashflows = self.state_reader.get_trade_economic_cashflows(
                symbol,
                entry_time_str,
                exit_time,
            )
            cashflow_totals = dict(cashflows.get("totals_usd") or {})
            cashflow_counts = dict(cashflows.get("counts") or {})
            cashflow_unvalued = dict(cashflows.get("unvalued") or {})
            actual_borrow_cost_usd = max(
                0.0,
                -float(cashflow_totals.get("BORROW_INTEREST") or 0.0),
            )
            if direction == "short" and not cashflow_counts.get("BORROW_INTEREST"):
                economic_reasons.append("borrow_interest_missing")
            if cashflow_unvalued:
                economic_reasons.append("unvalued_economic_cashflow")

            economic_status = "RECONCILED" if not economic_reasons else "INCOMPLETE"
            basis_pnl_usd = actual_basis_pnl_usd
            execution_cost_usd = actual_execution_cost_usd
            borrow_cost_usd = actual_borrow_cost_usd
            net_pnl = (
                basis_pnl_usd
                + funding_collected
                - execution_cost_usd
                - borrow_cost_usd
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
            economic_status=economic_status,
            economic_notes=json.dumps(sorted(set(economic_reasons))),
            estimated_net_pnl_usd=modeled_net_pnl,
            estimated_funding_collected=modeled_funding_collected,
            estimated_execution_cost_usd=estimated_total_cost_usd,
            estimated_basis_pnl_usd=modeled_basis_pnl_usd,
            estimated_borrow_cost_usd=modeled_borrow_cost_usd,
            exit_intent_id=durable_intent_id,
        )
        self.state_writer.project_exit_lifecycle(
            event_key=f"exit:{durable_intent_id}",
            intent_id=durable_intent_id,
            event_time=exit_time,
            trade=trade,
            evidence={
                "execution_type": execution_type,
                "spot_fill_price": spot_fill_price,
                "perp_fill_price": perp_fill_price,
                "avg_fill_price": avg_fill_price,
                "last_fill_price": last_fill_price,
            },
        )
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
        self._loop_heartbeats["on_order_update"] = time.monotonic()

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
            "cumulative_filled_qty": _kwargs.get("cumulative_filled_qty"),
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
            "market": _kwargs.get("market"),
            "side": _kwargs.get("side"),
            "order_id": _kwargs.get("order_id"),
            "trade_id": _kwargs.get("trade_id"),
            "account_id": _kwargs.get("account_id"),
            "environment": _kwargs.get("environment"),
            "strategy_id": _kwargs.get("strategy_id"),
            "cycle_id": _kwargs.get("cycle_id"),
            "intent_id": _kwargs.get("intent_id"),
            "leg_id": _kwargs.get("leg_id"),
            "config_version_hash": _kwargs.get("config_version_hash"),
        }
        
        maker_fills = _kwargs.get("maker_fills")
        taker_fills = _kwargs.get("taker_fills")
        if maker_fills is not None and taker_fills is not None:
            self.state_writer.set_stats({"maker_fills": maker_fills, "taker_fills": taker_fills})

        # Fill events must be durable before any position/trade lifecycle
        # mutation below.  In particular, a cycle-completion fill immediately
        # finalizes the trade and reads its commissions back from SQLite; putting
        # that event on the asynchronous queue used to race that read.  Persist
        # partial fills synchronously as well so their incremental commissions
        # are present when the terminal event arrives.  Non-fill telemetry can
        # retain the batched background path.
        if str(status).upper() in {"PARTIALLY_FILLED", "FILLED"}:
            try:
                execution_type = str(_kwargs.get("execution_type") or "").upper()
                normalized_market = str(_kwargs.get("market") or "").strip().lower()
                normalized_side = str(_kwargs.get("side") or "").strip().upper()
                trade_id = _kwargs.get("trade_id")
                order_id = _kwargs.get("order_id")
                fill_price = _pick_price(
                    _kwargs.get("last_fill_price"),
                    _kwargs.get("avg_fill_price"),
                )
                lineage = {
                    "account_id": str(_kwargs.get("account_id") or "").strip(),
                    "strategy_id": str(_kwargs.get("strategy_id") or "").strip(),
                    "cycle_id": str(_kwargs.get("cycle_id") or "").strip(),
                    "intent_id": str(_kwargs.get("intent_id") or "").strip(),
                    "leg_id": str(_kwargs.get("leg_id") or "").strip(),
                    "config_version_hash": str(
                        _kwargs.get("config_version_hash") or ""
                    ).strip(),
                }
                stable_trade_id = "" if trade_id is None else str(trade_id).strip()
                quote_asset = _extract_quote_asset(symbol)
                missing_lineage: list[str] = []
                if execution_type == "TRADE" and _float_or_zero(filled_qty) > 0.0:
                    if normalized_market not in {"spot", "perp"}:
                        missing_lineage.append("market")
                    if normalized_side not in {"BUY", "SELL"}:
                        missing_lineage.append("side")
                    if not stable_trade_id or stable_trade_id == "-1":
                        missing_lineage.append("trade_id")
                    if order_id is None or not str(order_id).strip():
                        missing_lineage.append("order_id")
                    if fill_price is None:
                        missing_lineage.append("last_fill_price")
                    if not quote_asset:
                        missing_lineage.append("quote_asset")
                    missing_lineage.extend(
                        key for key, value in lineage.items() if not value
                    )

                    if not missing_lineage:
                        exchange_fill_id = (
                            f"binance:{lineage['account_id']}:{normalized_market}:"
                            f"{symbol.upper()}:{stable_trade_id}"
                        )
                        self.state_writer.record_execution_and_economic_fill(
                            event_payload,
                            {
                                "account_id": lineage["account_id"],
                                "trading_mode": self._trading_mode,
                                "venue": "BINANCE",
                                "strategy_id": lineage["strategy_id"],
                                "event_time": event_time,
                                "symbol": symbol.upper(),
                                "instrument_type": (
                                    "SPOT" if normalized_market == "spot" else "PERPETUAL"
                                ),
                                "side": normalized_side,
                                "quantity": str(filled_qty),
                                "price": str(fill_price),
                                "quantity_asset": _extract_base_asset(symbol),
                                "quote_asset": quote_asset,
                                "exchange_fill_id": exchange_fill_id,
                                "source_event_id": exchange_fill_id,
                                "exchange_event_id": (
                                    f"{normalized_market}:{symbol.upper()}:{order_id}:"
                                    f"{stable_trade_id}"
                                ),
                                "cycle_id": lineage["cycle_id"],
                                "intent_id": lineage["intent_id"],
                                "order_id": str(order_id),
                                "client_order_id": str(
                                    _kwargs.get("client_order_id") or ""
                                ),
                                "commission_amount": _kwargs.get("commission"),
                                "commission_asset": str(
                                    _kwargs.get("commission_asset") or ""
                                ),
                                "realized_pnl_amount": _kwargs.get("realized_pnl"),
                                "realized_pnl_asset": quote_asset,
                                "runtime_mode": self._runtime_mode,
                                "session_id": self._session_id,
                                "metadata": {
                                    "status": str(status),
                                    "maker": _kwargs.get("maker"),
                                    "leg_id": lineage["leg_id"],
                                    "config_version_hash": lineage[
                                        "config_version_hash"
                                    ],
                                },
                                "raw_payload": event_payload,
                            },
                        )
                        self.state_writer.set_risk_snapshot(
                            {
                                "economic_ledger_ingestion_healthy": True,
                                "economic_ledger_last_fill_at": event_time,
                            }
                        )
                        self._queue_execution_markout(
                            symbol=symbol,
                            market=normalized_market,
                            side=normalized_side,
                            fill_price=_float_or_zero(fill_price),
                            filled_qty=_float_or_zero(filled_qty),
                            trade_id=stable_trade_id,
                            order_id=str(order_id),
                            client_order_id=str(
                                _kwargs.get("client_order_id") or ""
                            ),
                            account_id=lineage["account_id"],
                            commission=_kwargs.get("commission"),
                            commission_asset=str(
                                _kwargs.get("commission_asset") or ""
                            ),
                            maker=bool(_kwargs.get("maker")),
                            event_time=event_time,
                        )
                    else:
                        self.state_writer.record_execution_event(event_payload)
                        logger.critical(
                            "Economic fill for %s lacks stable lineage (%s); raw evidence retained and ledger readiness revoked",
                            symbol,
                            ", ".join(sorted(set(missing_lineage))),
                        )
                        self.state_writer.set_risk_snapshot(
                            {
                                "economic_ledger_reconciled": False,
                                "economic_ledger_ingestion_healthy": False,
                                "economic_ledger_lineage_error": ",".join(
                                    sorted(set(missing_lineage))
                                ),
                                "economic_ledger_lineage_error_at": event_time,
                                "allow_new_risk": False,
                            }
                        )
                        if self._trading_mode != "paper":
                            self._set_safe_mode_flag("economic_ledger_lineage", True)
                else:
                    # Cycle summaries and other non-exchange fills are useful
                    # lifecycle evidence but are never invented as economics.
                    self.state_writer.record_execution_event(event_payload)
            except Exception:
                # Do not mutate positions or finalize a trade whose fill could
                # not be written to the ledger.  Exchange reconciliation can
                # safely recover the lifecycle after storage is healthy again.
                logger.exception(
                    "Failed to persist fill event for %s; deferring lifecycle mutation",
                    symbol,
                )
                return
        else:
            try:
                self._execution_event_queue.put_nowait(event_payload)
            except asyncio.QueueFull:
                logger.error("Execution event queue full, dropping event for %s", symbol)
            except Exception as e:
                logger.error("Error queuing execution event for %s: %s", symbol, e)

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
        # A raw partial is one leg of a hedge cycle.  Mutating the aggregate pair
        # quantity here double-counts spot+perp reports and can falsely present a
        # partially hedged position as complete.  The per-leg execution ledger is
        # authoritative until Rust emits the reconciled FILLED_CYCLE summary.
        if status == "PARTIALLY_FILLED" and filled_qty > 0:
            logger.info(
                "Recorded leg-level partial for %s: leg=%s cumulative=%s last_fill=%s",
                symbol,
                _kwargs.get("market"),
                _kwargs.get("cumulative_filled_qty"),
                filled_qty,
            )

        if status != "FILLED":
            return

        is_cycle_complete = self._is_cycle_completion_event(
            _kwargs.get("execution_type"),
            _kwargs.get("spot_fill_price"),
            _kwargs.get("perp_fill_price"),
        )
        if is_cycle_complete and not self._cycle_completion_matches_pending_intent(
            symbol,
            intent_id=_kwargs.get("intent_id"),
            spot_fill_price=_kwargs.get("spot_fill_price"),
            perp_fill_price=_kwargs.get("perp_fill_price"),
        ):
            return
        if (
            symbol in self._exit_events
            or symbol in self._abandoned_exit_intents
            or symbol in self._pending_enters
            or symbol in self._stale_pending_enters
            or symbol in self._abandoned_pending_enters
        ) and not is_cycle_complete:
            logger.info(
                "Ignoring leg-level FILLED for %s until hedge cycle completes (execution_type=%s, spot_fill=%s, perp_fill=%s)",
                symbol,
                _kwargs.get("execution_type"),
                _kwargs.get("spot_fill_price"),
                _kwargs.get("perp_fill_price"),
            )
            return

        # -- Exit fill ----------------------------------------------------------
        if symbol in self._exit_events or symbol in self._abandoned_exit_intents:
            if symbol in self._abandoned_exit_intents:
                logger.warning(
                    "Late FILLED arrived for %s after a paper-mode EXIT intent was auto-cleared; reconciling position now",
                    symbol,
                )
            logger.info("Exit FILLED confirmed for %s - releasing capital slot", symbol)
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

        # -- Entry fill ---------------------------------------------------------
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

    def _set_symbol_safe_mode_reason(
        self,
        symbol: str,
        reason: str,
        active: bool,
    ) -> None:
        symbol = symbol.upper()
        normalized_reason = reason.strip().lower()
        if not symbol or not normalized_reason:
            return
        reasons = self._symbol_safe_mode_reasons.setdefault(symbol, set())
        before = set(reasons)
        if active:
            reasons.add(normalized_reason)
        else:
            reasons.discard(normalized_reason)
        if reasons:
            self._symbol_safe_mode_blocks.add(symbol)
        else:
            self._symbol_safe_mode_reasons.pop(symbol, None)
            self._symbol_safe_mode_blocks.discard(symbol)
        if reasons != before:
            self.state_writer.set_risk_snapshot(
                {f"symbol_block_reasons:{symbol}": sorted(reasons)}
            )

    def _clear_reconciled_position_divergence_blocks(
        self,
        unresolved_symbols: set[str],
    ) -> list[str]:
        """Clear stale divergence latches only after signed exchange proof."""

        unresolved = {str(symbol).upper() for symbol in unresolved_symbols}
        cleared: list[str] = []
        for symbol, reasons in list(self._symbol_safe_mode_reasons.items()):
            if "position_divergence" not in reasons or symbol.upper() in unresolved:
                continue
            self._set_symbol_safe_mode_reason(symbol, "position_divergence", False)
            cleared.append(symbol.upper())
        if cleared:
            logger.info(
                "Signed startup reconciliation cleared stale position-divergence blocks for %s",
                ", ".join(sorted(cleared)),
            )
        return sorted(cleared)

    @staticmethod
    def _depth_feed_source(symbol: str, market: str) -> FeedSource:
        return FeedSource("binance", f"depth_{market.lower()}", symbol.upper())

    def _durable_depth_ready_markets(self, symbol: str) -> set[str]:
        ready: set[str] = set()
        for row in self.feed_cursors.snapshot():
            if str(row.get("symbol") or "").upper() != symbol.upper():
                continue
            stream = str(row.get("stream") or "").lower()
            if stream not in {"depth_spot", "depth_perp"}:
                continue
            if str(row.get("state") or "") == FeedState.READY.value:
                ready.add(stream.removeprefix("depth_"))
        return ready

    def _restore_durable_feed_blocks(self) -> None:
        """Restore scoped entry blocks before any decision can run."""

        for key, raw_reasons in self.state_reader.get_risk().items():
            prefix = "symbol_block_reasons:"
            if not str(key).startswith(prefix):
                continue
            symbol = str(key)[len(prefix) :].strip().upper()
            if not symbol or not isinstance(raw_reasons, list):
                continue
            reasons = {
                str(reason).strip().lower()
                for reason in raw_reasons
                if str(reason).strip()
            }
            if reasons:
                self._symbol_safe_mode_blocks.add(symbol)
                self._symbol_safe_mode_reasons[symbol] = reasons

        gapped_symbols: set[str] = set()
        for row in self.feed_cursors.snapshot():
            symbol = str(row.get("symbol") or "").upper()
            stream = str(row.get("stream") or "").lower()
            state = str(row.get("state") or "")
            if not symbol or stream not in {"depth_spot", "depth_perp"}:
                continue
            market = stream.removeprefix("depth_")
            if state == FeedState.READY.value:
                self._feed_sequence_ready_markets.setdefault(symbol, set()).add(market)
            elif state in {FeedState.GAPPED.value, FeedState.BACKFILLING.value}:
                gapped_symbols.add(symbol)
                self._feed_sequence_ready_markets.setdefault(symbol, set()).discard(market)

        for symbol in gapped_symbols:
            self._symbol_safe_mode_blocks.add(symbol)
            self._symbol_safe_mode_reasons.setdefault(symbol, set()).add(
                "depth_sequence_gap"
            )
        if gapped_symbols:
            logger.warning(
                "Restored durable depth-gap entry blocks for %s; fresh spot and perp "
                "readiness proofs are required",
                ", ".join(sorted(gapped_symbols)),
            )

    def _handle_feed_gap(self, event: dict) -> None:
        symbol = str(event.get("symbol") or "").upper()
        market = str(event.get("market") or "").lower()
        if not symbol:
            return
        affected_markets = {"spot", "perp"}
        ready_before_gap = set(
            self._feed_sequence_ready_markets.get(symbol, set())
        )
        incident_was_active = (
            "depth_sequence_gap"
            in self._symbol_safe_mode_reasons.get(symbol, set())
        )
        self._feed_sequence_ready_markets.setdefault(symbol, set()).difference_update(
            affected_markets
        )
        observed_at = datetime.now(timezone.utc)
        try:
            gap_batch: list[tuple[FeedSource, dict[str, object]]] = []
            for affected_market in sorted(affected_markets):
                is_reported_market = affected_market == market
                gap_batch.append((
                    self._depth_feed_source(symbol, affected_market),
                    {
                        "prior_sequence": (
                            event.get("last_update_id")
                            if is_reported_market
                            else None
                        ),
                        "first_sequence": (
                            event.get("first_update_id")
                            if is_reported_market
                            else None
                        ),
                        "previous_final_sequence": (
                            event.get("previous_final_update_id")
                            if is_reported_market
                            else None
                        ),
                        "final_sequence": (
                            event.get("final_update_id")
                            if is_reported_market
                            else None
                        ),
                        "reason": (
                            str(event.get("reason") or "depth_sequence_gap")
                            if is_reported_market
                            else f"peer_book_invalidated_by_{market or 'unknown'}_gap"
                        ),
                    },
                ))
            self.feed_cursors.record_gap_batch(gap_batch, now=observed_at)
        except Exception:
            # The in-memory block is still armed below.  A persistence failure
            # can never turn a detected market-data gap into an entry permit.
            logger.exception(
                "Could not persist depth sequence gap for %s; retaining entry block",
                symbol,
            )
        self._set_symbol_safe_mode_reason(symbol, "depth_sequence_gap", True)
        if not incident_was_active or ready_before_gap:
            self.state_writer.set_risk_snapshot(
                {f"feed_gap:{symbol}:{market or 'unknown'}": {
                    "active": True,
                    "observed_at": observed_at.isoformat(),
                    "last_update_id": event.get("last_update_id"),
                    "first_update_id": event.get("first_update_id"),
                    "previous_final_update_id": event.get("previous_final_update_id"),
                    "final_update_id": event.get("final_update_id"),
                    "sequence_model": "ranged",
                    "invalidated_markets": sorted(affected_markets),
                }},
            )
            logger.error(
                "Blocked new %s risk after %s depth sequence gap; awaiting both fresh books",
                symbol,
                market or "unknown",
            )

    def _retire_untradable_depth_gap_blocks(
        self,
        tradable_symbols: set[str],
    ) -> set[str]:
        """Retire impossible recovery work after both exchange universes load."""

        if not self._spot_universe_ready_for_entries() or not tradable_symbols:
            return set()
        pinned = set(self._pinned_live_symbols())
        retired: set[str] = set()
        for symbol, reasons in list(self._symbol_safe_mode_reasons.items()):
            if (
                "depth_sequence_gap" not in reasons
                or symbol in tradable_symbols
                or symbol in pinned
            ):
                continue
            for market in ("spot", "perp"):
                self.feed_cursors.retire_source(
                    self._depth_feed_source(symbol, market),
                    reason="absent_from_verified_spot_perp_trading_universe",
                )
            self._feed_sequence_ready_markets.pop(symbol, None)
            self._set_symbol_safe_mode_reason(symbol, "depth_sequence_gap", False)
            self.state_writer.set_risk_snapshot(
                {f"feed_gap:{symbol}:retired": {
                    "active": False,
                    "retired_at": datetime.now(timezone.utc).isoformat(),
                    "reason": "symbol_not_tradable_on_both_venues",
                }}
            )
            retired.add(symbol)
        if retired:
            logger.info(
                "Retired stale depth-gap blocks for untradable symbols: %s",
                ", ".join(sorted(retired)),
            )
        return retired

    def _handle_sequenced_depth_event(self, event: dict) -> None:
        symbol = str(event.get("symbol") or "").upper()
        market = str(event.get("market") or "").lower()
        if not symbol or market not in {"spot", "perp"}:
            return
        if "depth_sequence_gap" not in self._symbol_safe_mode_reasons.get(symbol, set()):
            return
        final_update_id = event.get("final_update_id")
        is_snapshot = bool(event.get("is_snapshot"))
        sequence_contiguous = bool(event.get("sequence_contiguous"))
        if final_update_id is None or not (is_snapshot or sequence_contiguous):
            return
        try:
            proof = self.feed_cursors.record_readiness_proof(
                self._depth_feed_source(symbol, market),
                final_sequence=final_update_id,
                first_sequence=event.get("first_update_id"),
                previous_final_sequence=event.get("previous_final_update_id"),
                is_snapshot=is_snapshot,
                contiguous=sequence_contiguous,
            )
        except Exception:
            logger.exception(
                "Could not persist %s depth readiness proof for %s; retaining entry block",
                market,
                symbol,
            )
            return
        if not proof.accepted:
            return
        ready = self._durable_depth_ready_markets(symbol)
        self._feed_sequence_ready_markets[symbol] = ready
        if ready == {"spot", "perp"}:
            self._set_symbol_safe_mode_reason(symbol, "depth_sequence_gap", False)
            self.state_writer.set_risk_snapshot(
                {f"feed_gap:{symbol}:recovery": {
                    "active": False,
                    "recovered_at": datetime.now(timezone.utc).isoformat(),
                    "proof": "fresh_snapshot_or_contiguous_spot_and_perp_depth",
                }},
            )
            logger.info(
                "Depth sequence recovery proven for %s on both markets; feed-only block cleared",
                symbol,
            )

    def _handle_position_divergence(self, event: dict) -> None:
        symbol = event.get("symbol")
        if not symbol:
            return

        if self._trading_mode == "paper":
            logger.info("Ignoring position divergence for %s in paper mode", symbol.upper())
            self._set_symbol_safe_mode_reason(symbol, "position_divergence", False)
            return

        self._set_symbol_safe_mode_reason(symbol, "position_divergence", True)
        logger.critical(
            "Safe mode block activated for %s due to position divergence: %s (local: %s, exchange: %s)",
            symbol.upper(),
            event.get("divergence_type"),
            event.get("local_qty"),
            event.get("exchange_qty"),
        )

    def _get_open_positions(self, rows: list[dict] | None = None) -> list[OpenPosition]:
        """Returns all OPEN rows including manual_review; downstream consumers must filter by recovery_state if needed."""
        rows = rows if rows is not None else self.state_reader.get_positions()
        positions = []
        for r in rows:
            recovery_state = str(r.get("recovery_state") or "").strip().lower()
            spot_price = r.get("spot_live", 0.0)
            # If spot_live is populated (price > $1), use actual qty * price.
            # Otherwise fall back to configured slot size (e.g., cold start with stale cache).
            if spot_price > 1.0:
                notional_usd = r["qty"] * spot_price
            else:
                notional_usd = CAPITAL_PER_SLOT_USD * TARGET_LEVERAGE
            positions.append(OpenPosition(
                symbol=r["symbol"],
                notional_usd=notional_usd,
                ann_funding=self.funding_ranker.get_rate(r["symbol"]),
                qty=_float_or_zero(r.get("qty")),
                recovery_state=recovery_state,
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
        will never be set - callers rely on ROTATION_CONFIRM_TIMEOUT_S to unblock.
        The CRITICAL log from ExecutionClient is the alert signal.
        """
        symbol_safe_mode_reasons = self._symbol_safe_mode_reasons.get(
            symbol.upper(),
            set(),
        )
        exit_blocking_reasons = symbol_safe_mode_reasons - {"depth_sequence_gap"}
        if exit_blocking_reasons:
            logger.critical(
                "Refusing to dispatch EXIT for %s due to financial-state safe mode "
                "block (%s). Escalate to manual intervention.",
                symbol,
                ", ".join(sorted(exit_blocking_reasons)),
            )
            # Cannot exit, must escalate to global
            self._set_safe_mode_flag("divergence_exit_blocked", True)
            return asyncio.Event()

        event = asyncio.Event()
        position = position_row or next(
            (row for row in self.state_reader.get_positions() if str(row.get("symbol", "")).upper() == symbol.upper()),
            None,
        )
        qty = _float_or_zero(position.get("qty")) if position is not None else 0.0
        
        # If position_row was an OpenPosition object converted to dict, it might be missing 'qty'
        if qty <= 0.0 and position is not None:
            # Re-fetch from DB to be absolutely sure
            db_row = next(
                (row for row in self.state_reader.get_positions() if str(row.get("symbol", "")).upper() == symbol.upper()),
                None,
            )
            if db_row:
                qty = _float_or_zero(db_row.get("qty"))
                position = db_row

        if qty <= 0.0:
            if self._is_startup_recovery_symbol(symbol):
                logger.warning(
                    "Startup recovery EXIT for %s skipped because the local position quantity is already zero; "
                    "clearing the stale recovery row instead of escalating global safe mode",
                    symbol,
                )
            else:
                logger.critical("Refusing to dispatch EXIT for %s without a known position quantity", symbol)
                self._set_safe_mode_flag("exit_failure", True)
            # Register in exit_events and set immediately to prevent infinite loop in trading_loop
            self._exit_events[symbol] = event
            event.set()
            if position is not None:
                logger.warning("Cleaning up zero-quantity position row for %s from DB", symbol)
                self.state_writer.remove_position(symbol)
            self._startup_exit_candidates.pop(symbol, None)
            self._startup_manual_review_symbols.pop(symbol, None)
            self._clear_startup_recovery_exit_tracking(symbol)
            return event
        skip_spot_leg, skip_perp_leg = self._exit_leg_skip_flags(
            symbol,
            direction=direction,
            position_row=position,
        )
        hedge_ratio = 0.0
        if position is not None and direction == "long":
            hedge_ratio = min(1.0, max(0.0, _float_or_zero(position.get("hedge_ratio"))))
        spot_exit_qty = qty * hedge_ratio if direction == "long" else 0.0
        perp_exit_qty = qty
        if spot_exit_qty <= _POSITION_QTY_TOLERANCE or symbol in ["HIGHUSDT", "MBOXUSDT"]:
            skip_spot_leg = True
            spot_exit_qty = 0.0
        if perp_exit_qty <= _POSITION_QTY_TOLERANCE:
            skip_perp_leg = True
            perp_exit_qty = 0.0

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
                "spot_quantity": spot_exit_qty,
                "perp_quantity": perp_exit_qty,
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
        if spot_exit_qty > 0.0:
            payload["spot_quantity"] = spot_exit_qty
        if perp_exit_qty > 0.0:
            payload["perp_quantity"] = perp_exit_qty
        if skip_spot_leg:
            payload["skip_spot_leg"] = True
        if skip_perp_leg:
            payload["skip_perp_leg"] = True
        sent = self.execution.send_order_intent(payload)
        if sent:
            logger.info(
                "EXIT dispatched for %s qty=%.5f spot_qty=%.5f perp_qty=%.5f (urgency=%.1f, direction=%s, skip_spot=%s, skip_perp=%s)",
                symbol,
                qty,
                spot_exit_qty,
                perp_exit_qty,
                urgency,
                direction,
                skip_spot_leg,
                skip_perp_leg,
            )
            self._pending_exit_intents[symbol] = intent_id
            self._pending_exit_created_at[symbol] = created_at
            self.state_writer.update_pending_intent(intent_id, status="PENDING_ACK")
        else:
            logger.critical("EXIT for %s NOT sent - ZMQ down. Position unhedged!", symbol)
            self.state_writer.update_pending_intent(
                intent_id,
                status="FAILED",
                retry_count=1,
                last_error="zmq_send_timeout",
            )
            self._set_safe_mode_flag("execution_bridge", True)
            self._exit_events.pop(symbol, None)
        return event

    def _record_spot_borrow_availability(
        self,
        symbol: str,
        available_usd: float,
        *,
        observed_at: datetime | None = None,
    ) -> None:
        value = float(available_usd)
        if not math.isfinite(value) or value < 0.0:
            raise ValueError("spot borrow availability must be finite and non-negative")
        timestamp = observed_at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        self._spot_borrow_availability_usd[symbol.upper()] = (
            value,
            timestamp.astimezone(timezone.utc),
        )

    def _fresh_spot_borrow_availability_usd(self, symbol: str | None) -> float:
        if not symbol:
            return 0.0
        proof = self._spot_borrow_availability_usd.get(symbol.upper())
        if proof is None:
            return 0.0
        value, observed_at = proof
        age_seconds = (datetime.now(timezone.utc) - observed_at).total_seconds()
        if age_seconds < 0.0 or age_seconds > 60.0:
            return 0.0
        return max(0.0, value)

    def _capital_state_for_reservation(self, *, symbol: str | None = None) -> CapitalState:
        equity = max(
            0.0,
            float(
                self._latest_exchange_account_equity
                or self._config.get("account_equity_usd")
            ),
        )
        max_leverage = max(1.0, float(self._config.get("max_leverage")))
        current_gross = max(0.0, self._current_gross_exposure_usd)
        if self._trading_mode == "paper":
            spot_free = max(0.0, equity - current_gross / 2.0)
            futures_free = max(0.0, equity - current_gross / (2.0 * max_leverage))
        else:
            spot_free = max(
                0.0,
                float(self._latest_exchange_spot_cash_available or 0.0),
            )
            futures_free = max(
                0.0,
                float(self._latest_exchange_available_balance or 0.0),
            )
        return CapitalState(
            equity_usd=equity,
            spot_cash_available_usd=spot_free,
            futures_margin_available_usd=futures_free,
            current_pair_gross_usd=current_gross,
            max_pair_gross_usd=float(self._config.get("max_gross_exposure_usd")),
            current_initial_margin_usd=current_gross / (2.0 * max_leverage),
            spot_borrow_available_usd=self._fresh_spot_borrow_availability_usd(
                symbol
            ),
        )

    def _capital_reservation_policy(self) -> ReservationPolicy:
        equity = max(0.0, float(self._capital_state_for_reservation().equity_usd))
        return ReservationPolicy(
            repair_reserve_usd=max(25.0, equity * 0.05),
            exit_reserve_usd=max(10.0, equity * 0.02),
            minimum_liquidation_buffer_usd=max(50.0, equity * 0.10),
            max_margin_utilization=0.80,
        )

    def _release_entry_reservation(
        self,
        entry: dict,
        *,
        reason: str,
        exchange_terminal_proven: bool,
    ) -> None:
        reservation_id = str(entry.get("reservation_id") or "")
        if not reservation_id:
            return
        try:
            self.capital_reservations.release(
                reservation_id,
                reason=reason,
                exchange_terminal_proven=exchange_terminal_proven,
                evidence={
                    "intent_id": entry.get("intent_id"),
                    "cycle_id": entry.get("cycle_id"),
                    "symbol": entry.get("symbol"),
                },
            )
        except ReservationError:
            logger.exception(
                "Could not release capital reservation %s for %s",
                reservation_id,
                entry.get("symbol"),
            )
            self._set_safe_mode_flag("capital_reservation", True)

    def _dispatch_enter(
        self,
        symbol: str,
        notional_usd: float,
        direction: str = "long",
        ann_funding: float | None = None,
        *,
        cycle_id: str | None = None,
        rotation_entry: bool = False,
    ) -> None:
        """Send ENTER instruction. Skips if no mark price has been received yet."""
        symbol = symbol.upper()
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
        if symbol.upper() in self._symbol_safe_mode_blocks:
            logger.warning("Skipping ENTER for %s due to per-symbol safe mode block (divergence)", symbol)
            return

        mark_price = self._mark_prices.get(symbol, 0.0)
        if mark_price <= 0.0:
            logger.warning(
                "No mark price for %s yet - skipping ENTER (will retry next cycle)", symbol
            )
            return
        per_leg_notional_usd = self._per_leg_notional_usd(notional_usd)
        raw_qty = per_leg_notional_usd / mark_price
        step = self._lot_step.get(symbol, 1e-5)
        qty = self._round_to_step(raw_qty, step)
        if qty <= 0.0:
            logger.warning(
                "Rounded quantity for %s is 0 (raw=%.8f, step=%s) - skipping ENTER",
                symbol,
                raw_qty,
                step,
            )
            return

        # -- Prospective exposure guard ----------------------------------------
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
                "ENTER blocked for %s - projected gross $%.0f would exceed limit $%.0f "
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
                "ENTER blocked for %s - projected symbol notional $%.0f would exceed "
                "per-symbol cap $%.0f (open=$%.0f, new=$%.0f)",
                symbol,
                existing_symbol_gross + notional_usd,
                per_symbol_cap,
                existing_symbol_gross,
                notional_usd,
            )
            return

        effective_ann_funding = self.funding_ranker.get_rate(symbol) if ann_funding is None else ann_funding
        entry_allowed, entry_reasons, entry_metrics = self._entry_safety_decision(
            symbol,
            notional_usd,
            effective_ann_funding,
        )
        if not entry_allowed:
            logger.info(
                "ENTER rejected for %s - %s | net_edge=%.2fbps cost=%.2fbps spread=%.2fbps depth=$%.0f age=%.1fs hold=%.2fh",
                symbol,
                "; ".join(entry_reasons),
                entry_metrics["predicted_net_edge_bps"],
                entry_metrics["round_trip_cost_bps"],
                entry_metrics["spread_bps"],
                entry_metrics["entry_depth_usd"],
                entry_metrics["data_age_s"],
                entry_metrics["expected_holding_hours"],
            )
            self.state_writer.set_risk_snapshot(
                {
                    "last_entry_reject_symbol": symbol,
                    "last_entry_reject_reasons": entry_reasons,
                    "last_entry_reject_metrics": entry_metrics,
                    "last_entry_reject_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            return

        intent = "ENTER_SHORT" if direction == "short" else "ENTER_LONG"
        intent_id = self._next_intent_id(symbol, intent)
        # Direct dispatches may be retried within a single OS clock tick.  Use
        # the unique intent identity rather than a timestamp so each immutable
        # decision has a distinct ledger cycle.
        durable_cycle_id = str(cycle_id or f"direct:{intent_id}")
        decision_id = f"decision:{intent_id}"
        candidate_snapshot = (
            self.state_reader.get_candidate_snapshot(durable_cycle_id, symbol)
            if cycle_id
            else None
        )
        self.state_writer.record_execution_decision(
            decision_id=decision_id,
            cycle_id=durable_cycle_id,
            symbol=symbol,
            direction=direction,
            action=intent,
            accepted=True,
            config_version_hash=self._config.version_hash,
            model_version=f"legacy-selector+{OPPORTUNITY_KERNEL_VERSION}",
            payload={
                "notional_usd": notional_usd,
                "quantity": qty,
                "ann_funding": effective_ann_funding,
                "entry_safety_metrics": entry_metrics,
                "opportunity_kernel_version": OPPORTUNITY_KERNEL_VERSION,
                "candidate_cycle_present": candidate_snapshot is not None,
                "candidate_cycle_accepted": (
                    bool(candidate_snapshot.get("accepted"))
                    if candidate_snapshot is not None
                    else None
                ),
                "rotation_entry": rotation_entry,
            },
        )
        entry_depth_usd = self._cost_depth_or_default(entry_metrics["entry_depth_usd"])
        entry_spread_bps = entry_metrics["spread_bps"]
        maker_fill_probability = entry_metrics["maker_fill_probability"]
        entry_metadata = {
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "entry_price": mark_price,
            "qty": qty,
            "direction": direction,
            "ann_funding": effective_ann_funding,
            "estimated_entry_cost_usd": blended_entry_cost(
                per_leg_notional_usd,
                depth_usd=entry_depth_usd,
                spread_bps=entry_spread_bps,
                maker_fill_probability=maker_fill_probability,
            ),
            "entry_safety_metrics": entry_metrics,
            "intent_id": intent_id,
            "symbol": symbol,
            "cycle_id": durable_cycle_id,
            "decision_id": decision_id,
        }
        reservation_id = f"reservation:{intent_id}"
        entry_metadata["reservation_id"] = reservation_id
        max_leverage = max(1.0, float(self._config.get("max_leverage")))
        estimated_fees_usd = max(
            float(entry_metadata["estimated_entry_cost_usd"]),
            notional_usd
            * max(0.0, float(entry_metrics.get("round_trip_cost_bps") or 0.0))
            / 10_000.0,
        )
        reservation = self.capital_reservations.reserve(
            ReservationRequest(
                reservation_id=reservation_id,
                purpose=(
                    ReservationPurpose.ROTATION_ENTRY
                    if rotation_entry
                    else ReservationPurpose.ENTRY
                ),
                symbol=symbol,
                cycle_id=durable_cycle_id,
                spot_cash_usd=(
                    per_leg_notional_usd if direction != "short" else 0.0
                ),
                spot_borrow_usd=(
                    per_leg_notional_usd if direction == "short" else 0.0
                ),
                futures_margin_usd=per_leg_notional_usd / max_leverage,
                fees_usd=estimated_fees_usd,
                pair_gross_increment_usd=notional_usd,
                config_version=self._config.version_hash,
                expires_at=(
                    datetime.now(timezone.utc)
                    + timedelta(seconds=self._intent_timeout_seconds())
                ).isoformat(),
                metadata={
                    "decision_id": decision_id,
                    "intent_id": intent_id,
                    "direction": direction,
                },
            ),
            capital=self._capital_state_for_reservation(symbol=symbol),
            policy=self._capital_reservation_policy(),
        )
        if not reservation.allowed:
            logger.warning(
                "ENTER blocked for %s by capital reservation: %s",
                symbol,
                ", ".join(reservation.reasons),
            )
            self.state_writer.set_risk_snapshot(
                {
                    "last_capital_reservation_reject": {
                        "symbol": symbol,
                        "cycle_id": durable_cycle_id,
                        "reasons": list(reservation.reasons),
                    }
                }
            )
            return
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
        self.capital_reservations.mark_dispatched(
            reservation_id,
            evidence={"intent_id": intent_id, "decision_id": decision_id},
        )
        sent = self.execution.send_order_intent({
            "symbol": symbol,
            "intent": intent,
            "quantity": qty,
            "urgency": 0.8,
            "max_slippage_bps": entry_metrics["max_slippage_bps"],
            "exposure_scale": 1.0,
            "intent_id": intent_id,
            "cycle_id": durable_cycle_id,
        })
        if sent:
            logger.info(
                "ENTER dispatched for %s qty=%.5f (gross_notional=$%.0f, leg_notional=$%.0f, price=$%.2f, direction=%s, net_edge=%.2fbps, cost=%.2fbps, spread=%.2fbps, depth=$%.0f, hold=%.2fh)",
                symbol,
                qty,
                notional_usd,
                per_leg_notional_usd,
                mark_price,
                direction,
                entry_metrics["predicted_net_edge_bps"],
                entry_metrics["round_trip_cost_bps"],
                entry_metrics["spread_bps"],
                entry_metrics["entry_depth_usd"],
                entry_metrics["expected_holding_hours"],
            )
            self._pending_enters[symbol] = dict(entry_metadata)
            self.state_writer.update_pending_intent(intent_id, status="PENDING_ACK")
        else:
            logger.critical("ENTER for %s NOT sent - ZMQ down.", symbol)
            self.capital_reservations.mark_delivery_unknown(
                reservation_id,
                evidence={"intent_id": intent_id, "reason": "zmq_send_timeout"},
            )
            self.state_writer.update_pending_intent(
                intent_id,
                status="FAILED",
                retry_count=1,
                last_error="zmq_send_timeout",
            )
            self._set_safe_mode_flag("execution_bridge", True)
            return

    async def _await_exit_confirmation(self, symbol: str) -> bool:
        """Wait for FILLED event. Returns True if confirmed, False on timeout or rejection."""
        event = self._exit_events.get(symbol)
        if event is None:
            return False
        try:
            await asyncio.wait_for(event.wait(), timeout=ROTATION_CONFIRM_TIMEOUT_S)
            if symbol in self._exit_rejections:
                self._exit_rejections.discard(symbol)
                logger.warning("Exit for %s rejected by Rust - entry will be deferred", symbol)
                return False
            return True
        except asyncio.TimeoutError:
            logger.warning("Exit confirmation timeout for %s - entry will be deferred", symbol)
            pending_intent_id = self._pending_exit_intents.get(symbol)
            if pending_intent_id:
                self.state_writer.update_pending_intent(
                    pending_intent_id,
                    status="TIMEOUT",
                    last_error="exit_confirmation_timeout",
                )
            return False
        finally:
            self._exit_rejections.discard(symbol)

    async def _maybe_recompound(self) -> None:
        import time
        if time.time() - self._last_compound_check < 86400:
            return
        self._last_compound_check = time.time()
        equity = self.state_reader.get_account_equity()
        if equity and equity > 0:
            current_capital = float(self.allocator._capital_per_slot)
            proposed_capital = min(
                equity / MAX_CONCURRENT_POSITIONS,
                float(self._config.get("per_symbol_notional_cap_usd"))
                / max(1.0, TARGET_LEVERAGE),
                MAX_NOTIONAL_PER_TRADE / max(1.0, TARGET_LEVERAGE),
            )
            if proposed_capital >= current_capital:
                self.state_writer.set_risk_snapshot(
                    {
                        "compound_status": "increase_blocked_pending_promotion",
                        "compound_current_capital_per_slot": current_capital,
                        "compound_proposed_capital_per_slot": proposed_capital,
                        "compound_checked_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                return
            new_capital = proposed_capital
            self.allocator = PortfolioAllocator(
                self.depth_tracker,
                self.funding_ranker,
                capital_per_slot_usd=new_capital,
                per_symbol_cap_usd=float(self._config.get("per_symbol_notional_cap_usd")),
            )
            logger.warning(
                "Capital de-risk: equity=%.2f, capital_per_slot %.2f -> %.2f",
                equity,
                current_capital,
                new_capital,
            )
            
            cap = float(self._config.get("per_symbol_notional_cap_usd"))
            if new_capital * TARGET_LEVERAGE > cap:
                logger.warning(
                    "RECOMPOUND OVERSHOOT: capital_per_slot * leverage (%.2f) > per_symbol_cap (%.2f). "
                    "Allocator will cap base_target_notional but entries may be sparse if too many symbols are filtered.",
                    new_capital * TARGET_LEVERAGE,
                    cap,
                )

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
                        logger.warning("Sentiment score is non-finite (%s) - resetting to neutral", raw)
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
        validation_scale = max(
            0.1,
            min(1.0, _float_or_zero(self._validation_policy_snapshot()["validation_position_scale"]) or 1.0),
        )
        return max(0.1, min(1.0, min(adaptive_scale, self._risk_position_scale, validation_scale)))

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
        # Durable depth-gap blocks need live traffic to prove recovery. Pin
        # their symbols until both venue cursors are READY, even when the
        # current funding ranking would otherwise omit them.
        tradable_symbols = self._tradable_trade_symbols()
        for symbol, reasons in sorted(self._symbol_safe_mode_reasons.items()):
            if "depth_sequence_gap" not in reasons:
                continue
            if tradable_symbols and symbol.upper() not in tradable_symbols:
                continue
            pinned.append(symbol.upper())
        return list(dict.fromkeys(pinned))

    def _request_depth_recovery_subscriptions(self) -> int:
        """Ask Rust for feeds needed to clear durable gaps without placing orders."""

        requested = 0
        tradable_symbols = self._tradable_trade_symbols()
        for symbol, reasons in sorted(self._symbol_safe_mode_reasons.items()):
            normalized = symbol.upper()
            if "depth_sequence_gap" not in reasons:
                continue
            if tradable_symbols and normalized not in tradable_symbols:
                continue
            if self.execution.subscribe_market_data(normalized):
                requested += 1
        if requested:
            logger.info(
                "Requested side-effect-free market-data recovery for %d blocked symbols",
                requested,
            )
        return requested

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
        minutes_to_next_snap = max(
            0.1,
            self.funding_ranker.minutes_to_next_settlement(symbol),
        )
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
        minutes_to_next_snapshot = self._minutes_to_next_funding_snapshot(symbol)
        if minutes_to_next_snapshot <= 15.0:
            return f"only {minutes_to_next_snapshot:.0f} minutes to next funding snapshot"
        return None

    def _minutes_to_next_funding_snapshot(self, symbol: str | None = None) -> float:
        if symbol:
            return self.funding_ranker.minutes_to_next_settlement(symbol)
        return max(0.0, FUNDING_INTERVAL_HOURS * 60 - self._minutes_since_last_snapshot())

    def _entry_safety_decision(
        self,
        symbol: str,
        notional_usd: float,
        ann_funding: float | None,
    ) -> tuple[bool, list[str], dict[str, Any]]:
        symbol = symbol.upper()
        reasons: list[str] = []
        metrics: dict[str, Any] = {}
        ann_funding = self.funding_ranker.get_rate(symbol) if ann_funding is None else float(ann_funding)
        decision_time = datetime.now(timezone.utc)
        per_leg_notional_usd = self._per_leg_notional_usd(notional_usd)
        entry_depth_usd = self.depth_tracker.get_entry_depth(symbol)
        required_depth_usd = max(
            float(self._config.get("scanner_min_depth_usd")),
            notional_usd * float(self._config.get("scanner_min_depth_multiplier")),
        )
        data_age_s = self.depth_tracker.entry_data_age_seconds(symbol)
        max_data_age_s = float(self._config.get("scanner_max_data_stale_seconds"))
        spread_bps = self.depth_tracker.entry_spread_bps(symbol)
        max_spread_bps = float(self._config.get("scanner_max_spread_bps"))
        max_toxic_spread_bps = float(self._config.get("scanner_max_toxic_spread_bps"))
        basis_pct = self.depth_tracker.basis_pct(symbol)
        basis_bps = basis_pct * 10_000.0 if basis_pct is not None else float("nan")
        minutes_to_next_snapshot = self._minutes_to_next_funding_snapshot(symbol)
        funding_interval_hours = float(self.funding_ranker.calendar.interval_hours(symbol))
        # The baseline evaluates one complete, symbol-specific funding interval.
        # Cash is credited only at exact settlement instants inside the horizon;
        # time before settlement is exposure, not continuously earned funding.
        expected_holding_hours = max(0.0, funding_interval_hours)
        max_slippage_bps = float(self._config.get("execution_default_max_slippage_bps"))
        maker_fill_probability = (
            float(self._config.get("maker_fill_probability"))
            if self._trading_mode == "paper"
            else 0.0
        )

        if not self.depth_tracker.has_entry_book(symbol):
            reasons.append("incomplete spot/perp orderbook")
        if not math.isfinite(data_age_s) or data_age_s > max_data_age_s:
            age_text = "missing" if not math.isfinite(data_age_s) else f"{data_age_s:.1f}s"
            reasons.append(f"stale orderbook data age={age_text} max={max_data_age_s:.1f}s")
        if entry_depth_usd < required_depth_usd:
            reasons.append(
                f"entry depth ${entry_depth_usd:,.0f} below required ${required_depth_usd:,.0f}"
            )
        if not math.isfinite(spread_bps):
            reasons.append("invalid spot/perp spread")
        elif spread_bps > max_spread_bps:
            reasons.append(f"combined entry spread {spread_bps:.2f}bps exceeds {max_spread_bps:.2f}bps")
        if math.isfinite(spread_bps) and spread_bps > max_toxic_spread_bps:
            reasons.append(f"toxic entry spread {spread_bps:.2f}bps exceeds {max_toxic_spread_bps:.2f}bps")
        if math.isfinite(basis_bps) and basis_bps > max_toxic_spread_bps:
            reasons.append(f"basis premium {basis_bps:.2f}bps exceeds toxic threshold {max_toxic_spread_bps:.2f}bps")

        cost_depth_usd = self._cost_depth_or_default(entry_depth_usd)
        cost_spread_bps = (
            spread_bps if math.isfinite(spread_bps) else max_toxic_spread_bps
        )
        entry_execution_cost_usd = blended_entry_cost(
            per_leg_notional_usd,
            depth_usd=cost_depth_usd,
            spread_bps=cost_spread_bps,
            maker_fill_probability=maker_fill_probability,
        )
        exit_execution_cost_usd = blended_exit_cost(
            per_leg_notional_usd,
            depth_usd=cost_depth_usd,
            spread_bps=cost_spread_bps,
            maker_fill_probability=maker_fill_probability,
        )
        min_required_edge_bps = float(self._config.get("min_expected_edge_bps")) + max_slippage_bps

        funding_status = self.funding_ranker.status_snapshot()
        schedule = self.funding_ranker.calendar.snapshot().get(symbol, {})
        next_settlement_time = self._parse_timestamp(
            str(schedule.get("next_funding_time") or "")
        )
        schedule_observed_at = self._parse_timestamp(
            str(schedule.get("updated_at") or "")
        )
        funding_info_observed_at = self.funding_ranker.funding_info_observed_at()
        metadata_times = [
            value
            for value in (schedule_observed_at, funding_info_observed_at)
            if value is not None
        ]
        calendar_observed_at = min(metadata_times) if len(metadata_times) == 2 else None
        calendar_authoritative = bool(
            funding_status.get("funding_metadata_ready")
            and next_settlement_time is not None
            and calendar_observed_at is not None
        )
        horizon_end = decision_time + timedelta(hours=max(0.0, funding_interval_hours))
        try:
            settlement_times = self.funding_ranker.calendar.settlements_between(
                symbol,
                decision_time,
                horizon_end,
            )
        except ValueError:
            settlement_times = []
        raw_funding_rate = self.funding_ranker.get_raw_rate(symbol)
        evaluation_input = OpportunityEvaluationInput(
            symbol=symbol,
            direction="long_spot_short_perp",
            decision_time=decision_time,
            horizon_end=horizon_end,
            pair_gross_notional_usd=notional_usd,
            funding_liable_notional_usd=per_leg_notional_usd,
            settlement_interval_hours=funding_interval_hours,
            settlements=tuple(
                SettlementExpectation(
                    settlement_time=value,
                    expected_rate=raw_funding_rate,
                    eligibility_probability=1.0,
                    source_event_id=f"{symbol}:{value.isoformat()}",
                )
                for value in settlement_times
            ),
            entry_execution_cost_pct=(
                entry_execution_cost_usd / per_leg_notional_usd
                if per_leg_notional_usd > 0.0
                else 0.0
            ),
            exit_execution_cost_pct=(
                exit_execution_cost_usd / per_leg_notional_usd
                if per_leg_notional_usd > 0.0
                else 0.0
            ),
            minimum_net_edge_bps=min_required_edge_bps,
            calendar_authoritative=calendar_authoritative,
            calendar_observed_at=calendar_observed_at,
            funding_rate_observed_at=self.funding_ranker.rate_observed_at(symbol),
            max_calendar_age_seconds=float(MAX_FUNDING_STALENESS_MINUTES * 60),
            max_funding_rate_age_seconds=float(MAX_FUNDING_STALENESS_MINUTES * 60),
        )
        adapter = (
            PAPER_OPPORTUNITY_ADAPTER
            if self._trading_mode == "paper"
            else LIVE_OPPORTUNITY_ADAPTER
        )
        evaluation = adapter.evaluate(evaluation_input)
        predicted_net_edge_bps = evaluation.net_edge_bps
        round_trip_cost_bps = (
            evaluation.total_cost_usd / per_leg_notional_usd * 10_000.0
            if per_leg_notional_usd > 0.0
            else 0.0
        )
        reasons.extend(
            f"opportunity_kernel:{code}" for code in evaluation.reason_codes
        )
        if evaluation.valid and predicted_net_edge_bps < min_required_edge_bps:
            reasons.append(
                "expected net edge "
                f"{predicted_net_edge_bps:.2f}bps below required {min_required_edge_bps:.2f}bps "
                f"(round_trip_cost={round_trip_cost_bps:.2f}bps, hold={expected_holding_hours:.2f}h)"
            )

        metrics.update(
            {
                "ann_funding": ann_funding,
                "basis_bps": basis_bps if math.isfinite(basis_bps) else 0.0,
                "data_age_s": data_age_s if math.isfinite(data_age_s) else 99_999.0,
                "entry_depth_usd": entry_depth_usd,
                "expected_holding_hours": expected_holding_hours,
                "maker_fill_probability": maker_fill_probability,
                "max_slippage_bps": max_slippage_bps,
                "min_required_edge_bps": min_required_edge_bps,
                "notional_usd": notional_usd,
                "per_leg_notional_usd": per_leg_notional_usd,
                "predicted_net_edge_bps": predicted_net_edge_bps,
                "predicted_pnl_usd": evaluation.net_ev_usd,
                "required_depth_usd": required_depth_usd,
                "round_trip_cost_bps": round_trip_cost_bps,
                "spread_bps": spread_bps if math.isfinite(spread_bps) else 10_000.0,
                "opportunity_kernel_version": evaluation.kernel_version,
                "opportunity_kernel_valid": evaluation.valid,
                "settlement_count": evaluation.settlement_count,
                "raw_funding_rate": raw_funding_rate,
                "gross_funding_edge_bps": evaluation.gross_funding_edge_bps,
                "net_edge_pair_gross_bps": evaluation.net_edge_pair_gross_bps,
                "hours_to_first_settlement": (
                    max(
                        0.0,
                        (
                            evaluation.first_settlement_time - decision_time
                        ).total_seconds()
                        / 3_600.0,
                    )
                    if evaluation.first_settlement_time is not None
                    else 99_999.0
                ),
            }
        )
        return not reasons, list(dict.fromkeys(reasons)), metrics

    def _symbol_entry_gate_reasons(
        self,
        symbol: str,
        ann_funding: float,
        *,
        entry_threshold: float,
        target_notional_usd: float | None = None,
    ) -> list[str]:
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
        if target_notional_usd is None:
            target_notional_usd = min(
                self.allocator._capital_per_slot * TARGET_LEVERAGE * max(0.1, self._effective_notional_scale()),
                MAX_NOTIONAL_PER_TRADE,
                float(self._config.get("per_symbol_notional_cap_usd")),
            )
        _allowed, safety_reasons, _metrics = self._entry_safety_decision(
            symbol,
            float(target_notional_usd),
            ann_funding,
        )
        reasons.extend(safety_reasons)
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
        shadow_scores: list[OpportunityScore] = []
        shadow_routes: list[ShadowDecision] = []
        shadow_net_ev_decisions: list[ShadowDecision] = []
        shadow_portfolio_candidates: list[PortfolioCandidate] = []
        shadow_net_ev_scores_by_symbol: dict[str, NetEVScore] = {}
        settlement_forecasts_by_symbol: dict[str, SettlementForecast] = {}
        plugin_contexts: list[StrategyContext] = []
        try:
            decision_time = datetime.fromisoformat(cycle_id.replace("Z", "+00:00"))
            if decision_time.tzinfo is None:
                decision_time = decision_time.replace(tzinfo=timezone.utc)
            decision_time = decision_time.astimezone(timezone.utc)
        except (TypeError, ValueError):
            decision_time = datetime.now(timezone.utc)
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
                            float(self._config.get("per_symbol_notional_cap_usd")),
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
            target_notional = (
                candidate_notional_overrides.get(symbol)
                if candidate_notional_overrides is not None
                else None
            )
            if target_notional is None:
                target_notional = min(
                    self.allocator._capital_per_slot
                    * TARGET_LEVERAGE
                    * max(0.1, self._effective_notional_scale()),
                    MAX_NOTIONAL_PER_TRADE,
                    float(self._config.get("per_symbol_notional_cap_usd")),
                )
            _shadow_allowed, _shadow_reasons, shadow_metrics = self._entry_safety_decision(
                symbol,
                float(target_notional),
                ann_funding,
            )
            spread_bps = float(shadow_metrics.get("spread_bps", 10_000.0))
            historical_var_pct = _float_or_zero(self._historical_var_fraction(symbol))
            data_age_seconds = self.depth_tracker.entry_data_age_seconds(symbol)
            book_age_ms = (
                int(max(0.0, data_age_seconds) * 1_000.0)
                if math.isfinite(data_age_seconds)
                else 2_147_483_647
            )
            route_recommendation = self.route_optimizer.recommend(
                RouteInputs(
                    symbol=symbol,
                    notional_usd=float(target_notional),
                    spot_spread_bps=self.depth_tracker.spot_spread_bps(symbol),
                    perp_spread_bps=self.depth_tracker.perp_spread_bps(symbol),
                    spot_depth_usd=self.depth_tracker.spot_ask_depth(symbol),
                    perp_depth_usd=self.depth_tracker.perp_bid_depth(symbol),
                    book_age_ms=book_age_ms,
                    filters_ready=(
                        symbol in self._tradable_trade_symbols()
                        and _float_or_zero(self._lot_step.get(symbol)) > 0.0
                    ),
                    seconds_to_settlement=max(
                        0.0,
                        self.funding_ranker.minutes_to_next_settlement(symbol) * 60.0,
                    ),
                    settlement_value_bps=max(
                        0.0,
                        float(shadow_metrics.get("gross_funding_edge_bps", 0.0)),
                    ),
                    volatility_bps_per_second=(
                        historical_var_pct * 100.0 / math.sqrt(86_400.0)
                    ),
                    adverse_markout_bps=max(0.0, spread_bps * 0.10),
                    urgency=0.0,
                    max_book_age_ms=int(
                        max(
                            100.0,
                            float(self._config.get("scanner_max_data_stale_seconds"))
                            * 1_000.0,
                        )
                    ),
                )
            )
            selected_route = route_recommendation.selected_estimate
            shadow_routes.append(
                ShadowDecision(
                    trade_id=f"{cycle_id}:route:{symbol}",
                    symbol=symbol,
                    action=f"ROUTE_{route_recommendation.selected.value.upper()}",
                    hold_score=0.0,
                    exit_score=(
                        selected_route.total_objective_bps
                        if selected_route is not None
                        else 1_000_000_000.0
                    ),
                    incremental_value_usd=(
                        -float(target_notional)
                        * selected_route.total_objective_bps
                        / 10_000.0
                        if selected_route is not None
                        else 0.0
                    ),
                    recommended=(route_recommendation.selected is not RoutePolicy.NONE),
                    metadata={
                        "shadow_only": True,
                        "reason": route_recommendation.reason,
                        "book_age_ms": book_age_ms,
                        "estimates": [
                            {
                                "policy": estimate.policy.value,
                                "feasible": estimate.feasible,
                                "expected_cost_bps": estimate.expected_cost_bps,
                                "missed_settlement_bps": estimate.missed_settlement_bps,
                                "hedge_risk_notional_ms": estimate.hedge_risk_notional_ms,
                                "completion_ms": estimate.expected_completion_ms,
                                "reasons": list(estimate.reasons),
                            }
                            for estimate in route_recommendation.estimates
                        ],
                    },
                )
            )

            self.settlement_model.observe(
                FundingObservation(
                    symbol=symbol,
                    available_at=decision_time,
                    annualized_rate=ann_funding,
                    basis_pct=self.depth_tracker.basis_pct(symbol),
                    realized_volatility=historical_var_pct,
                    source_event_id=f"{cycle_id}:{symbol}",
                )
            )
            seconds_to_settlement = max(
                0.0,
                self.funding_ranker.minutes_to_next_settlement(symbol) * 60.0,
            )
            settlement_forecast = self.settlement_model.forecast(
                symbol=symbol,
                decision_time=decision_time,
                # Include the next discrete settlement despite small clock
                # movement between calendar reads.
                horizon_hours=max(1.0 / 3_600.0, (seconds_to_settlement + 1.0) / 3_600.0),
                notional_usd=float(target_notional),
                direction="long_spot_short_perp",
                calendar=self.funding_ranker.calendar,
            )
            round_trip_cost_bps = max(
                0.0, float(shadow_metrics.get("round_trip_cost_bps", 0.0))
            )
            net_ev_score = self.net_ev_scorer.score(
                CandidateEconomics(
                    symbol=symbol,
                    notional_usd=float(target_notional),
                    settlement_forecast=settlement_forecast,
                    entry_cost_bps=round_trip_cost_bps / 2.0,
                    exit_cost_bps=round_trip_cost_bps / 2.0,
                    basis_risk_bps=max(0.0, historical_var_pct * 10_000.0),
                    execution_uncertainty_bps=(
                        selected_route.expected_cost_bps * 0.25
                        if selected_route is not None
                        else max(5.0, spread_bps if math.isfinite(spread_bps) else 100.0)
                    ),
                    capacity_usd=max(
                        0.0,
                        float(
                            shadow_metrics.get(
                                "entry_depth_usd",
                                self.depth_tracker.get_entry_depth(symbol),
                            )
                        ),
                    ),
                    model_confidence=min(1.0, settlement_forecast.sample_count / 30.0),
                    input_age_seconds=(
                        data_age_seconds if math.isfinite(data_age_seconds) else 1e9
                    ),
                    max_input_age_seconds=float(
                        self._config.get("scanner_max_data_stale_seconds")
                    ),
                    active_baseline_net_edge_bps=float(
                        shadow_metrics.get("predicted_net_edge_bps", 0.0)
                    ),
                )
            )
            shadow_net_ev_scores_by_symbol[symbol] = net_ev_score
            settlement_forecasts_by_symbol[symbol] = settlement_forecast
            shadow_net_ev_decisions.append(
                ShadowDecision(
                    trade_id=f"{cycle_id}:net-ev:{symbol}",
                    symbol=symbol,
                    action="NET_EV_ENTER" if net_ev_score.eligible else "NET_EV_REJECT",
                    hold_score=net_ev_score.mean_net_ev_usd,
                    exit_score=net_ev_score.lower_bound_net_ev_usd,
                    incremental_value_usd=net_ev_score.lower_bound_net_ev_usd,
                    recommended=net_ev_score.eligible,
                    metadata={
                        "shadow_only": True,
                        "reason_codes": list(net_ev_score.reason_codes),
                        "explanation": net_ev_score.explanation,
                        "components_usd": net_ev_score.components_usd,
                        "uncertainty_usd": net_ev_score.uncertainty_usd,
                        "executable_notional_usd": net_ev_score.executable_notional_usd,
                        "forecast_sample_count": settlement_forecast.sample_count,
                        "prospective_settlements": len(settlement_forecast.payments),
                    },
                )
            )
            settlement_cluster = (
                settlement_forecast.payments[0].settlement_time.strftime("%H:%M")
                if settlement_forecast.payments
                else "unknown"
            )
            executable_capacity = max(
                0.0,
                float(
                    shadow_metrics.get(
                        "entry_depth_usd",
                        self.depth_tracker.get_entry_depth(symbol),
                    )
                ),
            )
            shadow_portfolio_candidates.append(
                PortfolioCandidate(
                    symbol=symbol,
                    net_ev_lcb_usd=net_ev_score.lower_bound_net_ev_usd,
                    requested_notional_usd=float(target_notional),
                    executable_capacity_usd=executable_capacity,
                    confidence=min(1.0, settlement_forecast.sample_count / 30.0),
                    cluster=self._candidate_cluster(symbol),
                    settlement_cluster=settlement_cluster,
                    liquidity_tier=(
                        "high"
                        if executable_capacity >= float(target_notional) * 5.0
                        else "low"
                    ),
                    venue="binance",
                    basis_stress_pct=max(0.005, historical_var_pct),
                    funding_reversal_loss_usd=abs(
                        settlement_forecast.expected_payment_usd
                    ),
                )
            )
            plugin_contexts.append(
                StrategyContext(
                    decision_time=decision_time,
                    symbol=symbol,
                    venue="binance",
                    requested_notional_usd=float(target_notional),
                    executable_capacity_usd=executable_capacity,
                    entry_exit_cost_usd=(
                        round_trip_cost_bps * float(target_notional) / 10_000.0
                    ),
                    basis_pct=_float_or_zero(self.depth_tracker.basis_pct(symbol)),
                    expected_exit_basis_pct=0.0,
                    basis_cvar_usd=(
                        historical_var_pct * float(target_notional)
                    ),
                    seconds_to_settlement=seconds_to_settlement,
                    settlement_forecast=settlement_forecast,
                    config_hash=self._config.version_hash,
                    model_hash=hashlib.sha256(
                        b"settlement-baseline-v1"
                    ).hexdigest(),
                )
            )

            snapshot = CandidateSnapshot(
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
                        "active_selector_rank": rank,
                        "active_gate_predicted_net_edge_bps": float(
                            shadow_metrics.get("predicted_net_edge_bps", 0.0)
                        ),
                        "active_gate_round_trip_cost_bps": float(
                            shadow_metrics.get("round_trip_cost_bps", 0.0)
                        ),
                        "active_gate_expected_holding_hours": float(
                            shadow_metrics.get("expected_holding_hours", 0.0)
                        ),
                        "active_gate_required_edge_bps": float(
                            shadow_metrics.get("min_required_edge_bps", 0.0)
                        ),
                        "active_opportunity_kernel_version": str(
                            shadow_metrics.get(
                                "opportunity_kernel_version",
                                OPPORTUNITY_KERNEL_VERSION,
                            )
                        ),
                        "active_gate_kernel_valid": bool(
                            shadow_metrics.get("opportunity_kernel_valid", False)
                        ),
                        "active_gate_settlement_count": int(
                            shadow_metrics.get("settlement_count", 0)
                        ),
                        "active_gate_raw_funding_rate": float(
                            shadow_metrics.get("raw_funding_rate", 0.0)
                        ),
                        "active_gate_gross_funding_edge_bps": float(
                            shadow_metrics.get("gross_funding_edge_bps", 0.0)
                        ),
                        "shadow_route": route_recommendation.selected.value,
                        "shadow_route_reason": route_recommendation.reason,
                        "shadow_route_cost_bps": (
                            selected_route.total_objective_bps
                            if selected_route is not None
                            else None
                        ),
                        "shadow_route_hedge_risk_notional_ms": (
                            selected_route.hedge_risk_notional_ms
                            if selected_route is not None
                            else None
                        ),
                        "shadow_net_ev_mean_usd": net_ev_score.mean_net_ev_usd,
                        "shadow_net_ev_lcb_usd": net_ev_score.lower_bound_net_ev_usd,
                        "shadow_net_ev_lcb_bps": net_ev_score.lower_bound_net_edge_bps,
                        "shadow_net_ev_eligible": net_ev_score.eligible,
                        "shadow_net_ev_reasons": list(net_ev_score.reason_codes),
                        "shadow_settlement_count": len(settlement_forecast.payments),
                        "shadow_settlement_model_samples": settlement_forecast.sample_count,
                },
                snapshot_time=datetime.now(timezone.utc).isoformat(),
                rank=rank,
            )
            snapshots.append(snapshot)
            predicted_net_edge_bps = float(
                shadow_metrics.get("predicted_net_edge_bps", 0.0)
            )
            required_depth = max(
                float(shadow_metrics.get("required_depth_usd", 0.0)),
                1e-9,
            )
            shadow_scores.append(
                OpportunityScore(
                    cycle_id=cycle_id,
                    symbol=symbol,
                    # Phase-0 shadow score deliberately mirrors the active
                    # economic gate.  It is observational only; later phases
                    # can add uncertainty and portfolio terms without changing
                    # the legacy selector silently.
                    # Require a minimally representative online history before
                    # the new score can even rank in shadow.  Until then the
                    # legacy net-edge score remains the explicit baseline.
                    total_score=(
                        net_ev_score.lower_bound_net_edge_bps
                        if settlement_forecast.sample_count >= 10
                        else predicted_net_edge_bps
                    ),
                    predicted_net_edge_bps=predicted_net_edge_bps,
                    rank=0,
                    selected=symbol in decision_enter_symbols,
                    component_scores={
                        "net_edge_bps": predicted_net_edge_bps,
                        "round_trip_cost_bps": -float(
                            shadow_metrics.get("round_trip_cost_bps", 0.0)
                        ),
                        "spread_bps": -spread_bps,
                        "depth_capacity_ratio": min(
                            100.0,
                            float(shadow_metrics.get("entry_depth_usd", 0.0))
                            / required_depth,
                        ),
                        "historical_var_pct": -historical_var_pct,
                        "active_selector_rank": float(rank),
                        "net_ev_mean_bps": net_ev_score.mean_net_edge_bps,
                        "net_ev_lcb_bps": net_ev_score.lower_bound_net_edge_bps,
                        "net_ev_uncertainty_usd": net_ev_score.uncertainty_usd,
                        "settlement_model_samples": float(settlement_forecast.sample_count),
                    },
                    expected_holding_hours=float(
                        shadow_metrics.get("expected_holding_hours", 0.0)
                    ),
                )
            )

        portfolio_positions: list[PortfolioPosition] = []
        for row in self.state_reader.get_positions_for_current_mode():
            position_symbol = str(row.get("symbol") or "").upper()
            if not position_symbol:
                continue
            quantity = abs(_float_or_zero(row.get("qty")))
            reference_price = max(
                _float_or_zero(row.get("perp_live")),
                _float_or_zero(row.get("perp_entry")),
                _float_or_zero(row.get("spot_live")),
                _float_or_zero(row.get("spot_entry")),
            )
            notional = quantity * reference_price
            if notional <= 0.0:
                continue
            try:
                settlement_cluster = self.funding_ranker.calendar.next_settlement(
                    position_symbol
                ).strftime("%H:%M")
            except (TypeError, ValueError):
                settlement_cluster = "unknown"
            portfolio_positions.append(
                PortfolioPosition(
                    symbol=position_symbol,
                    notional_usd=notional,
                    cluster=self._candidate_cluster(position_symbol),
                    settlement_cluster=settlement_cluster,
                    liquidity_tier=(
                        "high"
                        if self.depth_tracker.get_exit_depth(position_symbol)
                        >= notional * 5.0
                        else "low"
                    ),
                    venue="binance",
                    basis_stress_pct=max(
                        0.005,
                        _float_or_zero(self._historical_var_fraction(position_symbol)),
                    ),
                )
            )

        account_equity = max(1.0, float(self._config.get("account_equity_usd")))
        max_drawdown = max(0.001, float(self._config.get("max_drawdown_pct")))
        portfolio_optimizer = ShadowPortfolioOptimizer(
            PortfolioConstraints(
                max_pair_gross_usd=float(self._config.get("max_gross_exposure_usd")),
                per_symbol_cap_usd=float(
                    self._config.get("per_symbol_notional_cap_usd")
                ),
                per_cluster_cap_usd=float(
                    self._config.get("per_cluster_notional_cap_usd")
                ),
                per_settlement_cluster_cap_usd=max(
                    float(self._config.get("per_symbol_notional_cap_usd")),
                    float(self._config.get("max_gross_exposure_usd")) / 2.0,
                ),
                per_venue_cap_usd=float(self._config.get("max_gross_exposure_usd")),
                illiquid_tier_cap_usd=float(
                    self._config.get("per_symbol_notional_cap_usd")
                ),
                max_cvar_95_usd=account_equity * max_drawdown,
                max_stress_loss_usd=account_equity * max_drawdown,
                minimum_history=max(
                    8, int(self._config.get("historical_var_min_observations"))
                ),
                current_static_notional_cap_usd=float(
                    self._config.get("per_symbol_notional_cap_usd")
                ),
            )
        )
        point_in_time_returns = {
            symbol: list(values)
            for symbol, values in self._basis_returns.items()
            if values
        }
        portfolio_result = portfolio_optimizer.optimize(
            shadow_portfolio_candidates,
            portfolio_positions,
            point_in_time_returns,
        )
        portfolio_assessments = {
            item.symbol: item
            for item in (*portfolio_result.selected, *portfolio_result.rejected)
        }
        for symbol, assessment in portfolio_assessments.items():
            snapshot = next(
                (item for item in snapshots if item.symbol == symbol), None
            )
            if snapshot is not None:
                snapshot.metrics.update(
                    {
                        "shadow_portfolio_accepted": assessment.accepted,
                        "shadow_portfolio_target_notional_usd": assessment.target_notional_usd,
                        "shadow_portfolio_reasons": list(assessment.reasons),
                        "shadow_portfolio_cvar_95_usd": assessment.projected_cvar_95_usd,
                        "shadow_portfolio_stress_loss_usd": assessment.projected_stress_loss_usd,
                        "shadow_portfolio_history_status": assessment.history_status,
                    }
                )
            self.state_writer.record_shadow_decision(
                ShadowDecision(
                    trade_id=f"{cycle_id}:portfolio:{symbol}",
                    symbol=symbol,
                    action=(
                        "PORTFOLIO_ALLOCATE"
                        if assessment.accepted
                        else "PORTFOLIO_REJECT"
                    ),
                    hold_score=assessment.projected_cvar_95_usd,
                    exit_score=assessment.projected_stress_loss_usd,
                    incremental_value_usd=assessment.marginal_net_ev_lcb_usd,
                    recommended=assessment.accepted,
                    metadata={
                        "shadow_only": True,
                        "target_notional_usd": assessment.target_notional_usd,
                        "reasons": list(assessment.reasons),
                        "history_status": assessment.history_status,
                        "portfolio_diagnostics": portfolio_result.diagnostics,
                    },
                )
            )

        self._record_open_position_policy_shadows(
            cycle_id=cycle_id,
            decision_time=decision_time,
            net_ev_scores=shadow_net_ev_scores_by_symbol,
            settlement_forecasts=settlement_forecasts_by_symbol,
            portfolio_candidates=shadow_portfolio_candidates,
        )

        for proposal in self.strategy_plugins.evaluate(plugin_contexts):
            snapshot = next(
                (item for item in snapshots if item.symbol == proposal.symbol), None
            )
            if snapshot is not None:
                snapshot.metrics[
                    f"plugin_{proposal.strategy_id}_action"
                ] = proposal.action.value
                snapshot.metrics[
                    f"plugin_{proposal.strategy_id}_lcb_usd"
                ] = proposal.lower_bound_net_value_usd
            self.state_writer.record_shadow_decision(
                ShadowDecision(
                    trade_id=f"{cycle_id}:plugin:{proposal.strategy_id}:{proposal.symbol}",
                    symbol=proposal.symbol,
                    action=(
                        f"PLUGIN_{proposal.strategy_id.upper()}_"
                        f"{proposal.action.value.upper()}"
                    ),
                    hold_score=proposal.expected_net_value_usd,
                    exit_score=proposal.cvar_usd,
                    incremental_value_usd=proposal.lower_bound_net_value_usd,
                    recommended=(proposal.action.value == "enter"),
                    metadata={
                        "shadow_only": proposal.shadow_only,
                        "strategy_id": proposal.strategy_id,
                        "direction": proposal.direction,
                        "target_notional_usd": proposal.target_notional_usd,
                        "expected_loss_usd": proposal.expected_loss_usd,
                        "reason_codes": list(proposal.reason_codes),
                        "expires_at": proposal.expires_at,
                    },
                )
            )

        shadow_scores.sort(
            key=lambda score: (score.total_score, score.symbol),
            reverse=True,
        )
        snapshot_by_symbol = {snapshot.symbol: snapshot for snapshot in snapshots}
        for shadow_rank, score in enumerate(shadow_scores, start=1):
            score.rank = shadow_rank
            snapshot_by_symbol[score.symbol].metrics["shadow_net_ev_rank"] = shadow_rank

        self.state_writer.record_candidate_snapshots(snapshots)
        self.state_writer.record_opportunity_scores(shadow_scores)
        for route_decision in shadow_routes:
            self.state_writer.record_shadow_decision(route_decision)
        for net_ev_decision in shadow_net_ev_decisions:
            self.state_writer.record_shadow_decision(net_ev_decision)
        self.state_writer.set_stat("accepted_candidates", float(accepted_count))
        self.state_writer.set_stat(
            "rejected_candidates",
            float(max(0, total_candidates - accepted_count)),
        )
        self.state_writer.set_stat("scanner_breadth", float(total_candidates))
        return snapshots

    def _record_open_position_policy_shadows(
        self,
        *,
        cycle_id: str,
        decision_time: datetime,
        net_ev_scores: dict[str, NetEVScore],
        settlement_forecasts: dict[str, SettlementForecast],
        portfolio_candidates: list[PortfolioCandidate],
    ) -> None:
        """Persist hold/exit and keep/switch counterfactuals without trading."""

        candidate_by_symbol = {
            candidate.symbol.upper(): candidate for candidate in portfolio_candidates
        }
        for row in self.state_reader.get_positions_for_current_mode():
            symbol = str(row.get("symbol") or "").upper()
            if not symbol:
                continue
            quantity = abs(_float_or_zero(row.get("qty")))
            reference_price = max(
                _float_or_zero(row.get("perp_live")),
                _float_or_zero(row.get("perp_entry")),
                _float_or_zero(row.get("spot_live")),
                _float_or_zero(row.get("spot_entry")),
                _float_or_zero(self._mark_prices.get(symbol)),
            )
            notional = quantity * reference_price
            if notional <= 0.0:
                continue
            direction = (
                "short_spot_long_perp"
                if str(row.get("direction") or "").lower() == "short"
                else "long_spot_short_perp"
            )
            forecast = settlement_forecasts.get(symbol)
            if forecast is None:
                annualized_rate = self.funding_ranker.get_rate(symbol)
                self.settlement_model.observe(
                    FundingObservation(
                        symbol=symbol,
                        available_at=decision_time,
                        annualized_rate=annualized_rate,
                        basis_pct=self.depth_tracker.basis_pct(symbol),
                        source_event_id=f"{cycle_id}:{symbol}:open-position",
                    )
                )
                forecast = self.settlement_model.forecast(
                    symbol=symbol,
                    decision_time=decision_time,
                    horizon_hours=max(
                        1.0 / 3_600.0,
                        (
                            self.funding_ranker.minutes_to_next_settlement(symbol)
                            * 60.0
                            + 1.0
                        )
                        / 3_600.0,
                    ),
                    notional_usd=notional,
                    direction=direction,
                    calendar=self.funding_ranker.calendar,
                )
            current_basis = _float_or_zero(self.depth_tracker.basis_pct(symbol))
            exit_cost = blended_exit_cost(
                notional,
                depth_usd=self._cost_depth_or_default(
                    self.depth_tracker.get_exit_depth(symbol)
                ),
            )
            first_payment = forecast.payments[0] if forecast.payments else None
            recovery_state = str(row.get("recovery_state") or "").lower()
            hold_decision = self.hold_exit_policy.decide(
                HoldExitInputs(
                    symbol=symbol,
                    direction=direction,
                    notional_usd=notional,
                    expected_future_funding_usd=forecast.expected_payment_usd,
                    lower_future_funding_usd=forecast.lower_payment_usd,
                    current_basis_pct=current_basis,
                    expected_exit_basis_pct=0.0,
                    exit_cost_usd=exit_cost,
                    basis_tail_risk_usd=notional
                    * max(
                        0.0,
                        _float_or_zero(self._historical_var_fraction(symbol)),
                    ),
                    seconds_to_settlement=max(
                        0.0,
                        self.funding_ranker.minutes_to_next_settlement(symbol)
                        * 60.0,
                    ),
                    settlement_survival_probability=(
                        first_payment.favourable_sign_probability
                        if first_payment is not None
                        else 0.0
                    ),
                    imminent_settlement_payment_usd=(
                        first_payment.lower_payment_usd
                        if first_payment is not None
                        else 0.0
                    ),
                    forecast_favourable_probability=(
                        first_payment.favourable_sign_probability
                        if first_payment is not None
                        else 0.0
                    ),
                    risk_urgency=(1.0 if recovery_state == "manual_review" else 0.0),
                    hedge_mismatch_usd=notional
                    * abs(1.0 - _float_or_zero(row.get("hedge_ratio") or 1.0)),
                    maximum_hedge_mismatch_usd=max(1.0, notional * 0.01),
                    data_fresh=(
                        self.depth_tracker.entry_data_age_seconds(symbol)
                        <= float(self._config.get("scanner_max_data_stale_seconds"))
                    ),
                    exit_executable=self.depth_tracker.get_exit_depth(symbol) > 0.0,
                    entry_blocked=symbol in self._symbol_safe_mode_blocks,
                )
            )
            self.state_writer.record_shadow_decision(
                ShadowDecision(
                    trade_id=f"{cycle_id}:hold-exit:{symbol}",
                    symbol=symbol,
                    action=f"HOLD_EXIT_{hold_decision.action.value.upper()}",
                    hold_score=hold_decision.hold_value_usd,
                    exit_score=hold_decision.exit_value_usd,
                    incremental_value_usd=hold_decision.incremental_hold_value_usd,
                    recommended=hold_decision.action.value != "hold",
                    metadata={
                        "shadow_only": True,
                        "urgency": hold_decision.urgency,
                        "reason_codes": list(hold_decision.reason_codes),
                        "explanation": hold_decision.explanation,
                    },
                )
            )

            alternatives = [
                candidate
                for candidate in portfolio_candidates
                if candidate.symbol.upper() != symbol
            ]
            if not alternatives:
                continue
            candidate = max(
                alternatives,
                key=lambda item: (
                    item.net_ev_lcb_usd / max(1.0, item.requested_notional_usd),
                    item.symbol,
                ),
            )
            candidate_score = net_ev_scores.get(candidate.symbol.upper())
            if candidate_score is None:
                continue
            current_score = net_ev_scores.get(symbol)
            candidate_scaled_lcb = candidate_score.lower_bound_net_ev_usd * (
                notional / max(candidate.requested_notional_usd, 1.0)
            )
            current_remaining_lcb = (
                current_score.lower_bound_net_ev_usd
                if current_score is not None
                else hold_decision.incremental_hold_value_usd
            )
            entry_time_raw = self._entry_times.get(symbol) or str(
                row.get("updated_at") or ""
            )
            held_hours = 0.0
            try:
                entry_time = datetime.fromisoformat(entry_time_raw.replace("Z", "+00:00"))
                if entry_time.tzinfo is None:
                    entry_time = entry_time.replace(tzinfo=timezone.utc)
                held_hours = max(
                    0.0,
                    (decision_time - entry_time.astimezone(timezone.utc)).total_seconds()
                    / 3_600.0,
                )
            except (TypeError, ValueError):
                pass
            candidate_entry_cost = blended_entry_cost(
                notional,
                depth_usd=self._cost_depth_or_default(
                    self.depth_tracker.get_entry_depth(candidate.symbol)
                ),
            )
            earning_rate = max(
                0.0,
                candidate_score.mean_net_ev_usd
                / max(
                    1.0,
                    float(
                        settlement_forecasts.get(
                            candidate.symbol.upper(), forecast
                        ).interval_hours
                    ),
                ),
            )
            rotation = self.rotation_policy.decide(
                RotationInputs(
                    current_symbol=symbol,
                    candidate_symbol=candidate.symbol,
                    current_notional_usd=notional,
                    current_remaining_lcb_usd=current_remaining_lcb,
                    candidate_lcb_usd_at_current_size=candidate_scaled_lcb,
                    current_close_cost_usd=exit_cost,
                    candidate_open_cost_usd=candidate_entry_cost,
                    transition_loss_usd=0.0,
                    candidate_executable_capacity_usd=candidate.executable_capacity_usd,
                    candidate_confidence=candidate.confidence,
                    held_hours=held_hours,
                    minimum_hold_hours=float(
                        self.funding_ranker.calendar.interval_hours(symbol)
                    ),
                    minimum_incremental_value_usd=float(
                        self._config.get("min_incremental_portfolio_edge_bps")
                    )
                    * notional
                    / 10_000.0,
                    hysteresis_usd=max(0.50, exit_cost * 0.25),
                    max_payback_hours=float(
                        self._config.get("rotation_max_payback_days")
                    )
                    * 24.0,
                    candidate_net_earning_rate_usd_per_hour=earning_rate,
                    seconds_to_current_settlement=max(
                        0.0,
                        self.funding_ranker.minutes_to_next_settlement(symbol)
                        * 60.0,
                    ),
                    current_settlement_lower_payment_usd=(
                        first_payment.lower_payment_usd
                        if first_payment is not None
                        else 0.0
                    ),
                    cooldown_active=False,
                    pending_transition=(
                        symbol in self._pending_exit_intents
                    ),
                    previous_recommendation=RotationAction.KEEP,
                )
            )
            self.state_writer.record_shadow_decision(
                ShadowDecision(
                    trade_id=f"{cycle_id}:rotation:{symbol}:{candidate.symbol}",
                    symbol=symbol,
                    action=f"ROTATION_{rotation.action.value.upper()}",
                    hold_score=current_remaining_lcb,
                    exit_score=candidate_scaled_lcb,
                    incremental_value_usd=rotation.incremental_value_usd,
                    recommended=rotation.action in {
                        RotationAction.PARTIAL_ROTATE,
                        RotationAction.FULL_ROTATE,
                    },
                    metadata={
                        "shadow_only": True,
                        "candidate_symbol": candidate.symbol,
                        "rotate_notional_usd": rotation.rotate_notional_usd,
                        "payback_hours": rotation.payback_hours,
                        "reason_codes": list(rotation.reason_codes),
                        "explanation": rotation.explanation,
                    },
                )
            )

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
                self._loop_heartbeats["trading_loop"] = time.monotonic()
                if not self._telemetry_stream_healthy():
                    logger.info("Waiting for Rust subscriber connection before dispatching commands")
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
                self._expire_stale_pending_intents()

                # Evaluate risk controls first so the state (including venue latency)
                # is always fresh in the database/dashboard even during flattening.
                risk_decision = self._evaluate_risk_controls(position_rows)

                if self._maybe_process_operator_flatten_all_request(position_rows):
                    if await self._sleep_or_shutdown(1.0):
                        break
                    continue
                if self._dispatch_startup_recovery_exits(position_rows):
                    if await self._sleep_or_shutdown(1.0):
                        break
                    continue
                open_positions = self._get_open_positions(position_rows)
                managed_positions = [p for p in open_positions if p.recovery_state != "manual_review"]
                manual_review_count = len(open_positions) - len(managed_positions)
                funding_rates = {p.symbol: p.ann_funding for p in managed_positions}

                if risk_decision.kill_switch or risk_decision.derisk_required:
                    self._maybe_log_risk_engine_state(risk_decision)
                    for pos in managed_positions:
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
                if not risk_decision.allow_new_risk:
                    # This is an entry gate.  Continue evaluating verified
                    # reduce-only/economic exit paths below; entry dispatch is
                    # independently blocked by _external_entry_block_reason().
                    self._maybe_log_risk_engine_state(risk_decision)

                # -- 0. Post-snapshot funding decay exit ----------------------
                # Within 5 minutes after a funding snapshot, funding rates that
                # have decayed below the exit threshold are acted on immediately
                # rather than waiting for the next allocator cycle.
                self._last_risk_log_signature = None
                self._last_risk_log_monotonic = 0.0
                if managed_positions:
                    for pos in managed_positions:
                        if (
                            self._minutes_since_last_snapshot(pos.symbol) <= 5
                            and
                            self._funding_has_decayed(
                                self._position_directions.get(pos.symbol, "long"),
                                pos.ann_funding,
                            )
                            and pos.symbol not in self._exit_events
                        ):
                            logger.info(
                                "Post-snapshot decay: %s funding=%.1f%% crossed exit threshold - exiting",
                                pos.symbol, pos.ann_funding * 100,
                            )
                            self._dispatch_exit(
                                pos.symbol,
                                urgency=1.0,
                                direction=self._position_directions.get(pos.symbol, "long"),
                            )

                # -- 1. Circuit breaker ---------------------------------------
                liquidity_map = {
                    p.symbol: self.depth_tracker.get_exit_depth(p.symbol)
                    for p in managed_positions
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
                    logger.warning("CIRCUIT BREAKER: WARNED - %s", breaker_decision.reason)
                    # Entries still allowed; fall through to allocation logic

                elif breaker_decision.state == "PARTIAL_EXIT":
                    logger.warning("CIRCUIT BREAKER: PARTIAL_EXIT - %s", breaker_decision.reason)
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
                    logger.warning("CIRCUIT BREAKER: EMERGENCY - exiting all positions")
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
                        logger.info("CIRCUIT BREAKER: HALTED - blocking new entries")
                    elif now_halt - self._halted_since >= _HALTED_ESCALATION_SECS:
                        logger.warning(
                            "CIRCUIT BREAKER: HALTED for %.0f min - escalating to partial exits",
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
                else:
                    # Clear HALTED timer when breaker returns to non-blocking state
                    self._halted_since = 0.0

                # -- 2. Allocation decision -----------------------------------
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
                base_target_notional = min(
                    self.allocator._capital_per_slot * TARGET_LEVERAGE * max(0.1, self._effective_notional_scale()),
                    MAX_NOTIONAL_PER_TRADE,
                    float(self._config.get("per_symbol_notional_cap_usd")),
                )
                candidate_notional_overrides = {
                    symbol.upper(): round(self._var_sized_notional(symbol, base_target_notional), 2)
                    for symbol, _ann_funding in ranked
                }
                entry_gate_blocked = {
                    symbol: self._symbol_entry_gate_reasons(
                        symbol,
                        ann_funding,
                        entry_threshold=entry_threshold,
                        target_notional_usd=candidate_notional_overrides.get(symbol.upper()),
                    )
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
                            len(managed_positions),
                            manual_review_count,
                            top_rate * 100,
                            entry_threshold * 100,
                            cooldown_snapshot["global_reason"],
                            cooldown_snapshot["global_remaining_s"],
                        )
                        self.state_writer.set_stat("open_positions", float(len(position_rows)))
                        self.state_writer.set_stat("managed_open_positions", float(len(managed_positions)))
                        self.state_writer.set_stat("manual_review_positions", float(manual_review_count))
                        self.state_writer.set_stat("top_funding_rate", top_rate * 100)
                        self.state_writer.set_stat("top_funding_symbol", ranked[0][0] if ranked else "")
                        self._persist_guard_snapshot(regime_blocked)
                cooldown_blocked = self.cooldowns.blocked_symbols(ranked_symbols)
                blocked_symbols = set(regime_blocked) | set(cooldown_blocked) | set(entry_gate_blocked)
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
                # Candidate evidence must be durable before an accepted entry
                # decision or capital reservation can reference this cycle.
                self.state_writer.flush()
                self._record_entry_funnel_state(
                    decision,
                    external_entry_block_reason=external_entry_block_reason,
                    entry_gate_blocked=entry_gate_blocked,
                    now_monotonic=now,
                )

                # -- 3. Dispatch exits ----------------------------------------
                for symbol, reason in decision.exit:
                    if symbol not in self._exit_events:
                        logger.info("Rotation: exiting %s (%s)", symbol, reason)
                        self._dispatch_exit(
                            symbol,
                            urgency=0.8,
                            direction=self._position_directions.get(symbol, "long"),
                        )

                # -- 4. Await exit confirmations, dispatch rotation entries ----
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
                                    "Skipping rotation entry for %s - external risk gate active (%s)",
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
                                    "Skipping rotation entry for %s - cooldown active (%s)",
                                    rotation_target, cooldown_reason,
                                )
                                continue
                            regime_decision = self.regime_filter.evaluate(rotation_target)
                            if not regime_decision.allow_entry:
                                logger.info(
                                    "Skipping rotation entry for %s - regime filter blocked (%s)",
                                    rotation_target, ", ".join(regime_decision.reasons),
                                )
                                continue
                            rot_funding = self.funding_ranker.get_rate(rotation_target) or 0.0
                            rot_threshold = self._effective_entry_threshold()
                            if rot_funding < rot_threshold:
                                logger.info(
                                    "Skipping rotation entry for %s - funding %.2f%% below threshold %.1f%%",
                                    rotation_target, rot_funding * 100, rot_threshold * 100,
                                )
                                continue
                            if not self._entry_structure_allows_symbol(rotation_target):
                                continue
                            if not self._predictor_allows_entry(rotation_target, rot_threshold):
                                continue
                            rotation_notional = decision.rotation_notionals.get(
                                exited_symbol,
                                min(CAPITAL_PER_SLOT_USD * TARGET_LEVERAGE,
                                    float(self._config.get("per_symbol_notional_cap_usd"))),
                            )
                            self._dispatch_enter(
                                rotation_target,
                                rotation_notional,
                                direction="long",
                                ann_funding=rot_funding,
                                cycle_id=cycle_id,
                                rotation_entry=True,
                            )
                        else:
                            logger.warning(
                                "Skipping rotation entry for %s - exit of %s unconfirmed",
                                rotation_target, exited_symbol,
                            )

                # -- 5. Dispatch entries for empty slots ---------------------
                entry_threshold = self._effective_entry_threshold()
                for symbol, notional in decision.enter:
                    if symbol in self._exit_events:
                        continue
                    if symbol in self._pending_enters:
                        logger.debug("Skipping %s - entry already pending confirmation", symbol)
                        continue
                    if external_entry_block_reason is not None:
                        logger.info(
                            "Skipping %s - external risk gate active (%s)",
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
                            "Skipping %s - cooldown active (%s)",
                            symbol, cooldown_reason,
                        )
                        continue
                    regime_decision = self.regime_filter.evaluate(symbol)
                    if not regime_decision.allow_entry:
                        logger.info(
                            "Skipping %s - regime filter blocked (%s)",
                            symbol, ", ".join(regime_decision.reasons),
                        )
                        continue

                    ann_funding = self.funding_ranker.get_rate(symbol) or 0.0
                    # Long-only release candidate: only collect positive funding.
                    if ann_funding < entry_threshold:
                        logger.debug(
                            "Skipping %s - funding %.2f%% below threshold %.1f%%",
                            symbol, ann_funding * 100, entry_threshold * 100,
                        )
                        continue
                    if not self._entry_structure_allows_symbol(symbol):
                        continue
                    # Predictor gate: skip if projected rate decays below threshold at snapshot
                    if not self._predictor_allows_entry(symbol, entry_threshold):
                        continue
                    self._dispatch_enter(
                        symbol,
                        notional,
                        direction="long",
                        ann_funding=ann_funding,
                        cycle_id=cycle_id,
                    )

                # -- 6. Heartbeat - periodic status for logs + dashboard ----
                if now - _last_heartbeat >= 60:
                    _last_heartbeat = now
                    top_rate = ranked[0][1] if ranked else 0.0
                    live_enriched_symbols = self._live_enriched_symbols(ranked, open_positions)
                    logger.info(
                        "HEARTBEAT: %d managed positions | %d manual-review positions | "
                        "top funding=%.2f%% | threshold=%.1f%% | %d pending enters | %d pending exits | %d guarded symbols",
                        len(managed_positions),
                        manual_review_count,
                        top_rate * 100,
                        entry_threshold * 100,
                        len(self._pending_enters),
                        len(self._exit_events),
                        len(blocked_symbols),
                    )
                    self.state_writer.set_stat("open_positions", float(len(position_rows)))
                    self.state_writer.set_stat("managed_open_positions", float(len(managed_positions)))
                    self.state_writer.set_stat("manual_review_positions", float(manual_review_count))
                    self.state_writer.set_stat("top_funding_rate", top_rate * 100)
                    self.state_writer.set_stat("top_funding_symbol", ranked[0][0] if ranked else "")
                    self.state_writer.set_stat("live_enrichment_breadth", float(len(live_enriched_symbols)))
                    self._persist_guard_snapshot(regime_blocked)

                # Batch commit for any writes that occurred during the cycle.
                self.state_writer.flush()
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
            asyncio.create_task(self._run_execution_event_writer(), name="execution_event_writer"),
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
            subscriber_task = asyncio.create_task(self.subscriber.run(), name="rust_subscriber")
            self._background_tasks.append(subscriber_task)
            if not await self.subscriber.wait_until_connected(timeout=5.0):
                raise StartupBlockedError("Rust telemetry unavailable for execution ACK replay")
            replay_result = self.execution.replay_pending()
            logger.info("Durable execution outbox replay: %s", replay_result)
            if self._trading_mode != "paper" and not await self._ensure_config_consensus(
                timeout_s=10.0
            ):
                self._preflight_status = "blocked_config_consensus"
                reason = self._config_sync_reason or "matching Rust ConfigAck unavailable"
                self._set_blocked_reason(f"execution config consensus failed: {reason}")
                self._persist_runtime_state()
                raise StartupBlockedError(
                    f"Rust/Python execution config consensus failed: {reason}"
                )
            self._request_depth_recovery_subscriptions()
            self._background_tasks.extend([
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


def run_cli() -> None:
    """Run the canonical trader module from a process entry point."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("LiveTraderV2 stopped.")
    finally:
        logging.shutdown()


if __name__ == "__main__":
    run_cli()
