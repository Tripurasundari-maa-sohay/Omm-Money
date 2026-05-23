# Portfolio Dashboard — Claude Code Rules + Architecture

## What this repo is

Single-page portfolio dashboard hosted on **GitHub Pages** as a PWA. Tracks two
brokerage accounts:

- **🇺🇸 US** — Doha Bank Global (Saxo white-label). Source of truth = PDF
  statement + Saxo transactions xlsx.
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
inbox/*.xlsx (Saxo) ─► scripts/patch_fees_from_xlsx.py        │
                       └── Open/Close commission split        │
                                                              │
inbox/*.xlsx (Saxo) ─► scripts/build_transactions_us.py       ▼
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
- **Saxo** (sheets `Trades` + `Bookings`) → `patch_fees_from_xlsx.py` + `build_transactions_us.py`
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
| `683d5841` | New `scripts/patch_fees_from_xlsx.py`. Reads Saxo xlsx Bookings × Trades, joins on Trade ID, splits commissions Open/Close per ticker. Overlays correct fees onto `holdings_cost.json`. Example: RKLB open `fees: $0 → -$25`. xlsx total -$361.20 reconciled (residual $22.65 = PDF window covering Dec 1-14 not in xlsx). |
| `9bcd787b` | New `scripts/build_transactions_us.py` → `data/transactions_us.json`. Per-trade rows with date, ticker, side, qty, price, gross, commission, FTT, exchange_fee, net, open_close, realised_pl, ISIN, asset_type, exchange. Separate `cash_moves[]` for deposits, dividends, custody, interest, withholding. Dedup'd Saxo double-entry rows. `sync.sh` now sniffs xlsx flavor (Saxo vs India) by sheet names. |
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
6. **xlsx schema differs between brokers**. Saxo has `Transactions`/`Trades`/`Bookings`. Upstox/India has `All Transactions` + `Upstox`. `sync.sh` sniffs by sheet name — don't assume by filename.
7. **GH Actions logs require auth** to fetch via API. Reproduce failures locally with `python3 scripts/<name>.py` first.

## Known not-fixed (low priority)

- `parse_india_excel.py` expects old Upstox sheet layout (`All Transactions` + `Upstox`). New broker India xlsx exports may have different sheet names. Sniffer in `sync.sh` will skip gracefully but India side won't auto-update. User flagged this as **not urgent**.
- `screener.yml` reports as failed in GH Actions list when triggered on `push` event (no `push` trigger defined — GH oddity). Scheduled Sat 01:00 UTC run works fine.

## Useful one-liners

```bash
# Check GH Actions status without gh CLI
curl -s "https://api.github.com/repos/sabarnagchowdhury-ui/portfolio-dashboard/actions/runs?per_page=5" \
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
| `requirements.txt` missing pandas, openpyxl, beautifulsoup4, lxml, html5lib. Local fresh-clone install would crash inside India parser / Saxo builders / screener. | Full deps re-listed in `requirements.txt`. GH Actions still inline-installs its own subset per workflow. |
| `market_data.py` did not persist `fx_rate` into `data/processed/market_indices.json`. `data_audit.py` read None on every run and "healed" from open.er-api.com — every cycle, redundantly. | `market_data.py` now writes `fx_rate = round(live_fx, 4)` into the indices payload when within sanity bounds (70 ≤ rate ≤ 120). Audit heal path becomes a true fallback. |
| `sync.sh` push-retry loop failed silently when working tree had any unstaged change (e.g. someone editing the README). `git pull --rebase` aborts with "cannot pull with rebase: You have unstaged changes" — all 3 retries kept failing, but sync.sh still printed "✓ Done". | Push loop now auto-stashes with `git stash push -u`, pulls + pushes, then pops the stash. Either pushes cleanly or surfaces a real conflict. |

## Interlock map (verified post-audit)

```
inbox/*.pdf       ──► parse_broker_pdf.py        ─► data/holdings_cost.json
inbox/*.xlsx Saxo ──► patch_fees_from_xlsx.py    ─► data/holdings_cost.json (fees)
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
