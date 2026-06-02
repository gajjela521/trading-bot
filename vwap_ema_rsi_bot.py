"""
VWAP + EMA + RSI Strategy Bot — Alpaca Paper/Live Trading
==========================================================
Strategy logic:
  LONG  when: price > VWAP  AND  EMA9 > EMA21  AND  45 < RSI < 62
  SHORT when: price < VWAP  AND  EMA9 < EMA21  AND  38 < RSI < 55

Risk management:
  - Stop loss  : 1 ATR below/above entry
  - Target 1   : 2× risk  (close 50% position)
  - Target 2   : 3× risk  (close remaining 50%)
  - Max risk   : 1.5% of portfolio per trade
  - Max 3 open trades at any time
  - No trades in first 15 min after market open
  - No trades 30 min before market close

Setup:
  pip install alpaca-py pandas numpy
  Set your API keys below (or use env vars).
"""

import os
import time
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    LimitOrderRequest,
    StopOrderRequest,
    GetOrdersRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

# ─── CONFIG ────────────────────────────────────────────────────────────────────

API_KEY    = os.getenv("ALPACA_API_KEY",    "YOUR_API_KEY_HERE")
API_SECRET = os.getenv("ALPACA_API_SECRET", "YOUR_API_SECRET_HERE")
PAPER      = os.getenv("PAPER_MODE", "true").lower() == "true"          # ← Set False for live trading

# Stocks to monitor — add/remove as you like
WATCHLIST  = ["NBIS", "NVDA", "TSLA", "AAPL", "MSFT"]

# Strategy parameters
EMA_FAST        = 9
EMA_SLOW        = 21
RSI_PERIOD      = 14
ATR_PERIOD      = 14
RSI_LONG_LOW    = 45      # RSI must be above this to go long
RSI_LONG_HIGH   = 62      # RSI must be below this to go long
RSI_SHORT_LOW   = 38      # RSI must be above this to go short
RSI_SHORT_HIGH  = 55      # RSI must be below this to go short
VOLUME_MULT     = 1.2     # Current bar volume must be > 1.2× avg volume

# Risk management
MAX_RISK_PCT    = 0.015   # 1.5% of portfolio per trade
TARGET1_MULT    = 2.0     # Target 1 = 2× stop distance
TARGET2_MULT    = 3.0     # Target 2 = 3× stop distance
MAX_OPEN_TRADES = 3

# Timing (US Eastern)
MARKET_OPEN_BUFFER_MIN  = 15   # Skip first N minutes
MARKET_CLOSE_BUFFER_MIN = 30   # Stop N minutes before close
SCAN_INTERVAL_SEC       = 60   # How often to scan (seconds)

ET = ZoneInfo("America/New_York")

# ─── LOGGING ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("strategy_log.txt"),
    ],
)
log = logging.getLogger(__name__)

# ─── CLIENTS ───────────────────────────────────────────────────────────────────

trading = TradingClient(API_KEY, API_SECRET, paper=PAPER)
market_data = StockHistoricalDataClient(API_KEY, API_SECRET)

# ─── INDICATORS ────────────────────────────────────────────────────────────────

def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, adjust=False).mean()
    avg_loss = loss.ewm(com=period - 1, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    prev_c  = c.shift(1)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    return tr.ewm(com=period - 1, adjust=False).mean()

def vwap(df: pd.DataFrame) -> pd.Series:
    """Intraday VWAP — resets each day."""
    df = df.copy()
    df["date"]     = df.index.date
    df["tp"]       = (df["high"] + df["low"] + df["close"]) / 3
    df["cum_tpvol"] = df.groupby("date").apply(
        lambda g: (g["tp"] * g["volume"]).cumsum()
    ).values
    df["cum_vol"]   = df.groupby("date")["volume"].cumsum().values
    return df["cum_tpvol"] / df["cum_vol"]

# ─── DATA ──────────────────────────────────────────────────────────────────────

def get_bars(symbol: str, lookback_days: int = 5) -> pd.DataFrame:
    """Fetch 15-minute bars for the past N days."""
    end   = datetime.now(tz=timezone.utc)
    start = end - timedelta(days=lookback_days)
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame(15, TimeFrameUnit.Minute),
        start=start,
        end=end,
    )
    bars = market_data.get_stock_bars(req).df
    if bars.empty:
        return pd.DataFrame()
    if isinstance(bars.index, pd.MultiIndex):
        bars = bars.loc[symbol]
    bars.index = pd.to_datetime(bars.index, utc=True).tz_convert(ET)
    bars = bars.sort_index()
    return bars

# ─── SIGNAL ────────────────────────────────────────────────────────────────────

def compute_signal(symbol: str) -> dict:
    """
    Returns a signal dict:
      signal   : 'LONG' | 'SHORT' | 'NONE'
      price    : latest close
      vwap_val : VWAP
      ema9     : EMA 9
      ema21    : EMA 21
      rsi_val  : RSI
      atr_val  : ATR
      reason   : human-readable explanation
    """
    bars = get_bars(symbol)
    if bars is None or len(bars) < 30:
        return {"signal": "NONE", "reason": "Insufficient data"}

    bars["ema9"]    = ema(bars["close"], EMA_FAST)
    bars["ema21"]   = ema(bars["close"], EMA_SLOW)
    bars["rsi"]     = rsi(bars["close"], RSI_PERIOD)
    bars["atr"]     = atr(bars, ATR_PERIOD)
    bars["vwap"]    = vwap(bars)
    bars["avg_vol"] = bars["volume"].rolling(20).mean()

    last   = bars.iloc[-1]
    price  = last["close"]
    v      = last["vwap"]
    e9     = last["ema9"]
    e21    = last["ema21"]
    r      = last["rsi"]
    a      = last["atr"]
    vol    = last["volume"]
    avgvol = last["avg_vol"]

    result = {
        "symbol"   : symbol,
        "price"    : round(price, 2),
        "vwap_val" : round(v, 2),
        "ema9"     : round(e9, 2),
        "ema21"    : round(e21, 2),
        "rsi_val"  : round(r, 2),
        "atr_val"  : round(a, 2),
        "volume"   : int(vol),
        "avg_vol"  : int(avgvol),
    }

    vol_ok = vol >= avgvol * VOLUME_MULT

    # ── LONG conditions ──────────────────────────────────────────────────────
    long_vwap   = price > v
    long_ema    = e9 > e21
    long_rsi    = RSI_LONG_LOW < r < RSI_LONG_HIGH
    long_vol    = vol_ok

    if long_vwap and long_ema and long_rsi and long_vol:
        result["signal"] = "LONG"
        result["reason"] = (
            f"Price ${price} > VWAP ${v:.2f} | "
            f"EMA9 {e9:.2f} > EMA21 {e21:.2f} | "
            f"RSI {r:.1f} in [{RSI_LONG_LOW}–{RSI_LONG_HIGH}] | "
            f"Volume {vol:,} > avg {avgvol:,.0f}"
        )
        return result

    # ── SHORT conditions ─────────────────────────────────────────────────────
    short_vwap  = price < v
    short_ema   = e9 < e21
    short_rsi   = RSI_SHORT_LOW < r < RSI_SHORT_HIGH
    short_vol   = vol_ok

    if short_vwap and short_ema and short_rsi and short_vol:
        result["signal"] = "SHORT"
        result["reason"] = (
            f"Price ${price} < VWAP ${v:.2f} | "
            f"EMA9 {e9:.2f} < EMA21 {e21:.2f} | "
            f"RSI {r:.1f} in [{RSI_SHORT_LOW}–{RSI_SHORT_HIGH}] | "
            f"Volume {vol:,} > avg {avgvol:,.0f}"
        )
        return result

    # Build readable reason why no signal
    reasons = []
    if not (long_vwap or short_vwap):   reasons.append("Price at VWAP — no clear side")
    if not (long_ema or short_ema):     reasons.append("EMA9 ≈ EMA21 — no trend")
    if not (long_rsi or short_rsi):     reasons.append(f"RSI {r:.1f} outside entry zones")
    if not vol_ok:                      reasons.append(f"Volume too low ({vol:,} < {avgvol*VOLUME_MULT:,.0f})")

    result["signal"] = "NONE"
    result["reason"] = " | ".join(reasons) if reasons else "Conditions not met"
    return result

# ─── POSITION SIZING ───────────────────────────────────────────────────────────

def position_size(portfolio_value: float, price: float, atr_val: float) -> int:
    """Risk 1.5% of portfolio. Stop = 1 ATR."""
    risk_dollars  = portfolio_value * MAX_RISK_PCT
    stop_distance = atr_val                     # 1 ATR stop
    shares        = risk_dollars / stop_distance
    # Don't spend more than 20% of portfolio on one position
    max_shares    = (portfolio_value * 0.20) / price
    shares        = min(shares, max_shares)
    return max(1, int(shares))

# ─── ORDER EXECUTION ───────────────────────────────────────────────────────────

def place_trade(signal: dict, portfolio_value: float):
    symbol = signal["symbol"]
    side   = OrderSide.BUY if signal["signal"] == "LONG" else OrderSide.SELL
    price  = signal["price"]
    atr_v  = signal["atr_val"]
    qty    = position_size(portfolio_value, price, atr_v)

    if qty < 1:
        log.warning(f"{symbol}: Position size too small, skipping.")
        return

    stop_dist = atr_v
    if signal["signal"] == "LONG":
        stop_price   = round(price - stop_dist, 2)
        target1      = round(price + stop_dist * TARGET1_MULT, 2)
        target2      = round(price + stop_dist * TARGET2_MULT, 2)
    else:
        stop_price   = round(price + stop_dist, 2)
        target1      = round(price - stop_dist * TARGET1_MULT, 2)
        target2      = round(price - stop_dist * TARGET2_MULT, 2)

    log.info(
        f"{'='*60}\n"
        f"  TRADE SIGNAL  {symbol}  {signal['signal']}\n"
        f"  Entry: ${price}  |  Qty: {qty} shares\n"
        f"  Stop:  ${stop_price}  |  T1: ${target1}  |  T2: ${target2}\n"
        f"  Reason: {signal['reason']}\n"
        f"{'='*60}"
    )

    # ── Market entry ─────────────────────────────────────────────────────────
    entry_req = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=side,
        time_in_force=TimeInForce.DAY,
    )
    entry_order = trading.submit_order(entry_req)
    log.info(f"{symbol}: Entry order submitted — ID {entry_order.id}")

    # ── Stop loss ────────────────────────────────────────────────────────────
    stop_side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY
    stop_req  = StopOrderRequest(
        symbol=symbol,
        qty=qty,
        side=stop_side,
        stop_price=stop_price,
        time_in_force=TimeInForce.DAY,
    )
    stop_order = trading.submit_order(stop_req)
    log.info(f"{symbol}: Stop loss @ ${stop_price} — ID {stop_order.id}")

    # ── Target 1 — 50% of position ───────────────────────────────────────────
    t1_qty = max(1, qty // 2)
    t1_req = LimitOrderRequest(
        symbol=symbol,
        qty=t1_qty,
        side=stop_side,
        limit_price=target1,
        time_in_force=TimeInForce.DAY,
    )
    t1_order = trading.submit_order(t1_req)
    log.info(f"{symbol}: Target 1 @ ${target1} ({t1_qty} shares) — ID {t1_order.id}")

    # ── Target 2 — remaining shares ──────────────────────────────────────────
    t2_qty = qty - t1_qty
    if t2_qty > 0:
        t2_req = LimitOrderRequest(
            symbol=symbol,
            qty=t2_qty,
            side=stop_side,
            limit_price=target2,
            time_in_force=TimeInForce.DAY,
        )
        t2_order = trading.submit_order(t2_req)
        log.info(f"{symbol}: Target 2 @ ${target2} ({t2_qty} shares) — ID {t2_order.id}")

    log.info(f"{symbol}: Full trade setup complete ✓")

# ─── TIMING GUARDS ─────────────────────────────────────────────────────────────

def is_trading_window() -> tuple[bool, str]:
    """Returns (ok_to_trade, reason)."""
    clock = trading.get_clock()
    if not clock.is_open:
        return False, "Market closed"

    now        = datetime.now(ET)
    open_et    = clock.next_open.astimezone(ET)   if not clock.is_open else now.replace(hour=9, minute=30, second=0)
    close_et   = clock.next_close.astimezone(ET)

    # Re-derive today's open time
    today_open  = now.replace(hour=9, minute=30, second=0, microsecond=0)
    today_close = now.replace(hour=16, minute=0, second=0, microsecond=0)

    if now < today_open + timedelta(minutes=MARKET_OPEN_BUFFER_MIN):
        wait = int((today_open + timedelta(minutes=MARKET_OPEN_BUFFER_MIN) - now).total_seconds() / 60)
        return False, f"Opening buffer — {wait} min remaining"

    if now > today_close - timedelta(minutes=MARKET_CLOSE_BUFFER_MIN):
        return False, "Too close to market close"

    return True, "Market open ✓"

def already_in_position(symbol: str) -> bool:
    try:
        pos = trading.get_open_position(symbol)
        return int(pos.qty) != 0
    except Exception:
        return False

def open_trade_count() -> int:
    try:
        positions = trading.get_all_positions()
        return len(positions)
    except Exception:
        return 0

# ─── MAIN LOOP ─────────────────────────────────────────────────────────────────

def run():
    mode = "PAPER" if PAPER else "LIVE"
    log.info(f"Strategy bot starting — {mode} mode")
    log.info(f"Watchlist: {WATCHLIST}")
    log.info(f"Scan interval: {SCAN_INTERVAL_SEC}s | Max trades: {MAX_OPEN_TRADES}")

    while True:
        try:
            ok, timing_reason = is_trading_window()
            if not ok:
                log.info(f"Waiting — {timing_reason}")
                time.sleep(SCAN_INTERVAL_SEC)
                continue

            account        = trading.get_account()
            portfolio_val  = float(account.portfolio_value)
            open_trades    = open_trade_count()

            log.info(
                f"Scan — Portfolio: ${portfolio_val:,.2f} | "
                f"Open trades: {open_trades}/{MAX_OPEN_TRADES}"
            )

            if open_trades >= MAX_OPEN_TRADES:
                log.info("Max open trades reached — skipping scan")
                time.sleep(SCAN_INTERVAL_SEC)
                continue

            for symbol in WATCHLIST:
                if already_in_position(symbol):
                    log.info(f"{symbol}: Already in position — skipping")
                    continue

                signal = compute_signal(symbol)
                log.info(
                    f"{symbol}: {signal['signal']:5s} | "
                    f"Price ${signal.get('price','?')} | "
                    f"RSI {signal.get('rsi_val','?')} | "
                    f"{signal['reason']}"
                )

                if signal["signal"] in ("LONG", "SHORT"):
                    place_trade(signal, portfolio_val)
                    open_trades += 1
                    if open_trades >= MAX_OPEN_TRADES:
                        log.info("Max trades hit mid-scan — stopping scan")
                        break

                time.sleep(1)   # small pause between symbols

        except KeyboardInterrupt:
            log.info("Bot stopped by user.")
            break
        except Exception as e:
            log.error(f"Error in main loop: {e}", exc_info=True)

        time.sleep(SCAN_INTERVAL_SEC)

# ─── ENTRY POINT ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run()


def run_once():
    """
    Single-scan mode for GitHub Actions.
    Each cron trigger = one fresh scan of the watchlist.
    The loop/timing is handled by the cron schedule itself.
    """
    ok, timing_reason = is_trading_window()
    if not ok:
        log.info(f"Outside trading window — {timing_reason}. Exiting.")
        return

    account       = trading.get_account()
    portfolio_val = float(account.portfolio_value)
    open_trades   = open_trade_count()

    log.info(
        f"=== Scan start === Portfolio: \${portfolio_val:,.2f} | "
        f"Open trades: {open_trades}/{MAX_OPEN_TRADES}"
    )

    if open_trades >= MAX_OPEN_TRADES:
        log.info("Max open trades reached. Exiting.")
        return

    for symbol in WATCHLIST:
        if already_in_position(symbol):
            log.info(f"{symbol}: Already in position — skipping")
            continue

        signal = compute_signal(symbol)
        log.info(
            f"{symbol}: {signal['signal']:5s} | "
            f"Price \${signal.get('price','?')} | "
            f"RSI {signal.get('rsi_val','?')} | "
            f"{signal['reason']}"
        )

        if signal["signal"] in ("LONG", "SHORT"):
            place_trade(signal, portfolio_val)
            open_trades += 1
            if open_trades >= MAX_OPEN_TRADES:
                break

        time.sleep(1)

    log.info("=== Scan complete ===")


if __name__ == "__main__":
    # GitHub Actions: single scan per cron trigger
    # Local dev: uncomment run() below for continuous loop
    run_once()
    # run()   # ← uncomment for local continuous loop
