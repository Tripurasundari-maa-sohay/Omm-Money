#!/usr/bin/env python3
"""
parse_broker_pdf.py — Doha Bank Global statement → holdings_cost.json

Usage:
    python scripts/parse_broker_pdf.py path/to/statement.pdf
    python scripts/parse_broker_pdf.py path/to/statement.pdf --output data/holdings_cost.json

Extracts every field the dashboard needs:
    • account value, cash, net deposits, total P/L          → us.*
    • monthly cash, account value, % return, P&L, costs     → us.monthly
    • open positions  (qty, avg buy, fees, dividends)       → us.open
    • closed positions (income, costs, realised, return)    → us.closed
    • charges breakdown                                     → us.charges_breakdown

The India section is preserved as-is from the existing JSON.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

try:
    import pdfplumber
except ImportError:
    sys.exit("pip install pdfplumber")


# ───────────────────────── INSTRUMENT MAP ─────────────────────────
# Normalised name → (ticker, yfinance symbol, asset class).
# Add a line here when a new instrument appears in your statement.
TICKER_MAP = {
    "alphabetinc.classc":                  ("GOOG",  "GOOG",  "Stock"),
    "amazon.cominc.":                      ("AMZN",  "AMZN",  "Stock"),
    "broadcominc.":                        ("AVGO",  "AVGO",  "Stock"),
    "corninginc.":                         ("GLW",   "GLW",   "Stock"),
    "gevernovainc":                        ("GEV",   "GEV",   "Stock"),
    "gevernovainc.":                       ("GEV",   "GEV",   "Stock"),
    "microntechnologyinc.":                ("MU",    "MU",    "Stock"),
    "microsoftcorp.":                      ("MSFT",  "MSFT",  "Stock"),
    "mpmaterialscorp.":                    ("MP",    "MP",    "Stock"),
    "totalenergiesse":                     ("TTE",   "TTE",   "Stock"),
    "isharesmscisouthkoreaetf":            ("EWY",   "EWY",   "ETF"),
    "vanguards&p500growthetf":             ("VOOG",  "VOOG",  "ETF"),
    "8x8inc.":                             ("EGHT",  "EGHT",  "Stock"),
    "costcowholesalecorp.":                ("COST",  "COST",  "Stock"),
    "digitaloceanholdingsinc.":            ("DOCN",  "DOCN",  "Stock"),
    "innodatainc.":                        ("INOD",  "INOD",  "Stock"),
    "inversionesyrepresentacionessa-adr":  ("IRS",   "IRS",   "Stock"),
    "metaplatformsinc.":                   ("META",  "META",  "Stock"),
    "nvidiacorp.":                         ("NVDA",  "NVDA",  "Stock"),
    "rocketlabcorporation":                ("RKLB",  "RKLB",  "Stock"),
    "tollbrothersinc.":                    ("TOL",   "TOL",   "Stock"),
    "abrdnphysicalsilversharesetf":        ("SIVR",  "SIVR",  "ETF"),
    "directiondailymubull2xetf":           ("MUU",   "MUU",   "ETF"),
    "direxiondailymubull2xetf":            ("MUU",   "MUU",   "ETF"),
    "globalxdaxgermanyetf":                ("DAX",   "DAX",   "ETF"),
    "keelinfrastructurecorp":              ("KEEL",  "KEEL",  "Stock"),
    "roundhillhumanoidroboticsetf":        ("HUMN",  "HUMN",  "ETF"),
}


def norm(s: str) -> str:
    return re.sub(r"[\s,]+", "", s or "").lower()


def lookup(name: str):
    key = norm(name).split("(")[0]  # drop trailing "(ISIN:..." if present
    if key in TICKER_MAP:
        return TICKER_MAP[key]
    # try a fuzzy contains match
    for k, v in TICKER_MAP.items():
        if k in key or key in k:
            return v
    print(f"  [WARN] unmapped instrument: {name!r}", file=sys.stderr)
    return ("UNKNOWN_" + key[:10], "", "Stock")


# ───────────────────────── LOW-LEVEL HELPERS ─────────────────────────
def _floats(s: str) -> list[float]:
    return [float(x.replace(",", "")) for x in re.findall(r"-?\d{1,3}(?:,\d{3})*(?:\.\d+)?", s)]


def page_rows(page, y_tol: int = 4) -> list[list[str]]:
    """Group page words into rows by top-y proximity; sort each row left-to-right.
       Returns list of rows where each row is a list of word strings."""
    words = page.extract_words()
    buckets: dict[int, list[dict]] = defaultdict(list)
    for w in words:
        key = round(w["top"] / y_tol) * y_tol
        buckets[key].append(w)
    rows = []
    for y in sorted(buckets):
        rows.append([w["text"] for w in sorted(buckets[y], key=lambda x: x["x0"])])
    return rows


# ───────────────────────── SECTION PARSERS ─────────────────────────
def parse_account_summary(pdf) -> dict:
    """Page 2 — find the values that appear with 'USD' suffix in canonical order."""
    rows = page_rows(pdf.pages[1])
    # Period
    period_from = period_to = None
    for r in rows:
        line = " ".join(r)
        m = re.search(r"(\d{2}-\w{3}-\d{4}).*?(\d{2}-\w{3}-\d{4})", line)
        if m and not period_from:
            period_from, period_to = m.group(1), m.group(2)
    # Values: from the row that contains the four USD-marked numbers
    nums_usd = []
    for r in rows:
        for tok in r:
            m = re.match(r"^(-?\d{1,3}(?:,\d{3})*\.\d{2})USD$", tok)
            if m:
                nums_usd.append(float(m.group(1).replace(",", "")))
    # In the canonical PDF order: start_value (often 0.00), then P/L, deposits, end_value
    # but Doha sometimes lays them out: P/L, deposits, end, start.  Distinguish by magnitude:
    # • end_value > deposits (end_value = deposits + total_pl)
    # • start_value = 0 if new account
    pl = deposits = end = start = 0.0
    if len(nums_usd) >= 3:
        # Common observation: row reads "P/L deposits end" then a separate "0.00 USD" for start
        srtd = sorted(nums_usd[:4]) if len(nums_usd) >= 4 else sorted(nums_usd[:3] + [0.0])
        start = srtd[0]
        pl    = srtd[1]
        deposits = srtd[2]
        end   = srtd[3] if len(srtd) > 3 else (deposits + pl)
    return {
        "start_value":  start,
        "end_value":    end,
        "total_pl":     pl,
        "net_deposits": deposits,
        "period_from":  period_from,
        "period_to":    period_to,
    }


def parse_monthly_timeline(pdf) -> dict:
    """Page 3 (Cash/Accruals/Position/Account-value by month) + Page 4 (% return, P/L, costs).
       Both pages have a two-column layout: x<240 is explanatory prose, x>=240 is the
       data table.  We crop to the right column before extracting rows."""
    rows3 = page_rows(pdf.pages[2].crop((240, 0, pdf.pages[2].width, pdf.pages[2].height)))
    rows4 = page_rows(pdf.pages[3].crop((240, 0, pdf.pages[3].width, pdf.pages[3].height)))

    # Collect date headers in order of appearance (page 3)
    date_set, dates = set(), []
    for r in rows3 + rows4:
        for tok in r:
            m = re.match(r"^(\d{1,2}-\w{3}-\d{4})$", tok)
            if m and tok not in date_set:
                date_set.add(tok); dates.append(tok)
    # We expect 7 monthly columns in the table; keep the first 7 we find
    dates = dates[:7]
    labels = []
    for i, d in enumerate(dates):
        mo = d.split("-")[1]
        yr = d.split("-")[2][-2:]
        labels.append(f"{mo}-{yr}" if i in (0, len(dates) - 1) else mo)

    def row_starting_with(rows, label_tokens):
        """Find a row whose first tokens match label_tokens AND contains numeric data.
           If the label sits on a row by itself (e.g. 'Benchmark') the data row often
           follows immediately — we fall back to that row."""
        target = "".join(label_tokens).lower()

        def floats_in(r):
            out = []
            for t in r:
                try:
                    out.append(float(t.replace(",", "").replace("%", "")))
                except ValueError:
                    continue
            return out

        for i, r in enumerate(rows):
            if not "".join(r).lower().startswith(target):
                continue
            vals = floats_in(r)
            if vals:
                return vals
            # label was on its own line — try the very next row
            if i + 1 < len(rows):
                vals = floats_in(rows[i + 1])
                if vals:
                    return vals
            # not a match — keep scanning for a later row that does contain data
        return []

    # Page 3 rows
    cash_vals  = row_starting_with(rows3, ["Cash"])[:7]
    acct_vals  = row_starting_with(rows3, ["Accountvalue"])[:7]

    # Page 4 rows
    pct_vals       = row_starting_with(rows4, ["%Return"])[:6]
    bench_vals     = row_starting_with(rows4, ["Benchmark"])[:6]
    pl_vals        = row_starting_with(rows4, ["TotalP/L"])[:6]
    cost_vals      = row_starting_with(rows4, ["Totalcosts"])[:6]

    # Pad missing months so all arrays line up at len(labels)
    def pad(arr, n, fill=0.0):
        return (arr + [fill] * n)[:n]

    n = len(labels)
    cash  = pad(cash_vals, n)
    acct  = pad(acct_vals, n)
    # monthly arrays are over months only — first label is the start date (Nov-25) with 0% / 0 P&L
    pct   = [0.0] + pad(pct_vals,  n - 1)
    bench = [0.0] + pad(bench_vals,n - 1)
    pl    = [0.0] + pad(pl_vals,   n - 1)
    cost  = [0.0] + pad(cost_vals, n - 1)

    def cum(monthlies):
        c, out = 1.0, []
        for r in monthlies:
            c *= 1 + r / 100
            out.append(round((c - 1) * 100, 2))
        return out

    return {
        "labels":               labels,
        "cash_balance":         cash,
        "account_value":        acct,
        "monthly_pct_return":   pct,
        "monthly_bench_return": bench,
        "monthly_pl":           pl,
        "monthly_cost":         cost,
        "port_return_cum_pct":  cum(pct),
        "snp_return_cum_pct":   cum(bench),
        "cash_deployed":        [round(av - sum(pl[: i + 1]), 2) for i, av in enumerate(acct)],
    }


# ── data-row pattern for P/L breakdown rows (pages 5-7)
#    "<name>  <income>  <costs>  <P/L>  <ret>%"  where name may have multi-word
PL_ROW_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9&.,/()\- ]+?)\s+"
    r"(?P<income>-?\d{1,3}(?:,\d{3})*\.\d{2})\s+"
    r"(?P<costs>-?\d{1,3}(?:,\d{3})*\.\d{2})\s+"
    r"(?P<pl>-?\d{1,3}(?:,\d{3})*\.\d{2})\s+"
    r"(?P<ret>-?\d+\.\d+)%\s*$"
)


def parse_pl_breakdown(pdf) -> list[dict]:
    """Pages 5, 6, 7 — extract per-instrument P&L rows.

    Previous approach cropped at x=300 which sliced off the FRONT of every
    instrument name (leaving fragments like 'ClassC', 'nc.', '500GrowthETF').
    The regex pattern is specific enough (ends with 4 numbers + %) that we
    don't need the left crop — relying on pattern matching alone is safer.
    """
    rows_out = []
    for page_idx in (4, 5, 6):  # pages 5, 6, 7 → zero-indexed
        if page_idx >= len(pdf.pages):
            continue
        page = pdf.pages[page_idx]
        # Crop at x=50 (not 300 which truncated names, not 0 which drops some rows).
        cropped = page.crop((50, 0, page.width, page.height))
        for row in page_rows(cropped):
            line = " ".join(row)
            if not line or "Page" in line or "DohaBank" in line:
                continue
            m = PL_ROW_RE.match(line)
            if not m:
                continue
            name = m.group("name").strip()
            # Skip aggregate/header rows but NOT instrument names that start with "Total"
            # (e.g. "TotalEnergiesSE" is a valid instrument, "Total P/L" is not)
            nl = name.lower()
            if nl.startswith(("total ", "totalp", "totalc", "instrument", "p/l ")):
                continue
            rows_out.append({
                "name":    name,
                "income":  float(m.group("income").replace(",", "")),
                "costs":   float(m.group("costs").replace(",", "")),
                "pl":      float(m.group("pl").replace(",", "")),
                "ret_pct": float(m.group("ret")),
            })
    return rows_out


def parse_holdings(pdf) -> list[dict]:
    """Pages 8, 9, 10 — for each holding the name sits one row above the numeric row.
       Numeric row pattern (after currency tag): qty conv open current %chg upl mv mv%."""
    out = []
    DATA_RE = re.compile(
        r"^USD\s+"
        r"(?P<qty>-?\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s+"
        r"(?P<conv>\d+\.\d+)\s+"
        r"(?P<open>\d{1,3}(?:,\d{3})*\.\d+)\s+"
        r"(?P<cur>\d{1,3}(?:,\d{3})*\.\d+)\s+"
        r"(?P<chg>-?\d+\.\d+)%\s+"
        r"(?P<upl>-?\d{1,3}(?:,\d{3})*\.\d+)\s+"
        r"(?P<mv>\d{1,3}(?:,\d{3})*\.\d+)\s+"
        r"(?P<wt>\d+\.\d+)%\s*$"
    )
    for page_idx in (7, 8, 9):  # pages 8, 9, 10
        if page_idx >= len(pdf.pages):
            continue
        rows = page_rows(pdf.pages[page_idx])
        for i, row in enumerate(rows):
            line = " ".join(row)
            m = DATA_RE.match(line)
            if not m:
                continue
            # Name is on the row above (or two above for short names)
            name_line = " ".join(rows[i - 1]) if i > 0 else ""
            # Strip the trailing "(ISIN:" piece if it's at the end
            name_clean = re.sub(r"\(ISIN:.*$", "", name_line).strip()
            if not name_clean and i > 1:
                name_clean = re.sub(r"\(ISIN:.*$", "", " ".join(rows[i - 2])).strip()
            ticker, yf, cls = lookup(name_clean)
            qty = float(m.group("qty").replace(",", ""))
            out.append({
                "tk":    ticker,
                "yf":    yf,
                "name":  name_clean,
                "cls":   cls,
                "qty":   int(qty) if qty.is_integer() else qty,
                "avg":   round(float(m.group("open").replace(",", "")), 4),
                "_statement_ltp": round(float(m.group("cur").replace(",", "")), 4),
                "_statement_upl": round(float(m.group("upl").replace(",", "")), 2),
            })
    return out


def parse_cash(pdf) -> float:
    """Page 10 'Allaccounts USD <value> <%>'."""
    rows = page_rows(pdf.pages[9])
    for r in rows:
        line = " ".join(r).replace(",", "")
        m = re.search(r"Allaccounts\s+USD\s+(-?\d+\.\d+)", line)
        if m:
            return float(m.group(1))
    return 0.0


def parse_charges(pdf) -> list[dict]:
    """Cost summary page — search all pages for the charges section."""
    label_map = [
        ("Commission",                   "Commission"),
        ("ClientCustodyFee",             "Custody Fee (ongoing)"),
        ("ExchangeFee",                  "Exchange Fee"),
        ("FrenchFinancialTransaction",   "French FTT"),
        ("Externalproductcosts",         "External Product Costs (implicit)"),
    ]
    # Find the page containing "Cost summary" rather than hardcoding page index
    cost_page_rows = []
    for page in pdf.pages:
        text = "".join("".join(r) for r in page_rows(page))
        if "Costsummary" in text or "Commission" in text and "USD" in text:
            cost_page_rows = page_rows(page)
            break
    out = []
    for r in cost_page_rows:
        joined = "".join(r)
        for needle, pretty in label_map:
            if needle in joined:
                m = re.search(r"(-?\d{1,3}(?:,\d{3})*\.\d{2})USD", joined)
                if m:
                    out.append({"type": pretty, "amt": float(m.group(1).replace(",", ""))})
                break
    return out


# ───────────────────────── BUILD ─────────────────────────
def build_cost_json(pdf_path: Path, prev: dict | None) -> dict:
    with pdfplumber.open(pdf_path) as pdf:
        summary  = parse_account_summary(pdf)
        timeline = parse_monthly_timeline(pdf)
        pl_rows  = parse_pl_breakdown(pdf)
        holdings = parse_holdings(pdf)
        cash     = parse_cash(pdf)
        charges  = parse_charges(pdf)

    # ── Match P&L rows to positions by TICKER (robust vs truncated names) ──
    #
    # The P&L breakdown (pages 5-7) is per-instrument for the whole period.
    # Instruments that were closed AND reopened (e.g. VOOG, EWY) appear once
    # in the statement with combined costs.  We handle this by:
    #   • If ticker is currently OPEN  → costs go to open position
    #   • If ticker is only CLOSED     → costs go to closed entry
    # This avoids double-counting fees for re-opened positions.

    open_tickers  = {h["tk"] for h in holdings}

    # Build ticker → pl_row map. Sum costs if same ticker appears twice.
    pl_by_ticker: dict[str, dict] = {}
    for r in pl_rows:
        tk, _, _ = lookup(r["name"])
        if tk in pl_by_ticker:
            # Same ticker found twice (e.g. split rows): merge costs
            pl_by_ticker[tk]["costs"]   += r["costs"]
            pl_by_ticker[tk]["income"]  += r["income"]
            pl_by_ticker[tk]["pl"]      += r["pl"]
        else:
            pl_by_ticker[tk] = dict(r)

    open_full = []
    for h in holdings:
        plr = pl_by_ticker.get(h["tk"], {})
        open_full.append({
            "tk":     h["tk"],
            "yf":     h["yf"],
            "name":   h["name"],
            "cls":    h["cls"],
            "qty":    h["qty"],
            "avg":    h["avg"],
            "fees":   round(plr.get("costs", 0.0), 2),
            "income": round(plr.get("income", 0.0), 2),
        })

    closed_full = []
    for tk, r in pl_by_ticker.items():
        if tk in open_tickers:
            # Fees for this ticker already captured in the open position.
            # Still record the closed tranche's realised P&L (costs=0 to avoid double-count).
            if r["pl"] != 0.0 and not tk.startswith("UNKNOWN"):
                _, yf, cls = lookup(r["name"])
                closed_full.append({
                    "tk":       tk,
                    "name":     r["name"],
                    "cls":      cls,
                    "income":   round(r["income"], 2),
                    "costs":    0.0,   # costs already in open position; 0 here to avoid double-count
                    "realised": round(r["pl"], 2),
                    "ret_pct":  round(r["ret_pct"], 2),
                    "_note":    "costs counted in open position (same ticker, re-opened)",
                })
            continue
        _, yf, cls = lookup(r["name"])
        closed_full.append({
            "tk":       tk,
            "name":     r["name"],
            "cls":      cls,
            "income":   round(r["income"], 2),
            "costs":    round(r["costs"], 2),
            "realised": round(r["pl"], 2),
            "ret_pct":  round(r["ret_pct"], 2),
        })

    out = prev or {}
    out["as_of"]      = summary.get("period_to") or out.get("as_of")
    out["fx_inr_usd"] = out.get("fx_inr_usd", 83.5)
    out["us"] = {
        "broker":                  "Doha Bank Global",
        "cash":                    round(cash, 2),
        "cash_infusion_itd":       round(summary["net_deposits"], 2),
        "account_value_statement": round(summary["end_value"], 2),
        "total_pl_statement":      round(summary["total_pl"], 2),
        "monthly":                 timeline,
        "open":                    open_full,
        "closed":                  closed_full,
        "charges_breakdown":       charges,
    }
    out.setdefault("india", {"open": [], "note": "Awaiting broker statement parser"})
    return out


# ───────────────────────── CLI ─────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="Parse Doha Bank statement → holdings_cost.json")
    ap.add_argument("pdf", help="Path to broker statement PDF")
    ap.add_argument("--output", "-o", default="data/holdings_cost.json")
    args = ap.parse_args()

    pdf = Path(args.pdf).expanduser()
    out_path = Path(args.output).expanduser()
    if not pdf.exists():
        sys.exit(f"file not found: {pdf}")

    prev = json.loads(out_path.read_text()) if out_path.exists() else None
    print(f"Parsing {pdf} …")
    cost = build_cost_json(pdf, prev)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(cost, indent=2, ensure_ascii=False))

    us = cost["us"]
    print(f"\nParsed snapshot as of {cost['as_of']}:")
    print(f"  account value      ${us['account_value_statement']:>11,.2f}")
    print(f"  cash               ${us['cash']:>11,.2f}")
    print(f"  cash infusion ITD  ${us['cash_infusion_itd']:>11,.2f}")
    print(f"  total P/L          ${us['total_pl_statement']:>11,.2f}")
    print(f"  open positions     {len(us['open']):>11d}")
    print(f"  closed positions   {len(us['closed']):>11d}")
    print(f"  charges entries    {len(us['charges_breakdown']):>11d}")
    print(f"  monthly periods    {len(us['monthly']['labels']):>11d}")
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
