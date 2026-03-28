"""
config.py - Bot configuration v2.0
All tunable parameters live here.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Alpaca credentials ────────────────────────────────────────────────────────
BASE_URL   = os.getenv("ALPACA_BASE_URL",   "https://paper-api.alpaca.markets")
API_KEY    = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

# ── Trading universe ──────────────────────────────────────────────────────────
TICKERS = [
    # Mega-cap tech
    "TSLA", "NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "AMD",
    # Leveraged ETFs (high volatility)
    "TQQQ", "SOXL", "SPXL", "LABU", "FNGU", "TECL",
    # Broad market ETFs
    "SPY", "QQQ", "IWM",
    # High volatility individual stocks
    "MSTR", "COIN", "PLTR", "RKLB", "SMCI",
]

# ── Capital ───────────────────────────────────────────────────────────────────
STARTING_CAPITAL      = 10_000.0   # Simulated starting balance (USD)
MAX_POSITION_SIZE_PCT = 0.10       # Max 10% of capital per trade
MAX_CONCURRENT_POSITIONS = 3       # Max open positions at once

# ── Risk management ───────────────────────────────────────────────────────────
STOP_LOSS_PCT = 0.50               # Initial hard stop: -50% from entry

# ── Improvement #6: Trailing take-profit ──────────────────────────────────────
# Each tuple: (profit_trigger_pct, new_floor_pct)
# Once peak PnL exceeds trigger, the stop floor is raised to floor.
TRAILING_STOPS = [
    (0.50, 0.00),    # +50% gain  → move stop to breakeven
    (1.00, 0.50),    # +100% gain → trail at +50%
    (2.00, 1.50),    # +200% gain → trail at +150%
    (5.00, 4.00),    # +500% gain → trail at +400%
]

# ── RSI settings (base thresholds — may be tightened by VIX regime) ───────────
RSI_PERIOD     = 14
RSI_OVERSOLD   = 40    # 1-min RSI below this → downtrend signal (buy put)
RSI_OVERBOUGHT = 60    # 1-min RSI above this → uptrend signal (buy call)

# ── Volume momentum ───────────────────────────────────────────────────────────
VOLUME_LOOKBACK  = 20  # Rolling window for avg volume
VOLUME_THRESHOLD = 1.5 # Current vol must be 1.5× avg to confirm signal

# ── Options ───────────────────────────────────────────────────────────────────
DTE_MIN     = 1        # Skip same-day expiry
DTE_MAX     = 3        # Buy 1–3 DTE contracts
MIN_PREMIUM = 0.15     # Skip options cheaper than $0.15

# ── Improvement #1: IV Rank filter ───────────────────────────────────────────
IV_RANK_MAX          = 35   # Only trade when IV rank < 35th percentile (cheap options)
IV_HISTORY_DAYS      = 30   # Rolling window for IV history (days)
MIN_IV_HISTORY_POINTS = 5   # Min data points before IV rank filter activates

# ── Improvement #3: Trade-window filter (ET time, inclusive) ─────────────────
# Each tuple: (start_hour, start_min, end_hour, end_min)
TRADE_WINDOWS_ET = [
    (10, 0,  11, 30),   # Post-open momentum window
    (14, 0,  15, 30),   # Afternoon trend continuation
]

# ── Improvement #4: VIX regime thresholds ────────────────────────────────────
VIX_LOW     = 20   # Below: normal conditions, standard thresholds
VIX_HIGH    = 30   # 20–30: elevated fear, tighten thresholds, use spreads
VIX_EXTREME = 40   # Above: panic/capitulation, flip to mean-reversion calls

# ── Improvement #5: Vertical spread settings ─────────────────────────────────
SPREAD_WIDTH_PCT = 0.02   # Short leg strike 2% further OTM than long leg

# ── Scan timing ───────────────────────────────────────────────────────────────
SCAN_INTERVAL_SECONDS    = 60   # Scan for new signals every 60s
MONITOR_INTERVAL_SECONDS = 30   # Check open positions every 30s
ROI_REPORT_INTERVAL_MIN  = 5    # Print ROI summary every 5 min
