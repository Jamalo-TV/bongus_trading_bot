"""Canonical configuration for the Bongus funding arbitrage bot.

Static defaults live here. Runtime overrides belong in ``live_config.json``
and are hot-reloaded by :mod:`bongus.core.config_manager`.
"""

from __future__ import annotations

import os
from pathlib import Path


def _env_symbols(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return list(default)
    parsed = [symbol.strip().upper() for symbol in raw.split(",") if symbol.strip()]
    return parsed or list(default)


def _resolve_runtime_data_root(value: str | None) -> Path:
    """Resolve the one common runtime data root without permitting path escape."""

    raw = str(value or "").strip()
    if not raw:
        return Path(__file__).resolve().parents[2]
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise ValueError("BONGUS_DATA_ROOT must be an absolute path")
    return candidate.resolve(strict=False)


def _resolve_runtime_role_path(
    data_root: Path,
    filename: str,
    override: str | None,
) -> Path:
    """Bind every split database to the one manifest-owned data directory."""

    expected = (data_root / filename).resolve(strict=False)
    raw = str(override or "").strip()
    if not raw:
        return expected
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise ValueError(f"{filename} override must be an absolute path")
    candidate = candidate.resolve(strict=False)
    if candidate != expected:
        raise ValueError(
            f"{filename} override must be exactly {expected}; split storage is one "
            "manifest-bound data root"
        )
    return candidate

# ── Runtime Identity ──────────────────────────────────────────────────────
CANONICAL_RUNTIME_NAME = "multi_symbol_funding_bot"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
LIVE_CONFIG_PATH = str(PROJECT_ROOT / "live_config.json")
RUNTIME_DATA_ROOT = _resolve_runtime_data_root(os.getenv("BONGUS_DATA_ROOT"))
STATE_DB_PATH = str(
    _resolve_runtime_role_path(
        RUNTIME_DATA_ROOT,
        "state.db",
        os.getenv("BONGUS_STATE_DB_PATH"),
    )
)
AUDIT_DB_PATH = str(
    _resolve_runtime_role_path(
        RUNTIME_DATA_ROOT,
        "audit.db",
        os.getenv("BONGUS_AUDIT_DB_PATH"),
    )
)
RESEARCH_DB_PATH = str(
    _resolve_runtime_role_path(
        RUNTIME_DATA_ROOT,
        "research.db",
        os.getenv("BONGUS_RESEARCH_DB_PATH"),
    )
)

# ── Account Sizing ────────────────────────────────────────────────────────
ACCOUNT_EQUITY_USD = 10_000.0
MAX_LEVERAGE = 2.0
TARGET_CONCURRENT_POSITIONS = 4
MIN_TOP_N = 3
MAX_TOP_N = 5
SLOT_NOTIONAL_USD = 2_500.0
NOTIONAL_PER_TRADE = SLOT_NOTIONAL_USD
MAX_NOTIONAL_PER_TRADE = 2_500.0
MAX_GROSS_EXPOSURE_USD = 10_000.0
PER_SYMBOL_NOTIONAL_CAP_USD = 2_500.0
PER_CLUSTER_NOTIONAL_CAP_USD = 10_000.0
HISTORICAL_VAR_CONFIDENCE = 0.95
HISTORICAL_VAR_WINDOW = 120
HISTORICAL_VAR_MIN_OBSERVATIONS = 24
HISTORICAL_VAR_RISK_BUDGET_PCT = 0.02
CORRELATION_FILTER_THRESHOLD = 0.80
CORRELATION_FILTER_MIN_OBSERVATIONS = 24
STRESS_TEST_SPOT_CRASH_PCT = 0.30

# ── Cost Model ────────────────────────────────────────────────────────────
TAKER_FEE_SPOT = 0.00075
TAKER_FEE_PERP = 0.0005
MAKER_FEE_SPOT = 0.00075
MAKER_FEE_PERP = 0.0002
TAKER_FEE = (TAKER_FEE_SPOT + TAKER_FEE_PERP) / 2
MAKER_FEE = (MAKER_FEE_SPOT + MAKER_FEE_PERP) / 2
SLIPPAGE_ESTIMATE = 0.0002
LEGS_PER_ACTION = 2
ACTIONS_PER_ROUND_TRIP = 2
MAKER_FILL_PROBABILITY = 0.70
DEFAULT_HOLDING_HOURS = 8.0
ROTATION_MAX_PAYBACK_DAYS = 0.333
MIN_EXPECTED_EDGE_BPS = 6.0
MIN_INCREMENTAL_PORTFOLIO_EDGE_BPS = 4.0

# ── Funding Schedule ──────────────────────────────────────────────────────
FUNDING_INTERVAL_HOURS = 8
FUNDING_PERIODS_PER_DAY = 24 / FUNDING_INTERVAL_HOURS
FUNDING_PERIODS_PER_YEAR = int(FUNDING_PERIODS_PER_DAY * 365)
FUNDING_SNAPSHOT_HOURS = [0, 8, 16]

# ── Entry / Exit Controls ────────────────────────────────────────────────
ENTRY_ANN_FUNDING_THRESHOLD = 0.12
ENTRY_PREMIUM_THRESHOLD = 0.0002
EXIT_ANN_FUNDING_THRESHOLD = 0.02
EXIT_DISCOUNT_THRESHOLD = -0.0003
BASIS_DEVIATION_STOP = 0.003
SNIPE_ANN_FUNDING_THRESHOLD = 0.30
SNIPE_ENTRY_WINDOW_MIN = 60
SNIPE_ENTRY_WINDOW_MAX = 120

# ── Margin / Borrowing ───────────────────────────────────────────────────
MARGIN_BORROW_RATE_ANNUAL = 0.10

# ── Runtime Freshness / Safety ───────────────────────────────────────────
RUNTIME_HEARTBEAT_INTERVAL_SECONDS = 5
TRADER_CYCLE_INTERVAL_SECONDS = 15
MAX_ALLOWED_GAP_MINUTES = 1
MAX_FUNDING_STALENESS_MINUTES = 8 * 60
MAX_RUNTIME_STALENESS_SECONDS = 45
MAX_EVENT_LOOP_STALENESS_SECONDS = 30
MAX_VENUE_LATENCY_MS = 400
RUNTIME_SETTLING_SECONDS = 90.0
AUTONOMOUS_STARTUP_RECOVERY = False

# ── Risk Limits ───────────────────────────────────────────────────────────
MAX_SYMBOL_CONCENTRATION = 0.30
SOFT_DRAWDOWN_PCT = 0.04
MAX_DRAWDOWN_PCT = 0.10
MAX_DRAWDOWN_RELEASE_PCT = 0.08
MAX_CONSECUTIVE_LOSSES = 5

# ── Scanner Controls ─────────────────────────────────────────────────────
SCANNER_MIN_DEPTH_USD = 150_000.0
SCANNER_MIN_DEPTH_MULTIPLIER = 5.0
SCANNER_MAX_SPREAD_BPS = 12.0
SCANNER_MAX_TOXIC_SPREAD_BPS = 35.0
SCANNER_MIN_LISTING_AGE_DAYS = 30
SCANNER_MAX_DATA_STALE_SECONDS = 45
SCANNER_MIN_BOOK_DEPTH_LEVELS = 2
SCANNER_REQUIRE_SPOT_AND_PERP = True
SCANNER_ALLOWLIST: list[str] = []
SCANNER_BLOCKLIST: list[str] = []
SCANNER_MAX_CANDIDATES = 15

# ── Ranking Controls ─────────────────────────────────────────────────────
RANKER_WINSORIZE_LOWER_PCT = 0.05
RANKER_WINSORIZE_UPPER_PCT = 0.95
RANKER_WEIGHTS = {
    "net_edge": 0.35,
    "depth": 0.20,
    "spread": 0.15,
    "volatility": 0.10,
    "basis_stability": 0.10,
    "regime_health": 0.10,
}

# ── Execution Controls ───────────────────────────────────────────────────
EXECUTION_SEND_TIMEOUT_MS = 500
EXECUTION_SLICE_MAX_NOTIONAL_USD = 2_500.0
EXECUTION_DEFAULT_MAX_SLIPPAGE_BPS = 10.0
EXECUTION_MAX_PASSIVE_OFFSET_BPS = 2.0
EXECUTION_MIN_MAKER_FILL_PROBABILITY = 0.55
EXECUTION_QUALITY_TARGET_SLIPPAGE_BPS = 4.0

# ── Walk-Forward / Promotion Governance ──────────────────────────────────
WF_MIN_AVG_OOS_EDGE = 0.0
WF_MIN_WINDOWS_PASSING = 2
WF_MIN_TRADES_PER_WINDOW = 10
WF_MIN_SIGNAL_TO_NOISE = 0.1
WF_MAX_DRAWDOWN_PCT = 0.12
WF_MIN_UTILIZATION = 0.40
WF_PROMOTION_ENABLED = True

# ── Shadow Exits / Ratcheting ────────────────────────────────────────────
SHADOW_EXIT_ENABLED = True
SHADOW_EXIT_MODEL_PATH = "data/models/shadow_exit_model.json"
SHADOW_EXIT_MIN_INCREMENTAL_VALUE_USD = 0.0
RATCHETING_ENABLED = False
RATCHETING_AGE_MINUTES = 120
RATCHETING_BREAKEVEN_BPS = 10.0

# ── Universe Taxonomy ────────────────────────────────────────────────────
PORTFOLIO_CLUSTER_MAP = {
    "BTCUSDT": "MAJORS",
    "ETHUSDT": "MAJORS",
    "BNBUSDT": "MAJORS",
    "SOLUSDT": "L1",
    "ADAUSDT": "L1",
    "AVAXUSDT": "L1",
    "ATOMUSDT": "L1",
    "APTUSDT": "L1",
    "NEARUSDT": "L1",
    "DOGEUSDT": "MEME",
    "SHIBUSDT": "MEME",
    "PEPEUSDT": "MEME",
    "XRPUSDT": "PAYMENTS",
    "XLMUSDT": "PAYMENTS",
    "TRXUSDT": "PAYMENTS",
    "LINKUSDT": "INFRA",
    "ARBUSDT": "L2",
    "OPUSDT": "L2",
    "SUIUSDT": "L1",
    "DOTUSDT": "L1",
    "LTCUSDT": "MAJORS",
}
DEFAULT_CLUSTER = "OTHER"

# ── Legacy Compatibility / Shared Defaults ───────────────────────────────
DEFAULT_MONITORED_SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
]


def get_monitored_symbols() -> list[str]:
    return _env_symbols("MONITORED_SYMBOLS", DEFAULT_MONITORED_SYMBOLS)


MONITORED_SYMBOLS = get_monitored_symbols()

ENTRY_ANN_FUNDING_THRESHOLD_BTC = ENTRY_ANN_FUNDING_THRESHOLD
ENTRY_ANN_FUNDING_THRESHOLD_ALT = max(ENTRY_ANN_FUNDING_THRESHOLD, 0.05)
HOLD_THROUGH_FUNDING = True
FUNDING_CAPTURE_DELAY_MIN = 5
SNAPSHOT_SNIPE_ENABLED = False

MAX_CONCURRENT_POSITIONS = TARGET_CONCURRENT_POSITIONS
CAPITAL_PER_SLOT_USD = SLOT_NOTIONAL_USD / max(MAX_LEVERAGE, 1.0)
TARGET_LEVERAGE = MAX_LEVERAGE
LIQUIDITY_FILTER_MULTIPLIER = SCANNER_MIN_DEPTH_MULTIPLIER
ROTATION_MIN_GAP_ANN = 0.03
ROTATION_CONFIRM_TIMEOUT_S = 30
MAKER_ORDER_PATIENCE_SEC = 15
PAUSE_NEW_ENTRIES = False

BREAKER_WARN_RATIO = 0.33
BREAKER_HALT_RATIO = 0.50
BREAKER_PARTIAL_RATIO = 0.75
BREAKER_EMERGENCY_RATIO = 1.00

DYNAMIC_SYMBOL_MODE = True
MAX_FUNDING_SCAN_SYMBOLS = 0  # 0 = scan all eligible Binance perps
MAX_LIVE_ENRICHED_SYMBOLS = 15
MAX_MONITORED_SYMBOLS = MAX_LIVE_ENRICHED_SYMBOLS  # legacy alias
MAX_DEPTH_SUBSCRIPTIONS = 15
INVERSE_FUNDING_ENABLED = False
SENTIMENT_ENABLED = False

# Staged decision ownership.  The canonical engine is observational until a
# separately attested paper/testnet promotion artifact authorizes a later
# stage.  Reverse cash-spot entry remains unavailable without a complete
# borrow/interest/repayment lifecycle.
DECISION_ENGINE_STAGE = "shadow"
ALLOW_REVERSE_SPOT_ENTRY = False
LIVE_APPROVAL_REQUIRED = True
LIVE_APPROVAL_ARTIFACT_PATH = ""

FUNDING_PREDICTOR_SAMPLES = 28_800

REGIME_FILTER_ENABLED = True
REGIME_FILTER_MIN_SAMPLES = 20
REGIME_FILTER_BASIS_ZSCORE_MAX = 2.5
REGIME_FILTER_BASIS_ABS_FLOOR = 0.0008
REGIME_FILTER_PRICE_SHOCK_PCT = 0.015
REGIME_FILTER_DEPTH_RATIO_MIN = 0.50
REGIME_FILTER_FUNDING_DISPERSION_MAX = 3.0
REGIME_FILTER_BASIS_WIDENING_MAX = 3.0
REGIME_FILTER_VOLUME_SPIKE_MAX = 5.0

COOLDOWN_ENABLED = True
COOLDOWN_HALTED_MINUTES = 30
COOLDOWN_PARTIAL_EXIT_MINUTES = 60
COOLDOWN_EMERGENCY_MINUTES = 240
COOLDOWN_SYMBOL_MINUTES = 120

# Entry-rejection cooldown
ENTRY_REJECT_COOLDOWN_BASE_SECONDS = 600        # first rejection: 10 min
ENTRY_REJECT_COOLDOWN_MAX_SECONDS = 14400       # cap: 4 h
ENTRY_REJECT_COOLDOWN_BACKOFF_WINDOW_SECONDS = 3600  # 1 h window to count recent rejects
ENTRY_REJECT_COOLDOWN_BACKOFF_FACTOR = 2.0      # double on each repeat in window

# Stale-intent cooldown (for symbols that time out during entry/exit)
STALE_INTENT_COOLDOWN_BASE_SECONDS = 900        # first timeout: 15 min
STALE_INTENT_COOLDOWN_MAX_SECONDS = 86400       # cap: 24 h
STALE_INTENT_COOLDOWN_BACKOFF_FACTOR = 2.0      # double on each repeat

# Venue latency smoothing and debounce
VENUE_LATENCY_SMOOTHING_FACTOR = 0.2           # EMA factor for RTT (lower = smoother)
VENUE_LATENCY_DEBOUNCE_S = 30.0                # require latency to be high for this long

# Autonomous policy for recovered positions
ALLOW_AUTONOMOUS_INVERSE_LIQUIDATION = False   # If True, bot will auto-exit inverse positions (e.g. DENTUSDT)

TRAILING_BASIS_STOP_ENABLED = True
TRAILING_BASIS_STOP_LOCK_PCT = 0.50
TRAILING_BASIS_STOP_TRAIL_BPS = 15.0

AUTO_COMPOUND_ENABLED = False  # Increases require reconciled promotion evidence.
COMPOUND_UPDATE_INTERVAL_HOURS = 24
COMPOUND_HIGH_WATERMARK = True
COMPOUND_MIN_EQUITY_PCT = 0.02
COMPOUND_MAX_EQUITY_PCT = 1.00
COMPOUND_AGGESSION = 0.50

HEARTBEAT_INTERVAL_SECONDS = 2
HEARTBEAT_MISS_THRESHOLD = 5
PENDING_INTENT_MAX_AGE_SECONDS = 300
DATA_RETENTION_DAYS = 30
SNAPSHOT_RETENTION_DAYS = 2
FEATURE_RETENTION_DAYS = 3
# Market samples are already minute rollups. Keep the plan's seven-day minute
# window; SplitStateWriter materializes bounded 90-day hourly aggregates before
# pruning older minute rows.
MARKET_SAMPLE_RETENTION_DAYS = 7
HEALTH_SAMPLE_RETENTION_DAYS = 7
RESEARCH_EVIDENCE_MIN_INTERVAL_SECONDS = TRADER_CYCLE_INTERVAL_SECONDS

# Whole-volume production budget (decimal bytes).  Development toolchains,
# caches, Cargo targets and worktrees are deliberately outside this budget.
STORAGE_VOLUME_BUDGET_BYTES = 16_000_000_000
STORAGE_COMPONENT_BUDGETS_BYTES = {
    "application": 200_000_000,
    "python_runtime": 600_000_000,
    "state_db": 1_250_000_000,
    "sqlite_scratch": 500_000_000,
    "audit": 1_100_000_000,
    "backup": 1_500_000_000,
    "research": 1_500_000_000,
    "logs": 200_000_000,
    "rust_journals": 150_000_000,
    "models_caches": 250_000_000,
    "owned_temp": 250_000_000,
}
STORAGE_HEALTHY_FREE_BYTES = 4_000_000_000
STORAGE_WARNING_FREE_BYTES = 4_000_000_000
STORAGE_DEGRADED_FREE_BYTES = 3_000_000_000
STORAGE_EMERGENCY_FREE_BYTES = 2_000_000_000
STORAGE_CRITICAL_FREE_BYTES = 1_000_000_000
STORAGE_WARNING_FREE_FRACTION = 0.25
STORAGE_DEGRADED_FREE_FRACTION = 0.1875
STORAGE_EMERGENCY_FREE_FRACTION = 0.125
STORAGE_CRITICAL_FREE_FRACTION = 0.0625
STORAGE_WARNING_TTF_HOURS = 72.0
STORAGE_DEGRADED_TTF_HOURS = 24.0
STORAGE_EMERGENCY_TTF_HOURS = 6.0
STORAGE_CRITICAL_TTF_HOURS = 1.0
STORAGE_RECOVERY_HYSTERESIS_BYTES = 512_000_000
STORAGE_RECOVERY_HEALTHY_SAMPLES = 3
STORAGE_RESERVE_BYTES = 512_000_000
STORAGE_MONITOR_INTERVAL_SECONDS = 15.0
# These three fields form the hash-covered, durable control-plane handshake
# with the Rust execution engine.  Generation zero is the only valid initial
# (unlatched, unacknowledged) state; emergency and recovery transitions always
# use a strictly positive, increasing generation.
STORAGE_CONTROL_GENERATION = 0
STORAGE_EMERGENCY_LATCHED = False
STORAGE_RECOVERY_ACKNOWLEDGED = False
STORAGE_RECOVERY_REQUEST_ID = ""
STORAGE_RECOVERY_REQUESTED_AT = ""
STORAGE_RECOVERY_REQUESTED_BY = ""

ADAPTIVE_THRESHOLDS_ENABLED = False
HEALTH_MONITOR_ENABLED = False
AI_REPORT_AGENT_ENABLED = False
ADAPTIVE_RULES_PAPER_ONLY = True
HEALTH_ALERT_ZSCORE = 3.0
HEALTH_SAFE_MODE_ZSCORE = 5.0
LOSS_STREAK_TRIGGER = 3
WIN_STREAK_RESET = 5
LOSS_STREAK_NOTIONAL_SCALE = 0.50
LOSS_STREAK_ENTRY_MULTIPLIER = 1.50
# Trades held shorter than this are excluded from the consecutive-loss streak.
# Forced exits (risk engine, bridge errors) typically close in under a minute;
# 0.25 h (15 min) is well below any intentional hold but above any churn artifact.
LOSS_STREAK_MIN_HOLD_HOURS = 0.25

DAILY_PNL_SUMMARY_HOUR_UTC = 0
DAILY_PNL_SUMMARY_MINUTE_UTC = 5

VALIDATION_GO_SHARPE_MIN = 2.0
VALIDATION_ADJUST_SHARPE_MIN = 0.0
VALIDATION_GO_MAX_DRAWDOWN_PCT = 0.10
VALIDATION_NO_GO_MAX_DRAWDOWN_PCT = 0.15
VALIDATION_ADJUST_NOTIONAL_SCALE = 0.50
VALIDATION_GO_MIN_INTERVENTION_FREE_DAYS = 30.0
VALIDATION_TARGET_MONTHLY_RETURN_MIN_PCT = 0.01
VALIDATION_TARGET_MONTHLY_RETURN_MAX_PCT = 0.03
VALIDATION_TARGET_WIN_RATE_MIN = 0.65
VALIDATION_TARGET_COST_MODEL_ERROR_MAX_PCT = 15.0
VALIDATION_TARGET_UPTIME_MIN_PCT = 99.5
VALIDATION_SNAPSHOT_INTERVAL_MINUTES = 60
OPERATOR_FLATTEN_ALL_REQUEST_ID = ""
OPERATOR_FLATTEN_ALL_REQUESTED_AT = ""
OPERATOR_FLATTEN_ALL_REQUESTED_BY = ""
STARTUP_RECOVERY_ACKNOWLEDGE_SYMBOLS: list[str] = []
STARTUP_RECOVERY_AUTO_EXIT_MANUAL_REVIEW = False
# Startup recovery EXIT throttles: one attempt per symbol per backoff window,
# then mark as stuck after this many consecutive rejects.
STARTUP_RECOVERY_EXIT_BACKOFF_S = 30.0
STARTUP_RECOVERY_EXIT_MAX_REJECTIONS = 3
RESET_EQUITY_HIGH_WATERMARK = False
# Auto-heal the equity high-watermark after an extended underwater period.
# 0.0 keeps existing behavior (manual reset only).
HWM_AUTO_DECAY_AFTER_HOURS = 0.0
# Fraction of the gap (HWM-current equity) to decay when the timer fires.
# 1.0 snaps to current equity; lower values decay gradually.
HWM_AUTO_DECAY_FRACTION = 0.25
