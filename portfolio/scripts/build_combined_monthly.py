#!/usr/bin/env python3
"""Combined US+India monthly series — powers the 5 main-dashboard charts that
used to run off us.monthly (DBG-only, frozen at $13.87 since the Aug-13
DBG liquidation): Monthly P&L Heatmap, Rolling 3-Month Alpha, Portfolio
Drawdown from Peak, Cash Deployed vs Account Value, Realized vs Unrealized
P&L. Built 2026-10-02 at the user's request to replace that frozen DBG-only
pipeline with something that reflects the actual combined US+India account.

Reads combined_weekly_chart (full history, both legs, back to 2025-10-03)
from holdings_prices.json, weekly_chart.snp_ret for the S&P comparison, and
holdings_cost.json for known dated cash flows + closed-position history.
Writes data/processed/combined_monthly.json.

GAP HANDLING (confirmed with user 2026-10-02, see [[portfolio-weekly-chart-gap]]):
the Jun12-Sep5 stretch has no real weekly data at all (DBG held 9 open
positions with lost qty data; the known $2k/$14k/$8k IBKR transfers in that
window are DBG liquidation proceeds in transit, not fresh capital — an
earlier attempt to deposit-adjust across it produced a false -94.65% "crash"
from double-counting that transit money as a withdrawal). Decision: GAP the
whole window — no monthly_pl / port_return claim spans it. The first real
month after a gap gets monthly_pl=None and resets its cumulative % chain to
0 (a fresh baseline), rather than silently chaining through data we don't
trust. cash_deployed still advances through a gap (dated IBKR transfers are
real regardless of the valuation question). realized_cum_pl is tracked as a
true lifetime total (closed trades are real, independent of the valuation
gap); unrealized_cum_pl is derived against a "since last reset" realized
counter so the stacked bar's two eras each reconcile internally.

OTHER KNOWN APPROXIMATIONS:
  - Only IBKR's dated transfer_log entries adjust monthly_pl/cash_deployed.
    DBG's lifetime net flow and India's lifetime cash_infusion_itd have no
    dated history ("Cash/transaction history still pending" per india.note)
    and are not spread across months.
  - India closed-position rpnl (INR) uses the CURRENT fx_rate for every
    historical trade (no historical daily FX series available) — same
    simplification already accepted for inr_ret/fx_alpha elsewhere.
  - 61/79 India and 3/69 US closed trades have no sell_date (legacy
    entries); their realized P&L is summed into one "pre-tracking" bucket
    applied at the very first month, not spread across months it can't be
    dated to.
  - combined_weekly_chart's earliest points show us_usd=$0 (US wasn't
    populated in that series until ~Jan-2026) — inherited from upstream,
    not fixed here.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

PRICES_PATH = Path("/home/opc/web/portfolio/data/processed/holdings_prices.json")
COST_PATH   = Path("/home/opc/web/portfolio/data/holdings_cost.json")
INDICES_PATH = Path("/home/opc/web/portfolio/data/processed/market_indices.json")
OUT_PATH    = Path("/home/opc/web/portfolio/data/processed/combined_monthly.json")


def month_label(d: datetime, seen: set) -> str:
    key = d.strftime("%b-%Y")
    lbl = d.strftime("%b-%y") if key not in seen else d.strftime("%b")
    seen.add(key)
    return lbl


def nearest_at_or_before(dates, values, cutoff):
    best = None
    for d, v in zip(dates, values):
        if d <= cutoff:
            best = (d, v)
        else:
            break
    return best


def next_month(y, m):
    return (y + 1, 1) if m == 12 else (y, m + 1)


def main():
    prices = json.loads(PRICES_PATH.read_text())
    cost = json.loads(COST_PATH.read_text())
    try:
        fx_rate = json.loads(INDICES_PATH.read_text()).get("fx_rate") or 96.0
    except Exception:
        fx_rate = 96.0

    cwc = prices["combined_weekly_chart"]
    dates = [datetime.strptime(d, "%Y-%m-%d") for d in cwc["dates"]]
    us_usd, india_usd, total_usd = cwc["us_usd"], cwc["india_usd"], cwc["total_usd"]

    snp_series = prices.get("weekly_chart", {})
    snp_dates = [datetime.strptime(d, "%Y-%m-%d") for d in snp_series.get("dates", [])]
    snp_ret = snp_series.get("snp_ret", [])

    transfer_log = (cost["us"]["brokers"].get("IBKR") or {}).get("transfer_log") or []
    dated_flows = [(datetime.strptime(t["date"], "%Y-%m-%d"), float(t.get("amount_usd") or 0))
                   for t in transfer_log]

    pre_tracking_realised = 0.0
    dated_realised = []
    for c in cost["us"].get("closed", []):
        v = float(c.get("realised") or 0.0)
        sd = c.get("sell_date")
        placed = False
        if sd:
            try:
                dated_realised.append((datetime.strptime(sd[:10], "%Y-%m-%d"), v))
                placed = True
            except ValueError:
                pass
        if not placed:
            pre_tracking_realised += v
    for c in cost["india"].get("closed", []):
        v = float(c.get("rpnl") or 0.0) / fx_rate
        sd = c.get("sell_date")
        placed = False
        if sd:
            try:
                dated_realised.append((datetime.strptime(sd[:10], "%Y-%m-%d"), v))
                placed = True
            except ValueError:
                pass
        if not placed:
            pre_tracking_realised += v
    dated_realised.sort(key=lambda x: x[0])

    # ---- group by calendar month using ONLY real data points ----
    buckets = {}
    for d, uv, iv, tv in zip(dates, us_usd, india_usd, total_usd):
        buckets[(d.year, d.month)] = (d, (uv, iv, tv))
    month_keys = sorted(buckets)
    boundaries = [buckets[k][0] for k in month_keys]
    bucket_by_date = {buckets[k][0]: buckets[k][1] for k in buckets}

    seen_labels = set()
    out = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "labels": [], "label_dates": [], "gap_before": [],
        "us_usd": [], "india_usd": [], "total_usd": [], "account_value": [],
        "cash_deployed": [], "monthly_pl": [], "cum_pl": [],
        "port_return_cum_pct": [], "snp_return_cum_pct": [],
        "realized_cum_pl": [], "unrealized_cum_pl": [],
        "note": ("Combined US+India monthly series (replaces the DBG-only us.monthly "
                 "pipeline for the main-dashboard charts). The Jun-Aug 2026 stretch has "
                 "no real data (DBG era, qty lost) and is an explicit gap — entries right "
                 "after a gap (gap_before=true) reset their cumulative-% baseline to 0 and "
                 "have monthly_pl=null rather than a fabricated number spanning the gap. "
                 "See module docstring for all documented approximations."),
    }

    prev_total = None
    prev_key = None
    cum_pct = 0.0
    cum_deployed = 0.0
    cum_pl_running = 0.0
    realized_since_reset = 0.0
    prev_boundary = None

    for key, d in zip(month_keys, boundaries):
        uv, iv, tv = bucket_by_date[d]
        lbl = month_label(d, seen_labels)
        is_gap = prev_key is not None and next_month(*prev_key) != key

        out["labels"].append(lbl)
        out["label_dates"].append(d.strftime("%Y-%m-%d"))
        out["gap_before"].append(bool(is_gap))
        out["us_usd"].append(round(uv, 2))
        out["india_usd"].append(round(iv, 2))
        out["total_usd"].append(round(tv, 2))
        out["account_value"].append(round(tv, 2))

        period_flow = sum(amt for (fd, amt) in dated_flows
                           if (prev_boundary is None or fd > prev_boundary) and fd <= d)
        cum_deployed += period_flow
        out["cash_deployed"].append(round(cum_deployed, 2))

        if prev_total is None:
            # very first point overall
            monthly_pl = 0.0
            period_pct = 0.0
            cum_pct = 0.0
        elif is_gap:
            # explicit gap: no performance claim spans it — reset baseline
            monthly_pl = None
            cum_pct = 0.0
            realized_since_reset = 0.0
            cum_pl_running = 0.0  # fresh era starts at 0 P&L too
        else:
            monthly_pl = round(tv - prev_total - period_flow, 2)
            period_pct = (tv - period_flow) / prev_total - 1 if prev_total else 0.0
            cum_pct = ((1 + cum_pct / 100) * (1 + period_pct) - 1) * 100

        if monthly_pl is not None:
            cum_pl_running += monthly_pl
        out["monthly_pl"].append(monthly_pl)
        out["cum_pl"].append(round(cum_pl_running, 2) if monthly_pl is not None or prev_total is None else None)
        out["port_return_cum_pct"].append(round(cum_pct, 2))

        snp_hit = nearest_at_or_before(snp_dates, snp_ret, d)
        out["snp_return_cum_pct"].append(round(snp_hit[1], 2) if snp_hit else None)

        realised_lifetime_to_date = pre_tracking_realised + sum(v for (rd, v) in dated_realised if rd <= d)
        realised_new_this_period = sum(v for (rd, v) in dated_realised
                                        if (prev_boundary is None or rd > prev_boundary) and rd <= d)
        if prev_total is None:
            realized_since_reset = pre_tracking_realised + realised_new_this_period
        elif is_gap:
            realized_since_reset = realised_new_this_period  # already reset to 0 above, add this period's
        else:
            realized_since_reset += realised_new_this_period
        out["realized_cum_pl"].append(round(realised_lifetime_to_date, 2))
        out["unrealized_cum_pl"].append(round(cum_pl_running - realized_since_reset, 2))

        prev_total = tv
        prev_boundary = d
        prev_key = key

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2))
    print(f"Wrote {OUT_PATH} — {len(out['labels'])} months, "
          f"{out['labels'][0]}..{out['labels'][-1]}, "
          f"last total_usd=${out['total_usd'][-1]:,.0f}, "
          f"last cum_pl=${out['cum_pl'][-1]:,.0f}, "
          f"last port_return_cum_pct={out['port_return_cum_pct'][-1]}%")


if __name__ == "__main__":
    main()
