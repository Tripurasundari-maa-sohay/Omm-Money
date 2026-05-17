"""
screener.py — full-universe signal screener for the portfolio dashboard.

Scans:
  US:     S&P 500 + Nasdaq 100   (benchmark: SPY)
  India:  Nifty 500              (benchmark: ^NSEI)
  Macro:  Gold, Silver, Oil, Bonds, BTC

Writes: data/processed/screener.json

Runs via GitHub Actions (.github/workflows/screener.yml).
The dashboard Screener tab reads this JSON — zero browser-side computation.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import io
import requests
import pandas as pd

# ── PATH SETUP ───────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).parent))

# Import scoring functions from signals_update.py
from signals_update import fetch_history, score_ticker, MIN_BARS_FOR_SIGNALS, sma

OUT_SCREENER = ROOT / "data" / "processed" / "screener.json"

_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

def _wiki_tables(url: str) -> list:
    """Fetch Wikipedia page with browser UA, parse all tables."""
    resp = requests.get(url, headers=_HEADERS, timeout=20)
    resp.raise_for_status()
    return pd.read_html(io.StringIO(resp.text))


# ── UNIVERSE FETCH ────────────────────────────────────────────────────────────

def get_sp500_tickers() -> tuple[list[str], dict[str, str]]:
    """Returns (tickers, sector_map) from Wikipedia SP500 table."""
    try:
        tables = _wiki_tables("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
        df = tables[0]
        tickers = [t.replace(".", "-") for t in df["Symbol"].tolist()]
        sector_col = "GICS Sector" if "GICS Sector" in df.columns else df.columns[2]
        sectors = {
            t.replace(".", "-"): str(s)
            for t, s in zip(df["Symbol"], df[sector_col])
        }
        return tickers, sectors
    except Exception as e:
        print(f"WARN SP500 fetch failed: {e}", file=sys.stderr)
        return [], {}


def get_ndx100_tickers() -> tuple[list[str], dict[str, str]]:
    """Returns (tickers, sector_map) from Wikipedia NDX100 table."""
    try:
        tables = _wiki_tables("https://en.wikipedia.org/wiki/Nasdaq-100")
        for t in tables:
            if "Ticker" in t.columns:
                tickers = [
                    tk.replace(".", "-")
                    for tk in t["Ticker"].tolist()
                    if isinstance(tk, str) and tk not in ("Ticker", "nan")
                ]
                sector_col = next(
                    (c for c in t.columns if "sector" in c.lower() or "industry" in c.lower()),
                    None,
                )
                sectors: dict[str, str] = {}
                if sector_col:
                    for tk, s in zip(t["Ticker"], t[sector_col]):
                        if isinstance(tk, str) and tk not in ("Ticker", "nan"):
                            sectors[tk.replace(".", "-")] = str(s)
                return tickers, sectors
        return [], {}
    except Exception as e:
        print(f"WARN NDX100 fetch failed: {e}", file=sys.stderr)
    return [], {}


def get_nifty500_tickers() -> tuple[list[str], dict[str, str]]:
    """
    Returns (yf_symbols, sector_map) for Nifty 500 from NSE public CSV.
    yf symbols are in the form SYMBOL.NS (e.g. RELIANCE.NS).
    Falls back to Nifty 50 Wikipedia table if NSE CSV is unavailable.
    """
    # Primary: NSE official Nifty 500 index constituent CSV
    NSE_URLS = [
        "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
        "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
    ]
    for url in NSE_URLS:
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=30)
            resp.raise_for_status()
            df = pd.read_csv(io.StringIO(resp.text))
            # NSE CSV columns: Company Name, Industry, Symbol, Series, ISIN Code
            sym_col = next((c for c in df.columns if "symbol" in c.lower()), None)
            ind_col = next((c for c in df.columns if "industry" in c.lower()), None)
            if sym_col is None:
                continue
            symbols = [str(s).strip() for s in df[sym_col] if str(s).strip() and str(s).strip() != "nan"]
            sectors = {}
            if ind_col:
                for s, ind in zip(df[sym_col], df[ind_col]):
                    sectors[str(s).strip() + ".NS"] = str(ind).strip()
            yf_syms = [s + ".NS" for s in symbols]
            print(f"  Nifty 500: {len(yf_syms)} tickers from {url.split('//')[-1].split('/')[0]}")
            return yf_syms, sectors
        except Exception as e:
            print(f"  WARN Nifty500 fetch from {url}: {e}", file=sys.stderr)

    # Fallback: Nifty 50 from Wikipedia
    try:
        tables = _wiki_tables("https://en.wikipedia.org/wiki/NIFTY_50")
        for t in tables:
            sym_col = next((c for c in t.columns if "symbol" in c.lower() or "ticker" in c.lower()), None)
            if sym_col:
                syms = [str(s).strip() for s in t[sym_col] if isinstance(s, str) and s.strip()]
                print(f"  WARN using Nifty 50 fallback ({len(syms)} tickers)", file=sys.stderr)
                return [s + ".NS" for s in syms], {}
    except Exception as e:
        print(f"  WARN Nifty50 Wikipedia fallback failed: {e}", file=sys.stderr)

    return [], {}


# ── MAIN ─────────────────────────────────────────────────────────────────────

def main() -> int:
    print("Fetching universe tickers…")
    sp500, sp500_sectors   = get_sp500_tickers()
    ndx100, ndx100_sectors = get_ndx100_tickers()

    # Merged sector map — SP500 GICS data takes precedence
    sector_map: dict[str, str] = {**ndx100_sectors, **sp500_sectors}

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
                "sector":  sector_map.get(tk, "Unknown"),
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

    # ── Global commodities & macro signals ────────────────────────────────────
    # Gold (GLD), Silver (SLV), Oil (USO), Bonds (TLT), Bitcoin (BTC-USD)
    COMMODITIES = [
        ("GLD",     "Gold (SPDR ETF)",       "Precious Metals"),
        ("GC=F",    "Gold Futures (COMEX)",  "Precious Metals"),
        ("XAUUSD=X","Gold Spot (USD/oz)",    "Precious Metals"),
        ("SLV",     "Silver (iShares ETF)",  "Precious Metals"),
        ("USO",     "Crude Oil (ETF)",       "Energy"),
        ("TLT",     "US 20yr Treasury ETF",  "Bonds"),
        ("BTC-USD", "Bitcoin",               "Crypto"),
        ("GOLDBEES.NS", "Gold BeES (NSE)",   "Precious Metals"),
    ]
    commodities_out: list[dict] = []
    print("\nScoring commodities & macro…")
    for sym, label, cat in COMMODITIES:
        try:
            data = fetch_history(sym)
            if not data:
                print(f"  {sym:14s} → no data")
                continue
            ts, cl, vol = data
            if len(cl) < MIN_BARS_FOR_SIGNALS:
                print(f"  {sym:14s} → only {len(cl)} bars — skip")
                continue
            res = score_ticker(ts, cl, vol, spy_cl)
            px  = cl[-1]
            print(f"  {sym:14s} → score={res['score']} [{res['action']}]  RSI={res['rsi']}  vs200={res['v200']:+.1f}%  px={px:.2f}")
            commodities_out.append({
                "tk":      sym,
                "label":   label,
                "category": cat,
                "px":      round(px, 4),
                "score":   res["score"],
                "action":  res["action"],
                "rsi":     res["rsi"],
                "v200":    res["v200"],
                "rs":      res["rs"],
                "rng":     res["rng"],
                "vsMean":  res["vsMean"],
                "regime":  res["regime"],
            })
        except Exception as exc:
            print(f"  {sym:14s} → ERROR: {exc}", file=sys.stderr)
        time.sleep(0.3)

    # ── Nifty 500 — India universe ────────────────────────────────────────────
    print("\nFetching Nifty 500 universe…")
    nifty500, nifty_sectors = get_nifty500_tickers()

    # India benchmark: Nifty 50 index
    print("Fetching Nifty 50 benchmark (^NSEI)…")
    nsei_result = fetch_history("^NSEI")
    nsei_cl = nsei_result[1] if nsei_result else None
    nsei_summary: dict = {}
    india_regime = "BULL"
    if nsei_cl:
        nsei_px    = nsei_cl[-1]
        nsei_ma200 = sma(nsei_cl, 200)
        nsei_pct   = round((nsei_px - nsei_ma200) / nsei_ma200 * 100, 1)
        india_regime = "BULL" if nsei_px > nsei_ma200 else "BEAR"
        nsei_summary = {"px": round(nsei_px, 2), "ma200": round(nsei_ma200, 2), "pct_vs_200": nsei_pct}
        print(f"  ^NSEI → ₹{nsei_px:,.2f}  vs 200MA {nsei_pct:+.1f}%  [{india_regime}]")
    else:
        print("  WARN  ^NSEI fetch failed — India regime defaulting to BULL", file=sys.stderr)

    india_results: list[dict] = []
    india_errors = 0
    india_total  = len(nifty500)

    for i, yf_sym in enumerate(nifty500):
        tk = yf_sym.replace(".NS", "")
        try:
            data = fetch_history(yf_sym)
            if data is None:
                india_errors += 1
                time.sleep(0.2)
                continue

            ts, cl, vol = data
            if len(cl) < MIN_BARS_FOR_SIGNALS:
                india_errors += 1
                time.sleep(0.2)
                continue

            res = score_ticker(ts, cl, vol, nsei_cl)   # RS vs Nifty, not SPY
            # Override regime with India regime
            res["regime"] = india_regime
            if india_regime == "BEAR":
                res["score"] = round(res["score"] * 0.75)
                res["action"] = "BUY" if res["score"] >= 68 else "HOLD" if res["score"] >= 40 else "REDUCE"

            print(f"[{i+1}/{india_total}] {tk:16s} → score={res['score']} [{res['action']}]  RSI={res['rsi']}")
            india_results.append({
                "tk":      tk,
                "yf":      yf_sym,
                "sector":  nifty_sectors.get(yf_sym, "Unknown"),
                "score":   res["score"],
                "action":  res["action"],
                "rsi":     res["rsi"],
                "v200":    res["v200"],
                "rs":      res["rs"],
                "rng":     res["rng"],
                "vsMean":  res["vsMean"],
                "regime":  res["regime"],
                "px":      round(cl[-1], 2),
            })
        except Exception as exc:
            print(f"[{i+1}/{india_total}] {tk} → ERROR: {exc}", file=sys.stderr)
            india_errors += 1
        time.sleep(0.25)

    india_results.sort(key=lambda r: r["score"], reverse=True)
    print(f"\nIndia done — {len(india_results)}/{india_total} scored  ({india_errors} skipped)")

    # ── Write output ──────────────────────────────────────────────────────────
    OUT_SCREENER.parent.mkdir(parents=True, exist_ok=True)
    out = {
        "generated":      datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        # US
        "regime":         regime,
        "spy":            spy_summary,
        "count":          succeeded,
        "tickers":        results,
        # India
        "india_regime":   india_regime,
        "nsei":           nsei_summary,
        "india_count":    len(india_results),
        "india_tickers":  india_results,
        # Macro
        "commodities":    commodities_out,
    }
    OUT_SCREENER.write_text(json.dumps(out, indent=2))
    print(
        f"\nDone.\n"
        f"  US:     {succeeded}/{total} scored  ({errors} skipped)\n"
        f"  India:  {len(india_results)}/{india_total} scored  ({india_errors} skipped)\n"
        f"  Macro:  {len(commodities_out)} commodities\n"
        f"Wrote {OUT_SCREENER.relative_to(ROOT)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
