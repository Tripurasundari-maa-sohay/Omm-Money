"""
india_prices_vm.py
==================
Runs on Oracle VM (IP: 145.241.158.254) via cron every 5 min during India market hours.
Fetches real-time NSE prices from Angel One SmartAPI (requires fixed IP whitelist).
Commits result to GitHub repo via API.

Cron entry on Oracle VM (add via: crontab -e):
  */5 3-10 * * 1-5 source /home/opc/angel_env.sh && python3 /home/opc/india_prices_vm.py >> /home/opc/india_prices.log 2>&1
"""

import json, os, sys, time, base64, math, requests
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────
ANGEL_API_KEY  = os.environ.get("ANGEL_API_KEY", "")
ANGEL_CLIENT   = os.environ.get("ANGEL_CLIENT_ID", "")
ANGEL_MPIN     = os.environ.get("ANGEL_MPIN", "")
ANGEL_TOTP_SEC = os.environ.get("ANGEL_TOTP_SECRET", "")
GITHUB_TOKEN   = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO    = "Tripurasundari-maa-sohay/Omm-Money"
PRICES_PATH    = "portfolio/data/processed/india_prices.json"

# ── Angel One NSE token map (from OpenAPIScripMaster.json) ────────────────
# Format: "PORTFOLIO_TICKER": "NSE_TOKEN"
ANGEL_TOKEN_MAP = {
    "SBIN":        "3045",
    "RELIANCE":    "2885",
    "DIACABS":     "18543",
    "GMBREW":      "1168",
    "NBCC":        "31415",
    "JYOTISTRUC":  "1802",
    "GOLDBEES_M":  "14428",  # Nippon Gold BeES
    "GOLDBEES_U":  "14428",  # same underlying ETF
    "WAAREEENER":  "25907",
    # Not in Angel One master: AVADHSUGAR, IRBINVIT, FILATFASH, ASHALOG, PARAMATRIX
    # → Yahoo Finance fallback used for these
}

# ── Yahoo fallback symbols ─────────────────────────────────────────────────
YAHOO_FALLBACK = {
    "AVADHSUGAR":  "AVADHSUGAR.NS",
    "IRBINVIT":    "IRBINVIT.NS",
    "FILATFASH":   "FILATFASH.NS",
    "ASHALOG":     "ASHALOG.NS",
    "PARAMATRIX":  "PARAMATRIX.NS",
}

# ── Angel One Session ──────────────────────────────────────────────────────
_jwt_cache = {"token": None, "expires": 0}

def angel_login():
    if _jwt_cache["token"] and time.time() < _jwt_cache["expires"]:
        return _jwt_cache["token"]
    try:
        import pyotp
        totp = pyotp.TOTP(ANGEL_TOTP_SEC).now()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": "127.0.0.1",
            "X-ClientPublicIP": "127.0.0.1",
            "X-MACAddress": "00:00:00:00:00:00",
            "X-PrivateKey": ANGEL_API_KEY,
        }
        r = requests.post(
            "https://apiconnect.angelbroking.com/rest/auth/angelbroking/user/v1/loginByPassword",
            json={"clientcode": ANGEL_CLIENT, "password": ANGEL_MPIN, "totp": totp},
            headers=headers, timeout=15
        )
        if r.status_code == 200:
            jwt = r.json().get("data", {}).get("jwtToken")
            if jwt:
                _jwt_cache["token"] = jwt
                _jwt_cache["expires"] = time.time() + 3600
                print(f"  Angel One login OK")
                return jwt
        print(f"  Angel One login failed: {r.status_code}", file=sys.stderr)
    except Exception as e:
        print(f"  Angel One login error: {e}", file=sys.stderr)
    return None

def fetch_angel_batch(token_map: dict) -> dict:
    """
    Batch fetch all available tokens in one Angel One API call.
    Returns {ticker: {ltp, pc, source, as_of}}
    """
    jwt = angel_login()
    if not jwt:
        return {}
    # Deduplicate tokens (GOLDBEES_M and _U share same token)
    token_to_tickers = {}
    for tk, token in token_map.items():
        token_to_tickers.setdefault(token, []).append(tk)
    unique_tokens = list(token_to_tickers.keys())

    headers = {
        "Authorization": f"Bearer {jwt}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-UserType": "USER",
        "X-SourceID": "WEB",
        "X-PrivateKey": ANGEL_API_KEY,
    }
    # Angel One allows max 50 tokens per batch
    results = {}
    for i in range(0, len(unique_tokens), 50):
        batch = unique_tokens[i:i+50]
        try:
            r = requests.post(
                "https://apiconnect.angelbroking.com/rest/secure/angelbroking/market/v1/quote/",
                json={"mode": "FULL", "exchangeTokens": {"NSE": batch}},
                headers=headers, timeout=15
            )
            if r.status_code == 200:
                fetched = r.json().get("data", {}).get("fetched", [])
                for d in fetched:
                    token = str(d.get("symbolToken", ""))
                    ltp = d.get("ltp")
                    pc  = d.get("close")
                    if ltp and float(ltp) > 0:
                        entry = {
                            "ltp":    round(float(ltp), 4),
                            "pc":     round(float(pc), 4) if pc and float(pc) > 0 else None,
                            "source": "angelone",
                            "as_of":  datetime.now(timezone.utc).isoformat() + "Z"
                        }
                        for tk in token_to_tickers.get(token, []):
                            results[tk] = entry
                            print(f"  {tk:15s} → {ltp:.2f}  pc={pc}")
            else:
                print(f"  Angel One batch failed: {r.status_code}", file=sys.stderr)
        except Exception as e:
            print(f"  Angel One batch error: {e}", file=sys.stderr)
    return results

def fetch_yahoo_fallback(fallback_map: dict) -> dict:
    """Fetch prices for tickers not available on Angel One via Yahoo Finance."""
    results = {}
    try:
        import yfinance as yf
        for tk, yf_sym in fallback_map.items():
            try:
                hist = yf.Ticker(yf_sym).history(period="2d", interval="1d", auto_adjust=False)
                if not hist.empty:
                    ltp = round(float(hist["Close"].iloc[-1]), 4)
                    pc  = round(float(hist["Close"].iloc[-2]), 4) if len(hist) >= 2 else None
                    results[tk] = {
                        "ltp":    ltp,
                        "pc":     pc,
                        "source": "yahoo",
                        "as_of":  datetime.now(timezone.utc).isoformat() + "Z"
                    }
                    print(f"  {tk:15s} → {ltp:.2f}  (yahoo fallback)")
            except Exception as e:
                print(f"  Yahoo {tk}: {e}", file=sys.stderr)
    except ImportError:
        print("  yfinance not installed — skipping Yahoo fallback", file=sys.stderr)
    return results

def commit_to_github(prices_dict: dict):
    if not GITHUB_TOKEN:
        print("  GITHUB_TOKEN not set", file=sys.stderr)
        return False
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    payload = {
        "generated": datetime.now(timezone.utc).isoformat() + "Z",
        "prices": prices_dict
    }
    content = base64.b64encode(json.dumps(payload, indent=2).encode()).decode()
    r = requests.get(
        f"https://api.github.com/repos/{GITHUB_REPO}/contents/{PRICES_PATH}",
        headers=headers, timeout=10
    )
    sha = r.json().get("sha") if r.status_code == 200 else None
    body = {
        "message": f"data: India prices (Angel One) {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} [skip ci]",
        "content": content,
        "branch": "main",
    }
    if sha:
        body["sha"] = sha
    r = requests.put(
        f"https://api.github.com/repos/{GITHUB_REPO}/contents/{PRICES_PATH}",
        headers=headers, json=body, timeout=15
    )
    if r.status_code in (200, 201):
        print(f"  Committed to GitHub OK ({len(prices_dict)} prices)")
        return True
    print(f"  GitHub commit failed: {r.status_code}", file=sys.stderr)
    return False

def main():
    print(f"\n{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} India prices fetch")

    # 1. Angel One batch fetch for known tokens
    angel_results = fetch_angel_batch(ANGEL_TOKEN_MAP)

    # 2. Yahoo fallback for stocks not in Angel One
    yahoo_results = fetch_yahoo_fallback(YAHOO_FALLBACK)

    all_results = {**angel_results, **yahoo_results}

    print(f"\n  Summary: {len(angel_results)} Angel One + {len(yahoo_results)} Yahoo = {len(all_results)} total")

    if all_results:
        commit_to_github(all_results)
    else:
        print("  No prices fetched", file=sys.stderr)

if __name__ == "__main__":
    main()
