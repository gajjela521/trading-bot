# VWAP + EMA + RSI Automated Trading Bot — Documentation

A production-grade automated trading bot that runs on GitHub Actions (free),
scans your watchlist every 15 minutes during US market hours, and places
bracket orders on Alpaca when all strategy conditions align.

---

## Table of contents

1. [Architecture overview](#1-architecture-overview)
2. [Strategy rules](#2-strategy-rules)
3. [Risk management rules](#3-risk-management-rules)
4. [Entry and exit logic](#4-entry-and-exit-logic)
5. [Timing rules](#5-timing-rules)
6. [First-time setup](#6-first-time-setup)
7. [How to run manually](#7-how-to-run-manually)
8. [Live trading — capital confirmation](#8-live-trading--capital-confirmation)
9. [Trade records](#9-trade-records)
10. [How to monitor](#10-how-to-monitor)
11. [How to go live](#11-how-to-go-live)
12. [How to stop the bot](#12-how-to-stop-the-bot)
13. [How to update the watchlist](#13-how-to-update-the-watchlist)
14. [Tuning parameters](#14-tuning-parameters)
15. [Reading the log output](#15-reading-the-log-output)
16. [Free tier limits](#16-free-tier-limits)
17. [Disclaimer](#17-disclaimer)

---

## 1. Architecture overview

```
GitHub Actions (cron, every 15 min, Mon–Fri)
          │
          ▼
  Ubuntu runner spins up
  pip install (cached, ~10 sec)
  python vwap_ema_rsi_bot.py
          │
          ├─ Market closed? → log reason, exit immediately (no waiting)
          │
          ├─ Live mode? → verify LIVE_CAPITAL_LIMIT is set → log confirmation
          │
          ├─ Fetch 15-min bars for each stock in WATCHLIST
          │
          ├─ Compute VWAP / EMA 9 / EMA 21 / RSI 14 / ATR 14 / Volume
          │
          ├─ All 4 conditions met?
          │     YES → place entry + stop + T1 + T2 orders on Alpaca
          │           append record to trade_records.json
          │     NO  → log reason, move to next stock
          │
          └─ Upload strategy_log.txt + trade_records.json as artifacts
```

Each GitHub Actions run is a **single scan** — the bot does not loop or wait.
The cron schedule handles the 15-minute repeat. This means:
- No idle compute cost between scans
- Each run is fully stateless and isolated
- Market closed runs exit in under 5 seconds

---

## 2. Strategy rules

All **4 conditions** must be true simultaneously. Partial matches are skipped.

### Long (buy) signal

| # | Condition | Value | Meaning |
|---|-----------|-------|---------|
| 1 | Price vs VWAP | `price > VWAP` | Bullish side of institutional benchmark |
| 2 | EMA crossover | `EMA9 > EMA21` | Short-term trend above medium-term |
| 3 | RSI zone | `45 < RSI < 62` | Momentum rising, not yet overbought |
| 4 | Volume | `volume > 1.2× 20-bar avg` | Institutional participation confirmed |

### Short (sell) signal

| # | Condition | Value | Meaning |
|---|-----------|-------|---------|
| 1 | Price vs VWAP | `price < VWAP` | Bearish side of institutional benchmark |
| 2 | EMA crossover | `EMA9 < EMA21` | Short-term trend below medium-term |
| 3 | RSI zone | `38 < RSI < 55` | Momentum falling, not yet oversold |
| 4 | Volume | `volume > 1.2× 20-bar avg` | Selling pressure confirmed |

### Why the RSI 45–62 zone (not 30/70)?

Most traders use RSI at extremes. The edge here is the **middle zone**:
- RSI 45–62 for longs = momentum is rising but not exhausted → enter early, not late
- RSI 38–55 for shorts = momentum is falling but not yet oversold → get the move, not the bounce
- RSI above 65 on a long setup means the move has already happened → bot correctly skips it

### Why VWAP?

VWAP resets every trading day. It is the benchmark institutions use to measure their own execution. When price dips to VWAP in an uptrend, institutions step in to buy. Trading at VWAP puts you alongside smart money.

### Why EMA 9 / 21?

- EMA9 reacts quickly to recent price action
- EMA21 reflects the established short-term trend
- When EMA9 crosses above EMA21, an uptrend is confirmed — not a noise spike
- The crossing filters out single-candle fakeouts

---

## 3. Risk management rules

Every trade follows these rules automatically. They cannot be bypassed.

| Rule | Value | Reason |
|------|-------|--------|
| Max risk per trade | 1.5% of portfolio | 10 straight losses still leaves 85% of capital |
| Stop loss | 1 ATR from entry | ATR sizes stop to actual recent volatility |
| Target 1 | 2× stop distance, 50% of position | Lock in profit early |
| Target 2 | 3× stop distance, remaining 50% | Let winners run |
| Max simultaneous trades | 3 | Limits correlated market exposure |
| Max single position | 20% of portfolio | No single stock dominates |
| Live capital limit | Set by you per run | Hard ceiling — bot aborts if unset in live mode |

### Position size formula

```
risk_dollars   = portfolio_value × 1.5%
stop_distance  = 1 × ATR(14)
raw_shares     = risk_dollars ÷ stop_distance
cap_by_pct     = (portfolio × 20%) ÷ price
cap_by_limit   = LIVE_CAPITAL_LIMIT ÷ price   (live mode only)
final_shares   = min(raw_shares, cap_by_pct, cap_by_limit)
```

**Example** — $50,000 portfolio, stock at $100, ATR = $3, limit = $5,000:
```
risk_dollars  = $750
raw_shares    = 250
cap_by_pct    = 100 shares  ($10,000 cap)
cap_by_limit  = 50 shares   ($5,000 cap)
final_shares  = 50
position_val  = $5,000
max_loss      = 50 × $3 = $150
```

---

## 4. Entry and exit logic

### Order sequence (all placed within seconds of signal)

```
1. Market order  →  Entry at current price
2. Stop order    →  Stop loss at 1 ATR from entry
3. Limit order   →  Target 1 at 2× risk  (50% of shares)
4. Limit order   →  Target 2 at 3× risk  (remaining shares)
```

### Long trade example

```
Stock : NBIS   |   Price : $255   |   ATR : $8   |   Qty : 45 shares

Entry   $255.00  (immediate, market)
Stop    $247.00  (= 255 − 8)          → max loss if hit: $360
T1      $271.00  (= 255 + 8×2)        → profit on 22 shares: $352  ✓
T2      $279.00  (= 255 + 8×3)        → profit on 23 shares: $552  ✓
```

### Short trade example

```
Stock : TSLA   |   Price : $180   |   ATR : $5   |   Qty : 60 shares

Entry   $180.00  (immediate, market short)
Stop    $185.00  (= 180 + 5)          → max loss if hit: $300
T1      $170.00  (= 180 − 5×2)        → profit on 30 shares: $300  ✓
T2      $165.00  (= 180 − 5×3)        → profit on 30 shares: $450  ✓
```

### After Target 1 hits — manual step recommended

The bot sets all orders at trade entry. After T1 fills, **manually move your
stop to breakeven** in the Alpaca UI to protect against a winner turning loser:

1. Log in to app.alpaca.markets
2. Orders → find your open stop order for the symbol
3. Modify stop price to your original entry price

---

## 5. Timing rules

| Rule | Value | Reason |
|------|-------|--------|
| Opening buffer | Skip first 15 min (9:30–9:45 AM ET) | Gap volatility settles |
| Closing buffer | Stop 30 min before close (after 3:30 PM ET) | Erratic end-of-day moves |
| Schedule | Mon–Fri only | US market days |
| Pre/after market | Never | VWAP meaningless outside regular hours |
| Market closed | Exit immediately with clear message | No spinning, no waiting |

If the bot runs and the market is closed, you will see exactly one log line:

```
Market status: Market closed — next open: Mon Jun 08 09:30 AM ET
Nothing to do — exiting cleanly.
```

Then it exits. No looping, no sleeping, no wasted compute.

---

## 6. First-time setup

### Prerequisites

- GitHub account (free) — your repo is already set up
- Alpaca account (free) — paper or live at app.alpaca.markets

### Step 1 — Add GitHub Secrets

> Alpaca issues **two completely separate key pairs** — one for Paper, one for Live.
> They are different keys and must never be mixed up.

**Where to get your keys:**

| Account | URL |
|---------|-----|
| Paper keys | app.alpaca.markets → toggle to **Paper** (top-left) → API Keys |
| Live keys | app.alpaca.markets → toggle to **Live** → API Keys |

**Add secrets to GitHub:**

Go to: your repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

| Secret name | What to paste | When needed |
|---|---|---|
| `ALPACA_PAPER_API_KEY` | Paper API Key ID | Now (paper trading) |
| `ALPACA_PAPER_API_SECRET` | Paper Secret Key | Now (paper trading) |
| `ALPACA_LIVE_API_KEY` | Live API Key ID | Only when going live |
| `ALPACA_LIVE_API_SECRET` | Live Secret Key | Only when going live |

**How the bot picks keys:**

```
PAPER_MODE=true   →  uses ALPACA_PAPER_API_KEY + ALPACA_PAPER_API_SECRET
                      connects to https://paper-api.alpaca.markets

PAPER_MODE=false  →  uses ALPACA_LIVE_API_KEY  + ALPACA_LIVE_API_SECRET
                      connects to https://api.alpaca.markets
```

### Step 2 — Automatic scheduling is live

The workflow file is already in the repo. GitHub Actions will run the bot
automatically every 15 minutes on trading days. Nothing else to do.

---

## 7. How to run manually

Useful for testing or forcing a scan outside the cron schedule:

1. Go to your repo → **Actions** tab
2. Click **VWAP EMA RSI Trading Bot** in the left sidebar
3. Click **Run workflow** (top-right of the table)
4. Fill in the inputs:

| Input | Description | Example |
|-------|-------------|---------|
| `paper_mode` | `true` = paper, `false` = live | `true` |
| `live_capital_limit` | Max USD to use (live mode only) | `1000` |

5. Click the green **Run workflow** button
6. Click the run as it appears → click **run-bot** → watch live log

---

## 8. Live trading — capital confirmation

When `PAPER_MODE=false`, the bot runs a capital confirmation check **before
scanning any stocks**. It will log:

```
================================================================
  LIVE TRADING MODE — REAL MONEY WILL BE USED
================================================================
  Portfolio value   : $52,430.00
  Capital limit set : $5,000.00
  Max risk / trade  : $786.45  (1.5%)
  Max open trades   : 3
  Max total exposure: $31,458.00
================================================================
Capital confirmation passed — proceeding with limit $5,000.00.
```

The bot **aborts with an error** (no trades placed) if:
- `LIVE_CAPITAL_LIMIT` is not set or is zero
- The limit exceeds the portfolio value
- The limit is less than a single trade's risk amount

You set `live_capital_limit` in the **Run workflow** input each time you
trigger a live run manually. For scheduled live runs, set it as a GitHub
Secret named `LIVE_CAPITAL_LIMIT`.

---

## 9. Trade records

Every executed trade (and every failed attempt) is written to `trade_records.json`.

### What is stored per trade

```json
{
  "timestamp":       "2026-06-02T10:15:03-04:00",
  "run_id":          "1234567890",
  "mode":            "PAPER",
  "symbol":          "NBIS",
  "direction":       "LONG",
  "entry_price":     255.0,
  "qty":             45,
  "stop_price":      247.0,
  "target1_price":   271.0,
  "target2_price":   279.0,
  "risk_dollars":    360.0,
  "portfolio_value": 52430.0,
  "entry_order_id":  "abc123...",
  "stop_order_id":   "def456...",
  "t1_order_id":     "ghi789...",
  "t2_order_id":     "jkl012...",
  "signal_reason":   "Price 255.0 > VWAP 248.12  |  EMA9 261.4 > EMA21 244.8  |  RSI 52.3 in [45–62]  |  Vol 1,823,400 > 1,512,000",
  "status":          "OPEN"
}
```

### Retention policy

- Records older than **7 days are automatically purged** at the start of every run
- Only the current rolling week is kept
- Purge is atomic — the file is never left in a corrupt state
- Failed order attempts are also recorded (status = "ERROR: ...") for audit purposes

### Accessing the records

**In GitHub Actions artifacts** — every run uploads both files:
- `strategy_log.txt` — full human-readable log
- `trade_records.json` — structured trade data

Repo → Actions → click any run → scroll to **Artifacts** → download

Records are kept as artifacts for **30 days**.

### Weekly summary in log

Every run prints a summary of the week's records:

```
Trade records (last 7 days): 12 total  |  LONG=8  SHORT=4  |  PAPER=12  LIVE=0
```

---

## 10. How to monitor

### Live log during a run

Repo → **Actions** → click the running job → click **run-bot** → live output streams here

### Sample healthy scan output

```
================================================================
  VWAP + EMA + RSI Trading Bot  —  PAPER mode
  Endpoint  : https://paper-api.alpaca.markets
  Run ID    : 9876543210
  Watchlist : ['NBIS', 'NVDA', 'TSLA', 'AAPL', 'MSFT']
================================================================
Market status: Market open — 10:15 AM ET
Account  →  Portfolio: $52,430.00  |  Buying power: $48,100.00
Open positions: 1 / 3
Trade records (last 7 days): 4 total  |  LONG=3  SHORT=1
--- Scanning 5 stocks ---
NBIS: ✓ LONG  —  Price $255.0  |  VWAP $248.12  |  RSI 52.3  |  Vol 1,823,400
NBIS: Reason → Price 255.0 > VWAP 248.12  |  EMA9 261.4 > EMA21 244.8  |  RSI 52.3 in [45–62]
================================================================
  SIGNAL  NBIS  LONG  (PAPER)
  Entry : $255.0   Qty : 45 shares
  Stop  : $247.0   Risk : $360.0
  T1    : $271.0  (22 shares — 50 %)
  T2    : $279.0  (23 shares — remaining)
================================================================
NVDA: — NONE  —  Price $890.0  |  VWAP $875.0  |  RSI 67.1  |  Vol 5,200,000
NVDA: No signal → RSI 67.1 outside zone
================================================================
  Scan complete  |  Trades placed this run: 1
  Total open positions: 2 / 3
================================================================
```

### Sample market-closed output

```
Market status: Market closed — next open: Mon Jun 08 09:30 AM ET
Nothing to do — exiting cleanly.
Trade records (last 7 days): 4 total  |  LONG=3  SHORT=1
```

---

## 11. How to go live

**Do not go live until you have paper traded for at least 10 trading days.**

### Checklist

- [ ] Paper traded ≥ 10 days
- [ ] Reviewed every trade in the log — understand why each signal fired
- [ ] Win rate ≥ 55% on paper results
- [ ] Comfortable with position sizes
- [ ] Alpaca live account funded
- [ ] Live API keys added to GitHub Secrets

### Steps

1. Add live keys to GitHub Secrets (`ALPACA_LIVE_API_KEY`, `ALPACA_LIVE_API_SECRET`)
2. For **manual live runs**: trigger workflow → set `paper_mode = false`, set `live_capital_limit`
3. For **scheduled live runs**: add a GitHub Secret `LIVE_CAPITAL_LIMIT` = your dollar limit, and change the workflow default:

```yaml
# In .github/workflows/trade.yml
default: 'false'   # was 'true'
```

4. Confirm the first live run log shows:
```
Strategy bot starting — LIVE mode
Endpoint : https://api.alpaca.markets
```

---

## 12. How to stop the bot

| Action | How |
|--------|-----|
| Pause (temporary) | Repo → Actions → left sidebar → bot name → ··· → **Disable workflow** |
| Resume | Same menu → **Enable workflow** |
| Cancel one run | Repo → Actions → click running job → **Cancel workflow** |
| Stop permanently | Delete `.github/workflows/trade.yml` from the repo |
| Close positions now | Log in to Alpaca → Portfolio → Close positions manually |

---

## 13. How to update the watchlist

Open `vwap_ema_rsi_bot.py` in GitHub (click the file → pencil icon).

Find line ~60:
```python
WATCHLIST: list[str] = ["NBIS", "NVDA", "TSLA", "AAPL", "MSFT"]
```

Edit, commit — the next scheduled run picks it up automatically.

**Tips:**
- Use only US stock tickers (Alpaca US equities)
- Minimum 1M average daily volume for reliable fills
- Avoid stocks under $10 (wide spreads, erratic VWAP)
- Remove a stock on its earnings day — news overrides technicals
- 5–10 stocks is the sweet spot

---

## 14. Tuning parameters

All values are at the top of `vwap_ema_rsi_bot.py`. Edit and push — live on next run.

| Parameter | Default | Increasing → | Decreasing → |
|-----------|---------|-------------|-------------|
| `RSI_LONG_LOW` | 45 | Fewer long signals | More long signals |
| `RSI_LONG_HIGH` | 62 | More long signals | Fewer long signals |
| `RSI_SHORT_LOW` | 38 | More short signals | Fewer short signals |
| `RSI_SHORT_HIGH` | 55 | Fewer short signals | More short signals |
| `VOLUME_MULT` | 1.2 | Higher bar, fewer signals | Lower bar, more signals |
| `MAX_RISK_PCT` | 0.015 | Larger positions | Smaller positions |
| `TARGET1_MULT` | 2.0 | T1 harder to reach | T1 easier to reach |
| `TARGET2_MULT` | 3.0 | T2 harder to reach | T2 easier to reach |
| `MAX_OPEN_TRADES` | 3 | More concurrent trades | Fewer concurrent trades |
| `MARKET_OPEN_BUFFER_MIN` | 15 | Longer wait after open | Shorter wait |
| `MARKET_CLOSE_BUFFER_MIN` | 30 | Stop earlier | Stop later |

**Rule: do not change any parameter until you have 20+ paper trades of data.**

---

## 15. Reading the log output

| Log text | Meaning |
|----------|---------|
| `PAPER mode` / `LIVE mode` | Confirms which account type is active |
| `Endpoint: https://paper-api...` | Paper keys and URL confirmed |
| `Endpoint: https://api.alpaca...` | Live keys and URL confirmed |
| `Market closed — next open: ...` | Outside market hours — nothing done |
| `Opening buffer active` | Within first 15 min of open — waiting |
| `Closing buffer active` | Within last 30 min — no new trades |
| `✓ LONG` / `✓ SHORT` | Signal confirmed — trade being placed |
| `— NONE` | No signal — reason shown on next line |
| `Already open — skipped` | Position already exists for this stock |
| `Max trade limit reached` | 3 trades already open — scan stops |
| `Trade record saved` | Record written to trade_records.json |
| `Capital confirmation passed` | Live mode check passed — safe to trade |
| `CONFIG ERROR` | Missing key or bad config — bot aborts |
| `Order placement failed` | Alpaca rejected the order — see error detail |

---

## 16. Free tier limits

| Resource | Free allowance | This bot uses | Status |
|----------|---------------|---------------|--------|
| GitHub Actions minutes | 2,000 / month | ~1,100 / month | Safe ✓ |
| GitHub Secrets | Unlimited | 4 secrets | Safe ✓ |
| GitHub Artifacts storage | 500 MB | < 1 MB / run | Safe ✓ |
| Artifact retention | 30 days | 30 days set | Safe ✓ |
| Alpaca paper trading | Free forever | — | Safe ✓ |
| Alpaca live trading | Free (no commission) | — | Safe ✓ |

Calculation: 2 min/run × 26 runs/day × 21 trading days = **1,092 min/month**

---

## 17. Disclaimer

This software is provided for educational and personal use only. Automated
trading carries significant financial risk. Past performance of a strategy
does not guarantee future results. Always paper trade before using real money.
You are solely responsible for all trades placed by this bot on your account.
The authors accept no liability for financial losses of any kind.

**Never risk money you cannot afford to lose.**
