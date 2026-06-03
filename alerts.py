"""
Telegram alert system.
Sends a message to your phone on every trade and circuit breaker event.
"""
import json
import logging
import urllib.request
import urllib.parse
import config

log = logging.getLogger("Alerts")


def _send(text: str) -> bool:
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        log.info(f"Telegram not configured — alert skipped: {text[:60]}")
        return False
    try:
        payload = json.dumps({
            "chat_id":    config.TELEGRAM_CHAT_ID,
            "text":       text,
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=8)
        return True
    except Exception as exc:
        log.warning(f"Telegram send failed: {exc}")
        return False


def trade_entered(symbol, direction, price, qty, stop, t1, t2, strategy, mode):
    emoji = "🟢" if direction == "LONG" else "🔴"
    _send(
        f"{emoji} <b>TRADE ENTERED</b> [{mode}]\n"
        f"<b>{symbol}</b> {direction} via {strategy}\n"
        f"Entry  : <code>${price:.2f}</code>  ×{qty} shares\n"
        f"Stop   : <code>${stop:.2f}</code>\n"
        f"T1     : <code>${t1:.2f}</code>  (50%)\n"
        f"T2     : <code>${t2:.2f}</code>  (50%)\n"
        f"Risk   : <code>${abs(price-stop)*qty:.2f}</code>"
    )


def circuit_breaker(reason: str):
    _send(f"🚨 <b>CIRCUIT BREAKER TRIGGERED</b>\n{reason}")


def scan_summary(watchlist, signals_found, trades_placed, mode):
    _send(
        f"📊 <b>SCAN COMPLETE</b> [{mode}]\n"
        f"Scanned  : {', '.join(watchlist)}\n"
        f"Signals  : {signals_found}\n"
        f"Trades   : {trades_placed} placed\n"
    )


def market_closed(reason: str):
    _send(f"⏰ <b>BOT RAN</b>\n{reason}")
