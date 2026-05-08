"""
scripts/market_indices.py
=========================
Fetches S&P 500, NASDAQ, Nifty 50, Sensex via yfinance.
Writes → data/processed/market_indices.json

Runs entirely on GitHub Actions — no local Python needed.
Triggered by .github/workflows/full_update.yml every 15 minutes.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import pytz
import yfinance as yf

USA_TZ   = pytz.timezone("America/New_York")
INDIA_TZ = pytz.timezone("Asia/Kolkata")


def get_market_status(market: str) -> str:
    if market == "USA":
        now  = datetime.now(USA_TZ)
        wd   = now.weekday()
        mins = now.hour * 60 + now.minute
        if wd >= 5:              return "CLOSED"
        if 570 <= mins < 960:   return "OPEN"     # 9:30–16:00 ET
        if 240 <= mins < 570:   return "PREOPEN"  # 4:00–9:30 ET
        return "CLOSED"
    elif market == "India":
        now  = datetime.now(INDIA_TZ)
        wd   = now.weekday()
        mins = now.hour * 60 + now.minute
        if wd >= 5:              return "CLOSED"
        if 555 <= mins < 930:   return "OPEN"     # 9:15–15:30 IST
        if 540 <= mins < 555:   return "PREOPEN"  # 9:00–9:15 IST
        return "CLOSED"
    raise ValueError(f"Unknown market: {market}")


def fetch_index(symbol: str, name: str, tz) -> dict:
    try:
        hist = yf.Ticker(symbol).history(period="2d")
        if hist.empty:
            print(f"  WARNING  No data for {symbol}")
            return None
        current    = float(hist["Close"].iloc[-1])
        prev_close = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else float(hist["Open"].iloc[0])
        change     = current - prev_close
        change_pct = (change / prev_close) * 100 if prev_close else 0.0
        sign = "+" if change_pct >= 0 else ""
        print(f"  OK  {name}: {current:,.2f}  ({sign}{change_pct:.2f}%)")
        return {
            "ticker":     symbol,
            "name":       name,
            "current":    round(current, 2),
            "change":     round(change, 2),
            "change_pct": round(change_pct, 4),
            "prev_close": round(prev_close, 2),
            "timestamp":  datetime.now(tz).isoformat(),
        }
    except Exception as e:
        print(f"  ERROR  {symbol}: {e}")
        return None


def main():
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/processed/market_indices.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    usa_status   = get_market_status("USA")
    india_status = get_market_status("India")
    print(f"\nFetching indices  |  USA: {usa_status}  |  India: {india_status}\n")

    payload = {
        "usa_market": {
            "status":    usa_status,
            "indices": {
                "snp_500": fetch_index("^GSPC",  "S&P 500",  USA_TZ),
                "nasdaq":  fetch_index("^IXIC",  "NASDAQ",   USA_TZ),
            },
            "timestamp": datetime.now(USA_TZ).isoformat(),
        },
        "india_market": {
            "status":    india_status,
            "indices": {
                "nifty_50": fetch_index("^NSEI",  "Nifty 50", INDIA_TZ),
                "sensex":   fetch_index("^BSESN", "Sensex",   INDIA_TZ),
            },
            "timestamp": datetime.now(INDIA_TZ).isoformat(),
        },
    }

    out_path.write_text(json.dumps(payload, indent=2))
    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    main()
