"""
backtest.py — compare Model A (technical) vs Model B (enhanced) on historical data.

Model A: existing score_ticker() from signals_update.py (9-component, BUY>=65/HOLD>=40)
Model B: enhanced scoring implemented here (tweaked weights, 3-month momentum, BUY>=68/HOLD>=42)

Usage:
  python scripts/backtest.py
  python scripts/backtest.py --dates 2024-11-01 2024-08-01 2024-02-01 --forward 30 60 90
  python scripts/backtest.py --universe holdings  # only test on your holdings

Output: data/processed/backtest_results.json + printed summary table
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yfinance as yf

# ── PATH SETUP ────────────────────────────────────────────────────────────────
# Allow importing from scripts/ (sibling directory if run from project root,
# or same directory if run from scripts/)
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
from screener import (
    _wiki_tables,
    get_ndx100_tickers,
    get_sp500_tickers,
)

ROOT     = _SCRIPTS_DIR.parent
OUT_FILE = ROOT / "data" / "processed" / "backtest_results.json"
COST_FILE = ROOT / "data" / "holdings_cost.json"

# ─────────────────────────────────────────────────────────────────────────────
# DATA FETCH / SLICE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def fetch_full_history(yf_symbol: str):
    """
    Fetch daily OHLCV from 2022-01-01 to today.
    Returns a yfinance DataFrame or None on failure / insufficient data.
    """
    try:
        hist = yf.Ticker(yf_symbol).history(
            start="2022-01-01", interval="1d", auto_adjust=True
        )
        if len(hist) < 100:
            return None
        return hist
    except Exception:
        return None


def slice_history_at(
    hist_df, test_date: date
) -> tuple[list[int], list[float], list[float]] | None:
    """
    Slice DataFrame up to and including test_date.
    Returns (ts, cl, vol) or None if too few bars.
    """
    sliced = hist_df[hist_df.index.date <= test_date]
    if len(sliced) < MIN_BARS_FOR_SIGNALS:
        return None
    ts  = [int(idx.timestamp()) for idx in sliced.index]
    cl  = [float(v) for v in sliced["Close"]]
    vol = [float(v) for v in sliced["Volume"]]
    return ts, cl, vol


def forward_return(hist_df, test_date: date, forward_days: int) -> float | None:
    """
    Return % price change from test_date to ~forward_days calendar days later.
    Uses the first available trading day on or after each target date.
    Returns None when there is not enough future data.
    """
    dates = [d.date() for d in hist_df.index]

    # First trading day on or after test_date
    t0_idx = next((i for i, d in enumerate(dates) if d >= test_date), None)
    if t0_idx is None:
        return None

    # First trading day on or after (test_date + forward_days calendar days)
    fwd_target = test_date + timedelta(days=forward_days)
    t1_idx = next(
        (i for i, d in enumerate(dates) if d >= fwd_target), len(dates) - 1
    )
    if t1_idx <= t0_idx:
        return None

    p0 = hist_df["Close"].iloc[t0_idx]
    p1 = hist_df["Close"].iloc[t1_idx]
    return round((p1 - p0) / p0 * 100, 2)


# ─────────────────────────────────────────────────────────────────────────────
# MODEL B  — enhanced technical scorer
# ─────────────────────────────────────────────────────────────────────────────

def score_ticker_b(
    ts: list[int],
    cl: list[float],
    vol: list[float],
    spy_cl: list[float] | None,
) -> dict:
    """
    Model B — enhanced version of score_ticker().

    Changes from Model A:
      * Golden/Death cross (50/200 MA) REMOVED → replaced by 3-month momentum
      * RSI: no >73 = 0 penalty; new scale: <30→10, <50→10, <65→8, <80→5, else 2
      * RS vs SPY: raised to 18pts max
      * Weekly 10wk MA: reduced to 6pts (less redundant weight)
      * vs 1yr mean: reduced to 8pts max
      * vs 200MA: 12pts (unchanged)
      * MACD: 10pts (unchanged)
      * Volume OBV-lite: 8pts (unchanged)
      * 52w range: 8pts (unchanged)
      * NEW 3-month momentum (63 trading days): >15%→10, >5%→7, >0%→4, else 0

    Bear regime: ×0.80 (was ×0.75)
    Thresholds: BUY >= 68 / HOLD 42–67 / REDUCE < 42
    Max raw = 12+6+10+8+10+8+18+8+8+10 = 98
    """
    n, px = len(cl), cl[-1]

    # ── Core indicators ──────────────────────────────────────────────────────
    ma200   = sma(cl, 200)
    ma50    = sma(cl, 50)   # kept for internal use even though cross signal removed
    v200    = (px - ma200) / ma200 * 100

    rsi_val = calc_rsi(cl)

    ema12      = ewm(cl, 12)
    ema26      = ewm(cl, 26)
    macd_line  = [a - b for a, b in zip(ema12, ema26)]
    macd_bull  = macd_line[-1] > ewm(macd_line, 9)[-1]

    # Volume: up-day vs down-day avg volume (last 20 sessions)
    up_v = up_c = dn_v = dn_c = 0
    for i in range(1, min(20, n)):
        if cl[n - i] >= cl[n - i - 1]:
            up_v += vol[n - i]; up_c += 1
        else:
            dn_v += vol[n - i]; dn_c += 1
    vol_bull = bool(up_c and dn_c and (up_v / up_c) > (dn_v / dn_c))

    # Relative strength vs SPY (60-day return spread)
    rs = 0.0
    if spy_cl:
        p60 = min(60, len(spy_cl), n)
        rs  = (
            (px - cl[-p60]) / cl[-p60]
            - (spy_cl[-1] - spy_cl[-p60]) / spy_cl[-p60]
        ) * 100

    # 52-week range position
    cl252      = cl[-252:]
    hi52, lo52 = max(cl252), min(cl252)
    rng        = (px - lo52) / (hi52 - lo52) * 100 if hi52 > lo52 else 50.0

    # Valuation vs 1-year mean
    mean1   = sma(cl, 252)
    vs_mean = (px - mean1) / mean1 * 100

    # Weekly trend vs 10-week MA
    wk       = weekly_closes(ts, cl)
    wk_above = bool(wk and wk[-1] > sma(wk, 10))

    # 3-month momentum (63 trading days)
    p63  = min(63, n - 1)
    mom3 = (px - cl[-(p63 + 1)]) / cl[-(p63 + 1)] * 100 if p63 > 0 else 0.0

    # Market regime
    regime = "BULL"
    if spy_cl:
        regime = "BULL" if spy_cl[-1] > sma(spy_cl, 200) else "BEAR"

    # ── Component scores ─────────────────────────────────────────────────────
    sc = {
        # vs 200MA: unchanged (12pts max)
        "vs200": 12 if v200 > 5 else 8 if v200 > 0 else 4 if v200 > -5 else 0,

        # Weekly 10wk MA: reduced to 6pts (was 10)
        "wk":    6 if wk_above else 0,

        # RSI: no >73 penalty; top band is <80 (=5), else 2
        "rsi":   10 if rsi_val < 30 else 10 if rsi_val < 50 else 8 if rsi_val < 65 else 5 if rsi_val < 80 else 2,

        # MACD: unchanged (10pts)
        "macd":  10 if macd_bull else 0,

        # Volume OBV-lite: unchanged (8pts)
        "vol":   8 if vol_bull else 0,

        # RS vs SPY: raised to 18pts (was 15)
        "rs":    18 if rs > 10 else 12 if rs > 3 else 7 if rs > 0 else 3 if rs > -5 else 0,

        # 52w range: unchanged (8pts)
        "w52":   8 if rng > 75 else 6 if rng > 50 else 3 if rng > 25 else 0,

        # vs 1yr mean: reduced to 8pts max (was 10)
        "val":   8 if vs_mean < -10 else 6 if vs_mean < 0 else 4 if vs_mean < 10 else 2 if vs_mean < 20 else 0,

        # NEW — 3-month momentum (10pts max)
        "mom3":  10 if mom3 > 15 else 7 if mom3 > 5 else 4 if mom3 > 0 else 0,
    }
    # Note: Golden/Death cross (8pts in Model A) intentionally removed.

    raw    = sum(sc.values())
    score  = round(raw * 0.80) if regime == "BEAR" else raw
    action = "BUY" if score >= 68 else "HOLD" if score >= 42 else "REDUCE"

    return {
        "score":  score,
        "action": action,
        "regime": regime,
        "rsi":    round(rsi_val, 1),
        "v200":   round(v200, 1),
        "rs":     round(rs, 1),
        "hi52":   round(hi52, 2),
        "lo52":   round(lo52, 2),
        "rng":    int(round(rng)),
        "vsMean": round(vs_mean, 1),
        "mom3":   round(mom3, 1),
        "px":     round(px, 4),
        "sc":     sc,
    }


# ─────────────────────────────────────────────────────────────────────────────
# ACCURACY HELPER
# ─────────────────────────────────────────────────────────────────────────────

def call_correct(action: str, fwd_return: float | None) -> int | None:
    """
    Was the model's call directionally correct given the actual forward return?

    BUY    → correct if stock gained > 2%
    REDUCE → correct if stock fell  > 2%
    HOLD   → correct if move stayed within ±10%
    """
    if fwd_return is None:
        return None
    if action == "BUY":
        return int(fwd_return > 2.0)
    elif action == "REDUCE":
        return int(fwd_return < -2.0)
    else:  # HOLD
        return int(abs(fwd_return) <= 10.0)


# ─────────────────────────────────────────────────────────────────────────────
# SUMMARY / OUTPUT
# ─────────────────────────────────────────────────────────────────────────────

def _pct(n: int, d: int) -> str:
    return f"{n / d * 100:.1f}%" if d else "N/A"


def print_summary(
    results: list[dict],
    test_dates: list[date],
    forward_days: list[int],
) -> None:
    """Print a formatted comparison table to stdout."""
    sep = "═" * 63

    total = len(results)
    print(f"\n{sep}")
    print("BACKTEST RESULTS — Model A (Technical) vs Model B (Enhanced)")
    print(
        f"Universe: {total // len(test_dates) if test_dates else total} tickers per date  "
        f"|  Dates: {len(test_dates)}  |  Total calls: {total:,}"
    )
    print(sep)

    # ── Overall accuracy ────────────────────────────────────────────────────
    print("\nOVERALL ACCURACY (all dates, all calls)")
    header = f"{'':20s}" + "".join(f"{d}-day".center(16) for d in forward_days)
    print(header)

    for model, label in [("a", "Model A:"), ("b", "Model B:")]:
        row = f"{label:20s}"
        for d in forward_days:
            key = f"correct_{model}_{d}"
            vals = [r[key] for r in results if r[key] is not None]
            row += _pct(sum(vals), len(vals)).center(16)
        print(row)

    # Delta row
    row = f"{'Delta:':20s}"
    for d in forward_days:
        key_a = f"correct_a_{d}"
        key_b = f"correct_b_{d}"
        va = [r[key_a] for r in results if r[key_a] is not None]
        vb = [r[key_b] for r in results if r[key_b] is not None]
        if va and vb:
            delta = sum(vb) / len(vb) * 100 - sum(va) / len(va) * 100
            row += f"{delta:+.1f}%".center(16)
        else:
            row += "N/A".center(16)
    print(row)

    # ── By signal type (60-day) ─────────────────────────────────────────────
    fwd_mid = 60 if 60 in forward_days else forward_days[len(forward_days) // 2]
    print(f"\nBY SIGNAL TYPE ({fwd_mid}-day, all dates)")
    print(
        f"{'':12s}{'Model A calls':>14}{'Correct':>10}"
        f"{'Model B calls':>16}{'Correct':>10}"
    )
    for action in ["BUY", "HOLD", "REDUCE"]:
        key_a = f"correct_a_{fwd_mid}"
        key_b = f"correct_b_{fwd_mid}"
        ra = [r for r in results if r["action_a"] == action and r[key_a] is not None]
        rb = [r for r in results if r["action_b"] == action and r[key_b] is not None]
        ca = sum(r[key_a] for r in ra)
        cb = sum(r[key_b] for r in rb)
        print(
            f"{action+':':12s}{len(ra):>14}{_pct(ca, len(ra)):>10}"
            f"{len(rb):>16}{_pct(cb, len(rb)):>10}"
        )

    # ── By date (60-day) ───────────────────────────────────────────────────
    print(f"\nBY DATE ({fwd_mid}-day)")
    print(f"{'Date':16s}{'Model A':>10}{'Model B':>10}{'Delta':>10}")
    for td in test_dates:
        td_str = str(td)
        key_a  = f"correct_a_{fwd_mid}"
        key_b  = f"correct_b_{fwd_mid}"
        subset = [r for r in results if r["test_date"] == td_str]
        va = [r[key_a] for r in subset if r[key_a] is not None]
        vb = [r[key_b] for r in subset if r[key_b] is not None]
        acc_a = sum(va) / len(va) * 100 if va else None
        acc_b = sum(vb) / len(vb) * 100 if vb else None
        if acc_a is not None and acc_b is not None:
            delta = acc_b - acc_a
            print(
                f"{td_str:16s}{acc_a:>9.1f}%{acc_b:>9.1f}%{delta:>+9.1f}%"
            )
        else:
            print(f"{td_str:16s}{'N/A':>10}{'N/A':>10}{'N/A':>10}")

    # ── Agreement / divergence ──────────────────────────────────────────────
    agree_total  = sum(1 for r in results if r["agreement"])
    upgrades     = [r for r in results if r["action_a"] == "HOLD" and r["action_b"] == "BUY"]
    downgrades   = [r for r in results if r["action_a"] == "HOLD" and r["action_b"] == "REDUCE"]

    def avg_return(rows: list[dict], days: int) -> str:
        vals = [r[f"fwd_{days}"] for r in rows if r.get(f"fwd_{days}") is not None]
        return f"{sum(vals)/len(vals):+.1f}%" if vals else "N/A"

    print(
        f"\nAGREEMENT: Models agree on {_pct(agree_total, total)} "
        f"of calls ({agree_total:,} / {total:,})"
    )
    print(
        f"Model B upgraded  HOLD→BUY    vs A: {len(upgrades):>4} cases, "
        f"avg {fwd_mid}d return: {avg_return(upgrades, fwd_mid)}"
    )
    print(
        f"Model B downgraded HOLD→REDUCE vs A: {len(downgrades):>4} cases, "
        f"avg {fwd_mid}d return: {avg_return(downgrades, fwd_mid)}"
    )
    print(sep + "\n")


class _BoolEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, bool):
            return 1 if obj else 0
        return super().default(obj)


def write_output(
    results: list[dict],
    test_dates: list[date],
    forward_days: list[int],
) -> None:
    """Write results JSON to data/processed/backtest_results.json."""

    def _acc(model: str, days: int) -> float | None:
        key  = f"correct_{model}_{days}"
        vals = [r[key] for r in results if r[key] is not None]
        return round(sum(vals) / len(vals) * 100, 1) if vals else None

    summary = {
        "model_a": {f"accuracy_{d}": _acc("a", d) for d in forward_days},
        "model_b": {f"accuracy_{d}": _acc("b", d) for d in forward_days},
    }

    out = {
        "run_at":        datetime.utcnow().isoformat() + "Z",
        "test_dates":    [str(d) for d in test_dates],
        "forward_days":  forward_days,
        "universe_size": len({r["tk"] for r in results}),
        "total_calls":   len(results),
        "summary":       summary,
        "calls":         results,
    }
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(out, indent=2, cls=_BoolEncoder))
    print(f"Wrote results → {OUT_FILE}")


# ─────────────────────────────────────────────────────────────────────────────
# UNIVERSE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def get_holdings_tickers() -> tuple[list[str], dict[str, str]]:
    """Read US open positions from data/holdings_cost.json."""
    if not COST_FILE.exists():
        print(f"WARN  {COST_FILE} not found — no holdings universe", file=sys.stderr)
        return [], {}
    cost = json.loads(COST_FILE.read_text())
    us_open = cost.get("us", {}).get("open", [])
    tickers = [pos.get("yf") or pos.get("tk") for pos in us_open if pos.get("tk")]
    sectors: dict[str, str] = {}
    return [t for t in tickers if t], sectors


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Backtest Model A vs Model B on SP500+NDX100 universe."
    )
    p.add_argument(
        "--dates",
        nargs="+",
        default=["2024-11-01", "2024-08-01", "2024-02-01"],
        metavar="YYYY-MM-DD",
        help="Historical test dates (space-separated). Default: 2024-11-01 2024-08-01 2024-02-01",
    )
    p.add_argument(
        "--forward",
        nargs="+",
        type=int,
        default=[30, 60, 90],
        metavar="DAYS",
        help="Forward-return windows in calendar days. Default: 30 60 90",
    )
    p.add_argument(
        "--universe",
        default="sp500+ndx100",
        choices=["sp500+ndx100", "sp500", "ndx100", "holdings"],
        help="Ticker universe to test. Default: sp500+ndx100",
    )
    return p.parse_args()


def main() -> int:
    args     = parse_args()
    test_dates   = [date.fromisoformat(d) for d in args.dates]
    forward_days = sorted(set(args.forward))
    universe_arg = args.universe

    # ── 1. Build ticker universe ─────────────────────────────────────────────
    print("Fetching ticker universe…")
    sector_map: dict[str, str] = {}
    all_tickers: list[str] = []

    if universe_arg in ("sp500", "sp500+ndx100"):
        sp500, sp500_sectors = get_sp500_tickers()
        sector_map.update(sp500_sectors)
        all_tickers += sp500
        print(f"  SP500:  {len(sp500)} tickers")

    if universe_arg in ("ndx100", "sp500+ndx100"):
        ndx100, ndx100_sectors = get_ndx100_tickers()
        # Only update sectors for tickers not already mapped by SP500
        for tk, sec in ndx100_sectors.items():
            sector_map.setdefault(tk, sec)
        # Deduplicate while preserving order
        seen = set(all_tickers)
        added = [t for t in ndx100 if t not in seen]
        all_tickers += added
        print(f"  NDX100: {len(ndx100)} tickers  ({len(added)} new after dedup)")

    if universe_arg == "holdings":
        all_tickers, hld_sectors = get_holdings_tickers()
        sector_map.update(hld_sectors)
        print(f"  Holdings: {len(all_tickers)} tickers")

    if not all_tickers:
        print("ERROR  No tickers found — aborting.", file=sys.stderr)
        return 1

    total = len(all_tickers)
    print(f"\nUniverse: {total} tickers  |  Test dates: {[str(d) for d in test_dates]}")
    print(f"Forward windows: {forward_days} calendar days\n")

    # ── 2. Fetch SPY full history ────────────────────────────────────────────
    print("Fetching SPY benchmark history (2022-01-01 → today)…")
    spy_hist = fetch_full_history("SPY")
    if spy_hist is None:
        print("WARN  SPY fetch failed — regime will default to BULL", file=sys.stderr)

    # ── 3. Fetch all ticker histories (cached in memory) ─────────────────────
    print(f"Fetching {total} ticker histories (2022-01-01 → today) — this may take a few minutes…\n")
    cache: dict[str, object] = {}  # ticker → hist_df or None

    for i, tk in enumerate(all_tickers, 1):
        print(f"  [{i:4d}/{total}] {tk:8s}…", end="\r", flush=True)
        cache[tk] = fetch_full_history(tk)
        time.sleep(0.4)

    fetched = sum(1 for v in cache.values() if v is not None)
    print(f"\n  Fetched {fetched}/{total} tickers successfully.\n")

    # ── 4. Score each ticker × test_date, compute forward returns ────────────
    all_results: list[dict] = []

    for test_date in test_dates:
        print(f"Scoring test date {test_date}…")

        # Slice SPY for this test date
        spy_sliced = slice_history_at(spy_hist, test_date) if spy_hist is not None else None
        spy_cl     = spy_sliced[1] if spy_sliced else None

        date_count = 0
        for tk in all_tickers:
            hist_df = cache.get(tk)
            if hist_df is None:
                continue

            sliced = slice_history_at(hist_df, test_date)
            if sliced is None:
                continue

            ts, cl, vol = sliced

            # ── Model A ─────────────────────────────────────────────────────
            try:
                res_a   = score_ticker(ts, cl, vol, spy_cl)
                score_a = res_a["score"]
                action_a = res_a["action"]
            except Exception as exc:
                print(f"\n  WARN  Model A failed for {tk} @ {test_date}: {exc}", file=sys.stderr)
                continue

            # ── Model B ─────────────────────────────────────────────────────
            try:
                res_b   = score_ticker_b(ts, cl, vol, spy_cl)
                score_b = res_b["score"]
                action_b = res_b["action"]
            except Exception as exc:
                print(f"\n  WARN  Model B failed for {tk} @ {test_date}: {exc}", file=sys.stderr)
                continue

            # ── Forward returns ──────────────────────────────────────────────
            fwd: dict[int, float | None] = {}
            for days in forward_days:
                fwd[days] = forward_return(hist_df, test_date, days)

            # Skip this record entirely if *all* forward returns are missing
            # (test_date too recent to have any forward data).
            if all(v is None for v in fwd.values()):
                continue

            all_results.append({
                "tk":          tk,
                "sector":      sector_map.get(tk, "Unknown"),
                "test_date":   str(test_date),
                "score_a":     score_a,
                "action_a":    action_a,
                "score_b":     score_b,
                "action_b":    action_b,
                # Forward returns
                **{f"fwd_{d}": fwd.get(d) for d in forward_days},
                # Accuracy flags
                **{f"correct_a_{d}": call_correct(action_a, fwd.get(d)) for d in forward_days},
                **{f"correct_b_{d}": call_correct(action_b, fwd.get(d)) for d in forward_days},
                "agreement":   action_a == action_b,
            })
            date_count += 1

        print(f"  {test_date}: {date_count} tickers scored.")

    if not all_results:
        print("ERROR  No results generated — check ticker universe and test dates.", file=sys.stderr)
        return 1

    # ── 5. Print summary + write JSON ────────────────────────────────────────
    print_summary(all_results, test_dates, forward_days)
    write_output(all_results, test_dates, forward_days)

    return 0


if __name__ == "__main__":
    sys.exit(main())
