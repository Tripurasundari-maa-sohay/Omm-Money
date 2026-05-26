"""
post_fetch_audit.py
====================
Runs after market_data.py. Validates holdings_prices.json — NO network calls.
market_data.py already auto-heals pc from candle history.
This script just verifies the output is consistent and complete.
Exits 1 only on truly unrecoverable data (missing prices entirely).
"""

import json, sys
from datetime import datetime, timezone

PRICES_PATH = "portfolio/data/processed/holdings_prices.json"
COST_PATH   = "portfolio/data/holdings_cost.json"

def main():
    try:
        prices_data = json.load(open(PRICES_PATH))
        cost_data   = json.load(open(COST_PATH))
    except Exception as e:
        print(f"::error::post_fetch_audit: failed to load JSON — {e}")
        sys.exit(1)

    prices = prices_data.get("prices", {})
    generated = prices_data.get("generated", "?")

    all_holdings = []
    for h in cost_data.get("us", {}).get("open", []):
        all_holdings.append((h["tk"], h["qty"], "US"))
    for h in cost_data.get("india", {}).get("open", []):
        all_holdings.append((h["tk"], h["qty"], "IN"))

    now = datetime.now(timezone.utc)
    print(f"\n{'='*65}")
    print(f"POST-FETCH AUDIT  {now.strftime('%Y-%m-%dT%H:%M:%SZ')}  (data: {generated})")
    print(f"{'='*65}")
    print(f"  {'TICKER':15s} {'MKT':3s} {'QTY':6s} {'LTP':>10s} {'PC':>10s} {'DAY%':>8s}  STATUS")
    print(f"  {'-'*63}")

    missing_price = []
    missing_pc    = []
    bad_ltp       = []
    ok_count      = 0
    total_us_pl   = 0.0
    total_in_pl   = 0.0

    for tk, qty, mkt in all_holdings:
        p = prices.get(tk)

        if p is None:
            missing_price.append(tk)
            print(f"  {tk:15s} {mkt:3s} {qty:6}  {'NO DATA':>10s}  {'':>10s}  {'':>8s}  ❌ MISSING")
            continue

        ltp = p.get("ltp")
        pc  = p.get("pc")

        if not ltp or ltp <= 0:
            bad_ltp.append(tk)
            print(f"  {tk:15s} {mkt:3s} {qty:6}  {str(ltp):>10s}  {'':>10s}  {'':>8s}  ❌ BAD LTP")
            continue

        if pc is None:
            missing_pc.append(tk)
            print(f"  {tk:15s} {mkt:3s} {qty:6}  {ltp:>10.2f}  {'None':>10s}  {'---':>8s}  ⚠  pc=None (big mover / data gap)")
            continue

        day_pct = (ltp - pc) / pc * 100
        day_pl  = (ltp - pc) * qty
        if mkt == "US":
            total_us_pl  += day_pl
        else:
            total_in_pl  += day_pl

        flag = "✅"
        if abs(day_pct) > 30 and mkt == "US":
            flag = "⚠  >30% US move — verify"
        if abs(day_pct) > 20 and mkt == "IN":
            flag = "⚠  >20% IN move — verify"

        ok_count += 1
        print(f"  {tk:15s} {mkt:3s} {qty:6}  {ltp:>10.2f}  {pc:>10.2f}  {day_pct:>+7.2f}%  {flag}  pl={day_pl:+.2f}")

    fx = cost_data.get("fx_inr_usd", 95.0)
    total_combined_usd = total_us_pl + (total_in_pl / fx)

    print(f"\n  {'─'*63}")
    print(f"  US  day P&L:  ${total_us_pl:+.2f}")
    print(f"  IN  day P&L:  ₹{total_in_pl:+.2f}  (${total_in_pl/fx:+.2f})")
    print(f"  COMBINED:     ${total_combined_usd:+.2f}")
    print(f"\n{'='*65}")
    print(f"  Total: {len(all_holdings)}  OK: {ok_count}  pc=None: {len(missing_pc)}  Missing: {len(missing_price)}  Bad: {len(bad_ltp)}")

    # Warn about pc=None but don't fail — market_data.py already tried to heal
    if missing_pc:
        print(f"\n  ⚠  pc=None (excluded from dayPL): {', '.join(missing_pc)}")
        print(f"     These are big movers or data gaps — check holdings manually")
        for w in missing_pc:
            print(f"  ::warning::pc=None for {w} — dayPL excluded")

    # Only hard fail on unrecoverable issues (no price at all)
    if missing_price or bad_ltp:
        unrecoverable = missing_price + bad_ltp
        print(f"\n  ❌ UNRECOVERABLE: {unrecoverable}")
        print(f"  ::error::post_fetch_audit FAILED — {len(unrecoverable)} position(s) have no usable price data")
        sys.exit(1)

    print(f"\n  ✅ Audit passed\n")

if __name__ == "__main__":
    main()
