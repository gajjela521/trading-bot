# VWAP + EMA + RSI Automated Trading Bot

A fully automated short-term trading bot that runs on GitHub Actions (free), scans your watchlist every 15 minutes during US market hours, and places orders on your Alpaca account when all strategy conditions align.

---

## Table of contents

1. [How it works](#1-how-it-works)
2. [Strategy rules](#2-strategy-rules)
3. [Risk management rules](#3-risk-management-rules)
4. [Entry and exit logic](#4-entry-and-exit-logic)
5. [Timing rules](#5-timing-rules)
6. [First-time setup](#6-first-time-setup)
7. [How to add or remove stocks](#7-how-to-add-or-remove-stocks)
8. [How to monitor trades](#8-how-to-monitor-trades)
9. [How to go live](#9-how-to-go-live)
10. [How to stop the bot](#10-how-to-stop-the-bot)
11. [Tuning the parameters](#11-tuning-the-parameters)
12. [Understanding the log output](#12-understanding-the-log-output)
13. [Free tier limits](#13-free-tier-limits)
14. [Disclaimer](#14-disclaimer)

---

## 1. How it works

```
Every 15 minutes (Mon–Fri, 9:45 AM – 3:30 PM ET)
        ↓
GitHub Actions wakes up an Ubuntu runner
        ↓
Installs dependencies (cached, ~10 sec)
        ↓
Bot fetches 15-minute bars for each stock in WATCHLIST
        ↓
Computes VWAP, EMA 9, EMA 21, RSI 14, ATR 14, Volume
        ↓
Checks all 4 signal conditions
        ↓
Signal found?  YES → places 3 orders (entry + stop + targets)
               NO  → logs reason, exits cleanly
        ↓
Full log saved as GitHub Actions artifact (30 days)
```

Each GitHub Actions run is independent. The bot does one complete scan per trigger — it does not run continuously between scans.

---

## 2. Strategy rules

The strategy requires **all 4 conditions** to be true at the same time before placing any trade. One or two conditions alone are not enough — this is what makes it high-probability.

### Long (buy) signal — all 4 must be true

| # | Condition | What it means |
|---|-----------|---------------|
| 1 | `price > VWAP` | Stock is trading above the institutional benchmark — bulls in control |
| 2 | `EMA 9 > EMA 21` | Short-term momentum is above medium-term trend — uptrend confirmed |
| 3 | `45 < RSI < 62` | Momentum is building but not yet overbought — still room to run |
| 4 | `volume > 1.2× 20-bar average` | Institutional participation confirmed — not a weak, low-conviction move |

### Short (sell) signal — all 4 must be true

| # | Condition | What it means |
|---|-----------|---------------|
| 1 | `price < VWAP` | Stock is trading below the institutional benchmark — bears in control |
| 2 | `EMA 9 < EMA 21` | Short-term momentum is below medium-term trend — downtrend confirmed |
| 3 | `38 < RSI < 55` | Downward momentum building but not yet oversold — still room to fall |
| 4 | `volume > 1.2× 20-bar average` | Selling pressure is real and institutional — not a noise move |

### Why these specific RSI ranges?

Most traders use RSI at extremes (buy at 30, sell at 70). The edge here is the **middle zone**:
- RSI 45–62 for longs = momentum is rising but not exhausted. You are entering early, not chasing.
- RSI 38–55 for shorts = momentum is falling but not yet oversold. You get the move, not the bounce.
- If RSI is already above 65 on a long setup, the move has largely happened — the bot correctly skips it.

### Why VWAP matters

VWAP (Volume Weighted Average Price) resets every trading day at market open. It is the benchmark that institutional traders (mutual funds, hedge funds, market makers) use to measure their own execution quality. When price dips to VWAP in an uptrend, institutions buy. When price rallies to VWAP in a downtrend, institutions sell. Trading at VWAP puts you alongside smart money, not against it.

### Why EMA 9 / EMA 21

- EMA 9 = fast-moving, reacts quickly to recent price action
- EMA 21 = slower, reflects the established short-term trend
- When EMA 9 crosses above EMA 21, the trend has shifted up — confirmed uptrend
- When EMA 9 crosses below EMA 21, the trend has shifted down — confirmed downtrend
- Using both together filters out random single-candle spikes

---

## 3. Risk management rules

These rules are hard-coded and cannot be bypassed. Every single trade follows them automatically.

| Rule | Value | Why |
|------|-------|-----|
| Max risk per trade | **1.5% of portfolio** | A losing streak of 10 trades in a row still leaves 85% of capital intact |
| Stop loss | **1 ATR from entry** | ATR (Average True Range) sizes the stop to actual recent volatility — not arbitrary |
| Target 1 | **2× stop distance** | Minimum 1:2 risk-reward. Even at 40% win rate you break even |
| Target 2 | **3× stop distance** | Lets winners run on strong trends |
| Position split at T1 | **50% closed at T1** | Locks in profit while keeping half the position running |
| Max open trades | **3 simultaneous** | Prevents over-exposure to correlated market moves |
| Max position size | **20% of portfolio** | No single stock can dominate the account |

### How position size is calculated

```
Risk dollars   = Portfolio value × 1.5%
Stop distance  = 1 × ATR (14-period)
Shares         = Risk dollars ÷ Stop distance
Final shares   = min(calculated shares, 20% of portfolio ÷ price)
```

**Example:** $50,000 portfolio, stock at $100, ATR = $3.00
- Risk dollars = $50,000 × 1.5% = $750
- Shares = $750 ÷ $3.00 = 250 shares
- Position value = 250 × $100 = $25,000 (50% of portfolio — but capped at 20% = $10,000 = 100 shares)
- Final: 100 shares

---

## 4. Entry and exit logic

### Order sequence (placed automatically on signal)

```
1. Market order  →  Entry at current price (immediate fill)
2. Stop order    →  Stop loss at 1 ATR away (protects capital)
3. Limit order   →  Target 1 at 2× risk (closes 50% of position)
4. Limit order   →  Target 2 at 3× risk (closes remaining 50%)
```

All 4 orders are placed within seconds of each other.

### Long trade example

```
Stock:    NBIS
Price:    $255.00
ATR:      $8.00
Shares:   45

Entry:    $255.00  (market order, immediate)
Stop:     $247.00  (= 255 - 8 × 1.0)   → loss if hit: $360
Target 1: $271.00  (= 255 + 8 × 2.0)   → profit on 22 shares: $352  ✓
Target 2: $279.00  (= 255 + 8 × 3.0)   → profit on 23 shares: $552  ✓
```

### Short trade example

```
Stock:    TSLA
Price:    $180.00
ATR:      $5.00
Shares:   60

Entry:    $180.00  (market order, short)
Stop:     $185.00  (= 180 + 5 × 1.0)   → loss if hit: $300
Target 1: $170.00  (= 180 - 5 × 2.0)   → profit on 30 shares: $300  ✓
Target 2: $165.00  (= 180 - 5 × 3.0)   → profit on 30 shares: $450  ✓
```

### After Target 1 hits

The bot places the stop and target orders at trade entry, but does **not** automatically move the stop to breakeven after T1 fills. This is a manual action you should do yourself in Alpaca:

1. Log in to app.alpaca.markets
2. Go to Orders → find your open stop order
3. Modify the stop price to your entry price (breakeven)

This protects you from a winning trade turning into a loser.

---

## 5. Timing rules

| Rule | Value | Reason |
|------|-------|--------|
| Market open buffer | First 15 min skipped (9:30–9:45 AM ET) | Opening gap and noise settle down |
| Market close buffer | Last 30 min skipped (3:30–4:00 PM ET) | Wide spreads, erratic moves near close |
| Trading days | Monday–Friday only | US market schedule |
| Pre-market / after-hours | Never trades | VWAP is meaningless outside regular hours |
| Scan frequency | Every 15 minutes | Matches the 15-minute candle timeframe used for signals |

The bot checks market timing at the start of every run. If it is outside the trading window, it logs the reason and exits immediately without scanning.

---

## 6. First-time setup

### Prerequisites
- GitHub account (free)
- Alpaca account (free) — paper or live at app.alpaca.markets

### Step 1 — Repo is already set up

Your repo is live at: **https://github.com/gajjela521/trading-bot**

It contains:
```
trading-bot/
├── .github/
│   └── workflows/
│       └── trade.yml          ← GitHub Actions scheduler
├── vwap_ema_rsi_bot.py        ← Strategy bot
├── requirements.txt           ← Python dependencies
├── SETUP_GUIDE.md             ← Quick start
└── DOCUMENTATION.md           ← This file
```

### Step 2 — Add Alpaca API keys as GitHub Secrets

1. Go to your repo → **Settings** → **Secrets and variables** → **Actions**
2. Click **New repository secret** — add both:

| Secret name | Value |
|-------------|-------|
| `ALPACA_API_KEY` | Your Alpaca API key |
| `ALPACA_API_SECRET` | Your Alpaca API secret |

Get your keys at: https://app.alpaca.markets → API Keys (top right menu)

Your keys are encrypted by GitHub and never visible in any log or to anyone.

### Step 3 — Test with a manual run

1. Go to your repo → **Actions** tab
2. Click **VWAP EMA RSI Trading Bot** in the left sidebar
3. Click **Run workflow** (top right of the table)
4. Set `paper_mode` = **true**
5. Click the green **Run workflow** button
6. Click the run as it appears → click **run-bot** → watch the live log

You should see each stock in the watchlist being scanned with signal results.

### Step 4 — Automatic runs begin

Once the workflow file is in the repo, GitHub Actions automatically runs it on the cron schedule every trading day. No further action needed.

---

## 7. How to add or remove stocks

Open `vwap_ema_rsi_bot.py` in your repo (click the file → pencil icon to edit).

Find line ~48:
```python
WATCHLIST = ["NBIS", "NVDA", "TSLA", "AAPL", "MSFT"]
```

Add or remove any US stock ticker. Save (Commit changes). The next scheduled run picks it up automatically.

**Tips for choosing watchlist stocks:**
- Stick to stocks with average daily volume above 1 million shares (liquidity)
- Avoid stocks below $10 (too volatile, wide spreads)
- 5–10 stocks is the sweet spot — enough opportunities, not too much noise
- Earnings day? Remove the stock that day — fundamentals override technicals

---

## 8. How to monitor trades

### View the scan log (every run)

Repo → **Actions** tab → click any run → click **run-bot** job → full output visible

Each line shows:
```
2026-06-02 10:15:03  INFO     NBIS : LONG  | Price $255.0 | RSI 52.3 | Price $255 > VWAP $248 | EMA9 261 > EMA21 244 | RSI 52.3 in [45–62] | Volume 1.8M > avg 1.5M
2026-06-02 10:15:04  INFO     NVDA : NONE  | Price $890.0 | RSI 67.1 | RSI 67.1 outside entry zones
```

### Download the full log file

Repo → Actions → click any run → scroll to **Artifacts** section → download `trade-log-XXXXXX` (kept 30 days)

### View placed orders in Alpaca

- Paper trading: https://app.alpaca.markets → Paper Trading → Orders
- Live trading: https://app.alpaca.markets → Live Trading → Orders

---

## 9. How to go live

**Do not go live until you have completed at least 2 weeks of paper trading.**

Checklist before switching to live:
- [ ] Paper traded for at least 10 trading days
- [ ] Reviewed every trade in the log — understand why each signal fired
- [ ] Win rate above 55% on paper results
- [ ] Comfortable with the position sizes being placed
- [ ] Alpaca live account funded

To switch to live mode:

1. Open `vwap_ema_rsi_bot.py`
2. Find line ~44:
   ```python
   PAPER = os.getenv("PAPER_MODE", "true").lower() == "true"
   ```
3. Change the default to `"false"`:
   ```python
   PAPER = os.getenv("PAPER_MODE", "false").lower() == "true"
   ```
4. Commit and push — live trading begins on the next scheduled run

Alternatively, update the workflow file default:
```yaml
default: 'false'   # was 'true'
```

---

## 10. How to stop the bot

### Pause temporarily
Repo → **Actions** tab → left sidebar → **VWAP EMA RSI Trading Bot** → **...** menu → **Disable workflow**

Re-enable the same way whenever you want it running again.

### Stop permanently
Delete or rename `.github/workflows/trade.yml` in the repo.

### Cancel a specific run in progress
Repo → Actions → click the running job → **Cancel workflow**

### Close all open positions immediately
Log in to Alpaca → Portfolio → select positions → Close. The bot will not re-enter closed positions until the next scan confirms a fresh signal.

---

## 11. Tuning the parameters

All tunable values are at the top of `vwap_ema_rsi_bot.py` (lines 42–72). Edit in GitHub and commit — takes effect on the next run.

| Parameter | Default | Effect of increasing | Effect of decreasing |
|-----------|---------|----------------------|----------------------|
| `RSI_LONG_LOW` | 45 | Fewer long signals (stricter) | More long signals (looser) |
| `RSI_LONG_HIGH` | 62 | More long signals (looser) | Fewer long signals (stricter) |
| `RSI_SHORT_LOW` | 38 | More short signals (looser) | Fewer short signals (stricter) |
| `RSI_SHORT_HIGH` | 55 | Fewer short signals (stricter) | More short signals (looser) |
| `VOLUME_MULT` | 1.2 | Fewer signals (higher bar) | More signals (lower bar) |
| `MAX_RISK_PCT` | 0.015 (1.5%) | Larger positions | Smaller positions |
| `TARGET1_MULT` | 2.0 | Harder to reach T1 | Easier to reach T1 |
| `TARGET2_MULT` | 3.0 | Harder to reach T2 | Easier to reach T2 |
| `MAX_OPEN_TRADES` | 3 | More simultaneous trades | Fewer simultaneous trades |
| `MARKET_OPEN_BUFFER_MIN` | 15 | Longer wait after open | Shorter wait after open |

**Recommended**: Do not change any parameter until you have at least 20 trades of paper data. Changes should be tested on paper before going live.

---

## 12. Understanding the log output

```
2026-06-02 10:15:00  INFO     === Scan start === Portfolio: $52,430.00 | Open trades: 1/3
2026-06-02 10:15:01  INFO     NBIS : LONG  | Price $255.0 | RSI 52.3 | Price $255 > VWAP $248.12 | EMA9 261.4 > EMA21 244.8 | RSI 52.3 in [45–62] | Volume 1,823,400 > avg 1,512,000
2026-06-02 10:15:01  INFO     ============================================================
2026-06-02 10:15:01  INFO       TRADE SIGNAL  NBIS  LONG
2026-06-02 10:15:01  INFO       Entry: $255.0  |  Qty: 45 shares
2026-06-02 10:15:01  INFO       Stop:  $247.0  |  T1: $271.0  |  T2: $279.0
2026-06-02 10:15:02  INFO     NBIS: Entry order submitted — ID abc123
2026-06-02 10:15:02  INFO     NBIS: Stop loss @ $247.0 — ID def456
2026-06-02 10:15:02  INFO     NBIS: Target 1 @ $271.0 (22 shares) — ID ghi789
2026-06-02 10:15:02  INFO     NBIS: Target 2 @ $279.0 (23 shares) — ID jkl012
2026-06-02 10:15:03  INFO     NVDA : NONE  | Price $890.0 | RSI 67.1 | RSI 67.1 outside entry zones
2026-06-02 10:15:04  INFO     TSLA : NONE  | Price $175.0 | RSI 44.2 | Price at VWAP — no clear side
2026-06-02 10:15:05  INFO     === Scan complete ===
```

| Log item | Meaning |
|----------|---------|
| `Scan start` | Bot woke up and market is open |
| `LONG` / `SHORT` | Signal found — trade placed |
| `NONE` | No signal — reason given |
| `Outside trading window` | Bot ran but market closed or in buffer zone |
| `Already in position` | Stock already has an open trade — skipped |
| `Max open trades reached` | 3 trades already open — scan skipped |
| Order IDs | Alpaca order IDs — use these to look up trades in Alpaca UI |

---

## 13. Free tier limits

| Resource | Free allowance | This bot uses | Status |
|----------|---------------|---------------|--------|
| GitHub Actions minutes | 2,000 min/month | ~1,100 min/month | Safe ✓ |
| GitHub Actions storage | 500 MB | < 1 MB | Safe ✓ |
| GitHub private repos | Unlimited | 1 | Safe ✓ |
| Alpaca paper trading | Free forever | — | Safe ✓ |
| Alpaca live trading | Free (no commission) | — | Safe ✓ |

Calculation: 2 min/run × 26 runs/day × 21 trading days = 1,092 min/month

---

## 14. Disclaimer

This bot is for educational and personal use. Automated trading carries significant financial risk. Past performance of a strategy does not guarantee future results. Always paper trade before using real money. You are solely responsible for any trades placed by this bot on your account. The authors of this code accept no liability for financial losses.

**Never risk money you cannot afford to lose.**
