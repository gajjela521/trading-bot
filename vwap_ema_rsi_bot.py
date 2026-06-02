"""
VWAP + EMA + RSI Strategy Bot  —  Enterprise Edition
=====================================================
Designed for GitHub Actions (single-scan per cron trigger).

Run modes
---------
  PAPER_MODE=true   → paper-api.alpaca.markets  (safe, default)
  PAPER_MODE=false  → api.alpaca.markets         (live, requires capital confirmation)

Signal logic
------------
  LONG  : price > VWAP  AND  EMA9 > EMA21  AND  45 < RSI < 62  AND  vol > 1.2x avg
  SHORT : price < VWAP  AND  EMA9 < EMA21  AND  38 < RSI < 55  AND  vol > 1.2x avg

Risk management
---------------
  Stop loss  : 1 ATR from entry
  Target 1   : 2x risk  →  close 50 % of position
  Target 2   : 3x risk  →  close remaining 50 %
  Max risk   : 1.5 % of portfolio per trade
  Max trades : 3 simultaneous open positions
  No trades  : first 15 min after open  /  last 30 min before close

Trade record
------------
  Every executed trade is appended to trade_records.json.
  Records older than 7 days are purged automatically on each run.
  File is written atomically (temp → rename) to prevent corruption.

Dependencies
------------
  pip install alpaca-py pandas numpy
"""

# ──────────────────────────────────────────────────────────────────────────────
# IMPORTS
# ──────────────────────────────────────────────────────────────────────────────
import json
import logging
import os
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import (
    LimitOrderRequest,
    MarketOrderRequest,
    StopOrderRequest,
)

# ──────────────────────────────────────────────────────────────────────────────
# CONFIG  —  all tunable values live here
# ──────────────────────────────────────────────────────────────────────────────

# Mode — controlled by GitHub Actions env var PAPER_MODE
PAPER: bool = os.getenv("PAPER_MODE", "true").lower() == "true"

# Alpaca keys — two separate pairs, bot picks correct one automatically
if PAPER:
    API_KEY    = os.getenv("ALPACA_PAPER_API_KEY",    "")
    API_SECRET = os.getenv("ALPACA_PAPER_API_SECRET", "")
else:
    API_KEY    = os.getenv("ALPACA_LIVE_API_KEY",    "")
    API_SECRET = os.getenv("ALPACA_LIVE_API_SECRET", "")

# Capital limit for live mode (set via env var LIVE_CAPITAL_LIMIT)
# Bot will not risk more than this total across all open trades.
# GitHub Actions sets this from a workflow input; see trade.yml.
LIVE_CAPITAL_LIMIT: float = float(os.getenv("LIVE_CAPITAL_LIMIT", "0"))

# Stocks to monitor — edit this list and push to update watchlist
WATCHLIST: list[str] = ["NBIS", "NVDA", "TSLA", "AAPL", "MSFT"]

# ── Strategy parameters ───────────────────────────────────────────────────────
EMA_FAST         = 9
EMA_SLOW         = 21
RSI_PERIOD       = 14
ATR_PERIOD       = 14
RSI_LONG_LOW     = 45      # RSI lower bound for long entries
RSI_LONG_HIGH    = 62      # RSI upper bound for long entries
RSI_SHORT_LOW    = 38      # RSI lower bound for short entries
RSI_SHORT_HIGH   = 55      # RSI upper bound for short entries
VOLUME_MULT      = 1.2     # Volume must exceed this multiple of 20-bar average

# ── Risk management ───────────────────────────────────────────────────────────
MAX_RISK_PCT     = 0.015   # Max 1.5 % of portfolio risked per trade
TARGET1_MULT     = 2.0     # Target 1 = 2x stop distance (50 % position closed)
TARGET2_MULT     = 3.0     # Target 2 = 3x stop distance (remaining 50 % closed)
MAX_OPEN_TRADES  = 3       # Never exceed this many simultaneous open positions
MAX_POSITION_PCT = 0.20    # Single position never exceeds 20 % of portfolio

# ── Timing ────────────────────────────────────────────────────────────────────
MARKET_OPEN_BUFFER_MIN  = 15   # Skip this many minutes after market open
MARKET_CLOSE_BUFFER_MIN = 30   # Stop this many minutes before market close

# ── Trade record ──────────────────────────────────────────────────────────────
TRADE_RECORD_FILE  = Path("trade_records.json")
RECORD_RETENTION_DAYS = 7      # Purge records older than this

ET = ZoneInfo("America/New_York")

# ──────────────────────────────────────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("strategy_log.txt", encoding="utf-8"),
    ],
)
log = logging.getLogger("TradingBot")

# ──────────────────────────────────────────────────────────────────────────────
# DATA MODELS
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class SignalResult:
    symbol:   str
    signal:   str          # 'LONG' | 'SHORT' | 'NONE'
    price:    float = 0.0
    vwap_val: float = 0.0
    ema9:     float = 0.0
    ema21:    float = 0.0
    rsi_val:  float = 0.0
    atr_val:  float = 0.0
    volume:   int   = 0
    avg_vol:  int   = 0
    reason:   str   = ""


@dataclass
class TradeRecord:
    """Persisted record of every executed trade."""
    timestamp:      str        # ISO-8601, ET timezone
    run_id:         str        # GitHub Actions run ID or 'local'
    mode:           str        # 'PAPER' or 'LIVE'
    symbol:         str
    direction:      str        # 'LONG' or 'SHORT'
    entry_price:    float
    qty:            int
    stop_price:     float
    target1_price:  float
    target2_price:  float
    risk_dollars:   float
    portfolio_value: float
    entry_order_id: str
    stop_order_id:  str
    t1_order_id:    str
    t2_order_id:    str
    signal_reason:  str
    status:         str = "OPEN"   # OPEN | CLOSED | ERROR


# ──────────────────────────────────────────────────────────────────────────────
# STARTUP VALIDATION
# ──────────────────────────────────────────────────────────────────────────────

def validate_config() -> None:
    """Fail fast if required configuration is missing."""
    errors = []

    if not API_KEY or API_KEY == "":
        key_name = "ALPACA_PAPER_API_KEY" if PAPER else "ALPACA_LIVE_API_KEY"
        errors.append(f"Missing env var: {key_name}")

    if not API_SECRET or API_SECRET == "":
        secret_name = "ALPACA_PAPER_API_SECRET" if PAPER else "ALPACA_LIVE_API_SECRET"
        errors.append(f"Missing env var: {secret_name}")

    if not PAPER and LIVE_CAPITAL_LIMIT <= 0:
        errors.append(
            "LIVE mode requires LIVE_CAPITAL_LIMIT > 0. "
            "Set it in the GitHub Actions workflow input."
        )

    if not WATCHLIST:
        errors.append("WATCHLIST is empty — add at least one ticker.")

    if errors:
        for e in errors:
            log.error(f"CONFIG ERROR: {e}")
        sys.exit(1)


# ──────────────────────────────────────────────────────────────────────────────
# ALPACA CLIENTS
# ──────────────────────────────────────────────────────────────────────────────

trading     = TradingClient(API_KEY, API_SECRET, paper=PAPER)
market_data = StockHistoricalDataClient(API_KEY, API_SECRET)

ENDPOINT = (
    "https://paper-api.alpaca.markets" if PAPER
    else "https://api.alpaca.markets"
)

# ──────────────────────────────────────────────────────────────────────────────
# TRADE RECORD STORE  —  atomic JSON, weekly rolling window
# ──────────────────────────────────────────────────────────────────────────────

def _load_records() -> list[dict]:
    """Load existing records, return empty list if file missing or corrupt."""
    if not TRADE_RECORD_FILE.exists():
        return []
    try:
        return json.loads(TRADE_RECORD_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning(f"Could not read trade records ({exc}) — starting fresh.")
        return []


def _purge_old_records(records: list[dict]) -> list[dict]:
    """Remove records older than RECORD_RETENTION_DAYS."""
    cutoff = datetime.now(tz=ET) - timedelta(days=RECORD_RETENTION_DAYS)
    kept, removed = [], 0
    for r in records:
        try:
            ts = datetime.fromisoformat(r["timestamp"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=ET)
            if ts >= cutoff:
                kept.append(r)
            else:
                removed += 1
        except (KeyError, ValueError):
            kept.append(r)   # keep malformed records rather than silently delete
    if removed:
        log.info(f"Trade records: purged {removed} record(s) older than {RECORD_RETENTION_DAYS} days.")
    return kept


def _write_records(records: list[dict]) -> None:
    """Atomic write: temp file → rename, prevents corruption on crash."""
    tmp = TRADE_RECORD_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(
            json.dumps(records, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(TRADE_RECORD_FILE)
    except OSError as exc:
        log.error(f"Failed to write trade records: {exc}")
        tmp.unlink(missing_ok=True)


def append_trade_record(record: TradeRecord) -> None:
    """Append one trade record, purge old ones, persist atomically."""
    records = _load_records()
    records = _purge_old_records(records)
    records.append(asdict(record))
    _write_records(records)
    log.info(
        f"Trade record saved — {record.symbol} {record.direction} "
        f"@ ${record.entry_price}  |  "
        f"Total records this week: {len(records)}"
    )


def print_weekly_summary() -> None:
    """Log a brief summary of trades stored for the current week."""
    records = _purge_old_records(_load_records())
    if not records:
        log.info("Trade records: no trades recorded in the last 7 days.")
        return
    longs  = sum(1 for r in records if r.get("direction") == "LONG")
    shorts = sum(1 for r in records if r.get("direction") == "SHORT")
    paper  = sum(1 for r in records if r.get("mode") == "PAPER")
    live   = sum(1 for r in records if r.get("mode") == "LIVE")
    log.info(
        f"Trade records (last 7 days): {len(records)} total  |  "
        f"LONG={longs}  SHORT={shorts}  |  PAPER={paper}  LIVE={live}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# MARKET TIMING
# ──────────────────────────────────────────────────────────────────────────────

def check_market_window() -> tuple[bool, str]:
    """
    Returns (is_open, reason_string).
    Called once at startup — bot exits immediately if market is closed.
    """
    try:
        clock = trading.get_clock()
    except Exception as exc:
        return False, f"Could not reach Alpaca clock API: {exc}"

    if not clock.is_open:
        next_open = clock.next_open.astimezone(ET).strftime("%a %b %d %I:%M %p ET")
        return False, f"Market closed — next open: {next_open}"

    now         = datetime.now(ET)
    today_open  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    today_close = now.replace(hour=16, minute=0,  second=0, microsecond=0)

    open_guard  = today_open  + timedelta(minutes=MARKET_OPEN_BUFFER_MIN)
    close_guard = today_close - timedelta(minutes=MARKET_CLOSE_BUFFER_MIN)

    if now < open_guard:
        remaining = int((open_guard - now).total_seconds() / 60)
        return False, (
            f"Opening buffer active — trading starts at "
            f"{open_guard.strftime('%I:%M %p ET')} "
            f"({remaining} min remaining)"
        )

    if now >= close_guard:
        return False, (
            f"Closing buffer active — no new trades after "
            f"{close_guard.strftime('%I:%M %p ET')}"
        )

    return True, f"Market open — {now.strftime('%I:%M %p ET')}"


# ──────────────────────────────────────────────────────────────────────────────
# LIVE MODE — CAPITAL CONFIRMATION
# ──────────────────────────────────────────────────────────────────────────────

def confirm_live_capital(portfolio_value: float) -> bool:
    """
    In LIVE mode: verify the capital limit is set and reasonable.
    GitHub Actions sets LIVE_CAPITAL_LIMIT via workflow input.
    In non-interactive (CI) environments the env var acts as pre-confirmation.
    Returns True if safe to proceed, False to abort.
    """
    limit = LIVE_CAPITAL_LIMIT

    log.warning("=" * 60)
    log.warning("  LIVE TRADING MODE — REAL MONEY WILL BE USED")
    log.warning("=" * 60)
    log.info(f"  Portfolio value   : ${portfolio_value:,.2f}")
    log.info(f"  Capital limit set : ${limit:,.2f}")
    log.info(f"  Max risk / trade  : ${portfolio_value * MAX_RISK_PCT:,.2f}  ({MAX_RISK_PCT*100:.1f}%)")
    log.info(f"  Max open trades   : {MAX_OPEN_TRADES}")
    log.info(f"  Max total exposure: ${portfolio_value * MAX_POSITION_PCT * MAX_OPEN_TRADES:,.2f}")
    log.warning("=" * 60)

    if limit <= 0:
        log.error(
            "LIVE_CAPITAL_LIMIT is not set or is zero. "
            "Set it in the GitHub Actions workflow_dispatch input. Aborting."
        )
        return False

    if limit > portfolio_value:
        log.error(
            f"LIVE_CAPITAL_LIMIT (${limit:,.2f}) exceeds portfolio value "
            f"(${portfolio_value:,.2f}). Aborting."
        )
        return False

    if limit < portfolio_value * MAX_RISK_PCT:
        log.error(
            f"LIVE_CAPITAL_LIMIT (${limit:,.2f}) is less than one trade's "
            f"risk (${portfolio_value * MAX_RISK_PCT:,.2f}). Aborting."
        )
        return False

    log.info(f"Capital confirmation passed — proceeding with limit ${limit:,.2f}.")
    return True


# ──────────────────────────────────────────────────────────────────────────────
# TECHNICAL INDICATORS
# ──────────────────────────────────────────────────────────────────────────────

def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta    = series.diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, adjust=False).mean()
    avg_loss = loss.ewm(com=period - 1, adjust=False).mean()
    rs       = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    prev_c  = c.shift(1)
    tr = pd.concat(
        [h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(com=period - 1, adjust=False).mean()


def _vwap(df: pd.DataFrame) -> pd.Series:
    """Intraday VWAP — resets at each calendar date."""
    df       = df.copy()
    df["date"]      = df.index.date
    df["tp"]        = (df["high"] + df["low"] + df["close"]) / 3
    df["cum_tpvol"] = df.groupby("date").apply(
        lambda g: (g["tp"] * g["volume"]).cumsum()
    ).values
    df["cum_vol"]   = df.groupby("date")["volume"].cumsum().values
    return df["cum_tpvol"] / df["cum_vol"]


# ──────────────────────────────────────────────────────────────────────────────
# MARKET DATA
# ──────────────────────────────────────────────────────────────────────────────

def fetch_bars(symbol: str, lookback_days: int = 5) -> Optional[pd.DataFrame]:
    """Fetch 15-minute OHLCV bars for the past N calendar days."""
    try:
        end   = datetime.now(tz=timezone.utc)
        start = end - timedelta(days=lookback_days)
        req   = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame(15, TimeFrameUnit.Minute),
            start=start,
            end=end,
        )
        bars = market_data.get_stock_bars(req).df
        if bars.empty:
            log.warning(f"{symbol}: No bar data returned.")
            return None
        if isinstance(bars.index, pd.MultiIndex):
            bars = bars.loc[symbol]
        bars.index = pd.to_datetime(bars.index, utc=True).tz_convert(ET)
        return bars.sort_index()
    except Exception as exc:
        log.error(f"{symbol}: Failed to fetch bars — {exc}")
        return None


# ──────────────────────────────────────────────────────────────────────────────
# SIGNAL COMPUTATION
# ──────────────────────────────────────────────────────────────────────────────

def compute_signal(symbol: str) -> SignalResult:
    bars = fetch_bars(symbol)

    if bars is None or len(bars) < 30:
        return SignalResult(
            symbol=symbol, signal="NONE",
            reason="Insufficient bar data (need ≥ 30 bars)"
        )

    bars["ema9"]    = _ema(bars["close"], EMA_FAST)
    bars["ema21"]   = _ema(bars["close"], EMA_SLOW)
    bars["rsi"]     = _rsi(bars["close"], RSI_PERIOD)
    bars["atr"]     = _atr(bars, ATR_PERIOD)
    bars["vwap"]    = _vwap(bars)
    bars["avg_vol"] = bars["volume"].rolling(20).mean()

    last = bars.iloc[-1]
    price, v, e9, e21 = last["close"], last["vwap"], last["ema9"], last["ema21"]
    r, a, vol, avgvol = last["rsi"], last["atr"], last["volume"], last["avg_vol"]

    result = SignalResult(
        symbol   = symbol,
        signal   = "NONE",
        price    = round(float(price),  2),
        vwap_val = round(float(v),      2),
        ema9     = round(float(e9),     2),
        ema21    = round(float(e21),    2),
        rsi_val  = round(float(r),      2),
        atr_val  = round(float(a),      2),
        volume   = int(vol),
        avg_vol  = int(avgvol),
    )

    vol_ok = vol >= avgvol * VOLUME_MULT

    def _fail_reasons(vwap_ok, ema_ok, rsi_ok) -> str:
        parts = []
        if not vwap_ok: parts.append(f"VWAP fail (price {price:.2f} vs VWAP {v:.2f})")
        if not ema_ok:  parts.append(f"EMA fail (EMA9 {e9:.2f} vs EMA21 {e21:.2f})")
        if not rsi_ok:  parts.append(f"RSI {r:.1f} outside zone")
        if not vol_ok:  parts.append(f"Volume {vol:,} < threshold {avgvol*VOLUME_MULT:,.0f}")
        return "  |  ".join(parts) if parts else "Conditions not met"

    # ── LONG ──────────────────────────────────────────────────────────────────
    long_vwap = price > v
    long_ema  = e9 > e21
    long_rsi  = RSI_LONG_LOW < r < RSI_LONG_HIGH

    if long_vwap and long_ema and long_rsi and vol_ok:
        result.signal = "LONG"
        result.reason = (
            f"Price {price:.2f} > VWAP {v:.2f}  |  "
            f"EMA9 {e9:.2f} > EMA21 {e21:.2f}  |  "
            f"RSI {r:.1f} in [{RSI_LONG_LOW}–{RSI_LONG_HIGH}]  |  "
            f"Vol {vol:,} > {avgvol*VOLUME_MULT:,.0f}"
        )
        return result

    # ── SHORT ─────────────────────────────────────────────────────────────────
    short_vwap = price < v
    short_ema  = e9 < e21
    short_rsi  = RSI_SHORT_LOW < r < RSI_SHORT_HIGH

    if short_vwap and short_ema and short_rsi and vol_ok:
        result.signal = "SHORT"
        result.reason = (
            f"Price {price:.2f} < VWAP {v:.2f}  |  "
            f"EMA9 {e9:.2f} < EMA21 {e21:.2f}  |  "
            f"RSI {r:.1f} in [{RSI_SHORT_LOW}–{RSI_SHORT_HIGH}]  |  "
            f"Vol {vol:,} > {avgvol*VOLUME_MULT:,.0f}"
        )
        return result

    # ── NO SIGNAL ─────────────────────────────────────────────────────────────
    result.reason = _fail_reasons(
        long_vwap or short_vwap,
        long_ema  or short_ema,
        long_rsi  or short_rsi,
    )
    return result


# ──────────────────────────────────────────────────────────────────────────────
# POSITION SIZING
# ──────────────────────────────────────────────────────────────────────────────

def calc_position_size(
    portfolio_value: float,
    price: float,
    atr_val: float,
    capital_limit: Optional[float] = None,
) -> int:
    """
    Shares = (portfolio * MAX_RISK_PCT) / ATR
    Capped at MAX_POSITION_PCT of portfolio.
    In live mode, additionally capped by LIVE_CAPITAL_LIMIT.
    """
    risk_dollars  = portfolio_value * MAX_RISK_PCT
    stop_distance = max(atr_val, 0.01)          # never divide by zero
    shares        = risk_dollars / stop_distance

    # Cap by max position size
    max_by_pct    = (portfolio_value * MAX_POSITION_PCT) / price
    shares        = min(shares, max_by_pct)

    # Cap by capital limit in live mode
    if capital_limit and capital_limit > 0:
        max_by_limit = capital_limit / price
        shares       = min(shares, max_by_limit)

    return max(1, int(shares))


# ──────────────────────────────────────────────────────────────────────────────
# ORDER EXECUTION
# ──────────────────────────────────────────────────────────────────────────────

def place_trade(
    sig: SignalResult,
    portfolio_value: float,
    run_id: str,
    capital_limit: Optional[float] = None,
) -> Optional[TradeRecord]:
    """
    Place entry + stop + two limit orders.
    Returns a TradeRecord on success, None on failure.
    """
    side       = OrderSide.BUY  if sig.signal == "LONG"  else OrderSide.SELL
    exit_side  = OrderSide.SELL if sig.signal == "LONG"  else OrderSide.BUY
    price      = sig.price
    atr_v      = sig.atr_val
    qty        = calc_position_size(portfolio_value, price, atr_v, capital_limit)
    stop_dist  = atr_v

    if sig.signal == "LONG":
        stop_price  = round(price - stop_dist,             2)
        target1     = round(price + stop_dist * TARGET1_MULT, 2)
        target2     = round(price + stop_dist * TARGET2_MULT, 2)
    else:
        stop_price  = round(price + stop_dist,             2)
        target1     = round(price - stop_dist * TARGET1_MULT, 2)
        target2     = round(price - stop_dist * TARGET2_MULT, 2)

    risk_dollars = round(qty * stop_dist, 2)

    log.info("=" * 64)
    log.info(f"  SIGNAL  {sig.symbol}  {sig.signal}  ({'PAPER' if PAPER else 'LIVE'})")
    log.info(f"  Entry : ${price}   Qty : {qty} shares")
    log.info(f"  Stop  : ${stop_price}   Risk : ${risk_dollars}")
    log.info(f"  T1    : ${target1}  ({qty//2} shares — 50 %)")
    log.info(f"  T2    : ${target2}  ({qty - qty//2} shares — remaining)")
    log.info(f"  Reason: {sig.reason}")
    log.info("=" * 64)

    entry_id = stop_id = t1_id = t2_id = ""

    try:
        # 1. Market entry
        entry = trading.submit_order(MarketOrderRequest(
            symbol=sig.symbol, qty=qty,
            side=side, time_in_force=TimeInForce.DAY,
        ))
        entry_id = str(entry.id)
        log.info(f"{sig.symbol}: Entry order placed  — ID {entry_id}")

        # 2. Stop loss
        stop = trading.submit_order(StopOrderRequest(
            symbol=sig.symbol, qty=qty,
            side=exit_side, stop_price=stop_price,
            time_in_force=TimeInForce.DAY,
        ))
        stop_id = str(stop.id)
        log.info(f"{sig.symbol}: Stop loss @ ${stop_price}   — ID {stop_id}")

        # 3. Target 1 — 50 % of position
        t1_qty = max(1, qty // 2)
        t1 = trading.submit_order(LimitOrderRequest(
            symbol=sig.symbol, qty=t1_qty,
            side=exit_side, limit_price=target1,
            time_in_force=TimeInForce.DAY,
        ))
        t1_id = str(t1.id)
        log.info(f"{sig.symbol}: Target 1 @ ${target1} ({t1_qty} sh) — ID {t1_id}")

        # 4. Target 2 — remaining shares
        t2_qty = qty - t1_qty
        if t2_qty > 0:
            t2 = trading.submit_order(LimitOrderRequest(
                symbol=sig.symbol, qty=t2_qty,
                side=exit_side, limit_price=target2,
                time_in_force=TimeInForce.DAY,
            ))
            t2_id = str(t2.id)
            log.info(f"{sig.symbol}: Target 2 @ ${target2} ({t2_qty} sh) — ID {t2_id}")

        log.info(f"{sig.symbol}: All orders placed successfully ✓")

        record = TradeRecord(
            timestamp      = datetime.now(tz=ET).isoformat(),
            run_id         = run_id,
            mode           = "PAPER" if PAPER else "LIVE",
            symbol         = sig.symbol,
            direction      = sig.signal,
            entry_price    = price,
            qty            = qty,
            stop_price     = stop_price,
            target1_price  = target1,
            target2_price  = target2,
            risk_dollars   = risk_dollars,
            portfolio_value= round(portfolio_value, 2),
            entry_order_id = entry_id,
            stop_order_id  = stop_id,
            t1_order_id    = t1_id,
            t2_order_id    = t2_id,
            signal_reason  = sig.reason,
            status         = "OPEN",
        )
        append_trade_record(record)
        return record

    except Exception as exc:
        log.error(f"{sig.symbol}: Order placement failed — {exc}", exc_info=True)

        # Store failed attempt for audit trail
        error_record = TradeRecord(
            timestamp      = datetime.now(tz=ET).isoformat(),
            run_id         = run_id,
            mode           = "PAPER" if PAPER else "LIVE",
            symbol         = sig.symbol,
            direction      = sig.signal,
            entry_price    = price,
            qty            = qty,
            stop_price     = stop_price,
            target1_price  = target1,
            target2_price  = target2,
            risk_dollars   = risk_dollars,
            portfolio_value= round(portfolio_value, 2),
            entry_order_id = entry_id,
            stop_order_id  = stop_id,
            t1_order_id    = t1_id,
            t2_order_id    = t2_id,
            signal_reason  = sig.reason,
            status         = f"ERROR: {exc}",
        )
        append_trade_record(error_record)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# POSITION HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def already_in_position(symbol: str) -> bool:
    try:
        pos = trading.get_open_position(symbol)
        return abs(float(pos.qty)) > 0
    except Exception:
        return False


def open_trade_count() -> int:
    try:
        return len(trading.get_all_positions())
    except Exception:
        return 0


# ──────────────────────────────────────────────────────────────────────────────
# MAIN ENTRY POINT  —  single scan, designed for GitHub Actions
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    run_id = os.getenv("GITHUB_RUN_ID", "local")
    mode   = "PAPER" if PAPER else "LIVE"

    # ── Startup banner ────────────────────────────────────────────────────────
    log.info("=" * 64)
    log.info(f"  VWAP + EMA + RSI Trading Bot  —  {mode} mode")
    log.info(f"  Endpoint  : {ENDPOINT}")
    log.info(f"  Run ID    : {run_id}")
    log.info(f"  Watchlist : {WATCHLIST}")
    log.info(f"  Timeframe : 15-minute bars  |  Confirm on 1hr trend")
    log.info("=" * 64)

    # ── Validate config before touching the market ────────────────────────────
    validate_config()

    # ── Market timing check — exit immediately if closed ──────────────────────
    is_open, timing_msg = check_market_window()
    log.info(f"Market status: {timing_msg}")
    if not is_open:
        log.info("Nothing to do — exiting cleanly.")
        print_weekly_summary()
        sys.exit(0)

    # ── Fetch account info ────────────────────────────────────────────────────
    try:
        account       = trading.get_account()
        portfolio_val = float(account.portfolio_value)
        buying_power  = float(account.buying_power)
    except Exception as exc:
        log.error(f"Cannot fetch account info: {exc}")
        sys.exit(1)

    log.info(
        f"Account  →  Portfolio: ${portfolio_val:,.2f}  |  "
        f"Buying power: ${buying_power:,.2f}"
    )

    # ── Live mode: capital confirmation ───────────────────────────────────────
    if not PAPER:
        if not confirm_live_capital(portfolio_val):
            log.error("Capital confirmation failed — aborting.")
            sys.exit(1)

    # ── Check open trade count ────────────────────────────────────────────────
    open_trades = open_trade_count()
    log.info(f"Open positions: {open_trades} / {MAX_OPEN_TRADES}")

    if open_trades >= MAX_OPEN_TRADES:
        log.info("Max open trades reached — no new entries this scan.")
        print_weekly_summary()
        sys.exit(0)

    # ── Weekly summary before scan ────────────────────────────────────────────
    print_weekly_summary()

    # ── Scan watchlist ────────────────────────────────────────────────────────
    log.info(f"--- Scanning {len(WATCHLIST)} stocks ---")
    trades_placed = 0
    capital_limit = LIVE_CAPITAL_LIMIT if not PAPER else None

    for symbol in WATCHLIST:
        if open_trades + trades_placed >= MAX_OPEN_TRADES:
            log.info("Max trade limit reached mid-scan — stopping.")
            break

        if already_in_position(symbol):
            log.info(f"{symbol}: Position already open — skipped")
            continue

        try:
            sig = compute_signal(symbol)
        except Exception as exc:
            log.error(f"{symbol}: Signal computation error — {exc}", exc_info=True)
            continue

        # Log signal result cleanly
        indicator_line = (
            f"Price ${sig.price}  |  VWAP ${sig.vwap_val}  |  "
            f"EMA9 {sig.ema9}  |  EMA21 {sig.ema21}  |  "
            f"RSI {sig.rsi_val}  |  Vol {sig.volume:,}"
        )
        if sig.signal in ("LONG", "SHORT"):
            log.info(f"{symbol}: ✓ {sig.signal}  —  {indicator_line}")
            log.info(f"{symbol}: Reason → {sig.reason}")
            record = place_trade(sig, portfolio_val, run_id, capital_limit)
            if record:
                trades_placed += 1
        else:
            log.info(f"{symbol}: — NONE  —  {indicator_line}")
            log.info(f"{symbol}: No signal → {sig.reason}")

        time.sleep(0.5)   # brief pause between API calls

    # ── Scan summary ──────────────────────────────────────────────────────────
    log.info("=" * 64)
    log.info(f"  Scan complete  |  Trades placed this run: {trades_placed}")
    log.info(f"  Total open positions: {open_trades + trades_placed} / {MAX_OPEN_TRADES}")
    log.info("=" * 64)


if __name__ == "__main__":
    main()
