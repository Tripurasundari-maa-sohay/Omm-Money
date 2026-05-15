"""
screener.py — full-universe signal screener for the portfolio dashboard.

Scans the S&P 500 + Nasdaq 100 using the same scoring model as signals_update.py.
Writes: data/processed/screener.json

Runs daily via GitHub Actions (.github/workflows/screener.yml).
The dashboard Screener tab reads this JSON — zero browser-side computation,
zero Yahoo Finance fetches from the user's device.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

# ── PATH SETUP ───────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).parent))

# Import scoring functions from signals_update.py
from signals_update import fetch_history, score_ticker, MIN_BARS_FOR_SIGNALS, sma

OUT_SCREENER = ROOT / "data" / "processed" / "screener.json"


# ── UNIVERSE FETCH ────────────────────────────────────────────────────────────

def get_sp500_tickers() -> list[str]:
    try:
        tables = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
        tickers = tables[0]["Symbol"].tolist()
        return [t.replace(".", "-") for t in tickers]
    except Exception as e:
        print(f"WARN SP500 fetch failed: {e}", file=sys.stderr)
        return []


def get_ndx100_tickers() -> list[str]:
    try:
        tables = pd.read_html("https://en.wikipedia.org/wiki/Nasdaq-100")
        for t in tables:
            if "Ticker" in t.columns:
                return [
                    tk.replace(".", "-")
                    for tk in t["Ticker"].tolist()
                    if isinstance(tk, str) and tk != "Ticker"
                ]
        return []
    except Exception as e:
        print(f"WARN NDX100 fetch failed: {e}", file=sys.stderr)
        return []


# ── MAIN ─────────────────────────────────────────────────────────────────────

def main() -> int:
    print("Fetching universe tickers…")
    sp500  = get_sp500_tickers()
    ndx100 = get_ndx100_tickers()

    sp500_set  = set(sp500)
    ndx100_set = set(ndx100)

    # Build combined list preserving order (SP500 first, then NDX100-only additions)
    seen: dict[str, list[str]] = {}
    for tk in sp500:
        seen.setdefault(tk, []).append("SP500")
    for tk in ndx100:
        seen.setdefault(tk, []).append("NDX100")

    all_tickers = list(seen.keys())
    total = len(all_tickers)
    print(f"Universe: {len(sp500_set)} SP500 + {len(ndx100_set)} NDX100 → {total} unique tickers")

    # ── SPY benchmark ─────────────────────────────────────────────────────────
    print("Fetching SPY benchmark…")
    spy_result = fetch_history("SPY")
    spy_cl     = spy_result[1] if spy_result else None

    if spy_cl is None:
        print("  WARN  SPY fetch failed — regime defaulting to BULL", file=sys.stderr)

    spy_summary: dict = {}
    regime = "BULL"
    if spy_cl:
        spy_px    = spy_cl[-1]
        spy_ma200 = sma(spy_cl, 200)
        pct       = round((spy_px - spy_ma200) / spy_ma200 * 100, 1)
        regime    = "BULL" if spy_px > spy_ma200 else "BEAR"
        spy_summary = {"px": round(spy_px, 2), "ma200": round(spy_ma200, 2), "pct_vs_200": pct}
        print(f"  SPY → ${spy_px:.2f}  vs 200MA {pct:+.1f}%  [{regime}]")

    # ── Score each ticker ─────────────────────────────────────────────────────
    results: list[dict] = []
    errors = 0

    for i, tk in enumerate(all_tickers):
        indices = seen[tk]
        try:
            data = fetch_history(tk)
            if data is None:
                print(f"[{i+1}/{total}] {tk} → no data (skipped)")
                errors += 1
                time.sleep(0.3)
                continue

            ts, cl, vol = data
            if len(cl) < MIN_BARS_FOR_SIGNALS:
                print(
                    f"[{i+1}/{total}] {tk} → only {len(cl)} bars "
                    f"(need ≥{MIN_BARS_FOR_SIGNALS}) — skipped",
                    file=sys.stderr,
                )
                errors += 1
                time.sleep(0.3)
                continue

            res = score_ticker(ts, cl, vol, spy_cl)
            score  = res["score"]
            action = res["action"]
            print(f"[{i+1}/{total}] {tk} → score={score} [{action}]")

            results.append({
                "tk":      tk,
                "indices": indices,
                "score":   score,
                "action":  action,
                "rsi":     res["rsi"],
                "v200":    res["v200"],
                "rs":      res["rs"],
                "rng":     res["rng"],
                "vsMean":  res["vsMean"],
            })

        except Exception as exc:
            print(f"[{i+1}/{total}] {tk} → ERROR: {exc}", file=sys.stderr)
            errors += 1

        time.sleep(0.3)

    # ── Partial-data guard ────────────────────────────────────────────────────
    succeeded = len(results)
    if succeeded < total / 2:
        print(
            f"WARN  Only {succeeded}/{total} tickers scored successfully "
            f"(< 50%) — data may be partial.",
            file=sys.stderr,
        )

    # Sort by score descending
    results.sort(key=lambda r: r["score"], reverse=True)

    # ── Write output ──────────────────────────────────────────────────────────
    OUT_SCREENER.parent.mkdir(parents=True, exist_ok=True)
    out = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "regime":    regime,
        "spy":       spy_summary,
        "count":     succeeded,
        "tickers":   results,
    }
    OUT_SCREENER.write_text(json.dumps(out, indent=2))
    print(
        f"\nDone — {succeeded}/{total} scored  ({errors} skipped/errored)\n"
        f"Wrote {OUT_SCREENER.relative_to(ROOT)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
