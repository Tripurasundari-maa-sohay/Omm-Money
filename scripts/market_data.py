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
    """OPEN / CLOSED / PREMARKET / POSTMARKET / RESET based on local-market clock.
    RESET = 2-hour window before open (7:30–9:30 AM ET) — dashboard blanks daily P&L.
    PREMARKET = early pre-market (4:00–7:30 AM ET).
    POSTMARKET = extended after-hours (4:00–8:00 PM ET).
    """
    if market == "usa":
        tz   = pytz.timezone("America/New_York")
        now  = datetime.now(tz)
        wd   = now.weekday()
        mins = now.hour * 60 + now.minute
        if wd >= 5:
            return "CLOSED"
        if 9 * 60 + 30 <= mins < 16 * 60:
            return "OPEN"
        if 16 * 60 <= mins < 20 * 60:
            return "POSTMARKET"          # 4:00 PM – 8:00 PM ET
        if 7 * 60 + 30 <= mins < 9 * 60 + 30:
            return "RESET"               # 7:30 AM – 9:30 AM ET — daily P&L blanked
        if 4 * 60 <= mins < 7 * 60 + 30:
            return "PREMARKET"           # 4:00 AM – 7:30 AM ET
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


_YF_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def fetch_quote_direct(yf_symbol: str) -> dict | None:
    """
    Attempt 3 fallback: direct HTTP to Yahoo Finance chart API, bypassing the
    yfinance library entirely.  Tries query1 then query2.  Works for US + India.
    Returns {'ltp': float, 'pc': float|None} or None on failure.
    """
    for host in ("query1", "query2"):
        try:
            url = (
                f"https://{host}.finance.yahoo.com/v8/finance/chart/{yf_symbol}"
                f"?interval=1d&range=5d&includePrePost=false"
            )
            r = requests.get(url, headers=_YF_HEADERS, timeout=10)
            if r.status_code != 200:
                continue
            result = r.json().get("chart", {}).get("result", [None])[0]
            if not result:
                continue
            meta = result.get("meta", {})
            ltp  = meta.get("regularMarketPrice")
            pc   = meta.get("chartPreviousClose") or meta.get("previousClose")
            if ltp and float(ltp) > 0:
                print(f"  direct/{host}  {yf_symbol} → {float(ltp):.2f}")
                return {
                    "ltp": round(float(ltp), 4),
                    "pc":  round(float(pc), 4) if pc else None,
                }
        except Exception as exc:
            print(f"  WARN  direct/{host} {yf_symbol}: {exc}", file=sys.stderr)
    return None


def fetch_quote(yf_symbol: str) -> dict | None:
    """
    Return {'ltp': float, 'pc': float|None} or None on complete failure.
    Three-layer fallback:
      1. yfinance fast_info  (live intraday price + official prev close)
      2. yfinance history()  (2d daily candles — slower, survives rate limits)
      3. Direct Yahoo chart API via requests (bypasses yfinance library)
    """
    # Attempt 1: fast_info
    try:
        fi  = yf.Ticker(yf_symbol).fast_info
        ltp = fi.last_price
        pc  = fi.previous_close
        if ltp is not None and float(ltp) > 0:
            if pc is None:
                raise ValueError("previous_close is None — falling through")
            return {"ltp": round(float(ltp), 4), "pc": round(float(pc), 4)}
    except Exception as exc:
        print(f"  WARN  fast_info {yf_symbol}: {exc}", file=sys.stderr)

    # Attempt 2: yfinance history (2d candles)
    try:
        hist = yf.Ticker(yf_symbol).history(period="5d", interval="1d", auto_adjust=False)
        if not hist.empty:
            # Use last two rows to get true ltp + prev-close
            # history() during live session: iloc[-1] = today's last close-so-far
            # iloc[-2] = yesterday's actual close
            ltp = float(hist["Close"].iloc[-1])
            pc  = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else None
            if ltp > 0:
                return {"ltp": round(ltp, 4), "pc": round(pc, 4) if pc else None}
    except Exception as exc:
        print(f"  WARN  history {yf_symbol}: {exc}", file=sys.stderr)

    # Attempt 3: direct Yahoo Finance chart API (independent of yfinance)
    return fetch_quote_direct(yf_symbol)


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

    # Load existing prices for merge fallback (keeps stale data for tickers that fail this run)
    existing_prices: dict[str, dict] = {}
    if OUT_HOLDINGS.exists():
        try:
            existing_prices = json.loads(OUT_HOLDINGS.read_text()).get("prices", {})
        except Exception:
            pass

    print(f"Fetching {len(tickers)} holding quotes…")
    fresh_prices: dict[str, dict] = {}
    success_count = 0
    for tk, yf_sym in tickers:
        try:
            q = fetch_quote(yf_sym)
            is_india = yf_sym.endswith((".NS", ".BO"))

            # India: if fetch failed entirely → SME fallback
            if q is None and is_india:
                print(f"  INFO  {tk}: all yfinance attempts empty — trying NSE/Screener fallback…", file=sys.stderr)
                q = fetch_quote_india_sme(yf_sym)

            # India: if ltp OK but pc missing → use SME fallback just to get pc
            if q is not None and q.get("pc") is None and is_india:
                print(f"  INFO  {tk}: ltp={q['ltp']} but pc=None — fetching pc via NSE/Screener…", file=sys.stderr)
                sme = fetch_quote_india_sme(yf_sym)
                if sme and sme.get("pc") is not None:
                    q["pc"] = sme["pc"]
                    print(f"  INFO  {tk}: pc resolved → {q['pc']}", file=sys.stderr)

            # US: if ltp OK but pc missing → try direct API for pc
            if q is not None and q.get("pc") is None and not is_india:
                print(f"  INFO  {tk}: ltp={q['ltp']} but pc=None — retrying direct API for pc…", file=sys.stderr)
                direct = fetch_quote_direct(yf_sym)
                if direct and direct.get("pc") is not None:
                    q["pc"] = direct["pc"]
                    print(f"  INFO  {tk}: pc resolved → {q['pc']}", file=sys.stderr)

            if q is None:
                print(f"  FAIL  {tk} ({yf_sym}): no price obtained after all fallbacks", file=sys.stderr)
                continue
            pc         = q["pc"]    # may still be None if all sources lack prev-close
            change     = round(q["ltp"] - pc, 4) if pc is not None else None
            change_pct = round(change / pc * 100, 2) if (change is not None and pc) else None
            fresh_prices[tk] = {
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

    # Merge: start from existing prices, overlay fresh results.
    # Tickers that failed this run keep their previous price (with original as_of timestamp).
    # This ensures dayPL never freezes due to partial fetch failures at market open.
    merged = {**existing_prices, **fresh_prices}

    # Hard guard: if we got ZERO fresh prices something is badly wrong — skip write entirely.
    if success_count == 0:
        print(
            f"  WARN  0/{len(tickers)} tickers resolved — skipping write to avoid corrupting prices",
            file=sys.stderr,
        )
        return {"generated": datetime.utcnow().isoformat() + "Z", "prices": {}, "_write_skipped": True}

    print(f"  merged {success_count} fresh + {len(merged)-success_count} carried-over prices")
    return {"generated": datetime.utcnow().isoformat() + "Z", "prices": merged}


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


# ── ACTUAL S&P 500 MONTHLY RETURNS ───────────────────────────────────────
def build_inr_monthly(label_dates: list[str | None]) -> list[float] | None:
    """Fetch INR/USD closing rate for each label_date. Returns list of floats or None."""
    valid = [d for d in label_dates if d]
    if len(valid) < 2:
        return None
    try:
        from datetime import timedelta
        start = valid[0]
        end_dt = datetime.strptime(valid[-1], "%Y-%m-%d") + timedelta(days=7)
        hist = yf.Ticker("INR=X").history(
            start=start, end=end_dt.strftime("%Y-%m-%d"), interval="1d", auto_adjust=False)
        if hist.empty:
            print("  WARN  build_inr_monthly: INR=X history empty", file=sys.stderr)
            return None
        hist.index = hist.index.tz_localize(None) if hist.index.tzinfo else hist.index
        rates: list[float] = []
        for d in label_dates:
            target = datetime.strptime(d, "%Y-%m-%d") if d else None
            if target is None:
                rates.append(rates[-1] if rates else 84.0)
                continue
            subset = hist[hist.index <= target + timedelta(days=1)]
            rates.append(round(float(subset["Close"].iloc[-1]), 4) if not subset.empty
                         else (rates[-1] if rates else 84.0))
        print(f"  INR/USD monthly  {label_dates[0]} → {label_dates[-1]}"
              f"  start=₹{rates[0]}  end=₹{rates[-1]}")
        return rates
    except Exception as exc:
        print(f"  WARN  build_inr_monthly failed: {exc}", file=sys.stderr)
        return None


def build_snp_actual(label_dates: list[str | None]) -> list[float] | None:
    """
    Given ISO date strings matching portfolio monthly labels,
    fetch real ^GSPC closing prices and return cumulative % returns
    anchored to the first date (same as portfolio: 0% at start).

    Returns list of floats (same length as label_dates) or None on failure.
    """
    valid = [d for d in label_dates if d]
    if len(valid) < 2:
        print("  WARN  build_snp_actual: insufficient label_dates", file=sys.stderr)
        return None
    try:
        from datetime import timedelta
        start = valid[0]
        end_dt = datetime.strptime(valid[-1], "%Y-%m-%d") + timedelta(days=7)
        hist = yf.Ticker("^GSPC").history(
            start=start,
            end=end_dt.strftime("%Y-%m-%d"),
            interval="1d",
            auto_adjust=True,
        )
        if hist.empty:
            print("  WARN  build_snp_actual: ^GSPC history empty", file=sys.stderr)
            return None

        # For each label_date find closest available trading day close
        closes: list[float | None] = []
        hist.index = hist.index.tz_localize(None) if hist.index.tzinfo else hist.index
        for d in label_dates:
            if not d:
                closes.append(None)
                continue
            target = datetime.strptime(d, "%Y-%m-%d")
            # Find nearest row on or before target date
            subset = hist[hist.index <= target + timedelta(days=1)]
            if subset.empty:
                closes.append(None)
            else:
                closes.append(float(subset["Close"].iloc[-1]))

        # Compute cumulative return from first close
        base = closes[0]
        if not base:
            return None
        result = []
        for c in closes:
            if c is None:
                result.append(result[-1] if result else 0.0)
            else:
                result.append(round((c / base - 1) * 100, 2))
        print(f"  ^GSPC actual  {label_dates[0]} → {label_dates[-1]}  "
              f"cumulative: {result[-1]:+.2f}%")
        return result
    except Exception as exc:
        print(f"  WARN  build_snp_actual failed: {exc}", file=sys.stderr)
        return None


# ── FX BUY RATE ──────────────────────────────────────────────────────────
def fetch_fx_on_date(date_str: str) -> float | None:
    """Return INR/USD closing rate on a specific date (YYYY-MM-DD).
    Uses yfinance history for INR=X. Returns None on failure."""
    try:
        from datetime import timedelta
        dt  = datetime.strptime(date_str, "%Y-%m-%d")
        end = dt + timedelta(days=4)          # +4 days buffer for weekends/holidays
        hist = yf.Ticker("INR=X").history(
            start=dt.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            interval="1d",
            auto_adjust=False,
        )
        if hist.empty:
            return None
        return round(float(hist["Close"].iloc[0]), 4)
    except Exception as exc:
        print(f"  WARN  fx_on_date {date_str}: {exc}", file=sys.stderr)
        return None


def build_fx_buy_prices(existing_prices: dict[str, dict]) -> dict[str, float]:
    """
    Fetch historical INR/USD buy rates for all US open positions with buy_date.
    Returns dict {tk: fx_buy_rate}.

    Architecture: fx_buy lives in holdings_prices.json (network-first, always
    fresh) NOT holdings_cost.json (cache-first, stale in browser cache).
    Carries forward previously-fetched rates — idempotent, no re-fetch.
    """
    if not COST_BASIS.exists():
        return {}
    try:
        cost = json.loads(COST_BASIS.read_text())
    except Exception:
        return {}

    fx_buys: dict[str, float] = {}
    for tk, p in existing_prices.items():
        if p.get("fx_buy"):
            fx_buys[tk] = p["fx_buy"]

    for pos in cost.get("us", {}).get("open", []):
        tk       = pos.get("tk")
        buy_date = pos.get("buy_date")
        if not tk or not buy_date:
            continue
        if tk in fx_buys:
            continue
        fx = fetch_fx_on_date(buy_date)
        if fx:
            fx_buys[tk] = fx
            print(f"  fx_buy  {tk:8s}  {buy_date}  → ₹{fx}/USD")
        else:
            print(f"  WARN  fx_buy not found for {tk} on {buy_date}", file=sys.stderr)

    return fx_buys


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
        # fx_buy per ticker — stored here (network-first) not in holdings_cost.json (cache-first)
        print("Fetching fx_buy rates for open positions…")
        fx_buys = build_fx_buy_prices(holdings.get("prices", {}))
        for tk, rate in fx_buys.items():
            if tk in holdings["prices"]:
                holdings["prices"][tk]["fx_buy"] = rate
        # Real S&P 500 cumulative returns — replaces broker's benchmark in the chart
        try:
            cost_now = json.loads(COST_BASIS.read_text()) if COST_BASIS.exists() else {}
            label_dates = cost_now.get("us", {}).get("monthly", {}).get("label_dates", [])
            if label_dates:
                print("Fetching actual ^GSPC returns…")
                snp_actual = build_snp_actual(label_dates)
                if snp_actual:
                    holdings["snp_actual_cum_pct"] = snp_actual
                print("Fetching monthly INR/USD rates…")
                inr_monthly = build_inr_monthly(label_dates)
                if inr_monthly:
                    holdings["inr_fx_monthly"] = inr_monthly
        except Exception as exc:
            print(f"  WARN  snp_actual fetch failed: {exc}", file=sys.stderr)
        OUT_HOLDINGS.write_text(json.dumps(holdings, indent=2))
        print(f"  wrote {OUT_HOLDINGS.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
