"""
data_audit.py — Data integrity audit + self-healing for portfolio dashboard.

Reads holdings_cost.json, holdings_prices.json, market_indices.json, stock_signals.json.
Checks data quality, attempts targeted self-healing (FX rate, individual ticker retries),
and writes data/processed/audit.json.

Exits with code 0 always (informational — does not break CI pipeline).
"""

from __future__ import annotations

import sys
import json
import time
import requests
import yfinance as yf
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COST_FILE    = ROOT / "data" / "holdings_cost.json"
PRICES_FILE  = ROOT / "data" / "processed" / "holdings_prices.json"
INDICES_FILE = ROOT / "data" / "processed" / "market_indices.json"
SIGNALS_FILE = ROOT / "data" / "processed" / "stock_signals.json"
AUDIT_FILE   = ROOT / "data" / "processed" / "audit.json"
HISTORY_FILE = ROOT / "data" / "processed" / "audit_history.json"
TRANSACTIONS_US_FILE = ROOT / "data" / "transactions_us.json"
HISTORY_MAX_DAYS = 90

FX_SOURCES = [
    "https://open.er-api.com/v6/latest/USD",
    "https://api.exchangerate-api.com/v4/latest/USD",
]
FX_MIN = 70.0
FX_MAX = 120.0
STALE_THRESHOLD_MIN = 20.0
MAX_INDIVIDUAL_HEAL = 3   # only auto-retry if <= this many tickers failed

# ── PDF / xlsx cross-check thresholds ────────────────────────────────────────
# Tolerated drift between live (mv+cash) and PDF account_value_statement.
# Prices drift during trading hours, so anything under HEADLINE_DRIFT_PCT_INFO
# is normal. HEADLINE_DRIFT_PCT_ALERT means something is structurally wrong
# (e.g. open[] qty out of sync with broker, or fx_rate misapplied).
HEADLINE_DRIFT_PCT_INFO  = 5.0     # market move since statement date
HEADLINE_DRIFT_PCT_ALERT = 15.0    # structural mismatch
# How old the PDF anchor can get before we warn that a new statement is due.
PDF_STALE_DAYS_WARN = 14
PDF_STALE_DAYS_ALERT = 35
# How far our per-ticker fee attribution (open fees + closed _costs_paid) can
# stray from xlsx total commission before alerting. Some drift is expected
# because PDF covers Dec 1- vs xlsx covers Dec 15- (~14-day window mismatch
# observed on the 2026-05-23 reconciliation: $22.65 of pre-window close fees).
FEE_RECONCILE_TOLERANCE_USD = 50.0


# ── Helpers ──────────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path: Path):
    """Load JSON file; return None on any error."""
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        print(f"[audit] Warning: could not load {path.name}: {e}", file=sys.stderr)
        return None


def save_json(path: Path, data: dict) -> bool:
    """Write JSON atomically; return True on success."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        tmp.replace(path)
        return True
    except Exception as e:
        print(f"[audit] Warning: could not save {path.name}: {e}", file=sys.stderr)
        return False


def data_age_minutes(generated_str: str) -> float:
    """Return age in minutes of an ISO timestamp string; return 999 on parse error."""
    if not generated_str:
        return 999.0
    try:
        ts = datetime.fromisoformat(generated_str.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - ts
        return delta.total_seconds() / 60.0
    except Exception:
        return 999.0


def is_price_ok(val) -> bool:
    """Return True if val is a finite nonzero number."""
    try:
        return val is not None and float(val) != 0.0
    except (TypeError, ValueError):
        return False


# ── FX healing ───────────────────────────────────────────────────────────────

def fetch_fx_rate() -> float | None:
    """Try each FX source in order; return INR/USD rate or None."""
    for url in FX_SOURCES:
        try:
            resp = requests.get(url, timeout=8)
            resp.raise_for_status()
            data = resp.json()
            # open.er-api.com: {"rates": {"INR": ...}}
            # exchangerate-api.com/v4: {"rates": {"INR": ...}}
            rate = None
            rates = data.get("rates") or {}
            rate = rates.get("INR")
            if rate and FX_MIN <= float(rate) <= FX_MAX:
                return float(rate)
        except Exception as e:
            print(f"[audit] FX source {url} failed: {e}", file=sys.stderr)
    return None


# ── Ticker price healing ──────────────────────────────────────────────────────

def fetch_price_yf(yf_sym: str) -> float | None:
    """Fetch latest price via yfinance fast_info, fallback to history."""
    try:
        t = yf.Ticker(yf_sym)
        ltp = None
        try:
            fi = t.fast_info
            ltp = getattr(fi, "last_price", None) or getattr(fi, "regularMarketPrice", None)
        except Exception:
            pass
        if ltp and float(ltp) > 0:
            return float(ltp)
        # fallback: history
        hist = t.history(period="1d")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
    except Exception as e:
        print(f"[audit] yfinance fetch for {yf_sym} failed: {e}", file=sys.stderr)
    return None


# ── Main audit ────────────────────────────────────────────────────────────────

def main() -> None:
    healed: list[str] = []
    alerts: list[dict] = []

    # 1. Load all data files
    cost_data    = load_json(COST_FILE)
    prices_data  = load_json(PRICES_FILE)
    indices_data = load_json(INDICES_FILE)
    _signals     = load_json(SIGNALS_FILE)   # loaded for completeness; not yet checked

    # 2. Build expected ticker list from holdings_cost.json
    # Each entry has a "yf" field (yfinance symbol) and a "tk" field (dashboard ticker key).
    # For price lookup we use tk; for yfinance retry we use yf.
    expected: list[dict] = []   # [{tk, yf_sym}]
    if cost_data:
        for h in cost_data.get("us", {}).get("open", []):
            tk = h.get("tk") or ""
            yf_sym = h.get("yf") or tk
            if tk:
                expected.append({"tk": tk, "yf_sym": yf_sym})
        for h in cost_data.get("india", {}).get("open", []):
            tk = h.get("tk") or ""
            yf_sym = h.get("yf") or tk
            if tk:
                # Deduplicate: some tickers (e.g. GOLDBEES_M / GOLDBEES_U) share the same yf sym
                expected.append({"tk": tk, "yf_sym": yf_sym})

    total_expected = len(expected)

    # 3. Check data age
    prices_generated = (prices_data or {}).get("generated", "")
    data_age_min = data_age_minutes(prices_generated)

    if data_age_min > STALE_THRESHOLD_MIN:
        print(
            f"[audit] Data age {data_age_min:.1f} min > {STALE_THRESHOLD_MIN} min — "
            "watchdog should handle re-fetch.",
            file=sys.stderr,
        )

    # 4. Check FX rate
    fx_rate = None
    fx_healed = False
    if indices_data:
        fx_rate = indices_data.get("fx_rate")

    fx_bad = fx_rate is None or not (FX_MIN <= float(fx_rate or 0) <= FX_MAX)
    if fx_bad:
        print(f"[audit] FX rate {fx_rate!r} out of range — attempting heal.", file=sys.stderr)
        fresh_fx = fetch_fx_rate()
        if fresh_fx:
            # Update market_indices.json with healed rate
            if indices_data:
                indices_data["fx_rate"] = fresh_fx
                if save_json(INDICES_FILE, indices_data):
                    fx_rate = fresh_fx
                    fx_healed = True
                    healed.append("fx_retried_ok")
                    print(f"[audit] FX healed: {fresh_fx}", file=sys.stderr)
        else:
            # Use last known rate or hardcoded fallback
            fallback = None
            if indices_data:
                fallback = indices_data.get("fx_rate")
            if fallback is None or not (FX_MIN <= float(fallback or 0) <= FX_MAX):
                fallback = 84.0
            fx_rate = fallback
            alerts.append({
                "type": "fx_unavailable",
                "message": "All FX sources failed. Last known rate used.",
            })
            print("[audit] All FX sources failed; using fallback rate.", file=sys.stderr)

    # 5. Check individual ticker prices
    prices_map = (prices_data or {}).get("prices", {})
    failed_tickers: list[dict] = []
    ok_count = 0

    for entry in expected:
        tk = entry["tk"]
        price_entry = prices_map.get(tk)
        ltp = (price_entry or {}).get("ltp") if price_entry else None
        if is_price_ok(ltp):
            ok_count += 1
        else:
            failed_tickers.append(entry)

    tickers_failed_initial = len(failed_tickers)
    tickers_still_failed: list[dict] = []

    # Decide healing strategy
    source_wide_down = (
        total_expected > 0
        and tickers_failed_initial / total_expected > 0.5
    )

    if source_wide_down:
        # Don't attempt individual retries — it's a source-wide outage
        tickers_still_failed = failed_tickers
        print(
            f"[audit] {tickers_failed_initial}/{total_expected} tickers failed — "
            "treating as source-wide outage, skipping individual retries.",
            file=sys.stderr,
        )
        alerts.append({
            "type": "data_source_down",
            "message": (
                f"{tickers_failed_initial}/{total_expected} tickers failed. "
                "yfinance/NSE API may be down."
            ),
        })
    elif 0 < tickers_failed_initial <= MAX_INDIVIDUAL_HEAL:
        # Retry each failed ticker individually
        prices_updated = False
        for entry in failed_tickers:
            tk = entry["tk"]
            yf_sym = entry["yf_sym"]
            print(f"[audit] Retrying price for {tk} ({yf_sym})…", file=sys.stderr)
            ltp = fetch_price_yf(yf_sym)
            if ltp and ltp > 0:
                # Patch into prices_map
                if tk not in prices_map or not isinstance(prices_map[tk], dict):
                    prices_map[tk] = {}
                prices_map[tk]["ltp"] = ltp
                prices_map[tk]["as_of"] = now_iso()
                prices_updated = True
                ok_count += 1
                healed.append(f"{tk}_price_retried_ok")
                print(f"[audit] {tk} price healed: {ltp}", file=sys.stderr)
            else:
                tickers_still_failed.append(entry)
                print(f"[audit] {tk} price retry failed.", file=sys.stderr)

        if prices_updated and prices_data is not None:
            prices_data["prices"] = prices_map
            save_json(PRICES_FILE, prices_data)

    elif tickers_failed_initial > MAX_INDIVIDUAL_HEAL and not source_wide_down:
        # 4–50%: too many to heal but not source-wide — still mark them failed
        tickers_still_failed = failed_tickers

    # 6. Raise DELISTED_OR_RENAMED for isolated persistent failures
    for entry in tickers_still_failed:
        tk = entry["tk"]
        if not source_wide_down:  # isolated
            alerts.append({
                "type": "delisted_or_renamed",
                "ticker": tk,
                "message": (
                    f"Price null after all sources. "
                    f"Check if {tk} is delisted or symbol changed."
                ),
            })

    # 7. Recount final ok/failed
    final_failed = len(tickers_still_failed)
    final_ok = total_expected - final_failed

    # 7b. ── PDF cross-checks (US side) ──────────────────────────────────────
    # Compare runtime-computed numbers against the latest broker PDF anchors.
    # PDF is source of truth for positions, cash, anchors, totals; anything
    # the dashboard shows that disagrees with the PDF needs surfacing.
    crosscheck = {}
    if cost_data:
        us = cost_data.get("us", {}) or {}
        stmt_val   = us.get("account_value_statement")
        stmt_pl    = us.get("total_pl_statement")
        stmt_cash  = us.get("cash")
        infusion   = us.get("cash_infusion_itd")
        monthly    = us.get("monthly", {}) or {}
        label_dates = monthly.get("label_dates", []) or []
        anchor_av   = monthly.get("account_value", []) or []
        as_of       = us.get("as_of") or cost_data.get("as_of")
        open_pos    = us.get("open", []) or []

        # ── (a) Live mv + cash vs statement account_value_statement ──
        mv = 0.0
        priced = 0
        for h in open_pos:
            tk  = h.get("tk")
            qty = h.get("qty") or 0
            p   = (prices_map.get(tk) or {})
            ltp = p.get("ltp")
            if isinstance(ltp, (int, float)) and ltp > 0 and qty:
                mv += qty * ltp
                priced += 1
        live_total = mv + (stmt_cash or 0)
        if stmt_val and live_total > 0 and priced == len(open_pos):
            drift_pct = (live_total - stmt_val) / stmt_val * 100
            crosscheck["live_vs_statement_pct"] = round(drift_pct, 2)
            crosscheck["live_total_usd"]        = round(live_total, 2)
            crosscheck["statement_total_usd"]   = round(stmt_val, 2)
            if abs(drift_pct) >= HEADLINE_DRIFT_PCT_ALERT:
                alerts.append({
                    "type":    "headline_vs_statement_drift",
                    "message": (
                        f"Live total ${live_total:,.0f} vs statement "
                        f"${stmt_val:,.0f} drifts {drift_pct:+.1f}% — "
                        "check open[] qty / fx_rate / cash."
                    ),
                })

        # ── (b) Last monthly anchor must equal account_value_statement ──
        if stmt_val and anchor_av and anchor_av[-1] not in (None, 0):
            anchor_drift = abs(anchor_av[-1] - stmt_val)
            if anchor_drift > 1.0:   # >$1 means parser misaligned anchor vs statement
                alerts.append({
                    "type":    "anchor_vs_statement_mismatch",
                    "message": (
                        f"Last monthly anchor ${anchor_av[-1]:,.2f} does not match "
                        f"statement account_value ${stmt_val:,.2f} "
                        f"(diff ${anchor_drift:,.2f}). PDF parser bug or stale anchor."
                    ),
                })

        # ── (c) Statement freshness (PDF age) ──
        if as_of:
            try:
                # PDF as_of format: "22-May-2026"
                as_of_dt = datetime.strptime(as_of, "%d-%b-%Y").replace(tzinfo=timezone.utc)
                age_days = (datetime.now(timezone.utc) - as_of_dt).days
                crosscheck["pdf_age_days"] = age_days
                if age_days >= PDF_STALE_DAYS_ALERT:
                    alerts.append({
                        "type": "pdf_stale",
                        "message": (
                            f"PDF statement is {age_days} days old. "
                            "Download the latest statement from broker portal and re-run sync.sh."
                        ),
                    })
                elif age_days >= PDF_STALE_DAYS_WARN:
                    alerts.append({
                        "type": "pdf_stale_soft",
                        "message": f"PDF statement is {age_days} days old. New statement due soon.",
                    })
            except Exception:
                pass

        # ── (d) Cash infusion sanity ──
        if infusion is not None and infusion < 0:
            alerts.append({
                "type": "cash_infusion_negative",
                "message": f"cash_infusion_itd = ${infusion:,.2f} (negative — parser bug)",
            })

        # ── (e) Anchor count consistency ──
        if label_dates and anchor_av and len(label_dates) != len(anchor_av):
            alerts.append({
                "type": "anchor_array_length_mismatch",
                "message": (
                    f"monthly.label_dates has {len(label_dates)} entries but "
                    f"monthly.account_value has {len(anchor_av)} — parser bug."
                ),
            })

        # ── (f) xlsx reconciliation (only if transactions_us.json present) ──
        tx_us = load_json(TRANSACTIONS_US_FILE)
        if tx_us and isinstance(tx_us.get("totals"), dict):
            xlsx_comm = tx_us["totals"].get("commission_total")
            if xlsx_comm is not None:
                open_fees   = sum((p.get("fees") or 0) for p in open_pos)
                closed_cp   = sum((p.get("_costs_paid") or 0) for p in (us.get("closed", []) or []))
                attributed  = open_fees + closed_cp
                fee_diff    = xlsx_comm - attributed
                crosscheck["fees_xlsx_total"]      = round(xlsx_comm, 2)
                crosscheck["fees_attributed_sum"]  = round(attributed, 2)
                crosscheck["fees_diff_usd"]        = round(fee_diff, 2)
                if abs(fee_diff) > FEE_RECONCILE_TOLERANCE_USD:
                    alerts.append({
                        "type": "fee_attribution_drift",
                        "message": (
                            f"xlsx commission total ${xlsx_comm:,.2f} vs holdings "
                            f"open fees + closed _costs_paid ${attributed:,.2f} "
                            f"(diff ${fee_diff:+,.2f}, > ${FEE_RECONCILE_TOLERANCE_USD} tolerance). "
                            "Run scripts/patch_fees_from_xlsx.py."
                        ),
                    })

    # 8. Write audit.json
    audit = {
        "generated": now_iso(),
        "status": "alert" if alerts else "ok",
        "alerts": alerts,
        "healed": healed,
        "tickers_expected": total_expected,
        "tickers_ok": final_ok,
        "tickers_failed": final_failed,
        "fx_rate": round(float(fx_rate), 4) if fx_rate is not None else None,
        "data_age_min": round(data_age_min, 2),
        "crosscheck": crosscheck,
    }
    save_json(AUDIT_FILE, audit)

    # 9. Append to rolling 90-day history (one entry per calendar day, worst status wins)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    hist_data = load_json(HISTORY_FILE) or {"history": []}
    history = hist_data.get("history", [])

    # Find today's existing entry (if any) and merge — worst status wins
    existing_idx = next((i for i, d in enumerate(history) if d.get("date") == today), None)
    def status_rank(s): return {"ok": 0, "healed": 1, "alert": 2}.get(s, 0)

    new_entry = {
        "date": today,
        "status": "healed" if (not alerts and healed) else ("alert" if alerts else "ok"),
        "alerts": alerts,
        "healed": healed,
        "runs": 1,
        "tickers_ok": final_ok,
        "tickers_expected": total_expected,
        "data_age_min": round(data_age_min, 1),
    }
    if existing_idx is not None:
        prev = history[existing_idx]
        # Keep worst status of day
        if status_rank(new_entry["status"]) >= status_rank(prev.get("status", "ok")):
            new_entry["runs"] = prev.get("runs", 0) + 1
            history[existing_idx] = new_entry
        else:
            history[existing_idx]["runs"] = prev.get("runs", 0) + 1
    else:
        history.append(new_entry)

    # Keep only last HISTORY_MAX_DAYS days, sorted descending
    history.sort(key=lambda d: d["date"], reverse=True)
    history = history[:HISTORY_MAX_DAYS]
    save_json(HISTORY_FILE, {"history": history})

    print(
        f"[audit] Done — status={audit['status']}, "
        f"tickers {final_ok}/{total_expected} ok, "
        f"fx={audit['fx_rate']}, "
        f"age={audit['data_age_min']}min, "
        f"healed={healed}, "
        f"alerts={[a['type'] for a in alerts]}",
        file=sys.stderr,
    )
    # Always exit 0 — audit result is informational
    sys.exit(0)


if __name__ == "__main__":
    main()
