"""
scripts/market_indices.py
=========================
Fetches S&P 500, NASDAQ, Nifty 50, Sensex + all India/US holdings via yfinance.
Writes → data/processed/market_indices.json

Runs entirely on GitHub Actions — no local Python needed.
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
        if 570 <= mins < 960:   return "OPEN"
        if 240 <= mins < 570:   return "PREOPEN"
        return "CLOSED"
    elif market == "India":
        now  = datetime.now(INDIA_TZ)
        wd   = now.weekday()
        mins = now.hour * 60 + now.minute
        if wd >= 5:              return "CLOSED"
        if 555 <= mins < 930:   return "OPEN"
        if 540 <= mins < 555:   return "PREOPEN"
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


def fetch_stock(symbol: str, name: str, qty: int, purchase_px: float, currency: str, tz) -> dict:
    """Fetch a single stock/ETF holding's live price."""
    try:
        hist = yf.Ticker(symbol).history(period="2d")
        if hist.empty:
            print(f"  WARNING  No data for {symbol}")
            return None
        current    = float(hist["Close"].iloc[-1])
        prev_close = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else current
        day_change     = current - prev_close
        day_change_pct = (day_change / prev_close) * 100 if prev_close else 0.0

        if purchase_px and purchase_px > 0:
            unreal_pl     = (current - purchase_px) * qty
            unreal_pl_pct = ((current - purchase_px) / purchase_px) * 100
        else:
            unreal_pl     = None
            unreal_pl_pct = None

        mkt_value = current * qty
        sign = "+" if day_change_pct >= 0 else ""
        print(f"  OK  {name} ({symbol}): {current:,.2f}  day {sign}{day_change_pct:.2f}%")
        return {
            "ticker":        symbol,
            "name":          name,
            "qty":           qty,
            "purchase_px":   purchase_px,
            "ltp":           round(current, 2),
            "prev_close":    round(prev_close, 2),
            "day_change":    round(day_change, 2),
            "day_change_pct":round(day_change_pct, 4),
            "mkt_value":     round(mkt_value, 2),
            "unreal_pl":     round(unreal_pl, 2) if unreal_pl is not None else None,
            "unreal_pl_pct": round(unreal_pl_pct, 4) if unreal_pl_pct is not None else None,
            "currency":      currency,
            "timestamp":     datetime.now(tz).isoformat(),
        }
    except Exception as e:
        print(f"  ERROR  {symbol}: {e}")
        return None


# ── US HOLDINGS (Doha Bank) ──────────────────────────────────────────────────
US_HOLDINGS = [
    # (yfinance_ticker, display_name, qty, purchase_px)
    ("TTE",  "TotalEnergies SE",              8,   82.84),
    ("AVGO", "Broadcom Inc.",                 4,   341.70),
    ("AMZN", "Amazon.com Inc.",              10,   249.73),
    ("MSFT", "Microsoft Corp.",               6,   416.07),
    ("GEV",  "GE Vernova Inc",                1,  1097.14),
    ("VOOG", "Vanguard S&P 500 Growth ETF",  18,    73.58),
    ("GOOG", "Alphabet Inc. Class C",         3,   310.19),
    ("MU",   "Micron Technology Inc.",        1,   492.00),
    ("RKLB", "Rocket Lab Corporation",       15,    80.91),
    ("EWY",  "iShares MSCI South Korea ETF", 11,   138.22),
    ("MP",   "MP Materials Corp.",           10,    53.00),
    ("GLW",  "Corning Inc.",                  6,   182.17),
]

# ── INDIA HOLDINGS ───────────────────────────────────────────────────────────
# NSE tickers on yfinance use ".NS" suffix
INDIA_HOLDINGS = [
    # (yfinance_ticker, display_name, broker, qty, purchase_px)
    ("IRBINVIT.NS",   "IRB InvIT Fund",                  "Motilal", 507,    60.26),
    ("ASHALOGIS.NS",  "Asha Logistics",                  "Mstock",  1000,   64.70),
    ("AVADHSUGAR.NS", "Avadh Sugar & Energy Ltd",        "Motilal", 248,   405.13),
    ("DIAMONDPWR.NS", "Diamond Power Infrastructure",    "Mstock",  100,   154.21),
    ("FILATEX.NS",    "Filatex Fashions Ltd",            "Upstox",  90000,   0.24),
    ("GMBREW.NS",     "G M Breweries Ltd",               "Motilal", 194,   875.38),
    ("GOLDBEES.NS",   "Nippon India ETF Gold BeES (MO)", "Motilal", 10937,  89.39),
    ("GOLDBEES.NS",   "Nippon India ETF Gold BeES (UP)", "Upstox",  1563,  122.47),
    ("JYOTISTRUC.NS", "Jyoti Structures Ltd",            "Mstock",  1936,   24.74),
    ("NBCC.NS",       "NBCC (India) Ltd",                "Motilal", 150,     None),
    ("PARAMATRIX.NS", "Paramatrix Technologies",         "Motilal", 2400,   82.86),
    ("RELIANCE.NS",   "Reliance Industries Ltd",         "Motilal", 248,  1397.43),
    ("SBIN.NS",       "State Bank of India",             "Motilal", 100,   700.43),
    ("WAAREEENER.NS", "Waaree Energies Ltd",             "Upstox",  15,   2182.36),
]


def main():
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/processed/market_indices.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    usa_status   = get_market_status("USA")
    india_status = get_market_status("India")
    print(f"\nFetching indices  |  USA: {usa_status}  |  India: {india_status}\n")

    # ── Index prices ──────────────────────────────────────────────────────────
    print("--- INDICES ---")
    snp    = fetch_index("^GSPC",  "S&P 500",  USA_TZ)
    nasdaq = fetch_index("^IXIC",  "NASDAQ",   USA_TZ)
    nifty  = fetch_index("^NSEI",  "Nifty 50", INDIA_TZ)
    sensex = fetch_index("^BSESN", "Sensex",   INDIA_TZ)

    # ── US Holdings ───────────────────────────────────────────────────────────
    print("\n--- US HOLDINGS ---")
    us_holdings_data = []
    for ticker, name, qty, px in US_HOLDINGS:
        result = fetch_stock(ticker, name, qty, px, "USD", USA_TZ)
        if result:
            us_holdings_data.append(result)

    # ── India Holdings ────────────────────────────────────────────────────────
    print("\n--- INDIA HOLDINGS ---")
    india_holdings_data = []
    for ticker, name, broker, qty, px in INDIA_HOLDINGS:
        result = fetch_stock(ticker, name, qty, px, "INR", INDIA_TZ)
        if result:
            result["broker"] = broker
            india_holdings_data.append(result)

    # ── Portfolio summaries ───────────────────────────────────────────────────
    us_total_value    = sum(h["mkt_value"]  for h in us_holdings_data)
    us_total_invested = sum(h["purchase_px"] * h["qty"] for h in us_holdings_data if h["purchase_px"])
    us_total_unreal   = sum(h["unreal_pl"]  for h in us_holdings_data if h["unreal_pl"] is not None)
    us_day_pl         = sum(h["day_change"] * h["qty"] for h in us_holdings_data)

    india_total_value    = sum(h["mkt_value"]  for h in india_holdings_data)
    india_total_invested = sum(h["purchase_px"] * h["qty"] for h in india_holdings_data if h["purchase_px"])
    india_total_unreal   = sum(h["unreal_pl"]  for h in india_holdings_data if h["unreal_pl"] is not None)
    india_day_pl         = sum(h["day_change"] * h["qty"] for h in india_holdings_data)

    print(f"\n  US  Total Value: ${us_total_value:,.2f}  |  Unrealised P/L: ${us_total_unreal:,.2f}")
    print(f"  IN  Total Value: ₹{india_total_value:,.2f}  |  Unrealised P/L: ₹{india_total_unreal:,.2f}")

    payload = {
        "usa_market": {
            "status":    usa_status,
            "indices": {
                "snp_500": snp,
                "nasdaq":  nasdaq,
            },
            "holdings":  us_holdings_data,
            "summary": {
                "total_value":    round(us_total_value, 2),
                "total_invested": round(us_total_invested, 2),
                "total_unreal_pl":round(us_total_unreal, 2),
                "day_pl":         round(us_day_pl, 2),
                "cash":           46.84,
                "account_value":  round(us_total_value + 46.84, 2),
            },
            "timestamp": datetime.now(USA_TZ).isoformat(),
        },
        "india_market": {
            "status":    india_status,
            "indices": {
                "nifty_50": nifty,
                "sensex":   sensex,
            },
            "holdings":  india_holdings_data,
            "summary": {
                "total_value":    round(india_total_value, 2),
                "total_invested": round(india_total_invested, 2),
                "total_unreal_pl":round(india_total_unreal, 2),
                "day_pl":         round(india_day_pl, 2),
            },
            "timestamp": datetime.now(INDIA_TZ).isoformat(),
        },
    }

    out_path.write_text(json.dumps(payload, indent=2))
    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    main()
