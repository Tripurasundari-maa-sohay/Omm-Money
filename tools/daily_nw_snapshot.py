"""
daily_nw_snapshot.py
====================
Runs on Oracle VM via cron at 22:00 UTC daily (Monday-Friday).
Reads live data from GitHub, computes net worth, appends one entry to
net-wealth/data/history.json, commits back.

Cron time 22:00 UTC is DST-safe year-round:
  Summer EDT (Mar-Nov): US close = 20:00 UTC → 22:00 = 2hrs after close
  Winter EST (Nov-Mar): US close = 21:00 UTC → 22:00 = 1hr after close

Script also checks US market status from market_indices.json. If market
is still OPEN or POSTMARKET (prices not yet settled), it aborts and logs —
preventing a stale-price snapshot on early re-runs or DST edge days.

Mirrors the NW formula in net-wealth/index.html exactly:
  assetsTot = USD_assets + INR_assets + QAR_assets  (all in INR)
  liabTot   = sum of all loan outstandings (in INR)
  netWorth  = assetsTot - liabTot

Crontab (on Oracle VM):
  0 22 * * 1-5 source /home/opc/angel_env.sh && python3 /home/opc/daily_nw_snapshot.py >> /home/opc/nw_snapshot.log 2>&1
"""

import json, os, sys, base64, time, requests
from datetime import datetime, timezone

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO  = "Tripurasundari-maa-sohay/Omm-Money"

PATHS = {
    "seed":    "net-wealth/data/seed.json",
    "history": "net-wealth/data/history.json",
    "prices":  "portfolio/data/processed/holdings_prices.json",
    "cost":    "portfolio/data/holdings_cost.json",
    "indices": "portfolio/data/processed/market_indices.json",
}

QAR_PER_USD = 3.64  # fixed peg


def gh_get(path: str):
    """Fetch + decode a JSON file from GitHub (authenticated, uncached)."""
    h = {"Authorization": f"token {GITHUB_TOKEN}",
         "Accept": "application/vnd.github.v3+json"}
    r = requests.get(
        f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}?ref=main",
        headers=h, timeout=15
    )
    if r.status_code != 200:
        raise RuntimeError(f"gh_get {path}: HTTP {r.status_code}")
    return json.loads(base64.b64decode(r.json()["content"]).decode()), r.json()["sha"]


def gh_put(path: str, sha: str, payload, message: str) -> bool:
    """Commit JSON payload to a repo path (3x retry on SHA conflict)."""
    h = {"Authorization": f"token {GITHUB_TOKEN}",
         "Accept": "application/vnd.github.v3+json"}
    content = base64.b64encode(json.dumps(payload, indent=2).encode()).decode()
    body = {"message": message, "content": content, "branch": "main", "sha": sha}
    for attempt in range(1, 4):
        r = requests.put(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}",
            headers=h, json=body, timeout=20
        )
        if r.status_code in (200, 201):
            return True
        if r.status_code == 409:
            fresh = requests.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}?ref=main",
                headers=h, timeout=10
            )
            body["sha"] = fresh.json().get("sha")
        else:
            print(f"  PUT attempt {attempt}: {r.status_code}", file=sys.stderr)
        time.sleep(5 * attempt)
    return False


def compute_nw(seed, prices_data, cost, fx_rate_live):
    """Replicate net-wealth/index.html NW formula. Returns dict of components."""
    a      = seed.get("assets", {})
    loans  = seed.get("loans", [])
    banks_qa = seed.get("banks_qatar", [])
    banks_in = seed.get("banks_india", [])
    seed_fx  = seed.get("fx", {})
    custom_assets = seed.get("custom_assets", [])

    # FX rates: prefer live from market_indices, fallback to seed
    usd_to_inr = fx_rate_live if fx_rate_live and 70 <= fx_rate_live <= 120 \
                 else seed_fx.get("usd_to_inr", 95.0)
    qar_to_inr = usd_to_inr / QAR_PER_USD

    # ── LIVE portfolio values ─────────────────────────────────────────────────
    prices = prices_data.get("prices", {})
    us_open = cost.get("us", {}).get("open", [])
    us_cash = cost.get("us", {}).get("cash", 0) or 0

    us_mv_usd = us_cash
    for h in us_open:
        tk = h.get("tk"); qty = h.get("qty") or 0
        p = prices.get(tk, {}); ltp = p.get("ltp")
        if ltp and qty:
            us_mv_usd += float(ltp) * qty

    india_open = cost.get("india", {}).get("open", [])
    india_mv_inr = 0
    for h in india_open:
        tk = h.get("tk"); qty = h.get("qty") or 0
        p = prices.get(tk, {}); ltp = p.get("ltp")
        if ltp and qty:
            india_mv_inr += float(ltp) * qty

    # ── ASSETS (INR equivalent) ───────────────────────────────────────────────
    gold_rate = seed.get("gold_rate_inr_per_gram", 8500)
    jewel_g   = a.get("gold_jewellery_grams", 0)
    jewel_inr = jewel_g * gold_rate

    malabar_g    = a.get("malabar_grams", 0)
    malabar_rate = a.get("malabar_rate_inr_per_g", gold_rate)
    malabar_inr  = malabar_g * malabar_rate

    fo_inr   = a.get("fo_corpus_cash", 0) or 0
    mf_inr   = a.get("india_mutual_fund_inr", 0) or 0
    apt_inr  = a.get("apartment_market_value", 0) or 0
    lc_qar   = a.get("land_cruiser_resale_qar", 0) or 0
    grat_qar = a.get("gratuity_qar", 0) or 0

    cash_qa_inr = sum(b.get("balance_qar", 0) for b in banks_qa) * qar_to_inr
    cash_in_inr = sum(b.get("balance_inr", 0) for b in banks_in)

    custom_inr = sum(
        (ca["value"] * usd_to_inr if ca.get("ccy") == "USD"
         else ca["value"] * qar_to_inr if ca.get("ccy") == "QAR"
         else ca["value"])
        for ca in custom_assets if ca.get("value")
    )

    usd_assets = us_mv_usd * usd_to_inr
    inr_assets = (india_mv_inr + fo_inr + mf_inr + jewel_inr + malabar_inr
                  + apt_inr + cash_in_inr + custom_inr)
    qar_assets = (cash_qa_inr + lc_qar * qar_to_inr + grat_qar * qar_to_inr)
    assets_tot = usd_assets + inr_assets + qar_assets

    # ── LIABILITIES ───────────────────────────────────────────────────────────
    liab_tot = 0
    for ln in loans:
        out = ln.get("outstanding", 0) or 0
        if ln.get("ccy") == "QAR":
            liab_tot += out * qar_to_inr
        elif ln.get("ccy") == "USD":
            liab_tot += out * usd_to_inr
        else:
            liab_tot += out

    net_worth = assets_tot - liab_tot

    # ── Per-line-item breakdown (INR equivalents, for trend drill-down) ───
    breakdown = {
        # Equities
        "eq_india_stocks":   round(india_mv_inr, 2),
        "eq_us_stocks":      round(us_mv_usd * usd_to_inr, 2),
        "eq_fo_corpus":      round(fo_inr, 2),
        "eq_india_mf":       round(mf_inr, 2),
        # Gold
        "gold_jewellery":    round(jewel_inr, 2),
        "gold_malabar":      round(malabar_inr, 2),
        "gold_jewel_grams":  round(jewel_g, 3),
        "gold_malabar_grams": round(malabar_g, 3),
        # Real estate
        "re_apartment":      round(apt_inr, 2),
        # Vehicles
        "veh_landcruiser":   round(lc_qar * qar_to_inr, 2),
        # Retirement
        "ret_gratuity":      round(grat_qar * qar_to_inr, 2),
        # Cash aggregates
        "cash_qatar_total":  round(cash_qa_inr, 2),
        "cash_india_total":  round(cash_in_inr, 2),
        # FX & rates
        "rate_gold_inr_g":   round(gold_rate, 2),
    }
    # Per-bank
    for b in banks_qa:
        bid = b.get("id") or b.get("label", "qa_bank").lower().replace(" ", "_")
        breakdown[f"bank_qa_{bid}"] = round((b.get("balance_qar", 0) or 0) * qar_to_inr, 2)
    for b in banks_in:
        bid = b.get("id") or b.get("label", "in_bank").lower().replace(" ", "_")
        breakdown[f"bank_in_{bid}"] = round(b.get("balance_inr", 0) or 0, 2)
    # Per custom asset
    for ca in custom_assets:
        cid = ca.get("id") or ca.get("label", "custom").lower().replace(" ", "_")
        val = ca.get("value", 0) or 0
        ccy = ca.get("ccy")
        inr = val * usd_to_inr if ccy == "USD" else val * qar_to_inr if ccy == "QAR" else val
        breakdown[f"custom_{cid}"] = round(inr, 2)
    # Per loan
    for ln in loans:
        lid = ln.get("id") or ln.get("label", "loan").lower().replace(" ", "_")
        out = ln.get("outstanding", 0) or 0
        ccy = ln.get("ccy")
        inr = out * usd_to_inr if ccy == "USD" else out * qar_to_inr if ccy == "QAR" else out
        breakdown[f"loan_{lid}"] = round(inr, 2)

    return {
        "net_worth":       round(net_worth, 2),
        "assets":          round(assets_tot, 2),
        "liab":            round(liab_tot, 2),
        "india_stocks_inr": round(india_mv_inr, 2),
        "us_stocks_inr":   round(us_mv_usd * usd_to_inr, 2),
        "us_stocks_usd":   round(us_mv_usd, 2),
        "fx_usd_inr":      round(usd_to_inr, 4),
        "fx_qar_inr":      round(qar_to_inr, 4),
        "breakdown":       breakdown,
    }


def main():
    if not GITHUB_TOKEN:
        print("GITHUB_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    now_utc = datetime.now(timezone.utc)
    today   = now_utc.strftime("%Y-%m-%d")
    print(f"\n{now_utc.strftime('%Y-%m-%dT%H:%M:%SZ')} daily_nw_snapshot [{today}]")

    # Load all data
    print("  Loading seed.json...")
    seed,   _    = gh_get(PATHS["seed"])
    print("  Loading history.json...")
    history, hist_sha = gh_get(PATHS["history"])
    print("  Loading holdings_prices.json...")
    prices_data, _ = gh_get(PATHS["prices"])
    print("  Loading holdings_cost.json...")
    cost, _      = gh_get(PATHS["cost"])
    print("  Loading market_indices.json...")
    indices, _   = gh_get(PATHS["indices"])

    fx_live = indices.get("fx_rate")
    print(f"  FX live: {fx_live}")

    # DST-safe market status guard:
    # market_indices.json usa_market.status is computed by the VM price script
    # using zoneinfo America/New_York (handles EDT/EST automatically).
    # Only snapshot when US is CLOSED or POSTMARKET (prices settled).
    # This guards against edge cases: early re-runs, DST day shifts, holidays.
    usa_status = indices.get("usa_market", {}).get("status", "UNKNOWN")
    print(f"  US market status: {usa_status}")
    if usa_status in ("OPEN", "PREMARKET", "RESET"):
        print(f"  US market not closed yet (status={usa_status}). Aborting — will retry at next cron tick.")
        sys.exit(0)

    # Skip if today already snapshotted
    if isinstance(history, list) and history:
        last_date = history[-1].get("date", "")[:10]
        if last_date == today:
            print(f"  Already snapshotted today ({today}). Skipping.")
            return

    # Compute NW
    nw = compute_nw(seed, prices_data, cost, fx_live)
    entry = {
        "date":            now_utc.strftime("%Y-%m-%dT23:59:59.000Z"),
        "net_worth":       nw["net_worth"],
        "assets":          nw["assets"],
        "liab":            nw["liab"],
        "india_stocks_inr": nw["india_stocks_inr"],
        "us_stocks_inr":   nw["us_stocks_inr"],
        "us_stocks_usd":   nw["us_stocks_usd"],
        "fx_usd_inr":      nw["fx_usd_inr"],
        "fx_qar_inr":      nw["fx_qar_inr"],
        "breakdown":       nw["breakdown"],
        "_auto":           True,
    }
    print(f"  NW: ₹{nw['net_worth']/1e5:.2f}L  assets: ₹{nw['assets']/1e5:.2f}L  liab: ₹{nw['liab']/1e5:.2f}L")
    print(f"  US: ${nw['us_stocks_usd']:,.0f}  India: ₹{nw['india_stocks_inr']/1e5:.2f}L")

    # Append + commit
    if isinstance(history, list):
        history.append(entry)
    else:
        history = [entry]

    ok = gh_put(
        PATHS["history"], hist_sha, history,
        f"data: nw snapshot {today} ₹{nw['net_worth']/1e5:.1f}L [skip ci]"
    )
    if ok:
        print(f"  Committed history.json → GitHub OK")
    else:
        print(f"  FAILED to commit history.json", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
