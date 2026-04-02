"""Canonical configuration for the Bongus funding arbitrage bot.

Static defaults live here. Runtime overrides belong in ``live_config.json``
and are hot-reloaded by :mod:`bongus.core.config_manager`.
"""

from __future__ import annotations

# ── Runtime Identity ──────────────────────────────────────────────────────
CANONICAL_RUNTIME_NAME = "multi_symbol_funding_bot"
LIVE_CONFIG_PATH = "live_config.json"
STATE_DB_PATH = "state.db"

# ── Account Sizing ────────────────────────────────────────────────────────
ACCOUNT_EQUITY_USD = 10_000.0
MAX_LEVERAGE = 2.0
TARGET_CONCURRENT_POSITIONS = 4
MIN_TOP_N = 3
MAX_TOP_N = 5
SLOT_NOTIONAL_USD = 5_000.0
NOTIONAL_PER_TRADE = SLOT_NOTIONAL_USD
MAX_NOTIONAL_PER_TRADE = 7_500.0
MAX_GROSS_EXPOSURE_USD = 20_000.0
PER_SYMBOL_NOTIONAL_CAP_USD = 5_000.0
PER_CLUSTER_NOTIONAL_CAP_USD = 10_000.0

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

# ── Risk Limits ───────────────────────────────────────────────────────────
MAX_SYMBOL_CONCENTRATION = 0.30
SOFT_DRAWDOWN_PCT = 0.04
MAX_DRAWDOWN_PCT = 0.10
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
SCANNER_MAX_CANDIDATES = 64

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
