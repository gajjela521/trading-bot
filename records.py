"""
Persistent records — two separate files committed to the repo on every run.

trade_records.json  — one entry per executed trade (filled on signal)
run_log.json        — one entry per bot run (filled always, even market closed)
"""
import json
import logging
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import List
from zoneinfo import ZoneInfo
import config

ET  = ZoneInfo("America/New_York")
log = logging.getLogger("Records")

TRADE_FILE = Path("trade_records.json")
RUN_FILE   = Path("run_log.json")


# ─────────────────────────────────────────────────────────────────────────────
# Data models
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TradeRecord:
    timestamp:       str
    run_id:          str
    mode:            str
    symbol:          str
    strategy:        str
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
    confirmed:       bool
    orb_high:        float = 0.0
    orb_low:         float = 0.0
    status:          str   = "OPEN"


@dataclass
class ScanResult:
    """One row per stock scanned in a run."""
    symbol:    str
    signal:    str       # LONG | SHORT | NONE
    strategy:  str
    price:     float
    rsi:       float
    vwap:      float
    ema9:      float
    ema21:     float
    volume:    int
    confirmed: bool
    reason:    str
    traded:    bool      # True if an order was actually placed


@dataclass
class RunLog:
    """Written on EVERY bot run, regardless of outcome."""
    timestamp:        str          # ISO-8601 ET
    run_id:           str
    mode:             str          # PAPER | LIVE
    status:           str          # OK | MARKET_CLOSED | WEEKEND | ERROR | MAX_TRADES
    market_open:      bool
    timing_reason:    str          # human-readable timing message
    portfolio_value:  float        # 0 if could not fetch
    buying_power:     float
    capital_limit:    float
    open_positions:   int
    trades_placed:    int
    signals_found:    int
    guardrail_blocks: int
    watchlist:        List[str]    = field(default_factory=list)
    scans:            List[dict]   = field(default_factory=list)
    error_detail:     str          = ""


# ─────────────────────────────────────────────────────────────────────────────
# Gist helpers (optional — also writes locally for repo commit)
# ─────────────────────────────────────────────────────────────────────────────

def _headers():
    return {
        "Authorization": f"token {config.GIST_TOKEN}",
        "Accept":        "application/vnd.github.v3+json",
        "Content-Type":  "application/json",
        "User-Agent":    "trading-bot/4.0",
    }


def _gist_read(filename: str) -> list:
    if not (config.GIST_TOKEN and config.GIST_ID):
        return []
    try:
        req = urllib.request.Request(
            f"https://api.github.com/gists/{config.GIST_ID}",
            headers=_headers(),
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        content = data["files"].get(filename, {}).get("content", "[]")
        return json.loads(content)
    except Exception as e:
        log.warning(f"Gist read {filename} failed: {e}")
        return []


def _gist_write(files: dict) -> None:
    if not (config.GIST_TOKEN and config.GIST_ID):
        return
    try:
        payload = json.dumps({"files": {
            k: {"content": json.dumps(v, indent=2)} for k, v in files.items()
        }}).encode()
        req = urllib.request.Request(
            f"https://api.github.com/gists/{config.GIST_ID}",
            data=payload, headers=_headers(), method="PATCH",
        )
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        log.warning(f"Gist write failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Local atomic write
# ─────────────────────────────────────────────────────────────────────────────

def _write_local(path: Path, data: list) -> None:
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:
        log.error(f"Local write {path} failed: {e}")
        tmp.unlink(missing_ok=True)


def _read_local(path: Path) -> list:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


# ─────────────────────────────────────────────────────────────────────────────
# Purge
# ─────────────────────────────────────────────────────────────────────────────

def purge_old(records: list, days: int = None) -> list:
    days   = days or config.RETENTION_DAYS
    cutoff = datetime.now(tz=ET) - timedelta(days=days)
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
        log.info(f"Purged {dropped} old record(s) (>{days}d)")
    return kept


# ─────────────────────────────────────────────────────────────────────────────
# Trade records
# ─────────────────────────────────────────────────────────────────────────────

def load_trades() -> list:
    records = _read_local(TRADE_FILE)
    if not records:
        records = _gist_read("trade_records.json")
    return purge_old(records)


def append_trade(record: TradeRecord) -> None:
    records = load_trades()
    records.append(asdict(record))
    _write_local(TRADE_FILE, records)
    log.info(f"Trade record saved: {record.symbol} {record.direction} @ ${record.entry_price}")


# ─────────────────────────────────────────────────────────────────────────────
# Run log
# ─────────────────────────────────────────────────────────────────────────────

def load_runs() -> list:
    records = _read_local(RUN_FILE)
    if not records:
        records = _gist_read("run_log.json")
    return purge_old(records, days=7)


def save_run(run: RunLog) -> None:
    """Always called — even when market is closed or bot errors."""
    runs = load_runs()
    runs.append(asdict(run))
    _write_local(RUN_FILE, runs)

    # Also update trades file in same call for Gist efficiency
    trades = _read_local(TRADE_FILE) or []
    _gist_write({
        "run_log.json":         runs,
        "trade_records.json":   trades,
    })
    log.info(f"Run log saved: {run.status} | trades={run.trades_placed} | signals={run.signals_found}")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers used by bot.py
# ─────────────────────────────────────────────────────────────────────────────

def daily_trade_count(records: list) -> int:
    today = datetime.now(ET).date()
    return sum(
        1 for r in records
        if str(r.get("status","")).upper() != "ERROR"
        and _record_date(r) == today
    )


def _record_date(r: dict):
    try:
        ts = datetime.fromisoformat(r["timestamp"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=ET)
        return ts.astimezone(ET).date()
    except Exception:
        return None


def summary(records: list) -> str:
    longs  = sum(1 for r in records if r.get("direction") == "LONG")
    shorts = sum(1 for r in records if r.get("direction") == "SHORT")
    paper  = sum(1 for r in records if r.get("mode")      == "PAPER")
    live   = sum(1 for r in records if r.get("mode")      == "LIVE")
    risk   = sum(r.get("risk_dollars", 0) for r in records)
    today  = daily_trade_count(records)
    return (
        f"7-day: {len(records)} trades | LONG={longs} SHORT={shorts} | "
        f"PAPER={paper} LIVE={live} | Risk=${risk:,.2f} | Today={today}"
    )
