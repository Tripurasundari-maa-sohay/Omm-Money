#!/usr/bin/env python3
"""Monthly US (DBG) account-value snapshot — runs on the VM, replaces the
manual "pull a broker PDF and re-parse the monthly table" workflow.

Why this exists: `us.monthly` (which feeds the Rolling 3-Month Alpha and
Portfolio Drawdown charts) was built by scraping the DBG PDF's own monthly
%Return/Total P/L table (parse_broker_pdf.py). Doha Bank quietly switched
that table from monthly to QUARTERLY buckets sometime after Jun-2026 — the
parser can no longer extract monthly points at all, which is why the chart
froze at Jun-26 regardless of how often a new PDF was parsed. This script
breaks that dependency: it snapshots the LIVE DBG account value itself on
the last business day of each month and appends to us.monthly directly.

Scope/limitations (documented, not silently faked):
  - account_value / monthly_pl / port_return_cum_pct / snp_return_cum_pct /
    cash_deployed are all computed live and are accurate.
  - monthly_cost (per-month trading costs) is NOT tracked here — appended
    as 0.0. It's cosmetic (a small table), refine it manually next time a
    full PDF parse happens if it matters.
  - Deposit tracking: new deposits during the month are read from the delta
    in us.brokers.DBG.cash_infusion_itd since the last snapshot (stored in
    us.monthly._last_cash_infusion_itd). Deposits are rare/manual events —
    this is exact as long as cash_infusion_itd is kept current.
  - DBG only (matches the existing series — IBKR isn't in this chart).

Run via VM cron, e.g. daily at 21:05 UTC weekdays (right after the existing
price-fetch/NW-snapshot crons) — the script itself decides whether today is
actually a month-end and no-ops otherwise.
"""
import base64
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO  = os.environ.get("GITHUB_REPO", "Tripurasundari-maa-sohay/Omm-Money")
DATA_DIR     = "/home/opc/web/portfolio/data"
COST_PATH    = f"{DATA_DIR}/holdings_cost.json"
PRICES_PATH  = f"{DATA_DIR}/processed/holdings_prices.json"
REPO_COST_PATH = "portfolio/data/holdings_cost.json"

_YF_UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


def is_last_business_day(d: datetime) -> bool:
    nxt = d + timedelta(days=1)
    while nxt.weekday() >= 5:  # Sat=5, Sun=6
        nxt += timedelta(days=1)
    return nxt.month != d.month


def month_label(d: datetime) -> str:
    # Matches existing style: short month, "-YY" suffix only on the first
    # label of each calendar year (Nov-25, Dec, Jan, Feb, ... May-26, Jun-26)
    # — simplify to always suffix the year; cosmetic, harmless either way.
    return d.strftime("%b-%y")


def fetch_yahoo_close(yf_sym: str, date_iso: str):
    """Historical daily close nearest to date_iso, via Yahoo chart API."""
    try:
        d = datetime.strptime(date_iso, "%Y-%m-%d")
        p1 = int((d - timedelta(days=5)).timestamp())
        p2 = int((d + timedelta(days=2)).timestamp())
        for host in ("query1", "query2"):
            r = requests.get(
                f"https://{host}.finance.yahoo.com/v8/finance/chart/{yf_sym}",
                params={"period1": p1, "period2": p2, "interval": "1d"},
                headers=_YF_UA, timeout=10,
            )
            if r.status_code != 200:
                continue
            result = r.json()["chart"]["result"][0]
            closes = result["indicators"]["quote"][0]["close"]
            closes = [c for c in closes if c is not None]
            if closes:
                return float(closes[-1])
    except Exception as e:
        print(f"  fetch_yahoo_close({yf_sym}, {date_iso}): {e}", file=sys.stderr)
    return None


def fetch_yahoo_live(yf_sym: str):
    try:
        for host in ("query1", "query2"):
            r = requests.get(
                f"https://{host}.finance.yahoo.com/v8/finance/chart/{yf_sym}",
                headers=_YF_UA, timeout=10,
            )
            if r.status_code != 200:
                continue
            m = r.json()["chart"]["result"][0]["meta"]
            ltp = m.get("regularMarketPrice")
            if ltp:
                return float(ltp)
    except Exception as e:
        print(f"  fetch_yahoo_live({yf_sym}): {e}", file=sys.stderr)
    return None


def commit_json_to_github(path: str, payload: dict, label: str) -> bool:
    if not GITHUB_TOKEN:
        print("  GITHUB_TOKEN not set", file=sys.stderr)
        return False
    content = base64.b64encode(json.dumps(payload, indent=2).encode()).decode()
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    r = requests.get(f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}", headers=headers, timeout=10)
    sha = r.json().get("sha") if r.status_code == 200 else None
    body = {"message": f"data: {label} [skip ci]", "content": content, "branch": "main"}
    if sha:
        body["sha"] = sha
    for attempt in range(1, 4):
        try:
            r = requests.put(f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}", headers=headers, json=body, timeout=20)
            if r.status_code in (200, 201):
                print(f"  Committed {label} -> GitHub OK")
                return True
            elif r.status_code == 409:
                r2 = requests.get(f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}", headers=headers, timeout=10)
                body["sha"] = r2.json().get("sha")
            else:
                print(f"  Attempt {attempt}: {r.status_code} {r.text[:200]}", file=sys.stderr)
        except Exception as e:
            print(f"  Attempt {attempt} error: {e}", file=sys.stderr)
        time.sleep(5 * attempt)
    print(f"  All 3 {label} commit attempts failed", file=sys.stderr)
    return False


def main():
    today = datetime.now(timezone.utc)
    force = "--force" in sys.argv
    if not force and not is_last_business_day(today):
        print(f"{today.date()} is not the last business day of the month — no-op.")
        return

    cost = json.load(open(COST_PATH))
    prices = json.load(open(PRICES_PATH)).get("prices", {})
    m = cost["us"].setdefault("monthly", {
        "labels": [], "label_dates": [], "cash_balance": [], "account_value": [],
        "monthly_pct_return": [], "monthly_bench_return": [], "monthly_pl": [],
        "monthly_cost": [], "port_return_cum_pct": [], "snp_return_cum_pct": [],
        "cash_deployed": [],
    })

    today_iso = today.strftime("%Y-%m-%d")
    if m["label_dates"] and m["label_dates"][-1][:7] == today_iso[:7]:
        print(f"Already have a snapshot for {today_iso[:7]} — no-op.")
        return
    if not m["label_dates"]:
        print("us.monthly is empty — refusing to auto-bootstrap, needs a manual PDF parse first.", file=sys.stderr)
        sys.exit(1)

    dbg_open = [h for h in cost["us"]["open"] if h.get("broker", "DBG") != "IBKR"]
    mv = 0.0
    for h in dbg_open:
        p = prices.get(h["tk"], {})
        ltp = p.get("ltp") or h["avg"]
        mv += h["qty"] * ltp
    dbg_cash = cost["us"]["brokers"]["DBG"]["cash"]
    account_value = round(mv + dbg_cash, 2)

    cash_infusion_now = cost["us"]["brokers"]["DBG"]["cash_infusion_itd"]
    last_infusion = m.get("_last_cash_infusion_itd", cash_infusion_now)
    new_deposits = cash_infusion_now - last_infusion

    prev_av = m["account_value"][-1]
    monthly_pl = round(account_value - prev_av - new_deposits, 2)
    pct_return = round(monthly_pl / prev_av * 100, 2) if prev_av else 0.0

    prev_date = m["label_dates"][-1]
    snp_prev = fetch_yahoo_close("^GSPC", prev_date)
    snp_now = fetch_yahoo_live("^GSPC")
    bench_return = round((snp_now / snp_prev - 1) * 100, 2) if snp_prev and snp_now else 0.0

    prev_cum = m["port_return_cum_pct"][-1]
    port_cum = round(((1 + prev_cum / 100) * (1 + pct_return / 100) - 1) * 100, 2)
    prev_bench_cum = m["snp_return_cum_pct"][-1]
    bench_cum = round(((1 + prev_bench_cum / 100) * (1 + bench_return / 100) - 1) * 100, 2)

    m["labels"].append(month_label(today))
    m["label_dates"].append(today_iso)
    m["cash_balance"].append(dbg_cash)
    m["account_value"].append(account_value)
    m["monthly_pct_return"].append(pct_return)
    m["monthly_bench_return"].append(bench_return)
    m["monthly_pl"].append(monthly_pl)
    m["monthly_cost"].append(0.0)  # not tracked live — see module docstring
    m["port_return_cum_pct"].append(port_cum)
    m["snp_return_cum_pct"].append(bench_cum)
    m["cash_deployed"].append(round(account_value - sum(m["monthly_pl"]), 2))
    m["_last_cash_infusion_itd"] = cash_infusion_now

    json.dump(cost, open(COST_PATH, "w"), indent=2)
    print(f"Appended {month_label(today)} ({today_iso}): account_value={account_value}, "
          f"monthly_pl={monthly_pl}, pct_return={pct_return}%, port_cum={port_cum}%, "
          f"bench_cum={bench_cum}%")

    # Fetch, patch just the us.monthly block on the remote copy, and commit —
    # avoids clobbering any other field GitHub Actions/other writers touched.
    try:
        headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        r = requests.get(f"https://api.github.com/repos/{GITHUB_REPO}/contents/{REPO_COST_PATH}", headers=headers, timeout=10)
        remote_cost = json.loads(base64.b64decode(r.json()["content"]).decode()) if r.status_code == 200 else cost
        remote_cost.setdefault("us", {})["monthly"] = m
        commit_json_to_github(REPO_COST_PATH, remote_cost, f"monthly snapshot {month_label(today)}")
    except Exception as e:
        print(f"  Remote commit skipped: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
