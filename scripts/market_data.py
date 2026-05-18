"""
market_data.py — single source of all live market data.
Run on GitHub Actions every 15 minutes.

Outputs:
  data/processed/market_indices.json    — S&P 500, Nasdaq, Nifty 50, Sensex
  data/processed/holdings_prices.json   — live LTP + previous close for every
                                          ticker in data/holdings_cost.json

The dashboard reads both JSONs. Cost basis (qty, avg) stays in
holdings_cost.json (you edit this when you trade) — never overwritten here.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import time

import pytz
import requests
import yfinance as yf

# ── PATHS ────────────────────────────────────────────────────────────────
ROOT          = Path(__file__).resolve().parent.parent
COST_BASIS    = ROOT / "data" / "holdings_cost.json"
OUT_INDICES   = ROOT / "data" / "processed" / "market_indices.json"
OUT_HOLDINGS  = ROOT / "data" / "processed" / "holdings_prices.json"

INDICES = {
    "usa": {
        "snp_500": "^GSPC",
        "nasdaq":  "^IXIC",
    },
    "india": {
        "nifty_50": "^NSEI",
        "sensex":   "^BSESN",
    },
}


# ── HELPERS ──────────────────────────────────────────────────────────────
def market_status(market: str) -> str:
    """OPEN / CLOSED / PREOPEN based on local-market clock."""
    if market == "usa":
        tz   = pytz.timezone("America/New_York")
        now  = datetime.now(tz)
        wd   = now.weekday()
        mins = now.hour * 60 + now.minute
        if wd >= 5:
            return "CLOSED"
        if 9 * 60 + 30 <= mins < 16 * 60:
            return "OPEN"
        if 4 * 60 <= mins < 9 * 60 + 30:
            return "PREOPEN"
        return "CLOSED"
    if market == "india":
        tz   = pytz.timezone("Asia/Kolkata")
        now  = datetime.now(tz)
        wd   = now.weekday()
        mins = now.hour * 60 + now.minute
        if wd >= 5:
            return "CLOSED"
        if 9 * 60 + 15 <= mins < 15 * 60 + 30:
            return "OPEN"
        if 9 * 60 <= mins < 9 * 60 + 15:
            return "PREOPEN"
        return "CLOSED"
    return "CLOSED"


def fetch_quote(yf_symbol: str) -> dict | None:
    """Return {'ltp': float, 'pc': float} or None on failure."""
    # Attempt 1: fast_info — queries Yahoo's live quote endpoint,
    # gives true intraday LTP + official previous close.
    try:
        fi = yf.Ticker(yf_symbol).fast_info
        ltp = fi.last_price
        pc  = fi.previous_close
        # Explicit None check: fast_info can silently return None without raising
        if ltp is not None and float(ltp) > 0:
            if pc is None:
                # previous_close not yet published (e.g. market just opened) →
                # fall through to history() which has yesterday's actual close
                raise ValueError("previous_close is None — falling through to history fallback")
            return {"ltp": round(float(ltp), 4), "pc": round(float(pc), 4)}
    except Exception as exc:
        print(f"  WARN  fast_info {yf_symbol}: {exc}", file=sys.stderr)

    # Attempt 2: daily history fallback (works when fast_info is unavailable)
    try:
        hist = yf.Ticker(yf_symbol).history(period="2d", interval="1d", auto_adjust=False)
        if hist.empty:
            return None
        ltp = float(hist["Close"].iloc[-1])
        pc  = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else ltp
        return {"ltp": round(ltp, 4), "pc": round(pc, 4)}
    except Exception as exc:
        print(f"  WARN  history {yf_symbol}: {exc}", file=sys.stderr)
        return None


def fetch_quote_india_sme(yf_symbol: str) -> dict | None:
    """
    Fallback price fetcher for NSE SME/Emerge stocks that yfinance cannot
    resolve.  Tries two sources in order:

      1. NSE India quote-equity API  (may be blocked from GitHub Actions IPs)
      2. Screener.in company API     (public, no login required)

    Returns {'ltp': float, 'pc': float} or None if both fail.
    """
    base = yf_symbol.replace(".NS", "").replace(".BO", "").upper()

    # ── attempt 1: NSE India API ─────────────────────────────────────────
    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/",
    }
    try:
        session = requests.Session()
        session.get("https://www.nseindia.com/", headers=_HEADERS, timeout=8)
        time.sleep(1)
        r = session.get(
            f"https://www.nseindia.com/api/quote-equity?symbol={base}",
            headers=_HEADERS,
            timeout=8,
        )
        if r.status_code == 200:
            data = r.json()
            ltp = float(data["priceInfo"]["lastPrice"])
            pc  = float(data["priceInfo"]["previousClose"])
            print(f"  SME/NSE  {base} → {ltp:.2f}")
            return {"ltp": round(ltp, 4), "pc": round(pc, 4)}
    except Exception as exc:
        print(f"  WARN  NSE API {base}: {exc}", file=sys.stderr)

    # ── attempt 2: Screener.in ───────────────────────────────────────────
    try:
        r = requests.get(
            f"https://www.screener.in/api/company/{base}/",
            headers={"User-Agent": _HEADERS["User-Agent"]},
            timeout=8,
        )
        if r.status_code == 200:
            data = r.json()
            ltp = data.get("current_price")
            if ltp:
                ltp = round(float(ltp), 4)
                print(f"  SME/Screener  {base} → {ltp:.2f}")
                # Screener doesn't expose prev-close — try to get it from NSE API
                pc = None
                try:
                    nse_r = requests.get(
                        f"https://www.nseindia.com/api/quote-equity?symbol={base}",
                        headers=_HEADERS, timeout=8,
                    )
                    if nse_r.status_code == 200:
                        nse_d = nse_r.json()
                        pc = float(nse_d.get("priceInfo", {}).get("previousClose", 0)) or None
                except Exception:
                    pass
                # If still no prev-close, return None so caller shows unknown dayPL
                if pc is None:
                    return {"ltp": ltp, "pc": None}
                return {"ltp": ltp, "pc": round(pc, 4)}
    except Exception as exc:
        print(f"  WARN  Screener {base}: {exc}", file=sys.stderr)

    return None


# ── INDICES ──────────────────────────────────────────────────────────────
def build_indices_json() -> dict:
    print("Fetching index quotes…")
    out = {"generated": datetime.utcnow().isoformat() + "Z"}
    for market, mapping in INDICES.items():
        idx_block = {}
        for key, yf_sym in mapping.items():
            q = fetch_quote(yf_sym)
            if q is None:
                idx_block[key] = None
                continue
            change     = q["ltp"] - q["pc"]
            change_pct = (change / q["pc"]) * 100 if q["pc"] else 0.0
            idx_block[key] = {
                "current":    q["ltp"],
                "prev_close": q["pc"],
                "change":     round(change, 2),
                "change_pct": round(change_pct, 2),
            }
            print(f"  {yf_sym:8s} → {q['ltp']:>12,.2f}  ({change_pct:+.2f}%)")
        out[market + "_market"] = {
            "status":    market_status(market),
            "indices":   idx_block,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
    return out


# ── HOLDINGS ─────────────────────────────────────────────────────────────
def build_holdings_json() -> dict:
    if not COST_BASIS.exists():
        print(f"  WARN  no {COST_BASIS} — skipping holdings fetch")
        return {"generated": datetime.utcnow().isoformat() + "Z", "prices": {}}

    cost = json.loads(COST_BASIS.read_text())
    tickers: list[tuple[str, str]] = []
    for region in ("us", "india"):
        for p in cost.get(region, {}).get("open", []):
            tickers.append((p["tk"], p["yf"]))

    print(f"Fetching {len(tickers)} holding quotes…")
    prices: dict[str, dict] = {}
    success_count = 0
    for tk, yf_sym in tickers:
        try:
            q = fetch_quote(yf_sym)
            if q is None and yf_sym.endswith(".NS"):
                print(f"  INFO  {tk}: yfinance empty — trying SME fallback…", file=sys.stderr)
                q = fetch_quote_india_sme(yf_sym)
            if q is None:
                print(f"  FAIL  {tk} ({yf_sym}): no price obtained", file=sys.stderr)
                continue
            pc         = q["pc"]    # may be None for SME stocks with no prev-close
            change     = round(q["ltp"] - pc, 4) if pc is not None else None
            change_pct = round(change / pc * 100, 2) if (change is not None and pc) else None
            prices[tk] = {
                "ltp":        q["ltp"],
                "pc":         pc,          # None means "unknown" — JS shows "–"
                "change":     change,
                "change_pct": change_pct,
                "as_of":      datetime.utcnow().isoformat() + "Z",
            }
            success_count += 1
            print(f"  {tk:12s} ({yf_sym:14s}) → {q['ltp']:>12,.2f}  ({change_pct:+.2f}%)")
        except Exception as exc:
            print(f"  ERROR  {tk} ({yf_sym}): unexpected error: {exc}", file=sys.stderr)

    # Guard: only write if at least 50% of tickers got prices
    min_required = max(1, len(tickers) // 2)
    if success_count < min_required:
        print(
            f"  WARN  only {success_count}/{len(tickers)} tickers resolved "
            f"(need ≥{min_required}) — skipping holdings_prices.json write to avoid overwriting good data",
            file=sys.stderr,
        )
        return {"generated": datetime.utcnow().isoformat() + "Z", "prices": {}, "_write_skipped": True}

    return {"generated": datetime.utcnow().isoformat() + "Z", "prices": prices}


# ── FX RATE ──────────────────────────────────────────────────────────────
def fetch_live_fx() -> float | None:
    """Fetch live INR/USD rate from Yahoo Finance. Returns None on failure."""
    try:
        q = fetch_quote("INR=X")          # Yahoo symbol for USD/INR
        if q and q["ltp"] and q["ltp"] > 0:
            return round(q["ltp"], 4)
    except Exception:
        pass
    try:
        # Fallback: yfinance direct
        fi = yf.Ticker("INR=X").fast_info
        rate = fi.last_price
        if rate and rate > 0:
            return round(float(rate), 4)
    except Exception:
        pass
    return None


def update_fx_in_cost_basis(rate: float) -> None:
    """Write live fx_inr_usd back to holdings_cost.json (only field updated)."""
    if not COST_BASIS.exists():
        return
    try:
        cost = json.loads(COST_BASIS.read_text())
        cost["fx_inr_usd"] = rate
        COST_BASIS.write_text(json.dumps(cost, indent=2))
        print(f"  FX rate updated → ₹{rate}/USD")
    except Exception as exc:
        print(f"  WARN  FX update failed: {exc}", file=sys.stderr)


# ── DEMO TICKER PRICES ───────────────────────────────────────────────────
DEMO_PORTFOLIO_FILE = ROOT / "data" / "demo_portfolio.json"

def build_demo_prices() -> dict:
    """Read data/demo_portfolio.json, fetch live LTP+PC for every open position.
    Stored under 'demo_prices' key — never mixed with real holdings prices."""
    demo: dict[str, dict] = {}
    if not DEMO_PORTFOLIO_FILE.exists():
        print("  WARN  demo_portfolio.json not found — skipping demo prices", file=sys.stderr)
        return demo

    try:
        dp = json.loads(DEMO_PORTFOLIO_FILE.read_text())
    except Exception as exc:
        print(f"  WARN  demo_portfolio.json parse error: {exc}", file=sys.stderr)
        return demo

    tickers: list[tuple[str, str]] = []
    for region in ("us", "india"):
        for p in dp.get(region, {}).get("open", []):
            tickers.append((p["tk"], p.get("yf", p["tk"])))

    print(f"Fetching {len(tickers)} demo ticker prices…")
    for tk, yf_sym in tickers:
        try:
            q = fetch_quote(yf_sym)
            if q:
                demo[tk] = {
                    "ltp":   q["ltp"],
                    "pc":    q["pc"],
                    "as_of": datetime.utcnow().isoformat() + "Z",
                }
                print(f"  demo {tk:12s} ({yf_sym:16s}) → {q['ltp']:>12,.2f}")
            else:
                print(f"  demo {tk}: no price", file=sys.stderr)
        except Exception as exc:
            print(f"  demo {tk}: error {exc}", file=sys.stderr)
    return demo


# ── MAIN ─────────────────────────────────────────────────────────────────
def main() -> int:
    OUT_INDICES.parent.mkdir(parents=True, exist_ok=True)

    # Live FX rate — update holdings_cost.json so dashboard uses fresh rate
    print("Fetching live INR/USD rate…")
    fx = fetch_live_fx()
    if fx:
        update_fx_in_cost_basis(fx)
    else:
        print("  WARN  FX fetch failed — using stored rate", file=sys.stderr)

    indices = build_indices_json()
    OUT_INDICES.write_text(json.dumps(indices, indent=2))
    print(f"  wrote {OUT_INDICES.relative_to(ROOT)}")

    holdings = build_holdings_json()
    if holdings.get("_write_skipped"):
        print(f"  SKIP  {OUT_HOLDINGS.relative_to(ROOT)} (insufficient price data)")
    else:
        holdings["demo_prices"] = build_demo_prices()
        OUT_HOLDINGS.write_text(json.dumps(holdings, indent=2))
        print(f"  wrote {OUT_HOLDINGS.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
