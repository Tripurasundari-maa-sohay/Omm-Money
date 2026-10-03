#!/usr/bin/env python3
"""Combined US+India monthly series — powers the 5 main-dashboard charts that
used to run off us.monthly (DBG-only, frozen at $13.87 since the Aug-13
DBG liquidation): Monthly P&L Heatmap, Rolling 3-Month Alpha, Portfolio
Drawdown from Peak, Cash Deployed vs Account Value, Realized vs Unrealized
P&L. Built 2026-10-02 at the user's request to replace that frozen DBG-only
pipeline with something that reflects the actual combined US+India account.

Also emits a parallel US-ONLY series (us_cash_deployed/us_monthly_pl/
us_cum_pl/us_port_return_cum_pct/us_realized_cum_pl/us_unrealized_cum_pl),
added 2026-10-03, powering the US tab's own "US PORTFOLIO vs S&P 500" chart
(us-perf-chart). Needed because weekly_chart/daily_chart.port_ret — the
other US-only % series — chains day-over-day through the Jun-Sep gap with
no deposit-adjustment during the resume (same bug class this module was
built to fix), AND its first ~9 weeks (2025-10-03 through early Dec) are
meaningless since us_val_usd was literally $0 then: the user's real first
US dollar landed 2025-12-14 (cash_flows[0]). Caught 2026-10-03 when the
user asked "i started on dec 2025, where did you get the oct 2025 data?"
after a chat answer used weekly_chart's Oct-03 anchor for a performance
comparison. The US-only chain guards against the same zero-AUM distortion:
whenever the prior month's US balance was $0 (true before Dec-2025), the
%-chain re-inits at 0 instead of dividing by zero / swinging on a tiny
denominator (this is what made weekly_chart show +37%/+39% in its first
weeks — a deposit landing on a ~$0 base, not real performance).

snp_return_cum_pct is ALSO fixed here 2026-10-03: previously just copied
weekly_chart.snp_ret forward unchanged (continuous since 2025-10-03, never
reset), while port_return_cum_pct resets to 0 at the Jun-Sep gap boundary —
so post-gap "alpha" (port - snp, e.g. the Rolling 3-Month Alpha chart) was
comparing a <1-month port return against snp's full 12-month-and-counting
figure. Now computed independently per month from real S&P daily closes
(own Yahoo fetch below, same pattern as fetch_all_prices_vm.py's
_yahoo_close_on/fetch_yahoo_meta), reset at the SAME gap boundary as port.
Shared by both the combined and US-only series — one S&P index, no
US/India split needed on that side.

Reads combined_weekly_chart (full history, both legs, back to 2025-10-03)
from holdings_prices.json and holdings_cost.json for known dated cash flows
+ closed-position history. Writes data/processed/combined_monthly.json.

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
counter so the stacked bar's two eras each reconcile internally. Same
pattern applies to the US-only series.

OTHER KNOWN APPROXIMATIONS:
  - monthly_pl/cash_deployed are deposit-adjusted using us.cash_flows (DBG+
    IBKR combined, all 24 entries genuinely dated — fixed 2026-10-02, this
    used to wrongly read IBKR's transfer_log alone on a mistaken assumption
    DBG's flows had no dates). India's lifetime cash_infusion_itd still has
    no dated history ("Cash/transaction history still pending" per
    india.note) and is not spread across months.
  - India closed-position rpnl (INR) uses the CURRENT fx_rate for every
    historical trade (no historical daily FX series available) — same
    simplification already accepted for inr_ret/fx_alpha elsewhere.
  - 61/79 India and 3/69 US closed trades have no sell_date (legacy
    entries); their realized P&L is summed into one "pre-tracking" bucket
    applied at the very first month, not spread across months it can't be
    dated to.
  - combined_weekly_chart's earliest points show us_usd=$0 (US wasn't
    populated in that series until ~Jan-2026) — inherited from upstream,
    not fixed here (the US-only series' own zero-AUM guard, above, is how
    THIS module stays correct despite that upstream gap).
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

PRICES_PATH  = Path("/home/opc/web/portfolio/data/processed/holdings_prices.json")
COST_PATH    = Path("/home/opc/web/portfolio/data/holdings_cost.json")
INDICES_PATH = Path("/home/opc/web/portfolio/data/processed/market_indices.json")
OUT_PATH     = Path("/home/opc/web/portfolio/data/processed/combined_monthly.json")

_YF_UA = {"User-Agent": "Mozilla/5.0"}


def _yahoo_close_on(yf_sym: str, date_iso: str):
    """Historical daily close nearest date_iso. Same pattern as
    fetch_all_prices_vm.py's helper of the same name."""
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
            closes = [c for c in result["indicators"]["quote"][0]["close"] if c is not None]
            if closes:
                return float(closes[-1])
    except Exception as e:
        print(f"  _yahoo_close_on({yf_sym}, {date_iso}): {e}", file=sys.stderr)
    return None


def _yahoo_live(yf_sym: str):
    """Live/last quote. Same pattern as fetch_all_prices_vm.py's fetch_yahoo_meta."""
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
        print(f"  _yahoo_live({yf_sym}): {e}", file=sys.stderr)
    return None


def month_label(d: datetime, seen: set) -> str:
    key = d.strftime("%b-%Y")
    lbl = d.strftime("%b-%y") if key not in seen else d.strftime("%b")
    seen.add(key)
    return lbl


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

    # us.cash_flows (24 entries) — the SAME source holdings_cost.json's XIRR
    # tile already uses, DBG + IBKR combined, every entry genuinely dated.
    # cash_flows uses the XIRR sign convention (negative=deposit,
    # positive=withdrawal) — flipped here to this module's convention
    # (positive=net deposit).
    dated_flows = [(datetime.strptime(f["date"], "%Y-%m-%d"), -float(f.get("amount") or 0))
                   for f in (cost["us"].get("cash_flows") or [])]

    # Combined realized P&L (US+India) and US-only realized P&L, built in
    # parallel from the same source loops so neither drifts from the other.
    pre_tracking_realised, pre_tracking_realised_us = 0.0, 0.0
    dated_realised, dated_realised_us = [], []
    for c in cost["us"].get("closed", []):
        v = float(c.get("realised") or 0.0)
        sd = c.get("sell_date")
        placed = False
        if sd:
            try:
                rd = datetime.strptime(sd[:10], "%Y-%m-%d")
                dated_realised.append((rd, v))
                dated_realised_us.append((rd, v))
                placed = True
            except ValueError:
                pass
        if not placed:
            pre_tracking_realised += v
            pre_tracking_realised_us += v
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
    dated_realised_us.sort(key=lambda x: x[0])

    # ---- group by calendar month using ONLY real data points ----
    buckets = {}
    for d, uv, iv, tv in zip(dates, us_usd, india_usd, total_usd):
        buckets[(d.year, d.month)] = (d, (uv, iv, tv))
    month_keys = sorted(buckets)
    boundaries = [buckets[k][0] for k in month_keys]
    bucket_by_date = {buckets[k][0]: buckets[k][1] for k in buckets}

    # Real S&P close and USD/INR rate at each month boundary — historical for
    # past boundaries, a live quote for the last one (today's daily candle
    # may not exist yet). See module docstring for why this replaced copying
    # weekly_chart.snp_ret. INR added 2026-10-03 to restore a real INR-Return
    # line on the dashboard chart that reads this file (previously computed
    # client-side from a different, length-mismatched monthly series).
    snp_close_by_date, fx_close_by_date = {}, {}
    for i, d in enumerate(boundaries):
        iso = d.strftime("%Y-%m-%d")
        is_last = i == len(boundaries) - 1
        snp_close_by_date[d] = _yahoo_live("^GSPC") if is_last else _yahoo_close_on("^GSPC", iso)
        fx_close_by_date[d]  = _yahoo_live("INR=X") if is_last else _yahoo_close_on("INR=X", iso)

    seen_labels = set()
    out = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "labels": [], "label_dates": [], "gap_before": [],
        "us_usd": [], "india_usd": [], "total_usd": [], "account_value": [],
        "cash_deployed": [], "monthly_pl": [], "cum_pl": [],
        "port_return_cum_pct": [], "snp_return_cum_pct": [], "inr_return_cum_pct": [],
        "realized_cum_pl": [], "unrealized_cum_pl": [],
        "us_cash_deployed": [], "us_monthly_pl": [], "us_cum_pl": [],
        "us_port_return_cum_pct": [], "us_inr_return_cum_pct": [],
        "us_realized_cum_pl": [], "us_unrealized_cum_pl": [],
        "note": ("Combined US+India monthly series (replaces the DBG-only us.monthly "
                 "pipeline for the main-dashboard charts), plus a parallel US-only "
                 "series (us_* fields) for the US tab's own chart. The Jun-Aug 2026 "
                 "stretch has no real data (DBG era, qty lost) and is an explicit gap "
                 "— entries right after a gap (gap_before=true) reset their cumulative-% "
                 "baseline to 0 and have monthly_pl=null rather than a fabricated number "
                 "spanning the gap. snp_return_cum_pct resets at the same gap boundary "
                 "so alpha (port minus snp) stays apples-to-apples. "
                 "See module docstring for all documented approximations."),
    }

    prev_total = prev_us_total = None
    prev_key = None
    cum_pct = us_cum_pct = 0.0
    cum_deployed = cum_deployed_us = 0.0
    cum_pl_running = cum_pl_running_us = 0.0
    realized_since_reset = realized_since_reset_us = 0.0
    prev_boundary = None
    snp_cum, snp_anchor_close = 0.0, None
    fx_anchor_close = None

    for key, d in zip(month_keys, boundaries):
        uv, iv, tv = bucket_by_date[d]
        lbl = month_label(d, seen_labels)
        is_gap = prev_key is not None and next_month(*prev_key) != key
        # US-only zero-AUM guard: before the first real US dollar (2025-12-14),
        # uv is $0 and dividing to get a %-period would either blow up or
        # swing wildly on a near-zero base (this is what made weekly_chart
        # show +37%/+39% in its first weeks). Treat "prior US balance was
        # $0" the same as a gap for the US-only %-chain only.
        is_zero_base_us = not prev_us_total

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
        cum_deployed_us += period_flow  # dated_flows is US-only already
        out["cash_deployed"].append(round(cum_deployed, 2))
        out["us_cash_deployed"].append(round(cum_deployed_us, 2))

        if prev_total is None:
            monthly_pl, period_pct, cum_pct = 0.0, 0.0, 0.0
        elif is_gap:
            monthly_pl = None
            cum_pct = 0.0
            realized_since_reset = 0.0
            cum_pl_running = 0.0
            cum_pl_running_us = 0.0
        else:
            monthly_pl = round(tv - prev_total - period_flow, 2)
            period_pct = (tv - period_flow) / prev_total - 1 if prev_total else 0.0
            cum_pct = ((1 + cum_pct / 100) * (1 + period_pct) - 1) * 100

        # US-only monthly_pl needs no division, so it's safe even when
        # prev_us_total is $0 — the gap/zero-base guard only applies to the
        # %-chain below.
        monthly_pl_us = None if is_gap else round(uv - (prev_us_total or 0.0) - period_flow, 2)
        if is_gap or is_zero_base_us:
            us_cum_pct = 0.0
        else:
            us_period_pct = (uv - period_flow) / prev_us_total - 1
            us_cum_pct = ((1 + us_cum_pct / 100) * (1 + us_period_pct) - 1) * 100

        if monthly_pl is not None:
            cum_pl_running += monthly_pl
        if monthly_pl_us is not None:
            cum_pl_running_us += monthly_pl_us
        out["monthly_pl"].append(monthly_pl)
        out["us_monthly_pl"].append(monthly_pl_us)
        out["cum_pl"].append(round(cum_pl_running, 2) if monthly_pl is not None or prev_total is None else None)
        out["us_cum_pl"].append(round(cum_pl_running_us, 2) if monthly_pl_us is not None or prev_us_total is None else None)
        out["port_return_cum_pct"].append(round(cum_pct, 2))
        out["us_port_return_cum_pct"].append(round(us_cum_pct, 2))

        snp_close = snp_close_by_date.get(d)
        if is_gap or snp_anchor_close is None:
            snp_anchor_close = snp_close
            snp_cum = 0.0
        elif snp_close:
            snp_cum = (snp_close / snp_anchor_close - 1) * 100
        # else: fetch failed for this boundary — hold last value rather than guess
        out["snp_return_cum_pct"].append(round(snp_cum, 2) if snp_anchor_close is not None else None)

        # INR Return = portfolio's own USD return compounded with how much
        # the rupee moved against the dollar since the same era's anchor —
        # same reset boundary as snp_anchor_close above, so port/snp/inr are
        # always comparing the same window.
        fx_close = fx_close_by_date.get(d)
        if is_gap or fx_anchor_close is None:
            fx_anchor_close = fx_close
        fx_ratio = (fx_close / fx_anchor_close) if (fx_anchor_close and fx_close) else None
        out["inr_return_cum_pct"].append(
            round(((1 + cum_pct / 100) * fx_ratio - 1) * 100, 2) if fx_ratio is not None else None)
        out["us_inr_return_cum_pct"].append(
            round(((1 + us_cum_pct / 100) * fx_ratio - 1) * 100, 2) if fx_ratio is not None else None)

        realised_lifetime_to_date = pre_tracking_realised + sum(v for (rd, v) in dated_realised if rd <= d)
        realised_new_this_period = sum(v for (rd, v) in dated_realised
                                        if (prev_boundary is None or rd > prev_boundary) and rd <= d)
        realised_lifetime_to_date_us = pre_tracking_realised_us + sum(v for (rd, v) in dated_realised_us if rd <= d)
        realised_new_this_period_us = sum(v for (rd, v) in dated_realised_us
                                           if (prev_boundary is None or rd > prev_boundary) and rd <= d)
        if prev_total is None:
            realized_since_reset = pre_tracking_realised + realised_new_this_period
        elif is_gap:
            realized_since_reset = realised_new_this_period
        else:
            realized_since_reset += realised_new_this_period
        if prev_us_total is None:
            realized_since_reset_us = pre_tracking_realised_us + realised_new_this_period_us
        elif is_gap:
            realized_since_reset_us = realised_new_this_period_us
        else:
            realized_since_reset_us += realised_new_this_period_us
        out["realized_cum_pl"].append(round(realised_lifetime_to_date, 2))
        out["unrealized_cum_pl"].append(round(cum_pl_running - realized_since_reset, 2))
        out["us_realized_cum_pl"].append(round(realised_lifetime_to_date_us, 2))
        out["us_unrealized_cum_pl"].append(round(cum_pl_running_us - realized_since_reset_us, 2))

        prev_total = tv
        prev_us_total = uv
        prev_boundary = d
        prev_key = key

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2))
    print(f"Wrote {OUT_PATH} — {len(out['labels'])} months, "
          f"{out['labels'][0]}..{out['labels'][-1]}, "
          f"last total_usd=${out['total_usd'][-1]:,.0f}, "
          f"last cum_pl=${out['cum_pl'][-1]:,.0f}, "
          f"last port_return_cum_pct={out['port_return_cum_pct'][-1]}%, "
          f"last us_port_return_cum_pct={out['us_port_return_cum_pct'][-1]}%, "
          f"last snp_return_cum_pct={out['snp_return_cum_pct'][-1]}%")


if __name__ == "__main__":
    main()
