"""
snapshot_eod.py — daily end-of-day OHLCV snapshot for full US + India universe.

Writes/appends to:
  data/history/us/{TICKER}.csv      — NYSE + NASDAQ (filtered: price >= $1, vol > 10k)
  data/history/india/{TICKER}.csv   — NSE EQ series (excl. SME board)

Universe sources (free, no API key):
  US:    SEC EDGAR exchange tickers  https://www.sec.gov/files/company_tickers_exchange.json
  India: NSE equity master CSV       https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv

Each CSV row: date, open, high, low, close, volume, adj_close

Data quality rules applied on every run:
  1. Penny/shell/SPAC filter  — skip tickers with latest close < $1 or volume < 10,000
  2. Delisted cleanup         — yfinance returns no data → delete CSV if exists
  3. Ticker recycling         — gap > 90 days in stored data → wipe + full re-backfill
  4. Corporate action detect  — stored last close differs >5% from yfinance adj close
                                for same date → wipe + full re-backfill

First run per ticker: backfills N years of history automatically.
Subsequent runs: idempotent append (skips dates already stored, unless re-backfill triggered).

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

# ── Quality thresholds ────────────────────────────────────────────────────────
MIN_PRICE        = 1.00        # below → penny stock, skip
MIN_VOLUME       = 10_000      # below → illiquid shell, skip
CORP_ACTION_PCT  = 0.05        # >5% price divergence vs stored → re-backfill
RECYCLED_GAP     = 90          # days gap in stored data → treat as fresh ticker


# ─────────────────────────────────────────────────────────────────────────────
# UNIVERSE FETCH
# ─────────────────────────────────────────────────────────────────────────────

def get_us_tickers() -> list[str]:
    """All NYSE + NASDAQ tickers from SEC EDGAR (free, no API key)."""
    try:
        print("Fetching US universe from SEC EDGAR…")
        resp = requests.get(
            "https://www.sec.gov/files/company_tickers_exchange.json",
            headers={"User-Agent": "portfolio-dashboard/1.0 contact@example.com"},
            timeout=30,
        )
        resp.raise_for_status()
        data    = resp.json()
        fields  = data.get("fields", [])
        rows    = data.get("data", [])
        tk_idx  = fields.index("ticker")   if "ticker"   in fields else 2
        ex_idx  = fields.index("exchange") if "exchange" in fields else 3
        allowed = {"NYSE", "NASDAQ", "NYSE MKT", "NYSE ARCA", "BATS"}
        tickers = sorted({
            str(row[tk_idx]).strip().upper()
            for row in rows
            if str(row[ex_idx]).strip().upper() in allowed
            and 1 <= len(str(row[tk_idx]).strip()) <= 5
        })
        print(f"  US universe: {len(tickers)} tickers (NYSE + NASDAQ)")
        return tickers
    except Exception as exc:
        print(f"WARN  SEC EDGAR failed: {exc} — falling back to SP500+NDX100", file=sys.stderr)
        sys.path.insert(0, str(_SCRIPTS_DIR))
        from screener import get_sp500_tickers, get_ndx100_tickers
        sp, _ = get_sp500_tickers()
        nd, _ = get_ndx100_tickers()
        return sorted(set(sp + nd))


def get_india_tickers() -> list[str]:
    """All NSE EQ-series tickers (excl. SME board) — free from NSE master CSV."""
    try:
        print("Fetching India universe from NSE equity master…")
        resp = requests.get(
            "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
            headers=_HEADERS, timeout=30,
        )
        resp.raise_for_status()
        df      = pd.read_csv(io.StringIO(resp.text))
        sym_col = next((c for c in df.columns if "symbol" in c.lower()), df.columns[0])
        ser_col = next((c for c in df.columns if "series" in c.lower()), None)
        if ser_col:
            # EQ only — excludes SME, BE, BL, etc.
            df = df[df[ser_col].astype(str).str.strip().str.upper() == "EQ"]
        tickers = sorted({f"{str(s).strip()}.NS" for s in df[sym_col] if str(s).strip()})
        print(f"  India universe: {len(tickers)} NSE EQ tickers (SME excluded)")
        return tickers
    except Exception as exc:
        print(f"WARN  NSE master failed: {exc} — using holdings only", file=sys.stderr)
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


def yf_to_rows(hist: pd.DataFrame) -> pd.DataFrame:
    """Convert yfinance history DataFrame → our CSV schema."""
    if hist.empty:
        return pd.DataFrame(columns=CSV_COLS)
    return pd.DataFrame({
        "date":      pd.to_datetime(hist.index.date),
        "open":      hist["Open"].round(4),
        "high":      hist["High"].round(4),
        "low":       hist["Low"].round(4),
        "close":     hist["Close"].round(4),
        "volume":    hist["Volume"].astype("int64"),
        "adj_close": hist["Close"].round(4),   # auto_adjust=True → Close IS adj close
    })


# ─────────────────────────────────────────────────────────────────────────────
# DATA QUALITY CHECKS
# ─────────────────────────────────────────────────────────────────────────────

def is_penny_or_illiquid(hist: pd.DataFrame) -> bool:
    """True if latest close < $1 or latest volume < 10,000 — penny/shell/SPAC."""
    if hist.empty:
        return True
    last = hist.iloc[-1]
    return float(last["Close"]) < MIN_PRICE or float(last["Volume"]) < MIN_VOLUME


def detect_ticker_recycled(existing: pd.DataFrame) -> bool:
    """
    True if stored data has a gap > 90 days — indicates ticker was delisted
    and reissued to a new company. Wipe and start fresh.
    """
    if len(existing) < 2:
        return False
    dates = existing["date"].sort_values().dt.date.tolist()
    for a, b in zip(dates, dates[1:]):
        if (b - a).days > RECYCLED_GAP:
            return True
    return False


def detect_corporate_action(existing: pd.DataFrame, hist: pd.DataFrame) -> bool:
    """
    True if stored last close differs >5% from yfinance's adjusted close for
    the same date — signals a retroactive split/merger adjustment.
    yfinance auto_adjust=True retroactively adjusts all history on splits.
    """
    if existing.empty or hist.empty:
        return False
    # Find latest date stored that also appears in fresh yfinance data
    stored_dates = set(existing["date"].dt.date)
    yf_dates     = {d.date() for d in hist.index}
    overlap      = stored_dates & yf_dates
    if not overlap:
        return False
    check_date   = max(overlap)
    stored_row   = existing[existing["date"].dt.date == check_date]
    yf_row       = hist[hist.index.date == check_date]
    if stored_row.empty or yf_row.empty:
        return False
    stored_close = float(stored_row["close"].iloc[0])
    yf_close     = float(yf_row["Close"].iloc[0])
    if stored_close <= 0:
        return False
    divergence = abs(stored_close - yf_close) / stored_close
    return divergence > CORP_ACTION_PCT


# ─────────────────────────────────────────────────────────────────────────────
# BATCH FETCH + PROCESS
# ─────────────────────────────────────────────────────────────────────────────

def process_batch(
    tickers:      list[str],
    hist_dir:     Path,
    start_date:   str,
    today_date:   date,
    chunk_size:   int,
    sleep_s:      float,
    market_label: str,
) -> dict[str, int]:
    """
    Batch-download OHLCV, apply quality rules, append to per-ticker CSVs.
    Stats: new_tickers, updated, skipped, rebackfilled, deleted, errors.
    """
    stats = {"new_tickers": 0, "updated": 0, "skipped": 0,
             "rebackfilled": 0, "deleted": 0, "errors": 0}
    total_chunks = (len(tickers) + chunk_size - 1) // chunk_size

    # Build set of existing CSVs for delisting detection
    existing_files = {p.stem.upper(): p for p in hist_dir.glob("*.csv")}

    for ci, i in enumerate(range(0, len(tickers), chunk_size), 1):
        chunk = tickers[i : i + chunk_size]
        print(f"  [{market_label}] Batch {ci}/{total_chunks}: {len(chunk)}…", end=" ", flush=True)

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
            print(f"ERROR fetch: {exc}")
            stats["errors"] += len(chunk)
            time.sleep(sleep_s)
            continue

        ok = 0
        for tk in chunk:
            # Normalise CSV stem (strip .NS / .BO suffixes)
            stem     = tk.replace(".NS", "").replace(".BO", "").upper()
            csv_path = hist_dir / f"{stem}.csv"

            try:
                # Extract ticker slice from batch result
                if raw.empty:
                    sub = pd.DataFrame()
                elif isinstance(raw.columns, pd.MultiIndex):
                    try:
                        sub = raw.xs(tk, level=1, axis=1)
                    except KeyError:
                        sub = pd.DataFrame()
                else:
                    sub = raw  # single-ticker batch

                sub = sub.dropna(subset=["Close"]) if not sub.empty else sub

                # ── Rule 2: Delisted — no data returned ──────────────────
                if sub.empty or sub["Close"].isna().all():
                    if csv_path.exists():
                        csv_path.unlink()
                        stats["deleted"] += 1
                        print(f"\n    DEL {tk} (delisted — no data)")
                    else:
                        stats["errors"] += 1
                    continue

                # ── Rule 1: Penny / illiquid filter ──────────────────────
                if is_penny_or_illiquid(sub):
                    if csv_path.exists():
                        csv_path.unlink()
                        stats["deleted"] += 1
                    else:
                        stats["skipped"] += 1
                    continue

                existing = load_csv(csv_path)

                # ── Rule 3: Ticker recycled (gap > 90 days) ───────────────
                recycled = detect_ticker_recycled(existing)
                if recycled:
                    print(f"\n    RECYCLE {tk} — gap detected, re-backfilling")
                    existing  = pd.DataFrame(columns=CSV_COLS)
                    stats["rebackfilled"] += 1

                # ── Rule 4: Corporate action (retroactive price adjustment) ─
                elif detect_corporate_action(existing, sub):
                    print(f"\n    CORP-ACTION {tk} — price divergence >5%, re-backfilling")
                    existing = pd.DataFrame(columns=CSV_COLS)
                    stats["rebackfilled"] += 1

                # ── Normal append ─────────────────────────────────────────
                new_rows = yf_to_rows(sub)
                if new_rows.empty:
                    stats["errors"] += 1
                    continue

                stored_dates = set(existing["date"].dt.date) if not existing.empty else set()
                fresh = new_rows[~new_rows["date"].dt.date.isin(stored_dates)]

                if fresh.empty:
                    stats["skipped"] += 1
                    continue

                is_new = existing.empty
                save_csv(csv_path, pd.concat([existing, fresh], ignore_index=True))
                stats["new_tickers" if is_new else "updated"] += 1
                ok += 1

            except Exception as exc:
                print(f"\n    WARN  {tk}: {exc}", file=sys.stderr)
                stats["errors"] += 1

        print(f"ok={ok}/{len(chunk)}")
        time.sleep(sleep_s)

    # ── Rule 2 (sweep): delete CSVs for tickers no longer in universe ─────────
    universe_stems = {
        tk.replace(".NS", "").replace(".BO", "").upper()
        for tk in tickers
    }
    for stem, path in existing_files.items():
        if stem not in universe_stems and path.exists():
            path.unlink()
            stats["deleted"] += 1
            print(f"  DEL {stem}.csv — no longer in universe")

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
    meta[market] = {"last_run": datetime.utcnow().isoformat() + "Z", "universe": tickers, **stats}
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, indent=2))


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--market",         default="both", choices=["us", "india", "both"])
    p.add_argument("--chunk",          type=int,   default=100)
    p.add_argument("--sleep",          type=float, default=1.0)
    p.add_argument("--backfill-years", type=int,   default=2, dest="backfill_years")
    return p.parse_args()


def main() -> int:
    args       = parse_args()
    today      = date.today()
    start_date = str(today - timedelta(days=args.backfill_years * 365))

    print(f"EOD Snapshot — {today}  backfill start: {start_date}")
    print(f"Quality rules: price>=${MIN_PRICE}  vol>{MIN_VOLUME:,}  "
          f"corp-action>{int(CORP_ACTION_PCT*100)}%  recycle-gap>{RECYCLED_GAP}d\n")

    if args.market in ("us", "both"):
        us_tickers = get_us_tickers()
        HIST_US.mkdir(parents=True, exist_ok=True)
        print(f"\nProcessing {len(us_tickers)} US tickers…")
        stats = process_batch(us_tickers, HIST_US, start_date, today, args.chunk, args.sleep, "US")
        write_meta("us", len(us_tickers), stats)
        print(f"  US — new:{stats['new_tickers']} upd:{stats['updated']} "
              f"skip:{stats['skipped']} rebkfill:{stats['rebackfilled']} "
              f"del:{stats['deleted']} err:{stats['errors']}")

    if args.market in ("india", "both"):
        in_tickers = get_india_tickers()
        HIST_IN.mkdir(parents=True, exist_ok=True)
        print(f"\nProcessing {len(in_tickers)} India tickers…")
        stats = process_batch(in_tickers, HIST_IN, start_date, today, args.chunk, args.sleep, "IN")
        write_meta("india", len(in_tickers), stats)
        print(f"  India — new:{stats['new_tickers']} upd:{stats['updated']} "
              f"skip:{stats['skipped']} rebkfill:{stats['rebackfilled']} "
              f"del:{stats['deleted']} err:{stats['errors']}")

    print(f"\nSnapshot complete — {today}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
