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

import pytz
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
    try:
        t = yf.Ticker(yf_symbol)
        # 2d history gives us today's last price + previous close
        hist = t.history(period="2d", interval="1d", auto_adjust=False)
        if hist.empty:
            return None
        ltp = float(hist["Close"].iloc[-1])
        pc  = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else ltp
        return {"ltp": round(ltp, 4), "pc": round(pc, 4)}
    except Exception as exc:
        print(f"  WARN  {yf_symbol}: {exc}", file=sys.stderr)
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
    for tk, yf_sym in tickers:
        q = fetch_quote(yf_sym)
        if q is None:
            continue
        change     = q["ltp"] - q["pc"]
        change_pct = (change / q["pc"]) * 100 if q["pc"] else 0.0
        prices[tk] = {
            "ltp":        q["ltp"],
            "pc":         q["pc"],
            "change":     round(change, 4),
            "change_pct": round(change_pct, 2),
            "as_of":      datetime.utcnow().isoformat() + "Z",
        }
        print(f"  {tk:12s} ({yf_sym:14s}) → {q['ltp']:>12,.2f}  ({change_pct:+.2f}%)")

    return {"generated": datetime.utcnow().isoformat() + "Z", "prices": prices}


# ── MAIN ─────────────────────────────────────────────────────────────────
def main() -> int:
    OUT_INDICES.parent.mkdir(parents=True, exist_ok=True)

    indices = build_indices_json()
    OUT_INDICES.write_text(json.dumps(indices, indent=2))
    print(f"  wrote {OUT_INDICES.relative_to(ROOT)}")

    holdings = build_holdings_json()
    OUT_HOLDINGS.write_text(json.dumps(holdings, indent=2))
    print(f"  wrote {OUT_HOLDINGS.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
