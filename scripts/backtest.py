"""
backtest.py — Model A vs Model B on historically-accurate SP500/NDX100 universe.

Improvements over backtest_v1.py:
  * Point-in-time index membership: reconstructs SP500 composition at each
    test date using Wikipedia's change log — eliminates survivorship bias.
  * Batch yfinance download: one API call per chunk instead of one per ticker,
    dramatically reducing rate-limit failures.
  * NDX100 historical membership approximated (Wikipedia has no change log for
    NDX100 — we use current NDX100 as-is for NDX100-only tickers, clearly noted).

Usage:
  python scripts/backtest.py
  python scripts/backtest.py --dates 2024-11-01 2024-08-01 2024-02-01
  python scripts/backtest.py --forward 30 60 90
  python scripts/backtest.py --universe sp500       # SP500 only (point-in-time)
  python scripts/backtest.py --universe sp500+ndx100
  python scripts/backtest.py --universe holdings
  python scripts/backtest.py --chunk 50             # tickers per batch download

Output: data/processed/backtest_results.json + printed summary table
Legacy: backtest_v1.py — original single-ticker fetch, no point-in-time correction
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

# ── PATH SETUP ────────────────────────────────────────────────────────────────
_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))

from signals_update import (
    MIN_BARS_FOR_SIGNALS,
    calc_rsi,
    ewm,
    score_ticker,
    sma,
    weekly_closes,
)
from screener import _wiki_tables, get_ndx100_tickers, get_sp500_tickers

ROOT      = _SCRIPTS_DIR.parent
OUT_FILE  = ROOT / "data" / "processed" / "backtest_results.json"
COST_FILE = ROOT / "data" / "holdings_cost.json"


# ─────────────────────────────────────────────────────────────────────────────
# POINT-IN-TIME SP500 RECONSTRUCTION
# ─────────────────────────────────────────────────────────────────────────────

def get_sp500_at_date(test_date: date) -> tuple[list[str], dict[str, str]]:
    """
    Reconstruct SP500 membership on test_date using Wikipedia change log.
    Returns (tickers, sector_map) — free, no API key required.

    Method: start from current SP500, walk backward through the change log,
    undoing every addition/removal that happened AFTER test_date.

    Accuracy: Wikipedia change log goes back to ~2000. Sector map reflects
    current GICS assignments (minor inaccuracy for tickers that changed sector).
    """
    try:
        tables = _wiki_tables("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")

        # Current members + sectors
        curr_df     = tables[0]
        sector_col  = "GICS Sector" if "GICS Sector" in curr_df.columns else curr_df.columns[2]
        current     = {t.replace(".", "-") for t in curr_df["Symbol"]}
        sector_map  = {
            t.replace(".", "-"): str(s)
            for t, s in zip(curr_df["Symbol"], curr_df[sector_col])
        }

        # Change log (2nd table)
        chg = tables[1].copy()
        chg.columns = [str(c).strip() for c in chg.columns]

        # Identify date, added, removed columns flexibly
        date_col    = next((c for c in chg.columns if "date" in c.lower()), None)
        added_col   = next((c for c in chg.columns if "added" in c.lower() and "tick" in c.lower()), None)
        removed_col = next((c for c in chg.columns if "remov" in c.lower() and "tick" in c.lower()), None)

        if not date_col:
            print("WARN  SP500 change log date column not found — using current composition", file=sys.stderr)
            tickers = list(current)
            return tickers, {t: sector_map.get(t, "Unknown") for t in tickers}

        chg[date_col] = pd.to_datetime(chg[date_col], errors="coerce")

        # Walk backward through changes after test_date
        for _, row in chg.iterrows():
            row_date = row[date_col]
            if pd.isna(row_date) or row_date.date() <= test_date:
                continue
            # Undo: discard ticker added after test_date, restore ticker removed after test_date
            if added_col:
                added = str(row.get(added_col, "") or "").replace(".", "-").strip()
                if added and added.lower() not in ("nan", ""):
                    current.discard(added)
            if removed_col:
                removed = str(row.get(removed_col, "") or "").replace(".", "-").strip()
                if removed and removed.lower() not in ("nan", ""):
                    current.add(removed)

        tickers = sorted(current)
        print(f"  SP500 @ {test_date}: {len(tickers)} members (point-in-time)")
        return tickers, {t: sector_map.get(t, "Unknown") for t in tickers}

    except Exception as exc:
        print(f"WARN  SP500 point-in-time failed ({exc}) — falling back to current composition", file=sys.stderr)
        return get_sp500_tickers()


# ─────────────────────────────────────────────────────────────────────────────
# BATCH HISTORY FETCH
# ─────────────────────────────────────────────────────────────────────────────

def batch_fetch_histories(
    tickers: list[str],
    start: str = "2022-01-01",
    chunk_size: int = 50,
    sleep_s: float = 1.5,
) -> dict[str, object]:
    """
    Fetch OHLCV for all tickers in chunks using yf.download().
    Returns {ticker: DataFrame | None}.

    Batch download = far fewer HTTP requests than one-by-one fetch,
    reducing Yahoo Finance rate-limit errors.
    """
    cache: dict[str, object] = {}
    total_chunks = (len(tickers) + chunk_size - 1) // chunk_size

    for ci, i in enumerate(range(0, len(tickers), chunk_size), 1):
        chunk = tickers[i : i + chunk_size]
        print(f"  Batch {ci}/{total_chunks}: {len(chunk)} tickers…", end=" ", flush=True)
        try:
            raw = yf.download(
                chunk,
                start=start,
                interval="1d",
                auto_adjust=True,
                progress=False,
                threads=True,
            )
            if raw.empty:
                print("empty")
                for tk in chunk:
                    cache[tk] = None
                time.sleep(sleep_s)
                continue

            # Multi-ticker: columns are MultiIndex (field, ticker)
            # Single ticker: flat columns
            if isinstance(raw.columns, pd.MultiIndex):
                for tk in chunk:
                    try:
                        sub = raw.xs(tk, level=1, axis=1)
                        if len(sub) >= 100:
                            cache[tk] = sub
                        else:
                            cache[tk] = None
                    except KeyError:
                        cache[tk] = None
            else:
                # Single ticker returned as flat df
                tk = chunk[0]
                cache[tk] = raw if len(raw) >= 100 else None

            ok = sum(1 for v in cache.values() if v is not None)
            print(f"ok ({ok}/{len(cache)} total fetched)")

        except Exception as exc:
            print(f"ERROR: {exc}")
            for tk in chunk:
                cache.setdefault(tk, None)

        time.sleep(sleep_s)

    return cache


# ─────────────────────────────────────────────────────────────────────────────
# SLICE / FORWARD RETURN HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def slice_history_at(
    hist_df, test_date: date
) -> tuple[list[int], list[float], list[float]] | None:
    sliced = hist_df[hist_df.index.date <= test_date]
    if len(sliced) < MIN_BARS_FOR_SIGNALS:
        return None
    ts  = [int(idx.timestamp()) for idx in sliced.index]
    cl  = [float(v) for v in sliced["Close"]]
    vol = [float(v) for v in sliced["Volume"]]
    return ts, cl, vol


def forward_return(hist_df, test_date: date, forward_days: int) -> float | None:
    dates   = [d.date() for d in hist_df.index]
    t0_idx  = next((i for i, d in enumerate(dates) if d >= test_date), None)
    if t0_idx is None:
        return None
    fwd_target = test_date + timedelta(days=forward_days)
    t1_idx  = next((i for i, d in enumerate(dates) if d >= fwd_target), len(dates) - 1)
    if t1_idx <= t0_idx:
        return None
    p0 = hist_df["Close"].iloc[t0_idx]
    p1 = hist_df["Close"].iloc[t1_idx]
    return round((p1 - p0) / p0 * 100, 2)


# ─────────────────────────────────────────────────────────────────────────────
# MODEL B — enhanced technical scorer
# ─────────────────────────────────────────────────────────────────────────────

def score_ticker_b(
    ts: list[int],
    cl: list[float],
    vol: list[float],
    spy_cl: list[float] | None,
) -> dict:
    """
    Model B changes from Model A:
      - Golden/Death cross removed → 3-month momentum (10pts)
      - RSI: no >73 penalty; <80→5, else 2
      - RS vs SPY: 18pts max (was 15)
      - Weekly 10wk MA: 6pts (was 10)
      - vs 1yr mean: 8pts max (was 10)
      - Bear regime: ×0.80 (was ×0.75)
      - BUY>=68 / HOLD 42-67 / REDUCE<42
    Max raw = 98pts
    """
    n, px   = len(cl), cl[-1]
    ma200   = sma(cl, 200)
    v200    = (px - ma200) / ma200 * 100
    rsi_val = calc_rsi(cl)

    ema12     = ewm(cl, 12)
    ema26     = ewm(cl, 26)
    macd_line = [a - b for a, b in zip(ema12, ema26)]
    macd_bull = macd_line[-1] > ewm(macd_line, 9)[-1]

    up_v = up_c = dn_v = dn_c = 0
    for i in range(1, min(20, n)):
        if cl[n - i] >= cl[n - i - 1]:
            up_v += vol[n - i]; up_c += 1
        else:
            dn_v += vol[n - i]; dn_c += 1
    vol_bull = bool(up_c and dn_c and (up_v / up_c) > (dn_v / dn_c))

    rs = 0.0
    if spy_cl:
        p60 = min(60, len(spy_cl), n)
        rs  = ((px - cl[-p60]) / cl[-p60] - (spy_cl[-1] - spy_cl[-p60]) / spy_cl[-p60]) * 100

    cl252      = cl[-252:]
    hi52, lo52 = max(cl252), min(cl252)
    rng        = (px - lo52) / (hi52 - lo52) * 100 if hi52 > lo52 else 50.0

    mean1   = sma(cl, 252)
    vs_mean = (px - mean1) / mean1 * 100

    wk       = weekly_closes(ts, cl)
    wk_above = bool(wk and wk[-1] > sma(wk, 10))

    p63  = min(63, n - 1)
    mom3 = (px - cl[-(p63 + 1)]) / cl[-(p63 + 1)] * 100 if p63 > 0 else 0.0

    regime = "BULL"
    if spy_cl:
        regime = "BULL" if spy_cl[-1] > sma(spy_cl, 200) else "BEAR"

    sc = {
        "vs200": 12 if v200 > 5 else 8 if v200 > 0 else 4 if v200 > -5 else 0,
        "wk":    6  if wk_above else 0,
        "rsi":   10 if rsi_val < 30 else 10 if rsi_val < 50 else 8 if rsi_val < 65 else 5 if rsi_val < 80 else 2,
        "macd":  10 if macd_bull else 0,
        "vol":   8  if vol_bull else 0,
        "rs":    18 if rs > 10 else 12 if rs > 3 else 7 if rs > 0 else 3 if rs > -5 else 0,
        "w52":   8  if rng > 75 else 6 if rng > 50 else 3 if rng > 25 else 0,
        "val":   8  if vs_mean < -10 else 6 if vs_mean < 0 else 4 if vs_mean < 10 else 2 if vs_mean < 20 else 0,
        "mom3":  10 if mom3 > 15 else 7 if mom3 > 5 else 4 if mom3 > 0 else 0,
    }

    raw    = sum(sc.values())
    score  = round(raw * 0.80) if regime == "BEAR" else raw
    action = "BUY" if score >= 68 else "HOLD" if score >= 42 else "REDUCE"

    return {
        "score":  score, "action": action, "regime": regime,
        "rsi":    round(rsi_val, 1), "v200": round(v200, 1),
        "rs":     round(rs, 1), "hi52": round(hi52, 2), "lo52": round(lo52, 2),
        "rng":    int(round(rng)), "vsMean": round(vs_mean, 1),
        "mom3":   round(mom3, 1), "px": round(px, 4), "sc": sc,
    }


# ─────────────────────────────────────────────────────────────────────────────
# ACCURACY
# ─────────────────────────────────────────────────────────────────────────────

def call_correct(action: str, fwd: float | None) -> int | None:
    if fwd is None:
        return None
    if action == "BUY":    return int(fwd > 2.0)
    if action == "REDUCE": return int(fwd < -2.0)
    return int(abs(fwd) <= 10.0)   # HOLD


# ─────────────────────────────────────────────────────────────────────────────
# REPORTING
# ─────────────────────────────────────────────────────────────────────────────

def _pct(n: int, d: int) -> str:
    return f"{n / d * 100:.1f}%" if d else "N/A"


def print_summary(results: list[dict], test_dates: list[date], forward_days: list[int]) -> None:
    sep = "═" * 65
    total = len(results)
    print(f"\n{sep}")
    print("BACKTEST  —  Model A (9-factor Technical) vs Model B (Enhanced)")
    print(f"Universe: point-in-time SP500  |  Dates: {len(test_dates)}  |  Calls: {total:,}")
    print(sep)

    print("\nOVERALL ACCURACY (all dates, all calls)")
    print(f"{'':20s}" + "".join(f"{d}-day".center(16) for d in forward_days))
    for model, label in [("a", "Model A:"), ("b", "Model B:")]:
        row = f"{label:20s}"
        for d in forward_days:
            key  = f"correct_{model}_{d}"
            vals = [r[key] for r in results if r[key] is not None]
            row += _pct(sum(vals), len(vals)).center(16)
        print(row)

    row = f"{'Delta (B−A):':20s}"
    for d in forward_days:
        va = [r[f"correct_a_{d}"] for r in results if r[f"correct_a_{d}"] is not None]
        vb = [r[f"correct_b_{d}"] for r in results if r[f"correct_b_{d}"] is not None]
        if va and vb:
            row += f"{sum(vb)/len(vb)*100 - sum(va)/len(va)*100:+.1f}%".center(16)
        else:
            row += "N/A".center(16)
    print(row)

    fwd_mid = 60 if 60 in forward_days else forward_days[len(forward_days) // 2]
    print(f"\nBY SIGNAL TYPE ({fwd_mid}-day, all dates)")
    print(f"{'':12s}{'Model A calls':>14}{'Correct':>10}{'Model B calls':>16}{'Correct':>10}")
    for action in ["BUY", "HOLD", "REDUCE"]:
        ra = [r for r in results if r["action_a"] == action and r[f"correct_a_{fwd_mid}"] is not None]
        rb = [r for r in results if r["action_b"] == action and r[f"correct_b_{fwd_mid}"] is not None]
        print(
            f"{action+':':12s}{len(ra):>14}{_pct(sum(r[f'correct_a_{fwd_mid}'] for r in ra), len(ra)):>10}"
            f"{len(rb):>16}{_pct(sum(r[f'correct_b_{fwd_mid}'] for r in rb), len(rb)):>10}"
        )

    print(f"\nBY DATE ({fwd_mid}-day)")
    print(f"{'Date':16s}{'Model A':>10}{'Model B':>10}{'Delta':>10}{'Tickers':>10}")
    for td in test_dates:
        td_str = str(td)
        subset = [r for r in results if r["test_date"] == td_str]
        va = [r[f"correct_a_{fwd_mid}"] for r in subset if r[f"correct_a_{fwd_mid}"] is not None]
        vb = [r[f"correct_b_{fwd_mid}"] for r in subset if r[f"correct_b_{fwd_mid}"] is not None]
        if va and vb:
            aa, ab = sum(va)/len(va)*100, sum(vb)/len(vb)*100
            print(f"{td_str:16s}{aa:>9.1f}%{ab:>9.1f}%{ab-aa:>+9.1f}%{len(subset):>10,}")
        else:
            print(f"{td_str:16s}{'N/A':>10}{'N/A':>10}{'N/A':>10}{len(subset):>10,}")

    agree   = sum(1 for r in results if r["agreement"])
    upgrades   = [r for r in results if r["action_a"] == "HOLD" and r["action_b"] == "BUY"]
    downgrades = [r for r in results if r["action_a"] == "HOLD" and r["action_b"] == "REDUCE"]

    def avg_ret(rows, d):
        vals = [r[f"fwd_{d}"] for r in rows if r.get(f"fwd_{d}") is not None]
        return f"{sum(vals)/len(vals):+.1f}%" if vals else "N/A"

    print(f"\nAGREEMENT: {_pct(agree, total)} of calls ({agree:,}/{total:,})")
    print(f"B upgraded  HOLD→BUY:    {len(upgrades):>4}  avg {fwd_mid}d: {avg_ret(upgrades,   fwd_mid)}")
    print(f"B downgraded HOLD→REDUCE: {len(downgrades):>4}  avg {fwd_mid}d: {avg_ret(downgrades, fwd_mid)}")
    print(sep + "\n")


class _BoolEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, bool):
            return 1 if obj else 0
        return super().default(obj)


def write_output(results: list[dict], test_dates: list[date], forward_days: list[int]) -> None:
    def _acc(model, d):
        vals = [r[f"correct_{model}_{d}"] for r in results if r[f"correct_{model}_{d}"] is not None]
        return round(sum(vals) / len(vals) * 100, 1) if vals else None

    out = {
        "run_at":        datetime.utcnow().isoformat() + "Z",
        "version":       "v2-point-in-time",
        "test_dates":    [str(d) for d in test_dates],
        "forward_days":  forward_days,
        "universe_size": len({r["tk"] for r in results}),
        "total_calls":   len(results),
        "summary": {
            "model_a": {f"accuracy_{d}": _acc("a", d) for d in forward_days},
            "model_b": {f"accuracy_{d}": _acc("b", d) for d in forward_days},
        },
        "calls": results,
    }
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(out, indent=2, cls=_BoolEncoder))
    print(f"Wrote → {OUT_FILE}")


# ─────────────────────────────────────────────────────────────────────────────
# UNIVERSE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def get_holdings_tickers() -> tuple[list[str], dict[str, str]]:
    if not COST_FILE.exists():
        return [], {}
    cost    = json.loads(COST_FILE.read_text())
    us_open = cost.get("us", {}).get("open", [])
    tickers = [pos.get("yf") or pos.get("tk") for pos in us_open if pos.get("tk")]
    return [t for t in tickers if t], {}


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--dates",    nargs="+", default=["2024-11-01", "2024-08-01", "2024-02-01"])
    p.add_argument("--forward",  nargs="+", type=int, default=[30, 60, 90])
    p.add_argument("--universe", default="sp500+ndx100",
                   choices=["sp500+ndx100", "sp500", "ndx100", "holdings"])
    p.add_argument("--chunk",    type=int, default=50, help="Tickers per batch download")
    return p.parse_args()


def main() -> int:
    args         = parse_args()
    test_dates   = [date.fromisoformat(d) for d in args.dates]
    forward_days = sorted(set(args.forward))

    # ── 1. Build point-in-time universe per test date ─────────────────────────
    # We need a union of all tickers across all test dates for one-shot batch fetch,
    # but we track per-date membership separately to avoid scoring non-members.

    print("Building point-in-time index membership per test date…")
    per_date_tickers: dict[str, list[str]] = {}
    sector_map: dict[str, str] = {}

    if args.universe == "holdings":
        tks, _ = get_holdings_tickers()
        for td in test_dates:
            per_date_tickers[str(td)] = tks
    else:
        for td in test_dates:
            sp500_tks, sp500_sec = get_sp500_at_date(td)
            sector_map.update(sp500_sec)

            ndx100_tks: list[str] = []
            if args.universe == "sp500+ndx100":
                ndx100_tks, ndx100_sec = get_ndx100_tickers()
                print(f"  NDX100 @ {td}: {len(ndx100_tks)} members (NOTE: current composition, no change log)")
                for tk, sec in ndx100_sec.items():
                    sector_map.setdefault(tk, sec)

            seen: dict[str, bool] = {}
            for tk in sp500_tks + ndx100_tks:
                seen[tk] = True
            per_date_tickers[str(td)] = list(seen.keys())

    all_tickers = list({tk for tks in per_date_tickers.values() for tk in tks})
    print(f"\nUnion universe: {len(all_tickers)} unique tickers to fetch\n")

    # ── 2. Batch fetch SPY ────────────────────────────────────────────────────
    print("Fetching SPY benchmark…")
    spy_cache = batch_fetch_histories(["SPY"], start="2022-01-01", chunk_size=1, sleep_s=0.5)
    spy_hist  = spy_cache.get("SPY")
    if spy_hist is None:
        print("WARN  SPY fetch failed — regime defaults to BULL", file=sys.stderr)

    # ── 3. Batch fetch all tickers ────────────────────────────────────────────
    print(f"Batch-fetching {len(all_tickers)} tickers (chunk={args.chunk})…")
    cache = batch_fetch_histories(all_tickers, start="2022-01-01", chunk_size=args.chunk)
    fetched = sum(1 for v in cache.values() if v is not None)
    print(f"\nFetched {fetched}/{len(all_tickers)} tickers OK\n")

    # ── 4. Score each ticker × test_date ─────────────────────────────────────
    all_results: list[dict] = []

    for test_date in test_dates:
        td_str     = str(test_date)
        date_tickers = per_date_tickers[td_str]
        print(f"Scoring {td_str} ({len(date_tickers)} members)…")

        spy_sliced = slice_history_at(spy_hist, test_date) if spy_hist is not None else None
        spy_cl     = spy_sliced[1] if spy_sliced else None

        date_count = 0
        for tk in date_tickers:
            hist_df = cache.get(tk)
            if hist_df is None:
                continue

            sliced = slice_history_at(hist_df, test_date)
            if sliced is None:
                continue

            ts, cl, vol = sliced

            try:
                res_a    = score_ticker(ts, cl, vol, spy_cl)
                res_b    = score_ticker_b(ts, cl, vol, spy_cl)
            except Exception as exc:
                print(f"  WARN  scoring {tk}: {exc}", file=sys.stderr)
                continue

            fwd = {d: forward_return(hist_df, test_date, d) for d in forward_days}
            if all(v is None for v in fwd.values()):
                continue

            all_results.append({
                "tk":        tk,
                "sector":    sector_map.get(tk, "Unknown"),
                "test_date": td_str,
                "score_a":   res_a["score"],  "action_a": res_a["action"],
                "score_b":   res_b["score"],  "action_b": res_b["action"],
                **{f"fwd_{d}":       fwd[d]                          for d in forward_days},
                **{f"correct_a_{d}": call_correct(res_a["action"], fwd[d]) for d in forward_days},
                **{f"correct_b_{d}": call_correct(res_b["action"], fwd[d]) for d in forward_days},
                "agreement": res_a["action"] == res_b["action"],
            })
            date_count += 1

        print(f"  → {date_count} tickers scored for {td_str}")

    if not all_results:
        print("ERROR  No results — check universe and test dates.", file=sys.stderr)
        return 1

    print_summary(all_results, test_dates, forward_days)
    write_output(all_results, test_dates, forward_days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
