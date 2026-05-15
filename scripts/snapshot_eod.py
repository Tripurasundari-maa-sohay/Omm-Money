"""
snapshot_eod.py — daily end-of-day OHLCV snapshot for full US + India universe.

Writes/appends to:
  data/history/us/{TICKER}.csv      — all NYSE + NASDAQ listed companies
  data/history/india/{TICKER}.csv   — all NSE active equities

Universe sources (free, no API key):
  US:    SEC EDGAR company tickers   https://www.sec.gov/files/company_tickers.json
  India: NSE equity master CSV       https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv

Each CSV row: date, open, high, low, close, volume, adj_close

First run per ticker: backfills 2 years of history automatically.
Subsequent runs: appends today's session only (idempotent — skips if date already present).

Usage:
  python scripts/snapshot_eod.py                  # full universe
  python scripts/snapshot_eod.py --market us      # US only
  python scripts/snapshot_eod.py --market india   # India only
  python scripts/snapshot_eod.py --chunk 100      # tickers per yf.download() batch
  python scripts/snapshot_eod.py --backfill-years 2
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

_SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT         = _SCRIPTS_DIR.parent
HIST_US      = ROOT / "data" / "history" / "us"
HIST_IN      = ROOT / "data" / "history" / "india"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

CSV_COLS = ["date", "open", "high", "low", "close", "volume", "adj_close"]


# ─────────────────────────────────────────────────────────────────────────────
# UNIVERSE FETCH — free, no API key
# ─────────────────────────────────────────────────────────────────────────────

def get_us_tickers() -> list[str]:
    """
    All SEC-registered US companies with exchange tickers.
    Source: SEC EDGAR company_tickers.json (~10,000 entries).
    Filters to NYSE + NASDAQ only (excludes OTC/pink sheets).
    """
    try:
        print("Fetching US universe from SEC EDGAR…")
        resp = requests.get(
            "https://www.sec.gov/files/company_tickers_exchange.json",
            headers={"User-Agent": "portfolio-dashboard/1.0 contact@example.com"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        # Format: {"fields": [...], "data": [[cik, name, ticker, exchange], ...]}
        fields   = data.get("fields", [])
        rows     = data.get("data", [])
        tk_idx   = fields.index("ticker")   if "ticker"   in fields else 2
        ex_idx   = fields.index("exchange") if "exchange" in fields else 3

        allowed  = {"NYSE", "NASDAQ", "NYSE MKT", "NYSE ARCA", "BATS"}
        tickers  = []
        for row in rows:
            tk = str(row[tk_idx]).strip().upper()
            ex = str(row[ex_idx]).strip().upper()
            if tk and ex in allowed and len(tk) <= 5:  # skip long OTC symbols
                tickers.append(tk)

        tickers = sorted(set(tickers))
        print(f"  US universe: {len(tickers)} tickers (NYSE + NASDAQ)")
        return tickers

    except Exception as exc:
        print(f"WARN  SEC EDGAR fetch failed: {exc} — falling back to SP500+NDX100", file=sys.stderr)
        # Graceful fallback to screener universe
        sys.path.insert(0, str(_SCRIPTS_DIR))
        from screener import get_sp500_tickers, get_ndx100_tickers
        sp500, _ = get_sp500_tickers()
        ndx100, _ = get_ndx100_tickers()
        return sorted(set(sp500 + ndx100))


def get_india_tickers() -> list[str]:
    """
    All NSE-listed equities from NSE equity master CSV.
    Returns tickers in yfinance format: {SYMBOL}.NS
    """
    try:
        print("Fetching India universe from NSE equity master…")
        resp = requests.get(
            "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
            headers=_HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))

        # Column name varies — find symbol column
        sym_col = next(
            (c for c in df.columns if "symbol" in c.lower()),
            df.columns[0],
        )
        # Filter: only EQ series (exclude SME, ETF, rights etc.)
        series_col = next((c for c in df.columns if "series" in c.lower()), None)
        if series_col:
            df = df[df[series_col].astype(str).str.strip().str.upper() == "EQ"]

        tickers = [f"{str(s).strip()}.NS" for s in df[sym_col] if str(s).strip()]
        tickers = sorted(set(tickers))
        print(f"  India universe: {len(tickers)} NSE EQ tickers")
        return tickers

    except Exception as exc:
        print(f"WARN  NSE master fetch failed: {exc} — using holdings only", file=sys.stderr)
        # Fallback: India holdings only
        cost_file = ROOT / "data" / "holdings_cost.json"
        if cost_file.exists():
            cost = json.loads(cost_file.read_text())
            india_open = cost.get("india", {}).get("open", [])
            return [pos.get("yf", pos["tk"]) for pos in india_open if pos.get("tk")]
        return []


# ─────────────────────────────────────────────────────────────────────────────
# CSV HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def load_csv(path: Path) -> pd.DataFrame:
    if path.exists():
        try:
            return pd.read_csv(path, parse_dates=["date"])
        except Exception:
            return pd.DataFrame(columns=CSV_COLS)
    return pd.DataFrame(columns=CSV_COLS)


def save_csv(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df = df.drop_duplicates(subset=["date"]).sort_values("date")
    df.to_csv(path, index=False, date_format="%Y-%m-%d")


def df_to_rows(hist: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """Convert yfinance history DataFrame to our CSV schema."""
    if hist.empty:
        return pd.DataFrame(columns=CSV_COLS)
    rows = pd.DataFrame({
        "date":      hist.index.date,
        "open":      hist["Open"].round(4),
        "high":      hist["High"].round(4),
        "low":       hist["Low"].round(4),
        "close":     hist["Close"].round(4),
        "volume":    hist["Volume"].astype("int64"),
        "adj_close": hist["Close"].round(4),  # auto_adjust=True → Close IS adj_close
    })
    rows["date"] = pd.to_datetime(rows["date"])
    return rows


def dates_already_stored(existing: pd.DataFrame) -> set:
    if existing.empty:
        return set()
    return set(existing["date"].dt.date)


# ─────────────────────────────────────────────────────────────────────────────
# BATCH FETCH + APPEND
# ─────────────────────────────────────────────────────────────────────────────

def process_batch(
    tickers: list[str],
    hist_dir: Path,
    start_date: str,
    today_date: date,
    chunk_size: int,
    sleep_s: float,
    market_label: str,
) -> dict[str, int]:
    """
    Batch-download OHLCV for `tickers`, append new rows to each ticker's CSV.
    Returns stats: {"new_tickers": N, "updated": N, "skipped": N, "errors": N}
    """
    stats = {"new_tickers": 0, "updated": 0, "skipped": 0, "errors": 0}
    total_chunks = (len(tickers) + chunk_size - 1) // chunk_size

    for ci, i in enumerate(range(0, len(tickers), chunk_size), 1):
        chunk = tickers[i : i + chunk_size]
        print(f"  [{market_label}] Batch {ci}/{total_chunks}: {len(chunk)} tickers…", end=" ", flush=True)

        try:
            raw = yf.download(
                chunk,
                start=start_date,
                end=str(today_date + timedelta(days=1)),
                interval="1d",
                auto_adjust=True,
                progress=False,
                threads=True,
            )
        except Exception as exc:
            print(f"ERROR: {exc}")
            stats["errors"] += len(chunk)
            time.sleep(sleep_s)
            continue

        if raw.empty:
            print("empty")
            stats["errors"] += len(chunk)
            time.sleep(sleep_s)
            continue

        ok = 0
        for tk in chunk:
            csv_path = hist_dir / f"{tk.replace('.NS', '').replace('.BO', '')}.csv"

            try:
                # Extract this ticker's data
                if isinstance(raw.columns, pd.MultiIndex):
                    try:
                        sub = raw.xs(tk, level=1, axis=1)
                    except KeyError:
                        stats["errors"] += 1
                        continue
                else:
                    sub = raw  # single-ticker batch

                if sub.empty or sub["Close"].isna().all():
                    stats["errors"] += 1
                    continue

                new_rows = df_to_rows(sub.dropna(subset=["Close"]), tk)
                if new_rows.empty:
                    stats["errors"] += 1
                    continue

                existing   = load_csv(csv_path)
                stored_dates = dates_already_stored(existing)

                # Only keep rows not already stored
                fresh = new_rows[~new_rows["date"].dt.date.isin(stored_dates)]

                if fresh.empty:
                    stats["skipped"] += 1
                    continue

                is_new = existing.empty
                merged = pd.concat([existing, fresh], ignore_index=True)
                save_csv(csv_path, merged)

                if is_new:
                    stats["new_tickers"] += 1
                else:
                    stats["updated"] += 1
                ok += 1

            except Exception as exc:
                print(f"\n    WARN  {tk}: {exc}", file=sys.stderr)
                stats["errors"] += 1

        print(f"ok ({ok}/{len(chunk)})")
        time.sleep(sleep_s)

    return stats


# ─────────────────────────────────────────────────────────────────────────────
# META
# ─────────────────────────────────────────────────────────────────────────────

def write_meta(market: str, tickers: int, stats: dict) -> None:
    meta_path = ROOT / "data" / "history" / "_meta.json"
    meta: dict = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            pass
    meta[market] = {
        "last_run":   datetime.utcnow().isoformat() + "Z",
        "universe":   tickers,
        **stats,
    }
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, indent=2))


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Daily EOD OHLCV snapshot for US + India universe.")
    p.add_argument("--market",  default="both", choices=["us", "india", "both"])
    p.add_argument("--chunk",   type=int, default=100, help="Tickers per yf.download() batch")
    p.add_argument("--sleep",   type=float, default=1.0, help="Seconds between batches")
    p.add_argument("--backfill-years", type=int, default=2, dest="backfill_years",
                   help="Years of history to backfill for new tickers (default: 2)")
    return p.parse_args()


def main() -> int:
    args       = parse_args()
    today      = date.today()
    start_date = str(today - timedelta(days=args.backfill_years * 365))

    print(f"EOD Snapshot — {today}  backfill start: {start_date}")
    print(f"Chunk size: {args.chunk}  Sleep: {args.sleep}s  Market: {args.market}\n")

    total_stats: dict[str, dict] = {}

    if args.market in ("us", "both"):
        us_tickers = get_us_tickers()
        HIST_US.mkdir(parents=True, exist_ok=True)
        print(f"\nProcessing {len(us_tickers)} US tickers…")
        stats = process_batch(us_tickers, HIST_US, start_date, today, args.chunk, args.sleep, "US")
        write_meta("us", len(us_tickers), stats)
        total_stats["us"] = stats
        print(f"  US done — new:{stats['new_tickers']} updated:{stats['updated']} "
              f"skipped:{stats['skipped']} errors:{stats['errors']}")

    if args.market in ("india", "both"):
        in_tickers = get_india_tickers()
        HIST_IN.mkdir(parents=True, exist_ok=True)
        print(f"\nProcessing {len(in_tickers)} India tickers…")
        stats = process_batch(in_tickers, HIST_IN, start_date, today, args.chunk, args.sleep, "IN")
        write_meta("india", len(in_tickers), stats)
        total_stats["india"] = stats
        print(f"  India done — new:{stats['new_tickers']} updated:{stats['updated']} "
              f"skipped:{stats['skipped']} errors:{stats['errors']}")

    print(f"\nSnapshot complete — {today}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
