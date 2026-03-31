"""
Central configuration for the Delta-Neutral Funding Arbitrage Bot.
All tunable parameters live here so you can tweak them in one place.

Calibrated for: ~$10k demo account, multi-symbol, 5x max leverage.

PROFITABILITY TUNING (2026-03-28):
  - Higher entry threshold (3%) to filter low-quality trades
  - Longer hold periods to capture more funding
  - Fewer positions with larger size for efficiency
  - Conservative rotation to avoid overtrading

PHASE 1 OPTIMIZATIONS (2026-03-28):
  - Reduced MAKER_ORDER_PATIENCE_SEC from 30s to 15s for faster taker fallback
  - Increased CAPITAL_PER_SLOT_USD from $3k to $5k for better capital efficiency
  - Added MAKER_FILL_LOGGING for tracking actual vs expected fill rates
  - Added TRAILING_BASIS_STOP and DRAWDOWN_SCALE_FACTOR for risk management
"""

import os


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_symbols(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return list(default)
    parsed = [symbol.strip().upper() for symbol in raw.split(",") if symbol.strip()]
    return parsed or list(default)


def get_monitored_symbols() -> list[str]:
    return _env_symbols(
        "MONITORED_SYMBOLS",
        [
            "BTCUSDT",
            "ETHUSDT",
        ],
    )

# ── Account Sizing ────────────────────────────────────────────────────────
ACCOUNT_EQUITY_USD = _env_float("ACCOUNT_EQUITY_USD", 10_000)       # Starting demo account size
MAX_LEVERAGE = 5.0                # Hard cap on effective leverage

# ── Cost Model ────────────────────────────────────────────────────────────
# Binance VIP 0 w/ BNB discount (25% off spot fees — ensure BNB discount is ON in account)
TAKER_FEE_SPOT = 0.0005625  # 0.05625% spot taker (VIP 0 + BNB, was 0.075%)
TAKER_FEE_PERP = 0.0005     # 0.05% futures taker (VIP 0)
MAKER_FEE_SPOT = 0.0005625  # 0.05625% spot maker (VIP 0 + BNB, was 0.075%)
MAKER_FEE_PERP = 0.0002     # 0.02% futures maker (VIP 0)
TAKER_FEE = 0.00053125      # blended avg of spot+perp taker for legacy compat
MAKER_FEE = 0.00038125      # blended avg of spot+perp maker (no rebate at VIP 0)
SLIPPAGE_ESTIMATE = 0.0002  # 0.02% per leg baseline (scales with size in cost_model)

# Each action (open or close) touches 2 legs (spot + perp).
# A full round-trip is 2 actions x 2 legs = 4 crosses.
LEGS_PER_ACTION = 2
ACTIONS_PER_ROUND_TRIP = 2  # open + close

# Maker fill probability for blended cost estimation
# The Rust chase system tries limit orders first, falls back to market
# Conservative estimate to avoid overestimating profits
MAKER_FILL_PROBABILITY = 0.60

# ── Funding Schedule ─────────────────────────────────────────────────────
FUNDING_INTERVAL_HOURS = 8       # Binance/Bybit default: every 8 hours
FUNDING_PERIODS_PER_DAY = 24 / FUNDING_INTERVAL_HOURS  # 3
FUNDING_PERIODS_PER_YEAR = FUNDING_PERIODS_PER_DAY * 365  # 1095

# Snapshot hours (UTC) at which funding is paid
FUNDING_SNAPSHOT_HOURS = [0, 8, 16]

# ── Entry Thresholds ─────────────────────────────────────────────────────
# Higher thresholds = fewer but higher-quality trades
# Only enter when funding is likely to remain elevated
ENTRY_ANN_FUNDING_THRESHOLD = 0.03   # 3% annualized minimum (was 1%)
ENTRY_ANN_FUNDING_THRESHOLD_BTC = 0.02   # 2% for BTC (lower due to stability)
ENTRY_ANN_FUNDING_THRESHOLD_ALT = 0.05    # 5% for altcoins (higher risk = higher threshold)
ENTRY_PREMIUM_THRESHOLD = 0.0003     # 0.03% perp premium over spot

# ── Exit Thresholds ──────────────────────────────────────────────────────
# Higher exit threshold = let winners run longer
# Only exit when funding collapses OR basis inverts
EXIT_ANN_FUNDING_THRESHOLD = 0.01    # 1% annualized - exit when funding drops below this
EXIT_DISCOUNT_THRESHOLD = -0.0003    # -0.03% — stop on basis inversion
BASIS_DEVIATION_STOP = 0.003         # 0.3% — hard stop if basis deviates from entry basis

# ── Hold-Through-Funding Settings ──────────────────────────────────────────
HOLD_THROUGH_FUNDING = True          # Hold positions until AFTER funding payment
FUNDING_CAPTURE_DELAY_MIN = 5        # Minutes to wait after funding before evaluating exit

# ── Snapshot Snipe Mode ──────────────────────────────────────────────────
# Disabled for the release candidate until the cost model is recalibrated
# against realized paper/testnet fills.
SNAPSHOT_SNIPE_ENABLED = False
SNIPE_ANN_FUNDING_THRESHOLD = 0.30   # 30% annualized — only snipe when funding covers costs
SNIPE_ENTRY_WINDOW_MIN = 15          # Enter 15-30 minutes before snapshot (tightened)
SNIPE_ENTRY_WINDOW_MAX = 30

# ── Capital ───────────────────────────────────────────────────────────────
# Reduced for $10k account: $2,500 notional = 25% per trade, allows 4 concurrent positions
NOTIONAL_PER_TRADE = 2_500       # USD notional per side (was $20,000, too large for $10k account)
MAX_NOTIONAL_PER_TRADE = 5_000   # Hard cap even with Kelly scaling (was $50,000)

# ── Margin / Borrowing Cost ─────────────────────────────────────────────
MARGIN_BORROW_RATE_ANNUAL = 0.10     # 10% annual interest on borrowed USDT (Binance typical)

# ── Data & Latency Controls ──────────────────────────────────────────────
MAX_ALLOWED_GAP_MINUTES = 1
MAX_FUNDING_STALENESS_MINUTES = 8 * 60

# ── Risk Limits ───────────────────────────────────────────────────────────
MAX_GROSS_EXPOSURE_USD = _env_float("MAX_GROSS_EXPOSURE_USD", 50_000)  # Hard 5x cap on $10k account
MAX_SYMBOL_CONCENTRATION = 0.60  # Slightly relaxed for BTCUSDT-only focus
SOFT_DRAWDOWN_PCT = 0.04         # 4% — triggers position scale reduction
MAX_DRAWDOWN_PCT = 0.10          # 10% — triggers kill switch
MAX_VENUE_LATENCY_MS = 400

# ── Research Acceptance Gates ─────────────────────────────────────────────
WF_MIN_AVG_OOS_EDGE = 0.0
WF_MIN_WINDOWS_PASSING = 2
WF_MIN_TRADES_PER_WINDOW = 10
WF_MIN_SIGNAL_TO_NOISE = 0.1

# ── Multi-Symbol ─────────────────────────────────────────────────────────────
MONITORED_SYMBOLS = get_monitored_symbols()

# ── Capital Allocation ────────────────────────────────────────────────────────
# Release-candidate defaults are intentionally conservative: two-symbol paper
# certification, $2.5k capital per slot, and 5x depth required before entry.
MAX_CONCURRENT_POSITIONS = 2
CAPITAL_PER_SLOT_USD = 2_500
TARGET_LEVERAGE = 2.0
LIQUIDITY_FILTER_MULTIPLIER = 5.0

# ── Rotation ──────────────────────────────────────────────────────────────────
# Conservative rotation = avoid overtrading and eating into funding profits
ROTATION_MIN_GAP_ANN = 0.03
ROTATION_MAX_PAYBACK_DAYS = 0.333     # 8h maximum payback
ROTATION_CONFIRM_TIMEOUT_S = 30

# ── Maker Order Settings ──────────────────────────────────────────────────────
MAKER_ORDER_PATIENCE_SEC = 15         # Seconds to wait for maker fill before switching to taker (Phase 1: was 30s)
MAKER_REBATE_SPOT = 0.0001           # 0.01% maker rebate on spot (if available)
MAKER_REBATE_PERP = 0.00005          # 0.005% maker rebate on perp (if available)

# ── Maker Fill Logging (Phase 1) ─────────────────────────────────────────────
MAKER_FILL_LOGGING_ENABLED = True      # Log actual maker vs taker fills for calibration

# ── Circuit Breaker ───────────────────────────────────────────────────────────
BREAKER_WARN_RATIO = 0.33             # ≥ 33% of positions troubled → WARNED (entries still allowed)
BREAKER_HALT_RATIO = 0.50             # ≥ 50% of positions troubled → HALTED (block new entries)
BREAKER_PARTIAL_RATIO = 0.75          # ≥ 75% of positions troubled → PARTIAL_EXIT (exit most troubled half)
BREAKER_EMERGENCY_RATIO = 1.00        # 100% of positions troubled → EMERGENCY (exit all)

# ── Dynamic Symbol Universe ───────────────────────────────────────────────────
DYNAMIC_SYMBOL_MODE = False           # True requires Rust engine to also track dynamic symbols
MAX_MONITORED_SYMBOLS = 30            # Max symbols to track from Binance perps
MAX_DEPTH_SUBSCRIPTIONS = 15          # Max WS depth streams (rotate to top-N by funding)

# ── Inverse Funding Mode ──────────────────────────────────────────────────────
# Disabled until there is a real borrow / short-spot workflow behind it.
INVERSE_FUNDING_ENABLED = False

# ── Sentiment Overlay ─────────────────────────────────────────────────────────
# Disabled by default until it proves value in paper/live evaluation.
SENTIMENT_ENABLED = False

# ── Dynamic Leverage Scaling ──────────────────────────────────────────────────
# Scale notional with funding magnitude; basis-deviation stop bounds the risk
LEVERAGE_TIERS = [
    (0.25, 2.0),   # ann_funding < 25%  → 2x leverage
    (0.50, 3.0),   # ann_funding < 50%  → 3x leverage
    (1.00, 4.0),   # ann_funding < 100% → 4x leverage
    (float("inf"), 5.0),  # ann_funding >= 100% → 5x leverage (MAX_LEVERAGE cap)
]

# ── Funding Decay Prediction ──────────────────────────────────────────────────
FUNDING_PREDICTOR_SAMPLES = 28_800    # Rolling window: 8 h × 3600 s/h = one full funding epoch at 1 sample/s

# ── Basis-Aware Regime Filter ─────────────────────────────────────────────────
# Blocks new entries/rotations when basis, price action, or liquidity look toxic.
REGIME_FILTER_ENABLED = True
REGIME_FILTER_MIN_SAMPLES = 20         # Need enough observations before trusting the filter
REGIME_FILTER_BASIS_ZSCORE_MAX = 2.5   # Block when current basis is a large outlier vs recent history
REGIME_FILTER_BASIS_ABS_FLOOR = 0.0008 # Ignore tiny basis moves (< 8 bps) even if z-score is high
REGIME_FILTER_PRICE_SHOCK_PCT = 0.015  # Block if recent perp mark range exceeds 1.5%
REGIME_FILTER_DEPTH_RATIO_MIN = 0.50   # Block if entry depth falls below 50% of recent median
REGIME_FILTER_FUNDING_DISPERSION_MAX = 3.0
REGIME_FILTER_BASIS_WIDENING_MAX = 3.0
REGIME_FILTER_VOLUME_SPIKE_MAX = 5.0

# ── Cooldown Breakers ──────────────────────────────────────────────────────────
# Pause after stressed conditions so the bot doesn't immediately re-enter.
COOLDOWN_ENABLED = True
COOLDOWN_HALTED_MINUTES = 30           # HALTED breaker → 30 minute global cooldown
COOLDOWN_PARTIAL_EXIT_MINUTES = 60     # PARTIAL_EXIT breaker → 1 hour global cooldown
COOLDOWN_EMERGENCY_MINUTES = 240       # EMERGENCY breaker → 4 hour global cooldown
COOLDOWN_SYMBOL_MINUTES = 120          # Symbols exited under stress stay sidelined for 2 hours

# ── Trailing Basis Stop (Phase 1) ─────────────────────────────────────────────
# Lock in profits when basis moves favorably, protect against reversals
TRAILING_BASIS_STOP_ENABLED = True    # Enable trailing basis stop
TRAILING_BASIS_STOP_LOCK_PCT = 0.50   # Lock in 50% of peak profit as buffer
TRAILING_BASIS_STOP_TRAIL_BPS = 15    # Trail by 15 bps from peak

# ── Position Scaling on Drawdown (Phase 1) ───────────────────────────────────
# Reduce position size when drawdown hits thresholds instead of just halting
DRAWDOWN_SCALE_FACTOR = {
    0.02: 0.75,   # At 2% drawdown: use 75% of normal size
    0.05: 0.50,   # At 5% drawdown: use 50% of normal size
    0.10: 0.25,   # At 10% drawdown: use 25% of normal size
}

# ── Auto-Compounding (Phase 3) ─────────────────────────────────────────────────
# Automatically scale capital allocation based on equity growth
AUTO_COMPOUND_ENABLED = True           # Enable auto-compounding
COMPOUND_UPDATE_INTERVAL_HOURS = 24     # Recalculate equity every 24 hours
COMPOUND_HIGH_WATERMARK = True          # Only increase on new highs
COMPOUND_MIN_EQUITY_PCT = 0.02         # Minimum 2% gain before increasing
COMPOUND_MAX_EQUITY_PCT = 1.00         # Maximum 100% increase from initial
COMPOUND_AGGESSION = 0.50              # 50% of gains go to capital (conservative)

# ── Reliability / Production Controls ───────────────────────────────────────
HEARTBEAT_INTERVAL_SECONDS = 10
HEARTBEAT_MISS_THRESHOLD = 3
PENDING_INTENT_MAX_AGE_SECONDS = 300
DATA_RETENTION_DAYS = 90
MARKET_SAMPLE_RETENTION_DAYS = 21
HEALTH_SAMPLE_RETENTION_DAYS = 21

# ── Adaptive Controls ────────────────────────────────────────────────────────
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

# ── Scheduling ───────────────────────────────────────────────────────────────
DAILY_PNL_SUMMARY_HOUR_UTC = 0
DAILY_PNL_SUMMARY_MINUTE_UTC = 5

# ── Phase 4 Validation ───────────────────────────────────────────────────────
VALIDATION_GO_SHARPE_MIN = 2.0
VALIDATION_ADJUST_SHARPE_MIN = 1.0
VALIDATION_GO_MAX_DRAWDOWN_PCT = 0.10
VALIDATION_NO_GO_MAX_DRAWDOWN_PCT = 0.15
VALIDATION_GO_MIN_INTERVENTION_FREE_DAYS = 14.0
VALIDATION_TARGET_MONTHLY_RETURN_MIN_PCT = 0.01
VALIDATION_TARGET_MONTHLY_RETURN_MAX_PCT = 0.03
VALIDATION_TARGET_WIN_RATE_MIN = 0.65
VALIDATION_TARGET_COST_MODEL_ERROR_MAX_PCT = 15.0
VALIDATION_TARGET_UPTIME_MIN_PCT = 99.0
VALIDATION_SNAPSHOT_INTERVAL_MINUTES = 60
