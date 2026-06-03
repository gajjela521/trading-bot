"""
Trading Bot v4 — writes a RunLog entry on EVERY run, even market-closed.
Dashboard always has data to show.
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
from records import RunLog, ScanResult
from strategies import Signal, run_strategy

ET  = ZoneInfo("America/New_York")
log = logging.getLogger("Bot")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  [%(name)s]  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("strategy_log.txt", encoding="utf-8"),
    ],
)

# ── Run log builder ────────────────────────────────────────────────────────────
# Built up throughout main() and saved at every exit point

def _now_iso() -> str:
    return datetime.now(ET).isoformat()

def _save_and_exit(run: RunLog, code: int = 0):
    """Save run log then exit — called at every exit point."""
    records.save_run(run)
    sys.exit(code)


# ── Timing ─────────────────────────────────────────────────────────────────────
def check_timing() -> tuple[bool, str]:
    from datetime import timedelta
    now = datetime.now(ET)
    if now.weekday() >= 5:
        day = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][now.weekday()]
        return False, f"Weekend ({day}) — markets closed"
    today_open  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    today_close = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    open_guard  = today_open  + timedelta(minutes=config.OPEN_BUFFER_MIN)
    close_guard = today_close - timedelta(minutes=config.CLOSE_BUFFER_MIN)
    if now < open_guard:
        mins = int((open_guard - now).total_seconds() / 60)
        return False, f"Opening buffer — {mins} min until {open_guard.strftime('%I:%M %p ET')}"
    if now >= close_guard:
        return False, f"Closing buffer — no new trades after {close_guard.strftime('%I:%M %p ET')}"
    return True, f"Market open — {now.strftime('%A %I:%M %p ET')}"


# ── Key validation ──────────────────────────────────────────────────────────────
def validate_keys() -> tuple[str, str]:
    if config.PAPER:
        if not config.PAPER_API_KEY or not config.PAPER_API_SECRET:
            log.error("Missing: ALPACA_PAPER_API_KEY / ALPACA_PAPER_API_SECRET")
            return "", ""
        return config.PAPER_API_KEY, config.PAPER_API_SECRET
    if not config.LIVE_API_KEY or not config.LIVE_API_SECRET:
        log.error("Missing: ALPACA_LIVE_API_KEY / ALPACA_LIVE_API_SECRET")
        return "", ""
    return config.LIVE_API_KEY, config.LIVE_API_SECRET


# ── Mode confirmation ───────────────────────────────────────────────────────────
def confirm_mode(portfolio: float) -> tuple[bool, float]:
    """Returns (ok, capital_limit)."""
    if config.PAPER:
        if config.CAPITAL_LIMIT <= 0:
            log.error("Set CAPITAL_LIMIT > 0 in workflow input"); return False, 0
        log.info("─"*60)
        log.info(f"  PAPER MODE | Capital: ${config.CAPITAL_LIMIT:,.2f} | Portfolio: ${portfolio:,.2f}")
        log.info(f"  Endpoint: https://paper-api.alpaca.markets")
        log.info("─"*60)
        return True, config.CAPITAL_LIMIT
    log.warning("="*60)
    log.warning("  LIVE TRADING — REAL MONEY")
    log.warning("="*60)
    if config.LIVE_CONFIRM.strip().lower() != "confirm":
        log.error("Set live_confirm to exactly: confirm"); return False, 0
    if config.CAPITAL_LIMIT <= 0:
        log.error("Set capital_limit > 0"); return False, 0
    if config.CAPITAL_LIMIT > portfolio:
        log.error(f"capital_limit > portfolio"); return False, 0
    log.info(f"  Portfolio: ${portfolio:,.2f} | Capital: ${config.CAPITAL_LIMIT:,.2f} | CONFIRMED ✓")
    log.info("="*60)
    return True, config.CAPITAL_LIMIT


# ── Data fetch ──────────────────────────────────────────────────────────────────
def fetch_bars(mc, symbol: str):
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from alpaca.data.enums import DataFeed
    from datetime import timezone, timedelta
    import pandas as pd
    try:
        end   = datetime.now(tz=timezone.utc)
        start = end - timedelta(days=7)   # extra days to ensure 30+ bars
        req   = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame(15, TimeFrameUnit.Minute),
            start=start,
            end=end,
            feed=DataFeed.IEX,            # IEX feed — free for all Alpaca accounts
        )
        bars = mc.get_stock_bars(req).df
        if bars.empty:
            log.warning(f"{symbol}: empty bars — IEX may not have data for this symbol")
            return None
        if hasattr(bars.index, "levels"):
            bars = bars.loc[symbol]
        bars.index = pd.to_datetime(bars.index, utc=True).tz_convert(ET)
        bars = bars.sort_index()
        log.info(f"{symbol}: fetched {len(bars)} bars (latest: {bars.index[-1].strftime('%m/%d %I:%M %p ET')})")
        return bars
    except Exception as exc:
        log.error(f"{symbol}: fetch_bars failed — {exc}")
        return None


# ── Position sizing ─────────────────────────────────────────────────────────────
def calc_qty(portfolio: float, price: float, atr: float, limit: float) -> int:
    risk   = portfolio * config.MAX_RISK_PCT
    dist   = max(atr, 0.01)
    shares = risk / dist
    shares = min(shares, (portfolio * config.MAX_POSITION_PCT) / price)
    if limit > 0: shares = min(shares, limit / price)
    return max(1, int(shares))


# ── Order execution ─────────────────────────────────────────────────────────────
def place_trade(tc, sig: Signal, portfolio: float, capital_limit: float,
                run_id: str) -> bool:
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest, StopOrderRequest

    side      = OrderSide.BUY  if sig.signal == "LONG"  else OrderSide.SELL
    exit_side = OrderSide.SELL if sig.signal == "LONG"  else OrderSide.BUY
    qty       = calc_qty(portfolio, sig.price, sig.atr_val, capital_limit)
    dist      = sig.atr_val
    stop = round(sig.price - dist if sig.signal=="LONG" else sig.price + dist, 2)
    t1   = round(sig.price + dist*config.TARGET1_MULT if sig.signal=="LONG" else sig.price - dist*config.TARGET1_MULT, 2)
    t2   = round(sig.price + dist*config.TARGET2_MULT if sig.signal=="LONG" else sig.price - dist*config.TARGET2_MULT, 2)
    risk_dollars = round(qty * dist, 2)

    log.info("─"*60)
    log.info(f"  TRADE {sig.symbol} {sig.signal} [{sig.strategy}] ({'PAPER' if config.PAPER else 'LIVE'})")
    log.info(f"  Entry ${sig.price}  Qty {qty}  Stop ${stop}  Risk ${risk_dollars}")
    log.info(f"  T1 ${t1} ({qty//2} sh)  T2 ${t2} ({qty - qty//2} sh)")
    log.info(f"  {sig.reason}")
    log.info("─"*60)

    entry_id = stop_id = t1_id = t2_id = ""
    status = "OPEN"
    try:
        e = tc.submit_order(MarketOrderRequest(
            symbol=sig.symbol, qty=qty, side=side, time_in_force=TimeInForce.DAY))
        entry_id = str(e.id)
        s = tc.submit_order(StopOrderRequest(
            symbol=sig.symbol, qty=qty, side=exit_side,
            stop_price=stop, time_in_force=TimeInForce.DAY))
        stop_id = str(s.id)
        t1_qty = max(1, qty//2)
        o1 = tc.submit_order(LimitOrderRequest(
            symbol=sig.symbol, qty=t1_qty, side=exit_side,
            limit_price=t1, time_in_force=TimeInForce.DAY))
        t1_id = str(o1.id)
        t2_qty = qty - t1_qty
        if t2_qty > 0:
            o2 = tc.submit_order(LimitOrderRequest(
                symbol=sig.symbol, qty=t2_qty, side=exit_side,
                limit_price=t2, time_in_force=TimeInForce.DAY))
            t2_id = str(o2.id)
        alerts.trade_entered(sig.symbol, sig.signal, sig.price, qty, stop, t1, t2,
                             sig.strategy, "PAPER" if config.PAPER else "LIVE")
        log.info(f"{sig.symbol}: All orders placed ✓")
    except Exception as exc:
        log.error(f"{sig.symbol}: Order failed — {exc}", exc_info=True)
        status = f"ERROR: {exc}"

    records.append_trade(records.TradeRecord(
        timestamp=_now_iso(), run_id=run_id,
        mode="PAPER" if config.PAPER else "LIVE",
        symbol=sig.symbol, strategy=sig.strategy, direction=sig.signal,
        entry_price=sig.price, qty=qty, stop_price=stop,
        target1_price=t1, target2_price=t2, risk_dollars=risk_dollars,
        capital_limit=capital_limit, portfolio_value=round(portfolio, 2),
        entry_order_id=entry_id, stop_order_id=stop_id,
        t1_order_id=t1_id, t2_order_id=t2_id,
        signal_reason=sig.reason, confirmed=sig.confirmed,
        orb_high=sig.orb_high, orb_low=sig.orb_low, status=status,
    ))
    return "ERROR" not in status


# ── Helpers ─────────────────────────────────────────────────────────────────────
def in_position(tc, symbol):
    try: return abs(float(tc.get_open_position(symbol).qty)) > 0
    except Exception: return False

def open_count(tc):
    try: return len(tc.get_all_positions())
    except Exception: return 0


# ── Main ─────────────────────────────────────────────────────────────────────────
def main():
    run_id = os.getenv("GITHUB_RUN_ID", "local")
    mode   = "PAPER" if config.PAPER else "LIVE"
    now    = datetime.now(ET)

    log.info("="*60)
    log.info(f"  Trading Bot v4 — {mode} — {now.strftime('%a %b %d %I:%M %p ET')}")
    log.info(f"  Run ID: {run_id} | Watchlist: {config.WATCHLIST}")
    log.info("="*60)

    # Build run log — populated throughout and saved at every exit
    run = RunLog(
        timestamp=_now_iso(), run_id=run_id, mode=mode,
        status="OK", market_open=False, timing_reason="",
        portfolio_value=0, buying_power=0,
        capital_limit=config.CAPITAL_LIMIT,
        open_positions=0, trades_placed=0,
        signals_found=0, guardrail_blocks=0,
        watchlist=config.WATCHLIST, scans=[],
    )

    # 1. Timing
    ok, reason = check_timing()
    run.timing_reason = reason
    run.market_open   = ok
    log.info(f"Timing: {reason}")
    if not ok:
        run.status = "MARKET_CLOSED" if "closed" in reason.lower() or "buffer" in reason.lower() or "weekend" in reason.lower() else "TIMING"
        log.info("Exiting cleanly — nothing to do.")
        _save_and_exit(run, 0)

    # 2. Keys
    api_key, api_secret = validate_keys()
    if not api_key:
        run.status = "ERROR"
        run.error_detail = "Missing API keys"
        _save_and_exit(run, 1)

    # 3. Clients
    from alpaca.trading.client import TradingClient
    from alpaca.data.historical import StockHistoricalDataClient
    try:
        tc = TradingClient(api_key, api_secret, paper=config.PAPER)
        mc = StockHistoricalDataClient(api_key, api_secret)
    except Exception as exc:
        log.error(f"Alpaca connection failed: {exc}")
        run.status = "ERROR"
        run.error_detail = str(exc)
        _save_and_exit(run, 1)

    # 4. Account
    try:
        acct      = tc.get_account()
        portfolio = float(acct.portfolio_value)
        buying_pw = float(acct.buying_power)
        run.portfolio_value = round(portfolio, 2)
        run.buying_power    = round(buying_pw, 2)
    except Exception as exc:
        log.error(f"Cannot fetch account: {exc}")
        run.status = "ERROR"
        run.error_detail = str(exc)
        _save_and_exit(run, 1)

    log.info(f"Account: ${portfolio:,.2f} portfolio | ${buying_pw:,.2f} buying power")

    # 5. Mode confirmation
    confirmed, capital_limit = confirm_mode(portfolio)
    run.capital_limit = capital_limit
    if not confirmed:
        run.status = "ERROR"
        run.error_detail = "Mode confirmation failed"
        _save_and_exit(run, 1)

    # 6. Open positions
    try:
        open_positions_obj = tc.get_all_positions()
        open_positions     = [p.symbol for p in open_positions_obj]
        open_cnt           = len(open_positions)
    except Exception:
        open_positions, open_cnt = [], 0
    run.open_positions = open_cnt
    log.info(f"Open positions: {open_cnt}/{config.MAX_OPEN_TRADES}  {open_positions}")

    # 7. Account-level guardrails
    all_trades = records.load_trades()
    trades_today = records.daily_trade_count(all_trades)
    log.info(records.summary(all_trades))

    acct_ok, acct_msgs = guardrails.run_all_guardrails(
        symbol="ACCOUNT", portfolio=portfolio, buying_power=buying_pw,
        session_start_value=portfolio, session_high=portfolio,
        trades_today=trades_today, open_positions=open_positions,
    )
    for msg in acct_msgs:
        if "OK" not in msg: log.info(f"  Guardrail: {msg}")
    if not acct_ok:
        run.status = "GUARDRAIL_BLOCK"
        run.error_detail = "; ".join(m for m in acct_msgs if any(w in m for w in ["CIRCUIT","PDT","WARNING","Error"]))
        alerts.circuit_breaker(run.error_detail)
        _save_and_exit(run, 0)

    if open_cnt >= config.MAX_OPEN_TRADES:
        run.status = "MAX_TRADES"
        _save_and_exit(run, 0)

    # 8. Scan each symbol
    log.info(f"─── Scanning {len(config.WATCHLIST)} stocks ───")
    placed = 0
    scan_results = []

    for symbol in config.WATCHLIST:
        if open_cnt + placed >= config.MAX_OPEN_TRADES:
            log.info("Max trades — stopping scan."); break

        if symbol in open_positions:
            log.info(f"{symbol}: Already in position — skipped")
            scan_results.append(asdict_scan(symbol, "SKIPPED", "Already in position"))
            continue

        bars = fetch_bars(mc, symbol)
        if bars is None or len(bars) < 30:
            log.info(f"{symbol}: No data — skipped")
            scan_results.append(asdict_scan(symbol, "NO_DATA", "Insufficient bar data"))
            continue

        # Per-symbol guardrails
        sym_ok, sym_msgs = guardrails.run_all_guardrails(
            symbol=symbol, portfolio=portfolio, buying_power=buying_pw,
            session_start_value=portfolio, session_high=portfolio,
            trades_today=trades_today + placed,
            open_positions=open_positions, bars_df=bars,
        )
        if not sym_ok:
            block_msg = next((m for m in sym_msgs if "CIRCUIT" in m or "block" in m.lower() or "skip" in m.lower()), "Guardrail block")
            log.info(f"{symbol}: Blocked — {block_msg}")
            scan_results.append(asdict_scan(symbol, "BLOCKED", block_msg))
            run.guardrail_blocks += 1
            continue

        # Strategy
        try:
            sig = run_strategy(symbol, bars)
        except Exception as exc:
            log.error(f"{symbol}: Strategy error — {exc}", exc_info=True)
            scan_results.append(asdict_scan(symbol, "ERROR", str(exc)))
            continue

        indicators = (f"Price ${sig.price}  VWAP ${sig.vwap_val}  "
                      f"EMA9 {sig.ema9}  EMA21 {sig.ema21}  RSI {sig.rsi_val}  "
                      f"Vol {sig.volume:,}  ATR {sig.atr_val}")

        traded = False
        if sig.signal in ("LONG", "SHORT"):
            run.signals_found += 1
            log.info(f"{symbol}: ✓ {sig.signal} [{sig.strategy}]  —  {indicators}")
            log.info(f"{symbol}: {sig.reason}")
            success = place_trade(tc, sig, portfolio, capital_limit, run_id)
            if success:
                placed += 1
                traded = True
        else:
            log.info(f"{symbol}: — NONE [{sig.strategy}]  —  {indicators}")
            log.info(f"{symbol}: {sig.reason}")

        scan_results.append({
            "symbol":    symbol,
            "signal":    sig.signal,
            "strategy":  sig.strategy,
            "price":     sig.price,
            "rsi":       sig.rsi_val,
            "vwap":      sig.vwap_val,
            "ema9":      sig.ema9,
            "ema21":     sig.ema21,
            "volume":    sig.volume,
            "confirmed": sig.confirmed,
            "reason":    sig.reason,
            "traded":    traded,
        })
        time.sleep(0.5)

    # 9. Finalise run log
    run.trades_placed = placed
    run.scans         = scan_results
    run.status        = "OK"

    alerts.scan_summary(config.WATCHLIST, run.signals_found, placed, mode)
    log.info("="*60)
    log.info(f"  Complete: {placed} trades | {open_cnt+placed}/{config.MAX_OPEN_TRADES} open | signals={run.signals_found}")
    log.info("="*60)
    _save_and_exit(run, 0)


def asdict_scan(symbol, signal, reason):
    return {"symbol": symbol, "signal": signal, "strategy": "—",
            "price": 0, "rsi": 0, "vwap": 0, "ema9": 0, "ema21": 0,
            "volume": 0, "confirmed": False, "reason": reason, "traded": False}


if __name__ == "__main__":
    main()
