"""
VWAP + ORB Trading Bot v4 — Enterprise Edition
===============================================
Single-scan design for GitHub Actions.
Runs all guardrails, selects correct strategy by time of day,
executes trades, persists records to Gist, alerts via Telegram.
"""
import logging
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import config
import alerts
import guardrails
import records
from strategies import Signal, run_strategy

ET  = ZoneInfo("America/New_York")
log = logging.getLogger("Bot")


# ── Logging setup ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  [%(name)s]  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("strategy_log.txt", encoding="utf-8"),
    ],
)


# ── Timing ────────────────────────────────────────────────────────────────────

def check_timing() -> tuple[bool, str]:
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False, f"Weekend ({['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][now.weekday()]}) — no markets."
    today_open  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    today_close = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    from datetime import timedelta
    open_guard  = today_open  + timedelta(minutes=config.OPEN_BUFFER_MIN)
    close_guard = today_close - timedelta(minutes=config.CLOSE_BUFFER_MIN)
    if now < open_guard:
        mins = int((open_guard - now).total_seconds() / 60)
        return False, f"Opening buffer — {mins} min until {open_guard.strftime('%I:%M %p ET')}"
    if now >= close_guard:
        return False, f"Closing buffer — no new trades after {close_guard.strftime('%I:%M %p ET')}"
    return True, f"Market open — {now.strftime('%A %I:%M %p ET')}"


# ── Key validation ─────────────────────────────────────────────────────────────

def validate_keys() -> tuple[str, str]:
    if config.PAPER:
        if not config.PAPER_API_KEY or not config.PAPER_API_SECRET:
            log.error("Missing: ALPACA_PAPER_API_KEY / ALPACA_PAPER_API_SECRET in GitHub Secrets")
            sys.exit(1)
        return config.PAPER_API_KEY, config.PAPER_API_SECRET
    if not config.LIVE_API_KEY or not config.LIVE_API_SECRET:
        log.error("Missing: ALPACA_LIVE_API_KEY / ALPACA_LIVE_API_SECRET in GitHub Secrets")
        sys.exit(1)
    return config.LIVE_API_KEY, config.LIVE_API_SECRET


# ── Mode confirmation ──────────────────────────────────────────────────────────

def confirm_mode(portfolio: float) -> float:
    if config.PAPER:
        if config.CAPITAL_LIMIT <= 0:
            log.error("Set CAPITAL_LIMIT > 0 in workflow input"); sys.exit(1)
        log.info("─"*64)
        log.info(f"  PAPER MODE | Capital: ${config.CAPITAL_LIMIT:,.2f} | Portfolio: ${portfolio:,.2f}")
        log.info(f"  Endpoint: https://paper-api.alpaca.markets")
        log.info("─"*64)
        return config.CAPITAL_LIMIT

    # LIVE mode — strict confirmation
    log.warning("="*64)
    log.warning("  LIVE TRADING — REAL MONEY")
    log.warning("="*64)
    if config.LIVE_CONFIRM.strip().lower() != "confirm":
        log.error('Set live_confirm input to exactly: confirm'); sys.exit(1)
    if config.CAPITAL_LIMIT <= 0:
        log.error("Set capital_limit > 0 in workflow input"); sys.exit(1)
    if config.CAPITAL_LIMIT > portfolio:
        log.error(f"capital_limit ${config.CAPITAL_LIMIT:,.2f} > portfolio ${portfolio:,.2f}"); sys.exit(1)
    log.info(f"  Portfolio : ${portfolio:,.2f}")
    log.info(f"  Capital   : ${config.CAPITAL_LIMIT:,.2f}")
    log.info(f"  Max risk  : ${portfolio * config.MAX_RISK_PCT:,.2f}")
    log.info(f"  Endpoint  : https://api.alpaca.markets")
    log.info(f"  CONFIRMED ✓")
    log.info("="*64)
    return config.CAPITAL_LIMIT


# ── Data fetch ────────────────────────────────────────────────────────────────

def fetch_bars(mc, symbol: str):
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from datetime import timezone, timedelta
    try:
        end   = datetime.now(tz=timezone.utc)
        start = end - timedelta(days=5)
        req   = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame(15, TimeFrameUnit.Minute),
            start=start, end=end,
        )
        bars = mc.get_stock_bars(req).df
        if bars.empty: return None
        if hasattr(bars.index, 'levels'):
            bars = bars.loc[symbol]
        import pandas as pd
        bars.index = pd.to_datetime(bars.index, utc=True).tz_convert(ET)
        return bars.sort_index()
    except Exception as exc:
        log.error(f"{symbol}: fetch_bars — {exc}")
        return None


# ── Position sizing ────────────────────────────────────────────────────────────

def calc_qty(portfolio: float, price: float, atr: float, limit: float) -> int:
    risk   = portfolio * config.MAX_RISK_PCT
    dist   = max(atr, 0.01)
    shares = risk / dist
    shares = min(shares, (portfolio * config.MAX_POSITION_PCT) / price)
    if limit > 0: shares = min(shares, limit / price)
    return max(1, int(shares))


# ── Order execution ────────────────────────────────────────────────────────────

def place_trade(tc, sig: Signal, portfolio: float, capital_limit: float,
                run_id: str) -> bool:
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest, StopOrderRequest

    side      = OrderSide.BUY  if sig.signal == "LONG"  else OrderSide.SELL
    exit_side = OrderSide.SELL if sig.signal == "LONG"  else OrderSide.BUY
    qty       = calc_qty(portfolio, sig.price, sig.atr_val, capital_limit)
    dist      = sig.atr_val

    stop = round(sig.price - dist if sig.signal == "LONG" else sig.price + dist, 2)
    t1   = round(sig.price + dist*config.TARGET1_MULT if sig.signal == "LONG" else sig.price - dist*config.TARGET1_MULT, 2)
    t2   = round(sig.price + dist*config.TARGET2_MULT if sig.signal == "LONG" else sig.price - dist*config.TARGET2_MULT, 2)
    risk_dollars = round(qty * dist, 2)

    log.info("─"*64)
    log.info(f"  TRADE  {sig.symbol}  {sig.signal}  [{sig.strategy}]  ({'PAPER' if config.PAPER else 'LIVE'})")
    log.info(f"  Entry ${sig.price}  Qty {qty}  Stop ${stop}  Risk ${risk_dollars}")
    log.info(f"  T1 ${t1} ({qty//2} sh)  T2 ${t2} ({qty-qty//2} sh)")
    log.info(f"  {sig.reason}")
    log.info("─"*64)

    entry_id = stop_id = t1_id = t2_id = ""
    status = "OPEN"

    try:
        e = tc.submit_order(MarketOrderRequest(
            symbol=sig.symbol, qty=qty, side=side, time_in_force=TimeInForce.DAY))
        entry_id = str(e.id)
        log.info(f"{sig.symbol}: Entry — {entry_id}")

        s = tc.submit_order(StopOrderRequest(
            symbol=sig.symbol, qty=qty, side=exit_side,
            stop_price=stop, time_in_force=TimeInForce.DAY))
        stop_id = str(s.id)
        log.info(f"{sig.symbol}: Stop @ ${stop} — {stop_id}")

        t1_qty = max(1, qty//2)
        o1 = tc.submit_order(LimitOrderRequest(
            symbol=sig.symbol, qty=t1_qty, side=exit_side,
            limit_price=t1, time_in_force=TimeInForce.DAY))
        t1_id = str(o1.id)
        log.info(f"{sig.symbol}: T1 @ ${t1} ({t1_qty} sh) — {t1_id}")

        t2_qty = qty - t1_qty
        if t2_qty > 0:
            o2 = tc.submit_order(LimitOrderRequest(
                symbol=sig.symbol, qty=t2_qty, side=exit_side,
                limit_price=t2, time_in_force=TimeInForce.DAY))
            t2_id = str(o2.id)
            log.info(f"{sig.symbol}: T2 @ ${t2} ({t2_qty} sh) — {t2_id}")

        alerts.trade_entered(sig.symbol, sig.signal, sig.price, qty, stop, t1, t2,
                             sig.strategy, "PAPER" if config.PAPER else "LIVE")
        log.info(f"{sig.symbol}: All orders placed ✓")

    except Exception as exc:
        log.error(f"{sig.symbol}: Order failed — {exc}", exc_info=True)
        status = f"ERROR: {exc}"

    records.append(records.TradeRecord(
        timestamp=datetime.now(tz=ET).isoformat(),
        run_id=run_id, mode="PAPER" if config.PAPER else "LIVE",
        symbol=sig.symbol, strategy=sig.strategy, direction=sig.signal,
        entry_price=sig.price, qty=qty, stop_price=stop,
        target1_price=t1, target2_price=t2, risk_dollars=risk_dollars,
        capital_limit=capital_limit, portfolio_value=round(portfolio,2),
        entry_order_id=entry_id, stop_order_id=stop_id,
        t1_order_id=t1_id, t2_order_id=t2_id,
        signal_reason=sig.reason, confirmed=sig.confirmed,
        orb_high=sig.orb_high, orb_low=sig.orb_low,
        status=status,
    ))
    return "ERROR" not in status


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    run_id = os.getenv("GITHUB_RUN_ID", "local")
    mode   = "PAPER" if config.PAPER else "LIVE"
    now    = datetime.now(ET)

    log.info("="*64)
    log.info(f"  Trading Bot v4  —  {mode}  —  {now.strftime('%A %b %d %I:%M %p ET')}")
    log.info(f"  Run ID: {run_id}  |  Watchlist: {config.WATCHLIST}")
    log.info("="*64)

    # 1. Timing
    ok, reason = check_timing()
    log.info(f"Timing: {reason}")
    if not ok:
        log.info("Exiting cleanly.")
        alerts.market_closed(reason)
        sys.exit(0)

    # 2. Keys
    api_key, api_secret = validate_keys()

    # 3. Clients
    from alpaca.trading.client import TradingClient
    from alpaca.data.historical import StockHistoricalDataClient
    try:
        tc = TradingClient(api_key, api_secret, paper=config.PAPER)
        mc = StockHistoricalDataClient(api_key, api_secret)
    except Exception as exc:
        log.error(f"Alpaca connection failed: {exc}"); sys.exit(1)

    # 4. Account
    try:
        acct      = tc.get_account()
        portfolio = float(acct.portfolio_value)
        buying_pw = float(acct.buying_power)
    except Exception as exc:
        log.error(f"Cannot fetch account: {exc}"); sys.exit(1)

    log.info(f"Account: ${portfolio:,.2f} portfolio  |  ${buying_pw:,.2f} buying power")

    # 5. Mode confirmation
    capital_limit = confirm_mode(portfolio)

    # 6. Load records + session tracking
    all_records       = records.purge_old(records.load())
    trades_today      = records.daily_trade_count(all_records)
    session_start_val = portfolio   # simplification — ideal: track from first run today
    session_high      = portfolio
    log.info(records.summary(all_records))

    # 7. Open positions
    try:
        open_positions_obj = tc.get_all_positions()
        open_positions = [p.symbol for p in open_positions_obj]
        open_count     = len(open_positions)
    except Exception:
        open_positions, open_count = [], 0

    log.info(f"Open positions: {open_count}/{config.MAX_OPEN_TRADES}  {open_positions}")

    # 8. Global guardrails (account-level)
    acct_ok, acct_msgs = guardrails.run_all_guardrails(
        symbol="ACCOUNT", portfolio=portfolio, buying_power=buying_pw,
        session_start_value=session_start_val, session_high=session_high,
        trades_today=trades_today, open_positions=open_positions,
    )
    for msg in acct_msgs:
        log.info(f"  Guardrail: {msg}")
    if not acct_ok:
        alerts.circuit_breaker("\n".join(m for m in acct_msgs if "CIRCUIT" in m or "PDT" in m or "WARNING" in m))
        log.error("Account-level guardrail failed — no trades this run.")
        sys.exit(0)

    if open_count >= config.MAX_OPEN_TRADES:
        log.info("Max open trades reached — nothing to do.")
        sys.exit(0)

    # 9. Scan
    log.info(f"─── Scanning {len(config.WATCHLIST)} stocks ───")
    signals_found = 0
    placed        = 0

    for symbol in config.WATCHLIST:
        if open_count + placed >= config.MAX_OPEN_TRADES:
            log.info("Max trades reached — stopping scan."); break

        if symbol in open_positions:
            log.info(f"{symbol}: Already in position — skipped"); continue

        # Fetch bars
        bars = fetch_bars(mc, symbol)
        if bars is None or len(bars) < 10:
            log.info(f"{symbol}: No data — skipped"); continue

        # Per-symbol guardrails
        sym_ok, sym_msgs = guardrails.run_all_guardrails(
            symbol=symbol, portfolio=portfolio, buying_power=buying_pw,
            session_start_value=session_start_val, session_high=session_high,
            trades_today=trades_today + placed,
            open_positions=open_positions,
            bars_df=bars,
        )
        for msg in sym_msgs:
            if "OK" not in msg and "skipped" not in msg.lower():
                log.info(f"  {symbol} guardrail: {msg}")

        if not sym_ok:
            log.info(f"{symbol}: Blocked by guardrail — skipped")
            continue

        # Run strategy
        try:
            sig = run_strategy(symbol, bars)
        except Exception as exc:
            log.error(f"{symbol}: Strategy error — {exc}", exc_info=True)
            continue

        indicators = (
            f"Price ${sig.price}  VWAP ${sig.vwap_val}  "
            f"EMA9 {sig.ema9}  EMA21 {sig.ema21}  RSI {sig.rsi_val}  "
            f"Vol {sig.volume:,}  ATR {sig.atr_val}"
        )

        if sig.signal in ("LONG", "SHORT"):
            signals_found += 1
            log.info(f"{symbol}: ✓ {sig.signal} [{sig.strategy}]  —  {indicators}")
            log.info(f"{symbol}: {sig.reason}")
            success = place_trade(tc, sig, portfolio, capital_limit, run_id)
            if success:
                placed += 1
        else:
            log.info(f"{symbol}: — NONE [{sig.strategy}]  —  {indicators}")
            log.info(f"{symbol}: {sig.reason}")

        time.sleep(0.5)

    # 10. Done
    alerts.scan_summary(config.WATCHLIST, signals_found, placed, mode)
    log.info("="*64)
    log.info(f"  Complete: {placed} trades placed | {open_count+placed}/{config.MAX_OPEN_TRADES} open")
    log.info("="*64)


if __name__ == "__main__":
    main()
