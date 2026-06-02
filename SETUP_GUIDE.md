# Trading Bot — GitHub Actions Setup Guide
## Zero cost, always-on, fully automated

---

## What you get (all free)
| Component        | Service           | Cost  |
|------------------|-------------------|-------|
| Code hosting     | GitHub repo       | Free  |
| Scheduler        | GitHub Actions    | Free (2,000 min/month) |
| Secret storage   | GitHub Secrets    | Free  |
| Trade execution  | Alpaca Paper/Live | Free  |
| Log storage      | Actions Artifacts | Free (30 days) |

GitHub Actions free tier = 2,000 minutes/month.
This bot uses ~2 min per run × 26 runs/day × 21 trading days = ~1,092 min/month. Well within free limits.

---

## Setup (one time, ~10 minutes)

### Step 1 — Create GitHub repo
1. Go to https://github.com/new
2. Name it: `trading-bot` (set to Private)
3. Click "Create repository"

### Step 2 — Upload the 3 files
Upload these files to the root of your repo:
- `vwap_ema_rsi_bot.py`
- `requirements.txt`
- `.github/workflows/trade.yml`

You can drag-and-drop them on the GitHub web UI,
or use git:
```bash
git clone https://github.com/YOUR_USERNAME/trading-bot
cd trading-bot
# copy the 3 files here
git add .
git commit -m "Initial bot setup"
git push
```

### Step 3 — Add your Alpaca API keys as Secrets
1. In your repo → Settings → Secrets and variables → Actions
2. Click "New repository secret"
3. Add these two secrets:

   Name:  ALPACA_API_KEY
   Value: (your Alpaca key from app.alpaca.markets)

   Name:  ALPACA_API_SECRET
   Value: (your Alpaca secret)

Your keys are NEVER visible in logs or code. GitHub encrypts them.

### Step 4 — Edit your watchlist
Open `vwap_ema_rsi_bot.py` and edit line ~35:
```python
WATCHLIST = ["NBIS", "NVDA", "TSLA", "AAPL", "MSFT"]
```
Add or remove any US stock ticker.

### Step 5 — Test it manually
1. Go to your repo → Actions tab
2. Click "VWAP EMA RSI Trading Bot" on the left
3. Click "Run workflow" → set paper_mode = true → Run
4. Watch the live log — you'll see each stock being scanned

---

## How to monitor trades

### View logs
Repo → Actions → click any run → click "run-bot" → see full output

### Download trade log
Repo → Actions → click any run → scroll down → Artifacts → download `trade-log-XXXXX`

### View trades in Alpaca
Go to https://app.alpaca.markets → Paper Trading → Orders

---

## Go live checklist
Before switching PAPER_MODE to false:
- [ ] Paper traded for at least 2 weeks
- [ ] Win rate > 55% on paper
- [ ] Understand every trade the log shows
- [ ] Set real money position size (edit MAX_RISK_PCT)

To go live: in your repo → Settings → Secrets → edit PAPER_MODE
Or change the default in the workflow YAML:
```yaml
default: 'false'   # was 'true'
```

---

## Modify the watchlist anytime
Just edit WATCHLIST in `vwap_ema_rsi_bot.py` and push to GitHub.
The next cron run picks it up automatically.

---

## Stop the bot
Repo → Actions → (left sidebar) → "VWAP EMA RSI Trading Bot" → ··· menu → Disable workflow
Re-enable the same way.
