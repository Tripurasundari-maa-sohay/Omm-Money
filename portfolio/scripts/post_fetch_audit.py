"""
post_fetch_audit.py
====================
Runs after market_data.py in every GitHub Actions cycle.
Validates all open positions have correct pc, ltp, dayPL.
Auto-heals any missing/wrong pc from candle history.
Fails loudly (exit 1) if anything can't be healed — blocks bad data from committing.
"""

import json, sys, os
from datetime import datetime, timezone
import yfinance as yf

PRICES_PATH = "portfolio/data/processed/holdings_prices.json"
COST_PATH   = "portfolio/data/holdings_cost.json"
MAX_DIFF_PCT = 1.0   # pc vs candle must agree within 1%
HEALED = []
FAILED = []
WARNINGS = []

def load_json(path):
    with open(path) as f:
        return json.load(f)

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def candle_pc(yf_sym):
    """Return (ltp, pc) from 5d candle history, or (None, None) on failure."""
    try:
        hist = yf.Ticker(yf_sym).history(period="5d", interval="1d", auto_adjust=False)
        if hist.empty:
            return None, None
        today = datetime.now(timezone.utc).date()
        last_date = (hist.index[-1].date() if hasattr(hist.index[-1], "date")
                     else hist.index[-1].to_pydatetime().date())
        if last_date == today and len(hist) >= 2:
            return round(float(hist["Close"].iloc[-1]), 4), round(float(hist["Close"].iloc[-2]), 4)
        return round(float(hist["Close"].iloc[-1]), 4), None
    except Exception as e:
        return None, None

def main():
    prices_data = load_json(PRICES_PATH)
    cost_data   = load_json(COST_PATH)
    prices      = prices_data.get("prices", {})

    # Collect all open holdings (US + India)
    all_holdings = []
    for h in cost_data.get("us", {}).get("open", []):
        all_holdings.append((h["tk"], h["yf"], h["qty"], "US"))
    for h in cost_data.get("india", {}).get("open", []):
        all_holdings.append((h["tk"], h["yf"], h["qty"], "IN"))

    print(f"\n{'='*60}")
    print(f"POST-FETCH AUDIT  {datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')}")
    print(f"{'='*60}")
    print(f"{'TICKER':15s} {'MKT':3s} {'QTY':5s} {'LTP':>9s} {'PC':>9s} {'DAY%':>7s} {'STATUS'}")
    print(f"{'-'*65}")

    modified = False

    for tk, yf_sym, qty, mkt in all_holdings:
        p = prices.get(tk)

        if p is None:
            FAILED.append(f"{tk}: NO PRICE DATA in holdings_prices.json")
            print(f"  {tk:15s} {mkt:3s} {qty:5}  {'?':>9s}  {'?':>9s}  {'?':>7s}  ❌ MISSING")
            continue

        ltp = p.get("ltp")
        pc  = p.get("pc")

        # ── Check 1: ltp must exist and be positive ──
        if not ltp or ltp <= 0:
            FAILED.append(f"{tk}: invalid ltp={ltp}")
            print(f"  {tk:15s} {mkt:3s} {qty:5}  {str(ltp):>9s}  {'?':>9s}  {'?':>7s}  ❌ BAD LTP")
            continue

        # ── Check 2: pc must exist ──
        if pc is None:
            print(f"  {tk:15s} {mkt:3s} {qty:5}  {ltp:>9.2f}  {'None':>9s}  {'?':>7s}  ⚠ pc=None → healing...", end="")
            sys.stdout.flush()
            c_ltp, c_pc = candle_pc(yf_sym)
            if c_pc:
                prices[tk]["pc"] = c_pc
                # Recompute change fields
                prices[tk]["change"]     = round(ltp - c_pc, 4)
                prices[tk]["change_pct"] = round((ltp - c_pc) / c_pc * 100, 2)
                HEALED.append(f"{tk}: pc=None → {c_pc}")
                modified = True
                pct = (ltp - c_pc) / c_pc * 100
                print(f" healed pc={c_pc} ({pct:+.2f}%)")
                pc = c_pc
            else:
                FAILED.append(f"{tk}: pc=None and candle healing failed")
                print(f" ❌ FAILED to heal")
                continue

        # ── Check 3: pc vs candle cross-check ──
        c_ltp, c_pc = candle_pc(yf_sym)
        if c_pc and abs(pc - c_pc) / c_pc > (MAX_DIFF_PCT / 100):
            diff_pct = (pc - c_pc) / c_pc * 100
            print(f"  {tk:15s} {mkt:3s} {qty:5}  {ltp:>9.2f}  {pc:>9.2f}  {'?':>7s}  ⚠ pc mismatch {diff_pct:+.1f}% → healing to {c_pc}")
            prices[tk]["pc"]         = c_pc
            prices[tk]["change"]     = round(ltp - c_pc, 4)
            prices[tk]["change_pct"] = round((ltp - c_pc) / c_pc * 100, 2)
            HEALED.append(f"{tk}: api_pc={pc} → candle_pc={c_pc} ({diff_pct:+.1f}% fix)")
            modified = True
            pc = c_pc

        # ── Check 4: day P&L must be computable ──
        day_pl = (ltp - pc) * qty
        day_pct = (ltp - pc) / pc * 100

        status = "✅ OK"
        if abs(day_pct) > 30 and not yf_sym.endswith((".NS", ".BO")):
            WARNINGS.append(f"{tk}: large US move {day_pct:+.1f}% — verify no split/ex-div")
            status = f"⚠ large move"
        if abs(day_pct) > 20 and yf_sym.endswith((".NS", ".BO")):
            WARNINGS.append(f"{tk}: large India move {day_pct:+.1f}% — verify no split/ex-div")
            status = f"⚠ large move"

        print(f"  {tk:15s} {mkt:3s} {qty:5}  {ltp:>9.2f}  {pc:>9.2f}  {day_pct:>+6.2f}%  {status}  dayPL={day_pl:+.2f}")

    # ── Save if healed ──
    if modified:
        prices_data["prices"] = prices
        prices_data["generated"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
        save_json(PRICES_PATH, prices_data)
        print(f"\n✅ Auto-healed {len(HEALED)} issue(s) — holdings_prices.json updated")
        for h in HEALED:
            print(f"   HEALED: {h}")

    print(f"\n{'='*60}")
    print(f"AUDIT SUMMARY")
    print(f"  Total positions:  {len(all_holdings)}")
    print(f"  Healed:           {len(HEALED)}")
    print(f"  Warnings:         {len(WARNINGS)}")
    print(f"  Failures:         {len(FAILED)}")

    if WARNINGS:
        print(f"\nWARNINGS:")
        for w in WARNINGS: print(f"  ⚠ {w}")

    if FAILED:
        print(f"\nFAILURES (action required):")
        for f in FAILED: print(f"  ❌ {f}")
        print(f"\n::error::Post-fetch audit FAILED — {len(FAILED)} position(s) have bad data")
        sys.exit(1)

    print(f"\n✅ All {len(all_holdings)} positions validated\n")

if __name__ == "__main__":
    main()
