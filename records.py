"""
Persistent trade records via GitHub Gist with local file fallback.
Retains 7 days of data. Atomic writes only.
"""
import json
import logging
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import config

ET  = ZoneInfo("America/New_York")
log = logging.getLogger("Records")
LOCAL = Path("trade_records.json")


@dataclass
class TradeRecord:
    timestamp:       str
    run_id:          str
    mode:            str
    symbol:          str
    strategy:        str    # VWAP | ORB
    direction:       str    # LONG | SHORT
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


def _headers():
    return {
        "Authorization": f"token {config.GIST_TOKEN}",
        "Accept":        "application/vnd.github.v3+json",
        "Content-Type":  "application/json",
        "User-Agent":    "trading-bot/4.0",
    }


def load() -> list[dict]:
    if config.GIST_TOKEN and config.GIST_ID:
        try:
            req = urllib.request.Request(
                f"https://api.github.com/gists/{config.GIST_ID}",
                headers=_headers(),
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
            content = data["files"].get(config.GIST_FILENAME, {}).get("content", "[]")
            records = json.loads(content)
            log.info(f"Gist: loaded {len(records)} records")
            return records
        except Exception as e:
            log.warning(f"Gist read failed ({e}) — using local")
    if LOCAL.exists():
        try:
            return json.loads(LOCAL.read_text())
        except Exception:
            pass
    return []


def save(records: list[dict]) -> None:
    # Local atomic write
    tmp = LOCAL.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(records, indent=2))
        tmp.replace(LOCAL)
    except Exception as e:
        log.error(f"Local write failed: {e}")
    # Gist write
    if config.GIST_TOKEN and config.GIST_ID:
        try:
            payload = json.dumps({
                "files": {
                    config.GIST_FILENAME: {"content": json.dumps(records, indent=2)}
                }
            }).encode()
            req = urllib.request.Request(
                f"https://api.github.com/gists/{config.GIST_ID}",
                data=payload, headers=_headers(), method="PATCH",
            )
            urllib.request.urlopen(req, timeout=10).read()
            log.info(f"Gist: saved {len(records)} records")
        except Exception as e:
            log.warning(f"Gist write failed: {e}")


def purge_old(records: list[dict]) -> list[dict]:
    cutoff = datetime.now(tz=ET) - timedelta(days=config.RETENTION_DAYS)
    kept, dropped = [], 0
    for r in records:
        try:
            ts = datetime.fromisoformat(r["timestamp"])
            if ts.tzinfo is None: ts = ts.replace(tzinfo=ET)
            if ts >= cutoff: kept.append(r)
            else: dropped += 1
        except Exception:
            kept.append(r)
    if dropped:
        log.info(f"Purged {dropped} records older than {config.RETENTION_DAYS}d")
    return kept


def append(record: TradeRecord) -> None:
    records = purge_old(load())
    records.append(asdict(record))
    save(records)
    log.info(f"Record saved: {record.symbol} {record.direction} @ ${record.entry_price}")


def daily_trade_count(records: list[dict]) -> int:
    today = datetime.now(ET).date()
    return sum(
        1 for r in records
        if r.get("status", "").upper() != "ERROR"
        and _record_date(r) == today
    )


def _record_date(r: dict):
    try:
        ts = datetime.fromisoformat(r["timestamp"])
        if ts.tzinfo is None: ts = ts.replace(tzinfo=ET)
        return ts.astimezone(ET).date()
    except Exception:
        return None


def summary(records: list[dict]) -> str:
    longs   = sum(1 for r in records if r.get("direction") == "LONG")
    shorts  = sum(1 for r in records if r.get("direction") == "SHORT")
    paper   = sum(1 for r in records if r.get("mode")      == "PAPER")
    live    = sum(1 for r in records if r.get("mode")      == "LIVE")
    vwap    = sum(1 for r in records if r.get("strategy")  == "VWAP")
    orb     = sum(1 for r in records if r.get("strategy")  == "ORB")
    errors  = sum(1 for r in records if "ERROR" in str(r.get("status","")))
    risk    = sum(r.get("risk_dollars", 0) for r in records)
    today   = daily_trade_count(records)
    return (
        f"7-day records: {len(records)} total | "
        f"LONG={longs} SHORT={shorts} | "
        f"VWAP={vwap} ORB={orb} | "
        f"PAPER={paper} LIVE={live} | "
        f"Errors={errors} | "
        f"Risk deployed=${risk:,.2f} | "
        f"Today={today}/{config.MAX_TRADES_PER_DAY}"
    )
