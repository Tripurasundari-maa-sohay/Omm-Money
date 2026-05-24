"""
patch_chart.py — post-process holdings_prices.json to fix us_val_usd.

Runs AFTER market_data.py. Reads broker account_value from holdings_cost.json
and replaces us_val_usd in weekly_chart / combined_weekly_chart with
linear interpolation between verified month-end values.

Also prepends Oct–Nov Fridays with us_val=0 so the combined chart shows
the US portfolio launching from $0 before December deployment.

Pure Python — no yfinance, no network, cannot fail due to rate limits.
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COST_BASIS   = ROOT / "data" / "holdings_cost.json"
OUT_HOLDINGS = ROOT / "data" / "processed" / "holdings_prices.json"

CHART_START = date(2025, 10, 1)   # extend chart back to show $0 pre-portfolio


def _first_friday_on_or_after(d: date) -> date:
    while d.weekday() != 4:
        d += timedelta(days=1)
    return d


def _build_acct_pts(cost: dict) -> list[tuple[str, float]]:
    """Sorted list of (ISO-date, account_value) from broker statements, >$0 only."""
    monthly   = cost.get("us", {}).get("monthly", {})
    ld_list   = monthly.get("label_dates", [])
    av_list   = monthly.get("account_value", [])
    pts = [(ld, av) for ld, av in zip(ld_list, av_list) if ld and av and av > 0]
    return sorted(pts, key=lambda x: x[0])


def _interpolate_us_val(ds: str, acct_pts: list[tuple[str, float]]) -> float:
    """
    Linear interpolation of US portfolio value on date ds.
    Uses nearest bracketing broker month-end account_values.
    Forward-anchors Dec Fridays (before Dec-31 statement) to Dec-31 value.
    """
    prev_pt = nxt_pt = None
    for ld, av in acct_pts:
        if ld <= ds:
            prev_pt = (ld, av)
        elif ld > ds and nxt_pt is None:
            nxt_pt = (ld, av)

    if prev_pt and nxt_pt:
        d0 = date.fromisoformat(prev_pt[0])
        d1 = date.fromisoformat(nxt_pt[0])
        dc = date.fromisoformat(ds)
        frac = (dc - d0).days / (d1 - d0).days
        return round(prev_pt[1] + frac * (nxt_pt[1] - prev_pt[1]), 0)
    elif prev_pt:
        return round(prev_pt[1], 0)
    elif nxt_pt:
        return round(nxt_pt[1], 0)   # forward anchor (pre-first-statement)
    return 0.0


def patch_weekly_chart(weekly: dict, acct_pts: list[tuple[str, float]]) -> dict:
    """
    1. Replace us_val_usd with broker-anchored interpolated values.
    2. Prepend Oct–Nov Fridays with us_val=0 if not already present.
    Returns modified weekly dict.
    """
    if not weekly or not acct_pts:
        return weekly

    # ── Step 1: Replace us_val_usd ─────────────────────────────────────────
    dates = weekly.get("dates", [])
    new_us = [_interpolate_us_val(ds, acct_pts) for ds in dates]
    weekly["us_val_usd"] = new_us
    if new_us:
        print(f"  patch_chart: us_val_usd reanchored  "
              f"Dec={new_us[0]:,.0f} → latest={new_us[-1]:,.0f}")

    # ── Step 2: Prepend Oct–Nov $0 Fridays ─────────────────────────────────
    first_date = date.fromisoformat(dates[0]) if dates else None
    pre_start  = _first_friday_on_or_after(CHART_START)

    if first_date and pre_start < first_date:
        seen_months: set[str] = set()
        pre_labels, pre_dates, pre_us = [], [], []
        pre_zeros = []   # port_ret / snp_ret / inr_ret / fx_alpha all 0

        d = pre_start
        while d < first_date:
            mk = d.strftime("%b-%Y")
            pre_labels.append(d.strftime("%b-%y") if mk not in seen_months else "")
            seen_months.add(mk)
            pre_dates.append(d.isoformat())
            pre_us.append(0)
            pre_zeros.append(0.0)
            d += timedelta(days=7)

        n = len(pre_dates)
        weekly["labels"]    = pre_labels + weekly.get("labels",    [])
        weekly["dates"]     = pre_dates  + weekly.get("dates",     [])
        weekly["us_val_usd"]= pre_us     + weekly["us_val_usd"]
        weekly["port_ret"]  = pre_zeros  + weekly.get("port_ret",  [])
        weekly["snp_ret"]   = pre_zeros  + weekly.get("snp_ret",   [])
        weekly["inr_ret"]   = pre_zeros  + weekly.get("inr_ret",   [])
        weekly["fx_alpha"]  = pre_zeros  + weekly.get("fx_alpha",  [])
        print(f"  patch_chart: prepended {n} Oct–Nov Fridays ($0) to weekly_chart")

    return weekly


def patch_combined_chart(
    combined: dict,
    weekly: dict,
    india_weekly: dict | None = None,
) -> dict:
    """
    Rebuild combined_weekly_chart.us_usd + total_usd from patched weekly.
    Uses india_weekly_chart (has Oct–Nov data) preferring over combined.india_usd.
    """
    if not combined or not weekly:
        return combined

    us_map = dict(zip(weekly.get("dates", []), weekly.get("us_val_usd", [])))

    # Prefer india_weekly_chart (has Oct dates) over combined.india_usd (Dec-only)
    if india_weekly and india_weekly.get("dates"):
        india_map = dict(zip(
            india_weekly.get("dates", []),
            india_weekly.get("india_val_usd", []),
        ))
    else:
        india_map = dict(zip(
            combined.get("dates", []),
            combined.get("india_usd", []),
        ))

    # Common dates present in both
    common = sorted(set(us_map) & set(india_map))
    if not common:
        # Also include Oct–Nov dates where india_usd might exist
        common = sorted(set(us_map) | set(india_map))

    new_labels, new_dates, new_us, new_india, new_total = [], [], [], [], []
    seen_months: set[str] = set()
    for ds in common:
        us_v    = us_map.get(ds, 0) or 0
        india_v = india_map.get(ds)
        if india_v is None:
            continue
        d  = date.fromisoformat(ds)
        mk = d.strftime("%b-%Y")
        new_labels.append(d.strftime("%b-%y") if mk not in seen_months else "")
        seen_months.add(mk)
        new_dates.append(ds)
        new_us.append(round(us_v, 0))
        new_india.append(round(india_v, 2))
        new_total.append(round(us_v + india_v, 0))

    if new_dates:
        combined["labels"]    = new_labels
        combined["dates"]     = new_dates
        combined["us_usd"]    = new_us
        combined["india_usd"] = new_india
        combined["total_usd"] = new_total
        print(f"  patch_chart: combined rebuilt  "
              f"{new_dates[0]} → {new_dates[-1]}  "
              f"total={new_total[-1]:,.0f}")
    return combined


def main() -> int:
    if not OUT_HOLDINGS.exists():
        print("  SKIP  holdings_prices.json not found", file=sys.stderr)
        return 0
    if not COST_BASIS.exists():
        print("  SKIP  holdings_cost.json not found", file=sys.stderr)
        return 0

    try:
        holdings = json.loads(OUT_HOLDINGS.read_text())
        cost     = json.loads(COST_BASIS.read_text())
    except Exception as exc:
        print(f"  ERR   failed to read JSON: {exc}", file=sys.stderr)
        return 1

    acct_pts = _build_acct_pts(cost)
    if not acct_pts:
        print("  SKIP  no account_value data in holdings_cost.json", file=sys.stderr)
        return 0

    print(f"  patch_chart: {len(acct_pts)} broker anchor points  "
          f"{acct_pts[0][0]} → {acct_pts[-1][0]}")

    weekly   = holdings.get("weekly_chart")
    combined = holdings.get("combined_weekly_chart")

    # Patch weekly_chart (skip if Oct–Nov already prepended)
    first_date = weekly.get("dates", [""])[0] if weekly else ""
    already_patched = first_date and first_date <= "2025-10-31"
    if weekly and not already_patched:
        holdings["weekly_chart"] = patch_weekly_chart(weekly, acct_pts)
    elif weekly:
        print("  patch_chart: weekly Oct–Nov already present — skipping patch_weekly")

    # Always rebuild combined_weekly_chart using india_weekly_chart for full Oct range
    if combined:
        holdings["combined_weekly_chart"] = patch_combined_chart(
            combined,
            holdings.get("weekly_chart", {}),
            holdings.get("india_weekly_chart"),
        )

    OUT_HOLDINGS.write_text(json.dumps(holdings, indent=2))
    print(f"  patch_chart: wrote {OUT_HOLDINGS.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
