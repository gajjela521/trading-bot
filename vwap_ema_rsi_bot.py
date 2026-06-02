"""
VWAP + EMA + RSI Strategy Bot  —  Enterprise Edition
=====================================================
GitHub Actions single-scan design:
  - One scan per cron trigger. Bot never loops or waits.
  - Market closed  → logs reason, exits immediately (exit 0)
  - Weekend        → exits immediately (exit 0)
  - EOD            → cron stops at 3:30 PM ET; bot itself also guards close buffer
  - PAPER mode     → asks for paper capital amount via env, then runs
  - LIVE mode      → requires CONFIRM=confirm + capital amount, then runs

Run modes
---------
  PAPER_MODE=true   → paper-api.alpaca.markets  (safe, default)
  PAPER_MODE=false  → api.alpaca.markets         (real money)

Signal logic
------------
  LONG  : price > VWAP  AND  EMA9 > EMA21  AND  45 < RSI < 62  AND  vol > 1.2x avg
  SHORT : price < VWAP  AND  EMA9 < EMA21  AND  38 < RSI < 55  AND  vol > 1.2x avg

Risk management
---------------
  Stop loss  : 1 ATR from entry
  Target 1   : 2x risk  →  close 50% of position
  Target 2   : 3x risk  →  close remaining 50%
  Max risk   : 1.5% of portfolio per trade (capped by capital limit)
  Max trades : 3 simultaneous
  No trades  : first 15 min after open / last 30 min before close

Trade records
-------------
  Every trade appended to trade_records.json.
  Records older than 7 days auto-purged. Atomic write (temp→rename).
"""

# ─────────────────────────────────────────────────────────────────────────────
# IMPORTS
# ─────────────────────────────────────────────────────────────────────────────
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS & CONFIG
# ─────────────────────────────────────────────────────────────────────────────

ET = ZoneInfo("America/New_York")

# Mode — set by GitHub Actions env var; defaults to paper
PAPER: bool = os.getenv("PAPER_MODE", "true").lower() == "true"

# Keys loaded separately for paper vs live
PAPER_API_KEY    = os.getenv("ALPACA_PAPER_API_KEY",    "")
PAPER_API_SECRET = os.getenv("ALPACA_PAPER_API_SECRET", "")
LIVE_API_KEY     = os.getenv("ALPACA_LIVE_API_KEY",     "")
LIVE_API_SECRET  = os.getenv("ALPACA_LIVE_API_SECRET",  "")

# Confirmation + capital — set by workflow inputs
LIVE_CONFIRM      = os.getenv("LIVE_CONFIRM",       "")        # must equal "confirm"
CAPITAL_LIMIT     = float(os.getenv("CAPITAL_LIMIT", "0"))     # USD, both modes

# Watchlist
WATCHLIST: list[str] = ["NBIS", "NVDA", "TSLA", "AAPL", "MSFT"]

# Strategy
EMA_FAST          = 9
EMA_SLOW          = 21
RSI_PERIOD        = 14
ATR_PERIOD        = 14
RSI_LONG_LOW      = 45
RSI_LONG_HIGH     = 62
RSI_SHORT_LOW     = 38
RSI_SHORT_HIGH    = 55
VOLUME_MULT       = 1.2

# Risk
MAX_RISK_PCT      = 0.015
TARGET1_MULT      = 2.0
TARGET2_MULT      = 3.0
MAX_OPEN_TRADES   = 3
MAX_POSITION_PCT  = 0.20

# Timing
OPEN_BUFFER_MIN   = 15
CLOSE_BUFFER_MIN  = 30

# Records
RECORD_FILE       = Path("trade_records.json")
RETENTION_DAYS    = 7

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING  — set up before anything else
# ─────────────────────────────────────────────────────────────────────────────

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

# ─────────────────────────────────────────────────────────────────────────────
# DATA MODELS
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Signal:
    symbol:   str
    signal:   str    # LONG | SHORT | NONE
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
    timestamp:       str
    run_id:          str
    mode:            str
    symbol:          str
    direction:       str
    entry_price:     float
    qty:             int
    stop_price:      float
    target1_price:   float
    target2_price:   float
    risk_dollars:    float
    capital_limit:   float
    portfolio_value: float
    entry_order_id:  str
    stop_order_id:   str
    t1_order_id:     str
    t2_order_id:     str
    signal_reason:   str
    status:          str = "OPEN"

# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — WEEKEND / TIMING GUARD  (before any API call)
# ─────────────────────────────────────────────────────────────────────────────

def check_day_and_time() -> tuple[bool, str]:
    """
    Returns (ok, reason).
    Rejects weekends and times outside the trading window.
    Called before any Alpaca API call — no network needed.
    """
    now = datetime.now(ET)
    weekday = now.weekday()   # 0=Mon … 6=Sun

    if weekday >= 5:
        day_name = "Saturday" if weekday == 5 else "Sunday"
        return False, f"Weekend ({day_name}) — markets closed, no run needed."

    today_open  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    today_close = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    open_guard  = today_open  + timedelta(minutes=OPEN_BUFFER_MIN)
    close_guard = today_close - timedelta(minutes=CLOSE_BUFFER_MIN)

    if now < open_guard:
        mins = int((open_guard - now).total_seconds() / 60)
        return False, (
            f"Opening buffer — trading window opens at "
            f"{open_guard.strftime('%I:%M %p ET')} ({mins} min away)."
        )

    if now >= close_guard:
        return False, (
            f"Closing buffer — no new trades after "
            f"{close_guard.strftime('%I:%M %p ET')} to avoid end-of-day risk."
        )

    return True, f"Trading window open — {now.strftime('%A %I:%M %p ET')}"


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — KEY VALIDATION
# ─────────────────────────────────────────────────────────────────────────────

def validate_keys() -> tuple[str, str]:
    """
    Returns the correct (api_key, api_secret) pair for the active mode.
    Exits with a clear error if keys are missing.
    """
    if PAPER:
        if not PAPER_API_KEY or not PAPER_API_SECRET:
            log.error(
                "PAPER mode requires GitHub Secrets:\n"
                "  ALPACA_PAPER_API_KEY\n"
                "  ALPACA_PAPER_API_SECRET\n"
                "Go to: repo → Settings → Secrets and variables → Actions"
            )
            sys.exit(1)
        return PAPER_API_KEY, PAPER_API_SECRET
    else:
        if not LIVE_API_KEY or not LIVE_API_SECRET:
            log.error(
                "LIVE mode requires GitHub Secrets:\n"
                "  ALPACA_LIVE_API_KEY\n"
                "  ALPACA_LIVE_API_SECRET\n"
                "Go to: repo → Settings → Secrets and variables → Actions"
            )
            sys.exit(1)
        return LIVE_API_KEY, LIVE_API_SECRET


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — MODE-SPECIFIC CONFIRMATION
# ─────────────────────────────────────────────────────────────────────────────

def confirm_paper_run() -> float:
    """
    Paper mode: validate capital limit is provided.
    Returns the capital limit to use.
    """
    if CAPITAL_LIMIT <= 0:
        log.error(
            "Paper run requires CAPITAL_LIMIT > 0.\n"
            "Set it in the workflow_dispatch input 'capital_limit'."
        )
        sys.exit(1)

    log.info("─" * 64)
    log.info("  PAPER TRADING MODE — simulated orders, no real money")
    log.info(f"  Capital limit : ${CAPITAL_LIMIT:,.2f}")
    log.info(f"  Endpoint      : https://paper-api.alpaca.markets")
    log.info("─" * 64)
    return CAPITAL_LIMIT


def confirm_live_run(portfolio_value: float) -> float:
    """
    Live mode: requires LIVE_CONFIRM='confirm' AND CAPITAL_LIMIT > 0.
    Aborts with a clear message if either is missing or invalid.
    Returns the capital limit to use.
    """
    log.warning("=" * 64)
    log.warning("  LIVE TRADING MODE — REAL MONEY WILL BE USED")
    log.warning("=" * 64)

    # 1. Must type "confirm"
    if LIVE_CONFIRM.strip().lower() != "confirm":
        log.error(
            'LIVE mode aborted.\n'
            'You must set the workflow input "live_confirm" to exactly: confirm\n'
            'This is a safety check to prevent accidental live trades.'
        )
        sys.exit(1)

    # 2. Capital limit must be set
    if CAPITAL_LIMIT <= 0:
        log.error(
            "LIVE mode aborted.\n"
            "You must set 'capital_limit' > 0 in the workflow_dispatch input.\n"
            "This is the maximum USD the bot is allowed to use this run."
        )
        sys.exit(1)

    # 3. Capital limit must not exceed portfolio
    if CAPITAL_LIMIT > portfolio_value:
        log.error(
            f"LIVE mode aborted.\n"
            f"capital_limit (${CAPITAL_LIMIT:,.2f}) exceeds "
            f"portfolio value (${portfolio_value:,.2f})."
        )
        sys.exit(1)

    # 4. Must be enough for at least one trade
    min_needed = portfolio_value * MAX_RISK_PCT
    if CAPITAL_LIMIT < min_needed:
        log.error(
            f"LIVE mode aborted.\n"
            f"capital_limit (${CAPITAL_LIMIT:,.2f}) is less than "
            f"minimum trade risk (${min_needed:,.2f})."
        )
        sys.exit(1)

    log.info(f"  Portfolio value   : ${portfolio_value:,.2f}")
    log.info(f"  Capital limit     : ${CAPITAL_LIMIT:,.2f}")
    log.info(f"  Max risk / trade  : ${portfolio_value * MAX_RISK_PCT:,.2f}  ({MAX_RISK_PCT*100:.1f}%)")
    log.info(f"  Max open trades   : {MAX_OPEN_TRADES}")
    log.info(f"  Endpoint          : https://api.alpaca.markets")
    log.info("=" * 64)
    log.info("  Confirmation: RECEIVED — proceeding with live trading.")
    log.info("=" * 64)
    return CAPITAL_LIMIT


# ─────────────────────────────────────────────────────────────────────────────
# TRADE RECORDS
# ─────────────────────────────────────────────────────────────────────────────

def _load_records() -> list[dict]:
    if not RECORD_FILE.exists():
        return []
    try:
        return json.loads(RECORD_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _purge_old(records: list[dict]) -> list[dict]:
    cutoff = datetime.now(tz=ET) - timedelta(days=RETENTION_DAYS)
    kept, dropped = [], 0
    for r in records:
        try:
            ts = datetime.fromisoformat(r["timestamp"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=ET)
            if ts >= cutoff:
                kept.append(r)
            else:
                dropped += 1
        except Exception:
            kept.append(r)
    if dropped:
        log.info(f"Records: purged {dropped} entries older than {RETENTION_DAYS} days.")
    return kept


def _save_records(records: list[dict]) -> None:
    tmp = RECORD_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(records, indent=2), encoding="utf-8")
        tmp.replace(RECORD_FILE)
    except Exception as exc:
        log.error(f"Failed to save trade records: {exc}")
        tmp.unlink(missing_ok=True)


def save_trade(record: TradeRecord) -> None:
    records = _purge_old(_load_records())
    records.append(asdict(record))
    _save_records(records)
    log.info(
        f"Trade record saved — {record.symbol} {record.direction} "
        f"@ ${record.entry_price}  |  Week total: {len(records)} trades"
    )


def weekly_summary() -> None:
    records = _purge_old(_load_records())
    if not records:
        log.info("Trade records: none in the last 7 days.")
        return
    longs  = sum(1 for r in records if r.get("direction") == "LONG")
    shorts = sum(1 for r in records if r.get("direction") == "SHORT")
    paper  = sum(1 for r in records if r.get("mode") == "PAPER")
    live   = sum(1 for r in records if r.get("mode") == "LIVE")
    log.info(
        f"Trade records (7 days): {len(records)} total | "
        f"LONG={longs} SHORT={shorts} | PAPER={paper} LIVE={live}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# INDICATORS
# ─────────────────────────────────────────────────────────────────────────────

def _ema(s: pd.Series, p: int) -> pd.Series:
    return s.ewm(span=p, adjust=False).mean()


def _rsi(s: pd.Series, p: int = 14) -> pd.Series:
    d = s.diff()
    g = d.clip(lower=0)
    l = -d.clip(upper=0)
    ag = g.ewm(com=p - 1, adjust=False).mean()
    al = l.ewm(com=p - 1, adjust=False).mean()
    rs = ag / al.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _atr(df: pd.DataFrame, p: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat(
        [h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(com=p - 1, adjust=False).mean()


def _vwap(df: pd.DataFrame) -> pd.Series:
    df = df.copy()
    df["date"] = df.index.date
    df["tp"]   = (df["high"] + df["low"] + df["close"]) / 3
    df["ctpv"] = df.groupby("date").apply(
        lambda g: (g["tp"] * g["volume"]).cumsum()
    ).values
    df["cvol"] = df.groupby("date")["volume"].cumsum().values
    return df["ctpv"] / df["cvol"]


# ─────────────────────────────────────────────────────────────────────────────
# DATA FETCH
# ─────────────────────────────────────────────────────────────────────────────

def fetch_bars(client, symbol: str) -> Optional[pd.DataFrame]:
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    try:
        end   = datetime.now(tz=timezone.utc)
        start = end - timedelta(days=5)
        req   = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame(15, TimeFrameUnit.Minute),
            start=start,
            end=end,
        )
        bars = client.get_stock_bars(req).df
        if bars.empty:
            return None
        if isinstance(bars.index, pd.MultiIndex):
            bars = bars.loc[symbol]
        bars.index = pd.to_datetime(bars.index, utc=True).tz_convert(ET)
        return bars.sort_index()
    except Exception as exc:
        log.error(f"{symbol}: fetch_bars failed — {exc}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL
# ─────────────────────────────────────────────────────────────────────────────

def compute_signal(market_client, symbol: str) -> Signal:
    bars = fetch_bars(market_client, symbol)
    if bars is None or len(bars) < 30:
        return Signal(symbol=symbol, signal="NONE", reason="Insufficient data (need ≥ 30 bars)")

    bars["ema9"]    = _ema(bars["close"], EMA_FAST)
    bars["ema21"]   = _ema(bars["close"], EMA_SLOW)
    bars["rsi"]     = _rsi(bars["close"], RSI_PERIOD)
    bars["atr"]     = _atr(bars, ATR_PERIOD)
    bars["vwap"]    = _vwap(bars)
    bars["avg_vol"] = bars["volume"].rolling(20).mean()

    row = bars.iloc[-1]
    price = float(row["close"]); v  = float(row["vwap"])
    e9    = float(row["ema9"]);  e21 = float(row["ema21"])
    r     = float(row["rsi"]);   a   = float(row["atr"])
    vol   = int(row["volume"]);  avg = int(row["avg_vol"])

    sig = Signal(
        symbol=symbol, signal="NONE",
        price=round(price,2), vwap_val=round(v,2),
        ema9=round(e9,2), ema21=round(e21,2),
        rsi_val=round(r,2), atr_val=round(a,2),
        volume=vol, avg_vol=avg,
    )

    vol_ok = vol >= avg * VOLUME_MULT

    if price > v and e9 > e21 and RSI_LONG_LOW < r < RSI_LONG_HIGH and vol_ok:
        sig.signal = "LONG"
        sig.reason = (
            f"Price {price:.2f} > VWAP {v:.2f}  |  "
            f"EMA9 {e9:.2f} > EMA21 {e21:.2f}  |  "
            f"RSI {r:.1f} in [{RSI_LONG_LOW}–{RSI_LONG_HIGH}]  |  "
            f"Vol {vol:,} > {avg*VOLUME_MULT:,.0f}"
        )
        return sig

    if price < v and e9 < e21 and RSI_SHORT_LOW < r < RSI_SHORT_HIGH and vol_ok:
        sig.signal = "SHORT"
        sig.reason = (
            f"Price {price:.2f} < VWAP {v:.2f}  |  "
            f"EMA9 {e9:.2f} < EMA21 {e21:.2f}  |  "
            f"RSI {r:.1f} in [{RSI_SHORT_LOW}–{RSI_SHORT_HIGH}]  |  "
            f"Vol {vol:,} > {avg*VOLUME_MULT:,.0f}"
        )
        return sig

    # Build no-signal reason
    parts = []
    if not (price > v or price < v):  parts.append(f"Price at VWAP ({price:.2f})")
    elif price > v and not e9 > e21:  parts.append(f"EMA not bullish (EMA9 {e9:.2f} < EMA21 {e21:.2f})")
    elif price < v and not e9 < e21:  parts.append(f"EMA not bearish (EMA9 {e9:.2f} > EMA21 {e21:.2f})")
    if not (RSI_LONG_LOW < r < RSI_LONG_HIGH or RSI_SHORT_LOW < r < RSI_SHORT_HIGH):
        parts.append(f"RSI {r:.1f} outside all entry zones")
    if not vol_ok:
        parts.append(f"Volume {vol:,} below threshold {avg*VOLUME_MULT:,.0f}")
    sig.reason = "  |  ".join(parts) if parts else "Conditions not fully met"
    return sig


# ─────────────────────────────────────────────────────────────────────────────
# POSITION SIZING
# ─────────────────────────────────────────────────────────────────────────────

def calc_qty(portfolio: float, price: float, atr: float, limit: float) -> int:
    risk_dollars  = portfolio * MAX_RISK_PCT
    stop_dist     = max(atr, 0.01)
    shares        = risk_dollars / stop_dist
    shares        = min(shares, (portfolio * MAX_POSITION_PCT) / price)
    if limit > 0:
        shares    = min(shares, limit / price)
    return max(1, int(shares))


# ─────────────────────────────────────────────────────────────────────────────
# ORDER EXECUTION
# ─────────────────────────────────────────────────────────────────────────────

def place_trade(
    trade_client,
    sig: Signal,
    portfolio: float,
    capital_limit: float,
    run_id: str,
) -> Optional[TradeRecord]:
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import (
        LimitOrderRequest, MarketOrderRequest, StopOrderRequest,
    )

    side      = OrderSide.BUY  if sig.signal == "LONG"  else OrderSide.SELL
    exit_side = OrderSide.SELL if sig.signal == "LONG"  else OrderSide.BUY
    qty       = calc_qty(portfolio, sig.price, sig.atr_val, capital_limit)
    dist      = sig.atr_val

    if sig.signal == "LONG":
        stop    = round(sig.price - dist,             2)
        t1      = round(sig.price + dist * TARGET1_MULT, 2)
        t2      = round(sig.price + dist * TARGET2_MULT, 2)
    else:
        stop    = round(sig.price + dist,             2)
        t1      = round(sig.price - dist * TARGET1_MULT, 2)
        t2      = round(sig.price - dist * TARGET2_MULT, 2)

    risk_dollars = round(qty * dist, 2)

    log.info("─" * 64)
    log.info(f"  TRADE  {sig.symbol}  {sig.signal}  ({'PAPER' if PAPER else 'LIVE'})")
    log.info(f"  Entry  : ${sig.price}   Qty : {qty} shares")
    log.info(f"  Stop   : ${stop}        Risk: ${risk_dollars}")
    log.info(f"  T1     : ${t1}  ({qty//2} shares — 50%)")
    log.info(f"  T2     : ${t2}  ({qty - qty//2} shares — 50%)")
    log.info(f"  Reason : {sig.reason}")
    log.info("─" * 64)

    entry_id = stop_id = t1_id = t2_id = ""

    try:
        entry_order = trade_client.submit_order(MarketOrderRequest(
            symbol=sig.symbol, qty=qty, side=side,
            time_in_force=TimeInForce.DAY,
        ))
        entry_id = str(entry_order.id)
        log.info(f"{sig.symbol}: Entry placed   — ID {entry_id}")

        stop_order = trade_client.submit_order(StopOrderRequest(
            symbol=sig.symbol, qty=qty, side=exit_side,
            stop_price=stop, time_in_force=TimeInForce.DAY,
        ))
        stop_id = str(stop_order.id)
        log.info(f"{sig.symbol}: Stop @ ${stop}  — ID {stop_id}")

        t1_qty = max(1, qty // 2)
        t1_order = trade_client.submit_order(LimitOrderRequest(
            symbol=sig.symbol, qty=t1_qty, side=exit_side,
            limit_price=t1, time_in_force=TimeInForce.DAY,
        ))
        t1_id = str(t1_order.id)
        log.info(f"{sig.symbol}: T1 @ ${t1} ({t1_qty} sh) — ID {t1_id}")

        t2_qty = qty - t1_qty
        if t2_qty > 0:
            t2_order = trade_client.submit_order(LimitOrderRequest(
                symbol=sig.symbol, qty=t2_qty, side=exit_side,
                limit_price=t2, time_in_force=TimeInForce.DAY,
            ))
            t2_id = str(t2_order.id)
            log.info(f"{sig.symbol}: T2 @ ${t2} ({t2_qty} sh) — ID {t2_id}")

        log.info(f"{sig.symbol}: All orders placed ✓")
        status = "OPEN"

    except Exception as exc:
        log.error(f"{sig.symbol}: Order failed — {exc}", exc_info=True)
        status = f"ERROR: {exc}"

    record = TradeRecord(
        timestamp       = datetime.now(tz=ET).isoformat(),
        run_id          = run_id,
        mode            = "PAPER" if PAPER else "LIVE",
        symbol          = sig.symbol,
        direction       = sig.signal,
        entry_price     = sig.price,
        qty             = qty,
        stop_price      = stop,
        target1_price   = t1,
        target2_price   = t2,
        risk_dollars    = risk_dollars,
        capital_limit   = capital_limit,
        portfolio_value = round(portfolio, 2),
        entry_order_id  = entry_id,
        stop_order_id   = stop_id,
        t1_order_id     = t1_id,
        t2_order_id     = t2_id,
        signal_reason   = sig.reason,
        status          = status,
    )
    save_trade(record)
    return record if "ERROR" not in status else None


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def in_position(trade_client, symbol: str) -> bool:
    try:
        pos = trade_client.get_open_position(symbol)
        return abs(float(pos.qty)) > 0
    except Exception:
        return False


def open_count(trade_client) -> int:
    try:
        return len(trade_client.get_all_positions())
    except Exception:
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    run_id = os.getenv("GITHUB_RUN_ID", "local")
    mode   = "PAPER" if PAPER else "LIVE"

    log.info("=" * 64)
    log.info(f"  VWAP + EMA + RSI Bot  —  {mode} mode  —  Run {run_id}")
    log.info(f"  Watchlist : {WATCHLIST}")
    log.info("=" * 64)

    # ── STEP 1: Weekend + timing guard (no API needed) ────────────────────────
    ok, reason = check_day_and_time()
    log.info(f"Timing check: {reason}")
    if not ok:
        log.info("Exiting cleanly — nothing to do.")
        weekly_summary()
        sys.exit(0)

    # ── STEP 2: Validate keys before touching Alpaca ──────────────────────────
    api_key, api_secret = validate_keys()

    # ── STEP 3: Build clients (only after keys confirmed present) ─────────────
    from alpaca.trading.client import TradingClient
    from alpaca.data.historical import StockHistoricalDataClient

    try:
        trade_client  = TradingClient(api_key, api_secret, paper=PAPER)
        market_client = StockHistoricalDataClient(api_key, api_secret)
    except Exception as exc:
        log.error(f"Failed to connect to Alpaca: {exc}")
        sys.exit(1)

    # ── STEP 4: Fetch account ─────────────────────────────────────────────────
    try:
        account   = trade_client.get_account()
        portfolio = float(account.portfolio_value)
        buying_pw = float(account.buying_power)
    except Exception as exc:
        log.error(f"Cannot fetch account: {exc}")
        sys.exit(1)

    log.info(f"Account: Portfolio ${portfolio:,.2f}  |  Buying power ${buying_pw:,.2f}")

    # ── STEP 5: Mode-specific confirmation ────────────────────────────────────
    if PAPER:
        capital_limit = confirm_paper_run()
    else:
        capital_limit = confirm_live_run(portfolio)

    # ── STEP 6: Open position count ───────────────────────────────────────────
    open_trades = open_count(trade_client)
    log.info(f"Open positions: {open_trades} / {MAX_OPEN_TRADES}")

    if open_trades >= MAX_OPEN_TRADES:
        log.info("Max open trades reached — no new entries this scan.")
        weekly_summary()
        sys.exit(0)

    # ── STEP 7: Weekly summary before scan ────────────────────────────────────
    weekly_summary()

    # ── STEP 8: Scan watchlist ────────────────────────────────────────────────
    log.info(f"─── Scanning {len(WATCHLIST)} stocks ───")
    placed = 0

    for symbol in WATCHLIST:
        if open_trades + placed >= MAX_OPEN_TRADES:
            log.info("Max trades hit — stopping scan.")
            break

        if in_position(trade_client, symbol):
            log.info(f"{symbol}: Position already open — skipped")
            continue

        try:
            sig = compute_signal(market_client, symbol)
        except Exception as exc:
            log.error(f"{symbol}: Signal error — {exc}", exc_info=True)
            continue

        indicators = (
            f"Price ${sig.price}  VWAP ${sig.vwap_val}  "
            f"EMA9 {sig.ema9}  EMA21 {sig.ema21}  "
            f"RSI {sig.rsi_val}  Vol {sig.volume:,}"
        )

        if sig.signal in ("LONG", "SHORT"):
            log.info(f"{symbol}: ✓ {sig.signal}  —  {indicators}")
            log.info(f"{symbol}: {sig.reason}")
            result = place_trade(trade_client, sig, portfolio, capital_limit, run_id)
            if result:
                placed += 1
        else:
            log.info(f"{symbol}: — NONE   —  {indicators}")
            log.info(f"{symbol}: {sig.reason}")

        time.sleep(0.5)

    # ── Done ──────────────────────────────────────────────────────────────────
    log.info("=" * 64)
    log.info(f"  Scan complete — trades placed this run: {placed}")
    log.info(f"  Total open positions: {open_trades + placed} / {MAX_OPEN_TRADES}")
    log.info("=" * 64)
    weekly_summary()


if __name__ == "__main__":
    main()
