"""
Strategy implementations.
Each returns a Signal dataclass.

Strategies:
  1. VWAP Mean Reversion  — midday pullback to VWAP with EMA + RSI confirmation
  2. Opening Range Breakout (ORB) — breakout above/below first 15-min range
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import config

ET  = ZoneInfo("America/New_York")
log = logging.getLogger("Strategies")


@dataclass
class Signal:
    symbol:    str
    signal:    str       # LONG | SHORT | NONE
    strategy:  str       # VWAP | ORB | NONE
    price:     float = 0.0
    vwap_val:  float = 0.0
    ema9:      float = 0.0
    ema21:     float = 0.0
    rsi_val:   float = 0.0
    atr_val:   float = 0.0
    volume:    int   = 0
    avg_vol:   int   = 0
    confirmed: bool  = False
    reason:    str   = ""
    # ORB-specific
    orb_high:  float = 0.0
    orb_low:   float = 0.0


# ── Indicators ─────────────────────────────────────────────────────────────────

def _ema(s: pd.Series, p: int) -> pd.Series:
    return s.ewm(span=p, adjust=False).mean()

def _rsi(s: pd.Series, p: int = 14) -> pd.Series:
    d  = s.diff()
    ag = d.clip(lower=0).ewm(com=p-1, adjust=False).mean()
    al = (-d.clip(upper=0)).ewm(com=p-1, adjust=False).mean()
    return 100 - (100 / (1 + ag / al.replace(0, np.nan)))

def _atr(df: pd.DataFrame, p: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(com=p-1, adjust=False).mean()

def _vwap(df: pd.DataFrame) -> pd.Series:
    df = df.copy()
    df["date"] = df.index.date
    df["tp"]   = (df["high"] + df["low"] + df["close"]) / 3
    df["ctpv"] = df.groupby("date").apply(lambda g: (g["tp"]*g["volume"]).cumsum()).values
    df["cvol"] = df.groupby("date")["volume"].cumsum().values
    return df["ctpv"] / df["cvol"]


def _add_indicators(bars: pd.DataFrame) -> pd.DataFrame:
    bars["ema9"]    = _ema(bars["close"], config.EMA_FAST)
    bars["ema21"]   = _ema(bars["close"], config.EMA_SLOW)
    bars["rsi"]     = _rsi(bars["close"], config.RSI_PERIOD)
    bars["atr"]     = _atr(bars, config.ATR_PERIOD)
    bars["vwap"]    = _vwap(bars)
    bars["avg_vol"] = bars["volume"].rolling(20).mean()
    return bars


def _bar_vwap_signal(row: pd.Series) -> str:
    price, v   = row["close"], row["vwap"]
    e9, e21    = row["ema9"],  row["ema21"]
    r          = row["rsi"]
    vol_ok     = row["volume"] >= row["avg_vol"] * config.VOLUME_MULT
    if price > v and e9 > e21 and config.RSI_LONG_LOW < r < config.RSI_LONG_HIGH and vol_ok:
        return "LONG"
    if price < v and e9 < e21 and config.RSI_SHORT_LOW < r < config.RSI_SHORT_HIGH and vol_ok:
        return "SHORT"
    return "NONE"


# ── Strategy 1: VWAP Mean Reversion ───────────────────────────────────────────

def vwap_strategy(symbol: str, bars: pd.DataFrame) -> Signal:
    """
    Entry when price pulls back to VWAP in an established trend,
    confirmed on 2 consecutive 15-min bars.
    Best during 10:30 AM – 3:00 PM ET.
    """
    if len(bars) < 30:
        return Signal(symbol=symbol, signal="NONE", strategy="VWAP",
                      reason="Insufficient data")

    bars = _add_indicators(bars.copy())
    curr = bars.iloc[-1]
    prev = bars.iloc[-2]

    curr_sig = _bar_vwap_signal(curr)
    prev_sig = _bar_vwap_signal(prev)
    confirmed = curr_sig != "NONE" and curr_sig == prev_sig

    price = float(curr["close"]); v   = float(curr["vwap"])
    e9    = float(curr["ema9"]);  e21 = float(curr["ema21"])
    r     = float(curr["rsi"]);   a   = float(curr["atr"])
    vol   = int(curr["volume"]);  avg = int(curr["avg_vol"])

    base = Signal(
        symbol=symbol, strategy="VWAP",
        price=round(price,2), vwap_val=round(v,2),
        ema9=round(e9,2), ema21=round(e21,2),
        rsi_val=round(r,2), atr_val=round(a,2),
        volume=vol, avg_vol=avg,
    )

    if curr_sig == "NONE":
        parts = []
        if not (r < config.RSI_LONG_HIGH and r > config.RSI_SHORT_LOW):
            parts.append(f"RSI {r:.1f} outside zones")
        if not abs(price - v) / max(v, 1) < 0.02:
            parts.append("Price not near VWAP")
        if vol < avg * config.VOLUME_MULT:
            parts.append(f"Low volume")
        base.signal = "NONE"
        base.reason = "VWAP: " + (" | ".join(parts) if parts else "no signal")
        return base

    if not confirmed:
        base.signal    = "NONE"
        base.confirmed = False
        base.reason    = (
            f"VWAP: {curr_sig} on current bar — waiting for next bar to confirm. "
            f"(Prev bar: {prev_sig}). Will enter if next scan also shows {curr_sig}."
        )
        return base

    direction = "above" if curr_sig == "LONG" else "below"
    base.signal    = curr_sig
    base.confirmed = True
    base.reason    = (
        f"VWAP confirmed (2 bars): price {direction} VWAP | "
        f"EMA9 {'>' if curr_sig=='LONG' else '<'} EMA21 | "
        f"RSI {r:.1f} | Vol {vol:,}"
    )
    return base


# ── Strategy 2: Opening Range Breakout ────────────────────────────────────────

def orb_strategy(symbol: str, bars: pd.DataFrame) -> Signal:
    """
    Identify the opening range (first 15 min candles = 9:30–9:45 ET).
    Enter LONG on breakout above ORB high + ATR buffer.
    Enter SHORT on breakdown below ORB low - ATR buffer.
    Best during 9:45 AM – 10:30 AM ET.

    Why ORB works:
    - Market makers establish price in the first 15 min
    - Institutional orders often drive moves AFTER the opening range is set
    - A clean break with volume = directional intent for the rest of the day
    """
    if len(bars) < 10:
        return Signal(symbol=symbol, signal="NONE", strategy="ORB",
                      reason="Insufficient data for ORB")

    today = datetime.now(ET).date()
    today_bars = bars[bars.index.date == today]

    if len(today_bars) < 2:
        return Signal(symbol=symbol, signal="NONE", strategy="ORB",
                      reason="No today bars yet")

    # Opening range = first N 15-min bars after 9:30 AM
    market_open = datetime.now(ET).replace(hour=9, minute=30, second=0, microsecond=0)
    orb_end     = market_open + timedelta(minutes=config.ORB_MINUTES)
    orb_bars    = today_bars[today_bars.index <= orb_end.replace(tzinfo=ET)]

    if orb_bars.empty:
        return Signal(symbol=symbol, signal="NONE", strategy="ORB",
                      reason="Opening range not yet established")

    orb_high = float(orb_bars["high"].max())
    orb_low  = float(orb_bars["low"].min())
    orb_range= orb_high - orb_low

    # Need a meaningful range (not a flat open)
    if orb_range < 0.10:
        return Signal(symbol=symbol, signal="NONE", strategy="ORB",
                      reason=f"ORB range too tight (${orb_range:.2f}) — flat open, skipping")

    bars = _add_indicators(bars.copy())
    curr = bars.iloc[-1]
    price = float(curr["close"])
    atr   = float(curr["atr"])
    vol   = int(curr["volume"])
    avg   = int(curr["avg_vol"])
    vol_ok = vol >= avg * config.VOLUME_MULT

    buffer = atr * config.ORB_ATR_BUFFER

    base = Signal(
        symbol=symbol, strategy="ORB",
        price=round(price,2), atr_val=round(atr,2),
        orb_high=round(orb_high,2), orb_low=round(orb_low,2),
        vwap_val=round(float(curr["vwap"]),2),
        ema9=round(float(curr["ema9"]),2), ema21=round(float(curr["ema21"]),2),
        rsi_val=round(float(curr["rsi"]),2),
        volume=vol, avg_vol=avg,
    )

    if price > orb_high + buffer and vol_ok:
        base.signal    = "LONG"
        base.confirmed = True
        base.reason    = (
            f"ORB LONG: price ${price:.2f} broke above range high ${orb_high:.2f} "
            f"+ buffer ${buffer:.2f} | Range: ${orb_range:.2f} | Vol {vol:,}"
        )
    elif price < orb_low - buffer and vol_ok:
        base.signal    = "SHORT"
        base.confirmed = True
        base.reason    = (
            f"ORB SHORT: price ${price:.2f} broke below range low ${orb_low:.2f} "
            f"- buffer ${buffer:.2f} | Range: ${orb_range:.2f} | Vol {vol:,}"
        )
    else:
        reasons = []
        if price <= orb_high + buffer and price >= orb_low - buffer:
            reasons.append(f"Price ${price:.2f} inside range (${orb_low:.2f}–${orb_high:.2f})")
        if not vol_ok:
            reasons.append(f"Volume {vol:,} < threshold {avg*config.VOLUME_MULT:,.0f}")
        base.signal = "NONE"
        base.reason = "ORB: " + " | ".join(reasons)

    return base


# ── Strategy selector ─────────────────────────────────────────────────────────

def select_strategy(now: datetime) -> str:
    """Return the best strategy name for the current time."""
    for name, window in config.STRATEGIES.items():
        start = now.replace(hour=window["start"][0], minute=window["start"][1],
                            second=0, microsecond=0)
        end   = now.replace(hour=window["end"][0],   minute=window["end"][1],
                            second=0, microsecond=0)
        if start <= now < end:
            return name
    return "VWAP"   # default outside named windows


def run_strategy(symbol: str, bars: pd.DataFrame) -> Signal:
    """Pick and run the right strategy for the current time."""
    now      = datetime.now(ET)
    strategy = select_strategy(now)
    log.info(f"{symbol}: Running {strategy} strategy ({now.strftime('%I:%M %p ET')})")
    if strategy == "ORB":
        return orb_strategy(symbol, bars)
    return vwap_strategy(symbol, bars)
