"""
kronos_predict.py — Oracle VM cron, runs daily at 22:00 UTC after market close.
Reads EOD history CSVs, runs Kronos-small, predicts next 5 trading days close.
Commits kronos_signals.json to GitHub.

Cron: 0 22 * * 1-5 source /home/opc/angel_env.sh && python3.11 /home/opc/kronos_predict.py >> /home/opc/kronos.log 2>&1
"""
import os, sys, json, base64, time, requests
from datetime import datetime, timezone, timedelta

sys.path.insert(0, '/home/opc/Kronos')

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO  = "Tripurasundari-maa-sohay/Omm-Money"
OUT_PATH     = "portfolio/data/processed/kronos_signals.json"
COST_PATH    = "portfolio/data/holdings_cost.json"

def gh_get(path):
    h = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    r = requests.get(f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}?ref=main", headers=h, timeout=15)
    if r.status_code == 200:
        return json.loads(base64.b64decode(r.json()["content"]).decode()), r.json().get("sha")
    return None, None

def gh_put(path, content_str, sha, message):
    h = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    url  = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    body = {"message": message, "content": base64.b64encode(content_str.encode()).decode(), "branch": "main"}
    if sha: body["sha"] = sha
    for a in range(3):
        r = requests.put(url, headers=h, json=body, timeout=20)
        if r.status_code in (200, 201): return True
        if r.status_code == 409:
            r2 = requests.get(url, headers=h, timeout=10)
            body["sha"] = r2.json().get("sha")
        time.sleep(5*(a+1))
    return False

def get_open_tickers():
    cost, _ = gh_get(COST_PATH)
    if not cost: return {}
    result = {}
    for h in cost.get("us", {}).get("open", []):
        result[h["tk"]] = {"market": "US"}
    for h in cost.get("india", {}).get("open", []):
        result[h["tk"]] = {"market": "India"}
    return result

def fetch_history(tk, market):
    """Fetch raw CSV string from GitHub (base64-decode, not JSON-parse)."""
    import base64
    sub = "us" if market == "US" else "india"
    path = f"portfolio/data/history/{sub}/{tk}.csv"
    h = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    r = requests.get(f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}?ref=main",
                     headers=h, timeout=15)
    if r.status_code == 200:
        return base64.b64decode(r.json()["content"]).decode()
    return None

def csv_to_df(csv_str):
    import pandas as pd, io
    df = pd.read_csv(io.StringIO(csv_str))  # has header: date,open,high,low,close,volume,adj_close
    df["date"] = pd.to_datetime(df["date"], format="%Y-%m-%d", errors="coerce")
    df = df.dropna(subset=["date"]).set_index("date").sort_index()
    df = df[["open","high","low","close","volume"]].dropna().astype(float)
    return df.tail(60)

def next_trading_days(last_date, n=5):
    days, d = [], last_date
    while len(days) < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days.append(d)
    return days

def main():
    print(f"\n{datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')} kronos_predict start")

    import torch
    from model import Kronos, KronosTokenizer, KronosPredictor
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"  Loading Kronos-small ({device})...")
    tokenizer = KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-base")
    model     = Kronos.from_pretrained("NeoQuasar/Kronos-small")
    predictor = KronosPredictor(model, tokenizer, device=device, max_context=60)
    print("  Kronos loaded OK")

    tickers = get_open_tickers()
    print(f"  {len(tickers)} open positions")

    results = {}
    for tk, meta in sorted(tickers.items()):
        try:
            csv_str = fetch_history(tk, meta["market"])
            if not csv_str:
                print(f"  {tk}: no history — skip"); continue

            df = csv_to_df(csv_str)
            if len(df) < 20:
                print(f"  {tk}: only {len(df)} rows — skip"); continue

            import pandas as pd
            x_ts = pd.Series(df.index)
            y_ts = pd.bdate_range(start=df.index[-1] + pd.Timedelta(days=1), periods=5)

            pred = predictor.predict(df=df, x_timestamp=x_ts, y_timestamp=pd.Series(y_ts),
                                     pred_len=5, T=1.0, top_p=0.9, sample_count=1)

            cur  = float(df["close"].iloc[-1])
            p5d  = float(pred["close"].iloc[-1]) if "close" in pred.columns else None
            p1d  = float(pred["close"].iloc[0])  if "close" in pred.columns else None
            pct  = round((p5d - cur) / cur * 100, 2) if p5d else None
            sig  = "UP" if pct and pct > 1 else ("DOWN" if pct and pct < -1 else "FLAT")

            results[tk] = {
                "market":        meta["market"],
                "current_close": round(cur, 4),
                "pred_1d":       round(p1d, 4) if p1d else None,
                "pred_5d":       round(p5d, 4) if p5d else None,
                "pred_pct_5d":   pct,
                "signal":        sig,
                "pred_dates":    [str(d.date()) for d in y_ts],
                "pred_closes":   [round(float(pred["close"].iloc[i]), 4) for i in range(len(y_ts))] if "close" in pred.columns else [],
                "as_of":         str(datetime.utcnow().date()),
            }
            print(f"  {tk:<12} {cur:.2f} → 5d {p5d:.2f} ({pct:+.2f}%) [{sig}]")

        except Exception as e:
            print(f"  {tk}: {e}", file=sys.stderr)

    payload = {
        "generated": datetime.now(timezone.utc).isoformat() + "Z",
        "model":     "NeoQuasar/Kronos-small",
        "horizon":   "5 trading days",
        "forecasts": results,
    }
    _, sha = gh_get(OUT_PATH)
    ok = gh_put(OUT_PATH, json.dumps(payload, indent=2), sha,
                f"data: kronos {datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')} [skip ci]")
    print(f"  {'Committed' if ok else 'FAILED'} kronos_signals.json ({len(results)} tickers)")

if __name__ == "__main__":
    main()
