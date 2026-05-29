"""
fetch_all_prices_vm.py
======================
Single script running on Oracle VM (145.241.158.254) via cron.
Replaces GitHub Actions price fetch entirely.
Writes BOTH holdings_prices.json (LTP/pc) and market_indices.json
(S&P/Nasdaq/Nifty/Sensex + fx_rate + market status). The market status
field drives index.html's US DAY P&L tile (RESET → tile blanks "--").

Cron entries (add via: crontab -e):
  # India market hours: 03:45–10:00 UTC (09:15–15:30 IST)
  */5 3-10 * * 1-5 source /home/opc/angel_env.sh && python3 /home/opc/fetch_all_prices_vm.py india >> /home/opc/prices.log 2>&1

  # US market hours: 13:30–20:00 UTC (09:30–16:00 ET)
  */5 13-20 * * 1-5 source /home/opc/angel_env.sh && python3 /home/opc/fetch_all_prices_vm.py us >> /home/opc/prices.log 2>&1

Usage:
  python3 fetch_all_prices_vm.py india   # fetch India prices only
  python3 fetch_all_prices_vm.py us      # fetch US prices only
  python3 fetch_all_prices_vm.py all     # fetch both
"""

import json, os, sys, time, base64, math, requests
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────
ANGEL_API_KEY  = os.environ.get("ANGEL_API_KEY", "")
ANGEL_CLIENT   = os.environ.get("ANGEL_CLIENT_ID", "")
ANGEL_MPIN     = os.environ.get("ANGEL_MPIN", "")
ANGEL_TOTP_SEC = os.environ.get("ANGEL_TOTP_SECRET", "")
FINNHUB_KEY    = os.environ.get("FINNHUB_API_KEY", "")
GITHUB_TOKEN   = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO    = "Tripurasundari-maa-sohay/Omm-Money"
PRICES_PATH    = "portfolio/data/processed/holdings_prices.json"
INDICES_PATH   = "portfolio/data/processed/market_indices.json"

MODE = sys.argv[1] if len(sys.argv) > 1 else "all"

# ── Angel One NSE token map ────────────────────────────────────────────────
ANGEL_TOKEN_MAP = {
    "SBIN":       "3045",
    "RELIANCE":   "2885",
    "DIACABS":    "18543",
    "GMBREW":     "1168",
    "NBCC":       "31415",
    "JYOTISTRUC": "1802",
    "GOLDBEES_M": "14428",
    "GOLDBEES_U": "14428",
    "WAAREEENER": "25907",
}

YAHOO_INDIA_FALLBACK = {
    "AVADHSUGAR": "AVADHSUGAR.NS",
    "IRBINVIT":   "IRBINVIT.NS",
    "FILATFASH":  "FILATFASH.NS",
    "ASHALOG":    "ASHALOG.NS",
    "PARAMATRIX": "PARAMATRIX.NS",
}

US_HOLDINGS = [
    "GOOG","AMZN","AVGO","GLW","GEV","MU","MSFT","MP","RKLB","TTE","EWY","HUMN","VOOG",
    "SHIP","NOW"
]

# ── Angel One ─────────────────────────────────────────────────────────────
_jwt = {"token": None, "expires": 0}

def angel_login():
    if _jwt["token"] and time.time() < _jwt["expires"]:
        return _jwt["token"]
    try:
        import pyotp
        headers = {
            "Content-Type": "application/json", "Accept": "application/json",
            "X-UserType": "USER", "X-SourceID": "WEB",
            "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1",
            "X-MACAddress": "00:00:00:00:00:00", "X-PrivateKey": ANGEL_API_KEY,
        }
        r = requests.post(
            "https://apiconnect.angelbroking.com/rest/auth/angelbroking/user/v1/loginByPassword",
            json={"clientcode": ANGEL_CLIENT, "password": ANGEL_MPIN,
                  "totp": pyotp.TOTP(ANGEL_TOTP_SEC).now()},
            headers=headers, timeout=15
        )
        if r.status_code == 200:
            jwt = r.json().get("data", {}).get("jwtToken")
            if jwt:
                _jwt["token"] = jwt
                _jwt["expires"] = time.time() + 3600
                return jwt
        print(f"  Angel One login failed: {r.status_code}", file=sys.stderr)
    except Exception as e:
        print(f"  Angel One login error: {e}", file=sys.stderr)
    return None

def fetch_india_angel():
    jwt = angel_login()
    if not jwt:
        return {}
    token_to_tickers = {}
    for tk, token in ANGEL_TOKEN_MAP.items():
        token_to_tickers.setdefault(token, []).append(tk)
    headers = {
        "Authorization": f"Bearer {jwt}", "Content-Type": "application/json",
        "Accept": "application/json", "X-UserType": "USER",
        "X-SourceID": "WEB", "X-PrivateKey": ANGEL_API_KEY,
    }
    results = {}
    batch = list(token_to_tickers.keys())
    try:
        r = requests.post(
            "https://apiconnect.angelbroking.com/rest/secure/angelbroking/market/v1/quote/",
            json={"mode": "FULL", "exchangeTokens": {"NSE": batch}},
            headers=headers, timeout=15
        )
        if r.status_code == 200:
            for d in r.json().get("data", {}).get("fetched", []):
                token = str(d.get("symbolToken", ""))
                ltp, pc = d.get("ltp"), d.get("close")
                if ltp and float(ltp) > 0:
                    entry = {
                        "ltp": round(float(ltp), 4),
                        "pc":  round(float(pc), 4) if pc and float(pc) > 0 else None,
                        "source": "angelone",
                        "as_of":  datetime.now(timezone.utc).isoformat() + "Z"
                    }
                    for tk in token_to_tickers.get(token, []):
                        results[tk] = entry
                        print(f"  {tk:15s} → {ltp:.2f}  pc={pc}  [Angel One]")
    except Exception as e:
        print(f"  Angel One batch error: {e}", file=sys.stderr)
    return results

def fetch_india_yahoo_fallback():
    results = {}
    try:
        import yfinance as yf
        for tk, yf_sym in YAHOO_INDIA_FALLBACK.items():
            try:
                hist = yf.Ticker(yf_sym).history(period="2d", interval="1d", auto_adjust=False)
                if not hist.empty:
                    ltp = round(float(hist["Close"].iloc[-1]), 4)
                    pc  = round(float(hist["Close"].iloc[-2]), 4) if len(hist) >= 2 else None
                    results[tk] = {"ltp": ltp, "pc": pc, "source": "yahoo",
                                   "as_of": datetime.now(timezone.utc).isoformat() + "Z"}
                    print(f"  {tk:15s} → {ltp:.2f}  [Yahoo fallback]")
            except Exception as e:
                print(f"  Yahoo {tk}: {e}", file=sys.stderr)
    except ImportError:
        pass
    return results

def fetch_us_finnhub():
    if not FINNHUB_KEY:
        return {}
    delay = max(1, math.floor(60.0 / len(US_HOLDINGS)))
    results = {}
    for tk in US_HOLDINGS:
        try:
            r = requests.get(
                f"https://finnhub.io/api/v1/quote?symbol={tk}&token={FINNHUB_KEY}",
                headers={"Accept": "application/json"}, timeout=10
            )
            if r.status_code == 200:
                d = r.json()
                ltp, pc = d.get("c"), d.get("pc")
                if ltp and float(ltp) > 0:
                    results[tk] = {
                        "ltp": round(float(ltp), 4),
                        "pc":  round(float(pc), 4) if pc and float(pc) > 0 else None,
                        "source": "finnhub",
                        "as_of":  datetime.now(timezone.utc).isoformat() + "Z"
                    }
                    chg_pct = ((ltp - pc) / pc * 100) if pc else 0
                    print(f"  {tk:8s} → {ltp:.2f}  {chg_pct:+.2f}%  [Finnhub]")
        except Exception as e:
            print(f"  Finnhub {tk}: {e}", file=sys.stderr)
        time.sleep(delay)
    return results

# ── Market status (port of market_data.py:market_status) ───────────────────
def _now_in(tz_name: str):
    """Return datetime in given tz. Uses zoneinfo; falls back to fixed UTC
    offsets (no DST) if tzdata unavailable on the VM."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tz_name))
    except Exception:
        # Fallback fixed offsets — ET=-4 (EDT), IST=+5:30. DST-naive.
        from datetime import timedelta
        offset = {"America/New_York": -4, "Asia/Kolkata": 5.5}.get(tz_name, 0)
        return datetime.now(timezone.utc) + timedelta(hours=offset)

def market_status(market: str) -> str:
    """OPEN / CLOSED / PREMARKET / POSTMARKET / RESET based on local-market clock.
    RESET = 7:30–9:30 AM ET (dashboard blanks daily P&L)."""
    if market == "usa":
        now  = _now_in("America/New_York")
        wd   = now.weekday()
        mins = now.hour * 60 + now.minute
        if wd >= 5:
            return "CLOSED"
        if 9 * 60 + 30 <= mins < 16 * 60:
            return "OPEN"
        if 16 * 60 <= mins < 20 * 60:
            return "POSTMARKET"
        if 7 * 60 + 30 <= mins < 9 * 60 + 30:
            return "RESET"
        if 4 * 60 <= mins < 7 * 60 + 30:
            return "PREMARKET"
        return "CLOSED"
    if market == "india":
        now  = _now_in("Asia/Kolkata")
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

# ── Index + FX quotes via Yahoo chart endpoint (no yfinance dependency) ─────
_YF_UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
INDEX_MAP = {
    "usa":   {"snp_500": "^GSPC", "nasdaq": "^IXIC"},
    "india": {"nifty_50": "^NSEI", "sensex": "^BSESN"},
}

def fetch_yahoo_meta(yf_sym: str):
    """Return {'ltp', 'pc'} from Yahoo chart meta, or None."""
    try:
        for host in ("query1", "query2"):
            r = requests.get(
                f"https://{host}.finance.yahoo.com/v8/finance/chart/{yf_sym}",
                headers=_YF_UA, timeout=10
            )
            if r.status_code != 200:
                continue
            m = r.json()["chart"]["result"][0]["meta"]
            ltp = m.get("regularMarketPrice")
            pc  = m.get("chartPreviousClose") or m.get("previousClose")
            if ltp:
                return {"ltp": float(ltp), "pc": float(pc) if pc else None}
    except Exception as e:
        print(f"  Yahoo meta {yf_sym}: {e}", file=sys.stderr)
    return None

def build_indices_payload(existing: dict) -> dict:
    """Build market_indices.json payload. Refreshes the markets relevant to
    MODE; preserves the other market block + keys from existing file."""
    out = dict(existing) if isinstance(existing, dict) else {}
    out["generated"] = datetime.now(timezone.utc).isoformat() + "Z"

    markets = []
    if MODE in ("us", "all"):    markets.append("usa")
    if MODE in ("india", "all"): markets.append("india")

    for market in markets:
        idx_block = {}
        for key, yf_sym in INDEX_MAP[market].items():
            q = fetch_yahoo_meta(yf_sym)
            if q is None or q["pc"] is None:
                idx_block[key] = None
                continue
            change = q["ltp"] - q["pc"]
            idx_block[key] = {
                "current":    round(q["ltp"], 4),
                "prev_close": round(q["pc"], 4),
                "change":     round(change, 2),
                "change_pct": round(change / q["pc"] * 100, 2),
            }
            print(f"  {yf_sym:8s} → {q['ltp']:>12,.2f}  [index]")
        out[market + "_market"] = {
            "status":    market_status(market),
            "indices":   idx_block,
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
        }

    # FX (INR per USD) — keep within sanity bounds 70..120
    fx = fetch_yahoo_meta("INR=X")
    if fx and 70 <= fx["ltp"] <= 120:
        out["fx_rate"] = round(fx["ltp"], 4)
        print(f"  INR=X    → {fx['ltp']:.4f}  [fx]")
    elif "fx_rate" not in out:
        out["fx_rate"] = None

    return out

def commit_json_to_github(path: str, payload: dict, label: str) -> bool:
    """Commit any JSON payload to a repo path via Contents API (3x retry)."""
    if not GITHUB_TOKEN:
        print("  GITHUB_TOKEN not set", file=sys.stderr)
        return False
    content = base64.b64encode(json.dumps(payload, indent=2).encode()).decode()
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    r = requests.get(
        f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}",
        headers=headers, timeout=10
    )
    sha = r.json().get("sha") if r.status_code == 200 else None
    body = {
        "message": f"data: {label} ({MODE}) {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} [skip ci]",
        "content": content, "branch": "main",
    }
    if sha:
        body["sha"] = sha
    for attempt in range(1, 4):
        try:
            r = requests.put(
                f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}",
                headers=headers, json=body, timeout=20
            )
            if r.status_code in (200, 201):
                print(f"  Committed {label} → GitHub OK")
                return True
            elif r.status_code == 409:
                print(f"  Attempt {attempt}: SHA conflict, re-fetching...", file=sys.stderr)
                r2 = requests.get(
                    f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}",
                    headers=headers, timeout=10
                )
                body["sha"] = r2.json().get("sha")
            else:
                print(f"  Attempt {attempt}: {r.status_code}", file=sys.stderr)
        except Exception as e:
            print(f"  Attempt {attempt} error: {e}", file=sys.stderr)
        time.sleep(5 * attempt)
    print(f"  All 3 {label} commit attempts failed", file=sys.stderr)
    return False

def get_current_indices_from_github() -> dict:
    """Load existing market_indices.json to preserve the non-refreshed market."""
    try:
        r = requests.get(
            f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{INDICES_PATH}",
            timeout=10
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {}

def get_current_prices_from_github():
    """Load existing holdings_prices.json from GitHub to preserve non-price fields."""
    try:
        r = requests.get(
            f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{PRICES_PATH}",
            timeout=10
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {"prices": {}, "generated": ""}

def commit_prices_to_github(new_prices: dict, existing_data: dict):
    """
    Merge new_prices into existing holdings_prices.json prices dict.
    Preserves chart data (weekly_chart etc.) — only updates prices + generated.
    Retries 3x with backoff on failure.
    """
    if not GITHUB_TOKEN:
        print("  GITHUB_TOKEN not set", file=sys.stderr)
        return False

    # Merge: overlay new prices onto existing
    merged_prices = {**existing_data.get("prices", {}), **new_prices}

    payload = {**existing_data, "prices": merged_prices,
               "generated": datetime.now(timezone.utc).isoformat() + "Z"}

    content = base64.b64encode(json.dumps(payload, indent=2).encode()).decode()
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    # Get SHA
    r = requests.get(
        f"https://api.github.com/repos/{GITHUB_REPO}/contents/{PRICES_PATH}",
        headers=headers, timeout=10
    )
    sha = r.json().get("sha") if r.status_code == 200 else None

    body = {
        "message": f"data: prices ({MODE}) {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} [skip ci]",
        "content": content, "branch": "main",
    }
    if sha:
        body["sha"] = sha

    for attempt in range(1, 4):
        try:
            r = requests.put(
                f"https://api.github.com/repos/{GITHUB_REPO}/contents/{PRICES_PATH}",
                headers=headers, json=body, timeout=20
            )
            if r.status_code in (200, 201):
                print(f"  Committed {len(new_prices)} prices → GitHub OK")
                return True
            elif r.status_code == 409:  # conflict — re-fetch SHA and retry
                print(f"  Attempt {attempt}: SHA conflict, re-fetching...", file=sys.stderr)
                r2 = requests.get(
                    f"https://api.github.com/repos/{GITHUB_REPO}/contents/{PRICES_PATH}",
                    headers=headers, timeout=10
                )
                body["sha"] = r2.json().get("sha")
            else:
                print(f"  Attempt {attempt}: {r.status_code}", file=sys.stderr)
        except Exception as e:
            print(f"  Attempt {attempt} error: {e}", file=sys.stderr)
        time.sleep(5 * attempt)  # backoff: 5s, 10s, 15s

    print("  All 3 commit attempts failed", file=sys.stderr)
    return False

def main():
    print(f"\n{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} fetch_all_prices [{MODE}]")

    existing = get_current_prices_from_github()
    new_prices = {}

    if MODE in ("india", "all"):
        print("\n── India (Angel One + Yahoo fallback)")
        angel_p = fetch_india_angel()
        yahoo_p = fetch_india_yahoo_fallback()
        india_p = {**yahoo_p, **angel_p}  # Angel One wins over Yahoo for same ticker
        new_prices.update(india_p)
        print(f"  India: {len(angel_p)} Angel One + {len(yahoo_p)} Yahoo = {len(india_p)} total")

    if MODE in ("us", "all"):
        print("\n── US (Finnhub)")
        us_p = fetch_us_finnhub()
        new_prices.update(us_p)
        print(f"  US: {len(us_p)} Finnhub")

    if new_prices:
        commit_prices_to_github(new_prices, existing)
    else:
        print("  No prices fetched", file=sys.stderr)

    # ── Market indices + FX + status (unblanks US DAY P&L tile) ──────────────
    print("\n── Market indices + FX")
    existing_idx = get_current_indices_from_github()
    idx_payload  = build_indices_payload(existing_idx)
    commit_json_to_github(INDICES_PATH, idx_payload, "indices")

if __name__ == "__main__":
    main()
