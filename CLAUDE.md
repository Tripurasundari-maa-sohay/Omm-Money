# Portfolio Dashboard — Claude Code Rules + Architecture

---

# US BROKER ACCOUNT — PERMANENT FACTS (read every PDF parse session)

## Account facts
- **Account opened: 16-Dec-2025** (no activity before this date)
- Broker: US broker (PDF + xlsx statements)
- Statement PDF + Transactions xlsx always arrive together in `portfolio/inbox/`

## PDF parse checklist — run EVERY time a new statement arrives

### 1. Run pipeline
```bash
cd ~/Omm-Money/portfolio && bash sync.sh
```

### 2. Fee reconciliation (MUST match xlsx to $0)
```python
# open fees + closed _costs_paid == xlsx grand total
# xlsx grand total printed during sync: "grand tot: $-XXX.XX"
# If diff ≠ $0 → find misattributed ticker
```

### 3. Phantom closed position check
Cross-check every ticker in `holdings_cost.json us.closed[]` against xlsx sell trades.
Any closed ticker with NO sell in xlsx = phantom (parser misread corporate action).
```python
xlsx_sells = set(t['tk'] for t in txns['trades'] if t['open_close']=='close')
phantoms   = set(h['tk'] for h in cost['us']['closed']) - xlsx_sells
# phantoms must be EMPTY
```

### 4. Missing buy_date / fx_buy
Any open position with `buy_date=None` or `fx_buy=0` → INR return tile shows "—".
Fix: look up buy date in `transactions_us.json`, fetch FX from frankfurter.app.
```
https://api.frankfurter.app/YYYY-MM-DD?from=USD&to=INR
```

### 5. VOOG stock split (6:1, happened before account open)
VOOG was originally 3 shares × ~$441 → split 6:1 → now 18 shares × ~$73.
Parser may re-create phantom VOOG closed entry on each parse.
**Always remove phantom VOOG from closed if it appears** — VOOG is NOT closed.
Split attracted ZERO fees.

### 6. Known tickers with no Yahoo data (use Angel One)
ASHALOG (token 24711, NSE SME), PARAMATRIX (token 25069, NSE SME).
Signal scoring will log "no data" for these — expected, not a bug.

---

## Google Sheets export — rules (run every time user asks for sheet)

### Sources
- `portfolio/data/transactions_us.json` → individual buy/sell trade lots with dates, prices, commissions
- `portfolio/data/holdings_cost.json` → authoritative fees, realised P&L, open qty/avg
- `portfolio/data/processed/holdings_prices.json` → current LTP per ticker

### Columns (in order)
`Sl | Opened | Ticker | Name | Remarks | Holding TF | Broker | Current Px | QTY | Purchase Px | Brokerage-Buy | Brokerage-Sell | Cost Basis | Current MTM | Unrealized P/L | % Profit | Status | Closed Date | Closed Price | Realized P/L`

### Rules

**1. FIFO lot matching (CRITICAL)**
Tickers may have multiple buys — some lots closed, some open. Always apply FIFO:
- Sort buy trades by date ascending (oldest first)
- Match buy lots to close trades chronologically → closed lots → Closed rows
- Remaining unmatched buy qty → Open rows
- NEVER put closed buy lots into Open rows (inflates qty and MTM)
- Verify: sum of Open qty per ticker == PDF Holdings qty. If mismatch → FIFO broken.

**2. Fee split**
- Buy brokerage = proportional share of total open fees from `holdings_cost.json` per lot (by qty)
- Sell brokerage = proportional share of `_costs_paid` from closed entry (by qty across all buy lots for that ticker)
- Use `holdings_cost.json` fees as authoritative (xlsx patch already corrected them)

**3. Brokerage-Sell for open positions = $0** (not sold yet)

**4. Cost Basis** = QTY × Purchase Px + Brokerage-Buy + Brokerage-Sell

**5. Current MTM** = QTY × Current Px (0 for closed)

**6. Realized P/L** = proportional share of `realised` from `holdings_cost.json closed[]` by lot qty

**7. Closed Date / Closed Price** = from last sell trade for that ticker in `transactions_us.json`

**8. VOOG stock split (6:1)** — xlsx shows 3 shares at $441.46 (pre-split).
Always use holdings_cost values: 18 shares at $73.576 (post-split). Do NOT use xlsx buy for VOOG.

**9. Name map** — PDF/xlsx names are garbled. Always use clean name map:
```python
NAMES = {
    'AMZN':'Amazon.com Inc.','AVGO':'Broadcom Inc.','EWY':'iShares MSCI South Korea ETF',
    'GEV':'GE Vernova Inc.','GLW':'Corning Inc.','GOOG':'Alphabet Inc. Class C',
    'HUMN':'Roundhill Humanoid Robotics ETF','MP':'MP Materials Corp.',
    'MSFT':'Microsoft Corp.','MU':'Micron Technology Inc.','NOW':'ServiceNow Inc.',
    'RKLB':'Rocket Lab Corp.','SHIP':'Seanergy Maritime Holdings',
    'TTE':'TotalEnergies SE','VOOG':'Vanguard S&P 500 Growth ETF',
    'COST':'Costco Wholesale Corp.','DAX':'Global X DAX Germany ETF',
    'DOCN':'DigitalOcean Holdings Inc.','EGHT':'8x8 Inc.',
    'INOD':'Innodata Inc.','IRS':'IRSA Inversiones ADR','KEEL':'Keel Infrastructure Corp.',
    'META':'Meta Platforms Inc.','MUU':'Direxion Daily MU Bull 2X ETF',
    'NVDA':'NVIDIA Corp.','SIVR':'abrdn Physical Silver ETF','TOL':'Toll Brothers Inc.',
}
```

**10. Verify after generating — MANDATORY, no number should mismatch**
- All open qty per ticker == PDF Holdings qty (MUST = 0 diff)
- All open avg prices == PDF "open price" column (must match to 3dp)
- All open unrealized P&L == PDF unrealized (allow ±$0.02 rounding only)
- All closed realised P&L == PDF instrument P&L for closed tickers (MUST match)
- Commission total == xlsx grand total (MUST = $0 diff)
- Cash == PDF cash (MUST match exactly)
- Net deposits == PDF net deposits (MUST match exactly)
- Account value diff ≤ $1.00 (residual = accruals not tracked: custody + dividend accruals)
- VOOG: open = 18 shares (post 6:1 split), never in closed rows
- RKLB note: PDF blends open+closed P&L for same ticker — our closed realised ≠ PDF P&L by design

**15. Mixed ticker P&L (CRITICAL)**
Tickers with BOTH open + closed lots: `holdings_cost.realised` = PDF blended total P&L
(closed realised + open unrealized) — NOT pure closed P&L.
For Google Sheet "Realized P&L" on closed rows of mixed tickers, MUST compute from raw trade data:
  closed_realised = sum[(sell_px - buy_px) × lot_qty] - buy_comms - sell_comms + income
Mixed tickers as of May-2026 statement: AMZN, EWY, MU, RKLB
Correct values (raw trade calc):
  AMZN:  -$134.00  (5 sold @ $207.21, FIFO lot Dec-26 @ $232.01 → loss)
  EWY:   +$429.37  (11 sold @ $178.62, FIFO 5@150.49 + 6@128.00)
  MU:    +$341.82  (2@462.10 + 2@502.00)
  RKLB:  +$330.40  (15@70.15 + 15@102.00)
Pure closed tickers (no open remaining): use holdings_cost.realised directly ✓

**13. Deriving missing sell prices from PDF**
If xlsx has blank sell price (e.g. COST), back-calculate from PDF P&L breakdown:
  `sell_px = (P&L - income + costs_paid) / qty + avg_buy`
  e.g. COST: sell_px = (139.91 - 1.30 + 10.00)/1 + 851.00 = $999.61
Cross-check: (sell_px - avg) × qty + income - costs_paid must == PDF P&L.

**14. Income (dividends) in sheet**
Positions that received dividends while held show `income` in `holdings_cost.json closed[].income`.
Add to Remarks column: "Incl. div $X.XX".
Income IS included in realised P&L in holdings_cost — do not double-count.
Key dividend earners in May-2026 statement:
  COST $1.30, AVGO $4.55, GOOG $0.63, MU $0.30, MSFT $2.73, TOL $2.04, TTE $8.01,
  MUU $2.03, VOOG $3.49 (on open positions too).

**11. Untracked charges (from PDF cost summary — NOT in holdings_cost)**
- Client Custody Fee: ~$7.14/period
- Exchange Fee: ~$0.27/period
- French Financial Transaction Tax (TTE trades): ~$2.65/period
- Expected gap = ~$10. If gap > $15, investigate new charge type.

**12. Broker name**: "Doha Bank / DBG" for US broker (Doha Bank Global Markets)

---

# AUDIT CHECKLIST — run before every session ends
# ⚠️ VM IS THE PIPELINE — GH Actions full_update.yml / screener.yml are RETIRED

## 🔴 CRITICAL (data integrity)
- [ ] All open positions have `ltp` > 0 in `holdings_prices.json`
- [ ] All open positions have `pc` ≠ null (or confirmed big-mover with note)
- [ ] Day P&L = `(ltp - pc) × qty` for every position — verify 2-3 manually
- [ ] `holdings_prices.json.generated` timestamp < 5 min during market hours
- [ ] `market_indices.json.generated` timestamp < 5 min during market hours
- [ ] FX rate in `market_indices.json` between 80–110 INR/USD (sanity range)
- [ ] No GitHub secrets in any committed file (grep: `ghp_`, `apikey`, `password`, `token =`)

## 🟢 PIPELINE (VM cron — replaces all GH Actions)
```bash
ssh -i ~/Downloads/ssh-key-2026-05-26.key opc@145.241.158.254
crontab -l   # should show 4 entries:
#   * 3-10  Mon-Fri  → india prices (Angel One)
#   * 13-20 Mon-Fri  → US prices (Finnhub)
#   0 21    Mon-Fri  → NW snapshot
#   30 21   Mon-Fri  → signals update
tail -20 /home/opc/prices.log    # check last commit was OK (no 401)
tail -20 /home/opc/signals.log   # check signals committed OK
sudo systemctl status save-api   # save API for net-worth dashboard
sudo systemctl status caddy      # HTTPS proxy for save API
```
- [ ] VM cron running: `sudo systemctl status cron` or check `crontab -l`
- [ ] No `401 Bad credentials` in `/home/opc/prices.log` (token expired = silent freeze)
- [ ] Finnhub active: `tail prices.log | grep finnhub`
- [ ] Angel One active: `tail prices.log | grep "Angel One"`
- [ ] Signals committed today: `tail signals.log | grep "Committed signals"`
- [ ] save-api service active (net-worth save button)
- [ ] Caddy service active + HTTPS cert valid

## 🟠 LOGIC (calculations)
- [ ] Currency units consistent per tile (INR tile → INR values, USD tile → USD values)
- [ ] Day P&L tile NOT "--" during US market hours (status should be OPEN not RESET)
- [ ] FIRE tab: `fireTarget = annualExpenses / swrPct` (e.g. ₹24L / 0.04 = ₹6 Cr)
- [ ] Coast FIRE formula: `fireTarget / (1 + realRet)^yrsLeft`

## 🟡 DATA DISPLAY
- [ ] India holdings: DAY P&L and DAY % show ₹/% not USD
- [ ] US holdings: DAY P&L and DAY % show $/% not INR
- [ ] STALE tag on holdings where `p.live === false`
- [ ] Signal badges (BUY/HOLD/REDUCE) visible and color-coded
- [ ] Sector field in signals not "Unknown" (SECTOR_MAP covers all holdings)

## 🟢 NET-WEALTH (ODIN)
- [ ] `seed.json` has bank balances, loans, gold rates (not zeroed)
- [ ] `history.json` growing daily (VM cron 22:00 UTC appends one row/day)
- [ ] FIRE tab renders: progress bar, coast FIRE, child education, year-by-year table
- [ ] Net-worth save button works (`https://save.145-241-158-254.nip.io/save`)
- [ ] No Firebase references in code

## 🔵 SECURITY
- [ ] No hardcoded API keys in `.py`, `.html`, `.yml`
- [ ] VM `/home/opc/angel_env.sh` — GITHUB_TOKEN not expired (test: `curl -H "Authorization: token $GITHUB_TOKEN" https://api.github.com/user` → 200)
- [ ] VM `/etc/save-api.env` — chmod 600
- [ ] `sabarna-chowdhury` name: only in CLAUDE.md reference note (not in live code)
- [ ] Oracle VM SSH key: `~/Downloads/ssh-key-2026-05-26.key`

## 🔵 ARCHITECTURE (VM is source of truth)
- [ ] `data/processed/*.json` conflict → `git checkout --theirs` (VM owns these)
- [ ] Code files (`index.html`, `*.py`, `*.yml`) → NEVER `git reset --hard`
- [ ] Fallback chain: Finnhub → (nothing, stale carry) US | Angel One NSE+BSE → (nothing) India
- [ ] VOOG: open 18 shares post-split, never in closed. No phantom closed entry.
- [ ] GH Actions `full_update.yml` + `screener.yml`: schedule DISABLED (dispatch-only)

## QUICK GREP COMMANDS
```bash
# Secrets in code
grep -rn "ghp_\|apikey\s*=\|password\s*=\s*['\"]" portfolio/scripts/ net-wealth/index.html portfolio/index.html

# name exposure
grep -rn "sabarna-chowdhury" --include="*.html" --include="*.py" --include="*.yml" .

# VM token health (run on VM)
source /home/opc/angel_env.sh && curl -s -o /dev/null -w "%{http_code}" -H "Authorization: token $GITHUB_TOKEN" https://api.github.com/user

# Check stale data
python3 -c "import json,datetime; d=json.load(open('portfolio/data/processed/holdings_prices.json')); print(d['generated'])"

# Firebase leakage
grep -rn "firebase\|firestore" net-wealth/index.html portfolio/index.html
```

---


## What this repo is

Single-page portfolio dashboard hosted on **GitHub Pages** as a PWA. Tracks two
brokerage accounts:

- **🇺🇸 US** — US broker. Source of truth = PDF
  statement + US broker transactions xlsx.
- **🇮🇳 India** — Motilal Oswal / Upstox / Mirae Asset (FY24-26). Source =
  consolidated transactions xlsx.

Live market data + per-ticker history is fetched by **GitHub Actions** on a
cron and committed back to the repo. The browser loads JSON from the repo —
zero client-side computation of prices or indices.

## Data flow

```
inbox/*.pdf  ──► scripts/parse_broker_pdf.py
                 ├── extracts open/closed/monthly/cash/charges
                 └── preserves buy_date + fx_buy from prior holdings_cost.json
                                                              │
inbox/*.xlsx (US) ─► scripts/patch_fees_from_xlsx.py        │
                       └── Open/Close commission split        │
                                                              │
inbox/*.xlsx (US) ─► scripts/build_transactions_us.py       ▼
                       └── per-trade ledger + cash moves   data/holdings_cost.json
                                                              │
inbox/*.xlsx (India) ─► scripts/parse_india_excel.py          │  (also feeds
                                                              ▼   transactions_us.json)
                                                       data/transactions_us.json
                                                              │
GitHub Actions ─► scripts/market_data.py ──► data/processed/holdings_prices.json
                                              data/processed/market_indices.json
                                                              │
GitHub Actions ─► scripts/signals_update.py ─► data/processed/stock_signals.json
GitHub Actions ─► scripts/snapshot_eod.py   ─► data/history/{us,india}/*.csv
GitHub Actions ─► scripts/screener.py       ─► data/processed/screener.json
                                                              ▼
                                              index.html  (PWA loads from these JSONs)
```

## File ownership

| File | Owner | Conflict resolution |
|------|-------|---------------------|
| `data/processed/*.json` | GitHub Actions | `--theirs` |
| `data/holdings_cost.json` | GitHub Actions (FX/prices) + local (buy_date/fx_buy) | `--theirs` then re-apply local fields |
| `data/transactions_us.json` | sync.sh from xlsx | `--theirs` if conflict (rebuild from xlsx) |
| `data/history/` | GitHub Actions | `--theirs` |
| `index.html` | developer | NEVER reset |
| `scripts/*.py` | developer | NEVER reset |
| `sw.js` | developer | NEVER reset |
| `.github/workflows/*.yml` | developer | NEVER reset |

## Git Conflict Resolution (CRITICAL)

When `git pull --rebase` or `git push` causes conflicts:

**ALWAYS:**
```bash
git checkout --theirs data/holdings_cost.json
git checkout --theirs data/processed/
git add data/
git rebase --continue   # or: git push origin main
```

**NEVER:**
```bash
git reset --hard origin/main   # WIPES ALL CODE CHANGES
```

**After resolving any conflict, verify code survived:**
```bash
grep -n "S\.inrFxMon\s*=" index.html          # must exist
grep -n "build_inr_monthly\|build_fx_buy" scripts/market_data.py  # must exist
grep -n "renderTxLedger" index.html           # must exist (2026-05-23 onward)
```

Only `data/` files should be taken from remote (GitHub Actions owns them).
Code files (`index.html`, `*.py`, `*.yml`, `sw.js`) are always ours — never reset.

## GitHub Actions / cron

| Workflow | Schedule | Purpose |
|----------|----------|---------|
| `full_update.yml` | `*/15 4-21 * * 1-5` UTC | Fetch live prices + signals + audit + chart patch + commit |
| `watchdog.yml` | `*/5 4-21 * * 1-5` UTC | If `holdings_prices.json` >8 min stale during market hours → re-run full pipeline |
| `ping_trigger.yml` | external (cron-job.org every ~4 min) | Calls `workflow_dispatch` on `full_update` — bypasses GitHub's flaky scheduled cron |
| `eod_snapshot_india.yml` | `30 11 * * 1-5` UTC (5pm IST, after NSE close) | Pull NSE EOD bars → `data/history/india/*.csv` |
| `eod_snapshot_us.yml` | `0 21 * * 1-5` UTC (5pm ET, after US close) | Pull NYSE+NASDAQ EOD bars → `data/history/us/*.csv` |
| `screener.yml` | `0 1 * * 2,6` UTC | Daily S&P 500 + Nasdaq 100 + Nifty 500 + macro screen |

**5 min is the minimum** GH free-tier scheduled cron supports.

## Local sync flow

Drop the latest PDF + xlsx into `inbox/` and run:

```bash
./sync.sh
```

`sync.sh` sniffs each xlsx by sheet names:
- **US broker** (sheets `Trades` + `Bookings`) → `patch_fees_from_xlsx.py` + `build_transactions_us.py`
- **India** (sheet `All Transactions` or `Upstox`) → `parse_india_excel.py`

Then runs the **same pipeline GH Actions would run** so all derived chart /
tile values catch up to the new statement *before the commit*, not 5-15 min
later when cron fires.

### Every PDF → these files must refresh (sync.sh enforces it)

| File | Refreshed by | Why it must update on new PDF |
|------|--------------|-------------------------------|
| `data/holdings_cost.json` | `parse_broker_pdf.py` | Open positions, closed lots, cash, monthly anchors, statement totals — the new PDF *is* the source of truth |
| `data/holdings_cost.json` (fees) | `patch_fees_from_xlsx.py` | Per-ticker commission attribution corrected from xlsx Bookings × Trades |
| `data/transactions_us.json` | `build_transactions_us.py` | Per-trade ledger for the Transactions tab + ML/training |
| `data/processed/holdings_prices.json` | `market_data.py` | Live LTP/pc PLUS `weekly_chart` / `combined_weekly_chart` / `inr_fx_monthly` / `snp_actual_cum_pct` / `daily_chart` — these series all depend on the new positions + the new last `account_value` anchor |
| `data/processed/market_indices.json` | `market_data.py` | S&P 500, Nasdaq, Nifty, Sensex snapshots used in hero/USA banners |
| `data/processed/stock_signals.json` | `signals_update.py` | Buy/Hold/Reduce signals per ticker — must reflect current open positions |
| `data/processed/audit.json` + `audit_history.json` | `data_audit.py` | Health checks (alerts banner) recomputed against fresh state |
| chart anchors in `holdings_prices.json` | `patch_chart.py` | `us_val_usd[]` reanchored to broker `account_value[]` so chart matches statement penny-perfect |

If you skip any of these on a manual run, the dashboard will show **stale
hero values, stale headline tiles, stale charts**, or a misaligned INR-return
tile (when fx_buy isn't propagated). `sync.sh` runs all of them in order —
that's the whole point.

### Dashboard reads that depend on these files (index.html)

| Tile / chart | Sourced from |
|--------------|--------------|
| Hero "Total Portfolio Value" | `S.cost.us.cash` + holdings `mv` (live from `holdings_prices.json`) + `S.view.india.mv / fx` |
| US "ACCOUNT VALUE" tile | `us.totalValue = us.mv + us.cash` (live); secondary "Statement: $X · DD-Mon-YYYY" = `S.cost.us.account_value_statement` |
| US Day P&L tile | `holdings_prices.json` `ltp` − `pc` × qty |
| US "RETURN IN INR" tile | per-ticker `inrRet` derived from `holdings_prices.json[tk].fx_buy` + `S.cost.fx_inr_usd` (today FX) |
| Weekly Friday chart | `holdings_prices.json.weekly_chart.{port_ret, snp_ret, inr_ret, us_val_usd}` |
| Combined US+India weekly | `holdings_prices.json.combined_weekly_chart.{dates, total_usd}` |
| S&P 500 benchmark line | `holdings_prices.json.snp_actual_cum_pct` |
| INR overlay on returns chart | `holdings_prices.json.inr_fx_monthly` |
| Transactions tab summary stats | `holdings_cost.json.us` (open, closed, charges_breakdown) |
| Per-trade ledger | `data/transactions_us.json.trades[]` |
| Cash movements table | `data/transactions_us.json.cash_moves[]` |
| Audit banner | `data/processed/audit.json.alerts[]` |

Every one of these stales the moment the PDF changes if `sync.sh` only
commits `holdings_cost.json`. That's why sync.sh now runs the full pipeline.

### Local-only file fields (must survive PDF reparse)

`data/holdings_cost.json` `us.open[]` entries can carry these — they are **not
in the PDF**, but parser preserves them on reparse:

- `buy_date` — ISO date of first buy of the open lot
- `fx_buy` — INR/USD rate on `buy_date`

`fx_buy` is also written into `data/processed/holdings_prices.json` per ticker
by `market_data.py` for the dashboard's INR-return tile.

---

# Session log — 2026-05-23 (Sat)

## Symptoms reported
- Dashboard headline "ACCOUNT VALUE" frozen at 18-May broker value
- "Live prices unavailable — showing cost basis" banner
- All LTP = AVG (no live data)
- "DATA 8058m OLD" stale warning
- INR-return tile blank ("Run parser to activate")
- User believed cron + watchdog were "all lost"

## Root cause (the real one)

`scripts/market_data.py:364` had a backslash inside an f-string expression:

```python
print(f"  {tk:12s} ({yf_sym:14s}) → {q['ltp']:>12,.2f}  ({change_pct if change_pct is not None else \"—\":}%)")
```

Python 3.11 (GH Actions runtime) **rejects backslash in f-string expression
parts** with `SyntaxError`. Every `full_update.yml` run died at import time
on the "Fetch live prices" step. 8+ consecutive failures the day of the
session, dozens before. `cron-job.org` ping fired correctly, workflow
dispatched correctly, then Python died at line 1.

**Fix** (commit `49676292`): extract conditional to `_pct_str = ...` then
interpolate. First successful GH Actions run at 11:24 UTC same day.

## Secondary fixes shipped

| Commit | Change |
|--------|--------|
| `4cb01d2d` | `parse_broker_pdf.py` now preserves `buy_date` + `fx_buy` per-ticker on reparse. Was silently wiping them, breaking INR-return tile. |
| `34cf457a` | `index.html`: `us-account-value` headline shows live `us.totalValue` again. Statement value moved to small secondary line (`Statement: $X · DD-Mon-YYYY`). Reverted `add2b06f` which had frozen the headline. |
| `49676292` | **Root-cause fix**: `market_data.py` f-string SyntaxError. |
| `f51be11b` | **Removed biometric** entirely — bio-lock overlay HTML, BIOMETRIC IIFE, `window.BIOMETRIC` export, async wrapper around `refresh()`. Source of 5+ recurring refresh-flow bugs. `refresh()` now runs directly on `DOMContentLoaded`. SW v64. |
| `43934753` | 22-May PDF reparsed with the fixed parser. Account $19,496.03, 13 open positions. |
| `683d5841` | New `scripts/patch_fees_from_xlsx.py`. Reads US broker xlsx Bookings × Trades, joins on Trade ID, splits commissions Open/Close per ticker. Overlays correct fees onto `holdings_cost.json`. Example: RKLB open `fees: $0 → -$25`. xlsx total -$361.20 reconciled (residual $22.65 — account opened 16-Dec-2025, xlsx starts 15-Dec, no date gap; residual was a misattribution bug (VOOG partial-close fees)). |
| `9bcd787b` | New `scripts/build_transactions_us.py` → `data/transactions_us.json`. Per-trade rows with date, ticker, side, qty, price, gross, commission, FTT, exchange_fee, net, open_close, realised_pl, ISIN, asset_type, exchange. Separate `cash_moves[]` for deposits, dividends, custody, interest, withholding. Dedup'd US broker double-entry rows. `sync.sh` now sniffs xlsx flavor (US vs India) by sheet names. |
| `9c5e80f8` | UI: added two new sections under Transactions tab — **PER-TRADE LEDGER · US** (filter by side / open-close, search by ticker/name/ISIN, stat strip) and **CASH MOVEMENTS · US**. `load()` fetches `data/transactions_us.json` into `S.txUs`. SW v65. |

## Pipeline / cron — confirmed alive after fix

- `full_update.yml` succeeded at 11:24 UTC (first success in days)
- Auto-commits resumed: `2506365e`, `82dd8664`, `8626bceb`, `736aa7b9`...
- 5-min watchdog active during market hours
- `cron-job.org` external ping firing every ~4 min

## Recurring lessons / pitfalls

1. **Never `git reset --hard origin/main`** during conflict resolution — wipes local code.
2. **`paths-ignore: data/**`** on `full_update.yml` means data-only commits don't retrigger the workflow (anti-cascade). Code pushes DO trigger.
3. **F-strings with backslash** are illegal in Python <3.12. Always extract.
4. **PWA / SW caching** — bump `sw.js` `CACHE` const on every shipped change to `index.html` or the user will see stale UI on iOS until they close + reopen.
5. **PDF parser is non-authoritative for per-ticker fees** — it misattributes for re-opened tickers. xlsx Bookings × Trades is the authoritative source. Use `patch_fees_from_xlsx.py` after every PDF reparse.
6. **xlsx schema differs between brokers**. US broker has `Transactions`/`Trades`/`Bookings`. Upstox/India has `All Transactions` + `Upstox`. `sync.sh` sniffs by sheet name — don't assume by filename.
7. **GH Actions logs require auth** to fetch via API. Reproduce failures locally with `python3 scripts/<name>.py` first.

## Known not-fixed (low priority)

- `parse_india_excel.py` expects old Upstox sheet layout (`All Transactions` + `Upstox`). New broker India xlsx exports may have different sheet names. Sniffer in `sync.sh` will skip gracefully but India side won't auto-update. User flagged this as **not urgent**.
- `screener.yml` reports as failed in GH Actions list when triggered on `push` event (no `push` trigger defined — GH oddity). Scheduled Sat 01:00 UTC run works fine.

## Useful one-liners

```bash
# Check GH Actions status without gh CLI
curl -s "https://api.github.com/repos/${GH_REPO:-<owner>/portfolio-dashboard}/actions/runs?per_page=5" \
  | python3 -c "import json,sys; [print(r['created_at'], r['name'][:40], r['status'], r['conclusion']) for r in json.load(sys.stdin)['workflow_runs'][:5]]"

# Reproduce a failed market_data.py run locally
source .venv/bin/activate && python3 scripts/market_data.py 2>&1 | tail -30

# Verify no biometric refs sneaked back
grep -rln "BIOMETRIC\|biometric\|WebAuthn" index.html sw.js scripts/ .github/

# Full reparse + fee patch + transactions build (manual equivalent of sync.sh)
python3 scripts/parse_broker_pdf.py inbox/Portfolio_*.pdf
python3 scripts/patch_fees_from_xlsx.py inbox/Transactions_*.xlsx
python3 scripts/build_transactions_us.py inbox/Transactions_*.xlsx

# Sanity check: which scripts get called by what
grep -E "python3 \"\\\$SCRIPTS|python scripts/" sync.sh .github/workflows/*.yml

# Verify audit cross-check after a PDF reparse
python3 -c "import json; print(json.dumps(json.load(open('data/processed/audit.json'))['crosscheck'], indent=2))"
```

---

# Session log — 2026-05-23 (Sat) · Part 2 — post-mortem audit

After all the fixes shipped earlier in the day, a full pass was done to
catch points of failure that survived. Results:

## Dead code purged

| Path | Why gone |
|------|----------|
| `wealth.html` | Old React prototype dashboard. Zero references in `index.html`, `sw.js`, `sync.sh`, workflows or docs. |
| `scripts/parse_transactions.py` | Earlier per-position fee computer. Superseded by `scripts/patch_fees_from_xlsx.py` which uses xlsx Bookings × Trades join. Self-only references. |
| `scripts/backtest_v1.py` | Original backtester. `scripts/backtest.py` is the v2 (point-in-time index membership, batch yfinance, lower rate-limit failure). |

## Bugs fixed in same session

| Bug | Fix |
|-----|-----|
| `requirements.txt` missing pandas, openpyxl, beautifulsoup4, lxml, html5lib. Local fresh-clone install would crash inside India parser / US builders / screener. | Full deps re-listed in `requirements.txt`. GH Actions still inline-installs its own subset per workflow. |
| `market_data.py` did not persist `fx_rate` into `data/processed/market_indices.json`. `data_audit.py` read None on every run and "healed" from open.er-api.com — every cycle, redundantly. | `market_data.py` now writes `fx_rate = round(live_fx, 4)` into the indices payload when within sanity bounds (70 ≤ rate ≤ 120). Audit heal path becomes a true fallback. |
| `sync.sh` push-retry loop failed silently when working tree had any unstaged change (e.g. someone editing the README). `git pull --rebase` aborts with "cannot pull with rebase: You have unstaged changes" — all 3 retries kept failing, but sync.sh still printed "✓ Done". | Push loop now auto-stashes with `git stash push -u`, pulls + pushes, then pops the stash. Either pushes cleanly or surfaces a real conflict. |

## Interlock map (verified post-audit)

```
inbox/*.pdf       ──► parse_broker_pdf.py        ─► data/holdings_cost.json
inbox/*.xlsx US ──► patch_fees_from_xlsx.py    ─► data/holdings_cost.json (fees)
                  ──► build_transactions_us.py   ─► data/transactions_us.json
inbox/*.xlsx Ind. ──► parse_india_excel.py       ─► data/holdings_cost.json (india side; format-stale)

data/holdings_cost.json ──► market_data.py       ─► data/processed/holdings_prices.json
                                                 ─► data/processed/market_indices.json
                        ──► signals_update.py    ─► data/processed/stock_signals.json
                        ──► data_audit.py        ─► data/processed/audit.json + audit_history.json
                                                    (may heal market_indices.json / holdings_prices.json)
                        ──► patch_chart.py       ─► data/processed/holdings_prices.json (us_val_usd + combined)
data/processed/holdings_prices.json ──► signals_update.py (reads for momentum)
                                    ──► data_audit.py     (reads for staleness/drift)
data/processed/stock_signals.json   ──► index.html

data/history/{us,india}/*.csv ──► market_data.py (weekly chart sources)
                              ◄── snapshot_eod.py (writes EOD bars)

index.html fetches → holdings_cost.json, transactions_us.json, processed/{holdings_prices,market_indices,audit,stock_signals,screener}.json
                     demo_portfolio.json (lazy, for demo mode only)
```

Sequencing: `full_update.yml` runs market_data → signals_update → data_audit
→ patch_chart. `sync.sh` runs the same sequence locally after parsers.

## What lives but is rarely called (kept on purpose)

| File | Status |
|------|--------|
| `parse.sh` | Manual PDF-only quick path. Wraps venv creation + parse_broker_pdf.py. Useful when you only want to update positions, not the full sync pipeline. |
| `scripts/snapshot.sh` | Manual `data/holdings_cost.json` snapshot to `data/snapshots/YYYY-MM-DD.json` + git tag. Ad-hoc history capture. |
| `scripts/inbox_watch.sh` | macOS LaunchAgent helper — fires `sync.sh` when inbox/ changes. Optional auto-sync. |
| `scripts/backtest.py` | Manual backtest runner. No workflow calls it; user-invoked. |

## Audit cross-checks active in `data_audit.py`

The audit banner (top-right of dashboard) raises an alert when:

### Data anchor / drift checks
- `live_total (mv + cash)` drifts ≥ 15% from `account_value_statement` → `headline_vs_statement_drift`
- Last monthly anchor ≠ `account_value_statement` (>$1) → `anchor_vs_statement_mismatch`
- PDF age ≥ 35 days → `pdf_stale` · ≥ 14 days → `pdf_stale_soft`
- `cash_infusion_itd` < 0 → `cash_infusion_negative`
- `monthly.label_dates` length ≠ `monthly.account_value` length → `anchor_array_length_mismatch`
- xlsx commission_total vs `holdings_cost.json` open fees + closed `_costs_paid` drift > $50 → `fee_attribution_drift`

### Output-side schema checks (added 2026-05-23)
- `data/processed/*.json` missing a required top-level key → `json_schema_incomplete`
  - Catches partial producer-script output, e.g. screener.json shipped with only `tickers` (US) for 8 days but no `india_tickers` or `commodities`
- File unparseable → `json_file_unreadable`
- File missing entirely → `json_file_missing`
- Required key present but empty list/dict/string → `json_schema_empty`

Required-keys contract per file:

| File | Keys |
|------|------|
| `screener.json` | `tickers`, `india_tickers`, `commodities`, `regime`, `india_regime` |
| `holdings_prices.json` | `prices`, `weekly_chart`, `combined_weekly_chart`, `snp_actual_cum_pct`, `inr_fx_monthly` |
| `stock_signals.json` | `holdings`, `india_holdings`, `regime` |
| `market_indices.json` | `usa_market`, `india_market`, `fx_rate` |

### Workflow health checks (added 2026-05-23)
- Workflow has never produced a `success` conclusion → `workflow_dead`
- Last `success` older than 8 days → `workflow_stale`
- Catches the GH-free-tier scheduled-cron unreliability — a workflow can be
  "registered" + "enabled" but never actually fire because the cron queue
  silently drops it. `screener.yml` had zero successful runs for the lifetime
  of the repo and the audit was silent until this check landed.

Tracked workflows: `full_update`, `watchdog`, `eod_snapshot_us`,
`eod_snapshot_india`, `screener`, `ping_trigger`.

### Renderer-formula contract checks (added 2026-05-23)
- A synthetic closed-row test verifies the contract that `total = realised +
  income` (no `costs` term), because PDF `realised` is already net of fees.
  Catches the KEEL class of bug where the renderer added `costs` again and
  double-deducted.

### Raw numbers exposed under `audit.json.crosscheck{}`

`live_vs_statement_pct`, `live_total_usd`, `statement_total_usd`,
`pdf_age_days`, `fees_xlsx_total`, `fees_attributed_sum`, `fees_diff_usd`,
`schema_status`, `formula_status`, `workflow_status`. The dashboard can
render these on hover or in a debug panel.

## Past audit gaps (lessons recorded 2026-05-23)

The sanity audit done earlier in the same session was script-side + wire-side
only. It missed two real bugs because it never asserted output-side
correctness:

1. **KEEL P&L double-deduct** — `holdings_cost.json` had `realised: 107.98`
   + `costs: -24.02`, both correct. Bug was in `index.html` line 2085
   formula `total = realised + income + costs`. Audit didn't look at render
   formulas; the data was correct, the display was wrong.

2. **Screener India + commodities missing** — Audit verified `screener.py`
   compiles + `screener.yml` exists. Did NOT check whether `screener.json`
   had the keys the dashboard expects, and did NOT check whether the
   workflow had ever succeeded.

Schema-completeness + workflow-success-history + formula-contract checks
above now close these gaps.

---

# Session log — 2026-05-26/27 · Major architecture overhaul

## ODIN Net-Worth Dashboard (`net-wealth/`)

### Data layer
- `inputs.json` fully restored from `ODIN-Financials.xlsx` (all bank balances, loans, gold rates, vehicles, gratuity)
- `history.json` populated with 7 monthly snapshots (Nov-25 → May-26) so reconciliation tiles show data immediately
- `seed.json` updated: FIRE age=42, monthly expenses=₹2L, added `child_education` block
- PWA icons replaced with USD/INR coin symbol (192px, 512px, 180px apple-touch)

### FIRE tab — complete rebuild
| Feature | Detail |
|---------|--------|
| Progress bar | % to FIRE number |
| Coast FIRE | NW needed now to stop SIP and still retire at 42 |
| Monthly passive income | Current NW × 4% SWR ÷ 12 |
| Savings rate tracker | Income vs expense → rating (Exceptional/Good/Low) |
| Runway | Years current NW sustains at current expense rate |
| What-if slider | Drag ₹0–₹2L/mo → see FIRE age move in real-time |
| Child education fund | Future cost at 8% edu inflation + monthly SIP needed |
| Combined FIRE + edu target | Single number |
| 3 scenarios | Lean (₹1.5L) / Regular (₹2L) / Fat (₹3.5L) |
| Barista FIRE | Semi-retire with part-time income |
| Year-by-year table | NW vs target by age, FIRE date highlighted 🎯 |

FIRE inputs extended: `monthly_income_inr`, `barista_parttime_income_inr`, education fields.
`edu_*` keys routed through new `resolveKey()` branch → `S.seed.child_education`.

### UI fixes
- Sync symbols (☁️💾📸) removed from all buttons
- FX display: `$ to ₹XX.XX · QAR to ₹XX.XX`
- Theme toggle fixed (was returning blank page — now reloads instead of calling render())
- Firebase status indicator removed
- Stale data badge threshold: 8 min → 15 min

---

## Portfolio Dashboard (`portfolio/`)

### market_data.py — major refactor

#### Architecture split into two dedicated functions
```python
fetch_us_open_positions(holdings, existing_prices, manual_ltps)
    → Finnhub PRIMARY (real-time) → Yahoo fallback → stale carry-forward

fetch_india_open_positions(holdings, existing_prices, manual_ltps)
    → Angel One PRIMARY (real-time) → Yahoo fallback → NSE/Screener → stale
```

Dynamic throttle: `delay = max(1, math.floor(60 / N))` seconds between API calls.
Shared `_build_price_entry()` handles auto-heal + sanity cap for both functions.

#### Finnhub integration (US PRIMARY)
- `fetch_quote_finnhub(us_symbol)` → real-time, free tier
- Key: `FINNHUB_API_KEY` GitHub secret
- 13 US stocks → 60/13 = 4s gap → well within 60/min limit

#### Angel One SmartAPI (India PRIMARY)
- `fetch_quote_angelone(nse_symbol)` → real-time NSE prices
- Session cache (JWT token, 1-hour TTL, auto-refresh via TOTP)
- Dynamic token lookup via Angel One search API + `_ANGEL_TOKEN_CACHE`
- Keys: `ANGEL_API_KEY`, `ANGEL_SECRET_KEY`, `ANGEL_CLIENT_ID`, `ANGEL_MPIN`, `ANGEL_TOTP_SECRET`
- IP whitelist: `145.241.158.254` (Oracle VM)
- angel_active auto-detects from env vars — no code change needed to activate

#### Auto-heal pc (candle cross-check)
Every fetched price is validated: if API `pc` differs from Yahoo 5d candle by >1% → candle wins.
Prevents wrong `chartPreviousClose` values (fixed GLW bug: stored $207 vs actual $194).

#### Cap raised
- US: 15% → 30% (covers earnings moves like MU +17%)
- India: 10% → 20% (covers circuit breakers)

#### post_fetch_audit.py — zero network calls
Rewritten to be JSON-only validator (was making 27 yfinance calls → caused workflow timeout).
- Validates all positions have ltp + pc
- Computes and prints day P&L summary
- Exits 1 only on unrecoverable missing price data
- Runs in <0.1s vs old ~4 min

### Oracle VM — self-hosted runner
| Item | Value |
|------|-------|
| Provider | Oracle Cloud Always Free |
| Region | Saudi Arabia (me-riyadh-1) |
| Shape | VM.Standard.A1.Flex (ARM, 1 OCPU, 6GB RAM) |
| Public IP | `145.241.158.254` (static, permanent) |
| OS | Oracle Linux 9 (aarch64) |
| Service | `github-runner.service` (systemd, auto-restarts) |
| SSH | `ssh -i ~/Downloads/ssh-key-2026-05-26.key opc@145.241.158.254` |
| Purpose | Fixed IP for Angel One whitelist + self-hosted GH Actions runner |

Workflow `full_update.yml` now runs on `[self-hosted, oracle-vm]` instead of `ubuntu-latest`.
Runner registered as `oracle-vm`, service `/etc/systemd/system/github-runner.service`.

### Cron interval
Changed from `*/15` to `*/5` — fetches every 5 min during market hours.
Page auto-refresh: 15m10s → 5m30s.

### UI — portfolio/index.html
- **DAY % column** added to US Holdings table (was only in India)
- Both tables: null-safe `—` when pc missing instead of NaN display
- Sort by `dayPct` wired in both `US_SORT_KEY` and `IN_SORT_KEY`
- Stale data badge fires at >15 min (was 8 min — too tight for 5+7 min pipeline)
- Badge tooltip updated: "check GitHub Actions if >20 min"

### GitHub Secrets (all active)
| Secret | Purpose |
|--------|---------|
| `FINNHUB_API_KEY` | US real-time prices |
| `ANGEL_API_KEY` | India real-time prices |
| `ANGEL_SECRET_KEY` | Angel One auth |
| `ANGEL_CLIENT_ID` | Angel One login |
| `ANGEL_MPIN` | Angel One PIN |
| `ANGEL_TOTP_SECRET` | TOTP for Angel One 2FA |

### Known pending
- PARAMATRIX + ASHALOG: delisted on Yahoo. Angel One may resolve on next India market open.
  If still failing → set `manual_ltp` in `holdings_cost.json`
- Finnhub WebSocket (browser-side real-time for US) — discussed, not yet built
- Angel One WebSocket → Firebase → browser (real-time India) — discussed, not yet built
- Oracle VM: 3 more ARM cores + 18GB RAM still unused in Always Free quota

### Recurring pitfalls from this session
1. **Git conflict on data files** — always `git checkout --ours data/processed/` when rebasing
2. **post_fetch_audit making network calls** caused workflow timeout — keep it JSON-only
3. **chartPreviousClose stale** — Yahoo API field unreliable; always cross-check vs candle
4. **Cap too low** — 15% cap silently excluded MU +17% earnings move; raised to 30%
5. **Token storage** — never commit PAT/API keys in code; always GitHub Secrets + `os.environ.get()`

---

# Session log — 2026-05-29 (Fri) · VM is now the price pipeline (GH Actions retired)

## ARCHITECTURE NOW (read this first)
Live prices are **NOT** fetched by GitHub Actions anymore. The Oracle VM
(`145.241.158.254`, user `opc`) runs `scripts/fetch_all_prices_vm.py` via
**crontab every minute** and commits JSON to the repo via the GitHub Contents
API (`requests.put`, SHA-based, 3× retry). `full_update.yml` still exists on
`runs-on: ubuntu-latest` but is **effectively dead** — VM replaced it.

- VM SSH: `ssh -i ~/Downloads/ssh-key-2026-05-26.key opc@145.241.158.254`
- VM script path: `/home/opc/fetch_all_prices_vm.py` (its **own copy** — editing
  the repo file is NOT enough; must `scp` to VM to go live)
- VM secrets: `/home/opc/angel_env.sh` (`source` it before running) — holds
  `ANGEL_*`, `FINNHUB_API_KEY`, `GITHUB_TOKEN`
- Crontab: `* 3-10 * * 1-5` india (flock /tmp/fetch_india.lock) ·
  `* 13-20 * * 1-5` us (flock /tmp/fetch_us.lock) — UTC, every minute
- **Deploy = edit repo file → `scp` to VM → `python3 -m py_compile` on VM →
  run once to verify.** Also push repo copy (use Contents API PUT to dodge the
  every-minute commit race; `git push` keeps losing the rebase).

## What broke + what was fixed

### 1. US DAY P&L tile stuck on "--"
- `market_indices.json` was orphaned at the last `full_update.yml` run — status
  frozen at `RESET`. index.html:1959 blanks the US DAY P&L tile to `--` whenever
  `usa_market.status === 'RESET'`.
- **Fix:** VM script now ALSO writes `market_indices.json` every cycle —
  S&P/Nasdaq (`^GSPC`/`^IXIC`), Nifty/Sensex (`^NSEI`/`^BSESN`), `fx_rate`
  (`INR=X`) via the Yahoo **chart-endpoint meta** (`fetch_yahoo_meta`), plus a
  ported `market_status()` (zoneinfo `America/New_York` / `Asia/Kolkata`, with a
  fixed-offset fallback if tzdata missing). Status now tracks the real clock →
  tile shows live P&L during market hours, blanks only in the true 7:30–9:30 ET
  RESET window.

### 2. CDN-cache clobber (read-modify-write race)
- `market_indices.json` holds both `usa_market` + `india_market`. Each cron run
  rewrites only its market and preserves the other from the existing file. The
  existing file was read from `raw.githubusercontent.com` which is **CDN-cached
  ~5min** → the india run read a stale copy and clobbered the fresh usa block
  (and vice versa).
- **Fix:** `get_current_indices_from_github()` reads via the **authenticated
  Contents API** (uncached), falls back to raw CDN only on error.

### 3. GITHUB_TOKEN expired → entire pipeline silently froze
- VM `GITHUB_TOKEN` (old `ghp_0hl…`) expired ~09:30 UTC → ALL commits 401 Bad
  Credentials → prices AND indices stopped updating, **no alert**. Earlier
  symptoms (stale data) traced back to this.
- **Fix:** replaced token in `angel_env.sh`. ⚠️ **Token expiry is a silent
  single point of failure.** TODO: add an audit check that flags VM commit
  failures / `holdings_prices.generated` age during market hours.

### 4. India prices not matching broker (Daily P&L)
Per-share LTP from Angel One was already accurate. The mismatches were:
- **3 stocks dropped from Day P&L** (AVADHSUGAR/IRBINVIT/FILATFASH had `pc=None`)
  because the old `fetch_india_yahoo_fallback()` used `yfinance .history(period=2d)`
  — flaky intraday (no pc, and IRBINVIT silently froze hours-old).
  **Fix:** rewrote fallback to use `fetch_yahoo_meta` (reliable ltp +
  `chartPreviousClose`). Later retired Yahoo entirely (see below).
- **ASHALOG + PARAMATRIX** delisted on Yahoo, frozen 7 days. They ARE on NSE as
  **SME** stocks (`-SM` suffix). Added to `ANGEL_TOKEN_MAP`: ASHALOG=`24711`,
  PARAMATRIX=`25069`. Now fetch live.
- **Exchange mismatch:** broker holds AVADHSUGAR + GMBREW on **BSE**; dashboard
  was reading NSE (AVADHSUGAR NSE 447 vs BSE 437 — illiquid divergence). Added
  `ANGEL_BSE_TOKEN_MAP` (AVADHSUGAR=`540649`, GMBREW=`507488`) and made
  `fetch_india_angel()` query NSE+BSE in one call, keying results by
  `(exchange, token)`. Removed GMBREW from the NSE map.
- **Yahoo fallback now fully retired** (`YAHOO_INDIA_FALLBACK = {}`) — all 14
  India positions come from Angel One (NSE + BSE).

## Broker → ticker mapping (holdings_cost.json `india.open[].broker`)
- `Motilal Oswal` → SBIN, AVADHSUGAR(BSE), IRBINVIT, GMBREW(BSE), GOLDBEES_M,
  RELIANCE, NBCC, PARAMATRIX
- `Upstox` → GOLDBEES_U, WAAREEENER, FILATFASH
- `m.Stock` → JYOTISTRUC, DIACABS, ASHALOG  **( m.Stock = Mirae Asset )**

GOLDBEES is split `_M` (Motilal) + `_U` (Upstox), both Angel token `14428`.

## Angel One useful bits
- Scrip-master (public, no auth) for token lookup:
  `https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json`
  — grep by `name`/`symbol`, fields `symbol`,`token`,`exch_seg`. SME = `-SM`,
  BE-series = `-BE`, InvIT = `-IV`.
- Quote API takes `exchangeTokens: {"NSE":[...],"BSE":[...]}`, response
  `data.fetched[]` has `symbolToken`,`exchange`,`ltp`,`close` (=prev close).
- `searchScrip` order-API endpoint returned 400 in testing — use the scrip
  master file instead.

## FIXED: PARAMATRIX phantom day P&L (volume-based heal)
Illiquid SME with 0 trades today has a stale Angel `close` (older session) →
phantom day P&L (was +₹4,800 on Motilal). **Fix shipped:** `fetch_india_angel`
reads `tradeVolume` from the FULL quote; if `int(tradeVolume) == 0`, set
`pc = ltp` → Day P&L = 0, matching broker (which treats prev-close = last
price). Auto-corrects any future no-trade SME, no manual upkeep. PARAMATRIX
now pc=ltp=68 (day 0); after this, Motilal reconciled −18,252 vs broker
−18,257. Commit `cc21c48de`.

## FIXED: pipeline failure alerting (ntfy)
VM script now POSTs a push alert to `https://ntfy.sh/<NTFY_TOPIC>` when a cycle
fails (commit 401/expired token, no prices fetched, indices commit fail). 30-min
cooldown (`/tmp/ommoney_last_alert`) so the every-minute cron can't spam.
**Activation:** set `NTFY_TOPIC` in `/home/opc/angel_env.sh` AND subscribe to
that exact topic in the ntfy app (https://ntfy.sh) on phone. Empty topic =
alerts disabled (only logs `ALERT:` to stderr/prices.log). Verified end-to-end
with a forced bad-token run. Commit `db18c9a3d`.

## FIXED: full_update.yml retired
Auto-triggers (push + schedule) commented out; `workflow_dispatch` kept as a
manual backup. VM cron is the sole live pipeline. Stops redundant/racing
commits + wasted Actions minutes + the stale-market_indices class of bug.
Commit `7d5b29454`.

## FIXED: GITHUB_TOKEN rotated (2026-05-29)
Old `ghp_68JA…` (shared in plaintext chat) revoked. New token `ghp_Rky…`
set in `/home/opc/angel_env.sh` directly via SSH (never through chat).
Verified: 200 OK + full commit cycle working.

## FIXED: RETURN IN INR tile (2026-05-29)
VM script now reads `holdings_cost.json` via Contents API and stamps `fx_buy` +
`buy_date` into each US price entry before committing. Previously VM wrote only
`{ltp, pc, source, as_of}` — `fx_buy=None` → tile showed "— / Run parser to
activate". Commit `c3efd1796`.

## FIXED: Expense Planner deployed (2026-05-29)
Added `expense/` subfolder to repo. Standalone PWA, no integration with
portfolio or net-worth. Google Drive sync built in. Paths fixed for
`/Omm-Money/expense/` subfolder (manifest, sw.js, icon refs).
URL: `https://tripurasundari-maa-sohay.github.io/Omm-Money/expense/`

## KNOWN PENDING (not fixed)

### 🟠 Still to build
- **6a. VM watchdog cron** — auto re-trigger `fetch_all_prices_vm.py` if `holdings_prices.json`
  is stale >10 min during market hours. Heals pipeline silently.
- **6b. Dashboard CPR button** — manual "Refresh Prices" button + auto-heal on page load:
  if data stale → fetch direct from Yahoo chart API in browser, overlay on tiles.
  No VM needed, no API key, works even if VM is down.
- **Privacy (January 2026 review)** — Decide: private repo (GitHub Pro $4/mo) vs
  Cloudflare Access vs VM data serve. Dashboard/ODIN data JSON files are still public.
- **Congress/Politician trade tracking** — Track US politician STOCK Act disclosures.
  Source: Capitol Trades API (free, no auth) + Quiver Quant (free key).
  Script: `congress_fetch.py` on VM daily → `portfolio/data/processed/congress_trades.json`.
  Dashboard: new CONGRESS tab (politician · party · ticker · amount · date · days_ago).
  Signal integration: politician BUY on your holdings = +score in signals.
  Scope options: (A) your holdings only, (B) broader watchlist ideas, (C) both.
  User to confirm scope before build.

### 🟡 Low priority / noise
- WAAREEENER / GOLDBEES_U on NSE — broker exchange unconfirmed; tiny gaps,
  treated as live-timing noise.

---

# Session log — 2026-06-01 (Sun) · Security + Signals + Infrastructure

## Completed this session

### ✅ Security / Privacy
1. **Cloudflare Worker → VM save API** — `save_api.py` on Oracle VM, Caddy HTTPS,
   `https://save.145-241-158-254.nip.io/save`. `sabarna-chowdhury` name fully removed
   from `net-wealth/index.html` + `CHANGELOG.md`. Commits: `a3de41995`, `80a432bb3`.
2. **Expense planner OAuth** — `https://tripurasundari-maa-sohay.github.io` added to
   Google Cloud OAuth Client `1000939411703-…`. Confirmed working.
3. **`monthly_income_inr` zeroed** — was 600000 (salary exposed). Now 0 in `seed.json`.

### ✅ VM Services (new systemd services on 145.241.158.254)
- `save-api.service` — Flask save API for net-worth dashboard (port 8765, local only)
- `caddy.service` — Caddy HTTPS reverse proxy (ports 80+443, nip.io domain, Let's Encrypt)
- Oracle VCN security list: ports 80+443 opened for ingress
- SELinux label fixed on Caddy binary (`chcon -t bin_t`)
- VM env file: `/etc/save-api.env` (chmod 600, holds GITHUB_TOKEN for save API)

### ✅ Signals upgrade
- **yfinance replaced** with direct Yahoo chart API in `signals_update.py` — was broken
  on Python 3.9, now uses `requests` + Yahoo v8 chart endpoint directly.
- **FinanceDatabase integrated** — `pip install financedatabase` on VM. US stocks get
  authoritative sector + industry (e.g. GEV = Energy not Industrials, RKLB = Aerospace & Defense).
  ETFs + India SME → SECTOR_MAP fallback.
- **`sector` + `industry`** now in `stock_signals.json` and `signals_history.csv`.
- **VM cron added**: `30 21 * * 1-5` runs signals daily, commits to GitHub.
- **`screener.yml` schedule disabled** — was `no_success_ever` (yfinance broken + 2.5hr job
  on free-tier GH Actions). Kept dispatch-only. screener.py migration to VM is pending.

### ✅ ML training data
- `portfolio/data/history/signals_history.csv` — daily append of all scored tickers.
  Schema: `date, ticker, market, score, action, regime, rsi, v200, rs, hi52, lo52,
           rng, vsMean, px, sector, industry, spy_px, spy_pct_vs200`
  Grows 27 rows/weekday. Idempotent (re-runs same day don't duplicate).

### ✅ CLAUDE.md audit checklist rewritten
- Reflects VM pipeline (not dead GH Actions)
- Includes VM health checks, token expiry check, save-api + Caddy status

## VM crontab (full, as of 2026-06-01)
```
* 3-10  Mon-Fri  → India prices (Angel One NSE+BSE, every minute)
* 13-20 Mon-Fri  → US prices (Finnhub, every minute)
0 21    Mon-Fri  → NW snapshot (daily_nw_snapshot.py)
30 21   Mon-Fri  → Signals (signals_update.py → stock_signals.json + signals_history.csv)
```

## Key new VM files
| File | Purpose |
|------|---------|
| `/home/opc/save_api.py` | Flask save proxy (replaces Cloudflare Worker) |
| `/home/opc/signals_update.py` | Signals scorer (copy of repo scripts/signals_update.py) |
| `/home/opc/daily_nw_snapshot.py` | NW snapshot (copy of repo tools/daily_nw_snapshot.py) |
| `/etc/caddy/Caddyfile` | Caddy config: `save.145-241-158-254.nip.io → :8765` |
| `/etc/save-api.env` | Env vars for save-api service (chmod 600) |
| `/etc/systemd/system/save-api.service` | save-api systemd unit |
| `/etc/systemd/system/caddy.service` | Caddy systemd unit |

## Signals architecture (post-upgrade)
```
VM cron 21:30 UTC
  → signals_update.py
  → Yahoo chart API (direct, no yfinance)  ← replaced broken yfinance
  → OpenBB technical: RSI-14, MACD, EMA   ← new (installed on VM)
  → FinanceDatabase: sector + industry      ← new (installed on VM)
  → 9-factor scoring model (unchanged, already sophisticated)
  → stock_signals.json committed to GitHub
  → signals_history.csv appended (ML training data)
```

## Privacy status (Jan 2026 review pending)
- `sabarna-chowdhury` name: REMOVED from all code ✓
- `monthly_income_inr`: zeroed ✓
- Data JSON files (holdings_prices.json, seed.json etc): still public in repo
- Decision deferred: use repo heavily for 6 months → then decide private repo vs Cloudflare Access
- If going private: raw.githubusercontent.com won't work in browser → need VM data serve

## ✅ FORECAST TAB (added 2026-06-01)
`net-wealth/index.html` now has 📈 FORECAST tab with component-level wealth projection.

### Locked assumptions (2026-06-01 review)
| Component | Rate / Rule |
|-----------|-------------|
| India equities | Slider % + India SIP slider |
| US equities | Slider % + **$1K/mo locked** |
| Gold (jewellery + Malabar) | +11%/yr |
| Apartment MV | +2%/yr |
| Apt loan (Canara) | **9% pa** · EMI ₹36,387 + ₹15K prepay/mo |
| Car (LC) | −15%/yr depreciation |
| LC loan (Doha) | 4.5% pa · QAR 6,851/mo · May+Jun-26 paused |
| CCs (QA + Lulu) | Cleared Jun-26 |
| Bou loan | +12% every March, never settled |
| Gratuity | +QAR 9,444 every January |
| F&O corpus | +6.5% (conservative) |
| FX | $1=₹95 · QAR=₹26.1 (fixed) |

### Tab features
- Equity return slider (4–25%)
- India SIP slider (₹0–₹5L/mo) + US $1K/mo locked
- Years slider (1–30) + Inflation slider
- Summary: starting NW / projected NW / real NW (inflation-adj) / total invested
- Milestone badges: ₹1Cr → ₹100Cr + 🔥 FIRE milestone
- Chart: nominal vs real NW curve
- Year-by-year table with breakdown (Eq · Gold · Apt(net) · Gratuity · Bou)
- 3-scenario comparison: Conservative 7% / Expected / Aggressive 18%
- `_projectComponents()` models each asset/liability separately (not simple compound)

## SECURITY
- GITHUB_TOKEN: `ghp_Rky…` (rotated 2026-05-29). In `/home/opc/angel_env.sh` + `/etc/save-api.env`.
- Next token rotation: check expiry in GitHub Settings → Developer settings → PATs.

## Reconciliation snapshot (same-moment broker screenshots, 2026-05-29 ~12:43 IST)
After fixes (PARAMATRIX phantom aside, everything within live-timing noise):
- Mirae: dash −1,067 vs broker −1,257
- Upstox: dash +73 vs broker −105
- Motilal: dash −13,298 vs −17,265 (≈ −18,098 without PARAMATRIX phantom → ~₹830 timing)

## Commits this session
- `market_data.py`-independent VM script gained: market_indices write, Contents
  API read, chart-endpoint India fallback, Angel SME tokens, Angel BSE map.
- Pushed via Contents API PUT (git push loses race to every-minute VM commits):
  `cacf2adec`, `8110787d6`, `31b03a13a`, `6a5280791`, `d9ccc5916`.

---

# Session log — 2026-06-02 (Mon) · PDF parse, Auto-onboard, Dashboard upgrades

## PDF parse (new statement 01-Jun-2026)
- Statement date: 01-Jun-2026
- Account value: $21,080.37 · Cash: $70.55 · Total P&L: +$3,890.23
- 16 open positions (ORCL added) · 16 closed

## New position: ORCL (Oracle Corp.)
- 4 shares @ $230.00, bought 2026-06-01, FX ₹94.99/USD
- Added to: `holdings_cost.json`, `fetch_all_prices_vm.py` US_HOLDINGS,
  `parse_broker_pdf.py` TICKER_MAP, `signals_update.py` SECTOR_MAP (Technology)
- VM price fetch triggered manually: ORCL → $248.15 (+9.91%) from Finnhub

## Auto-onboard script (`portfolio/scripts/onboard_new_tickers.py`)
- Runs automatically in `sync.sh` after PDF/xlsx parse, before `market_data.py`
- Detects new tickers in `holdings_cost.json` not yet in:
  1. `fetch_all_prices_vm.py` US_HOLDINGS (appends)
  2. `signals_update.py` SECTOR_MAP (sector via FinanceDatabase)
  3. `parse_broker_pdf.py` TICKER_MAP (PDF name → ticker)
- Auto-fixes `buy_date` + `fx_buy` from `transactions_us.json` + frankfurter.app
- **VOOG phantom auto-removal baked in** — parser re-creates phantom every PDF parse;
  onboard script detects and removes it, keeping fees at $0.00 diff vs xlsx.

## Fee reconciliation fixes
- RKLB open fee corrected: −$25 → −$5 (only 5 remaining shares = 1 buy lot)
- VOOG phantom removed (6:1 split artefact) → fees reconcile $0.00 diff vs xlsx
- SHIP open fee confirmed: −$5 (single lot 2026-05-27)

## Audit alert fix (`data_audit.py`)
- `anchor_vs_statement_mismatch` now only fires if same calendar month AND >2% drift
- Previously fired on any >$1 diff — false positive after portfolio gains between
  month-end anchor and next-day statement

## Dashboard upgrades (portfolio/index.html)

### Drag-to-reorder columns
- Both US + India holdings tables: drag `<th>` headers to reorder columns
- Order persisted in `localStorage` per table
- **↺ cols** button resets to default order
- `US_COL_ORDER` / `IN_COL_ORDER` arrays drive both header + row render
- Column config: `US_COL_DEFS` / `IN_COL_DEFS` with `renderCell(p)` per column
- Footer rows (`TOTAL` + `NET IF CLOSED`) rebuild dynamically with column order

### Default column order (US)
TICKER · QTY · AVG · LTP · **MKT VALUE** · DAY P&L · DAY % · FEES · UNREAL P&L · RETURN % · INR RETURN % · SIGNAL

### US Analytics redesign
- Layout: 4 rows with consistent `an-card` / `an-grid-2` CSS
  - Row 1: Weight donut | Today's Day % chart
  - Row 2: Return % ITD (full width)
  - Row 3: Stock/ETF mix | Sector exposure
  - Row 4: Signal distribution | P&L Waterfall
- Mobile responsive: `@media(max-width:700px)` collapses `an-grid-2` to single column
- Card titles use `--accent-g` (teal) for contrast

### Chart contrast improvements (all themes + mobile)
- Datalabels: **white text** (`_dlColor`) with shadow — no longer same color as bar
- Bars: `cc` (80%) opacity + 1.5px border — crisper than previous `bb` (73%)
- Axis ticks: `_tickC = _priC` (bright primary text) not `_secC` (dim secondary)
- `layout.padding.right: 60` — space for labels outside bar on mobile
- Works correctly on all 4 themes: default dark / violet / ivory (light) / arctic (light)
- `_isDark` check: white labels on dark themes, dark labels on light themes
- Shared `_DL_BAR` config for Day % + Return % charts
- Waterfall chart: datalabels added (USD values on each bar)

### DATA badge fix
- `⚠ DATA` badge was showing false positive from `anchor_vs_statement_mismatch`
- Fixed in `data_audit.py` — see above

## Congress trade tracking (PENDING — scope decision needed)
- Added to TODO list. User to confirm scope (A/B/C) before build.
- See KNOWN PENDING section above.

## Key commits this session
- `onboard_new_tickers.py` added to repo + sync.sh
- `parse_broker_pdf.py` TICKER_MAP: ORCL added
- `fetch_all_prices_vm.py` + `signals_update.py`: ORCL added, deployed to VM
- Multiple `portfolio/index.html` pushes: column reorder, analytics redesign, chart contrast
- `data_audit.py`: anchor mismatch threshold fix
- All pushed via GitHub Contents API (bypasses VM commit race)
