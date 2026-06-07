"""
signals_update.py — server-side signal scorer for US holdings.

Reads  : data/holdings_cost.json      (US open positions + yf symbols)
Writes : data/processed/stock_signals.json

Runs in GitHub Actions every 15 min alongside market_data.py.
The dashboard Signals tab reads this JSON — zero browser-side computation,
zero Yahoo Finance fetches from the user's device.

Scoring model (max 87 pts raw):
  vs 200MA        12 / 8 / 4 / 0
  Golden/Death    8 / 0
  Weekly 10wk MA  10 / 0
  RSI-14          12 / 10 / 8 / 5 / 0
  MACD            10 / 0
  Volume OBV-lite  8 / 0
  RS vs SPY 60d   15 / 10 / 6 / 3 / 0
  52w range        8 / 6 / 3 / 0
  vs 1yr mean     10 / 8 / 6 / 3 / 0

  Bear regime (SPY < 200MA) → ×0.75 multiplier applied
  Model C hybrid thresholds (B's BUY precision + A's HOLD/REDUCE range):
  BUY ≥ 68 / HOLD 40–67 / REDUCE < 40
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT        = Path(__file__).resolve().parent.parent
COST_BASIS  = ROOT / "data" / "holdings_cost.json"
OUT_SIGNALS = ROOT / "data" / "processed" / "stock_signals.json"

# ── SECTOR MAP (Yahoo quoteSummary now requires auth → hardcode known holdings) ──
# Yahoo v10 quoteSummary returns 401; use static map for our fixed universe.
SECTOR_MAP: dict[str, str] = {
    # US holdings
    "GOOG": "Communication Services", "AMZN": "Consumer Cyclical",
    "AVGO": "Technology",             "GLW":  "Technology",
    "GEV":  "Industrials",            "MU":   "Technology",
    "MSFT": "Technology",             "MP":   "Basic Materials",
    "RKLB": "Industrials",            "SHIP": "Industrials",
    "NOW":  "Technology",             "TTE":  "Energy",
    "EWY":  "ETF",                    "HUMN": "ETF",
    "VOOG": "ETF",
    "JDZG": "Financial Services",
    "INTC": "Technology",
    "ORCL": "Technology",
    # India holdings
    "SBIN": "Financial Services",     "AVADHSUGAR": "Consumer Defensive",
    "IRBINVIT": "InvIT",              "JYOTISTRUC": "Industrials",
    "GMBREW": "Consumer Defensive",   "GOLDBEES_M": "ETF",
    "GOLDBEES_U": "ETF",              "RELIANCE": "Energy",
    "WAAREEENER": "Industrials",      "DIACABS": "Industrials",
    "FILATFASH": "Consumer Cyclical", "NBCC": "Industrials",
    "ASHALOG": "Industrials",         "PARAMATRIX": "Technology",
}


# ── MATH HELPERS ─────────────────────────────────────────────────────────────

# ── FINANCEDATABASE sector lookup (lazy-loaded once per run) ─────────────────
_FD_DF = None

def _load_fd():
    global _FD_DF
    if _FD_DF is not None:
        return _FD_DF
    try:
        import financedatabase as fd
        _FD_DF = fd.Equities().select()
        print(f"  FinanceDatabase loaded: {len(_FD_DF)} equities")
    except Exception as e:
        print(f"  FinanceDatabase unavailable: {e}", file=sys.stderr)
        _FD_DF = {}   # sentinel — skip on failure
    return _FD_DF


def ewm(data: list[float], span: int) -> list[float]:
    """Exponential weighted mean — matches JS sigEwm exactly."""
    k, ema = 2 / (span + 1), data[0]
    out = []
    for v in data:
        ema = v * k + ema * (1 - k)
        out.append(ema)
    return out


def sma(arr: list[float], p: int) -> float:
    # When len(arr) < p, slc is the full array — average over fewer periods.
    # This is intentional: shorter history produces a valid (shorter) moving average.
    slc = arr[-p:]
    return sum(slc) / len(slc) if slc else 0.0  # guard: empty array → 0


def calc_rsi(cl: list[float], n: int = 14) -> float:
    gains, losses = [], []
    for i in range(1, len(cl)):
        d = cl[i] - cl[i - 1]
        gains.append(d if d > 0 else 0.0)
        losses.append(-d if d < 0 else 0.0)
    if not gains:
        return 50.0
    ag, al = ewm(gains, n)[-1], ewm(losses, n)[-1]
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def weekly_closes(ts: list[int], cl: list[float]) -> list[float]:
    """Group daily data into per-week last close (Monday-keyed). Matches JS sigWeekly."""
    weeks: dict[str, float] = {}
    for t_val, c_val in zip(ts, cl):
        dt = datetime.fromtimestamp(t_val, tz=timezone.utc)
        monday = (dt - timedelta(days=dt.weekday())).date()
        weeks[str(monday)] = c_val
    return list(weeks.values())


# ── DATA FETCH (direct Yahoo chart API — no yfinance dependency) ─────────────

_YF_UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


def fetch_history(yf_symbol: str) -> "tuple[list[int], list[float], list[float]] | None":
    """Return (unix_timestamps, closes, volumes) for 2 years of daily data.
    Uses Yahoo Finance v8 chart API directly — avoids yfinance Python 3.9 breakage."""
    end   = int(time.time())
    start = end - 730 * 86400   # 2 years
    for host in ("query1", "query2"):
        try:
            url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/{yf_symbol}"
                   f"?interval=1d&period1={start}&period2={end}")
            r   = requests.get(url, headers=_YF_UA, timeout=15)
            if r.status_code != 200:
                continue
            result  = r.json()["chart"]["result"][0]
            ts      = result["timestamp"]
            quote   = result["indicators"]["quote"][0]
            adj     = result["indicators"].get("adjclose", [{}])[0]
            closes  = adj.get("adjclose") or quote.get("close") or []
            volumes = quote.get("volume") or []
            # Zip + filter out None values
            rows = [(t, c, v) for t, c, v in zip(ts, closes, volumes)
                    if c is not None and v is not None]
            if len(rows) < 50:
                return None
            ts_out  = [r[0] for r in rows]
            cl_out  = [float(r[1]) for r in rows]
            vo_out  = [float(r[2]) for r in rows]
            return ts_out, cl_out, vo_out
        except Exception as exc:
            print(f"  WARN  {yf_symbol} ({host}): {exc}", file=sys.stderr)
    return None


def fetch_sector(yf_symbol: str) -> tuple:
    """Return (sector, industry).
    1st: FinanceDatabase (authoritative, auto-covers new tickers)
    2nd: SECTOR_MAP fallback (ETFs + India SME not in FD)"""
    base = yf_symbol.replace(".NS", "").replace(".BO", "")
    df = _load_fd()
    if df is not None and hasattr(df, 'loc'):
        try:
            rows = df.loc[[base]] if base in df.index else None
            if rows is not None and len(rows):
                us  = rows[rows['country'] == 'United States']
                ind = rows[rows['country'] == 'India']
                row = us.iloc[0] if len(us) else (ind.iloc[0] if len(ind) else rows.iloc[0])
                sec = str(row.get('sector', '') or '')
                ind_str = str(row.get('industry', '') or '')
                if sec and sec not in ('None', 'nan', ''):
                    return sec, (ind_str if ind_str not in ('None','nan','') else '')
        except Exception:
            pass
    sec = SECTOR_MAP.get(base, SECTOR_MAP.get(yf_symbol, "Unknown"))
    return sec, ""

# Minimum bars needed for meaningful signal computation (RSI-14 + some trend context)
MIN_BARS_FOR_SIGNALS = 60


# ── SCORER ───────────────────────────────────────────────────────────────────

def score_ticker(
    ts: list[int],
    cl: list[float],
    vol: list[float],
    spy_cl: list[float] | None,
) -> dict:
    """Exact Python translation of JS sigScore()."""
    n, px = len(cl), cl[-1]

    ma200 = sma(cl, 200)
    ma50  = sma(cl, 50)
    v200  = (px - ma200) / ma200 * 100 if ma200 else 0.0

    rsi_val = calc_rsi(cl)

    ema12 = ewm(cl, 12)
    ema26 = ewm(cl, 26)
    macd_line = [a - b for a, b in zip(ema12, ema26)]
    macd_bull = macd_line[-1] > ewm(macd_line, 9)[-1]

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
        base_stk = cl[-p60]
        base_spy = spy_cl[-p60]
        if base_stk and base_spy:   # guard: both denominators non-zero
            rs = (
                (px - base_stk) / base_stk
                - (spy_cl[-1] - base_spy) / base_spy
            ) * 100

    # 52-week range position
    cl252 = cl[-252:]
    hi52, lo52 = max(cl252), min(cl252)
    rng = (px - lo52) / (hi52 - lo52) * 100 if hi52 > lo52 else 50.0

    # Valuation vs 1-year mean
    mean1   = sma(cl, 252)
    vs_mean = (px - mean1) / mean1 * 100 if mean1 else 0.0

    # Weekly trend vs 10-week MA
    wk = weekly_closes(ts, cl)
    wk_above = bool(wk and wk[-1] > sma(wk, 10))

    # Market regime
    regime = "BULL"
    if spy_cl:
        regime = "BULL" if spy_cl[-1] > sma(spy_cl, 200) else "BEAR"

    sc = {
        "vs200": 12 if v200 > 5 else 8 if v200 > 0 else 4 if v200 > -5 else 0,
        "cross": 8 if ma50 > ma200 else 0,
        "wk":    10 if wk_above else 0,
        "rsi":   12 if rsi_val < 30 else 10 if rsi_val < 50 else 8 if rsi_val < 65 else 5 if rsi_val < 73 else 0,
        "macd":  10 if macd_bull else 0,
        "vol":   8 if vol_bull else 0,
        "rs":    15 if rs > 10 else 10 if rs > 3 else 6 if rs > 0 else 3 if rs > -5 else 0,
        "w52":   8 if rng > 75 else 6 if rng > 50 else 3 if rng > 25 else 0,
        "val":   10 if vs_mean < -10 else 8 if vs_mean < 0 else 6 if vs_mean < 10 else 3 if vs_mean < 20 else 0,
    }

    raw   = sum(sc.values())
    score = round(raw * 0.75) if regime == "BEAR" else raw
    action = "BUY" if score >= 68 else "HOLD" if score >= 40 else "REDUCE"  # Model C: B's BUY bar + A's HOLD/REDUCE

    return {
        "score":   score,
        "action":  action,
        "regime":  regime,
        "rsi":     round(rsi_val, 1),
        "v200":    round(v200, 1),
        "rs":      round(rs, 1),
        "hi52":    round(hi52, 2),
        "lo52":    round(lo52, 2),
        "rng":     int(round(rng)),
        "vsMean":  round(vs_mean, 1),
        "px":      round(px, 4),
        "sc":      sc,
    }


# ── MAIN ─────────────────────────────────────────────────────────────────────

def _load_cost() -> dict:
    """Load holdings_cost.json — local file (GitHub Actions/dev) or GitHub API (VM mode)."""
    if COST_BASIS.exists():
        return json.loads(COST_BASIS.read_text())
    if _GITHUB_TOKEN:
        print("  Loading holdings_cost.json from GitHub (VM mode)…")
        headers = {"Authorization": f"token {_GITHUB_TOKEN}",
                   "Accept": "application/vnd.github.v3+json"}
        import base64 as _b
        r = requests.get(
            f"https://api.github.com/repos/{_GITHUB_REPO}/contents/portfolio/data/holdings_cost.json?ref=main",
            headers=headers, timeout=15)
        if r.status_code == 200:
            return json.loads(_b.b64decode(r.json()["content"]).decode())
    return {}


def main() -> int:
    cost = _load_cost()
    if not cost:
        print(f"WARN  holdings_cost.json not found locally or via GitHub — skipping signals", file=sys.stderr)
        return 1
    us_open = cost.get("us", {}).get("open", [])
    if not us_open:
        print("WARN  no US open positions found", file=sys.stderr)
        return 1

    # ── SPY benchmark ────────────────────────────────────────────────────────
    print("Fetching SPY benchmark…")
    spy_result = fetch_history("SPY")
    spy_cl = spy_result[1] if spy_result else None

    if spy_cl is None:
        print("  WARN  SPY fetch failed — regime defaulting to BULL (signals may be optimistic)", file=sys.stderr)

    spy_summary: dict = {}
    regime = "BULL"
    if spy_cl:
        spy_px   = spy_cl[-1]
        spy_ma200 = sma(spy_cl, 200)
        pct      = round((spy_px - spy_ma200) / spy_ma200 * 100, 1)
        regime   = "BULL" if spy_px > spy_ma200 else "BEAR"
        spy_summary = {"px": round(spy_px, 2), "ma200": round(spy_ma200, 2), "pct_vs_200": pct}
        print(f"  SPY → ${spy_px:.2f}  vs 200MA {pct:+.1f}%  [{regime}]")

    # ── Score each US holding ─────────────────────────────────────────────────
    holdings_out: dict[str, dict] = {}
    seen_yf: dict[str, tuple] = {}  # cache by yf symbol (e.g. VOOG deduplication)

    for pos in us_open:
        tk  = pos["tk"]
        yfs = pos.get("yf", tk)
        print(f"  {tk:12s} ({yfs})…", end=" ")

        if yfs not in seen_yf:
            seen_yf[yfs] = fetch_history(yfs) or ()

        result = seen_yf[yfs]
        if not result:
            print("no data")
            continue

        ts, cl, vol = result
        if len(cl) < MIN_BARS_FOR_SIGNALS:
            print(
                f"  WARN  {tk} ({yfs}): only {len(cl)} bars available "
                f"(need ≥{MIN_BARS_FOR_SIGNALS}) — skipping signals",
                file=sys.stderr,
            )
            continue

        try:
            res = score_ticker(ts, cl, vol, spy_cl)
            res["sector"], res["industry"] = fetch_sector(yfs)
            holdings_out[tk] = res
            print(f"score={res['score']} [{res['action']}]  RSI={res['rsi']}  vs200={res['v200']:+.1f}%  sector={res['sector']}")
        except Exception as exc:
            print(f"ERROR scoring: {exc}", file=sys.stderr)

    # ── Score India holdings ──────────────────────────────────────────────────
    india_open = cost.get("india", {}).get("open", [])
    india_out: dict[str, dict] = {}
    seen_in_yf: dict[str, tuple] = {}

    if india_open:
        print(f"Scoring {len(india_open)} India holdings…")
        for pos in india_open:
            tk  = pos["tk"]
            yfs = pos.get("yf", tk + ".NS")
            print(f"  {tk:16s} ({yfs})…", end=" ")

            if yfs not in seen_in_yf:
                seen_in_yf[yfs] = fetch_history(yfs) or ()

            result = seen_in_yf[yfs]
            if not result:
                print("no data")
                continue

            ts, cl, vol = result
            if len(cl) < MIN_BARS_FOR_SIGNALS:
                print(f"only {len(cl)} bars — skip", file=sys.stderr)
                continue

            try:
                res = score_ticker(ts, cl, vol, spy_cl)
                res["sector"], res["industry"] = fetch_sector(yfs)
                india_out[tk] = res
                print(f"score={res['score']} [{res['action']}]  RSI={res['rsi']}  vs200={res['v200']:+.1f}%")
            except Exception as exc:
                print(f"ERROR: {exc}", file=sys.stderr)

    # ── Write output ──────────────────────────────────────────────────────────
    out = {
        "generated":      datetime.utcnow().isoformat() + "Z",
        "regime":         regime,
        "spy":            spy_summary,
        "holdings":       holdings_out,
        "india_holdings": india_out,
    }
    out_json = json.dumps(out, indent=2)

    # Write locally if repo structure exists (GitHub Actions / local dev)
    if OUT_SIGNALS.parent.exists() or not _GITHUB_TOKEN:
        OUT_SIGNALS.parent.mkdir(parents=True, exist_ok=True)
        OUT_SIGNALS.write_text(out_json)
        print(f"  wrote {OUT_SIGNALS.relative_to(ROOT)}  (US:{len(holdings_out)}  India:{len(india_out)} scored)")

    # Commit to GitHub if token available (VM mode — no local repo)
    if _GITHUB_TOKEN:
        _commit_to_github("portfolio/data/processed/stock_signals.json", out_json,
                          f"data: signals {datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')} [skip ci]")
        print(f"  wrote stock_signals.json → GitHub  (US:{len(holdings_out)}  India:{len(india_out)} scored)")

    # ── Append to signals history CSV (for ML training) ──────────────────────
    today = datetime.utcnow().strftime("%Y-%m-%d")
    csv_rows = []
    CSV_HEADER = "date,ticker,market,score,action,regime,rsi,v200,rs,hi52,lo52,rng,vsMean,px,sector,industry,spy_px,spy_pct_vs200\n"
    spy_px   = round(spy_summary.get("px", 0), 2)
    spy_pct  = round(spy_summary.get("pct_vs_200", 0), 2)

    for tk, r in {**{k: {**v, "_mkt": "US"}     for k, v in holdings_out.items()},
                  **{k: {**v, "_mkt": "India"}   for k, v in india_out.items()}}.items():
        csv_rows.append(
            f"{today},{tk},{r['_mkt']},{r['score']},{r['action']},{r['regime']},"
            f"{r['rsi']},{r['v200']},{r['rs']},{r['hi52']},{r['lo52']},"
            f"{r['rng']},{r['vsMean']},{r['px']},{r.get('sector','Unknown')},"
            f"{r.get('industry','')},"
            f"{spy_px},{spy_pct}\n"
        )

    if csv_rows:
        if _GITHUB_TOKEN:
            _append_signals_history(today, CSV_HEADER, csv_rows)
        else:
            hist_path = ROOT / "data" / "history" / "signals_history.csv"
            hist_path.parent.mkdir(parents=True, exist_ok=True)
            write_header = not hist_path.exists()
            with open(hist_path, "a") as f:
                if write_header: f.write(CSV_HEADER)
                f.writelines(csv_rows)
            print(f"  appended {len(csv_rows)} rows → signals_history.csv")

    return 0


# ── GitHub commit (VM mode) ───────────────────────────────────────────────────
import os as _os, base64 as _b64

_GITHUB_TOKEN = _os.environ.get("GITHUB_TOKEN", "")
_GITHUB_REPO  = "Tripurasundari-maa-sohay/Omm-Money"


def _append_signals_history(today: str, header: str, new_rows: list) -> None:
    """Read existing signals_history.csv from GitHub, strip today's rows (idempotent),
    append new rows, commit back."""
    import base64 as _b
    path    = "portfolio/data/history/signals_history.csv"
    headers = {"Authorization": f"token {_GITHUB_TOKEN}",
               "Accept": "application/vnd.github.v3+json"}
    url     = f"https://api.github.com/repos/{_GITHUB_REPO}/contents/{path}"
    r       = requests.get(url, headers=headers, timeout=10)
    if r.status_code == 200:
        existing = _b.b64decode(r.json()["content"]).decode()
        sha = r.json()["sha"]
        # Strip header + today's rows (idempotent re-run)
        lines = [l for l in existing.splitlines(keepends=True)
                 if not l.startswith(today) and not l.startswith("date,")]
    else:
        lines = []
        sha   = None
    content = header + "".join(lines) + "".join(new_rows)
    _commit_to_github(path, content,
        f"data: signals history {today} [skip ci]", sha)
    print(f"  appended {len(new_rows)} rows → signals_history.csv")


def _commit_to_github(path: str, content: str, message: str, sha: str = None) -> None:
    import time as _time
    headers = {"Authorization": f"token {_GITHUB_TOKEN}",
               "Accept": "application/vnd.github.v3+json"}
    url  = f"https://api.github.com/repos/{_GITHUB_REPO}/contents/{path}"
    enc  = _b64.b64encode(content.encode()).decode()
    if sha is None:
        r   = requests.get(url, headers=headers, timeout=10)
        sha = r.json().get("sha") if r.status_code == 200 else None
    body = {"message": message, "content": enc, "branch": "main"}
    if sha: body["sha"] = sha
    for attempt in range(1, 4):
        r = requests.put(url, headers=headers, json=body, timeout=20)
        if r.status_code in (200, 201):
            print(f"  Committed signals → GitHub OK"); return
        if r.status_code == 409:
            body["sha"] = requests.get(url, headers=headers, timeout=10).json().get("sha")
        else:
            print(f"  signals commit attempt {attempt}: {r.status_code}", file=sys.stderr)
        _time.sleep(5 * attempt)
    print("  All signal commit attempts failed", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
