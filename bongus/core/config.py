"""
Central configuration for the Delta-Neutral Funding Arbitrage Bot.
All tunable parameters live here so you can tweak them in one place.

Calibrated for: ~$10k demo account, multi-symbol, 5x max leverage.
"""

# ── Account Sizing ────────────────────────────────────────────────────────
ACCOUNT_EQUITY_USD = 10_000       # Starting demo account size
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
MAKER_FILL_PROBABILITY = 0.70

# ── Funding Schedule ─────────────────────────────────────────────────────
FUNDING_INTERVAL_HOURS = 8       # Binance/Bybit default: every 8 hours
FUNDING_PERIODS_PER_DAY = 24 / FUNDING_INTERVAL_HOURS  # 3
FUNDING_PERIODS_PER_YEAR = FUNDING_PERIODS_PER_DAY * 365  # 1095

# Snapshot hours (UTC) at which funding is paid
FUNDING_SNAPSHOT_HOURS = [0, 8, 16]

# ── Entry Thresholds ─────────────────────────────────────────────────────
# Lowered to 8% for BTC (realistic in normal/volatile markets)
# Altcoins can use higher thresholds via symbol-specific config
ENTRY_ANN_FUNDING_THRESHOLD = 0.01   # Capped down to enter more often
ENTRY_ANN_FUNDING_THRESHOLD_BTC = 0.01
ENTRY_ANN_FUNDING_THRESHOLD_ALT = 0.01  # Higher threshold for altcoins
ENTRY_PREMIUM_THRESHOLD = 0.0003     # 0.03% perp premium over spot

# ── Exit Thresholds ──────────────────────────────────────────────────────
# Exit at 5% to let winners run - funding often stays elevated
EXIT_ANN_FUNDING_THRESHOLD = 0.005    # Exit when funding drops significantly
EXIT_DISCOUNT_THRESHOLD = -0.0003    # -0.03% — stop on basis inversion
BASIS_DEVIATION_STOP = 0.003         # 0.3% — hard stop if basis deviates from entry basis

# ── Hold-Through-Funding Settings ──────────────────────────────────────────
HOLD_THROUGH_FUNDING = True          # Hold positions until AFTER funding payment
FUNDING_CAPTURE_DELAY_MIN = 5        # Minutes to wait after funding before evaluating exit

# ── Snapshot Snipe Mode ──────────────────────────────────────────────────
# Must exceed round-trip cost in a single snapshot to be positive-EV
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
MAX_GROSS_EXPOSURE_USD = 50_000  # Hard 5x cap on $10k account
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
MONITORED_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT",
    "PEPEUSDT", "BNBUSDT", "ARBUSDT", "SUIUSDT",
]

# ── Capital Allocation ────────────────────────────────────────────────────────
MAX_CONCURRENT_POSITIONS = 4
CAPITAL_PER_SLOT_USD = 2_500          # ACCOUNT_EQUITY_USD / MAX_CONCURRENT_POSITIONS
TARGET_LEVERAGE = 2.0                  # notional = CAPITAL_PER_SLOT_USD * TARGET_LEVERAGE = $5K
LIQUIDITY_FILTER_MULTIPLIER = 1.0     # skip if min(spot_ask, perp_bid) < 1× notional

# ── Rotation ──────────────────────────────────────────────────────────────────
ROTATION_MIN_GAP_ANN = 0.03           # 3% annualized minimum rate gap to trigger rotation (lowered)
ROTATION_MAX_PAYBACK_DAYS = 0.333     # fees must pay back within 1 funding period (8h)
ROTATION_CONFIRM_TIMEOUT_S = 30       # seconds to wait for FILLED confirmation (increased for maker orders)

# ── Maker Order Settings ──────────────────────────────────────────────────────
MAKER_ORDER_PATIENCE_SEC = 30         # Seconds to wait for maker fill before switching to taker
MAKER_REBATE_SPOT = 0.0001           # 0.01% maker rebate on spot (if available)
MAKER_REBATE_PERP = 0.00005          # 0.005% maker rebate on perp (if available)

# ── Circuit Breaker ───────────────────────────────────────────────────────────
BREAKER_HALT_RATIO = 0.50             # ≥ 50% of positions negative → HALTED
BREAKER_EMERGENCY_RATIO = 1.00        # 100% of positions negative → EMERGENCY

# ── Dynamic Symbol Universe ───────────────────────────────────────────────────
DYNAMIC_SYMBOL_MODE = False           # True requires Rust engine to also track dynamic symbols
MAX_MONITORED_SYMBOLS = 30            # Max symbols to track from Binance perps
MAX_DEPTH_SUBSCRIPTIONS = 15          # Max WS depth streams (rotate to top-N by funding)

# ── Inverse Funding Mode ──────────────────────────────────────────────────────
INVERSE_FUNDING_ENABLED = True        # Short spot + long perp when funding is negative

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
