"""
Guardrails — all safety checks before any trade is placed.
Each check returns (ok: bool, reason: str).
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
import config

ET = ZoneInfo("America/New_York")
log = logging.getLogger("Guardrails")


def check_account_minimums(portfolio: float, buying_power: float) -> tuple[bool, str]:
    """Hard floor on account balance."""
    if portfolio < config.MIN_ACCOUNT_BALANCE:
        return False, (
            f"CIRCUIT BREAKER: Portfolio ${portfolio:,.2f} is below minimum "
            f"${config.MIN_ACCOUNT_BALANCE:,.2f}. All trading halted."
        )
    return True, f"Account balance OK (${portfolio:,.2f})"


def check_pdt_rule(portfolio: float) -> tuple[bool, str]:
    """Pattern Day Trader warning — live accounts only."""
    if config.PAPER:
        return True, "PDT check skipped (paper mode)"
    if portfolio < config.PDT_MINIMUM_EQUITY:
        return False, (
            f"PDT WARNING: Live portfolio ${portfolio:,.2f} < $25,000. "
            f"You are limited to 3 day trades per rolling 5-day window. "
            f"Bot will not trade to protect your account from PDT restrictions."
        )
    return True, f"PDT check OK (${portfolio:,.2f} ≥ $25,000)"


def check_daily_loss_limit(portfolio: float, session_start_value: float) -> tuple[bool, str]:
    """Stop trading if daily loss exceeds limit."""
    if session_start_value <= 0:
        return True, "No session baseline yet"
    loss_pct = (portfolio - session_start_value) / session_start_value
    limit    = -config.DAILY_LOSS_LIMIT_PCT
    if loss_pct < limit:
        return False, (
            f"CIRCUIT BREAKER: Daily loss {loss_pct*100:.2f}% exceeds limit "
            f"{limit*100:.2f}%. No new trades today."
        )
    return True, f"Daily P&L: {loss_pct*100:+.2f}% (limit {limit*100:.2f}%)"


def check_max_drawdown(portfolio: float, session_high: float) -> tuple[bool, str]:
    """Emergency stop on large drawdown from session high."""
    if session_high <= 0:
        return True, "No session high yet"
    dd = (portfolio - session_high) / session_high
    if dd < -config.MAX_DRAWDOWN_PCT:
        return False, (
            f"CIRCUIT BREAKER: Drawdown {dd*100:.2f}% from session high "
            f"${session_high:,.2f} exceeds -{config.MAX_DRAWDOWN_PCT*100:.0f}%. "
            f"Emergency halt."
        )
    return True, f"Drawdown OK ({dd*100:.2f}% from high)"


def check_daily_trade_count(trades_today: int) -> tuple[bool, str]:
    """Hard cap on daily trade count."""
    if trades_today >= config.MAX_TRADES_PER_DAY:
        return False, (
            f"Daily trade limit reached ({trades_today}/{config.MAX_TRADES_PER_DAY}). "
            f"No new trades until tomorrow."
        )
    return True, f"Trade count OK ({trades_today}/{config.MAX_TRADES_PER_DAY} today)"


def check_correlation(symbol: str, open_positions: list[str]) -> tuple[bool, str]:
    """Don't hold highly correlated stocks simultaneously."""
    for group in config.CORRELATED_SYMBOLS:
        if symbol in group:
            held = [s for s in open_positions if s in group]
            if held:
                return False, (
                    f"Correlation block: {symbol} is in same group as held "
                    f"position(s) {held}. Skipping to avoid overexposure."
                )
    return True, "Correlation OK"


def check_earnings_proximity(symbol: str, bars_df) -> tuple[bool, str]:
    """
    Heuristic: if today's volume is 3x+ above recent average,
    it may be an earnings day — skip to avoid news-driven whipsaw.
    """
    try:
        recent_avg = bars_df["volume"].rolling(10).mean().iloc[-1]
        today_vol  = bars_df["volume"].iloc[-1]
        if today_vol > recent_avg * 3:
            return False, (
                f"{symbol}: Volume {int(today_vol):,} is {today_vol/recent_avg:.1f}x "
                f"above average — possible earnings/news event. Skipping."
            )
    except Exception:
        pass
    return True, "Volume check OK"


def run_all_guardrails(
    symbol: str,
    portfolio: float,
    buying_power: float,
    session_start_value: float,
    session_high: float,
    trades_today: int,
    open_positions: list[str],
    bars_df=None,
) -> tuple[bool, list[str]]:
    """
    Run every guardrail. Returns (all_passed, list_of_messages).
    If any check fails, trading for this symbol is blocked.
    """
    checks = [
        check_account_minimums(portfolio, buying_power),
        check_pdt_rule(portfolio),
        check_daily_loss_limit(portfolio, session_start_value),
        check_max_drawdown(portfolio, session_high),
        check_daily_trade_count(trades_today),
        check_correlation(symbol, open_positions),
    ]
    if bars_df is not None:
        checks.append(check_earnings_proximity(symbol, bars_df))

    messages = []
    all_ok = True
    for ok, msg in checks:
        messages.append(msg)
        if not ok:
            all_ok = False
            log.warning(f"GUARDRAIL FAILED: {msg}")

    return all_ok, messages
