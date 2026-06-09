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
COST_PATH      = "portfolio/data/holdings_cost.json"
# Push alerts on pipeline failure (commit 401/expired token, login fail, etc).
# Set NTFY_TOPIC in angel_env.sh and subscribe to that topic in the ntfy app
# (https://ntfy.sh) on your phone. Empty => alerts disabled.
NTFY_TOPIC     = os.environ.get("NTFY_TOPIC", "")
_ALERT_COOLDOWN_FILE = "/tmp/ommoney_last_alert"
_ALERT_COOLDOWN_SEC  = 1800   # max one alert per 30 min (cron runs every minute)

MODE = sys.argv[1] if len(sys.argv) > 1 else "all"

# ── Angel One NSE token map ────────────────────────────────────────────────
ANGEL_TOKEN_MAP = {
    "SBIN":       "3045",
    "RELIANCE":   "2885",
    "DIACABS":    "18543",
    "NBCC":       "31415",
    "JYOTISTRUC": "1802",
    "GOLDBEES_M": "14428",
    "GOLDBEES_U": "14428",
    "WAAREEENER": "25907",
    "ASHALOG":    "24711",   # ASHALOG-SM (NSE SME) — delisted on Yahoo, Angel One only
    "PARAMATRIX": "25069",   # PARAMATRIX-SM (NSE SME) — delisted on Yahoo, Angel One only
    "IRBINVIT":   "20817",   # IRBINVIT-IV (NSE InvIT) — was flaky on Yahoo fallback
    "FILATFASH":  "23651",   # FILATFASH-BE (NSE) — was flaky on Yahoo fallback
}

# Held on BSE in broker — fetch BSE price (NSE/BSE diverge for illiquid names,
# e.g. AVADHSUGAR NSE 447 vs BSE 437). Token from BSE segment of scrip master.
ANGEL_BSE_TOKEN_MAP = {
    "AVADHSUGAR": "540649",   # broker holds on BSE (438) — NSE diverges (447)
    "GMBREW":     "507488",   # broker holds on BSE (928) — NSE (932)
}

YAHOO_INDIA_FALLBACK = {}

US_HOLDINGS = [
    "GOOG","AMZN","AVGO","GLW","GEV","MU","MSFT","MP","RKLB","TTE","EWY","HUMN","VOOG",
    "SHIP","NOW","ORCL",
    "INTC",
    "JDZG",
    # Watchlist tickers (data/watchlist.json) — not held, monitored for buy zones
    "NVDA","AMD","META","TSM","QCOM","CRCL"
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
    # Map (exchange, token) -> [tickers]; query NSE + BSE in one call.
    # Tokens can collide across exchanges, so key by (exch, token).
    key_to_tickers = {}
    exch_tokens = {"NSE": [], "BSE": []}
    for tk, token in ANGEL_TOKEN_MAP.items():
        key_to_tickers.setdefault(("NSE", token), []).append(tk)
        exch_tokens["NSE"].append(token)
    for tk, token in ANGEL_BSE_TOKEN_MAP.items():
        key_to_tickers.setdefault(("BSE", token), []).append(tk)
        exch_tokens["BSE"].append(token)
    exch_tokens = {e: t for e, t in exch_tokens.items() if t}
    headers = {
        "Authorization": f"Bearer {jwt}", "Content-Type": "application/json",
        "Accept": "application/json", "X-UserType": "USER",
        "X-SourceID": "WEB", "X-PrivateKey": ANGEL_API_KEY,
    }
    results = {}
    try:
        r = requests.post(
            "https://apiconnect.angelbroking.com/rest/secure/angelbroking/market/v1/quote/",
            json={"mode": "FULL", "exchangeTokens": exch_tokens},
            headers=headers, timeout=15
        )
        if r.status_code == 200:
            for d in r.json().get("data", {}).get("fetched", []):
                token = str(d.get("symbolToken", ""))
                exch  = d.get("exchange", "NSE")
                ltp, pc = d.get("ltp"), d.get("close")
                vol = d.get("tradeVolume")
                if ltp and float(ltp) > 0:
                    pc_val = round(float(pc), 4) if pc and float(pc) > 0 else None
                    # Volume heal: illiquid SME with 0 trades today has a stale
                    # `close` (older session) → broker treats prev-close = last
                    # price (0% day). Force pc = ltp so Day P&L = 0, matching broker.
                    healed = False
                    try:
                        if vol is not None and int(vol) == 0:
                            pc_val = round(float(ltp), 4)
                            healed = True
                    except (TypeError, ValueError):
                        pass
                    entry = {
                        "ltp": round(float(ltp), 4),
                        "pc":  pc_val,
                        "source": "angelone",
                        "as_of":  datetime.now(timezone.utc).isoformat() + "Z"
                    }
                    for tk in key_to_tickers.get((exch, token), []):
                        results[tk] = entry
                        tag = "  [vol=0 heal: pc=ltp]" if healed else ""
                        print(f"  {tk:15s} → {ltp:.2f}  pc={pc_val}  [{exch} Angel One]{tag}")
    except Exception as e:
        print(f"  Angel One batch error: {e}", file=sys.stderr)
    return results

def fetch_india_yahoo_fallback():
    """Yahoo fallback for India tickers Angel One can't serve.
    Uses the chart-endpoint meta (regularMarketPrice + chartPreviousClose) —
    reliable intraday ltp AND pc every run. The old yfinance .history(period=2d)
    path was flaky intraday: it left pc=None (so the dashboard dropped the stock
    from Day P&L) and silently failed for some tickers (IRBINVIT froze hours-old)."""
    results = {}
    for tk, yf_sym in YAHOO_INDIA_FALLBACK.items():
        q = fetch_yahoo_meta(yf_sym)
        if q is None or not q.get("ltp"):
            continue
        results[tk] = {
            "ltp": round(float(q["ltp"]), 4),
            "pc":  round(float(q["pc"]), 4) if q.get("pc") else None,
            "source": "yahoo",
            "as_of":  datetime.now(timezone.utc).isoformat() + "Z",
        }
        print(f"  {tk:15s} → {q['ltp']:.2f}  pc={q.get('pc')}  [Yahoo fallback]")
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
    """Load existing market_indices.json to preserve the non-refreshed market.
    Uses the authenticated Contents API (NOT raw.githubusercontent, which is
    CDN-cached ~5min and would serve a stale block — clobbering the other
    market's fresh data on read-modify-write)."""
    if GITHUB_TOKEN:
        try:
            r = requests.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/contents/{INDICES_PATH}",
                headers={"Authorization": f"token {GITHUB_TOKEN}",
                         "Accept": "application/vnd.github.v3+json"},
                params={"ref": "main"}, timeout=10
            )
            if r.status_code == 200:
                return json.loads(base64.b64decode(r.json()["content"]).decode())
        except Exception as e:
            print(f"  indices read (API) error: {e}", file=sys.stderr)
    # Fallback: raw CDN (may be stale)
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

def get_fx_buy_map() -> dict:
    """Read holdings_cost.json and return {tk: {fx_buy, buy_date}} for all US open
    positions. Used to stamp fx_buy into price entries so the INR-return tile works."""
    if not GITHUB_TOKEN:
        return {}
    try:
        r = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/{COST_PATH}?ref=main",
            headers={"Authorization": f"token {GITHUB_TOKEN}",
                     "Accept": "application/vnd.github.v3+json"},
            timeout=10
        )
        if r.status_code == 200:
            cost = json.loads(base64.b64decode(r.json()["content"]).decode())
            result = {}
            for h in cost.get("us", {}).get("open", []):
                tk = h.get("tk")
                if tk and (h.get("fx_buy") or h.get("buy_date")):
                    result[tk] = {"fx_buy": h.get("fx_buy"), "buy_date": h.get("buy_date")}
            print(f"  fx_buy map loaded: {len(result)} tickers")
            return result
    except Exception as e:
        print(f"  fx_buy map load error: {e}", file=sys.stderr)
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
        # Stamp fx_buy + buy_date from holdings_cost.json into each US price entry.
        # market_data.py does this in full runs; VM must replicate or INR-return tile
        # shows "—" / "Run parser to activate" (fx_buy=None in holdings_prices.json).
        fx_buy_map = get_fx_buy_map()
        for tk, entry in us_p.items():
            if tk in fx_buy_map:
                entry["fx_buy"]   = fx_buy_map[tk].get("fx_buy")
                entry["buy_date"] = fx_buy_map[tk].get("buy_date")
        new_prices.update(us_p)
        print(f"  US: {len(us_p)} Finnhub  (fx_buy stamped: {sum(1 for t in us_p if t in fx_buy_map)})")

    prices_ok = True
    if new_prices:
        prices_ok = commit_prices_to_github(new_prices, existing)
    else:
        print("  No prices fetched", file=sys.stderr)

    # ── Market indices + FX + status (unblanks US DAY P&L tile) ──────────────
    print("\n── Market indices + FX")
    existing_idx = get_current_indices_from_github()
    idx_payload  = build_indices_payload(existing_idx)
    idx_ok = commit_json_to_github(INDICES_PATH, idx_payload, "indices")

    # ── Failure alert (catches silent freezes e.g. expired GITHUB_TOKEN) ─────
    fails = []
    if not GITHUB_TOKEN:
        fails.append("GITHUB_TOKEN missing")
    if not new_prices:
        fails.append(f"no prices fetched ({MODE}) — Angel/Finnhub down?")
    elif not prices_ok:
        fails.append(f"prices commit FAILED ({MODE}) — token expired / GitHub down?")
    if not idx_ok:
        fails.append(f"indices commit FAILED ({MODE})")
    if fails:
        alert("Omm-Money pipeline: " + "; ".join(fails))

def alert(msg: str):
    """Push a failure alert to ntfy.sh, with a 30-min cooldown to avoid the
    every-minute cron spamming. No-op if NTFY_TOPIC unset."""
    print(f"  ALERT: {msg}", file=sys.stderr)
    if not NTFY_TOPIC:
        return
    try:
        if os.path.exists(_ALERT_COOLDOWN_FILE):
            if time.time() - os.path.getmtime(_ALERT_COOLDOWN_FILE) < _ALERT_COOLDOWN_SEC:
                return
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}",
                      data=msg.encode("utf-8"),
                      headers={"Title": "Omm-Money pipeline FAIL", "Priority": "high",
                               "Tags": "warning"}, timeout=10)
        open(_ALERT_COOLDOWN_FILE, "w").write(str(time.time()))
    except Exception as e:
        print(f"  alert post error: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()
