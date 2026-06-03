"""
Central configuration — edit this file to tune everything.
All guardrails, strategies, and risk rules live here.
"""
import os
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# ── Mode ──────────────────────────────────────────────────────────────────────
PAPER: bool = os.getenv("PAPER_MODE", "true").lower() == "true"

# ── Alpaca keys (auto-selected by mode) ───────────────────────────────────────
PAPER_API_KEY    = os.getenv("ALPACA_PAPER_API_KEY",    "")
PAPER_API_SECRET = os.getenv("ALPACA_PAPER_API_SECRET", "")
LIVE_API_KEY     = os.getenv("ALPACA_LIVE_API_KEY",     "")
LIVE_API_SECRET  = os.getenv("ALPACA_LIVE_API_SECRET",  "")

# ── Live-mode safety ──────────────────────────────────────────────────────────
LIVE_CONFIRM     = os.getenv("LIVE_CONFIRM",    "")     # must equal "confirm"
CAPITAL_LIMIT    = float(os.getenv("CAPITAL_LIMIT", "0"))

# ── Telegram alerts ───────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")     # Bot token from @BotFather
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")   # Your personal chat ID

# ── Gist persistence ──────────────────────────────────────────────────────────
GIST_TOKEN    = os.getenv("GIST_TOKEN", "")
GIST_ID       = os.getenv("GIST_ID",    "")
GIST_FILENAME = "trade_records.json"

# ── Watchlist ─────────────────────────────────────────────────────────────────
WATCHLIST: list[str] = ["NBIS", "NVDA", "TSLA", "AAPL", "MSFT", "AMD", "META"]

# ── Active strategies per time window ─────────────────────────────────────────
# Bot selects strategy automatically based on time of day
# ORB  = Opening Range Breakout  (9:45 – 10:30 AM ET)  — high momentum entries
# VWAP = VWAP Mean Reversion     (10:30 AM – 3:00 PM)  — midday pullback entries
# MOM  = Momentum continuation   (all day, secondary)  — trending stocks only
STRATEGIES = {
    "ORB":  {"start": (9, 45),  "end": (10, 30)},
    "VWAP": {"start": (10, 30), "end": (15, 0)},
}

# ── Strategy parameters ───────────────────────────────────────────────────────
EMA_FAST       = 9
EMA_SLOW       = 21
RSI_PERIOD     = 14
ATR_PERIOD     = 14

# VWAP strategy RSI zones
RSI_LONG_LOW   = 45
RSI_LONG_HIGH  = 62
RSI_SHORT_LOW  = 38
RSI_SHORT_HIGH = 55

# ORB strategy — breakout above/below opening range high/low
ORB_MINUTES    = 15    # opening range = first 15 min candles (9:30–9:45)
ORB_ATR_BUFFER = 0.25  # breakout must exceed range by 0.25x ATR to filter fakes

VOLUME_MULT    = 1.2   # volume must be > 1.2x 20-bar average
CONFIRM_BARS   = 2     # signal must hold for 2 consecutive bars

# ── Risk management ───────────────────────────────────────────────────────────
MAX_RISK_PCT       = 0.015   # max 1.5% of portfolio per trade
TARGET1_MULT       = 2.0     # T1 = 2x stop distance (close 50%)
TARGET2_MULT       = 3.0     # T2 = 3x stop distance (close 50%)
MAX_OPEN_TRADES    = 3       # never exceed 3 simultaneous positions
MAX_POSITION_PCT   = 0.20    # single position ≤ 20% of portfolio

# ── Circuit breakers (GUARDRAILS) ─────────────────────────────────────────────
DAILY_LOSS_LIMIT_PCT  = 0.02   # stop ALL trading if day P&L < -2% of portfolio
MAX_DRAWDOWN_PCT      = 0.06   # emergency stop if portfolio drops 6% from session high
MIN_ACCOUNT_BALANCE   = 1000.0 # hard floor — never trade if account < $1,000
PDT_MINIMUM_EQUITY    = 25000  # Pattern Day Trader rule — warn if below $25k in live mode
MAX_TRADES_PER_DAY    = 6      # hard limit on total trades placed per day
CORRELATED_SYMBOLS    = [      # never hold more than 1 of these at once
    ["NVDA", "AMD", "SMCI"],   # GPU/chip group
    ["AAPL", "MSFT", "META"],  # mega-cap tech group
]

# ── Timing ────────────────────────────────────────────────────────────────────
OPEN_BUFFER_MIN   = 15   # skip first 15 min after open (9:30–9:45)
CLOSE_BUFFER_MIN  = 30   # stop 30 min before close (after 3:30 PM)

# ── Records ───────────────────────────────────────────────────────────────────
RETENTION_DAYS = 7
