"""
Central configuration for the Delta-Neutral Funding Arbitrage Bot.
All tunable parameters live here so you can tweak them in one place.

Calibrated for: ~$10k demo account, BTCUSDT, 5x max leverage.
"""

# ── Account Sizing ────────────────────────────────────────────────────────
ACCOUNT_EQUITY_USD = 10_000       # Starting demo account size
MAX_LEVERAGE = 5.0                # Hard cap on effective leverage

# ── Cost Model ────────────────────────────────────────────────────────────
# Binance VIP 0 w/ BNB discount (update these to match your actual tier)
TAKER_FEE_SPOT = 0.00075    # 0.075% spot taker (VIP 0 + BNB)
TAKER_FEE_PERP = 0.0005     # 0.05% futures taker (VIP 0)
MAKER_FEE_SPOT = 0.00075    # 0.075% spot maker (VIP 0 + BNB, no rebate)
MAKER_FEE_PERP = 0.0002     # 0.02% futures maker (VIP 0)
TAKER_FEE = 0.000625        # blended avg of spot+perp taker for legacy compat
MAKER_FEE = 0.000475        # blended avg of spot+perp maker (no rebate at VIP 0)
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
# 15% ensures break-even within 2-3 snapshots even under taker-only costs
ENTRY_ANN_FUNDING_THRESHOLD = 0.15   # 15% annualized threshold to enter
ENTRY_PREMIUM_THRESHOLD = 0.0003     # 0.03% perp premium over spot

# ── Exit Thresholds ──────────────────────────────────────────────────────
# Tightened: exit at 1% instead of 0% to lock in profits before funding flips
EXIT_ANN_FUNDING_THRESHOLD = 0.01    # 1% annualized — exit when funding drops too low
EXIT_DISCOUNT_THRESHOLD = -0.0003    # -0.03% — tighter stop on basis inversion
BASIS_DEVIATION_STOP = 0.003         # 0.3% — hard stop if basis deviates from entry basis

# ── Snapshot Snipe Mode ──────────────────────────────────────────────────
# Must exceed round-trip cost in a single snapshot to be positive-EV
SNIPE_ANN_FUNDING_THRESHOLD = 0.30   # 30% annualized — only snipe when funding covers costs
SNIPE_ENTRY_WINDOW_MIN = 60          # Enter 60-120 minutes before snapshot
SNIPE_ENTRY_WINDOW_MAX = 120

# ── Capital ───────────────────────────────────────────────────────────────
NOTIONAL_PER_TRADE = 20_000      # USD notional per side (2x leverage baseline)
MAX_NOTIONAL_PER_TRADE = 50_000  # Hard cap even with Kelly scaling (5x)

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
